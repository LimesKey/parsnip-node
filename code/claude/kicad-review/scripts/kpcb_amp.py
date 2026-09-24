"""kpcb.py `ampacity`: IPC-2221 current capacity of a routed net (copper graph + bottleneck)."""
import json, math
from collections import defaultdict
from kcommon import natkey, prefix, parse_value
from kpcb_board import _f, ipc_current, ipc_width, POUR_MIN, RHO_CU, thick_of, via_current

def _net_pads(b, net):
    """(ref, pin, x, y) for every pad on a net - the named landmarks you can
    Ctrl+F / click in KiCad to find the trace."""
    return [(f.ref, p['num'], p['x'], p['y'])
            for f in b.fps.values() for p in f.pads if p['net'] == net]

def _nearest_pad_dist(pads, pt):
    """(ref, pin, distance mm) for the pad closest to a point, or None."""
    if not pt or not pads:
        return None
    return min(((r, n, math.hypot(x - pt[0], y - pt[1])) for r, n, x, y in pads),
               key=lambda t: t[2])

def _nearest_pad(pads, pt):
    """The pad closest to a point, as 'REF.PIN (D.D mm)', or '' if none."""
    t = _nearest_pad_dist(pads, pt)
    return f"{t[0]}.{t[1]} ({t[2]:.1f} mm away)" if t else ''

def _net_graph(b, net, tol=0.05):
    """Connectivity graph of one net's routed copper, so width is read in context
    instead of segment-by-segment. Nodes = track endpoints merged within `tol` mm,
    stitched across layers where a via sits and bridged where tracks land on a
    shared pad. Edges = the segments. A BRIDGE edge is one whose removal splits the
    graph: all current between the two sides must cross it, so the narrowest bridge
    is the real series bottleneck. A segment inside a parallel loop is not a bridge,
    so two traces that split and reconverge no longer read as one thin strand.
    Endpoint-based: two traces that only cross mid-span with no shared end/via/pad
    are still separate (KiCad would merge that copper; this does not)."""
    segs = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
    if not segs:
        return None

    def q(p):
        return (round(p[0] / tol), round(p[1] / tol))
    parent = {}

    def find(k):
        parent.setdefault(k, k)
        root = k
        while parent[root] != root:
            root = parent[root]
        while parent[k] != root:
            parent[k], k = root, parent[k]
        return root

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    ekeys = []
    for t in segs:
        ka, kb = (t['layer'],) + q(t['a']), (t['layer'],) + q(t['b'])
        find(ka); find(kb)
        ekeys.append((ka, kb))
    bygrid = defaultdict(list)
    for k in list(parent):
        bygrid[k[1:]].append(k)

    def near(gx, gy, r=1):
        return [k for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                for k in bygrid.get((gx + dx, gy + dy), [])]
    for v in b.vias:                                  # a via stitches its layers
        if v['net'] == net:
            ks = near(*q((v['x'], v['y'])))
            for k in ks[1:]:
                union(ks[0], k)
    pad_key = {}                                       # ref -> a grid key at one of its pads
    for f in b.fps.values():                          # a pad bridges tracks on it
        for p in f.pads:
            if p['net'] != net:
                continue
            th = p['drill'] > 0 or any('*.Cu' in l for l in p['layers'])
            ks = near(*q((p['x'], p['y'])), r=max(1, int(p['w'] / 2 / tol)))
            if not th:
                ks = [k for k in ks if k[0] in p['layers']]
            if ks:
                pad_key[f.ref] = ks[0]
            for k in ks[1:]:
                union(ks[0], k)

    node = {}

    def nid(k):
        return node.setdefault(find(k), len(node))
    adj, enode = defaultdict(list), []
    for i, (ka, kb) in enumerate(ekeys):
        u, w = nid(ka), nid(kb)
        enode.append((u, w))
        adj[u].append((w, i)); adj[w].append((u, i))
    n = len(node)
    cid, comp = {}, 0                                 # label components over the edge graph
    for s in range(n):
        if s in cid:
            continue
        cid[s] = comp; stack = [s]
        while stack:
            u = stack.pop()
            for w, _ in adj[u]:
                if w not in cid:
                    cid[w] = comp; stack.append(w)
        comp += 1
    # the main current-carrying copper is the component with the most track length;
    # a stray fragment or a one-pad spur is its own (tiny) component and its lone
    # edge would otherwise count as a false bridge.
    clen = defaultdict(float)
    for i, (u, _w) in enumerate(enode):
        clen[cid[u]] += segs[i]['len']
    main = max(clen, key=clen.get) if clen else 0
    # bridges: iterative DFS low-link, skipping only the edge we entered on (so a
    # second parallel edge between the same nodes correctly prevents a bridge).
    bridges, disc, low, timer = set(), {}, {}, [0]
    for s in range(n):
        if s in disc:
            continue
        disc[s] = low[s] = timer[0]; timer[0] += 1
        stack = [(s, -1, iter(adj[s]))]
        while stack:
            u, pe, it = stack[-1]
            for (w, ei) in it:
                if ei == pe:
                    continue
                if w not in disc:
                    disc[w] = low[w] = timer[0]; timer[0] += 1
                    stack.append((w, ei, iter(adj[w])))
                    break
                low[u] = min(low[u], disc[w])
            else:
                stack.pop()
                if stack:
                    pu = stack[-1][0]
                    low[pu] = min(low[pu], low[u])
                    if low[u] > disc[pu]:
                        bridges.add(pe)
    main_bridges = {i for i in bridges if cid[enode[i][0]] == main}
    pad_node = {ref: nid(find(k)) for ref, k in pad_key.items()}
    return {'segs': segs, 'nodes': n, 'comp': comp, 'bridges': main_bridges,
            'adj': adj, 'enode': enode, 'cid': cid, 'pad_node': pad_node}

def _net_geo(b, net):
    """One net's routed copper in a single pass: {layer: [minw, len]}, total
    length, list of via drills, and series-sum resistance (ohm, pessimistic)."""
    layers, total, r = {}, 0.0, 0.0
    for t in b.tracks:
        if t['net'] != net or t['w'] <= 0:
            continue
        total += t['len']
        r += RHO_CU * t['len'] / (t['w'] * thick_of(b, t['layer']))
        L = layers.setdefault(t['layer'], [math.inf, 0.0, (0.0, 0.0), 0.0])
        L[1] += t['len']
        if t['w'] < L[0]:                        # remember the narrowest seg + where it is
            L[0], L[2], L[3] = t['w'], t['mid'], t['len']
    vd = [v['drill'] for v in b.vias if v['net'] == net and v['drill'] > 0]
    return layers, total, vd, r

def _seg_amp(b, t, dt):
    return ipc_current(t['w'], thick_of(b, t['layer']), t['layer'] in b.outer, dt)

def _is_tap_ref(b, ref):
    """A part that can only be a current SENSE tap, never a series power path:
    a thermistor/test point outright, or a resistor whose value is too high to
    be a power-path element (a shunt/current-sense R is <<1 ohm; a divider/pull
    tap is typically >=1k)."""
    p = prefix(ref)
    if p in ('TH', 'TP'):
        return True
    if p == 'R':
        fp = b.fps.get(ref)
        val = parse_value(fp.value, 'R') if fp else None
        return val is not None and val >= 1000.0
    return False

def _bridge_is_tap(b, g, i):
    """True if bridge edge `i` isolates a pendant sub-branch, on EITHER side,
    whose sole pads belong to sense-tap parts (see `_is_tap_ref`). Such a
    bridge is a false series bottleneck: the net's full budgeted current has
    no reason to detour down a thermistor or pull-up leg, so it should not be
    picked as the mandatory bridge for a TRACE-THIN verdict. Checking both
    sides (not just the smaller one) sidesteps a tie when a 2-node net splits
    1-vs-1."""
    u, w = g['enode'][i]
    seen = {u}
    stack = [u]
    while stack:
        x = stack.pop()
        for y, ei in g['adj'][x]:
            if ei == i or y in seen:
                continue
            seen.add(y)
            stack.append(y)
    comp_nodes = {n for n, c in g['cid'].items() if c == g['cid'][u]}
    other = comp_nodes - seen
    for side in (seen, other):
        refs = {ref for ref, nd in g['pad_node'].items() if nd in side}
        if refs and all(_is_tap_ref(b, ref) for ref in refs):
            return True
    return False

def _bott_fields(b, t, dt):
    """Bottleneck fields from a single track dict (the constraining segment)."""
    return {'i': _seg_amp(b, t, dt), 'ly': t['layer'], 'w': t['w'],
            'mid': t['mid'], 'seg': t['len'], 'ext': t['layer'] in b.outer}

def _amp_row(b, net, need, a, graph=False):
    layers, total, vd, r = _net_geo(b, net)
    segs = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
    if not layers and not vd:
        return None
    rows = []                                    # per-layer table, informational
    for ly, (minw, ln, mid, seglen) in sorted(layers.items()):
        ext = ly in b.outer
        rows.append((ly, minw, ln, ipc_current(minw, thick_of(b, ly), ext, a.dt),
                     ext, mid, seglen))
    # narrowest single segment anywhere (the old, geometry-blind number)
    naive = min(segs, key=lambda t: _seg_amp(b, t, a.dt)) if segs else None
    # narrowest MANDATORY segment: a bridge in the connectivity graph, i.e. one all
    # the current must cross. A segment in a parallel loop is skipped, so split-and-
    # reconverge no longer reads as one thin strand. Falls back to naive if no graph.
    meshed, comp, bott, bott_is_tap = False, None, naive, False
    if graph and segs:
        g = _net_graph(b, net)
        comp = g['comp'] if g else None
        bridge_idx = list(g['bridges']) if g else []
        # a bridge that only isolates a thermistor/test-point/pull-R leg is a
        # sense tap, not a series power path - the net's budgeted current has
        # no reason to run down it, so it's excluded before picking the
        # narrowest MANDATORY bottleneck (see _bridge_is_tap).
        real_idx = [i for i in bridge_idx if not _bridge_is_tap(b, g, i)]
        if real_idx:
            bott = min((g['segs'][i] for i in real_idx), key=lambda t: _seg_amp(b, t, a.dt))
        elif bridge_idx:
            bott = min((g['segs'][i] for i in bridge_idx), key=lambda t: _seg_amp(b, t, a.dt))
            bott_is_tap = True                  # every bridge left is a sense tap
        elif g:
            meshed = True                        # a full mesh: no single mandatory seg
    per_via = [via_current(d, a.plating / 1000.0, a.dt) for d in vd]
    farea = defaultdict(list)                   # real filled copper per layer (mm2)
    for fl in b.fills:
        if fl['net'] == net:
            farea[fl['layer']].append(fl['area'])
    poured = sorted(ly for ly, ar in farea.items() if sum(ar) > POUR_MIN)
    pour = {ly: (sum(farea[ly]), len(farea[ly]), max(farea[ly])) for ly in poured}
    bf = _bott_fields(b, bott, a.dt) if bott else {'i': 0.0, 'ly': '-', 'w': 0.0,
                                                   'mid': (0.0, 0.0), 'seg': 0.0, 'ext': False}
    nf = _bott_fields(b, naive, a.dt) if naive else bf
    pads = _net_pads(b, net)
    near = _nearest_pad_dist(pads, bf['mid'])
    # a short, wide stub landing right on a pad is a pad neck: IPC-2221's
    # long-trace steady-state formula overstates its thermal risk because the
    # pad copper (and, for a fine-pitch part, the part's own die/thermal pad)
    # sinks heat that a real long trace of this width could not.
    pad_neck = bool(bott) and bool(near) and 0 < bf['seg'] < 2 * bf['w'] and near[2] <= 1.0
    return {'net': net, 'need': need, 'layers': rows, 'total': total,
            'bott_i': bf['i'], 'bott_ly': bf['ly'], 'bott_mid': bf['mid'], 'bott_seg': bf['seg'],
            'bott_w': bf['w'], 'bott_is_tap': bott_is_tap, 'pad_neck': pad_neck,
            'naive_i': nf['i'], 'naive_ly': nf['ly'], 'naive_w': nf['w'], 'naive_mid': nf['mid'],
            'meshed': meshed, 'comp': comp, 'graphed': graph and bool(segs),
            'ends': sorted({r for r, _n, _x, _y in pads},
                           key=lambda r: (prefix(r) in ('C', 'R', 'TP', 'TH', 'FB'), natkey(r))),
            'bott_near': _nearest_pad(pads, bf['mid']),
            'need_w': ipc_width(need or 0, thick_of(b, bf['ly']), bf['ext'], a.dt) if bf['ly'] != '-' else 0.0,
            'bott_ext': bf['ext'],
            'need_w_outer': ipc_width(need or 0, thick_of(b, b.copper[0]), True, a.dt)
                            if b.copper and not bf['ext'] else 0.0,
            'vias': len(vd), 'via_bound': sum(per_via),
            'via_min': min(per_via) if per_via else 0.0, 'r': r, 'poured': poured,
            'pour': pour}

def _amp_verdicts(row, a):
    need = row['need']
    if need is None:
        return []
    if row['poured']:                    # a plane net: the pour carries it, not these stubs
        # area > POUR_MIN says a pour EXISTS, not that it is wide enough: a
        # fragmented fill or one thin neck can still be the real limiter
        shape = '; '.join(f"{ly} {ar:.0f} mm2 in {n} fragment(s), largest {100*mx/ar:.0f}%"
                          for ly, (ar, n, mx) in sorted(row['pour'].items()))
        return [('POURED', f"a pour carries it ({shape}). UNVERIFIED, not a pass: the "
                          f"pour's narrowest neck is not measured - check the path between "
                          f"the end pads in KiCad, and `zones` for fill state")]
    out = []
    if row['meshed']:                    # a full mesh: no single segment is mandatory
        if row['naive_i'] < need:
            out.append(('MESH-CHECK', f"no series bottleneck (fully meshed); narrowest single "
                                     f"seg is {row['naive_i']:.2f} A but current splits - "
                                     f"confirm the parallel copper sums >= {need:.2f} A"))
        return out
    if row['bott_i'] < need:
        mx, my = row['bott_mid']
        near = row['bott_near'] or f'{mx:.1f},{my:.1f}'
        if row['pad_neck']:
            out.append(('PAD-NECK', f"narrowest copper ({row['bott_seg']:.2f} mm long, "
                                    f"{row['bott_w']:.2f} mm wide) is a stub landing right on "
                                    f"{near} - IPC-2221's long-trace formula ({row['bott_i']:.2f} A) "
                                    f"overstates the risk here since the pad sinks heat locally. "
                                    f"Not a real TRACE-THIN unless the copper stays this narrow "
                                    f"past the pad."))
        elif row['bott_is_tap']:
            out.append(('MIXED-NET', f"every series bottleneck left after excluding thermistor/"
                                     f"test-point/pull-R taps is itself one, narrowest "
                                     f"{row['bott_i']:.2f} A near {near} - this net mixes a power "
                                     f"path with sense taps; the {need:.2f} A budget likely runs "
                                     f"through different copper than this tap. Verify visually."))
        else:
            msg = (f"bottleneck {row['bott_i']:.2f} A < {need:.2f} A on "
                   f"{row['bott_ly']}; widen to >= {row['need_w']:.2f} mm. "
                   f"Narrowest bridge {row['bott_seg']:.1f} mm seg near {near} "
                   f"(cursor to {mx:.1f},{my:.1f})")
            if not row['bott_ext'] and row['need_w'] > 2.0:
                msg += (f". {row['need_w']:.2f} mm on an inner layer is impractical - "
                        f"move this bridge to F.Cu/B.Cu instead (needs only "
                        f">= {row['need_w_outer']:.2f} mm there)")
            out.append(('TRACE-THIN', msg))
    # a thinner segment exists but is paralleled (not on the mandatory path)
    if row['naive_i'] < need and row['naive_i'] < row['bott_i'] - 1e-6:
        nx, ny = row['naive_mid']
        out.append(('PARALLEL-CHECK', f"a thinner {row['naive_w']:.2f} mm seg ({row['naive_i']:.2f} A) "
                                     f"at {nx:.1f},{ny:.1f} is paralleled, not mandatory - OK only "
                                     f"if its parallel group sums >= {need:.2f} A"))
    if row['vias'] and row['via_bound'] < need:
        want = math.ceil(need / row['via_min']) if row['via_min'] > 0 else 0
        out.append(('VIA-FEW', f"{row['vias']} via(s) ~{row['via_bound']:.2f} A parallel "
                               f"< {need:.2f} A; want ~{want} of this size"))
    if a.vdrop and need * row['r'] > a.vdrop:
        out.append(('LONG-DROP', f"Vdrop <= {need * row['r'] * 1000:.0f} mV @ {need:.2f} A "
                                 f"over {row['total']:.0f} mm (series upper bound)"))
    return out

def _print_amp(row, a):
    need, v = row['need'], _amp_verdicts(row, a)
    head = f"{row['net']}: "
    if need is not None:
        head += f"need {need:.2f} A   "
    if row['meshed']:
        kind = 'meshed, no series bottleneck'
    elif row['poured']:
        kind = (f"POURED - thinnest TRACK {row['bott_i']:.2f} A on {row['bott_ly']} is not "
                f"the net's capacity")
    else:
        tag = ' (narrowest bridge)' if row['graphed'] else ''
        near = f" near {row['bott_near']}" if row['bott_near'] else ''
        kind = f"bottleneck {row['bott_i']:.2f} A on {row['bott_ly']}{tag}{near}"
    head += f"routed {row['total']:.1f} mm   {kind}"
    if need is not None and not v:
        head += "   OK"
    print(head)
    if row['graphed'] and row['comp'] is not None:
        note = f" - copper is in {row['comp']} island(s); only the pour/pads join them" \
            if row['comp'] > 1 else ''
        naive_note = f"; narrowest single seg {row['naive_i']:.2f} A (paralleled)" \
            if row['naive_i'] < row['bott_i'] - 1e-6 else ''
        print(f"    graph: {len(row['ends'])} pad(s){note}{naive_note}")
    if row['ends']:
        landmarks = ' '.join(row['ends'][:10]) + (' ...' if len(row['ends']) > 10 else '')
        print(f"    find it: click any of these in KiCad to highlight the net -> {landmarks}")
    for ly, minw, ln, i, ext, mid, seg in row['layers']:
        mark = ('' if row['poured'] else '  <- bottleneck layer') if ly == row['bott_ly'] else ''
        print(f"    {ly:<8} len {ln:6.1f}  minw {minw:.3f}  ->  {i:5.2f} A  "
              f"({'external' if ext else 'internal'}){mark}")
    if row['vias']:
        print(f"    {'vias':<8} {row['vias']:>3} x        ->  {row['via_bound']:5.2f} A "
              f"parallel bound ({row['via_min']:.2f} A each)")
    if need is not None:
        print(f"    R<={row['r'] * 1000:.1f} mohm  Vdrop<={need * row['r'] * 1000:.0f} mV  "
              f"P<={need * need * row['r'] * 1000:.0f} mW  (series upper bound"
              + (", tracks only - the pour is ignored)" if row['poured'] else ")"))
    for tag, msg in v:
        print(f"    !! {tag}: {msg}")
    return v

def _amp_json(row):
    if not row:
        return None
    return {'net': row['net'], 'need_A': row['need'], 'routed_mm': round(row['total'], 2),
            'bottleneck_A': round(row['bott_i'], 3), 'bottleneck_layer': row['bott_ly'],
            'need_width_mm': round(row['need_w'], 3) if row['need'] else None,
            'bottleneck_at': [round(v, 2) for v in row['bott_mid']],
            'bottleneck_near': row['bott_near'], 'on_refs': row['ends'],
            'bottleneck_is_bridge': row['graphed'] and not row['meshed'],
            'bottleneck_is_pad_neck': row['pad_neck'], 'bottleneck_is_tap': row['bott_is_tap'],
            'narrowest_single_A': round(row['naive_i'], 3), 'meshed': row['meshed'],
            'components': row['comp'],
            'layers': [{'layer': l, 'minw_mm': w, 'len_mm': round(ln, 2), 'amp_A': round(i, 3),
                        'external': e} for l, w, ln, i, e, _m, _s in row['layers']],
            'vias': row['vias'], 'via_bound_A': round(row['via_bound'], 3),
            'r_mohm': round(row['r'] * 1000, 2),
            # when present, bottleneck_A is track-only and NOT the net's capacity
            'pour': {ly: {'area_mm2': round(ar, 1), 'fragments': n, 'largest_mm2': round(mx, 1)}
                     for ly, (ar, n, mx) in row['pour'].items()}}

def _amp_footer():
    print("\nIPC-2221: I = k*dT^0.44*A^0.725 (k=0.048 outer, 0.024 inner). Outer-layer\n"
          "numbers match the published charts and are trustworthy; the INNER-layer 0.024 k\n"
          "is very conservative - with adjacent GND planes (this board pours GND on all 4)\n"
          "real inner ampacity (IPC-2152) runs ~2-3x higher, so an internal TRACE-THIN\n"
          "overstates how thin it is. Thickness is read from the stackup, so fix the foil\n"
          "weight there if it is wrong.\n"
          "Bottleneck is the narrowest BRIDGE in the copper graph (endpoints merged, vias\n"
          "and shared pads stitched): a segment all the current must cross. A segment inside\n"
          "a parallel loop is skipped, so a split-and-reconverge no longer reads as one thin\n"
          "strand. Remaining blind spots: two traces that only cross mid-span with no shared\n"
          "end/via/pad are still separate copper here (KiCad would merge them); a parallel\n"
          "group whose widths individually pass but SUM short is flagged PARALLEL-CHECK for\n"
          "you to add up. Via bound is optimistic (all vias parallel). R/Vdrop/P are a\n"
          "SERIES UPPER BOUND, so a small bound is definitely fine and a large one just\n"
          "means trace the real source-to-load path by hand.")

def c_ampacity(b, a):
    """Current-carrying check on the ROUTED copper (not the schematic).

    Per net: IPC-2221 ampacity of the narrowest segment on each layer (the
    series bottleneck), the parallel current bound of its vias, and a
    series-upper-bound resistance / voltage drop for the length. Give a
    required current with `--amps X` on named nets, or a kpcb.json
    "current":{"NET":amps} budget, and it warns TRACE-THIN / VIA-FEW /
    LONG-DROP. With no budget it just reports capacity."""
    if not b.tracks:
        print(f"{b.path}: no routed tracks yet (nothing routed, or a pre-route board)")
        return 0
    jbud = {}
    for k, vv in (getattr(a, 'current', None) or {}).items():
        jbud[k] = _f(vv)
    named, fail = list(a.args) + list(a.net or []), 0

    if named:
        rows = []
        for net in named:
            need = a.amps if a.amps is not None else jbud.get(net)
            row = _amp_row(b, net, need, a, graph=True)
            if row is None:
                print(f"{net}: no routed copper on this net")
                continue
            rows.append(row)
        if a.json:
            print(json.dumps([_amp_json(r) for r in rows], indent=1))
            return 0
        print(f"{b.path}: trace ampacity  (IPC-2221, dT={a.dt:g} C, via plating {a.plating:g} um)\n")
        for row in rows:
            fail += any(t in ('TRACE-THIN', 'VIA-FEW') for t, _ in _print_amp(row, a))
            print()
        _amp_footer()
        return 2 if fail else 0

    # scan mode: verdict any budgeted net, then list the heaviest routed nets
    allnets = sorted({t['net'] for t in b.tracks if t['net']})
    budg = [n for n in allnets if n in jbud]
    if a.json:
        src = budg or allnets
        print(json.dumps([o for o in (_amp_json(_amp_row(b, n, jbud.get(n), a, graph=True))
                                      for n in src) if o], indent=1))
        return 0
    print(f"{b.path}: trace ampacity scan  (IPC-2221, dT={a.dt:g} C)\n")
    if budg:
        print("Budgeted nets (kpcb.json current{}):")
        for n in budg:
            fail += any(t in ('TRACE-THIN', 'VIA-FEW')
                        for t, _ in _print_amp(_amp_row(b, n, jbud[n], a, graph=True), a))
            print()
    geos = sorted((r for r in (_amp_row(b, n, None, a) for n in allnets) if r),
                  key=lambda r: -r['total'])
    print(f"Heaviest routed nets (capacity only, top {a.max}):")
    print(f"  {'net':<18} {'routed':>7}  {'bott':>6}  {'layer(minw)':<15} vias")
    for row in geos[:a.max]:
        minw = next((m for l, m, *_ in row['layers'] if l == row['bott_ly']), 0.0)
        tag = '  poured (plane carries it)' if row['poured'] else ''
        print(f"  {row['net']:<18} {row['total']:6.1f}  {row['bott_i']:5.2f} A  "
              f"{row['bott_ly'] + f'({minw:.2f})':<15} {row['vias']}{tag}")
    if not budg:
        print('\nNo current budget set -> capacity only, no warnings. Add per-net amps to\n'
              'kpcb.json "current":{"VSYS":2.7}, or run `ampacity VSYS --amps 2.7`, to get\n'
              'too-thin / too-few-vias / voltage-drop warnings.')
    _amp_footer()
    return 2 if fail else 0
