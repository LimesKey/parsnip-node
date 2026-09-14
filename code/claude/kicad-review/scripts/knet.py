#!/usr/bin/env python3
"""
knet.py v3 - query + review tool for KiCad netlists (.net, S-expr, KiCad 6-10).

Indexes built from the netlist:
  comps    ref -> value, footprint, sheet, libsource, props, dnp, in_bom, lcsc
  libparts (lib,part) -> {pin num: (name, type)}   pins the SYMBOL declares
  nets     net name -> [nodes]
  pinnet   (ref,pin) -> net                        pins actually CONNECTED
  cpins    ref -> {pin: net}                       fast per-part lookup
The gap between libparts and pinnet is where floating-pin bugs live.
If *.kicad_sch files sit next to the netlist they are parsed as a sidecar:
no_connect flags, schematic text notes, and symbol positions become available
(self-validated; silently disabled if the geometry does not check out).

Query:
  knet.py FILE summary                  overview, sheets, rails, DNP list
  knet.py FILE comp U4 R13 ...          pin table w/ net per pin
  knet.py FILE net '\\+5V|VCC_RF'        nets by regex, with node values
  knet.py FILE pin U2.9                 net at a pin + everything else on it
  knet.py FILE around U8                comp + pin + peers in one call (use first)
  knet.py FILE find 'BQ29|0603'         search refs/values/footprints/props/nets
  knet.py FILE bom                      grouped BOM, DNP separated (board-wide by default)
  knet.py FILE bom --sheet /Root/Rails/ ...same, scoped to one sheet and its children
  knet.py FILE sheets                   components grouped by hierarchical sheet
  knet.py FILE rails                    every power rail: source, loads, decoupling
  knet.py FILE divider U5.OVLO          2-resistor: nominal + worst-case trip voltage
                                         from resistor tolerance, computed directly.
                                         3-resistor UVLO/OVLO string: pass --vth (and
                                         --vth-tol) for the IC's comparator threshold
  knet.py FILE notes                    schematic text notes grouped by sheet

Connectivity:
  knet.py FILE walk R15 -d 3            expand outward through 2-pin passives
  knet.py FILE path R13 U2              shortest electrical path between two parts
  knet.py FILE unconnected              floating pins (NC-flag aware) + solo nets
  knet.py FILE draw U8 -d 2 -o u8.svg   KiCad-style schematic of a part or net
  knet.py FILE draw U8 --spec           ...as an editable ksch spec (see ksch.py)

Review:
  knet.py FILE check                    rule-based schematic review (see RULES)
  knet.py FILE check --only DOMAIN,DECOUPLE
  knet.py FILE check --since old.net    only NEW findings vs an older export
  knet.py FILE diff old.net             what changed vs another export (rename-safe)

Flags:
  --through R,L,FB,F,FL,JP  ref prefixes treated as pass-through (add C, D as needed)
  --fanout N                nets with >N nodes are rails: reported, not expanded (8)
  --as-drawn                traverse DNP parts too (default: DNP = open circuit)
  --rail '+5V=5,VBUS=5'     override/add rail voltages for the domain checks
  --peer-max N              cap peer lists in `around` (12)
  -o FILE                   output path for `draw`
  --json                    machine-readable output (summary/check/rails/diff/bom)
  --only / --skip           comma list of rule ids for `check`
  --sheet PATH              scope `bom` to one hierarchical sheet + its children
  --vth V / --vth-tol PCT   IC comparator threshold + accuracy for a 3-resistor `divider`

Project config: a knet.json next to the netlist persists defaults, e.g.
  {"rails": {"+BATT": 8.4}, "through": "R,L,FB,F,FL,JP,C", "fanout": 8,
   "suppress": ["RFSTUB:GPS_ANT", "OCNOPULL:U9"]}
Precedence: built-in defaults < knet.json < command line.

`suppress` mutes `check` findings you've already confirmed are not bugs, so
they stop costing tokens every single review instead of you re-stating "don't
re-flag this" each session. Each entry is "RULE" (mute the whole rule) or
"RULE:TOKEN" (mute only findings naming that ref or net, e.g. a false-positive
RFSTUB on one antenna net does not hide a real one elsewhere). `check` reports
how many were muted; `--no-suppress` shows everything, e.g. for a fresh audit
after a big rewire.

Exit codes: 0 clean, 1 not found, 2 `check` found an ERROR (or `draw` produced a
diagram that fails its own layout/netlist verification), 3 bad file/spec.
"""
import sys, os, re, json, argparse
from collections import defaultdict, deque

from kcommon import *          # parser, helpers, Netlist/SchInfo, print_findings
from kcommon import _fkey      # underscore name c_diff uses; import * skips it

def c_notes(nl, a):
    s = nl.sch()
    if not s.files:
        print("no .kicad_sch files found next to the netlist"); return 1
    if not s.notes:
        print("no text notes in the schematic"); return
    bysheet = defaultdict(list)
    for path, x, y, text in s.notes:
        bysheet[path].append((y, x, text))
    for path in sorted(bysheet):
        print(f"\n=== {path}")
        for y, x, text in sorted(bysheet[path]):
            body = text.replace('\n', '\n      ')
            print(f"  ({x:.0f},{y:.0f})  {body}")

# ---------------- basic commands ----------------

def c_summary(nl, a):
    if a.json:
        print(json.dumps({'source': nl.source, 'tool': nl.tool, 'date': nl.date,
                          'comps': len(nl.comps), 'nets': len(nl.nets),
                          'dnp': [r for r, c in nl.comps.items() if c['dnp']],
                          'rails': {n: nl.railv[n] for n in nl.railv}}, indent=1))
        return
    print(f"source : {nl.source}")
    print(f"tool   : {nl.tool}   exported {nl.date}")
    print(f"comps  : {len(nl.comps)}   nets: {len(nl.nets)}   libparts: {len(nl.libparts)}")
    print("\nsheets:")
    for num, name in nl.sheets:
        cnt = sum(1 for c in nl.comps.values() if c['sheet'] == name)
        print(f"  {num:>2}  {name:<24} {cnt} comps")
    byprefix = defaultdict(list)
    for ref in nl.comps:
        byprefix[nl.comps[ref]['prefix']].append(ref)
    print("\nby prefix:")
    for p in sorted(byprefix):
        print(f"  {p:<4} {len(byprefix[p]):>3}  {refrange(byprefix[p])}")
    print("\npower rails (name-derived):")
    for n in sorted(nl.railv, key=lambda x: -nl.railv[x]):
        print(f"  {nl.railv[n]:>5.2f} V  {n:<24} {len(nl.nets.get(n,[]))} nodes")
    dnp = sorted([r for r, c in nl.comps.items() if c['dnp']], key=natkey)
    print(f"\nDNP / not populated ({len(dnp)}): {' '.join(dnp) or 'none'}")
    print("\nlargest nets:")
    for name, nodes in sorted(nl.nets.items(), key=lambda kv: -len(kv[1]))[:8]:
        print(f"  {len(nodes):>3}  {name}")

def c_comp(nl, a):
    miss = False
    for ref in a.args:
        c = nl.comps.get(ref)
        if not c:
            print(f"{ref}: NOT FOUND (try `find {ref}`)"); miss = True; continue
        print(f"\n=== {ref}  {c['value']}{'   [DNP - NOT POPULATED]' if c['dnp'] else ''} ===")
        print(f"  footprint : {c['footprint']}")
        print(f"  symbol    : {c['lib']}:{c['part']}")
        print(f"  sheet     : {c['sheet']}")
        if c['description']:
            print(f"  desc      : {c['description']}")
        if c['lcsc']:
            print(f"  LCSC      : {c['lcsc']}")
        for k in ('MPN', 'Manufacturer', 'Voltage Rating', 'Tolerance', 'Datasheet'):
            if isinstance(c['props'].get(k), str) and c['props'][k]:
                print(f"  {k:<10}: {c['props'][k]}")
        rail, rv = nl.supply_rail(ref)
        if rail:
            print(f"  supply    : {rail} ({rv} V)")
        pins = nl.sympins(ref)
        allnums = sorted(set(pins) | set(nl.cpins.get(ref, {})), key=natkey)
        print(f"  {'pin':<5} {'name':<16} {'type':<12} {'nodes':<6} net")
        for num in allnums:
            nm, ty = pins.get(num, ('?', '?'))
            net = nl.cpins.get(ref, {}).get(num)
            cnt = len(nl.nets.get(net, [])) if net else 0
            flag = ''
            if not net:
                flag = '   <-- NOT IN ANY NET'
            elif net.startswith('unconnected-'):
                flag = '   <-- floating'
            elif cnt == 1:
                flag = '   <-- SINGLE NODE'
            print(f"  {num:<5} {nm:<16} {ty:<12} {cnt:<6} {net or '(none)'}{flag}")
    return 1 if miss else 0

def c_net(nl, a):
    q = a.args[0]
    if q in nl.nets:
        hits = [q]
    else:
        hits = [n for n in nl.nets if n.split('/')[-1].lower() == q.lower()]
        if not hits:
            pat = smart_re(q)
            hits = [n for n in nl.nets if pat.search(n)]
    if not hits:
        near = [n for n in nl.nets if q.lower().strip('+/') in n.lower()][:6]
        print(f"no net matches {q!r}" + (f"; closest: {', '.join(near)}" if near else
              "; run `summary` or `find` to list nets"))
        return 1
    for name in sorted(hits, key=natkey):
        nodes = nl.nets[name]
        cls = nl.netclass.get(name, '')
        rv = f"  rail {nl.railv[name]} V" if name in nl.railv else ''
        print(f"\n=== {name}   [{cls}]  {len(nodes)} nodes{rv}")
        for nd in sorted(nodes, key=lambda x: (natkey(x['ref']), natkey(x['pin']))):
            c = nl.comps.get(nd['ref'], {})
            print(f"  {nd['ref']:<6} pin {nd['pin']:<4} {nd['fn'] or '':<16} "
                  f"{nd['type']:<12} {c.get('value','')}{nl.tag(nd['ref'])}")

def c_pin(nl, a):
    for spec in a.args:
        ref, _, pin = spec.partition('.')
        pin = nl.resolve_pin(ref, pin) or pin
        net = nl.cpins.get(ref, {}).get(pin)
        if not net:
            print(f"{spec} ({nl.pinname(ref,pin) or '?'}): NOT CONNECTED TO ANY NET")
            continue
        print(f"\n{spec} [{nl.pinname(ref,pin)}] -> {net}  ({len(nl.nets[net])} nodes)")
        for nd in sorted(nl.nets[net], key=lambda x: natkey(x['ref'])):
            if (nd['ref'], nd['pin']) == (ref, pin):
                continue
            print(f"    {nd['ref']:<6} pin {nd['pin']:<4} {nd['fn'] or '':<16} "
                  f"{nl.value(nd['ref'])}{nl.tag(nd['ref'])}")

def c_find(nl, a):
    pat = smart_re(a.args[0])
    hit = 0
    comps = [(ref, c) for ref, c in sorted(nl.comps.items(), key=lambda kv: natkey(kv[0]))
             if pat.search(' '.join([ref, c['value'], c['footprint'], c['description'],
                                     c['lib'], c['part']]
                                    + [f"{k}={v}" for k, v in c['props'].items()
                                       if isinstance(v, str)]))]
    if comps:
        print("components:")
        for ref, c in comps:
            print(f"  {ref:<6} {c['value']:<26} {trunc(c['footprint'],44)}{nl.tag(ref)}")
        hit += len(comps)
    pins = [(r, p, nl.pinname(r, p)) for (r, p) in sorted(nl.pinnet, key=lambda k: natkey(k[0]))
            if nl.pinname(r, p) and pat.search(nl.pinname(r, p))]
    if pins:
        print("pin functions:")
        for r, p, fn in pins:
            print(f"  {r}.{p:<5} {fn:<24} -> {nl.pinnet[(r,p)]}")
        hit += len(pins)
    nets = [n for n in sorted(nl.nets, key=natkey) if pat.search(n)]
    if nets:
        print("nets:")
        for n in nets:
            print(f"  {n:<34} {len(nl.nets[n])} nodes")
        hit += len(nets)
    if not hit:
        print(f"no match for {a.args[0]!r}")
        return 1

def c_bom(nl, a):
    # bom is board-wide by default; --sheet scopes it to one hierarchical sheet
    # (and everything nested under it), reusing `sheets`' own per-sheet grouping
    # rather than re-deriving sheet membership here.
    sf = (getattr(a, 'sheet', '') or '').strip()
    sf = (sf if sf.endswith('/') else sf + '/') if sf else ''
    groups = defaultdict(list)
    for ref, c in nl.comps.items():
        if not c['in_bom']:
            continue
        if sf and not (c['sheet'] if c['sheet'].endswith('/') else c['sheet'] + '/').startswith(sf):
            continue
        groups[(c['dnp'], c['value'], c['footprint'], c['lcsc'])].append(ref)
    if a.json:
        print(json.dumps([{'qty': len(v), 'dnp': k[0], 'value': k[1], 'footprint': k[2],
                           'lcsc': k[3], 'refs': sorted(v, key=natkey)}
                          for k, v in groups.items()], indent=1))
        return
    place = sum(len(v) for k, v in groups.items() if not k[0])
    skip = sum(len(v) for k, v in groups.items() if k[0])
    print(f"{'qty':<4} {'value':<24} {'LCSC':<11} {'footprint':<30} refs")
    for dnp in (False, True):
        sel = {k: v for k, v in groups.items() if k[0] is dnp}
        if not sel:
            continue
        print(f"\n--- {'DO NOT POPULATE' if dnp else 'POPULATE'} ---")
        for (_, v, fp, lcsc), refs in sorted(sel.items(), key=lambda kv: -len(kv[1])):
            print(f"{len(refs):<4} {v:<24} {lcsc:<11} {trunc(fp.split(':')[-1],30):<30} "
                  f"{refrange(refs)}")
    print(f"\ntotal placements: {place}   DNP: {skip}")
    nolcsc = sorted([r for r, c in nl.comps.items()
                     if c['in_bom'] and not c['dnp'] and not c['lcsc']
                     and c['prefix'] not in ('H', 'TP', 'J', 'BT', 'SW')
                     and (not sf or (c['sheet'] if c['sheet'].endswith('/') else c['sheet'] + '/').startswith(sf))],
                    key=natkey)
    if nolcsc:
        print(f"no LCSC part number: {' '.join(nolcsc)}")

def c_sheets(nl, a):
    bysheet = defaultdict(list)
    for ref, c in nl.comps.items():
        bysheet[c['sheet']].append(ref)
    for s in sorted(bysheet):
        print(f"\n{s}  ({len(bysheet[s])})")
        for ref in sorted(bysheet[s], key=natkey):
            print(f"  {ref:<6} {nl.comps[ref]['value']}{nl.tag(ref)}")

# ---------------- rails ----------------

def caps_on(nl, net):
    """Capacitors bridging `net` to ground: [(ref, farads, dnp)]."""
    out = []
    for nd in nl.nets.get(net, []):
        r = nd['ref']
        if nl.comps.get(r, {}).get('prefix') != 'C':
            continue
        op = nl.other_pin(r, nd['pin'])
        if op and nl.is_gnd(nl.cpins[r].get(op, '')):
            out.append((r, parse_value(nl.value(r), 'C'), nl.comps[r]['dnp']))
    return out

def sources_of(nl, net):
    """What can drive this net: active drivers, batteries/connectors, and upstream
    passives (a ferrite or resistor whose far side is a different net)."""
    out, seen = [], set()
    for nd in nl.nets.get(net, []):
        ref, t = nd['ref'], nl.pintype(nd['ref'], nd['pin'])
        pfx = nl.comps.get(ref, {}).get('prefix')
        if t in ('power_out', 'output') or pfx in ('BT',):
            out.append((ref, nd['pin'], nd['fn'] or nl.pinname(ref, nd['pin']), t or 'source'))
        elif pfx in ('L', 'FB', 'F', 'FL', 'JP') or (
                pfx == 'R' and (parse_value(nl.value(ref), 'R') or 1e9) <= 10):
            if nl.no_dnp and nl.comps.get(ref, {}).get('dnp'):
                continue
            if len(nl.conn_pins(ref)) != 2:
                continue
            op = nl.other_pin(ref, nd['pin'])
            far = nl.cpins[ref].get(op) if op else None
            if far and far != net and not nl.is_gnd(far) and (ref, far) not in seen:
                seen.add((ref, far))
                out.append((ref, nd['pin'], f"fed from {far}", 'via ' + nl.value(ref)))
    if not out:   # symbols that declare their supply output as a plain passive pin
        for nd in nl.nets.get(net, []):
            nm = (nd['fn'] or nl.pinname(nd['ref'], nd['pin']) or '').upper()
            if re.search(r'VOUT|OUT|3V3|5V|VDD', nm) and nl.pintype(nd['ref'], nd['pin']) != 'power_in':
                out.append((nd['ref'], nd['pin'], nm, 'passive pin, likely the source'))
    return out

def c_rails(nl, a):
    rows = []
    for net in sorted(nl.railv, key=lambda n: (-nl.railv[n], n)):
        if nl.is_gnd(net):
            continue
        cs = caps_on(nl, net)
        tot = sum(v for _, v, d in cs if v and not d)
        loads = sorted({nd['ref'] for nd in nl.nets[net]
                        if nl.comps.get(nd['ref'], {}).get('prefix') not in ('C',)}, key=natkey)
        rows.append({'net': net, 'volts': nl.railv[net], 'nodes': len(nl.nets[net]),
                     'caps': [(r, v, d) for r, v, d in cs], 'total_C': tot,
                     'sources': sources_of(nl, net), 'loads': loads})
    if a.json:
        print(json.dumps(rows, indent=1)); return
    for r in rows:
        print(f"\n=== {r['net']}   {r['volts']} V   {r['nodes']} nodes")
        if r['sources']:
            for ref, pin, fn, t in r['sources']:
                print(f"  source : {ref}.{pin} {fn} ({t}) [{nl.value(ref)}]")
        else:
            print("  source : none found (derived through passives - check `walk`)")
        cs = ' '.join(f"{ref}={eng(v,'F')}{'(DNP)' if d else ''}" for ref, v, d in r['caps'])
        print(f"  decoup : {eng(r['total_C'],'F') if r['total_C'] else 'NONE'}   {cs}")
        print(f"  loads  : {' '.join(r['loads'])}")

# ---------------- divider / trip-voltage math ----------------

_TOLRE = re.compile(r'([0-9.]+)\s*%')

def res_tolerance(c):
    """Fractional tolerance (0.01 for 1%) from a 'Tolerance' property, or a
    trailing percent in Value ('402k 0.1%'). None if stated nowhere - callers
    must not assume a default, since silently guessing 1%/5% would misreport
    a worst-case trip voltage as if it were backed by the actual BOM."""
    t = c['props'].get('Tolerance')
    if isinstance(t, str):
        m = _TOLRE.search(t)
        if m:
            return float(m.group(1)) / 100.0
    m = _TOLRE.search(c.get('value') or '')
    return float(m.group(1)) / 100.0 if m else None

def divider_legs(nl, net, fanout=8):
    """2-pin resistors touching `net` (a suspected divider tap), classified by
    what their far end reaches: [(ref, kind, far, volts_or_None, ohms_or_None)]
    with kind in 'rail'|'gnd'|'other'. Only resistors, only DNP-aware,
    matching the pass-through convention used everywhere else in this file."""
    out = []
    for nd in nl.nets.get(net, []):
        r = nd['ref']
        if nl.comps.get(r, {}).get('prefix') != 'R':
            continue
        if nl.no_dnp and nl.comps[r]['dnp']:
            continue
        if len(nl.conn_pins(r)) != 2:
            continue
        op = nl.other_pin(r, nd['pin'])
        far = nl.cpins[r].get(op) if op else None
        if not far or far == net:
            continue
        ohms = parse_value(nl.value(r), 'R')
        if far in nl.railv and nl.railv[far] > 0:
            out.append((r, 'rail', far, nl.railv[far], ohms))
        elif nl.is_gnd(far):
            out.append((r, 'gnd', far, 0.0, ohms))
        else:
            out.append((r, 'other', far, None, ohms))
    return out

def try_3r_divider(nl, tap1, legs1, fanout=8):
    """Detect TI's 3-resistor UVLO/OVLO string: SOURCE -rail-> R1 -> tap1 -> R2 ->
    tap2 -> R3 -> GND, with some U/IC part's pins landing on both taps (`legs1` is
    already computed as divider_legs(nl, tap1, fanout)). None if the shape isn't
    there - a bare 2-taps-in-series network with no IC on the second tap is just
    an attenuator, not this specific UVLO/OVLO topology."""
    rails = [l for l in legs1 if l[1] == 'rail']
    others = [l for l in legs1 if l[1] == 'other']
    if len(legs1) != 2 or len(rails) != 1 or len(others) != 1:
        return None
    r1, _, _, vtop, ohms1 = rails[0]
    r2, _, tap2, _, ohms2 = others[0]
    legs2 = divider_legs(nl, tap2, fanout)
    gnds2 = [l for l in legs2 if l[1] == 'gnd']
    loops = [l for l in legs2 if l[0] == r2 and l[2] == tap1]
    if len(legs2) != 2 or len(gnds2) != 1 or len(loops) != 1:
        return None
    r3, _, _, _, ohms3 = gnds2[0]
    if not (ohms1 and ohms2 and ohms3):
        return None
    ic = next((ref for ref, c in nl.comps.items()
               if c['prefix'] in ('U', 'IC')
               and tap1 in nl.cpins.get(ref, {}).values()
               and tap2 in nl.cpins.get(ref, {}).values()), None)
    if not ic:
        return None
    return {'ic': ic, 'r1': r1, 'ohms1': ohms1, 'tap1': tap1, 'vtop': vtop,
            'r2': r2, 'ohms2': ohms2, 'tap2': tap2, 'r3': r3, 'ohms3': ohms3}

def c_divider(nl, a):
    spec = a.args[0]
    if '.' in spec and spec.partition('.')[0] in nl.comps:
        ref, _, pin = spec.partition('.')
        pin = nl.resolve_pin(ref, pin) or pin
        net = nl.cpins.get(ref, {}).get(pin)
        if not net:
            print(f"{spec}: not on any net"); return 1
    elif spec in nl.nets:
        net = spec
    else:
        alt = next((n for n in nl.nets if n.split('/')[-1].lower() == spec.lower()), None)
        if not alt:
            print(f"no net or pin named {spec!r}"); return 1
        net = alt
    legs = divider_legs(nl, net, a.fanout)
    if a.json:
        print(json.dumps({'net': net, 'legs': legs}, indent=1)); return
    print(f"{net}  ({len(legs)} resistor leg(s) directly on this net)")
    for r, kind, far, v, ohms in legs:
        tol = res_tolerance(nl.comps[r])
        tolstr = (f"  tol={tol*100:g}%" if tol is not None else
                  "  tol=? (no 'Tolerance' property or 'N%' in Value)")
        rv = f"  ({v:g} V)" if kind == 'rail' else ''
        print(f"  {r:<6} {eng(ohms,chr(0x2126)) if ohms else nl.value(r):<10} -> "
              f"{kind:<4} {far}{rv}{tolstr}{nl.tag(r)}")
    rails = [l for l in legs if l[1] == 'rail']
    gnds = [l for l in legs if l[1] == 'gnd']
    others = [l for l in legs if l[1] == 'other']
    if len(legs) == 2 and len(rails) == 1 and len(gnds) == 1:
        rtop, _, _, vtop, ohms_top = rails[0]
        rbot, _, _, _, ohms_bot = gnds[0]
        if not (ohms_top and ohms_bot):
            print("\ncould not parse both resistor values as numbers"); return
        ratio = ohms_bot / (ohms_top + ohms_bot)
        vnom = vtop * ratio
        print(f"\nsimple 2-resistor divider: {rtop} (top, {eng(ohms_top,chr(0x2126))}, "
              f"rail-side) / {rbot} (bottom, {eng(ohms_bot,chr(0x2126))}, GND-side), fed from {vtop:g} V")
        print(f"  V_tap nominal = {vtop:g} * {rbot}/({rtop}+{rbot}) = {vnom:.4f} V")
        ttop, tbot = res_tolerance(nl.comps[rtop]), res_tolerance(nl.comps[rbot])
        if ttop is not None and tbot is not None:
            rt_lo, rt_hi = ohms_top * (1 - ttop), ohms_top * (1 + ttop)
            rb_lo, rb_hi = ohms_bot * (1 - tbot), ohms_bot * (1 + tbot)
            vmax = vtop * rb_hi / (rt_lo + rb_hi)
            vmin = vtop * rb_lo / (rt_hi + rb_lo)
            print(f"  worst case ({rtop} {ttop*100:g}%, {rbot} {tbot*100:g}%): "
                  f"{vmin:.4f} - {vmax:.4f} V")
        else:
            print("  worst-case range not computed: tolerance missing on one or both "
                  "resistors (add a 'Tolerance' property or 'N%' in Value)")
        print("  (V_top here is the rail's name-derived or --rail/knet.json voltage - "
              "override it if the regulator's actual output differs from the net name)")
    else:
        d = try_3r_divider(nl, net, legs, a.fanout) if others else None
        if d:
            r1o, r2o, r3o = d['ohms1'], d['ohms2'], d['ohms3']
            rsum = r1o + r2o + r3o
            print(f"\n3-resistor UVLO/OVLO string on {d['ic']}: {d['vtop']:g} V -> "
                  f"{d['r1']} ({eng(r1o,chr(0x2126))}) -> {d['tap1']} -> "
                  f"{d['r2']} ({eng(r2o,chr(0x2126))}) -> {d['tap2']} -> "
                  f"{d['r3']} ({eng(r3o,chr(0x2126))}) -> GND")
            if a.vth is None:
                print("  give --vth <IC comparator threshold from the datasheet, e.g. "
                      "1.223 for TPS2594x> to compute VIN_UV/VIN_OV (add --vth-tol <%> "
                      "for the IC's own threshold accuracy, which usually dominates the "
                      "resistor tolerance)")
                return
            vth = a.vth
            vuv = vth * rsum / (r2o + r3o)
            vov = vth * rsum / r3o
            print(f"  VIN_UV = {vth:g} * ({d['r1']}+{d['r2']}+{d['r3']})/"
                  f"({d['r2']}+{d['r3']}) = {vuv:.4f} V")
            print(f"  VIN_OV = {vth:g} * ({d['r1']}+{d['r2']}+{d['r3']})/{d['r3']} = "
                  f"{vov:.4f} V")
            t1 = res_tolerance(nl.comps[d['r1']])
            t2 = res_tolerance(nl.comps[d['r2']])
            t3 = res_tolerance(nl.comps[d['r3']])
            if None not in (t1, t2, t3):
                vtht = (a.vth_tol or 0.0) / 100.0
                vth_lo, vth_hi = vth * (1 - vtht), vth * (1 + vtht)
                r1_lo, r1_hi = r1o * (1 - t1), r1o * (1 + t1)
                r2_lo, r2_hi = r2o * (1 - t2), r2o * (1 + t2)
                r3_lo, r3_hi = r3o * (1 - t3), r3o * (1 + t3)
                uv_lo = vth_lo * (r1_lo + r2_hi + r3_hi) / (r2_hi + r3_hi)
                uv_hi = vth_hi * (r1_hi + r2_lo + r3_lo) / (r2_lo + r3_lo)
                ov_lo = vth_lo * (r1_lo + r2_lo + r3_hi) / r3_hi
                ov_hi = vth_hi * (r1_hi + r2_hi + r3_lo) / r3_lo
                print(f"  worst case (R tol {d['r1']}={t1*100:g}% {d['r2']}={t2*100:g}% "
                      f"{d['r3']}={t3*100:g}%"
                      + (f", Vth tol {a.vth_tol:g}%" if vtht else "") + "): "
                      f"VIN_UV {uv_lo:.4f}-{uv_hi:.4f} V, VIN_OV {ov_lo:.4f}-{ov_hi:.4f} V")
                if not vtht:
                    print("  (no --vth-tol given - the IC's own threshold accuracy usually "
                          "dominates the resistor tolerance; e.g. TPS25947 is +/-1.7% vs "
                          "typical 0.1% resistors)")
            else:
                print("  worst-case range not computed: tolerance missing on one or more "
                      "of the three resistors")
        elif others:
            print(f"\nnot a simple 2-resistor divider: {len(others)} leg(s) reach a net that is "
                  f"neither a named rail nor GND ({', '.join(o[2] for o in others)}) - could be "
                  f"hysteresis feedback from a digital/output pin, a deeper network, a "
                  f"3-resistor UVLO/OVLO string with no IC pin found on both taps, or simply a "
                  f"rail whose net name this tool doesn't recognise (add it with "
                  f"--rail 'NAME=volts' or knet.json and rerun). `walk {net} -d 2` to trace "
                  f"further either way.")
        else:
            print(f"\n{len(legs)} resistor leg(s) found; automatic trip-voltage math needs "
                  f"exactly one to a named rail and one to GND (2-resistor), or the "
                  f"3-resistor UVLO/OVLO shape")

# ---------------- connectivity ----------------

def expand(nl, net, classes, fanout, force=False):
    out = []
    if not force and (net in nl.railv or (fanout and len(nl.nets.get(net, [])) > fanout)):
        return out
    for nd in nl.nets.get(net, []):
        ref = nd['ref']
        if not nl.is_passthrough(ref, classes, fanout):
            continue
        op = nl.other_pin(ref, nd['pin'])
        if not op:
            continue
        far = nl.cpins[ref].get(op)
        if far and far != net:
            out.append((ref, nd['pin'], op, far))
    return out

def c_walk(nl, a):
    classes = {c.strip().upper() for c in a.through.split(',') if c.strip()}
    spec = a.args[0]
    if spec in nl.nets:
        print(f"{spec}   [{len(nl.nets[spec])} nodes]")
        _walk(nl, spec, 1, a.depth, {spec}, classes, a.fanout, "", force=True)
        return
    ref, _, pin = spec.partition('.')
    if pin and ref in nl.comps:
        pin = nl.resolve_pin(ref, pin) or pin
    if ref not in nl.comps:
        alt = next((n for n in nl.nets if n.lower() == spec.lower()), None)
        if alt:
            print(f"{alt}   [{len(nl.nets[alt])} nodes]")
            _walk(nl, alt, 1, a.depth, {alt}, classes, a.fanout, "", force=True)
            return
        print(f"no net or component named {spec}"); return
    pins = [pin] if pin else sorted(set(nl.cpins.get(ref, {})) | set(nl.sympins(ref)), key=natkey)
    c = nl.comps[ref]
    print(f"{ref}  [{c['value']}]{'  DNP' if c['dnp'] else ''}  {c['footprint']}  sheet {c['sheet']}")
    for p in pins:
        net = nl.cpins.get(ref, {}).get(p)
        nm = nl.pinname(ref, p)
        if not net:
            print(f"\n  pin {p} {nm}: not on any net"); continue
        rv = f"  ({nl.railv[net]} V rail)" if net in nl.railv else ''
        print(f"\n  pin {p} {nm} -> {net}  [{len(nl.nets[net])} nodes]{rv}")
        if len(nl.nets[net]) > a.fanout > 0:
            print(f"    (rail: showing loads only)")
        _walk(nl, net, 1, a.depth, {net}, classes, a.fanout, "  ", skip=ref)

def _walk(nl, net, depth, maxd, seen, classes, fanout, prefix_, skip=None, force=False, via=None):
    if depth > maxd:
        return
    loads = sorted((nd for nd in nl.nets.get(net, [])
                    if not nl.is_passthrough(nd['ref'], classes, fanout) and nd['ref'] != skip),
                   key=lambda x: natkey(x['ref']))
    # a rail's loads are printed "showing loads only" for a reason: dumping all ~280
    # nodes of a GND net burns a huge amount of context for no benefit. Cap it.
    cap = 10 if nl.is_rail(net, fanout) else None
    for nd in (loads[:cap] if cap else loads):
        print(f"{prefix_}  |- {nd['ref']}.{nd['pin']} {nd['fn'] or ''} "
              f"[{nl.value(nd['ref'])}]{nl.tag(nd['ref'])}")
    if cap and len(loads) > cap:
        print(f"{prefix_}  |- ... +{len(loads) - cap} more (rail net, {len(loads)} loads total)")
    for ref, pin, op, far in expand(nl, net, classes, fanout, force=force):
        if ref == skip or ref == via:
            continue
        mark = "  (already seen)" if far in seen else ""
        big = (f"  [rail {nl.railv[far]}V, {len(nl.nets[far])} nodes - loads only]"
               if far in nl.railv else
               (f"  [{len(nl.nets[far])} nodes - loads only]"
                if fanout and len(nl.nets[far]) > fanout else ""))
        print(f"{prefix_}  +-{ref} [{nl.value(ref)}]{nl.tag(ref)} pin{pin}->pin{op}  ==> {far}{mark}{big}")
        if far not in seen:
            seen.add(far)
            _walk(nl, far, depth + 1, maxd, seen, classes, fanout, prefix_ + "  |  ", via=ref)

def c_path(nl, a):
    classes = {c.strip().upper() for c in a.through.split(',') if c.strip()}
    def endpoints(spec):
        if '.' in spec and spec.partition('.')[0] in nl.comps:
            r, _, p = spec.partition('.')
            n = nl.cpins.get(r, {}).get(p)
            return {n: (r, p)} if n else {}
        if spec in nl.nets:
            return {spec: None}
        out = {}
        for p, n in nl.cpins.get(spec, {}).items():
            if n in nl.railv or (a.fanout and len(nl.nets.get(n, [])) > a.fanout):
                continue          # GND/+3V3 as an endpoint makes everything "connected"
            out[n] = (spec, p)
        return out
    src, dst = endpoints(a.args[0]), endpoints(a.args[1])
    if not src or not dst:
        print("could not resolve one of the endpoints"); return
    q = deque([(n, [(n, None)]) for n in src])
    seen = set(src)
    while q:
        net, path = q.popleft()
        if net in dst:
            print(f"path {a.args[0]} -> {a.args[1]}:")
            for i, (n, via) in enumerate(path):
                if via:
                    print(f"    | {via[0]} [{nl.value(via[0])}]{nl.tag(via[0])} "
                          f"pin{via[1]}->pin{via[2]}")
                print(f"  {n}  [{len(nl.nets.get(n,[]))} nodes]")
            return
        for ref, pin, op, far in expand(nl, net, classes, a.fanout, force=(len(path) == 1)):
            if far not in seen:
                seen.add(far)
                q.append((far, path + [(far, (ref, pin, op))]))
    print(f"no path from {a.args[0]} to {a.args[1]} through "
          f"[{','.join(sorted(classes))}] parts (fanout limit {a.fanout}).")
    print(f"reached {len(seen)} net(s): {', '.join(sorted(seen, key=natkey))}")
    boundary = {}
    for n in seen:
        for nd in nl.nets.get(n, []):
            if not nl.is_passthrough(nd['ref'], classes, a.fanout):
                if len(nl.conn_pins(nd['ref'])) > 2:
                    boundary.setdefault(nd['ref'], set()).add(f"{nd['pin']}@{n}")
    if boundary:
        print("multi-pin parts on the boundary (crossing one may continue the chain):")
        for r in sorted(boundary, key=natkey):
            print(f"  {r:<6} [{nl.value(r)}] via {', '.join(sorted(boundary[r]))}")
    print("hint: add the prefix to --through, or raise --fanout, to search further")

def c_unconnected(nl, a):
    print("=== symbol pins present in the schematic but on no net ===")
    any_ = False
    for ref in sorted(nl.comps, key=natkey):
        pins = nl.sympins(ref)
        miss = [(n, v) for n, v in sorted(pins.items(), key=lambda kv: natkey(kv[0]))
                if n not in nl.cpins.get(ref, {}) and v[1] != 'no_connect']
        if miss:
            any_ = True
            print(f"  {ref:<6} {nl.value(ref)}")
            for n, (nm, ty) in miss:
                print(f"      pin {n:<4} {nm:<16} {ty}")
    if not any_:
        print("  none")
    solo = [(n, v[0]) for n, v in nl.nets.items() if len(v) == 1]
    named = [(n, nd) for n, nd in solo if not n.startswith('unconnected-')]
    auto = [(n, nd) for n, nd in solo if n.startswith('unconnected-')]
    print("\n=== NAMED nets with only one node (label that goes nowhere: usually a real bug) ===")
    if not named:
        print("  none")
    for n, nd in sorted(named, key=lambda kv: natkey(kv[0])):
        print(f"  {n:<34} {nd['ref']}.{nd['pin']} {nd['fn']} [{nl.value(nd['ref'])}]")
    print("\n=== KiCad auto 'unconnected-*' pins ===")
    probe = nl.ncflag(auto[0][1]['ref'], auto[0][1]['pin']) if auto else None
    have_sch = probe is not None or (auto and nl.sch().valid and nl.sch().files)
    for n, nd in sorted(auto, key=lambda kv: natkey(kv[0])):
        ty = nl.pintype(nd['ref'], nd['pin']) or '?'
        sev = '  <-- POWER PIN' if ty == 'power_in' else ''
        f = nl.ncflag(nd['ref'], nd['pin'])
        tag = ('' if f is None else
               ('  [NC flag in schematic]' if f else '  [NO NC flag - possibly forgotten]'))
        print(f"  {nd['ref']}.{nd['pin']:<4} {nd['fn']:<22} {ty}{sev}{tag}")
    if not have_sch:
        print("  (no .kicad_sch next to the netlist, so intentional-vs-forgotten "
              "cannot be distinguished)")

# ---------------- rule-based review ----------------

RULES = {
    'FLOATPWR':  'power_in pin left unconnected',
    'NCDRIVEN':  'pin marked no_connect but wired to a net',
    'SOLO':      'named net with a single node',
    'CONTEND':   'two drivers (output/power_out) on one net',
    'DOMAIN':    'pull-up/down or series R to a rail above the part supply voltage',
    'DECOUPLE':  'power_in pin with no local bypass capacitor',
    'OCNOPULL':  'open-collector/open-drain output with no pull-up',
    'I2CPULL':   'SDA/SCL net with no pull-up to a rail',
    'PASSONLY':  'net made only of passives (no IC, no connector)',
    'RFSTUB':    'RF / 50 ohm net with more than 2 non-ground nodes',
    'DNPPATH':   'DNP part sits in series in a signal path (open circuit on the board)',
    'CAPRATING': 'capacitor voltage rating below 2x the rail it sits on',
    'NOVALUE':   'component with no value or no footprint',
    'GNDISLAND': 'GND-named pins wired together but isolated from the main GND net',
    'CLAMPRATING': 'TVS standoff voltage below the rail it clamps',
    'PARPIN':    'a paralleled pin left dangling while a same-named pin is on a real net',
}

def find_pull(nl, net, fanout):
    """2-pin R/L/FB from `net` to a voltage rail -> [(ref, railnet, volts)]."""
    out = []
    for nd in nl.nets.get(net, []):
        r = nd['ref']
        if nl.comps.get(r, {}).get('prefix') not in ('R', 'L', 'FB', 'F', 'FL', 'JP'):
            continue
        if nl.no_dnp and nl.comps[r]['dnp']:
            continue
        if len(nl.conn_pins(r)) != 2:
            continue
        op = nl.other_pin(r, nd['pin'])
        far = nl.cpins[r].get(op) if op else None
        if far and far in nl.railv and nl.railv[far] > 0:
            out.append((r, far, nl.railv[far]))
    return out

def gen_findings(nl, a):
    only = {s.strip().upper() for s in a.only.split(',')} if a.only else None
    skip = {s.strip().upper() for s in a.skip.split(',')} if a.skip else set()
    F = []
    def add(sev, rule, msg, refs=()):
        if only and rule not in only:
            return
        if rule in skip:
            return
        F.append({'severity': sev, 'rule': rule, 'msg': msg, 'refs': list(refs)})

    # FLOATPWR / NCDRIVEN
    for ref, c in nl.comps.items():
        for pin, (nm, ty) in nl.sympins(ref).items():
            base = ty.split('+')[0]
            net = nl.cpins.get(ref, {}).get(pin)
            if base == 'power_in' and (net is None or net.startswith('unconnected-')):
                f = nl.ncflag(ref, pin)
                if f:
                    add('WARN', 'FLOATPWR', f"{ref}.{pin} ({nm}) is a power input with an "
                        f"explicit NC flag in the schematic - confirm that is really intended", [ref])
                else:
                    tail = ' (no NC flag in the schematic - likely forgotten)' if f is False else ''
                    add('ERROR', 'FLOATPWR',
                        f"{ref}.{pin} ({nm}) is a power input and is not connected{tail}", [ref])
            if 'no_connect' in ty and net and not net.startswith('unconnected-'):
                add('WARN', 'NCDRIVEN', f"{ref}.{pin} ({nm}) is marked NC but is wired to {net}", [ref])

    # PARPIN: a paralleled pad (two or more pins sharing the same declared NAME,
    # e.g. multiple BAT pins on a battery-charger IC) where one is wired to a real
    # net and its sibling is left floating. FLOATPWR only catches this when the
    # dangling pin is typed power_in; a symbol that types a paralleled pin merely
    # 'passive' (seen on BQ25798 pin 23, "BAT") slips past it, so this rule matches
    # on pin NAME instead of pin type.
    for ref, c in nl.comps.items():
        byname = defaultdict(list)
        for pin, (nm, ty) in nl.sympins(ref).items():
            if nm:
                byname[nm].append(pin)
        for nm, plist in byname.items():
            if len(plist) < 2:
                continue
            wired = [p for p in plist if (nl.cpins.get(ref, {}).get(p) or '')
                     and not nl.cpins[ref][p].startswith('unconnected-')]
            dangling = [p for p in plist if p not in wired]
            if wired and dangling:
                for p in dangling:
                    add('ERROR', 'PARPIN',
                        f"{ref}.{p} ({nm}) is not connected, but {ref}.{wired[0]} "
                        f"(same pin name '{nm}') is on {nl.cpins[ref][wired[0]]} - "
                        f"a paralleled pad likely left dangling", [ref])

    # GNDISLAND: pins that are GND by NAME, wired to each other, but the net they
    # land on is not recognised as ground (is_gnd only matches by net NAME) - this
    # is what a ground pin merged to the wrong net, or a connector's GND pins never
    # joined to the board GND, looks like in a netlist. Different from FLOATPWR
    # (no net at all): here the pins ARE connected, just to each other and nothing
    # else, which is easy to misread as "fine" until you look at what's missing.
    gnd_islands = defaultdict(set)
    for (ref, pin), net in nl.pinnet.items():
        if net.startswith('unconnected-') or nl.is_gnd(net):
            continue
        if GND_RE.match(nl.pinname(ref, pin) or ''):
            gnd_islands[net].add((ref, pin))
    for net, pins in gnd_islands.items():
        if len(pins) > 1 and len(pins) == len(nl.nets.get(net, [])):
            refs = sorted({r for r, p in pins}, key=natkey)
            names = ', '.join(f"{r}.{p}" for r, p in sorted(pins, key=lambda x: natkey(x[0])))
            add('ERROR', 'GNDISLAND', f"{net}: {len(pins)} GND-named pin(s) ({names}) "
                f"wired together but not on the main GND net - likely needs a ground merge", refs)

    # SOLO
    for n, nodes in nl.nets.items():
        if len(nodes) == 1 and not n.startswith('unconnected-'):
            nd = nodes[0]
            add('ERROR', 'SOLO', f"net {n} has only {nd['ref']}.{nd['pin']} on it", [nd['ref']])

    # CONTEND
    for n, nodes in nl.nets.items():
        drv = [(nd['ref'], nd['pin'], nl.pintype(nd['ref'], nd['pin'])) for nd in nodes
               if nl.pintype(nd['ref'], nd['pin']) in ('output', 'power_out')]
        groups = {(r, nl.pinname(r, p) or p) for r, p, _ in drv}
        if len(groups) > 1:
            add('ERROR', 'CONTEND', f"net {n} has {len(drv)} drivers: " +
                ', '.join(f"{r}.{p}({t})" for r, p, t in drv), [d[0] for d in drv])
        if len(drv) == 1 and n in nl.railv and nl.railv[n] > 0 and drv[0][2] == 'power_out':
            pulls = find_pull(nl, n, a.fanout)
            if pulls:
                add('WARN', 'CONTEND', f"net {n} is driven by {drv[0][0]}.{drv[0][1]} (power_out) "
                    f"and also tied through {', '.join(p[0] for p in pulls)} to "
                    f"{', '.join(p[1] for p in pulls)} - possible back-feed", [drv[0][0]])

    # DOMAIN: pull to a rail higher than the part's own supply
    for n, nodes in nl.nets.items():
        if n in nl.railv or len(nodes) > a.fanout:
            continue
        pulls = find_pull(nl, n, a.fanout)
        if not pulls:
            continue
        top = max(p[2] for p in pulls)
        for nd in nodes:
            ref = nd['ref']
            c = nl.comps.get(ref, {})
            if c.get('prefix') not in ('U', 'IC'):
                continue
            srail, sv = nl.supply_rail(ref)
            if sv and top > sv + 0.35:
                via = ', '.join(f"{p[0]}[{nl.value(p[0])}]->{p[1]}" for p in pulls if p[2] == top)
                add('WARN', 'DOMAIN',
                    f"{n} is pulled to {top} V via {via}, but {ref}.{nd['pin']} "
                    f"({nl.pinname(ref, nd['pin'])}) is supplied from {srail} ({sv} V)", [ref])

    # DECOUPLE
    for ref, c in nl.comps.items():
        if c['prefix'] not in ('U', 'IC'):
            continue
        for pin, net in nl.cpins.get(ref, {}).items():
            if nl.pintype(ref, pin) != 'power_in' or nl.is_gnd(net):
                continue
            if GND_RE.match(nl.pinname(ref, pin) or ''):
                continue   # a GND-named power pin never needs a bypass cap, even
                           # on an auto-named/isolated net; GNDISLAND below is the
                           # rule that catches that case (it is a wiring bug, not
                           # a missing-capacitor one)
            cs = [x for x in caps_on(nl, net) if not x[2]]
            small = [x for x in cs if x[1] and x[1] <= 1e-6]
            if not cs:
                add('WARN', 'DECOUPLE', f"{ref}.{pin} ({nl.pinname(ref,pin)}) on {net}: "
                    f"no capacitor to ground on that net", [ref])
            elif not small:
                add('INFO', 'DECOUPLE', f"{ref}.{pin} on {net}: only bulk "
                    f"({', '.join(f'{r}={eng(v,chr(70))}' for r, v, _ in cs)}), no <=1uF HF bypass", [ref])

    # OCNOPULL
    for ref in nl.comps:
        for pin, (nm, ty) in nl.sympins(ref).items():
            if 'open_collector' not in ty and 'open_emitter' not in ty:
                continue
            net = nl.cpins.get(ref, {}).get(pin)
            if not net or net.startswith('unconnected-'):
                add('INFO', 'OCNOPULL', f"{ref}.{pin} ({nm}) is open-drain and unconnected", [ref])
            elif not find_pull(nl, net, a.fanout):
                add('WARN', 'OCNOPULL', f"{ref}.{pin} ({nm}) is open-drain on {net} "
                    f"with no pull-up to a rail", [ref])

    # I2CPULL
    for n, nodes in nl.nets.items():
        base = n.split('/')[-1].upper()
        if not re.search(r'\b(SDA|SCL)\b|^(SDA|SCL)', base):
            continue
        if not find_pull(nl, n, a.fanout):
            add('WARN', 'I2CPULL', f"{n} looks like an I2C line with no pull-up to a rail",
                [nd['ref'] for nd in nodes])

    # PASSONLY
    PASS = {'R', 'C', 'L', 'FB', 'D', 'TP', 'H', 'JP'}
    for n, nodes in nl.nets.items():
        if n.startswith('unconnected-') or n in nl.railv or len(nodes) < 3 or len(nodes) > a.fanout:
            continue
        if any(nl.comps.get(nd['ref'], {}).get('prefix') in ('JP', 'TP', 'H') for nd in nodes):
            continue
        if all(nl.comps.get(nd['ref'], {}).get('prefix') in PASS for nd in nodes):
            add('INFO', 'PASSONLY', f"{n}: only passives ({', '.join(sorted({nd['ref'] for nd in nodes}, key=natkey))})",
                [nd['ref'] for nd in nodes])

    # RFSTUB - DNP parts are open pads, not stubs, so they are reported but not counted
    for n, nodes in nl.nets.items():
        cls = nl.netclass.get(n, '')
        rf = 'RF' in cls.upper() or re.search(r'ANT|_RF\b|RF_IN|RFIN', n.upper())
        if not rf:
            continue
        pop = sorted({nd['ref'] for nd in nodes
                      if not nl.comps.get(nd['ref'], {}).get('dnp')}, key=natkey)
        dnp = sorted({nd['ref'] for nd in nodes
                      if nl.comps.get(nd['ref'], {}).get('dnp')}, key=natkey)
        if len(pop) > 2:
            add('WARN', 'RFSTUB', f"{n} [{cls}] has {len(pop)} populated parts on it "
                f"({', '.join(pop)})"
                + (f" plus DNP {', '.join(dnp)}" if dnp else '')
                + " - anything beyond a 2-port chain is a stub on a 50 ohm line", pop)

    # DNPPATH
    for ref, c in nl.comps.items():
        if not c['dnp']:
            continue
        cp = nl.conn_pins(ref)
        if len(cp) == 2:
            nets = list(cp.values())
            if not any(nl.is_gnd(x) for x in nets):
                add('INFO', 'DNPPATH', f"{ref} [{c['value']}] is DNP and sits in series "
                    f"between {nets[0]} and {nets[1]} - that path is open on the built board", [ref])

    # CAPRATING
    for ref, c in nl.comps.items():
        if c['prefix'] != 'C' or c['dnp']:
            continue
        vr = c['props'].get('Voltage Rating')
        vr = parse_value(vr.replace('v', 'V').replace('V', ''), 'R') if isinstance(vr, str) else None
        if not vr:
            continue
        for pin, net in nl.cpins.get(ref, {}).items():
            rv = nl.railv.get(net)
            if rv and rv > 0 and vr < 2 * rv:
                add('WARN', 'CAPRATING', f"{ref} rated {vr:g} V sits on {net} ({rv} V); "
                    f"MLCC DC-bias derating wants >=2x", [ref])

    # CLAMPRATING: a recognisable TVS part number (SMF/SMAJ/SMBJ/SMCJ/SM6T/SM8S/
    # P6KE/1.5KE/P4KE series) sitting directly between a named rail and GND,
    # whose standoff voltage - parsed straight from the part number - is below
    # the rail it is meant to protect. A clamp with standoff under the rail
    # conducts (and self-heats/degrades) during normal operation, not only
    # during a transient. Heuristic and deliberately conservative: only fires
    # for recognised TVS series naming and only strictly-below (no margin
    # requirement), so it won't flag a clamp that merely has thin headroom -
    # confirm the actual part's rating against its datasheet either way.
    for ref, c in nl.comps.items():
        if c['prefix'] != 'D' or c['dnp']:
            continue
        standoff = tvs_standoff(c['value'])
        if standoff is None:
            continue
        cp = nl.conn_pins(ref)
        if len(cp) != 2:
            continue
        nets = list(cp.values())
        rail_net = next((n for n in nets if n in nl.railv and nl.railv[n] > 0), None)
        gnd_net = next((n for n in nets if nl.is_gnd(n)), None)
        if rail_net and gnd_net and standoff <= nl.railv[rail_net]:
            add('WARN', 'CLAMPRATING',
                f"{ref} [{c['value']}] standoff {standoff:g} V has no margin over the "
                f"{nl.railv[rail_net]:g} V rail ({rail_net}) it clamps - note the rail figure is "
                f"name-derived/--rail, not the regulator's actual analog output, so this can "
                f"understate the gap; would conduct/degrade under normal operation if so, not "
                f"just during a transient", [ref])

    # NOVALUE
    for ref, c in nl.comps.items():
        if not c['footprint']:
            add('ERROR', 'NOVALUE', f"{ref} has no footprint", [ref])
        elif not c['value'] or c['value'] in ('~', '?'):
            add('WARN', 'NOVALUE', f"{ref} has no value", [ref])

    supp = getattr(a, 'suppress', None) or {}
    if not supp:
        return F, 0
    kept = [f for f in F if not suppressed(f, supp)]
    return kept, len(F) - len(kept)

def c_check(nl, a):
    F, supp_n = gen_findings(nl, a)
    if a.since:
        try:
            old = Netlist(a.since, no_dnp=nl.no_dnp)
        except FileNotFoundError:
            print(f"no such netlist: {a.since}", file=sys.stderr); return 3
        Fo, _ = gen_findings(old, a)
        okeys = {_fkey(f) for f in Fo}
        nkeys = {_fkey(f) for f in F}
        new = [f for f in F if _fkey(f) not in okeys]
        fixed = [f for f in Fo if _fkey(f) not in nkeys]
        same = len(F) - len(new)
        if a.json:
            print(json.dumps({'new': new, 'fixed': fixed, 'unchanged': same}, indent=1))
            return 2 if any(f['severity'] == 'ERROR' for f in new) else 0
        print(f"vs {a.since}: {len(new)} new, {len(fixed)} fixed, "
              f"{same} unchanged (rerun without --since for the full list)")
        if new:
            print_findings(new, "\n### NEW findings", rules=RULES)
        if fixed:
            print("\n### FIXED since old export")
            for f in sorted(fixed, key=lambda x: (x['rule'], x['msg'])):
                print(f"    [{f['severity']}] {f['rule']}: {f['msg']}")
        return 2 if any(f['severity'] == 'ERROR' for f in new) else 0
    if a.json:
        print(json.dumps(F, indent=1)); return
    counts = defaultdict(int)
    for f in F:
        counts[f['severity']] += 1
    print_findings(F, f"{nl.path}: {counts['ERROR']} error, "
                      f"{counts['WARN']} warn, {counts['INFO']} info\n", rules=RULES)
    if not F:
        print("no findings")
    if supp_n:
        print(f"\n({supp_n} finding(s) suppressed via knet.json `suppress` list - "
              f"`--no-suppress` to see them)")
    if a.rules or not F:
        print("\nrules: " + ', '.join(f"{k}={v}" for k, v in sorted(RULES.items())))
    return 2 if counts['ERROR'] else 0

# ---------------- diff ----------------

def neighbours(nl, ref, pin):
    """Set of other pins sharing this pin's net. Immune to KiCad auto-net renaming."""
    n = nl.cpins.get(ref, {}).get(pin)
    if not n:
        return None
    return frozenset(f"{x['ref']}.{x['pin']}" for x in nl.nets[n] if (x['ref'], x['pin']) != (ref, pin))

def c_diff(nl, a):
    old = Netlist(a.args[0], no_dnp=nl.no_dnp)
    new = nl
    rep = {'added': [], 'removed': [], 'changed': [], 'rewired': [], 'gained': [],
           'nets_added': [], 'nets_removed': []}
    for ref in sorted(set(new.comps) - set(old.comps), key=natkey):
        rep['added'].append(f"{ref} [{new.value(ref)}] {new.comps[ref]['footprint']}")
    for ref in sorted(set(old.comps) - set(new.comps), key=natkey):
        rep['removed'].append(f"{ref} [{old.value(ref)}]")
    for ref in sorted(set(old.comps) & set(new.comps), key=natkey):
        o, n = old.comps[ref], new.comps[ref]
        for f in ('value', 'footprint', 'lcsc', 'sheet'):
            if o[f] != n[f]:
                rep['changed'].append(f"{ref} {f}: {o[f] or '(none)'}  ->  {n[f] or '(none)'}")
        if o['dnp'] != n['dnp']:
            rep['changed'].append(f"{ref} dnp: {o['dnp']} -> {n['dnp']}")
    for ref in sorted(set(old.comps) & set(new.comps), key=natkey):
        for pin in sorted(set(old.cpins.get(ref, {})) | set(new.cpins.get(ref, {})), key=natkey):
            ob, nb = neighbours(old, ref, pin), neighbours(new, ref, pin)
            if ob == nb:
                continue
            on = old.cpins.get(ref, {}).get(pin, '(none)')
            nn = new.cpins.get(ref, {}).get(pin, '(none)')
            gone = sorted((ob or set()) - (nb or set()), key=natkey)
            got = sorted((nb or set()) - (ob or set()), key=natkey)
            line = (f"{ref}.{pin} ({new.pinname(ref,pin) or old.pinname(ref,pin)}): {on} -> {nn}"
                    + (f"\n        lost: {', '.join(gone)}" if gone else "")
                    + (f"\n        new : {', '.join(got)}" if got else ""))
            rep['rewired' if gone else 'gained'].append(line)
    onames = {n for n in old.nets if not n.startswith('unconnected-')}
    nnames = {n for n in new.nets if not n.startswith('unconnected-')}
    rep['nets_added'] = sorted(nnames - onames, key=natkey)
    rep['nets_removed'] = sorted(onames - nnames, key=natkey)
    if a.json:
        print(json.dumps(rep, indent=1)); return
    print(f"old: {old.path}  ({old.date})")
    print(f"new: {new.path}  ({new.date})")
    for title, key in (('components added', 'added'), ('components removed', 'removed'),
                       ('component fields changed', 'changed'),
                       ('REWIRED - pin lost a connection', 'rewired'),
                       ('pins that only gained neighbours (bystanders on a changed net)', 'gained'),
                       ('net names added', 'nets_added'), ('net names removed', 'nets_removed')):
        items = rep[key]
        print(f"\n=== {title} ({len(items)}) ===")
        for i in items:
            print(f"    {i}")
        if not items:
            print("    none")


# ---------------- branch model (shared by draw) ----------------

def _mkbranch(nl, net, classes, fanout, depth, seen, skip):
    b = {'net': net, 'loads': [], 'subs': [], 'rail': None, 'gnd': False,
         'floating': False, 'single': False}
    if net is None or net.startswith('unconnected-'):
        b['floating'] = True; return b
    if nl.is_gnd(net):
        b['gnd'] = True; return b
    if net in nl.railv and nl.railv[net] != 0:
        b['rail'] = nl.railv[net]; return b
    if fanout and len(nl.nets.get(net, [])) > fanout:
        b['rail'] = 0; b['big'] = len(nl.nets[net]); return b
    nodes = nl.nets.get(net, [])
    b['single'] = len(nodes) == 1
    for nd in sorted(nodes, key=lambda x: natkey(x['ref'])):
        if nd['ref'] == skip:
            continue
        if nl.is_passthrough(nd['ref'], classes, fanout) and depth > 1:
            op = nl.other_pin(nd['ref'], nd['pin'])
            far = nl.cpins[nd['ref']].get(op) if op else None
            if far and far != net and far not in seen:
                seen.add(far)
                sub = _mkbranch(nl, far, classes, fanout, depth - 1, seen, nd['ref'])
                b['subs'].append((nd['ref'], nl.value(nd['ref']),
                                  nl.comps[nd['ref']]['dnp'], sub, nd['pin'], op))
                continue
        c = nl.comps.get(nd['ref'], {})
        b['loads'].append((nd['ref'], nd['pin'], nd['fn'] or nl.pinname(nd['ref'], nd['pin']),
                           c.get('value', ''), c.get('dnp', False)))
    return b

def _bh(b, hseen=None):
    # mirrors _rbranch collapse logic so allocated rows == rendered rows
    if b['rail'] is not None or b['gnd'] or b['floating']:
        return 1
    if hseen is not None:
        if b['net'] in hseen:
            return 1
        if b['net']:
            hseen.add(b['net'])
    return max(1, len(b['loads']) + sum(_bh(s[3], hseen) for s in b['subs']))

# ---------------- draw (KiCad-style schematic via ksch.py) ----------------
# knet works out WHAT connects to what; ksch.py draws it. One renderer, one
# symbol library, and the spec is plain text so it can be edited and re-rendered.

PREFIX_SYM = {'R': 'r', 'C': 'c', 'L': 'l', 'FB': 'fb', 'FL': 'fb', 'D': 'd',
              'F': 'fuse', 'JP': 'jp', 'SW': 'sw', 'Y': 'xtal', 'X': 'xtal',
              'TP': 'tp', 'BT': 'bat', 'AE': 'ant', 'E': 'ant'}
COL = 5          # x pitch between hops (wire + 2-unit part)
ROW = 3          # y pitch, matches the IC pin pitch (p=3 on the ic line)


def _ksch():
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import ksch
    return ksch


def _q(s):
    s = str(s)
    return '"%s"' % s if (' ' in s or not s) else s


def _leaf(n):
    return unesc_disp((n or '').split('/')[-1])


def _mkbranch_pins(nl, ref, pin):
    op = nl.other_pin(ref, pin)
    return pin, op


def _sym_for(ref):
    return PREFIX_SYM.get(prefix(ref), 'r')


class _SpecGen:
    """turns one focal part into a ksch spec. Every branch owns its own rows,
    so nothing can collide by construction."""

    def __init__(self, nl, a):
        self.nl, self.a = nl, a
        self.ks = _ksch()
        self.lines = []
        self.placed = {}          # ref -> True
        self.notes = []
        self._sympin_cache = {}   # ksch symbol type -> its fixed pin-key set

    def emit(self, s):
        self.lines.append(s)

    def _has_pins(self, typ, *pins):
        """True if ksch's `typ` symbol actually defines every pin named in `pins`.
        A ref's PREFIX_SYM guess can be wrong for the real part: e.g. 'TP' maps to
        ksch's single-pin 'tp' test point, but KiCad also has a 2-pole test point
        symbol (2 real pins) that shares the TP refdes prefix. Without this check,
        two_pin() would reference a pin the ksch symbol does not have and crash."""
        if typ not in self._sympin_cache:
            self._sympin_cache[typ] = (set(self.ks.TWO_PIN[typ]({})[1])
                                       if typ in self.ks.TWO_PIN else set())
        return all(p in self._sympin_cache[typ] for p in pins)

    def peer_box(self, ref, pin, fn, val, dnp, side, trunk, y, srcx):
        """a peer part shown as a symbol fragment: one pin, ref and value."""
        nl = self.nl
        if ref in self.placed:
            self.emit('note %g,%g %s' % (trunk + .4, y + .45,
                                         _q('to %s.%s (drawn above)' % (ref, pin))))
            return None
        name = trunc(unesc_disp(val), 16) or ref
        fname = unesc_disp(fn or nl.pinname(ref, pin) or '')
        if fname == pin:
            fname = ''
        sides = {'R' if side < 0 else 'L': [(pin, fname)]}
        _pr, _pins, bb = self.ks.s_ic(sides, None, None, name)
        w = bb[2]
        x = trunk - (w + 2) if side < 0 else trunk + 2
        self.emit('conn %s %g,%g %s flat %s:%s=%s%s' % (
            ref, x, y - 1, _q(name), 'R' if side < 0 else 'L', pin,
            _q(fname), ' dnp' if dnp else ''))
        self.placed[ref] = True
        return '%s.%s' % (ref, pin)

    def two_pin(self, typ, ref, near, far, val, dnp, src, side, trunk, yy):
        """draw a real R/C/L symbol and terminate its far pin the way the
        netlist does - a decoupling cap should look like a cap, not a box."""
        nl = self.nl
        if side < 0:
            orient, x = ('hr', trunk) if near == '1' else ('h', trunk - 2)
        else:
            orient, x = ('h', trunk) if near == '1' else ('hr', trunk + 2)
        self.emit('%s %s %g,%g %s %s%s' % (typ, ref, x, yy, orient,
                                           _q(trunc(unesc_disp(val), 14)),
                                           ' dnp' if dnp else ''))
        self.placed[ref] = True
        self.emit('wire %s %s.%s' % (src, ref, near))
        fnet = nl.cpins.get(ref, {}).get(far)
        tgt = '%s.%s' % (ref, far)
        if not fnet or fnet.startswith('unconnected-'):
            self.emit('nc %s' % tgt)
        elif nl.is_gnd(fnet):
            self.emit('gnd %s' % tgt)
        elif nl.railv.get(fnet):
            self.emit('pwr %s %s' % (_leaf(fnet), tgt))
        else:
            self.emit('label %s %s' % (_q(_leaf(fnet)), tgt))

    def chain(self, b, src, side, trunk, row, ytop, srcx=0):
        """src = 'REF.PIN' the branch hangs off. Returns rows consumed."""
        nl = self.nl
        y = ytop + ROW * row
        loads, subs = b['loads'], b['subs']
        if not loads and not subs:
            return 1
        used = 0
        for (ref, pin, fn, val, dnp) in loads:
            yy = ytop + ROW * (row + used)
            typ = PREFIX_SYM.get(prefix(ref))
            other = nl.other_pin(ref, pin)
            if typ and other and ref not in self.placed and len(nl.conn_pins(ref)) == 2 \
                    and self._has_pins(typ, pin, other):
                self.two_pin(typ, ref, pin, other, val, dnp, src, side, trunk, yy)
            else:
                tgt = self.peer_box(ref, pin, fn, val, dnp, side, trunk, yy, srcx)
                if tgt:
                    self.emit('wire %s %s' % (src, tgt))
            used += 1
        for sub in subs:
            ref, val, dnp, sb = sub[0], sub[1], sub[2], sub[3]
            near, far = (sub[4], sub[5]) if len(sub) > 5 else ('1', '2')
            yy = ytop + ROW * (row + used)
            if ref in self.placed:
                used += 1
                continue
            typ = _sym_for(ref)
            # orient so the pin that really connects faces the focal part
            if side < 0:
                orient, x = ('hr', trunk) if near == '1' else ('h', trunk - 2)
            else:
                orient, x = ('h', trunk) if near == '1' else ('hr', trunk + 2)
            self.emit('%s %s %g,%g %s %s%s' % (typ, ref, x, yy, orient,
                                               _q(trunc(unesc_disp(val), 14)),
                                               ' dnp' if dnp else ''))
            self.placed[ref] = True
            self.emit('wire %s %s.%s' % (src, ref, near))
            nxt = '%s.%s' % (ref, far)
            ntrunk = trunk + side * COL
            if sb['gnd']:
                self.emit('gnd %s' % nxt)
            elif sb['rail'] and sb['rail'] != 0:
                self.emit('pwr %s %s' % (_leaf(sb['net']), nxt))
            elif sb['floating']:
                self.emit('nc %s' % nxt)
            elif not (sb['loads'] or sb['subs']):
                self.emit('label %s %s' % (
                    _q(_leaf(sb['net']) + ('  SINGLE NODE' if sb['single'] else '')), nxt))
            else:
                self.emit('note %g,%g %s' % (
                    (ntrunk + .4) if side < 0 else (trunk + 2.4), yy - .4,
                    _q(_leaf(sb['net']))))
                used += self.chain(sb, nxt, side, ntrunk, row + used, ytop,
                                   trunk - 2 if side < 0 else trunk + 2) - 1
            used += 1
        return max(1, used)

    def component(self, ref):
        nl, a = self.nl, self.a
        c = nl.comps[ref]
        # the focal part can reappear deeper in its own branches; it is already
        # on the sheet, so those become cross-references, not a second symbol
        self.placed[ref] = True
        classes = {x.strip().upper() for x in a.through.split(',') if x.strip()}
        declared = nl.sympins(ref)
        allp = sorted(set(declared) | set(nl.cpins.get(ref, {})), key=natkey)
        br, hs = {}, set()
        for p in allp:
            net = nl.cpins.get(ref, {}).get(p)
            br[p] = _mkbranch(nl, net, classes, a.fanout, a.depth, {net}, ref)
        seen_net = {}
        T, B, Lp, Rp = [], [], [], []
        for p in allp:
            b = br[p]
            n = b['net']
            if b['gnd']:
                B.append(p); continue
            if b['rail'] and b['rail'] != 0:
                T.append(p); continue
            if n and n in seen_net:
                b['dup'] = seen_net[n]
            elif n:
                seen_net[n] = p
            ty = (declared.get(p) or ('', ''))[1]
            (Rp if ty in ('output', 'power_out', 'tri_state', 'open_collector') else Lp).append(p)
        if not Rp and len(Lp) > 5:
            half = (len(Lp) + 1) // 2
            Lp, Rp = Lp[:half], Lp[half:]
        rows = {}
        for p in Lp + Rp:
            b = br[p]
            if b['gnd'] or b['floating'] or b.get('dup') or b['single'] or \
                    b['rail'] is not None:
                rows[p] = 1
            else:
                h = _bh(b, hs)
                rows[p] = h + (1 if h > 1 else 0)
        hL, hR = sum(rows[p] for p in Lp), sum(rows[p] for p in Rp)
        # pin slots: one entry per row, blanks keep every branch on its own line
        def side_spec(pins):
            out = []
            for p in pins:
                nm = unesc_disp((declared.get(p) or ('', ''))[0] or '')
                out.append('%s=%s' % (p, nm.replace(',', ';') or p))
                out += [''] * (rows[p] - 1)
            return ','.join(out)
        parts = []
        if Lp:
            parts.append('L:' + side_spec(Lp))
        if Rp:
            parts.append('R:' + side_spec(Rp))
        if T:
            parts.append('T:' + ','.join('%s=%s' % (p, unesc_disp((declared.get(p) or ('', ''))[0] or p)) for p in T))
        if B:
            parts.append('B:' + ','.join('%s=%s' % (p, unesc_disp((declared.get(p) or ('', ''))[0] or p)) for p in B))
        icx, icy = 0, 0
        self.emit('ic %s %g,%g %s p=%d h=%g %s%s' % (
            ref, icx, icy, _q(trunc(unesc_disp(c['value']), 24)), ROW,
            max(2, ROW * max(hL, hR)), ' '.join(parts), ' dnp' if c['dnp'] else ''))
        _pr, _pins, bb = self.ks.s_ic(
            {'L': [(p, unesc_disp((declared.get(p) or ('', ''))[0] or p)) for p in Lp],
             'R': [(p, unesc_disp((declared.get(p) or ('', ''))[0] or p)) for p in Rp],
             'T': [(p, '') for p in T], 'B': [(p, '') for p in B]},
            None, max(2, ROW * max(hL, hR)), trunc(c['value'], 24), ROW)
        w = bb[2]
        for p in T:
            self.emit('pwr %s %s.%s' % (_leaf(br[p]['net']), ref, p))
        for p in B:
            self.emit('gnd %s.%s' % (ref, p))
        for side, pins in ((-1, Lp), (1, Rp)):
            row = 0
            for p in pins:
                b = br[p]
                src = '%s.%s' % (ref, p)
                y = icy + 1 + ROW * row
                trunk = (icx - 2 - COL) if side < 0 else (icx + w + 2 + COL)
                nx = icx - 2 if side < 0 else icx + w + 2
                if b['floating']:
                    self.emit('nc %s' % src)
                elif b['rail'] is not None:      # big net: name it, do not expand
                    self.emit('label %s %s' % (_q('%s (%d nodes)' % (_leaf(b['net']),
                                                                     b.get('big', 0))), src))
                elif b.get('dup'):
                    self.emit('label %s %s' % (_q(_leaf(b['net'])), src))
                elif b['single']:
                    self.emit('label %s %s' % (_q(_leaf(b['net']) + '  SINGLE NODE'), src))
                elif not (b['loads'] or b['subs']):
                    self.emit('label %s %s' % (_q(_leaf(b['net'])), src))
                else:
                    self.emit('note %g,%g %s' % (
                        (trunk + .4) if side < 0 else (nx + .4), y - .4,
                        _q(_leaf(b['net']))))
                    self.chain(b, src, side, trunk, row, icy + 1, nx)
                row += rows[p]
        return self.lines

    def net(self, net):
        """one box per part (a part can sit on the same net twice), all hung
        off a single trunk - which is exactly how KiCad draws a bus stub."""
        nl = self.nl
        byref = {}
        for nd in sorted(nl.nets[net], key=lambda z: natkey(z['ref'])):
            byref.setdefault(nd['ref'], []).append(nd)
        boxes, widest = [], 0
        for ref, nds in byref.items():
            c = nl.comps.get(ref, {})
            nm = trunc(unesc_disp(c.get('value', '')), 16) or ref
            pins = []
            for nd in nds:
                fn = unesc_disp(nd['fn'] or nl.pinname(ref, nd['pin']) or '')
                pins.append((nd['pin'], '' if fn == nd['pin'] else fn))
            _p, _q2, bb = self.ks.s_ic({'R': pins}, None, None, nm)
            widest = max(widest, bb[2])
            boxes.append((ref, nm, pins, bb[2], c.get('dnp', False)))
        trunk = widest + 5
        eps, y = [], 0
        for ref, nm, pins, w, dnp in boxes:
            self.emit('conn %s %g,%g %s flat R:%s%s' % (
                ref, widest - w, y, _q(nm),
                ','.join('%s=%s' % (p, _q(n)) for p, n in pins),
                ' dnp' if dnp else ''))
            eps += ['%s.%s' % (ref, p) for p, _n in pins]
            y += 2 * max(1, len(pins)) + 2
        self.emit('net %s @x=%g %s' % (_q(_leaf(net)), trunk, ' '.join(eps)))
        return self.lines


def c_draw(nl, a):
    if not a.args:
        print("usage: draw REF | NETNAME  [-d N] [-o out.svg] [--spec]"); return 1
    spec = a.args[0]
    g = _SpecGen(nl, a)
    if spec in nl.comps:
        title = '%s  %s   (sheet %s, depth %d)' % (
            spec, trunc(nl.comps[spec]['value'], 24), nl.comps[spec]['sheet'], a.depth)
        g.emit('title ' + title)
        g.component(spec)
    else:
        net = spec if spec in nl.nets else next(
            (n for n in nl.nets if n.split('/')[-1].lower() == spec.lower()), None)
        if not net:
            print("no component or net named %s" % spec); return 1
        g.emit('title net %s   (%d nodes)' % (unesc_disp(_leaf(net)), len(nl.nets[net])))
        g.net(net)
    text = '\n'.join(g.lines) + '\n'
    if getattr(a, 'spec', False):
        print(text, end=''); return 0
    ks = _ksch()
    d = ks.Doc(dict(px=22, theme=getattr(a, 'theme', 'kicad'), nl=nl))
    d.parse(text)
    if d.errors:
        for m in d.errors:
            print("spec error: " + m, file=sys.stderr)
        print("(run with --spec to see the generated spec)", file=sys.stderr)
        return 3
    out = a.out or 'knet_%s.svg' % re.sub(r'[^A-Za-z0-9._-]', '_', spec)
    open(out, 'w', encoding='utf-8').write(d.svg())
    print("wrote %s  (%d symbols, %d wire segments)  edit it with: "
          "knet.py FILE draw %s --spec > x.ksch" % (out, len(d.order), len(d.segs), spec))
    msgs = ks.check_doc(d) + ks.verify_doc(d, nl)
    for sev, m in msgs:
        print(f"  {sev:<6} {m}", file=sys.stderr)
    return 2 if any(sev == 'ERROR' for sev, m in msgs) else 0

# ---------------- main ----------------

def c_around(nl, a):
    """Everything about a part in one call: pins, nets, and the parts one hop away.
    Rails are summarised, not expanded, because listing 86 GND nodes helps nobody."""
    for ref in a.args:
        c = nl.comps.get(ref)
        if not c:
            print(f"{ref}: NOT FOUND"); continue
        rail, rv = nl.supply_rail(ref)
        pos = nl.sch().pos.get(ref) if nl.sch().valid else None
        at = f"  at ({pos[0][1]:.0f},{pos[0][2]:.0f})mm" if pos else ''
        print(f"\n{ref} [{c['value']}]{'  DNP' if c['dnp'] else ''}  {c['footprint']}  "
              f"sheet {c['sheet']}{at}" + (f"  LCSC {c['lcsc']}" if c['lcsc'] else ''))
        if c['description']:
            print(f"  {trunc(c['description'], 150)}")
        if rail:
            print(f"  supply {rail} = {rv} V")
        pins = nl.sympins(ref)
        for p in sorted(set(pins) | set(nl.cpins.get(ref, {})), key=natkey):
            nm, ty = pins.get(p, ('?', '?'))
            net = nl.cpins.get(ref, {}).get(p)
            if not net:
                print(f"  {p:<4} {nm:<16} {ty:<12} NOT ON ANY NET"); continue
            n = len(nl.nets[net])
            if net.startswith('unconnected-'):
                print(f"  {p:<4} {nm:<16} {ty:<12} floating"); continue
            if net in nl.railv:
                print(f"  {p:<4} {nm:<16} {ty:<12} {net} (rail {nl.railv[net]}V, {n} nodes)")
                continue
            peers = [f"{x['ref']}.{x['pin']}"
                     + (f"[{trunc(nl.value(x['ref']),14)}]" if nl.value(x['ref']) else '')
                     + (f"({trunc(x['fn'],14)})" if x['fn'] else '')
                     + nl.tag(x['ref'])
                     for x in sorted(nl.nets[net], key=lambda y: natkey(y['ref']))
                     if (x['ref'], x['pin']) != (ref, p)]
            flag = '  <-- SINGLE NODE' if n == 1 else ''
            print(f"  {p:<4} {nm:<16} {ty:<12} {net}{flag}")
            if peers:
                extra = len(peers) - a.peer_max
                shown = peers[:a.peer_max]
                print(f"       {', '.join(shown)}"
                      + (f"  ... +{extra} more (use `net` for the full list)" if extra > 0 else ''))

CMDS = {'around': c_around, 'notes': c_notes, 'draw': c_draw, 'summary': c_summary, 'comp': c_comp, 'net': c_net, 'pin': c_pin, 'find': c_find,
        'unconnected': c_unconnected, 'bom': c_bom, 'sheets': c_sheets, 'rails': c_rails,
        'walk': c_walk, 'path': c_path, 'check': c_check, 'diff': c_diff, 'divider': c_divider}

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('file')
    ap.add_argument('cmd', choices=list(CMDS))
    ap.add_argument('args', nargs='*')
    ap.add_argument('-d', '--depth', type=int, default=2)
    ap.add_argument('-o', '--out', default='', help='output path for `draw` (svg)')
    ap.add_argument('--through', default=None)
    ap.add_argument('--fanout', type=int, default=None)
    ap.add_argument('--as-drawn', action='store_true',
                    help='traverse DNP parts as if populated (default: open circuits)')
    ap.add_argument('--no-dnp', action='store_true', help=argparse.SUPPRESS)  # old default, now a no-op
    ap.add_argument('--rail', default='', help="e.g. '+5V=5,VCC_RF=3.3'")
    ap.add_argument('--vth', type=float, default=None,
                    help="for `divider` on a 3-resistor UVLO/OVLO string: the IC's "
                         "comparator threshold in volts, from the datasheet")
    ap.add_argument('--vth-tol', type=float, default=None,
                    help="for `divider`: the IC threshold's own accuracy in percent, "
                         "e.g. 1.7 for a part specified +/-1.7%%")
    ap.add_argument('--since', default='', help='for `check`: only findings new/fixed vs this old .net')
    ap.add_argument('--sheet', default='',
                    help="for `bom`: only components on this hierarchical sheet path and "
                         "below, e.g. '/Root/Rails/' (bom is board-wide by default)")
    ap.add_argument('--peer-max', type=int, default=12, help='max peers listed per net in `around`')
    ap.add_argument('--only', default='')
    ap.add_argument('--skip', default='')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--spec', action='store_true',
                    help='for `draw`: print the ksch spec instead of the SVG')
    ap.add_argument('--theme', default='kicad', help='for `draw`: kicad|mono|dark')
    ap.add_argument('--rules', action='store_true', help='print the check rule legend')
    ap.add_argument('--no-suppress', action='store_true',
                    help="for `check`: ignore knet.json's suppress list")
    ap.add_argument('-h', '--help', action='store_true')
    a = ap.parse_args()
    if a.help:
        print(__doc__); return
    # project config: knet.json next to the netlist. CLI flags win over config.
    cfg = {}
    try:
        cp = os.path.join(os.path.dirname(os.path.abspath(a.file)), 'knet.json')
        if os.path.exists(cp):
            cfg = json.load(open(cp))
    except Exception as e:
        print(f"(ignoring malformed knet.json: {e})", file=sys.stderr)
    if a.through is None:
        a.through = cfg.get('through', 'R,L,FB,F,FL,JP')
    if a.fanout is None:
        a.fanout = int(cfg.get('fanout', 8))
    ov = {}
    for k, v in (cfg.get('rails') or {}).items():
        try:
            ov[k] = float(v)
        except (TypeError, ValueError):
            pass
    for part in a.rail.split(','):
        if '=' in part:
            k, _, v = part.partition('=')
            try:
                ov[k.strip()] = float(v)
            except ValueError:
                pass
    a.suppress = {}
    if not a.no_suppress:
        for entry in cfg.get('suppress') or []:
            rule, _, tok = str(entry).partition(':')
            a.suppress.setdefault(rule.strip().upper(), set()).add(tok.strip())
    try:
        nl = Netlist(a.file, rail_overrides=ov, no_dnp=not a.as_drawn)
    except FileNotFoundError:
        print(f"no such netlist: {a.file}", file=sys.stderr); return 3
    return CMDS[a.cmd](nl, a) or 0

try:                      # piping to `head` should not print a traceback
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
