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
  knet.py FILE i2c                      each SDA/SCL bus: controller, targets with their
                                         7-bit address from strap pins, pull-ups, dup ERROR
  knet.py FILE powertree                sources -> links/FETs/regulators -> rails -> loads,
                                         with FB-divider Vout and table ILIM (knet.json)
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
  knet.py FILE revpol [--stack BT1,BT2] reversed pack / each reversed cell: which
                                         diodes + FET body diodes conduct (fused?),
                                         which IC pins go below GND (series R)
  knet.py FILE check --only DOMAIN,DECOUPLE
  knet.py FILE check --since old.net    only NEW findings vs an older export
  knet.py NEW.net diff OLD.net          what changed OLD -> NEW (rename-safe; never regenerates either)

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
import sys, os, json, argparse, signal
from collections import Counter, defaultdict, deque

from kcommon import Netlist, natkey, parse_args, refrange, smart_re, trunc
from knet_power import c_divider, c_i2c, c_powertree, c_rails
from knet_check import c_check
from knet_draw import c_draw
from knet_revpol import c_revpol

def c_notes(nl, a):
    s = nl.sch()
    if not s.files:
        print("no .kicad_sch files found next to the netlist"); return 1
    if not s.notes:
        print("no text notes in the schematic"); return
    bysheet = defaultdict(list)
    for path, x, y, text, fs, box in s.notes:
        bysheet[path].append((y, x, text, fs, box))
    small = Counter()
    for path in sorted(bysheet):
        print(f"\n=== {path}")
        for y, x, text, fs, box in sorted(bysheet[path]):
            small[path] += fs < 1.27
            tag = f"{'box ' if box else ''}{fs:g} mm{'  SMALL' if fs < 1.27 else ''}"
            body = text.replace('\n', '\n      ')
            print(f"  ({x:.0f},{y:.0f})  [{tag}]  {body}")
    if +small:
        print("\nnotes under 1.27 mm text (KiCad's default, hard to read printed): "
              + ', '.join(f"{p} {n}" for p, n in sorted(small.items()) if n))

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
            print("    (rail: showing loads only)")
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
        tag = ('  [no_connect pin type]' if ty == 'no_connect' else '' if f is None else
               ('  [NC flag in schematic]' if f else '  [NO NC flag - possibly forgotten]'))
        print(f"  {nd['ref']}.{nd['pin']:<4} {nd['fn']:<22} {ty}{sev}{tag}")
    if not have_sch:
        print("  (no .kicad_sch next to the netlist, so intentional-vs-forgotten "
              "cannot be distinguished)")

# ---------------- diff ----------------

def neighbours(nl, ref, pin):
    """Set of other pins sharing this pin's net. Immune to KiCad auto-net renaming."""
    n = nl.cpins.get(ref, {}).get(pin)
    if not n:
        return None
    return frozenset(f"{x['ref']}.{x['pin']}" for x in nl.nets[n] if (x['ref'], x['pin']) != (ref, pin))

def c_diff(nl, a):
    old = Netlist(a.args[0], no_dnp=nl.no_dnp, refresh=False)
    new = nl
    if old.date and new.date and old.date > new.date:
        print(f"note: the OLD side {old.path} ({old.date}) was exported after the NEW side "
              f"{new.path} ({new.date}). The order is `knet.py NEW.net diff OLD.net`; "
              f"swap the two files if you meant the reverse.", file=sys.stderr)
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
    print(f"old: {old.path}  ({old.date})  [the diff argument]")
    print(f"new: {new.path}  ({new.date})  [FILE]")
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
              f"sym {c['lib']}:{c['part']}  sheet {c['sheet']}{at}"
              + (f"  LCSC {c['lcsc']}" if c['lcsc'] else ''))
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
                f = nl.ncflag(ref, p)
                print(f"  {p:<4} {nm:<16} {ty:<12} " + ('NC (flagged)' if f else 'floating'
                      + ('  <-- no NC flag' if f is False else ''))); continue
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
        'walk': c_walk, 'path': c_path, 'check': c_check, 'diff': c_diff, 'divider': c_divider,
        'revpol': c_revpol, 'powertree': c_powertree, 'i2c': c_i2c}

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
    ap.add_argument('--stack', default='', help='for `revpol`: series cells, e.g. BT1,BT2 (default: every BT*)')
    ap.add_argument('--pair', default='', help='for `revpol`: a +/- net pair instead, e.g. +PACK,-BATT')
    ap.add_argument('--volts', type=float, default=None, help='for `revpol --pair`: the pair voltage')
    ap.add_argument('--vcell', type=float, default=4.2, help='for `revpol`: volts per cell (4.2)')
    ap.add_argument('--max', type=int, default=12, help='for `revpol`: lines per section (12)')
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
    a = parse_args(ap)
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
    a.cfg = cfg
    a.suppress = {}
    if not a.no_suppress:
        for entry in cfg.get('suppress') or []:
            rule, _, tok = str(entry).partition(':')
            a.suppress.setdefault(rule.strip().upper(), set()).add(tok.strip())
    try:
        nl = Netlist(a.file, rail_overrides=ov, no_dnp=not a.as_drawn, refresh=a.cmd != 'diff')
    except FileNotFoundError:
        print(f"no such netlist: {a.file}", file=sys.stderr); return 3
    return CMDS[a.cmd](nl, a) or 0

if __name__ == '__main__':
    if hasattr(signal, 'SIGPIPE'):                  # piping to `head`: no traceback
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main() or 0)
