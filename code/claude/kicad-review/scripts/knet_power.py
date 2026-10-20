"""knet.py `rails`, `powertree`, `i2c`, `divider`: what feeds each rail, I2C addresses, divider trip points."""
import re, json, math
from collections import defaultdict, deque

from kcommon import eng, natkey, parse_value, refrange, trunc

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
        elif pfx in ('L', 'FB', 'F', 'FL', 'JP') or (pfx == 'R' and _ohms_le(nl.value(ref), 10)):
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

# ---------------- power tree ----------------

_PWR_CLASS = re.compile(r'PWR|POWER|SUPPLY', re.I)
_SW_PIN = re.compile(r'^(SW|LX|PH|BOOT|BST|CB)\d*$', re.I)
_SRC_NAME = re.compile(r'VBUS|SOLAR|VIN|VBAT|BATT?\b|PWR_IN|DC_?IN', re.I)
_IN_PIN = re.compile(r'^(P?VIN\d*|IN\d*|VBUS\w*|VAC\d+|BAT)$', re.I)
_OUT_PIN = re.compile(r'^(P?VOUT\d*|OUT\d*|SYS|SW\d*|LX\d*|PPHV|LDO_?\w*|BAT|PMID|REGN|VREG\w*)$', re.I)
_LOAD_PFX = ('U', 'IC', 'J', 'P', 'CN', 'Y', 'X', 'DS', 'M', 'BZ', 'LS', 'K')

def _ohms_le(value, lim):
    """value parses to <= lim ohms. `(parse_value(v) or 1e9) <= 10` read a 0 R
    (0.0, falsy) as 1e9, so 0 R links were never followed."""
    v = parse_value(value, 'R')
    return v is not None and v <= lim

def power_nets(nl):
    """{net: volts or None}: rails by name (+ --rail/knet.json), nets in a power
    netclass, and nets a power_out pin drives other than a switch node. GND out.
    Names alone missed VSYS, PPHV, CHG_VBUS and +PACK."""
    out = {n: v for n, v in nl.railv.items() if not nl.is_gnd(n)}
    for n, c in nl.netclass.items():
        if any(_PWR_CLASS.search(x) for x in str(c).split(',')) and not nl.is_gnd(n):
            out.setdefault(n, None)
    for ref, pins in nl.cpins.items():
        for p, n in pins.items():
            if (nl.pintype(ref, p) == 'power_out' and not _SW_PIN.match(nl.pinname(ref, p) or '')
                    and not nl.is_gnd(n) and not n.startswith('unconnected-')):
                out.setdefault(n, None)
    return out

def power_edges(nl):
    """{net: [(to_net, ref)]}: what power can flow through. Links (L, FB, F, R <= 10
    ohm, a closed solder jumper) and FET channels both ways; an IC from each
    input-named pin's net to each output-named pin's net (pin types are mostly
    `unspecified` on imported symbols, so names decide). GND is never a node."""
    E = defaultdict(list)
    ok = lambda n: n and not nl.is_gnd(n) and not n.startswith('unconnected-')
    for ref, c in sorted(nl.comps.items(), key=lambda kv: natkey(kv[0])):
        if nl.no_dnp and c['dnp']:
            continue
        pins, pfx = nl.cpins.get(ref, {}), c['prefix']
        nm = {p: (nl.pinname(ref, p) or p) for p in pins}
        part = (c.get('part') or '').lower()
        if pfx in ('U', 'IC'):
            ins = {pins[p] for p in pins if _IN_PIN.match(nm[p]) and ok(pins[p])}
            pout = {pins[p] for p in pins if ok(pins[p]) and nl.pintype(ref, p) == 'power_out'
                    and not _SW_PIN.match(nm[p])}
            outs = pout | {pins[p] for p in pins if ok(pins[p]) and _OUT_PIN.match(nm[p])}
            for x in sorted(ins):
                E[x] += [(y, ref) for y in sorted(outs - {x})]
            # a load's own supply pin feeds only a typed power_out (NEO VCC -> VCC_RF)
            for x in sorted({pins[p] for p in pins if ok(pins[p]) and re.match(r'^(VCC|VDD)', nm[p], re.I)} - ins):
                E[x] += [(y, ref) for y in sorted(pout - {x})]
            continue
        if pfx == 'D':
            # a series power diode, anode -> cathode only: +3V3 -> D27 -> U23 VIN. Only
            # into a named rail or a regulator/switch IN, so a clamp or LED is no branch
            ak = {nm[p].upper(): pins[p] for p in pins}
            an, ca = ak.get('A'), ak.get('K')
            if (len(pins) == 2 and ok(an) and ok(ca) and an != ca
                    and not re.search(r'LED|ZENER|TVS', f"{part} {c['value']}", re.I)
                    and (ca in nl.railv or any(_IN_PIN.match(nl.pinname(x['ref'], x['pin']) or '')
                                               and nl.comps[x['ref']]['prefix'] in ('U', 'IC')
                                               for x in nl.nets.get(ca, [])))):
                E[an].append((ca, ref))
            continue
        if pfx == 'Q':
            d = {pins[p] for p in pins if nm[p].upper().startswith('D')}
            s = {pins[p] for p in pins if nm[p].upper().startswith('S')}
            ends = (d | s) if len(d) == len(s) == 1 else set()
        elif pfx == 'JP' and 'bridged12' in part:
            ends = {pins.get('1'), pins.get('2')}
        elif pfx in ('L', 'FB', 'F', 'JP') or (pfx == 'R' and _ohms_le(c['value'], 10)):
            ends = set(pins.values()) if len(pins) == 2 and 'open' not in part else set()
        else:
            continue
        if len(ends) == 2 and all(ok(n) for n in ends):
            x, y = sorted(ends)
            E[x].append((y, ref)); E[y].append((x, ref))
    return E

def power_sources(nl):
    """[(net, label)]: the top of each cell stack, then connector pins named like a
    supply input (VBUS, SOLAR, VIN, BAT) or on a net so named."""
    out, plus, minus = [], {}, set()
    for ref, c in nl.comps.items():
        if c['prefix'] == 'BT' and not (nl.no_dnp and c['dnp']):
            for p, n in nl.cpins[ref].items():
                (plus.__setitem__(n, ref) if nl.pinname(ref, p) == '+' else minus.add(n))
    bt = {r: {nl.pinname(r, p): n for p, n in nl.cpins[r].items()} for r in plus.values()}
    for top in sorted(set(plus) - minus, key=natkey):
        stack, n = [], top
        while n in plus and plus[n] not in stack:
            stack.append(plus[n]); n = bt[plus[n]].get('-')
        out.append((top, f"cells {'+'.join(stack)}"))
    seen = {n for n, _ in out}
    for ref, c in sorted(nl.comps.items(), key=lambda kv: natkey(kv[0])):
        if c['prefix'] not in ('J', 'P', 'CN') or (nl.no_dnp and c['dnp']):
            continue
        for p, n in nl.cpins[ref].items():
            pn = nl.pinname(ref, p) or ''
            if (n not in seen and not nl.is_gnd(n) and not n.startswith('unconnected-')
                    and (_SRC_NAME.search(pn) or _SRC_NAME.search(n.rsplit('/', 1)[-1]))):
                seen.add(n); out.append((n, f"{ref} {pn or p}"))
    return out

def power_tree(nl):
    """One BFS per source, cells first: (sources, parent {net: (from, ref)},
    also {net: [(from, ref)]}). A net belongs to the first source that reaches it,
    so the system rails hang off the battery and USB/solar show up as "also fed"
    (a level-synchronous BFS over all sources let solar claim the charger)."""
    E, src = power_edges(nl), power_sources(nl)
    roots = {n for n, _ in src}
    parent, also = {}, defaultdict(list)
    for root, _ in src:
        q, mine = deque([root]), {root}
        while q:
            n = q.popleft()
            for m, ref in E.get(n, []):
                if m == n or parent.get(n) == (m, ref) or m in roots:
                    continue              # walked back, or into where power enters
                if m not in parent:
                    parent[m] = (n, ref); mine.add(m); q.append(m)
                elif m not in mine and parent[m] != (n, ref) and (n, ref) not in also[m]:
                    also[m].append((n, ref))      # a second source's way in
    return src, parent, also

def _conv_note(nl, ref, cfg):
    """' [FB 402k/101k: Vout = 4.98 V]' / ' [ILIM 5.40/6.07/6.60 A (R128 549)]'.
    VFB and ILIM tables come from knet.json ("vref", "ilim"), keyed by a
    substring of the part value - never guessed."""
    bits, val_ = [], nl.value(ref).upper()
    for p, n in nl.cpins.get(ref, {}).items():
        pn = nl.pinname(ref, p) or ''
        if re.fullmatch(r'V?FB\d*|ADJ', pn, re.I):
            legs = divider_legs(nl, n)
            top = [l for l in legs if l[1] != 'gnd' and l[4]]
            bot = [l for l in legs if l[1] == 'gnd' and l[4]]
            if len(top) == 1 and len(bot) == 1:
                k = (top[0][4] + bot[0][4]) / bot[0][4]
                vref = next((float(v) for key, v in (cfg.get('vref') or {}).items() if key.upper() in val_), None)
                bits.append(f"FB {eng(top[0][4])}/{eng(bot[0][4])}: Vout = "
                            + (f"{vref * k:.3f} V" if vref else f"{k:.3f} x VFB (knet.json vref)"))
        tab = next((t for key, t in (cfg.get('ilim') or {}).items() if key.upper() in val_), None)
        if tab and re.search(r'IL|ISET|IMAX|ICL', pn, re.I):
            for r, kind, _f, _v, ohms in divider_legs(nl, n):
                hit = next((v for k, v in tab.items()
                            if kind == 'gnd' and ohms and abs(parse_value(k, 'R') / ohms - 1) < .005), None)
                if hit:
                    bits.append(f"ILIM {hit} ({r} {eng(ohms)})")
    return f"  [{'; '.join(bits)}]" if bits else ''

def _vf_note(nl, ref):
    """' [-Vf 350mV]' on a series diode hop: the rail below it sits a drop lower."""
    c = nl.comps[ref]
    if c['prefix'] != 'D':
        return ''
    t = f"{c.get('description', '')} {c.get('part', '')} {c['value']}"
    m = re.search(r'Vf\s*([\d.]+\s*m?V)', t, re.I)
    return f"  [-Vf {m.group(1) if m else '~0.3-0.5 V' if re.search('schottky', t, re.I) else '~0.7 V'}]"

def powertree_rows(nl, cfg):
    """the tree `powertree` prints, as data: (items, lost). items in print order:
    ('src', label, net, info) and ('hop', depth, [(ref, text)], net, info), info =
    {'v': volts, 'loads': [refs], 'also': text}; lost = power nets no source reaches.
    None when there is no source."""
    P = power_nets(nl)
    src, parent, also = power_tree(nl)
    if not src:
        return None
    kids_ = defaultdict(list)
    for m, (n, ref) in parent.items():
        kids_[n].append((m, ref))
    edge_refs = defaultdict(set)          # not loads: what feeds a net, what it feeds on
    for m, (n, ref) in parent.items():
        edge_refs[m].add(ref)
        if not any(re.match(r'^(VCC|VDD)', nl.pinname(ref, p) or '', re.I)
                   for p, x in nl.cpins[ref].items() if x == n):
            edge_refs[n].add(ref)         # ...but a part's own supply pin is a load
    label = dict(src)
    def loads(n):
        own = edge_refs[n] | {label.get(n, '').split(' ')[0]}
        return sorted({nd['ref'] for nd in nl.nets.get(n, []) if nd['ref'] not in own
                       and nl.comps[nd['ref']]['prefix'] in _LOAD_PFX
                       and not (nl.no_dnp and nl.comps[nd['ref']]['dnp'])}, key=natkey)
    keep = {}
    def kept(n):
        if n not in keep:
            keep[n] = False
            keep[n] = n in P or bool(loads(n)) or any([kept(m) for m, _ in kids_[n]])
        return keep[n]
    def info(n):
        via = parent.get(n, (None, None))[1]       # the feeding IC's other inputs go on its hop
        return {'v': P.get(n, nl.railv.get(n)), 'loads': loads(n),
                'also': '; '.join(f"{f} via {r}" for f, r in [x for x in also[n] if x[1] != via][:3])}
    told = set()
    def hop(ref, m):
        other = sorted({f for f, r in also[m] if r == ref} - {parent[m][0]}, key=natkey)
        tail = f" (also from {', '.join(other)})" if other and ref not in told else ''
        told.update([ref] if other else [])
        return ref, f"{ref} {trunc(nl.value(ref), 18)}{_conv_note(nl, ref, cfg)}{_vf_note(nl, ref)}{tail}"
    items = []
    def show(n, depth):
        for m, ref in sorted(kids_[n], key=lambda t: natkey(t[0])):
            if not kept(m):
                continue
            chain = [hop(ref, m)]
            while m not in P and not loads(m) and not also[m]:
                nxt = [k for k in kids_[m] if kept(k[0])]
                if len(nxt) != 1:
                    break
                m, r2 = nxt[0]
                chain.append(hop(r2, m))
            items.append(('hop', depth, chain, m, info(m)))
            show(m, depth + 1)
    for n, lab in src:
        items.append(('src', lab, n, info(n)))
        show(n, 0)
    mids = {n for r, c in nl.comps.items() if c['prefix'] == 'BT'
            for p, n in nl.cpins[r].items() if nl.pinname(r, p) == '-'}   # cell interconnects
    return items, sorted((n for n in P if n not in parent and n not in label and n not in mids), key=natkey)

def net_line(n, i):
    v = i['v']
    return (f"{n}{f'  {v:g} V' if v is not None else ''}"
            + (f"  loads {refrange(i['loads'])}" if i['loads'] else '')
            + (f"  (also fed from {i['also']})" if i['also'] else ''))

def c_powertree(nl, a):
    r = powertree_rows(nl, a.cfg)
    if r is None:
        print("no power source found (no BT cells, no connector pin named VBUS/SOLAR/VIN/BAT)"); return 1
    items, lost = r
    print(f"power tree from {sum(1 for it in items if it[0] == 'src')} source(s); volts are name-derived "
          f"(--rail/knet.json), Vout/ILIM from knet.json vref/ilim tables")
    for it in items:
        if it[0] == 'src':
            print(f"\n{it[1]}: {net_line(it[2], it[3])}")
        else:
            _k, depth, chain, m, i = it
            print(f"{'  ' + '   ' * depth}-> {' -> '.join(t for _r, t in chain)} -> {net_line(m, i)}")
    if lost:
        print(f"\nnot reached from any source: {', '.join(lost)}")

# ---------------- I2C buses and addresses ----------------

# 7-bit address per part (a substring of the value), from each datasheet: a fixed
# list, or a base plus strap pins LSB first (pin-name regex). knet.json "i2c" adds
# or overrides: {"PART": {"base": "0x48", "straps": ["A0", "A1"]}} or {"addr": ["0x42"]}.
I2C_PARTS = {
    'BQ25798': {'addr': [0x6B]},                                    # 7.3.14.5, fixed
    'MAX17320': {'addr': [0x36, 0x0B]},                             # 8-bit 6Ch / 16h
    'QMC6309': {'addr': [0x7C]},                                    # 5.4, one address
    'TCAL9539': {'base': 0x74, 'straps': ['A0', 'A1']},             # 1110 1 A1 A0
    'LSM6DSV16X': {'base': 0x6A, 'straps': [r'(SDO/)?SA0']},        # 110101 SA0
    'M24512': {'base': 0x50, 'straps': ['E0', 'E1', 'E2']},         # 1010 E2 E1 E0
    'TPS25751': {'tps25751': True},                                 # ADCIN1/2, tables 8-2/8-6
}
# TPS25751 Table 8-2 (ADCINx decoded value from RDOWN/(RUP+RDOWN) of LDO_3V3) and
# Table 8-6 ((ADCIN1, ADCIN2) -> I2C address index #1..#4 = 0x20..0x23, Table 8-5)
_ADCIN_MAX = (0.0228, 0.0722, 0.1425, 0.2372, 0.3671, 0.7064, 0.9060, 1.0)
_ADCIN_IDX = {(7, 5): 1, (5, 5): 2, (2, 0): 3, (1, 7): 4, (7, 3): 1, (3, 3): 2, (4, 0): 3,
              (3, 7): 4, (7, 0): 1, (0, 0): 2, (6, 0): 3, (5, 7): 4}

def _link_end(nl, net, P, seen=None):
    """Follow 0-10 R resistors and closed jumpers from net: ('GND', ref|None),
    ('rail', 'NAME via JP4'), or (None, net) when it leads nowhere fixed."""
    seen = seen or {net}
    if nl.is_gnd(net):
        return 'GND', net
    if net in P or (nl.railv.get(net) or 0) > 0:
        return 'rail', net
    for nd in nl.nets.get(net, []):
        r, c = nd['ref'], nl.comps.get(nd['ref'], {})
        link = ((c.get('prefix') == 'R' and _ohms_le(c.get('value'), 10))
                or (c.get('prefix') == 'JP' and 'open' not in (c.get('part') or '').lower()))
        if not link or (nl.no_dnp and c.get('dnp')) or len(nl.cpins.get(r, {})) != 2:
            continue
        far = nl.cpins[r].get(nl.other_pin(r, nd['pin']))
        if far and far not in seen:
            seen.add(far)
            k, n = _link_end(nl, far, P, seen)
            if k:
                return k, f"{n} via {r}"
    return None, net

def _res_legs(nl, net):
    """[(ref, ohms, far net)]: 2-pin resistors and resistor-array elements on net
    (an array element's other end: the pin named Rk.x's partner, else pin N+1-n)."""
    out = []
    for nd in nl.nets.get(net, []):
        r, c = nd['ref'], nl.comps.get(nd['ref'], {})
        if c.get('prefix') not in ('R', 'RN') or (nl.no_dnp and c.get('dnp')):
            continue
        pins = nl.cpins.get(r, {})
        if c['prefix'] == 'R' and len(pins) == 2:
            op = nl.other_pin(r, nd['pin'])
        elif c['prefix'] == 'RN':
            m = re.fullmatch(r'R(\d+)\.([12])', nl.pinname(r, nd['pin']) or '')
            op = next((p for p in pins if m and nl.pinname(r, p) == f"R{m[1]}.{3 - int(m[2])}"),
                      str(len(pins) + 1 - int(nd['pin'])) if nd['pin'].isdigit() else None)
        else:
            continue
        if op in pins:
            out.append((r, parse_value(c['value'], 'R'), pins[op]))
    return out

def strap_level(nl, net, P):
    """(level, why): 0, 1, a divider ratio (float in 0..1) or None, for an address pin."""
    k, n = _link_end(nl, net, P)
    if k:
        return (0 if k == 'GND' else 1), n
    up, dn = [], []
    for r, ohms, far in _res_legs(nl, net):
        kk, nn = _link_end(nl, far, P)
        (dn if kk == 'GND' else up if kk == 'rail' else [None]).append((r, ohms, nn))
    if up and dn and len(up) == len(dn) == 1 and up[0][1] and dn[0][1]:
        ratio = dn[0][1] / (up[0][1] + dn[0][1])
        return ratio, f"{up[0][0]} {eng(up[0][1])} to {up[0][2]} / {dn[0][0]} {eng(dn[0][1])} to GND = {ratio:.3f}"
    if dn and not up:
        return 0, f"{dn[0][0]} {eng(dn[0][1]) if dn[0][1] else ''} to GND"
    if up and not dn:
        return 1, f"{up[0][0]} {eng(up[0][1]) if up[0][1] else ''} to {up[0][2]}"
    drv = [f"{nd['ref']}.{nd['pin']}" for nd in nl.nets.get(net, [])
           if nl.comps.get(nd['ref'], {}).get('prefix') in ('U', 'IC')]
    return None, (f"driven by {' '.join(drv)}" if len(drv) > 1 else 'floating? no strap found')

def i2c_address(nl, ref, P, table):
    """([7-bit addresses], [(pin, why)]) for ref, or (None, []) if the table lacks it."""
    v = nl.value(ref).upper()
    ent = next((e for k, e in table.items() if k.upper() in v), None)
    if not ent:
        return None, []
    pins = {(nl.pinname(ref, p) or p): n for p, n in nl.cpins.get(ref, {}).items()}
    if ent.get('tps25751'):
        idx, why = [], []
        for nm in ('ADCIN1', 'ADCIN2'):
            lv, w = strap_level(nl, pins.get(nm, ''), P)
            d = (None if lv is None else 7 if lv == 1 and not isinstance(lv, float)
                 else next(i for i, mx in enumerate(_ADCIN_MAX) if lv <= mx))
            idx.append(d); why.append((nm, f"{w} -> {d}"))
        i = _ADCIN_IDX.get(tuple(idx))
        return ([0x1F + i] if i else []), why + [('index', f"#{i}" if i else f"{tuple(idx)} not in Table 8-6")]
    if 'addr' in ent:
        return [int(x, 0) if isinstance(x, str) else x for x in ent['addr']], []
    base, why, ok = int(str(ent['base']), 0), [], True
    for bit, rx in enumerate(ent.get('straps', [])):
        pin = next((p for p in pins if re.fullmatch(rx, p, re.I)), None)
        lv, w = strap_level(nl, pins[pin], P) if pin else (None, 'no such pin')
        why.append((pin or rx, w))
        if lv in (0, 1) and not isinstance(lv, float):
            base |= lv << bit
        else:
            ok = False
    return ([base] if ok else []), why

def i2c_buses(nl):
    """[(sda_nets, scl_nets)]: each SDA-named net merged with what 0-10 R links and
    closed jumpers join it to, paired with the SCL net by name or by the devices' SCL pins."""
    def group(net):
        g, todo = {net}, [net]
        while todo:
            n = todo.pop()
            for nd in nl.nets.get(n, []):
                c = nl.comps.get(nd['ref'], {})
                if ((c.get('prefix') == 'R' and _ohms_le(c.get('value'), 10)) or
                        (c.get('prefix') == 'JP' and 'open' not in (c.get('part') or '').lower())) \
                        and len(nl.cpins[nd['ref']]) == 2 and not (nl.no_dnp and c.get('dnp')):
                    far = nl.cpins[nd['ref']].get(nl.other_pin(nd['ref'], nd['pin']))
                    if far and far not in g and not nl.is_gnd(far) and far not in nl.railv:
                        g.add(far); todo.append(far)
        return g
    out, done = [], set()
    for n in sorted(nl.nets, key=natkey):
        if n in done or 'SDA' not in n.rsplit('/', 1)[-1].upper():
            continue
        sda = group(n); done |= sda
        scl_name = n.replace('SDA', 'SCL').replace('sda', 'scl')
        scl = next((nl.cpins[nd['ref']][p] for x in sda for nd in nl.nets[x]
                    for p in nl.cpins[nd['ref']] if 'SCL' in (nl.pinname(nd['ref'], p) or '').upper()
                    and nl.comps[nd['ref']]['prefix'] in ('U', 'IC')), scl_name if scl_name in nl.nets else None)
        out.append((sda, group(scl) if scl else set()))
    return out

def i2c_report(nl, cfg):
    """the buses `i2c` prints, as data: [{'name', 'scl', 'pulls', 'targets': [(ref, value,
    addrs | None, why)], 'ctrl': [(ref, value, pins)], 'conn', 'other', 'dups': [(addr, refs)]}]"""
    P = power_nets(nl)
    table = dict(I2C_PARTS)
    table.update((cfg.get('i2c') or {}))
    out = []
    for sda, scl in i2c_buses(nl):
        nodes = [(nd['ref'], nd['pin']) for x in sda for nd in nl.nets[x]]
        pulls = []
        for x in sorted(sda | scl, key=natkey):
            for r, ohms, far in _res_legs(nl, x):
                k, n = _link_end(nl, far, P)
                if k == 'rail' and not _ohms_le(nl.value(r), 10):
                    pulls.append(f"{r} {eng(ohms) if ohms else nl.value(r)} ({x.rsplit('/', 1)[-1]}) to {n}")
        bus = {'name': ' + '.join(sorted(sda, key=natkey)), 'scl': ' + '.join(sorted(scl, key=natkey)),
               'pulls': pulls, 'targets': [], 'ctrl': [], 'conn': [], 'other': [], 'dups': []}
        seen = defaultdict(list)
        for ref in sorted({r for r, _ in nodes}, key=natkey):
            c = nl.comps[ref]
            if nl.no_dnp and c['dnp']:
                continue
            pn = ' '.join(nl.pinname(ref, p) or p for r, p in nodes if r == ref)
            if c['prefix'] in ('J', 'P', 'CN', 'TP'):
                bus['conn'].append(ref); continue
            if c['prefix'] not in ('U', 'IC'):
                if c['prefix'] not in ('R', 'C', 'JP'):
                    bus['other'].append(ref)
                continue
            if re.search(r'I2Cc|MASTER|^G?P?IO\d+', pn, re.I):
                bus['ctrl'].append((ref, c['value'], pn)); continue
            addrs, why = i2c_address(nl, ref, P, table)
            for ad in addrs or []:
                seen[ad].append(ref)
            bus['targets'].append((ref, c['value'], addrs, why))
        bus['dups'] = [(ad, refs) for ad, refs in sorted(seen.items()) if len(refs) > 1]
        out.append(bus)
    return out

def c_i2c(nl, a):
    buses = i2c_report(nl, a.cfg)
    if not buses:
        print("no net named like SDA"); return 1
    dup = 0
    for bus in buses:
        print(f"\n=== {bus['name']}  /  {bus['scl'] or 'no SCL found'}")
        print(f"  pull-ups : {'; '.join(bus['pulls']) or 'NONE'}")
        for ref, value, addrs, why in bus['targets']:
            at = ('?    ' if addrs is None else '  '.join(f"0x{x:02X}" for x in addrs) if addrs else 'UNRESOLVED')
            print((f"  target   : {ref:<5} {trunc(value, 20):<20} {at:<10}"
                   + ('  ' + '; '.join(f"{p}: {w}" for p, w in why) if why else '')
                   + ("  (not in the address table: knet.json \"i2c\")" if addrs is None else '')).rstrip())
        print(f"  controller: {'; '.join(f'{r} {trunc(v, 22)} ({pn})' for r, v, pn in bus['ctrl']) or 'NONE on this bus'}")
        if bus['conn']:
            print(f"  connectors: {refrange(bus['conn'])}")
        if bus['other']:
            print(f"  also      : {refrange(bus['other'])}")
        for ad, refs in bus['dups']:
            dup += 1
            print(f"  ERROR address 0x{ad:02X} used by {' '.join(refs)}")
    return 2 if dup else 0

def c_rails(nl, a):
    rows = []
    P = power_nets(nl)
    _src, parent, _also = power_tree(nl)
    for net in sorted(P, key=lambda n: (P[n] is None, -(P[n] or 0), natkey(n))):
        cs = caps_on(nl, net)
        tot = sum(v for _, v, d in cs if v and not d)
        loads = sorted({nd['ref'] for nd in nl.nets[net]
                        if nl.comps.get(nd['ref'], {}).get('prefix') not in ('C',)}, key=natkey)
        fed = dict(_src).get(net) or (f"{parent[net][1]} [{nl.value(parent[net][1])}] from {parent[net][0]}"
                                      if net in parent else '')
        rows.append({'net': net, 'volts': P[net], 'nodes': len(nl.nets[net]),
                     'caps': [(r, v, d) for r, v, d in cs], 'total_C': tot, 'fed': fed,
                     'sources': [] if fed else sources_of(nl, net), 'loads': loads})
    if a.json:
        print(json.dumps(rows, indent=1)); return
    for r in rows:
        print(f"\n=== {r['net']}   {'?' if r['volts'] is None else r['volts']} V   {r['nodes']} nodes")
        if r['fed']:
            print(f"  source : {r['fed']}  (powertree)")
        elif r['sources']:
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

def is_thermistor(nl, ref):
    c = nl.comps.get(ref, {})
    return c.get('prefix') in ('TH', 'RT', 'NTC') or 'thermistor' in (c.get('part') or '').lower()

_BETA = re.compile(r'\bB\s*(?:25\s*/\s*(\d+))?\s*(?:=|:)?\s*(\d{4})\s*K\b')

def ntc_beta(nl, ref):
    """(B in K, 'B25/50') from the Value/Description/props of an NTC, else None.
    The first B listed wins: B25/50 is the usual datasheet headline."""
    c = nl.comps.get(ref, {})
    txt = ' '.join([c.get('value') or '', c.get('description') or '']
                   + [v for v in c.get('props', {}).values() if isinstance(v, str)])
    m = _BETA.search(txt)
    return (float(m[2]), f"B25/{m[1]}" if m[1] else 'B') if m else None

def divider_legs(nl, net, fanout=8):
    """2-pin resistors (thermistors too, at their 25 C value) touching `net` (a
    suspected divider tap), classified by what their far end reaches:
    [(ref, kind, far, volts_or_None, ohms_or_None)] with kind in
    'rail'|'gnd'|'other'. DNP-aware, matching the pass-through convention used
    everywhere else in this file."""
    out = []
    for nd in nl.nets.get(net, []):
        r = nd['ref']
        if nl.comps.get(r, {}).get('prefix') != 'R' and not is_thermistor(nl, r):
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
    tops = rails + others
    if gnds and tops and len({l[2] for l in tops}) == 1:
        # every non-GND leg reaches the same net: parallel legs combine, and a top
        # net with no known voltage still gives the tap as a fraction of it
        if not all(l[4] for l in legs):
            print("\ncould not parse every leg's value as a number"); return
        W = chr(0x2126)
        def par(ls, k=lambda l: l[4]):
            return 1 / sum(1 / k(l) for l in ls)
        rtop, rbot = '||'.join(l[0] for l in tops), '||'.join(l[0] for l in gnds)
        ohms_top, ohms_bot = par(tops), par(gnds)
        top_net, vtop = tops[0][2], tops[0][3]
        ratio = ohms_bot / (ohms_top + ohms_bot)
        fed = f"{vtop:g} V" if vtop else f"{top_net} (voltage unknown)"
        print(f"\n{'simple 2-resistor' if len(legs) == 2 else f'{len(legs)}-leg'} divider: "
              f"{rtop} (top, {eng(ohms_top, W)}, rail-side) / {rbot} (bottom, "
              f"{eng(ohms_bot, W)}, GND-side), fed from {fed}")
        if vtop:
            print(f"  V_tap nominal = {vtop:g} * {rbot}/({rtop}+{rbot}) = {vtop * ratio:.4f} V")
        else:
            print(f"  tap = {rbot}/({rtop}+{rbot}) = {ratio*100:.2f}% of {top_net}  "
                  f"(--rail '{top_net.split('/')[-1]}=V' for volts)")
        tol = {l[0]: res_tolerance(nl.comps[l[0]]) for l in legs}
        if None not in tol.values():
            lo = lambda l: l[4] * (1 - tol[l[0]])
            hi = lambda l: l[4] * (1 + tol[l[0]])
            fmax = par(gnds, hi) / (par(tops, lo) + par(gnds, hi))
            fmin = par(gnds, lo) / (par(tops, hi) + par(gnds, lo))
            what = ', '.join(f"{r} {t*100:g}%" for r, t in tol.items())
            print(f"  worst case ({what}): " + (f"{vtop*fmin:.4f} - {vtop*fmax:.4f} V" if vtop
                                               else f"{fmin*100:.2f} - {fmax*100:.2f}%"))
        else:
            print("  worst-case range not computed: tolerance missing on "
                  + ', '.join(r for r, t in tol.items() if t is None)
                  + " (add a 'Tolerance' property or 'N%' in Value)")
        ths = [l for l in legs if is_thermistor(nl, l[0])]
        if ths:
            print(f"  {', '.join(l[0] for l in ths)}: thermistor counted at its 25 C value "
                  f"({', '.join(nl.value(l[0]) for l in ths)}); the tap moves with temperature")
            betas = {l[0]: ntc_beta(nl, l[0]) for l in ths}
            if all(betas.values()):
                def at(l, t):
                    if l[0] not in betas:
                        return l[4]
                    return l[4] * math.exp(betas[l[0]][0] * (1 / (t + 273.15) - 1 / 298.15))
                temps = (-20, -10, 0, 10, 25, 45, 60)
                row = [par(gnds, lambda l: at(l, t)) / (par(tops, lambda l: at(l, t))
                                                        + par(gnds, lambda l: at(l, t))) for t in temps]
                print(f"  NTC ({', '.join(f'{r} {b[1]}={b[0]:g}K' for r, b in betas.items())}), "
                      f"tap vs cell temperature - compare with the IC's TS thresholds (kdoc):")
                print("    T (C)  " + ''.join(f"{t:>7}" for t in temps))
                print("    tap %  " + ''.join(f"{f*100:>7.1f}" for f in row))
            else:
                print("  no B value found in Value/Description (e.g. 'B25/50 3380K'), so no "
                      "temperature table")
        if vtop:
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
