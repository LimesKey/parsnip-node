"""kpcb.py `congest`, `fanout` and `reroute`: routing a dense board.

`congest` estimates routing demand against capacity per cell, before or between routing
passes: demand is RUDY (each link's wire spread evenly over its bounding box, the classic
placement estimator), capacity is the free copper per layer over the class pitch. The
default demand is what is still unrouted (`airwires`' links over all copper on the board);
`--all` is the placement view: every net's pad-to-pad MST over pads and pours alone (no
tracks or vias), so it shows where the floorplan forces density before any track exists.
A plane (GND, or > 20% of a layer) counts as half a lane - a route may cut it - and any
other pour as copper, as `route` sees them.

`fanout REF` is the escape plan for a fine-pitch part: per netted pad, whether a track of
its class width leaves the courtyard on the pad's own layer (and which side), else the
nearest legal via spot it reaches (a dogbone), else what it needs (via-in-pad, a neck).
Each net is judged alone against today's copper, so two pins may claim the same channel.

`reroute [NET...]` compares routed signal nets with the router's own best: a net whose
copper is much longer than its pad MST (or carries extra vias) is ripped and routed again
on a copy, and the new route is kept only when it is complete and better (length + via
cost). Without NETs it takes `tracks`' detour rows. -o writes the improved board."""
import math
from collections import defaultdict
from kcommon import GND_RE, unesc_disp
from kpcb_board import PolyIndex, grow, inside, pad_box

SQ2 = math.sqrt(2)


def octd(p, q):
    """octilinear distance: the shortest 0/45/90 track between two points"""
    dx, dy = abs(p[0] - q[0]), abs(p[1] - q[1])
    return max(dx, dy) + (SQ2 - 1) * min(dx, dy)


def mst(pts):
    """Prim over octilinear distance: [(i, j, d)] edges"""
    if len(pts) < 2:
        return []
    best = {i: (octd(pts[0], pts[i]), 0) for i in range(1, len(pts))}
    out = []
    while best:
        i = min(best, key=lambda k: best[k][0])
        d, j = best.pop(i)
        out.append((j, i, d))
        for k in best:
            dk = octd(pts[i], pts[k])
            if dk < best[k][0]:
                best[k] = (dk, i)
    return out


def cu_raster(b, ly, res, tracks=True, pours=True, islands_hard=False):
    """(W, H, hard, soft) over the outline box at `res`: hard = pad/track/via copper on
    layer ly (tracks/vias only with `tracks`), soft = pour fill (only with `pours`);
    islands_hard: a pour that is not a plane is copper (the router's rule), not soft"""
    from kpcb_route import _iv, _pad_shape, _planes
    from kpcb_move import cu_layers, via_layers
    o = b.outline
    W, H = int((o[2] - o[0]) / res) + 1, int((o[3] - o[1]) / res) + 1
    hard, soft = bytearray(W * H), bytearray(W * H)

    def paint(ras, sh, R=0.0):
        y0, y1, f = _iv(sh, R)
        for j in range(max(0, math.ceil((y0 - o[1]) / res)), min(H - 1, math.floor((y1 - o[1]) / res)) + 1):
            iv = f(o[1] + j * res)
            if iv:
                i0, i1 = max(0, math.ceil((iv[0] - o[0]) / res)), min(W - 1, math.floor((iv[1] - o[0]) / res))
                if i0 <= i1:
                    ras[j * W + i0:j * W + i1 + 1] = b'\x01' * (i1 - i0 + 1)
    for f in b.placed():
        for p in f.pads:
            if p['kind'] != 'np_thru_hole' and (p['drill'] or ly in cu_layers(p['layers'], b.copper)):
                paint(hard, _pad_shape(p, pad_box(p)))
    for t in b.tracks if tracks else ():
        if t['layer'] == ly:
            paint(hard, ('cap', t['a'], t['b'], t['w'] / 2))
    for v in b.vias if tracks else ():
        if ly in via_layers(v, b.copper):
            paint(hard, ('disk', v['x'], v['y'], v['size'] / 2))
    planes = _planes(b) if islands_hard else ()
    for fl in b.fills if pours else ():
        if fl['layer'] == ly:
            ras = hard if islands_hard and (fl['net'], ly) not in planes else soft
            idx = PolyIndex(fl['pts'])
            for j in range(H):
                y = o[1] + j * res
                xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1) for x1, y1, x2, y2 in
                            idx.rows.get(math.floor(y / idx.cell), ()) if (y1 > y) != (y2 > y))
                for a0, a1 in zip(xs[::2], xs[1::2]):
                    i0, i1 = max(0, math.ceil((a0 - o[0]) / res)), min(W - 1, math.floor((a1 - o[0]) / res))
                    if i0 <= i1:
                        ras[j * W + i0:j * W + i1 + 1] = b'\x01' * (i1 - i0 + 1)
    return W, H, hard, soft


# ---------------------------------------------------------------- congest

def _pitch(b, net):
    """track width + clearance of the net's class (0.2 + 0.2 when no project)"""
    from kpcb_route import _netclass
    c = _netclass(b, net)
    return float(c.get('track_width', 0.2)) + float(c.get('clearance', 0.2))


def _demand_links(b, all_nets):
    """[(net, p, q)]: the unrouted links (airwires), or with all_nets every signal net's
    pad MST - not GND, not a net with a pour (it reaches its pads through the plane)"""
    from kpcb_route import links
    poured = {fl['net'] for fl in b.fills}
    pads = defaultdict(list)
    for f in b.placed():
        for p in f.pads:
            if p['net'] and not p['net'].startswith('unconnected-'):
                pads[p['net']].append((p['x'], p['y']))
    out = []
    for net, pts in pads.items():
        if len(pts) < 2:
            continue
        if all_nets:
            if GND_RE.search(net) or net in poured:
                continue
            out += [(net, pts[i], pts[j]) for i, j, _d in mst(pts)]
        else:
            out += [(net, pa[:2], pb[:2]) for _d, pa, pb in links(b, net)[1]]
    return out


def c_congest(b, a):
    o = b.outline
    if not o:
        print("congest: no board outline"); return 1
    cell = a.cell or 2.0
    layers = [l for l in (a.layers.split(',') if a.layers else b.copper) if l in b.copper]
    res, p0 = 0.2, _pitch(b, '')
    L = _demand_links(b, a.all)
    W, H = int((o[2] - o[0]) / res) + 1, int((o[3] - o[1]) / res) + 1
    C, R = int(math.ceil((o[2] - o[0]) / cell)), int(math.ceil((o[3] - o[1]) / cell))
    free = {}                                    # per layer, per cell: fraction a track may use
    for ly in layers:
        _W, _H, hard, soft = cu_raster(b, ly, res, tracks=not a.all, islands_hard=True)   # pours are power design
        fr = [0.0] * (C * R)
        for gy in range(R):
            j0, j1 = int(gy * cell / res), min(H, int((gy + 1) * cell / res))
            for gx in range(C):
                i0, i1 = int(gx * cell / res), min(W, int((gx + 1) * cell / res))
                n = max(1, (i1 - i0) * (j1 - j0))
                hc = sum(sum(hard[j * W + i0:j * W + i1]) for j in range(j0, j1)) / n
                sc = sum(sum(soft[j * W + i0:j * W + i1]) for j in range(j0, j1)) / n
                fr[gy * C + gx] = max(0.0, 1 - hc - 0.5 * max(0.0, sc - hc))   # cutting a plane: half a lane
        free[ly] = fr
    cap = [sum(free[ly][k] for ly in layers) * cell / p0 for k in range(C * R)]
    from kpcb_route import rf_ref_layers               # where to send long routes: route's own layer costs
    pref = {ly: 1.0 if ly in b.outer else 0.4 if ly in rf_ref_layers(b) else 0.8 for ly in layers}
    dem, through = [0.0] * (C * R), defaultdict(set)
    for net, p, q in L:                          # RUDY: a link's wire spread evenly over its box
        x0, x1 = sorted((p[0], q[0]))
        y0, y1 = sorted((p[1], q[1]))
        x0, x1, y0, y1 = x0 - p0 / 2, x1 + p0 / 2, y0 - p0 / 2, y1 + p0 / 2
        wt = _pitch(b, net) / p0                 # a power track takes more than one lane
        dens = wt * ((x1 - x0) + (y1 - y0)) / ((x1 - x0) * (y1 - y0))     # tracks per mm of cut, per mm2
        for gy in range(max(0, int((y0 - o[1]) / cell)), min(R, int((y1 - o[1]) / cell) + 1)):
            for gx in range(max(0, int((x0 - o[0]) / cell)), min(C, int((x1 - o[0]) / cell) + 1)):
                cx0, cy0 = o[0] + gx * cell, o[1] + gy * cell
                ov = max(0.0, min(x1, cx0 + cell) - max(x0, cx0)) * max(0.0, min(y1, cy0 + cell) - max(y0, cy0))
                if ov > 0:
                    dem[gy * C + gx] += dens * ov / cell
                    through[gy * C + gx].add(net)
    ins = [not b.rings or inside((o[0] + (k % C + .5) * cell, o[1] + (k // C + .5) * cell), b.rings) for k in range(C * R)]
    util = [dem[k] / cap[k] if cap[k] > 1e-6 else (math.inf if dem[k] > 0.05 else 0.0) for k in range(C * R)]
    pct = lambda u: 'no free copper' if u == math.inf else f"{u:.0%}"      # noqa: E731
    what = "every signal net's pad MST over pads and pours, no tracks (placement view)" if a.all else \
        f"{len(L)} unrouted link(s) over the copper on the board"
    print(f"congest: {what}; {', '.join(layers)}; cell {cell:g} mm, pitch {p0:g} mm "
          f"(Default width + clearance); demand RUDY, capacity free copper / pitch")
    if not L:
        print("  nothing to route" + ("" if a.all else " - `congest --all` shows where the placement forces density"))
        return 0
    step = max(1, math.ceil(C / 60))             # keep the map under ~60 columns
    print("      +" + "-" * math.ceil(C / step) + "+")
    for gy in range(R):
        line = ''
        for gx in range(0, C, step):
            ks = [gy * C + x for x in range(gx, min(C, gx + step))]
            if not any(ins[k] for k in ks):
                line += ' '
                continue
            u = max(util[k] for k in ks if ins[k])
            line += '#' if u >= 1 else '*' if u >= .75 else '+' if u >= .5 else ':' if u >= .25 else '.' if u > .02 else ' '
        print(f"{o[1] + gy * cell:>6.0f}|{line}|")
    print("      +" + "-" * math.ceil(C / step) + "+")
    print(f"key: # demand >= capacity, * >= 75%, + >= 50%, : >= 25%, . some  (1 column = {cell * step:g} mm)")
    hot = [k for k in range(C * R) if ins[k] and util[k] >= .5]     # past half, detours and vias eat the rest
    hotset = set(hot)
    seen, regions = set(), []
    for k in sorted(hot, key=lambda k: -util[k]):        # 8-connected regions of hot cells
        if k in seen:
            continue
        reg, st = [], [k]
        seen.add(k)
        while st:
            c = st.pop()
            reg.append(c)
            for dx in (-1, 0, 1):
                for dy in (-1, 0, 1):
                    nx, ny = c % C + dx, c // C + dy
                    nk = ny * C + nx
                    if 0 <= nx < C and 0 <= ny < R and nk not in seen and nk in hotset:
                        seen.add(nk)
                        st.append(nk)
        regions.append(reg)
    if not regions:
        print(f"\nno cell over 50% (peak {pct(max(util[k] for k in range(C * R) if ins[k]))}): the routing fits "
              f"the free copper (RUDY is optimistic about detours; `route` is the proof)")
        return 0
    print(f"\n{len(regions)} region(s) over 50%, worst first:")
    for reg in regions[:a.max]:
        bx = (o[0] + min(k % C for k in reg) * cell, o[1] + min(k // C for k in reg) * cell,
              o[0] + (max(k % C for k in reg) + 1) * cell, o[1] + (max(k // C for k in reg) + 1) * cell)
        d, c = sum(dem[k] for k in reg), sum(cap[k] for k in reg)
        nets = defaultdict(float)
        for k in reg:
            for n in through[k]:
                nets[n] += 1
        fl = {ly: sum(free[ly][k] for k in reg) / len(reg) for ly in layers}
        roomy = max(fl, key=lambda ly: fl[ly] * pref[ly])
        print(f"  {bx[0]:.0f},{bx[1]:.0f} .. {bx[2]:.0f},{bx[3]:.0f}  peak {pct(max(util[k] for k in reg))}  "
              f"demand {d:.1f} vs capacity {c:.1f} track(s)  free: "
              + ' '.join(f"{ly} {v:.0%}" for ly, v in fl.items()))
        top = sorted(nets, key=lambda n: -nets[n])
        print(f"    nets: {', '.join(unesc_disp(n.split('/')[-1]) for n in top[:8])}"
              + (f" +{len(top) - 8}" if len(top) > 8 else ''))
        if d <= c * 1.0:
            hint = f"most room on {roomy}: send the region's long links there (`route NET --layers {roomy}`)"
        else:
            hint = ("over capacity on every layer: spread the parts (`tidy`, `movecheck`), rotate an IC so its "
                    "pins face their loads, or fan out through vias (`fanout REF`)")
        print(f"    {hint}")
    if len(regions) > a.max:
        print(f"  ... +{len(regions) - a.max} more - raise --max")
    print("\nRUDY spreads each link over its bounding box: a long diagonal link reads thin and wide. "
          "`airwires` names the links, `map --layers L` shows one layer's copper.")
    return 0


# ---------------------------------------------------------------- fanout

def _side(f, pt):
    """N/E/S/W of a point seen from the part's centre (board y grows down)"""
    cx, cy = (f.crtyd[0] + f.crtyd[2]) / 2, (f.crtyd[1] + f.crtyd[3]) / 2
    dx, dy = pt[0] - cx, pt[1] - cy
    return ('E' if dx > 0 else 'W') if abs(dx) * (f.crtyd[3] - f.crtyd[1]) > abs(dy) * (f.crtyd[2] - f.crtyd[0]) \
        else ('S' if dy > 0 else 'N')


def _escape(g, start, ly, outside, maxcells):
    """BFS on one layer from the start cells: (exit cell or None, its steps, nearest
    via-legal cell or None, its steps)"""
    W, H, blk = g.W, g.H, g.blk[ly]
    seen = {k: 0 for k in start}
    q, ex, via = list(start), None, None
    for k in q:
        d = seen[k]
        if via is None and not g.vblk[k]:
            via = (k, d)
        if ex is None and outside(g.xy(k)):
            ex = (k, d)
        if ex and via:
            break
        if d >= maxcells:
            continue
        x, y = k % W, k // W
        for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1), (1, 1), (1, -1), (-1, 1), (-1, -1)):
            nx, ny = x + dx, y + dy
            nk = ny * W + nx
            if 0 <= nx < W and 0 <= ny < H and nk not in seen and not blk[nk]:
                seen[nk] = d + 1
                q.append(nk)
    return (ex or (None, 0)) + (via or (None, 0))


def c_fanout(b, a):
    from kpcb_route import Grid, _netclass, piece_parts
    from kpcb_move import cu_layers
    if not a.args or a.args[0] not in b.fps:
        print("fanout REF   (per pin: escapes on its layer, a dogbone via, or boxed in)"); return 1
    f = b.fps[a.args[0]]
    if not f.placed:
        print(f"{f.ref} is not on the board yet: `place {f.ref}` first"); return 1
    res, cr = 0.05, f.crtyd
    box = grow(cr, 2.0)
    out = lambda pt: not (cr[0] - .1 <= pt[0] <= cr[2] + .1 and cr[1] - .1 <= pt[1] <= cr[3] + .1)   # noqa: E731
    pads = [p for p in f.pads if p['net'] and not p['net'].startswith('unconnected-')]
    rows, nets = [], sorted({p['net'] for p in pads})
    print(f"fanout {f.ref} ({f.value}): {len(pads)} netted pad(s), {len(nets)} net(s); courtyard "
          f"{cr[2] - cr[0]:.1f} x {cr[3] - cr[1]:.1f} mm; each net alone against today's copper")
    for net in nets:
        nc = _netclass(b, net)
        w0 = float(nc.get('track_width', 0.2))
        vsz, vdr = float(nc.get('via_diameter', 0.6)), float(nc.get('via_drill', 0.3))
        done = {id(p) for q in piece_parts(b, net) if q['tracks'] or q['vias'] or q['fills']
                for g_, p in q['pads'] if g_ is f}
        for p in (p for p in pads if p['net'] == net):
            if id(p) in done:
                rows.append((p, 'routed', '', None)); continue
            lys = [ly for ly in b.copper if p['drill'] or ly in cu_layers(p['layers'], b.copper)]
            verdict = None
            for w in [w0] + [x for x in (0.4, 0.3, 0.25, 0.2, 0.15) if x < w0 - 1e-9]:    # route's neck widths
                g = Grid(b, net, box, res, w, vsz, vdr, b.copper)
                cells, _c = g.cells_of({'pads': [(f, p)], 'vias': [], 'tracks': [], 'fills': []})
                best = None
                for ly in lys:
                    st = cells.get(ly)
                    if not st:
                        continue
                    ex, de, vk, dv = _escape(g, st, ly, out, int(6 / res))
                    cand = (0 if ex is not None else 1, de if ex is not None else dv, ly, ex, de, vk, dv)
                    if (ex is not None or vk is not None) and (best is None or cand < best):
                        best = cand
                if best:
                    _o, _d, ly, ex, de, vk, dv = best
                    neck = f" (neck {w:g} mm: {w0:g} does not leave the pad)" if w < w0 - 1e-9 else ''
                    if ex is not None and (vk is None or de * res <= 3.0 or p['drill']):
                        verdict = ('escape', f"{ly} out {_side(f, g.xy(ex))} at w {w:g}, {de * res:.1f} mm to clear "
                                   f"the courtyard{neck}", g.xy(ex))
                    else:
                        verdict = ('via', f"dogbone via {vsz:g}/{vdr:g} at {g.xy(vk)[0]:.2f},{g.xy(vk)[1]:.2f} "
                                   f"({dv * res:.2f} mm, {ly}){neck}" + ('' if ex is not None else '; no same-layer exit'),
                                   g.xy(vk))
                    break
                if any(cells.get(ly) for ly in lys):     # it leaves the pad but reaches neither
                    verdict = ('boxed', f"leaves the pad at w {w:g} but reaches no exit or via spot within 6 mm", None)
                    break
            if verdict is None:
                verdict = ('boxed', f"no track as narrow as {min(0.15, w0):g} mm leaves the pad: via-in-pad "
                           f"({p['sx']:.2f} x {p['sy']:.2f} mm pad) or move the neighbour", None)
            rows.append((p, *verdict))
    from collections import Counter
    from kcommon import natkey
    n = Counter(r[1] for r in rows)
    sides = Counter(_side(f, r[3]) for r in rows if r[1] == 'escape' and r[3])
    print(f"  {n['routed']} routed, {n['escape']} escape on their layer ({' '.join(f'{k} {v}' for k, v in sorted(sides.items()))}), "
          f"{n['via']} need a dogbone via, {n['boxed']} boxed in")
    seen = Counter((r[0]['num'], r[1]) for r in rows)      # a thermal pad's many copper pieces: one line
    for p, kind, txt, _pt in sorted(rows, key=lambda r: ({'boxed': 0, 'via': 1, 'escape': 2, 'routed': 3}[r[1]],
                                                         natkey(r[0]['num']))):
        if (kind == 'routed' and not a.all) or seen[(p['num'], kind)] == 0:
            continue
        x = f" x{seen[(p['num'], kind)]}" if seen[(p['num'], kind)] > 1 else ''
        seen[(p['num'], kind)] = 0
        print(f"  {(f.ref + '.' + p['num'] + x):<10} {unesc_disp(p['net'].split('/')[-1]):<20} {kind:<7} {txt}")
    if n['routed'] and not a.all:
        print(f"  ({n['routed']} routed pad(s) not listed: --all)")
    print("plan: boxed pins first (via-in-pad, or move what boxes them), then the dogbones (a via row "
          "one pitch out, alternating sides), then `route` the escapes; `vscan NET BOX` shows a pin's cells")
    return 0



# ---------------------------------------------------------------- detours / reroute

NOREROUTE = ('PWR', 'RF', 'USB', 'DIFF')


def detours(b, skip=(), ratio=1.5, excess=3.0, nets=None):
    """[(excess mm, net, routed mm, MST mm, vias, pads, main layer)]: routed signal nets whose
    copper is > ratio x their pad MST (octilinear) and > excess mm longer, or that carry more
    than two vias per link. Not GND, poured, power/RF/USB/diff-class or 9+ pad nets (named
    `nets` skip the class and size tests)."""
    poured = {fl['net'] for fl in b.fills}
    pads, ln, vias, lay = defaultdict(list), defaultdict(float), defaultdict(int), defaultdict(lambda: defaultdict(float))
    for f in b.placed():
        for p in f.pads:
            if p['net']:
                pads[p['net']].append((p['x'], p['y']))
    for t in b.tracks:
        ln[t['net']] += t['len']
        lay[t['net']][t['layer']] += t['len']
    for v in b.vias:
        vias[v['net']] += 1
    out = []
    for net in (nets or ln):
        pts = list(dict.fromkeys(pads.get(net, ())))
        if not net or net not in ln or len(pts) < 2:
            continue
        if nets is None and (net in skip or GND_RE.search(net) or net in poured or len(pts) > 8
                             or any(k in (b.netclass(net) or '').upper() for k in NOREROUTE)):
            continue
        M = sum(d for *_ij, d in mst(pts))
        L, V = ln[net], vias[net]
        if nets or (L - M > excess and L > ratio * M) or V > 2 * (len(pts) - 1):
            out.append((L - M, net, L, M, V, len(pts), max(lay[net], key=lay[net].get)))
    return sorted(out, key=lambda r: -r[0])


def c_reroute(b, a):
    import copy, io, os, shutil, contextlib
    from kpcb_board import Board, _resolve_net
    from kpcb_edit import c_ripup
    from kpcb_route import c_route, links
    names = []
    for nm in a.args:
        net, cand = _resolve_net(b, nm)
        if not net:
            print(f"{nm}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); return 1
        names.append(net)
    from kpcb_smooth import skipped
    rows = detours(b, skipped(b), nets=names or None)
    if not rows:
        print("reroute: no detour (routed signal nets within 1.5x their pad MST and <= 2 vias per link)"
              if not names else "reroute: none of those nets is routed"); return 0
    rows = rows[:a.max]
    vc = a.via_cost
    print(f"reroute: {len(rows)} net(s), each ripped and routed again on a copy; kept when complete and "
          f"length + {vc:g} mm per via drops by >= 5%")
    d = os.path.dirname(os.path.abspath(b.path))        # beside the .kicad_pro: the router reads its rules
    cur, tmp, kept = b.path, [], 0
    try:
        for i, (_x, net, L, M, V, npads, _ly) in enumerate(rows):
            B = Board(cur) if cur != b.path else b
            mine = [t for t in B.tracks if t['net'] == net] + [v for v in B.vias if v['net'] == net]
            L1, V1 = sum(t['len'] for t in mine if 'len' in t), sum(1 for v in mine if 'drill' in v)
            label = f"  {unesc_disp(net.split('/')[-1]):<22} {npads} pads  {L1:6.1f} mm {V1} via(s) (MST {M:.1f})"
            if any(x.get('locked') for x in mine):
                print(f"{label}  skipped: locked copper"); continue
            if any(fl['net'] == net for fl in B.fills):
                print(f"{label}  skipped: it has a pour"); continue
            t1, t2 = (os.path.join(d, f".reroute-{os.getpid()}-{i}{k}.kicad_pcb") for k in 'ab')
            tmp += [t1, t2]
            log = io.StringIO()
            with contextlib.redirect_stdout(log):
                a1 = copy.copy(a); a1.args, a1.out, a1.drc, a1.box = [net], t1, '', ''
                r1 = c_ripup(B, a1)
                ok = r1 in (0, None) and os.path.exists(t1)
                if ok:
                    a2 = copy.copy(a); a2.args, a2.out, a2.pending = [net], t2, False
                    c_route(Board(t1), a2)
            if not (ok and os.path.exists(t2)):
                print(f"{label}  kept: the route failed ({(log.getvalue().strip().splitlines() or ['?'])[-1][:70]})")
                continue
            B2 = Board(t2)
            if links(B2, net)[1]:
                print(f"{label}  kept: the router cannot complete it without the old copper"); continue
            L2 = sum(t['len'] for t in B2.tracks if t['net'] == net)
            V2 = sum(1 for v in B2.vias if v['net'] == net)
            s1, s2 = L1 + vc * V1, L2 + vc * V2
            if s2 < s1 * 0.95:
                print(f"{label}  -> {L2:6.1f} mm {V2} via(s)  REROUTED")
                cur, kept = t2, kept + 1
            else:
                print(f"{label}  -> {L2:6.1f} mm {V2} via(s)  kept: not better (the detour is the shortest legal path)")
        if a.out and kept:
            shutil.copy(cur, a.out)
            print(f"wrote {a.out}: {kept} net(s) re-laid. Then `smooth --all`, refill and "
                  f"`kdrc.py {a.out} drc --refill-save --baseline BEFORE.json`")
        elif kept:
            print(f"(dry run: {kept} net(s) would be re-laid; -o OUT writes them)")
    finally:
        for f in tmp:
            if os.path.exists(f):
                os.remove(f)
    return 0
