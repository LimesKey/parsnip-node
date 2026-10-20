"""knet.py `check`: the rule-based netlist review (RULES, gen_findings)."""
import sys, os, re, json
from collections import defaultdict

from kcommon import (GND_RE, Netlist, eng, fp_pads, natkey, parse_value, print_findings, refrange,
                     suppressed, tvs_standoff, _fkey)
from knet_power import caps_on

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
    'RFSTUB':    'RF / 50 ohm net with more than 2 parts in series (shunts to GND not counted)',
    'DNPPATH':   'DNP part sits in series in a signal path (open circuit on the board)',
    'CAPRATING': 'capacitor voltage rating below 2x the rail it sits on',
    'NOVALUE':   'component with no value or no footprint',
    'GNDISLAND': 'GND-named pins wired together but isolated from the main GND net',
    'CLAMPRATING': 'TVS standoff voltage below the rail it clamps',
    'PARPIN':    'a paralleled pin left dangling while a same-named pin is on a real net',
    'PINOUT':    "symbol's pin order contradicts the part's SOT-23 pinout or its power-FET footprint's pads",
    'FPPAD':     'footprint pad with no symbol pin (imports with no net), or a wired pin with no pad',
}

# PINOUT: the real SOT-23/323 pin roles of parts often drawn with a generic
# symbol. Letters per pin 1..3: A anode, K cathode, C series centre, G/S/D.
# Symbol side comes from the generic symbol's name suffix (Device:D_Dual_Series_ACK
# = 1 A, 2 centre, 3 K; Q_NMOS_GSD), else from G/S/D pin names. Stock DIODE pin
# names are never trusted: Diode:BAV99's hidden names K/A/K contradict its own
# graphics, and Diode:BAT54A has none. Extend the table, don't loosen the match.
PINOUT = ((re.compile(r'^(BAV99|BAV199|BAT54S)\b', re.I), 'AKC', 'a series pair, 1=A 2=K 3=centre'),
          (re.compile(r'^BAT54A\b', re.I), 'KKA', 'common anode, 1=K 2=K 3=A'),
          (re.compile(r'^BAT54C\b', re.I), 'AAK', 'common cathode, 1=A 2=A 3=K'),
          (re.compile(r'^(2N7002|SI2308|AO340[01]|BSS138|BSS84)', re.I), 'GSD', '1=G 2=S 3=D'))
SOT3 = re.compile(r'SOT-23(?!-\d)|SOT-323|SC-70(?!-\d)', re.I)

def sym_roles(nl, ref):
    """(pin-role string for pins 1..3, where it came from) or (None, '')."""
    m = re.search(r'_([AKCGSD]{3})$', nl.comps[ref]['part'])
    if m:
        return m.group(1), 'its name suffix'
    nm = ''.join((nl.pinname(ref, p) or '?')[:1].upper() for p in '123')
    return (nm, 'its pin names') if sorted(nm) == ['D', 'G', 'S'] else (None, '')

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

def led_pull(nl, net, fanout, via_r=True):
    """An LED from `net` whose far side is a rail or pulled to one: an open-drain
    status pin sinking an indicator (STAT -> LED -> 3k -> rail), or the same with
    the resistor first (STAT -> 3k -> LED -> rail, parsnip's U8 STAT / R66 / D8)."""
    for nd in nl.nets.get(net, []):
        c = nl.comps.get(nd['ref'], {})
        op = nl.other_pin(nd['ref'], nd['pin'])
        far = nl.cpins[nd['ref']].get(op) if op else None
        if not far or far == net:
            continue
        if c.get('prefix') == 'R' and via_r and len(nl.conn_pins(nd['ref'])) == 2:
            if led_pull(nl, far, fanout, via_r=False):
                return True
            continue
        if c.get('prefix') != 'D' or not re.search(r'LED', f"{c.get('part')} {c.get('value')}", re.I):
            continue
        if (far in nl.railv and nl.railv[far] > 0) or (via_r and find_pull(nl, far, fanout)):
            return True
    return False

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

    # PINOUT: see the PINOUT table
    for ref, c in nl.comps.items():
        if not SOT3.search(c['footprint'] or '') or len(nl.sympins(ref)) != 3:
            continue
        want = next(((r, d) for rx, r, d in PINOUT if rx.search(c['value'] or '')), None)
        have, src = sym_roles(nl, ref)
        if want and have and have != want[0]:
            fix = (f" - use {c['lib']}:{c['part'][:-3]}{want[0]}"
                   if re.search(r'_[AKCGSD]{3}$', c['part']) else '')
            add('ERROR', 'PINOUT', f"{ref} {c['value']} on {c['lib']}:{c['part']}: "
                f"symbol pins 1-3 are {have} (from {src}), but a {c['value']} is "
                f"{want[1]} ({want[0]}) - it is wired with pins swapped{fix}", [ref])

    # Footprint pads, from the .kicad_mod each footprint field names (fp_pads):
    # PARPIN and FPPAD compare them with the symbol's pin numbers.
    projdir = os.path.dirname(os.path.abspath(nl.path))
    padcache = {}
    def pads_of(ref):
        fp = nl.comps[ref]['footprint']
        if fp not in padcache:
            padcache[fp] = fp_pads(fp, projdir) if fp else None
        return padcache[fp]
    # PINOUT, power-FET packages: a footprint that numbers its pads by function (KiCad's
    # VSONP/TDSON/VSON NexFET: pad 1 = pins 1-3, 2 = pin 4, 3 = pins 5-8 + EP) fixes
    # S, G and D by pad, which FPPAD cannot see (the numbers match); a generic
    # Q_[NP]MOS_xyz symbol on it must name S, G, D in that pad order
    for ref, c in nl.comps.items():
        m = re.match(r'^Q_[NP]MOS_([DGS]{3})$', c['part'] or '')
        cnt = fp_pads(c['footprint'], projdir, count=True) if m and c['footprint'] else None
        if not cnt or set(cnt) != {'1', '2', '3'} or cnt['2'] != 1:
            continue
        s_, d_ = sorted(('1', '3'), key=lambda n: cnt[n])
        if cnt[s_] != 3 or cnt[d_] < 4:
            continue
        want = ''.join({'2': 'G', s_: 'S', d_: 'D'}[n] for n in '123')
        if m.group(1) != want:
            add('ERROR', 'PINOUT', f"{ref} {c['value']} on {c['lib']}:{c['part']}: "
                f"{c['footprint'].split(':')[-1]} numbers its pads by function (pad {s_} = 3 pins: S, "
                f"pad 2 = G, pad {d_} = {cnt[d_]} pieces with the EP: D), so pins 1-3 must be {want}; "
                f"as drawn they are {m.group(1)} and the FET mounts with "
                f"{'D and S swapped' if m.group(1).translate(str.maketrans('DS', 'SD')) == want else 'pins swapped'}"
                f" - use {c['lib']}:{c['part'][:-3]}{want}", [ref])

    def real(ref, pin):
        n = nl.cpins.get(ref, {}).get(pin)
        return n if n and not n.startswith('unconnected-') else None

    # PARPIN: a paralleled pad (two or more pins sharing the same declared NAME,
    # e.g. multiple BAT pins on a battery-charger IC) where one is wired to a real
    # net and its sibling is left floating. FLOATPWR only catches this when the
    # dangling pin is typed power_in; a symbol that types a paralleled pin merely
    # 'passive' (seen on BQ25798 pin 23, "BAT") slips past it, so this rule matches
    # on pin NAME instead of pin type. A dangling pin whose number the footprint
    # has no pad for is fused into a sibling's pad (TPS25751 REF0038A: pad 20 spans
    # pins 20-22), so the copper is right: INFO, not ERROR.
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
                pads = pads_of(ref)
                for p in dangling:
                    host = [w for w in wired if pads and w in pads]
                    if pads is not None and p not in pads and host:
                        w = min(host, key=lambda w: abs(int(w) - int(p))
                                if w.isdigit() and p.isdigit() else 0)
                        add('INFO', 'PARPIN', f"{ref}.{p} ({nm}) has no pad of its own in "
                            f"{c['footprint'].split(':')[-1]}: fused into pad {w} "
                            f"({nl.cpins[ref][w]}) - the copper is right", [ref])
                        continue
                    add('ERROR', 'PARPIN',
                        f"{ref}.{p} ({nm}) is not connected, but {ref}.{wired[0]} "
                        f"(same pin name '{nm}') is on {nl.cpins[ref][wired[0]]} - "
                        f"a paralleled pad likely left dangling", [ref])

    # FPPAD: pads the symbol has no pin for import with NO net (an exposed pad
    # that should be GND, a FET's drain tab), and a wired pin the footprint has
    # no pad for loses that connection on the board. KiCad's own parity check
    # only runs on parts already on the board. 'MP' is KiCad's name for a
    # mechanical mounting pad and is meant to have no net.
    unresolved, resolved = defaultdict(list), 0
    for ref, c in nl.comps.items():
        pads = pads_of(ref)
        if pads is None:
            if c['footprint']:
                unresolved[c['footprint']].append(ref)
            continue
        resolved += 1
        pins = nl.sympins(ref)
        for pad in sorted(set(pads) - set(pins) - {'MP'}, key=natkey):
            add('WARN', 'FPPAD', f"{ref} pad {pad} ({pads[pad]}) of {c['footprint'].split(':')[-1]} "
                f"has no symbol pin - it imports with no net (an exposed/thermal pad or "
                f"tab: GND or floating per the datasheet?)", [ref])
        for pin in sorted(set(pins) - set(pads), key=natkey):
            if real(ref, pin):
                add('ERROR', 'FPPAD', f"{ref}.{pin} ({nl.pinname(ref, pin)}) is on {real(ref, pin)} "
                    f"but {c['footprint']} has no pad {pin} - that connection is lost on "
                    f"the board", [ref])
    if unresolved:
        fps = sorted(unresolved, key=lambda f: -len(unresolved[f]))
        add('INFO', 'FPPAD', (f"{len(fps)} footprint(s) not found through any fp-lib-table, pads "
            f"unchecked: " if resolved else "no footprint library reachable (no fp-lib-table or "
            "KiCad install found), pad checks skipped: ")
            + ', '.join(f"{f} ({refrange(unresolved[f])})" for f in fps[:4])
            + (' ...' if len(fps) > 4 else ''), [r for f in fps for r in unresolved[f]])

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

    # SOLO. A lone net whose short name lives on another sheet is half of a split
    # label: a local label on a parent and on its subsheet never connect.
    short = defaultdict(list)
    for n in nl.nets:
        short[n.rsplit('/', 1)[-1]].append(n)
    for n, nodes in nl.nets.items():
        if len(nodes) == 1 and not n.startswith('unconnected-'):
            nd = nodes[0]
            twin = [m for m in short[n.rsplit('/', 1)[-1]] if m != n]
            hint = ''.join(f"; {m} ({', '.join(x['ref'] + '.' + x['pin'] for x in nl.nets[m][:4])}) has the "
                           f"same name on another sheet: local labels never cross sheets, use a "
                           f"hierarchical label + sheet pin or a global label" for m in twin[:2])
            add('ERROR', 'SOLO', f"net {n} has only {nd['ref']}.{nd['pin']} on it{hint}",
                [nd['ref']] + sorted({x['ref'] for m in twin[:2] for x in nl.nets[m]}, key=natkey))

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
            elif not find_pull(nl, net, a.fanout) and not led_pull(nl, net, a.fanout):
                add('WARN', 'OCNOPULL', f"{ref}.{pin} ({nm}) is open-drain on {net} "
                    f"with no pull-up to a rail", [ref])

    # I2CPULL
    for n, nodes in nl.nets.items():
        base = n.split('/')[-1].upper()
        if not re.search(r'\b(SDA|SCL)\b|^(SDA|SCL)', base):
            continue
        if not find_pull(nl, n, a.fanout):
            if re.search(r'SPI|MOSI|MISO|SCK|SCLK|CIPO|COPI', base):
                add('INFO', 'I2CPULL', f"{n}: dual I2C/SPI name, no pull-up - needs one only "
                    f"if it runs as I2C", [nd['ref'] for nd in nodes])
            else:
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

    # RFSTUB - DNP parts are open pads, not stubs, so they are reported but not
    # counted; nor is a 2-pin part whose other end is GND (a shunt element: pad
    # shunt R, ESD diode, matching L/C), which sits AT the node. Net names count
    # as RF only when the project has no RF netclass: once it does, a name like
    # VCC_RF or ANT_DETECT is a DC line the designer chose not to class.
    by_name = not any('RF' in c.upper() for c in nl.netclass.values())
    def shunt(ref, pin):
        """A 2-pin part to GND, or an RF choke: an L whose far end is bypassed
        to GND by a capacitor (a bias tee) or only feeds resistors >= 1k (an
        antenna-detect / DC-sense line) - high impedance at RF by design."""
        cp = nl.conn_pins(ref)
        far = [x for p, x in cp.items() if p != pin]
        if len(cp) != 2 or not far or nl.comps[ref]['prefix'] not in ('R', 'C', 'L', 'D', 'FB'):
            return False                      # a connector or IC is a port, never a shunt
        if nl.is_gnd(far[0]):
            return True
        if nl.comps[ref]['prefix'] != 'L':
            return False
        rest = [nd['ref'] for nd in nl.nets.get(far[0], []) if nd['ref'] != ref]
        return any(not dnp for _, _, dnp in caps_on(nl, far[0])) or (rest and all(
            nl.comps[r]['prefix'] == 'R' and (parse_value(nl.value(r), 'R') or 0) >= 1e3 for r in rest))
    for n, nodes in nl.nets.items():
        cls = nl.netclass.get(n, '')
        rf = 'RF' in cls.upper() or (by_name and re.search(r'ANT|_RF\b|RF_IN|RFIN', n.upper()))
        if not rf:
            continue
        pop = sorted({nd['ref'] for nd in nodes if not nl.comps.get(nd['ref'], {}).get('dnp')
                      and not shunt(nd['ref'], nd['pin'])}, key=natkey)
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
            old = Netlist(a.since, no_dnp=nl.no_dnp, refresh=False)
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
