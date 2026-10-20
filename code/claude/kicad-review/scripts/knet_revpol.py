"""knet.py `revpol`: the reverse-polarity what-if."""
import sys, re
from collections import defaultdict

from kcommon import GND_RE, eng, natkey, parse_value, refrange, trunc, tvs_standoff, unesc_disp
from knet_check import PINOUT, sym_roles

# ---------------- reverse-polarity what-if ----------------

VF = 0.7     # forward drop assumed for every diode junction and FET body diode
VTH = 2.0    # |Vgs| past which a FET channel counts as on (logic-level: 1-2.5 V)
PFET_VAL = re.compile(r'^(AO3401|AO3415|BSS84|SI2301|SI2305|DMP\d)', re.I)
NFET_VAL = re.compile(r'^(2N7002|BSS138|SI2308|AO3400|AON7534|CSD1\d|IRLML2502|DMN\d)', re.I)

def _zener_v(val):
    """Zener voltage from the part number: BZX585-C15 -> 15, BZX84-C5V1 -> 5.1,
    '5V1' -> 5.1, '12V' -> 12. None when it does not say."""
    m = re.search(r'(\d+)V(\d+)|-[A-C](\d+(?:\.\d+)?)\b|(?<![\w.])(\d+(?:\.\d+)?)V\b', val or '', re.I)
    if not m:
        return None
    return float(f"{m.group(1)}.{m.group(2)}") if m.group(1) else float(m.group(3) or m.group(4))

def _diode_pairs(nl, ref, pins, val, names):
    """[(label, anode pin, cathode pin)] for a diode part whose orientation can be
    read, else None. A PINOUT-table part uses its datasheet pinout by pad number
    even when the symbol is wrong (D6): the REAL part decides what conducts."""
    n3 = len(nl.sympins(ref)) == 3
    roles = next((r for rx, r, _ in PINOUT if rx.search(val)), None) if n3 else None
    if not roles and n3:
        roles = sym_roles(nl, ref)[0]
    if roles and set(roles) <= set('AKC') and all(str(i + 1) in pins for i in range(3)):
        n = {r: [str(i + 1) for i, x in enumerate(roles) if x == r] for r in 'AKC'}
        pp = ([(n['A'][0], n['C'][0]), (n['C'][0], n['K'][0])] if n['C']      # series pair
              else [(pa, pk) for pa in n['A'] for pk in n['K']])               # common A / K
        return [(f"{val} {pa}->{pk}", pa, pk) for pa, pk in pp]
    if sorted(x for x in names.values() if x in ('A', 'K')) == ['A', 'K']:    # A/K (+NC): BAT54W
        inv = {v: k for k, v in names.items()}
        return [(val, inv['A'], inv['K'])]
    if set(pins) == {'1', '2'}:                                                # KiCad: pad 1 is K
        return [(f"{val} (pin1=K by convention)", '2', '1')]
    return None

def _junctions(nl):
    """(junctions, fets, not-modelled) for revpol.
    junctions [(ref, label, anode net, cathode net, V to conduct)]: diodes forward
      at VF; a zener (voltage from its part number) or unidirectional TVS (~1.1x
      the part number's standoff) also backwards at breakdown; a bidirectional
      TVS both ways at breakdown; each I/O of an ESD array above its GND pin (and
      below a supply pin if it has one); both base junctions of a BJT; a FET's
      body diode.
    fets [(ref, 'N'|'P', gate, source, drain)]: revpol turns the channel on when
      the gate is driven past VTH.
    not-modelled {reason: [refs]}."""
    out, fets, skip = [], [], defaultdict(list)
    for ref, c in nl.comps.items():
        if nl.no_dnp and c['dnp']:
            continue
        pins, val = nl.conn_pins(ref), c['value'] or ''
        if len(set(pins.values())) < 2:
            continue                                  # nothing to conduct between
        txt = ' '.join(str(x) for x in (c['part'], c['description'], val,
                                          *c['props'].values()) if x)
        names = {pn: (nl.pinname(ref, pn) or '').upper() for pn in pins}
        if c['prefix'] == 'D':
            so = tvs_standoff(val)
            bidir = (re.search(r'\d(CA|C|B)\b', val) if so else
                     re.search(r'\bbi-?directional', txt, re.I)
                     or (sorted(names.values()) == ['A1', 'A2']
                         and not re.search(r'uni-?directional', txt, re.I)))
            if bidir:
                if so and len(pins) == 2:
                    x, y = pins.values()
                    lab = f"{val} bidirectional, Vbr ~{so * 1.1:.3g} V"
                    out += [(ref, lab, x, y, so * 1.1), (ref, lab, y, x, so * 1.1)]
                else:
                    skip['bidirectional TVS/ESD, breakdown not in the part number'].append(ref)
                continue
            rev = (_zener_v(val) if re.search(r'zener', txt, re.I) else None) or (so and so * 1.1)
            pairs = _diode_pairs(nl, ref, pins, val, names)
            if pairs is not None:
                for lab, pa, pk in pairs:
                    out.append((ref, lab, pins[pa], pins[pk], VF))
                    if rev:
                        out.append((ref, f"{lab} breakdown ~{rev:.3g} V", pins[pk], pins[pa], rev))
                continue
            gnd = [pn for pn, nm in names.items() if GND_RE.match(nm)]
            if gnd and len(pins) >= 3:                       # ESD array: steering diodes
                sup = [pn for pn, nm in names.items() if re.match(r'V(CC|DD|BUS|IO|P)\b', nm)]
                for pn, nm in names.items():
                    if pn not in gnd and pn not in sup:
                        out.append((ref, f"{val} {nm or pn} array", pins[gnd[0]], pins[pn], VF))
                        if sup:
                            out.append((ref, f"{val} {nm or pn} array", pins[pn], pins[sup[0]], VF))
                continue
            skip['multi-pin diode, orientation unreadable'].append(ref)
        elif c['prefix'] == 'Q':
            bjt = re.search(r'(?<![A-Z])(NPN|PNP)(?![A-Z])', txt, re.I)
            sn = {}
            for pn, n in pins.items():
                if names[pn]:
                    sn.setdefault(names[pn][:1], n)
            if bjt:
                pnp = bjt.group(1).upper() == 'PNP'
                if all(k in sn for k in 'BCE'):
                    for k in 'EC':
                        lab = f"{val} B-{k} ({bjt.group(1).upper()})"
                        out.append((ref, lab, sn[k], sn['B'], VF) if pnp else (ref, lab, sn['B'], sn[k], VF))
                else:
                    skip['BJT, B/C/E pin names unreadable'].append(ref)
                continue
            pol = ('P' if re.search(r'(?<![A-Z])P(-?CH|MOS)', txt, re.I) or PFET_VAL.search(val) else
                   'N' if re.search(r'(?<![A-Z])N(-?CH|MOS)', txt, re.I) or NFET_VAL.search(val) else None)
            if pol and 'S' in sn and 'D' in sn:
                a_, k_ = (sn['S'], sn['D']) if pol == 'N' else (sn['D'], sn['S'])
                out.append((ref, f"{val} body diode ({pol}MOS)", a_, k_, VF))
                if 'G' in sn:
                    fets.append((ref, pol, sn['G'], sn['S'], sn['D']))
            elif 'S' in sn and 'D' in sn:
                skip['FET, polarity not in its symbol/description/value'].append(ref)
    return out, fets, skip

def _conductors(nl, skip=(), join=()):
    """Union-find over nets joined by near-zero-ohm parts: fuses, ferrites,
    inductors, R <= 1 ohm, and solder jumpers whose footprint says Bridged,
    plus the extra `join` net pairs (FET channels revpol found on). Parts in
    `skip` are left open (to ask "does this fuse separate them")."""
    parent = {}
    def find(x):
        parent.setdefault(x, x)
        while parent[x] != x:
            parent[x] = parent[parent[x]]; x = parent[x]
        return x
    for ref, c in nl.comps.items():
        if (nl.no_dnp and c['dnp']) or ref in skip:
            continue
        pins = list(nl.conn_pins(ref).values())
        p = c['prefix']
        r = parse_value(c['value'], 'R') if p == 'R' else None
        if p == 'R' and r is not None and r <= 1.0 and len(pins) >= 2:   # incl. 4-pin Kelvin shunts
            for x in pins[1:]:
                parent[find(x)] = find(pins[0])
            continue
        if len(pins) != 2:
            continue
        if p in ('FB', 'L', 'F') or (p == 'JP' and 'Bridged' in (c['footprint'] or '')):
            parent[find(pins[0])] = find(pins[1])
    for x, y in join:
        parent[find(x)] = find(y)
    return find

def _stack(nl, a):
    """[(net, normal V, [(case label, V)]...)] nodes bottom->top and the case list."""
    v = a.vcell
    if a.pair:
        pn, nn = [x.strip() for x in a.pair.split(',')]
        vt = a.volts or v
        return [nn, pn], [('normal', [0.0, vt]), (f'{pn}/{nn} reversed', [0.0, -vt])], \
            f"pair {pn} (+) / {nn} (-), {vt:g} V"
    refs = [r.strip() for r in a.stack.split(',')] if a.stack else \
        sorted((r for r in nl.comps if nl.comps[r]['prefix'] == 'BT'), key=natkey)
    cells = {}
    for r in refs:
        pins = nl.conn_pins(r)
        nm = {(nl.pinname(r, pn) or pn): n for pn, n in pins.items()}
        pos, neg = nm.get('+', pins.get('1')), nm.get('-', pins.get('2'))
        if pos and neg:
            cells[r] = (pos, neg)
    if not cells:
        return None, None, "no cells: pass --stack BT1,BT2 or --pair NETP,NETN"
    pos_nets = {p for p, _ in cells.values()}
    bottom = [r for r, (p, n) in cells.items() if n not in pos_nets]
    order, cur = [], (bottom[0] if bottom else None)
    while cur and cur not in order:
        order.append(cur)
        cur = next((r for r, (p, n) in cells.items() if n == cells[cur][0]), None)
    nodes = [cells[order[0]][1]] + [cells[r][0] for r in order]
    def volts(signs):
        acc, out = 0.0, [0.0]
        for sg in signs:
            acc += sg * v; out.append(acc)
        return out
    cases = [('normal', volts([1] * len(order)))]
    if len(order) > 1:
        cases.append(('whole pack reversed', volts([-1] * len(order))))
    for i, r in enumerate(order):
        cases.append((f"{r} reversed", volts([-1 if j == i else 1 for j in range(len(order))])))
    return nodes, cases, ' -> '.join(f"{r} ({cells[r][1]} / {cells[r][0]})" for r in order) \
        + f", {v:g} V/cell"

def _solve(nl, nodes, volts, J, find):
    """Net-group voltages, strongest drive first: the cell nodes ('stack'), then
    one resistor hop from them (('R', ref, ohm) - a sense line, open-circuit
    voltage), then forward diodes into groups nothing else drives (('D', ref)).
    Diode propagation last, or it claims a resistively driven sense node first.
    Only a forward junction whose anode is not a ground net drives a node: a
    breakdown path or a GND-to-signal clamp conducts, but powers nothing."""
    V, src = {}, {}
    for n, v in zip(nodes, volts):
        V[find(n)], src[find(n)] = v, 'stack'
    for ref, c in nl.comps.items():
        if c['prefix'] != 'R' or (nl.no_dnp and c['dnp']):
            continue
        pins, ohm = list(nl.conn_pins(ref).values()), parse_value(c['value'], 'R')
        if len(pins) != 2 or ohm is None or ohm <= 1.0:
            continue
        for x, y in (pins, pins[::-1]):
            if src.get(find(x)) == 'stack' and find(y) not in V:
                V[find(y)], src[find(y)] = V[find(x)], ('R', ref, ohm)
    drive = [(ref, an, kn) for ref, _, an, kn, vf in J if vf <= VF and not nl.is_gnd(an)]
    for _ in range(12):
        moved = False
        for ref, an, kn in drive:
            ga, gk = find(an), find(kn)
            if ga in V and gk not in V:
                V[gk], src[gk], moved = V[ga] - VF, ('D', ref), True
        if not moved:
            break
    return V, src

def c_revpol(nl, a):
    """`revpol`: what a reversed cell or pack does. Per case: diode / TVS / FET
    body-diode junctions that start conducting (between two cell-driven nodes =
    a loop current: is a fuse in it?), FET channels the cells switch, ICs whose
    ground floats, and IC pins pushed below their own GND or above their normal
    level, with the series R back to the cell and the ESD-diode current that R
    allows - each one an abs-max check for `kdoc.py grep`. Only changes vs the
    normal case print."""
    nodes, cases, desc = _stack(nl, a)
    if not nodes:
        print(desc, file=sys.stderr); return 1
    J, fets, skipped = _junctions(nl)
    fuses = [r for r, c in nl.comps.items() if c['prefix'] == 'F'
             and not (nl.no_dnp and c['dnp']) and len(nl.conn_pins(r)) == 2]

    def solve(volts):
        """V, src, find, FETs on: re-solve until the set of FETs whose gate the
        cells drive past VTH stops changing. An undriven source sits near its
        drain (the body diode), so the drain stands in for it."""
        on, find = [], _conductors(nl)
        for _ in range(4):
            V, src = _solve(nl, nodes, volts, J, find)
            now = []
            for ref, pol, g, s_, d in fets:
                gv, sv = V.get(find(g)), V.get(find(s_), V.get(find(d)))
                if gv is not None and sv is not None and (gv - sv if pol == 'N' else sv - gv) > VTH:
                    now.append(ref)
            if now == on:
                break
            on = now
            find = _conductors(nl, join=[(s_, d) for r, _, _, s_, d in fets if r in on])
        return V, src, find, on

    def pin_v(V, src, find, net):
        """(volts, series ohms, via ref) for a net, directly or one resistor away."""
        g = find(net)
        if g in V:
            sv = src.get(g)
            return (V[g], sv[2], sv[1]) if isinstance(sv, tuple) and sv[0] == 'R' else (V[g], 0.0, '')
        best = None
        for nd in nl.nets.get(net, []):
            r = nd['ref']
            if nl.comps.get(r, {}).get('prefix') != 'R' or (nl.no_dnp and nl.comps[r]['dnp']):
                continue
            o = nl.other_pin(r, nd['pin'])
            on = nl.cpins[r].get(o) if o else None
            ohm = parse_value(nl.value(r), 'R')
            if on and find(on) in V and ohm is not None and (best is None or ohm < best[1]):
                best = (V[find(on)], ohm, r)
        return best

    def ic_levels(V, src, find):
        """({(ref, pin): (V vs its GND, ohms, via, gnd net, supply V vs GND or None)},
        {IC ref: (GND net, [(pin, V, ohms, via)] still cell-driven)} for ICs whose
        ground nothing drives)."""
        out, floating = {}, {}
        for ref, c in nl.comps.items():
            if c['prefix'] != 'U' or (nl.no_dnp and c['dnp']):
                continue
            pins = nl.conn_pins(ref)
            gnd = next((n for pn, n in pins.items() if nl.is_gnd(n)
                        or re.match(r'(GND|VSS|PGND|AGND)', nl.pinname(ref, pn) or '', re.I)), None)
            if not gnd:
                continue
            if find(gnd) not in V:
                floating[ref] = (gnd, [(pn,) + pv for pn, n in pins.items()
                                       for pv in [pin_v(V, src, find, n)] if pv])
                continue
            g0 = V[find(gnd)]
            sup = [pin_v(V, src, find, n) for pn, n in pins.items()
                   if nl.pintype(ref, pn) == 'power_in' and n != gnd and not nl.is_gnd(n)]
            sup = max((p[0] - g0 for p in sup if p), default=None)
            for pn, n in pins.items():
                pv = pin_v(V, src, find, n)
                if pv and n != gnd:
                    out[(ref, pn)] = (pv[0] - g0, pv[1], pv[2], gnd, sup)
        return out, floating

    def esd_amps(excess, ohm):
        """What drives the pin's ESD diode, as text: `excess` volts past its clamp."""
        if excess <= 0:
            return ''
        return (f", ~{eng(excess / ohm, 'A')} into its ESD diode" if ohm
                else ", ESD diode current limited only by the cells/fuse")

    base = None
    print(f"revpol: {desc}   (Vf {VF} V per junction; DNP = open; FET on at |Vgs| > {VTH:g} V)")
    for label, volts in cases:
        V, src, find, on = solve(volts)
        driven = lambda n: src.get(find(n)) == 'stack' or (isinstance(src.get(find(n)), tuple)
                                                            and src[find(n)][0] == 'R')
        fwd = {(ref, lab): (an, kn) for ref, lab, an, kn, vf in J
               if find(an) in V and find(kn) in V and V[find(an)] - V[find(kn)] > max(vf, VF / 2)}
        lv, floating = ic_levels(V, src, find)
        if label == 'normal':
            base = (set(fwd), lv, set(on), floating)
            if on:
                print(f"\n=== normal operation: FET channel(s) on, gate driven from the cells: "
                      f"{' '.join(sorted(on, key=natkey))}")
            already = [(k, v) for k, v in fwd.items() if driven(v[0]) and driven(v[1])
                       and not re.search(r'LED', nl.comps[k[0]]['part'] + nl.comps[k[0]]['value'], re.I)]
            if already:
                print("\n=== normal operation: already conducting between two driven nodes "
                      "(intended? LEDs excluded)")
            for (ref, lab), (an, kn) in already:
                print(f"    {ref:<5} {trunc(lab, 34):<34} A {an} {V[find(an)]:+.1f} V -> K {kn} "
                      f"{V[find(kn)]:+.1f} V")
            continue
        print(f"\n=== {label}:  " + '   '.join(f"{n} {v:+.1f} V" for n, v in zip(nodes, volts)))
        flips = ([f"{r} off" for r in sorted(base[2] - set(on), key=natkey)]
                 + [f"{r} ON" for r in sorted(set(on) - base[2], key=natkey)])
        if flips:
            print(f"  FET channels vs normal: {', '.join(flips)}")
        lost = {r: g for r, g in floating.items() if r not in base[3]}
        # a floating-ground IC conducts only between cell-driven pins > VF apart
        spread = {r: (max(v for _, v, _, _ in drv) - min(v for _, v, _, _ in drv)) if drv else 0
                  for r, (g, drv) in lost.items()}
        iso = [r for r in lost if spread[r] <= VF]
        if iso:
            print(f"  {len(iso)} IC(s) lose their ground ({', '.join(sorted({lost[r][0] for r in iso}))} "
                  f"undriven), no two pins driven > {VF} V apart - isolated from the cells: {refrange(iso)}")
        hang = sorted((r for r in lost if spread[r] > VF), key=natkey)
        if hang:
            print("  ground undriven but pins still on the cells (the IC's internal/ESD network "
                  "conducts between them; pin vs pin, not vs GND):")
        for r in hang:
            byv = defaultdict(list)
            for pn, v, o, via in lost[r][1]:
                byv[round(v, 1)].append(f"{nl.pinname(r, pn) or pn}"
                                        + (f" ({eng(o, 'ohm')} {via})" if via else ''))
            print(f"    {r:<5} {trunc(nl.value(r), 16):<16} spread {max(byv) - min(byv):.1f} V   "
                  + ';  '.join(f"{v:+.1f} V: {', '.join(ps[:3])}" + (f" +{len(ps) - 3}" if len(ps) > 3 else '')
                               for v, ps in sorted(byv.items())))
        new = [(k, v) for k, v in fwd.items() if k not in base[0]]
        loop = [(k, v) for k, v in new if src.get(find(v[0])) == 'stack' and src.get(find(v[1])) == 'stack']
        lim = [(k, v) for k, v in new if (k, v) not in loop and driven(v[0]) and driven(v[1])]
        feed = [(k, v) for k, v in new if (k, v) not in loop and (k, v) not in lim]
        without = {}
        def fuses_on(net):
            """Fuses that alone separate `net` from the cell terminal driving it."""
            sn = [n for n in nodes if find(n) == find(net)]
            joins = [(s_, d) for r, _, _, s_, d in fets if r in on]
            for f in fuses:
                without.setdefault(f, _conductors(nl, skip={f}, join=joins))
            return [f for f in fuses if sn and all(without[f](net) != without[f](x) for x in sn)]
        if loop:
            print("  conducting between two cell-driven nodes (a loop current - only the cells limit it):")
        for (ref, lab), (an, kn) in loop:
            onpath = sorted(set(fuses_on(an) + fuses_on(kn)), key=natkey)
            print(f"    {ref:<5} {trunc(lab, 34):<34} A {an} {V[find(an)]:+.1f} V -> K {kn} "
                  f"{V[find(kn)]:+.1f} V   " + (f"fuse in loop: {' '.join(onpath)}"
                                              if onpath else "NO FUSE in this loop"))
        if lim:
            print("  conducting between driven nodes through a series resistor:")
        for (ref, lab), (an, kn) in lim:
            r_ = [src[find(x)] for x in (an, kn) if isinstance(src.get(find(x)), tuple)]
            print(f"    {ref:<5} {trunc(lab, 34):<34} A {an} {V[find(an)]:+.1f} V -> K {kn} "
                  f"{V[find(kn)]:+.1f} V   limited by " + ' + '.join(f"{x[1]} {eng(x[2], 'ohm')}" for x in r_))
        if feed:
            print("  now conducting into another node:")
        for (ref, lab), (an, kn) in feed[:a.max]:
            print(f"    {ref:<5} {trunc(lab, 34):<34} {an} {V[find(an)]:+.1f} V -> {kn} "
                  f"{V[find(kn)]:+.1f} V")
        if len(feed) > a.max:
            print(f"    ... +{len(feed) - a.max} more (--max)")
        bad = []
        for k, (v, ohm, via, gnd, sup) in sorted(lv.items(), key=lambda kv: (natkey(kv[0][0]), natkey(kv[0][1]))):
            v0 = base[1].get(k, (None,))[0]
            if v < -0.3 and (v0 is None or v0 >= -0.3):
                bad.append((k, v, ohm, via, 'below its GND' + esd_amps(-v - VF, ohm)))
            elif v0 is not None and v > v0 + 0.5 and v > 0.3:
                over = f" and {v - sup:.1f} V over its supply pin" if sup is not None and v > sup + 0.3 else ''
                bad.append((k, v, ohm, via, f'above its normal {v0:+.1f} V{over}'
                            + (esd_amps(v - sup - VF, ohm) if over else '')))
        if bad:
            print("  IC pins pushed outside their normal range (vs each IC's own GND pin; "
                  "abs-max is usually -0.3 V, ESD-diode injection ~10 mA):")
        for (ref, pn), v, ohm, via, why in bad[:a.max * 2]:
            nm = nl.pinname(ref, pn)
            print(f"    {ref}.{pn:<4} {trunc(nm, 12):<12} {v:+.1f} V {why.split(',')[0]}"
                  + (f", via {eng(ohm, 'ohm')} ({via})" if via else ', direct')
                  + ''.join(',' + x for x in why.split(',')[1:])
                  + f"   -> kdoc.py grep '{unesc_disp(nm) or pn}' -d "
                  + (re.match(r'[A-Za-z]+\d+', nl.value(ref) or '') or [nl.value(ref)])[0])
        if len(bad) > a.max * 2:
            print(f"    ... +{len(bad) - a.max * 2} more (--max)")
        if not (loop or lim or feed or bad or lost or flips):
            print("  nothing new conducts and no IC pin leaves its normal range")
    if skipped:
        print("\nnot modelled: " + '; '.join(f"{why}: {refrange(refs)}" for why, refs in skipped.items()))
    print("\nModel: cells as ideal sources, near-0-ohm parts (F, FB, L, R<=1, bridged JP) as wires,\n"
          "every junction a 0.7 V diode (zener/TVS breakdown from the part number), a FET\n"
          "channel on when the cells drive its gate past the threshold, ICs as open circuits\n"
          "whose ESD diodes only clamp: a pin 'below its GND' conducts through them at the\n"
          "current shown. A what-if to aim datasheet checks, not a simulation.")
    return 0
