"""kpcb.py `zones`: per-layer pour coverage, or coverage under one footprint."""
import os, re, json, glob, math
from collections import defaultdict, Counter
from kcommon import GND_RE, rail_voltage, load_sexp, kids, kid
from kpcb_board import (_sample_grid, bbox, ctr, hit, inside, overlap_area, point_in_poly,
                        poly_area, PolyIndex, pt_seg_dist, xf)
from kpcb_copper import _local, _reach

def _clip(p, q, r):
    """length of segment p-q inside rectangle r (Liang-Barsky)"""
    t0, t1 = 0.0, 1.0
    dx, dy = q[0] - p[0], q[1] - p[1]
    for pp, qq in ((-dx, p[0] - r[0]), (dx, r[2] - p[0]), (-dy, p[1] - r[1]), (dy, r[3] - p[1])):
        if pp == 0:
            if qq < 0:
                return 0.0
        elif pp < 0:
            t0 = max(t0, qq / pp)
        else:
            t1 = min(t1, qq / pp)
    return max(0.0, t1 - t0) * math.hypot(dx, dy)


def _slots(b, ly, plane, rect, min_len=None):
    """Foreign-net tracks on a plane layer that cut across the rectangle: a slot
    in the reference, which point samples only see when a column happens to land
    on it. [(net, (length inside, widest, where))], longest first, for runs across
    >= half the rect's shorter side (or min_len mm)."""
    lim = min_len if min_len is not None else min(rect[2] - rect[0], rect[3] - rect[1]) / 2
    per = defaultdict(lambda: [0.0, 0.0, []])
    for t in b.tracks:
        if t['layer'] == ly and t['net'] != plane:
            L = _clip(t['a'], t['b'], rect)
            if L > 0:
                q = per[t['net']]
                q[0] += L
                q[1] = max(q[1], t['w'])
                q[2] += [t['a'], t['b']]
    out = []
    for net, (L, w, pts) in sorted(per.items(), key=lambda z: -z[1][0]):
        if L >= lim:
            xs, ys = [p[0] for p in pts], [p[1] for p in pts]
            span = (f"x={sum(xs) / len(xs):.1f}" if max(xs) - min(xs) < 1 else
                    f"y={sum(ys) / len(ys):.1f}" if max(ys) - min(ys) < 1 else
                    f"{min(xs):.1f},{min(ys):.1f}..{max(xs):.1f},{max(ys):.1f}")
            out.append((net, (L, w, span)))
    return out


def _zones_under(b, a):
    """`zones REF...`: point-sample whether a filled net actually covers a
    footprint's courtyard, instead of just eyeballing the whole-board table.
    Closes the RF-reference-plane question ("is GND continuous under U9")
    that the bbox-margin view can only infer from dominant-island + all-edges-
    reached. 11x11 samples per footprint; each sample tests every fill whose
    own bbox overlaps the courtyard, so a plane broken into several polygons
    still counts as covered wherever any piece of it reaches."""
    fail = 0
    for ref in a.args:
        f = b.fps.get(ref)
        if not f:
            print(f"{ref}: no such footprint"); fail = 1; continue
        rect = f.crtyd
        # an edge-mount SMA/USB-C courtyard overhangs the outline; copper can't
        # exist there, so sampling it would read a good launch as MOSTLY MISSING.
        # Grid only the on-board part (then drop any point a notch still excludes).
        o = b.outline or rect
        on = (max(rect[0], o[0]), max(rect[1], o[1]), min(rect[2], o[2]), min(rect[3], o[3]))
        # ponytail: fixed 0.5 mm inset on clipped sides for the pour's edge pullback;
        # read the zone/edge clearances if a board pulls back further
        e = 0.5
        g = (on[0] + e * (on[0] > rect[0]), on[1] + e * (on[1] > rect[1]),
             on[2] - e * (on[2] < rect[2]), on[3] - e * (on[3] < rect[3]))
        pts = _sample_grid(g, 11) if g[0] < g[2] and g[1] < g[3] else []
        if b.rings:
            pts = [p for p in pts if inside(p, b.rings)]
        off = 1 - overlap_area(on, rect) / max(f.area, 1e-9) if pts else 1.0
        print(f"{ref}: courtyard {rect[2]-rect[0]:.1f} x {rect[3]-rect[1]:.1f} mm "
              f"@ {ctr(rect)[0]:.1f},{ctr(rect)[1]:.1f}"
              + (f"  ({100*off:.0f}% hangs off the board edge, not sampled)" if off > 0.005 else ''))
        if not pts:
            continue
        for ly in b.copper:
            # bbox overlap is only a coarse prefilter: a net's OWN pour can be
            # substantial yet still never actually reach this courtyard (its
            # island bbox just happens to graze it), so score every candidate
            # net and only report ones that land at least one real sample -
            # otherwise "the plane exists somewhere nearby" reads as "broken".
            fills_here = [fl for fl in b.fills if fl['layer'] == ly and hit(fl['bbox'], rect)]
            cand = sorted({fl['net'] for fl in fills_here})
            scored = []
            for net in cand:
                fs = [fl for fl in fills_here if fl['net'] == net]
                hit_n, holes = 0, []
                for p in pts:
                    if any(point_in_poly(p, fl['pts']) for fl in fs):
                        hit_n += 1
                    else:
                        holes.append(p)
                if hit_n:
                    scored.append((net, hit_n, holes))
            if not scored:
                print(f"    {ly:<8} no fill actually reaches this footprint's courtyard")
                fail = 1
                continue
            for net, hit_n, holes in scored:
                pct = 100 * hit_n / len(pts)
                # a handful of misses is normal: every non-plane pad/via under
                # the part carries its own clearance moat. Only a MAJORITY
                # miss (a genuinely broken or absent reference) fails the exit
                # code; a few holes just get listed for a look, not a verdict.
                if hit_n == len(pts):
                    tag = 'continuous'
                elif not (GND_RE.match(net.split('/')[-1]) or rail_voltage(net.split('/')[-1]) is not None):
                    tag = 'local signal pour, not a reference plane'   # no verdict
                elif pct >= 50:
                    tag = 'has gaps (normal near non-plane pads/vias unless clustered)'
                else:
                    tag = 'MOSTLY MISSING'
                    fail = 1
                sl = []
                if tag != 'local signal pour, not a reference plane':
                    own = {p['net'] for p in f.pads} if ly == f.layer else set()   # its own fan-out
                    sl = [x for x in _slots(b, ly, net, g) if x[0] not in own]
                    if sl and tag.startswith('has gaps'):
                        tag = 'has gaps, and a SLOT cuts it'
                print(f"    {ly:<8} {net:<10} {pct:5.0f}% of samples covered  ({tag})")
                if sl:
                    for s_net, (L, w, span) in sl[:2]:
                        print(f"        !! SLOT: {s_net} track(s) ({w:.2f} mm) run {L:.1f} mm through the "
                              f"{net} plane under {ref} ({span}) - return current detours around it")
                    if len(sl) > 2:
                        print(f"        !! +{len(sl) - 2} more foreign net(s) cutting it: "
                              + ', '.join(n for n, _ in sl[2:6]) + (' ...' if len(sl) > 6 else ''))
                if holes:
                    shown = ' '.join(f'{x:.1f},{y:.1f}' for x, y in holes[:4])
                    more = f' ...+{len(holes)-4}' if len(holes) > 4 else ''
                    print(f"        uncovered near: {shown}{more}")
    print("\nSamples are an 11x11 grid inside the courtyard, not the whole fill - a scattered few\n"
          "misses are the normal clearance moat around any non-plane pad or via under the part,\n"
          "not a defect; only a large contiguous run of misses or <50% total is flagged. 100% is\n"
          "on this footprint only, not a guarantee elsewhere on the plane. Fills are the LAST\n"
          "SAVED state (see `zones` with no args).")
    return 2 if fail else 0

# ---------------------------------------------------------------- voids

def _clearance_fn(b):
    """clr(net_a, net_b): the larger netclass clearance, raised by any .kicad_dru
    rule that names the pair with A.NetName/B.NetName (the unfused-pack rule)"""
    d = os.path.dirname(os.path.abspath(b.path))
    cls = {}
    for pro in glob.glob(os.path.join(d, '*.kicad_pro')):
        try:
            cls = {c['name']: c for c in json.load(open(pro))['net_settings']['classes']}
        except (OSError, ValueError, KeyError):
            pass
        break
    pair = {}
    for dru in glob.glob(os.path.join(d, '*.kicad_dru')):
        txt = open(dru, encoding='utf-8', errors='replace').read()
        for m in re.finditer(r'\(constraint clearance \(min ([\d.]+)mm\)\)\s*\(condition "([^"]*)"\)', txt):
            A = re.findall(r"A\.NetName == '([^']*)'", m[2])
            B = re.findall(r"B\.NetName == '([^']*)'", m[2])
            for x in A:
                for y in B:
                    pair[frozenset((x, y))] = max(pair.get(frozenset((x, y)), 0), float(m[1]))
    dflt = (cls.get('Default') or {}).get('clearance') or 0.2
    def clr(n1, n2):
        c = [(cls.get(b.netclass(n) or 'Default') or {}).get('clearance') or dflt for n in (n1, n2)]
        return max(c + [pair.get(frozenset((n1, n2)), 0)])
    via = cls.get(b.netclass('GND') or 'Default') or {}
    return clr, (via.get('via_diameter') or 0.6), (via.get('via_drill') or 0.3)


def _keepouts(b):
    """[(layers, PolyIndex, vias_blocked, pour_blocked)] from board and footprint rule areas"""
    out = []
    root = load_sexp(b.path)
    def add(z, T):
        ko = kid(z, 'keepout')
        pts = [T(float(q[1]), float(q[2])) for q in (kid(kid(z, 'polygon') or [], 'pts') or [])[1:]
               if isinstance(q, list)]
        if ko and len(pts) >= 3:
            lays = [l for l in (kid(z, 'layers') or kid(z, 'layer') or [])[1:] if isinstance(l, str)]
            rule = {k[0]: k[1] for k in ko[1:] if isinstance(k, list) and len(k) > 1}
            out.append((set(lays) if '*.Cu' not in lays else set(b.copper), PolyIndex(pts),
                        rule.get('vias') == 'not_allowed', rule.get('copperpour') == 'not_allowed'))
    for z in kids(root, 'zone'):
        add(z, lambda x, y: (x, y))
    for node in kids(root, 'footprint'):
        ref = next((p[2] for p in kids(node, 'property') if len(p) > 2 and p[1] == 'Reference'), '')
        f = b.fps.get(ref)
        for z in kids(node, 'zone'):
            if f:                                    # footprint rule areas are stored local
                add(z, lambda x, y, f=f: xf(x, y, f.x, f.y, f.rot))
    return out


def _voids(b, a):
    """`zones --voids`: copper-free regions on the plane net's layers, and for each
    a via spot that would tie it to the plane on another layer."""
    P, o = 0.25, b.outline
    if not o:
        print("no board outline"); return 1
    x0, y0 = o[0] - 1, o[1] - 1                     # 1 mm of outside, so the edge erodes
    W, H = int((o[2] + 1 - x0) / P) + 1, int((o[3] + 1 - y0) / P) + 1
    area = defaultdict(float)
    for fl in b.fills:
        area[fl['net']] += fl['area']
    plane = (a.net or [None])[0] or max(area, key=area.get, default=None)
    if not plane:
        print("no zone fills on this board"); return 1
    clr, vdia, vdrill = _clearance_fn(b)
    vr = vdia / 2
    board_a = max((poly_area(r) for r in b.rings), default=(o[2] - o[0]) * (o[3] - o[1]))
    layers = [ly for ly in b.copper
              if sum(fl['area'] for fl in b.fills if fl['net'] == plane and fl['layer'] == ly) > .3 * board_a]

    def raster(idxs, m=None):
        """cells whose centre is inside (even-odd over every ring given)"""
        m = m if m is not None else bytearray(W * H)
        for r in range(H):
            y = y0 + (r + .5) * P
            xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1) for ix in idxs
                        for x1, y1, x2, y2 in ix.rows.get(math.floor(y / ix.cell), ())
                        if (y1 > y) != (y2 > y))
            for xa, xb in zip(xs[0::2], xs[1::2]):
                for c in range(max(0, math.ceil((xa - x0) / P - .5)), min(W - 1, math.floor((xb - x0) / P - .5)) + 1):
                    m[r * W + c] = 1
        return m

    def dilate(m, k):
        """square dilation by k cells, separable"""
        out = bytearray(W * H)
        for r in range(H):
            row, run = m[r * W:(r + 1) * W], 0
            for c in range(W + k):
                if c < W and row[c]:
                    run = 2 * k + 1
                if run:
                    if c - k >= 0:
                        out[r * W + c - k] = 1
                    run -= 1
        out2 = bytearray(W * H)
        for c in range(W):
            run = 0
            for r in range(H + k):
                if r < H and out[r * W + c]:
                    run = 2 * k + 1
                if run:
                    if r - k >= 0:
                        out2[(r - k) * W + c] = 1
                    run -= 1
        return out2

    def disc(m, x, y, rad, test=None):
        for r in range(max(0, int((y - rad - y0) / P)), min(H, int((y + rad - y0) / P) + 2)):
            for c in range(max(0, int((x - rad - x0) / P)), min(W, int((x + rad - x0) / P) + 2)):
                cx, cy = x0 + (c + .5) * P, y0 + (r + .5) * P
                if (test(cx, cy) if test else math.hypot(cx - x, cy - y) <= rad):
                    m[r * W + c] = 1

    board = raster([PolyIndex(r) for r in b.rings]) if b.rings else bytearray(b'\x01' * (W * H))
    inner = bytearray(1 - v for v in dilate(bytearray(1 - v for v in board), 2))   # 0.5 mm off the edge
    kos = _keepouts(b)
    pads = [(f, p) for f in b.fps.values() for p in f.pads]
    cu, gnd, blk, soft = {}, {}, {}, {}
    for ly in b.copper:
        fm, ff, gm = bytearray(W * H), bytearray(W * H), bytearray(W * H)
        for fl in b.fills:
            if fl['layer'] == ly:
                fl['_ix'] = PolyIndex(fl['pts'])
                raster([fl['_ix']], gm if fl['net'] == plane else ff)
        fm = bytearray(x | y for x, y in zip(gm, ff))
        sf = dilate(ff, math.ceil((vr + clr(plane, '')) / P))       # a foreign pour: it refills around a via
        bl = bytearray(W * H)
        for t in b.tracks:
            if t['layer'] != ly:
                continue
            A, B = t['a'], t['b']
            disc(fm, *t['mid'], t['len'] / 2 + t['w'] / 2, lambda x, y: pt_seg_dist((x, y), A, B) <= t['w'] / 2)
            if t['net'] != plane:
                d = t['w'] / 2 + vr + clr(plane, t['net'])
                disc(bl, *t['mid'], t['len'] / 2 + d, lambda x, y: pt_seg_dist((x, y), A, B) <= d)
        for f, p in pads:
            if p['kind'] == 'np_thru_hole':
                continue                             # no copper: `drills` covers the hole
            lys = b.copper if p['drill'] or any(l.startswith('*') for l in p['layers']) else p['layers']
            if ly in lys:
                hx, hy = _reach(p)
                def inpad(x, y, d, p=p, hx=hx, hy=hy):
                    u, v = _local(p, (x, y))
                    return abs(u) <= hx + d and abs(v) <= hy + d
                disc(fm, p['x'], p['y'], math.hypot(hx, hy), lambda x, y: inpad(x, y, 0))
                if p['net'] != plane:
                    d = vr + clr(plane, p['net'])
                    disc(bl, p['x'], p['y'], math.hypot(hx, hy) + d, lambda x, y, d=d: inpad(x, y, d))
        for v in b.vias:
            disc(fm, v['x'], v['y'], (v['size'] or .6) / 2)
            if v['net'] != plane:
                disc(bl, v['x'], v['y'], (v['size'] or .6) / 2 + vr + clr(plane, v['net']))
        cu[ly], gnd[ly], blk[ly], soft[ly] = fm, gm, bl, sf
    drills = bytearray(W * H)                        # hole-to-hole, any net
    for x, y, d in [(v['x'], v['y'], v['drill']) for v in b.vias] + \
                   [(p['x'], p['y'], p['drill']) for _f, p in pads if p['drill']]:
        disc(drills, x, y, d / 2 + vdrill / 2 + .25)
    via_ko = bytearray(W * H)
    for lys, ix, novia, _nopour in kos:
        if novia:
            x_0, y_0, x_1, y_1 = ix.bbox
            disc(via_ko, (x_0 + x_1) / 2, (y_0 + y_1) / 2, math.hypot(x_1 - x_0, y_1 - y_0) / 2,
                 lambda x, y, ix=ix: (x, y) in ix)

    print(f"{b.path}: voids in the {plane} pour > {a.area:g} mm2  (0.25 mm raster of the LAST SAVED fill; "
          f"a void's core is >= 0.5 mm from any copper)")
    print(f"  plane layers: {', '.join(layers) or 'none'}   rescue via {vdia:g}/{vdrill:g} mm, "
          f"clearances from the netclasses + .kicad_dru net-pair rules")
    found = 0
    for ly in layers:
        core = bytearray(i and not c for i, c in zip(inner, dilate(cu[ly], 2)))
        seen = bytearray(W * H)
        for start in range(W * H):
            if not core[start] or seen[start]:
                continue
            comp, todo = [], [start]
            seen[start] = 1
            while todo:
                i = todo.pop()
                comp.append(i)
                r, c = divmod(i, W)
                for j in ((i - 1) if c else -1, (i + 1) if c < W - 1 else -1, i - W, i + W):
                    if 0 <= j < W * H and core[j] and not seen[j]:
                        seen[j] = 1
                        todo.append(j)
            ar = len(comp) * P * P
            if ar < a.area:
                continue
            found += 1
            xs = [x0 + (i % W + .5) * P for i in comp]
            ys = [y0 + (i // W + .5) * P for i in comp]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            ko = sum(1 for i in comp for lys, ix, _v, nopour in kos
                     if nopour and ly in lys and (x0 + (i % W + .5) * P, y0 + (i // W + .5) * P) in ix)
            line = (f"  {ly:<7} void core {ar:6.1f} mm2  x {min(xs):.1f}..{max(xs):.1f}  "
                    f"y {min(ys):.1f}..{max(ys):.1f}")
            if ko > len(comp) / 2:
                print(line + "  (a no-pour keepout: intended)"); continue
            ok = [i for i in comp if not drills[i] and not via_ko[i]
                  and not any(blk[l][i] for l in b.copper)
                  and any(gnd[l][i] for l in layers if l != ly)]
            clean = [i for i in ok if not any(soft[l][i] for l in b.copper)]
            if ok:
                pick = clean or ok
                best = min(pick, key=lambda i: math.hypot(x0 + (i % W + .5) * P - cx, y0 + (i // W + .5) * P - cy))
                bx, by = x0 + (best % W + .5) * P, y0 + (best // W + .5) * P
                on = [l for l in layers if l != ly and gnd[l][best]]
                d = vr + clr(plane, '')
                ring = [(bx + d * math.cos(k * math.pi / 4), by + d * math.sin(k * math.pi / 4)) for k in range(8)]
                cut = sorted({(fl['layer'], fl['net']) for fl in b.fills if fl['net'] != plane and hit(
                    fl['bbox'], (bx - d, by - d, bx + d, by + d)) and any(q in fl['_ix'] for q in ring + [(bx, by)])})
                print(line + f"\n          rescue via at {bx:.2f},{by:.2f} ({len(clean)} clean / {len(ok)} "
                             f"track-clear spot(s)): {plane} on {', '.join(on)} there"
                             + (", nothing foreign within clearance on any layer" if not cut else
                                "; punches the " + ', '.join(f"{n} pour on {l}" for l, n in cut)
                                + " (it refills around the via)"))
            else:
                why = Counter()
                for i in comp:
                    why.update(['drill'] * bool(drills[i]) + ['via keepout'] * bool(via_ko[i])
                               + [f"foreign track/pad/via on {l}" for l in b.copper if blk[l][i]]
                               + ([] if any(gnd[l][i] for l in layers if l != ly) else [f"no {plane} on another layer"]))
                print(line + f"\n          no clear via spot inside it. Of {len(comp)} cells: "
                      + ', '.join(f"{n} {w}" for w, n in why.most_common(4)))
    if not found:
        print("  none")
    print("\nA void here is copper-free board, usually a pour island KiCad removed because no via\n"
          "ties it to the plane. A rescue via spot clears foreign copper by via radius +\n"
          "clearance on every layer, other drills by 0.25 mm, and via keepouts. Refill (B) after\n"
          "adding one, then `kdrc.py` to confirm.")
    return 0


def c_zones(b, a):
    """Zone-fill coverage per copper layer, from the fills cached in the board.

    Reports gross copper area over board area, island count, largest-island
    share, and the fill's bounding-box margins to each board edge - enough to
    catch a pour that is fragmented, missing a layer, or nowhere near an edge.
    It is geometry on the LAST SAVED fill, not a live refill: a zone shows 0
    if it was never filled or a part moved after the last Fill All Zones.

    `zones REF...` instead point-samples whether a net's fill actually covers
    that footprint's courtyard - see `_zones_under`."""
    if getattr(a, 'voids', False):
        return _voids(b, a)
    if a.args:
        return _zones_under(b, a)
    o = b.outline
    board_area = max((poly_area(r) for r in b.rings), default=0.0) or \
        ((o[2] - o[0]) * (o[3] - o[1]) if o else 0.0)
    by_layer = defaultdict(lambda: defaultdict(list))     # layer -> net -> [fill]
    for fl in b.fills:
        by_layer[fl['layer']][fl['net']].append(fl)
    declared = {(net, ly) for net, lays in b.zones for ly in lays}
    filled = set()
    rows = []
    for ly in b.copper:
        nets = by_layer.get(ly)
        if not nets:
            rows.append((ly, '-', 0.0, 0, 0.0, None))
            continue
        for net, fs in sorted(nets.items()):
            filled.add((net, ly))
            area = sum(f['area'] for f in fs)
            fb = bbox([p for f in fs for p in f['pts']])
            largest = max((f['area'] for f in fs), default=0.0)
            rows.append((ly, net, area, len(fs), largest, fb))
    unfilled = sorted(declared - filled)

    if a.json:
        print(json.dumps({'board_area_mm2': round(board_area, 1), 'outline': o,
                          'layers': [{'layer': ly, 'net': net,
                                      'area_mm2': round(ar, 1),
                                      'coverage_pct': round(100 * ar / board_area, 1) if board_area else 0,
                                      'islands': isl,
                                      'largest_island_pct': round(100 * lg / ar, 1) if ar else 0,
                                      'fill_bbox': [round(v, 2) for v in fb] if fb else None}
                                     for ly, net, ar, isl, lg, fb in rows if net != '-'],
                          'declared_unfilled': [{'net': n, 'layer': l} for n, l in unfilled]},
                         indent=1))
        return 0

    print(f"{b.path}: zone fill coverage")
    if o:
        print(f"board {board_area:.0f} mm2 (outline ring)   bbox "
              f"x {o[0]:.1f}..{o[2]:.1f}  y {o[1]:.1f}..{o[3]:.1f}")
    print(f"\n{'layer':<8} {'net':<8} {'cover':>6} {'isl':>4} {'top1':>5}  "
          f"uncovered margins (mm, of fill bbox)")
    for ly, net, area, isl, largest, fb in rows:
        if net == '-':
            print(f"{ly:<8} {'-':<8} {'0%':>6} {'0':>4} {'-':>5}  (no fill on this layer)")
            continue
        cov = 100 * area / board_area if board_area else 0
        top1 = 100 * largest / area if area else 0
        marg = (f"top {fb[1]-o[1]:.1f}  bot {o[3]-fb[3]:.1f}  "
                f"L {fb[0]-o[0]:.1f}  R {o[2]-fb[2]:.1f}") if (fb and o) else ''
        print(f"{ly:<8} {net:<8} {cov:5.0f}% {isl:>4} {top1:4.0f}%  {marg}")
    if unfilled:
        print("\nDECLARED BUT NOT FILLED (never filled, or emptied on last save):")
        for net, ly in unfilled:
            print(f"  {net} on {ly}")
    print("\nCoverage is gross copper area / board area; a low % or a small top-island "
          "share means a\nfragmented pour. Margins are of the fill's bounding box, so they "
          "only flag when NOTHING\nreaches that edge (one sliver hides the gap) - eyeball a "
          "render for interior voids.\nFills are the LAST SAVED state: refill in KiCad "
          "(Edit > Fill All Zones, save) or run\n`kdrc.py FILE drc` (refills in memory) if a "
          "zone reads 0 or parts moved since the fill.")
    return 0
