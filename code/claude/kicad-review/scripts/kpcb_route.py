"""kpcb.py `airwires` and `route`.

`airwires` is the board's own ratsnest from kpcb_copper: every net whose copper is
not one piece, and per missing link the nearest copper of the two pieces (pad, track,
via or pour), the gap and the netclass width. It reads the SAVED fill, so refill first
(kdrc.py flags a stale one).

`route NET` is a grid A* (8 directions, through vias) from the smallest piece of NET
to the rest of it, through clearance-inflated foreign copper on every copper layer:
netclass + .kicad_dru net-pair clearances, copper to edge, rule-area keepouts, hole
to hole for a via. It prefers free board over foreign pours (cutting a plane) and outer
over inner layers, and penalises bends. It prints the path; -o OUT writes it into a
copy. KiCad's DRC on that copy is the verdict - the grid is conservative by half a
cell, pads are their rectangle (a roundrect's corners are never assumed away)."""
import heapq, itertools, math, re
from collections import defaultdict
from kcommon import natkey, unesc_disp
from kpcb_board import PolyIndex, bbox, grow, inside, pad_box, pt_seg_dist, seg_seg_dist as _segd
from kpcb_copper import Copper, _local, _q
from kpcb_move import cu_layers, via_layers
from kpcb_zones import _clearance_fn, _keepouts
from kpcb_edit import PcbText, tear_edits

DIRS = [(1, 0), (1, 1), (0, 1), (-1, 1), (-1, 0), (-1, -1), (0, -1), (1, -1)]


def _pidx(b, fl):
    """a fill's PolyIndex, built once per board: GND alone has ~200 fragments"""
    c = b.__dict__.setdefault('_pidx', {})
    if id(fl) not in c:
        c[id(fl)] = PolyIndex(fl['pts'])
    return c[id(fl)]


def _planes(b):
    """{(net, layer)} that are planes: GND, or > 20% of the board area filled by one net"""
    if '_planes' not in b.__dict__:
        o = b.outline
        A = (o[2] - o[0]) * (o[3] - o[1]) if o else 1.0
        tot = defaultdict(float)
        for fl in b.fills:
            tot[(fl['net'], fl['layer'])] += fl['area']
        b._planes = {k for k, v in tot.items() if k[0] == 'GND' or v > 0.2 * A}
    return b._planes


def _kos(b):
    """rule areas, read once per board (each read re-parses the whole file)"""
    if '_kos' not in b.__dict__:
        b._kos = _keepouts(b, tracks=True)
    return b._kos
SQ2 = math.sqrt(2)


def _netclass(b, net):
    import glob, json, os
    for pro in glob.glob(os.path.join(os.path.dirname(os.path.abspath(b.path)), '*.kicad_pro')):
        try:
            cls = {c['name']: c for c in json.load(open(pro))['net_settings']['classes']}
        except (OSError, ValueError, KeyError):
            return {}
        return cls.get(b.netclass(net) or 'Default') or cls.get('Default') or {}
    return {}


def class_via(cls):
    """(size, drill) of the via route lays for a netclass dict: the class's, or 0.5/0.2 when
    that annular ring is under the fab minimum"""
    vsz, vdr = (cls.get('via_diameter') or 0.6), (cls.get('via_drill') or 0.3)
    if (vsz - vdr) / 2 < 0.15 - 1e-6:
        vsz, vdr = 0.5, 0.2
    return vsz, vdr


# ---------------------------------------------------------------- pieces

def piece_parts(b, net, strict=False):
    """the net's copper split into its pieces: [{'pads': [(f, p)], 'tracks': [...],
    'vias': [...], 'fills': [...]}], the one holding the most pads first (strict: Copper's)"""
    C = Copper(b, net, strict)
    F = C.piece.find
    P = defaultdict(lambda: {'pads': [], 'tracks': [], 'vias': [], 'fills': []})
    for i, fp in enumerate(C.pads):
        P[F(('pad', i))]['pads'].append(fp)
    for k, v in enumerate(C.vias):
        P[F(('via', k))]['vias'].append(v)
    for t in C.tracks:
        P[F(('pt', t['layer'], *_q(t['a'])))]['tracks'].append(t)
    for j, fl in enumerate(C.fills):
        P[F(('fill', j))]['fills'].append(fl)
    out = [q for q in P.values() if any(f.placed for f, _p in q['pads'])]   # padless islands and
    for q in out:                                        # parked parts need no route
        q['pads'] = [(f, p) for f, p in q['pads'] if f.placed]
    return sorted(out, key=lambda q: -len(q['pads']))


def _points(q, cu):
    """sample points of a piece: (x, y, layers, label)"""
    pts = []
    for f, p in q['pads']:
        pts.append((p['x'], p['y'], set(cu) if p['drill'] else cu_layers(p['layers'], cu), f"{f.ref}.{p['num']}"))
    for v in q['vias']:
        pts.append((v['x'], v['y'], via_layers(v, cu), 'via'))
    for t in q['tracks']:
        n = max(1, int(t['len'] / 0.5))
        for k in range(n + 1):
            pts.append((t['a'][0] + (t['b'][0] - t['a'][0]) * k / n,
                        t['a'][1] + (t['b'][1] - t['a'][1]) * k / n, {t['layer']}, f"track {t['layer']}"))
    for fl in q['fills']:
        st = max(1, len(fl['pts']) // 400)
        pts += [(x, y, {fl['layer']}, f"pour {fl['layer']}") for x, y in fl['pts'][::st]]
    return pts


def links(b, net, cell=2.0):
    """Boruvka over the pieces: each round every component (bar the biggest, which the
    others reach) takes its nearest point in another component, until the net is one
    tree. [(gap, point a, point b)] with points (x, y, layers, label)."""
    Q = piece_parts(b, net)
    if len(Q) < 2:
        return Q, []
    P = [_points(q, b.copper) for q in Q]
    G = defaultdict(list)
    for i, pts in enumerate(P):
        for pt in pts:
            G[(math.floor(pt[0] / cell), math.floor(pt[1] / cell))].append((i, pt))
    par = list(range(len(Q)))
    def find(x):
        while par[x] != x:
            par[x] = par[par[x]]
            x = par[x]
        return x
    def nearest(pt, r0):
        """nearest point of another component: ring r holds points >= (r-1)*cell away"""
        gx, gy = math.floor(pt[0] / cell), math.floor(pt[1] / cell)
        best = None
        for r in range(0, 200):
            if best and best[0] <= (r - 1) * cell:
                break
            for cx in range(gx - r, gx + r + 1):
                for cy in (range(gy - r, gy + r + 1) if cx in (gx - r, gx + r) else (gy - r, gy + r)):
                    for j, o in G.get((cx, cy), ()):
                        if find(j) != r0:
                            d = math.dist(pt[:2], o[:2])
                            if best is None or d < best[0]:
                                best = (d, o, j)
        return best
    out = []
    while True:
        roots = defaultdict(list)
        for i in range(len(Q)):
            roots[find(i)].append(i)
        if len(roots) < 2:
            break
        big = max(roots, key=lambda r: sum(len(P[i]) for i in roots[r]))
        cand = []
        for r, members in roots.items():
            if r == big:
                continue
            best = None
            for i in members:
                for pt in P[i]:
                    o = nearest(pt, r)
                    if o and (best is None or o[0] < best[0]):
                        best = (o[0], pt, o[1], o[2], i)
            if best:
                cand.append(best)
        if not cand:
            break
        for d, pa, pb, j, i in sorted(cand, key=lambda c: c[0]):
            ra = find(i)
            if ra != find(j):
                par[ra] = find(j)
                out.append((d, pa, pb))
    return Q, out


def c_splits(b, a):
    """`airwires --since OLD`: nets with more copper pieces than in OLD (a route split a
    pour), and the new tracks of other nets that cross OLD's fill of such a net on their
    layer - the ones to rip. Both boards need a fresh fill (kdrc.py --refill-save)."""
    import json
    from kpcb_board import Board
    o = Board(a.since)
    old = {t['uuid'] for t in o.tracks} | {v['uuid'] for v in o.vias}
    nets = sorted({p['net'] for f in b.placed() for p in f.pads if p['net'] and not p['net'].startswith('unconnected-')})
    worse = []
    for n in nets:
        k0, k1 = len(piece_parts(o, n)), len(piece_parts(b, n))
        if k1 > k0:
            worse.append((n, k0, k1))
    if not worse:
        print(f"no net has more pieces than in {a.since}: nothing was split"); return 0
    rip = []
    for n, k0, k1 in worse:
        hits = []
        for fl in (f for f in o.fills if f['net'] == n):
            idx, fb = PolyIndex(fl['pts']), fl['bbox']
            for t in b.tracks:
                if t['uuid'] in old or t['net'] == n or t['layer'] != fl['layer'] or \
                        max(t['a'][0], t['b'][0]) < fb[0] or min(t['a'][0], t['b'][0]) > fb[2] or \
                        max(t['a'][1], t['b'][1]) < fb[1] or min(t['a'][1], t['b'][1]) > fb[3]:
                    continue
                L = max(1, int(t['len'] / 0.1))
                if any((t['a'][0] + (t['b'][0] - t['a'][0]) * i / L, t['a'][1] + (t['b'][1] - t['a'][1]) * i / L) in idx
                       for i in range(L + 1)) and t not in hits:
                    hits.append(t)
        print(f"{unesc_disp(n)}: {k0} -> {k1} pieces; {len(hits)} new track(s) of other nets cross its old pour")
        for t in hits[:a.max]:
            print(f"    {unesc_disp(t['net']):<30} {t['layer']:<7} @{t['mid'][0]:.2f},{t['mid'][1]:.2f}  w {t['w']:g}")
        rip += hits
    if a.report:
        with open(a.report, 'w') as fh:
            json.dump({'violations': [{'type': 'split', 'items': [{'uuid': t['uuid']} for t in rip]}]}, fh)
        print(f"wrote {len(rip)} uuid(s) to {a.report}: `ripup --drc {a.report} --types split -o OUT` rips them")
    return 2


def c_airwires(b, a):
    if a.since:
        return c_splits(b, a)
    nets = list(a.args) or sorted({p['net'] for f in b.placed() for p in f.pads if p['net']
                                   and not p['net'].startswith('unconnected-')})
    rows = []
    for net in nets:
        Q, L = links(b, net)
        for d, pa, pb in L:
            rows.append((net, len(Q), d, pa, pb))
    if not rows:
        print("airwires: every net is one piece of copper" + (f" ({len(nets)} checked)" if nets else ''))
        return 0
    bynet = defaultdict(list)
    for r in rows:
        bynet[r[0]].append(r)
    print(f"airwires: {len(bynet)} net(s) not in one piece, {len(rows)} link(s) to route "
          f"(from the saved fill - refill first if tracks moved)")
    def lab(p):
        return f"{p[3]} {p[0]:.2f},{p[1]:.2f}"
    order = sorted(bynet, key=lambda n: (sum(r[2] for r in bynet[n]), n))
    for net in order[:a.max * 4]:
        rs = bynet[net]
        nc = b.netclass(net) or 'Default'
        cls = _netclass(b, net)
        print(f"  {unesc_disp(net):<34} {rs[0][1]} pieces  {nc}"
              + (f" w{cls.get('track_width')}" if cls.get('track_width') else ''))
        for _n, _k, d, pa, pb in rs[:a.max]:
            print(f"      {d:6.2f} mm  {lab(pa)}  ->  {lab(pb)}")
        if len(rs) > a.max:
            print(f"      ... +{len(rs) - a.max} more link(s)")
    if len(order) > a.max * 4:
        print(f"  ... +{len(order) - a.max * 4} more net(s)")
    return 0


# ---------------------------------------------------------------- pairs

def _legs(b):
    """[(P net, N net)]: nets named X+/X- or XP/XN (KiCad's diff-pair suffixes)"""
    names = {t['net'] for t in b.tracks if t['net']} | {p['net'] for f in b.fps.values() for p in f.pads if p['net']}
    return [(n, n[:-1] + sn) for n in sorted(names) for sp, sn in (('+', '-'), ('P', 'N'))
            if n.endswith(sp) and n[:-1] + sn in names]


def _coupling(A, B, gmax):
    """(coupled mm of A, [edge gaps]): per 0.1 mm of A's tracks, the edge gap to the nearest
    B track on its layer running parallel (< 15 deg) with the foot of the perpendicular on
    it, counted when within gmax. An arc is read as its chord."""
    def foot(pt, u):
        ex, ey = u['b'][0] - u['a'][0], u['b'][1] - u['a'][1]
        s = ((pt[0] - u['a'][0]) * ex + (pt[1] - u['a'][1]) * ey) / (ex * ex + ey * ey)
        return pt_seg_dist(pt, u['a'], u['b']) if -1e-9 <= s <= 1 + 1e-9 else 1e9
    L, gaps = 0.0, []
    for t in A:
        dx, dy = t['b'][0] - t['a'][0], t['b'][1] - t['a'][1]
        d = math.hypot(dx, dy)
        par = [u for u in B if u['layer'] == t['layer'] and d > 1e-6 and math.dist(u['a'], u['b']) > 1e-6 and
               abs(dx * (u['b'][1] - u['a'][1]) - dy * (u['b'][0] - u['a'][0])) < 0.26 * d * math.dist(u['a'], u['b'])]
        if not par:
            continue
        n = max(1, int(t['len'] / 0.1))
        for k in range(n):
            pt = (t['a'][0] + dx * (k + .5) / n, t['a'][1] + dy * (k + .5) / n)
            g = min(foot(pt, u) - (t['w'] + u['w']) / 2 for u in par)
            if g <= gmax:
                L, gaps = L + t['len'] / n, gaps + [g]
    return L, gaps


def c_pairs(b, a):
    """`pairs [NET...]`: per diff pair the routed length of each leg, the skew, vias, and
    how much of each leg runs coupled (same layer, parallel, edge gap <= 2x the
    netclass diff_pair_gap + its width) with that gap's spread. `route` lays each leg
    alone, so this is the check after it."""
    P = [q for q in _legs(b) if not a.args or any(x in (q[0], q[1], q[0][:-1]) for x in a.args)]
    if not P:
        print("pairs: no diff pair (nets named X+/X- or XP/XN)" + (f" matching {' '.join(a.args)}" if a.args else ''))
        return 1
    print(f"pairs: {len(P)} diff pair(s); lengths are routed track (vias not counted)")
    rc = 0
    for p, n in P:
        nc, cls = b.netclass(p) or 'Default', _netclass(b, p)
        g0, w0 = cls.get('diff_pair_gap') or 0.0, cls.get('diff_pair_width') or cls.get('track_width') or 0.2
        T = [[t for t in b.tracks if t['net'] == x and t['w'] > 0] for x in (p, n)]
        L = [sum(t['len'] for t in ts) for ts in T]
        V = [sum(v['net'] == x for v in b.vias) for x in (p, n)]
        gmax = 2 * g0 + w0 if g0 else 0.5
        (c1, ga), (c2, gb) = _coupling(T[0], T[1], gmax), _coupling(T[1], T[0], gmax)
        W = ['/'.join(f"{w:g}" for w in sorted({t['w'] for t in ts})) or '-' for ts in T]
        print(f"  {unesc_disp(p)} / {unesc_disp(n)}   {nc}: width {w0:g}, gap {g0:g}" if g0 else
              f"  {unesc_disp(p)} / {unesc_disp(n)}   {nc}: no diff_pair_gap set")
        print(f"      routed {L[0]:.2f} / {L[1]:.2f} mm   skew {abs(L[0] - L[1]):.2f} mm   vias {V[0]} / {V[1]}"
              f"   widths {W[0]} / {W[1]}" + ('   <-- a leg is unrouted' if min(L) == 0 else ''))
        G = sorted(ga + gb)
        if G:
            low = g0 and G[0] < g0 - 0.005
            rc = max(rc, 2 if low else 0)
            print(f"      coupled {c1:.2f} / {c2:.2f} mm (uncoupled {L[0] - c1:.2f} / {L[1] - c2:.2f}), edge gap "
                  f"median {G[len(G) // 2]:.3f}, min {G[0]:.3f}, max {G[-1]:.3f} mm"
                  + (f"   <-- under the {g0:g} mm pair gap" if low else ''))
        elif min(L) > 0:
            print(f"      never coupled: no parallel same-layer stretch within {gmax:g} mm of the other leg")
    return rc


# ---------------------------------------------------------------- router

ONES = b'\x01' * 100000


def _pad_shape(p, bb):
    """a pad as a paintable shape: disk, axis-aligned rect, rotated rect (custom pads:
    their whole copper's bounding box)"""
    if p['prims']:
        return ('rect', (bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2, (bb[2] - bb[0]) / 2, (bb[3] - bb[1]) / 2)
    if p['shape'] == 'circle':
        return ('disk', p['x'], p['y'], p['sx'] / 2)
    q = round(p['prot'] / 90.0)
    if abs(p['prot'] - 90 * q) < 1e-6:
        hx, hy = (p['sx'] / 2, p['sy'] / 2) if q % 2 == 0 else (p['sy'] / 2, p['sx'] / 2)
        return ('rect', p['x'], p['y'], hx, hy)
    return ('rot', p)


def _iv(sh, R):
    """(y_lo, y_hi, f) where f(y) is the x-interval of the shape grown by R on row y"""
    k = sh[0]
    if k == 'disk':
        _k, cx, cy, r0 = sh
        r = r0 + R
        def f(y):
            dy = abs(y - cy)
            if dy > r:
                return None
            dx = math.sqrt(r * r - dy * dy)
            return cx - dx, cx + dx
        return cy - r, cy + r, f
    if k == 'rect':
        _k, cx, cy, hx, hy = sh
        def f(y):
            dy = abs(y - cy) - hy
            if dy > R:
                return None
            dx = R if dy <= 0 else math.sqrt(R * R - dy * dy)
            return cx - hx - dx, cx + hx + dx
        return cy - hy - R, cy + hy + R, f
    if k == 'cap':
        _k, (ax, ay), (bx, by), hw = sh
        r = hw + R
        L = math.hypot(bx - ax, by - ay)
        if L < 1e-9:
            return _iv(('disk', ax, ay, hw), R)
        tx, ty = (bx - ax) / L, (by - ay) / L
        _a, _b, fa = _iv(('disk', ax, ay, 0.0), r)
        _a, _b, fb = _iv(('disk', bx, by, 0.0), r)
        def f(y):
            ivs = [iv for iv in (fa(y), fb(y)) if iv]
            lo, hi = -1e18, 1e18
            c1, c2 = tx * (y - ay), ty * (y - ay)      # |n.(p-a)| <= r, 0 <= t.(p-a) <= L
            if abs(ty) < 1e-12:
                if abs(c1) > r:
                    lo, hi = 1.0, 0.0
            else:
                u1, u2 = (r + c1) / ty, (c1 - r) / ty
                lo, hi = max(lo, ax + min(u1, u2)), min(hi, ax + max(u1, u2))
            if abs(tx) < 1e-12:
                if not (-1e-12 <= c2 <= L + 1e-12):
                    lo, hi = 1.0, 0.0
            else:
                u1, u2 = -c2 / tx, (L - c2) / tx
                lo, hi = max(lo, ax + min(u1, u2)), min(hi, ax + max(u1, u2))
            if lo <= hi:
                ivs.append((lo, hi))
            if not ivs:
                return None
            return min(v[0] for v in ivs), max(v[1] for v in ivs)
        return min(ay, by) - r, max(ay, by) + r, f
    p = sh[1]                                            # a pad at an odd angle: per point
    hx, hy = p['sx'] / 2, p['sy'] / 2
    ext = math.hypot(hx, hy) + R
    def f(y):
        xs = [p['x'] - ext + i * 0.005 for i in range(int(2 * ext / 0.005) + 1)]
        ins = [x for x in xs if (lambda u, v: math.hypot(max(abs(u) - hx, 0), max(abs(v) - hy, 0)))(*_local(p, (x, y))) < R]
        return (ins[0], ins[-1]) if ins else None
    return p['y'] - ext, p['y'] + ext, f


class Grid:
    """one routing window: per-layer `blocked` rasters for a track centreline of width
    w, a `vblk` raster for a through via, and a foreign-pour cost raster. Tracks/vias
    this pass laid whose `rid` is in `soft` go in sblk/svblk instead: passable at the
    rip-up cost"""

    def __init__(self, b, net, box, res, w, vsz, vdr, layers, extra_clr=0.0, cut=False, soft=frozenset(), ownpads=True):
        self.b, self.net, self.res, self.w = b, net, res, w
        self.x0, self.y0 = box[0], box[1]
        self.W = int((box[2] - box[0]) / res) + 1
        self.H = int((box[3] - box[1]) / res) + 1
        self.layers, cu = layers, b.copper
        N = self.W * self.H
        self.blk = {ly: bytearray(N) for ly in layers}
        self.pour = {ly: bytearray(N) for ly in layers}
        self.vblk = bytearray(N)
        self.vpour = bytearray(N)                           # a via here sits in a power-island pour
        self.sblk = {ly: bytearray(N) for ly in layers} if soft else None
        self.svblk = bytearray(N) if soft else None
        self.follow, self.follow_k = None, 4.0          # `route --pair`: per-layer deviation from the partner
        clr = _clearance_fn(b)[0]
        # a straight step of length s between two clear grid points dips at most
        # s^2/(8R) into a convex obstacle inflated by R (>= ~0.2 mm here): ~0.003 mm
        m = (res * SQ2) ** 2 / (8 * 0.2) + 0.001
        gbox = grow(box, 2.5)

        def near(bb):
            return bb[2] >= gbox[0] and bb[0] <= gbox[2] and bb[3] >= gbox[1] and bb[1] <= gbox[3]

        def paint(ras, shape, R):
            y_lo, y_hi, f = _iv(shape, R)
            W = self.W
            for jj in range(max(0, math.ceil((y_lo - self.y0) / res)), min(self.H - 1, math.floor((y_hi - self.y0) / res)) + 1):
                iv = f(self.y0 + jj * res)
                if iv:
                    i0 = max(0, math.ceil((iv[0] - self.x0) / res - 1e-9))
                    i1 = min(W - 1, math.floor((iv[1] - self.x0) / res + 1e-9))
                    if i0 <= i1:
                        ras[jj * W + i0:jj * W + i1 + 1] = ONES[:i1 - i0 + 1]

        pads = [(f, p) for f in b.fps.values() if f.placed for p in f.pads if p['kind'] != 'np_thru_hole']
        holes = []
        for f in b.fps.values():                         # NPTH: hole to track/via 0.2 (the .kicad_dru)
            for p in f.pads:
                if f.placed and p['kind'] == 'np_thru_hole' and p['drill'] and near(pad_box(p)):
                    sh = ('disk', p['x'], p['y'], p['drill'] / 2)
                    for ly in layers:
                        paint(self.blk[ly], sh, 0.2 + w / 2 + m)
                    paint(self.vblk, sh, 0.25 + vsz / 2 + m)
        for net2, ly, ring, sw, filled in b.cushapes:   # copper drawn as graphics
            if (net2 == net and net) or not near(bbox(ring)):
                continue
            c = clr(net, net2) + extra_clr
            edges = list(zip(ring, ring[1:]))           # closed shapes repeat their first point
            for ras, R in ((self.blk.get(ly), c + w / 2 + m), (self.vblk, c + vsz / 2 + m)):
                if ras is None:
                    continue
                for u, v in edges:
                    paint(ras, ('cap', u, v, sw / 2), R)
                if filled:
                    self.fill_rows(PolyIndex(ring), ras)
        for f, p in pads:
            bb = pad_box(p)
            if not near(bb):
                continue
            lys = set(cu) if p['drill'] else cu_layers(p['layers'], cu)
            if p['drill']:
                holes.append((p['x'], p['y'], p['drill'] / 2))
            sh = _pad_shape(p, bb)
            same = p['net'] == net and net
            for ly in lys:
                if ly in self.blk and not same:
                    paint(self.blk[ly], sh, clr(net, p['net'], 'track', 'pad', ly) + extra_clr + w / 2 + m)
            if same:                                     # no via in its own SMD pad
                if not p['drill'] and ownpads:           # (`stitch --in-pad` judges big pads itself)
                    paint(self.vblk, sh, vsz / 2 + m)
            else:
                c = max(clr(net, p['net'], 'via', 'pad', ly) for ly in lys) if lys else clr(net, p['net'])
                paint(self.vblk, sh, c + extra_clr + vsz / 2 + m)
        for t in b.tracks:
            hw = t['w'] / 2
            bb = (min(t['a'][0], t['b'][0]) - hw, min(t['a'][1], t['b'][1]) - hw,
                  max(t['a'][0], t['b'][0]) + hw, max(t['a'][1], t['b'][1]) + hw)
            if (t['net'] == net and net) or not near(bb):
                continue
            sh = ('cap', t['a'], t['b'], hw)
            B, VB = (self.sblk, self.svblk) if t.get('rid') in soft else (self.blk, self.vblk)
            if t['layer'] in B:
                paint(B[t['layer']], sh, clr(net, t['net'], 'track', 'track', t['layer']) + extra_clr + w / 2 + m)
            paint(VB, sh, clr(net, t['net'], 'via', 'track', t['layer']) + extra_clr + vsz / 2 + m)
        for v in b.vias:
            r0 = v['size'] / 2
            bb = (v['x'] - r0, v['y'] - r0, v['x'] + r0, v['y'] + r0)
            if not near(bb):
                continue
            B, VB = (self.sblk, self.svblk) if v.get('rid') in soft else (self.blk, self.vblk)
            holes.append((v['x'], v['y'], v['drill'] / 2, VB))
            if v['net'] == net and net:
                continue
            sh = ('disk', v['x'], v['y'], r0)
            vl = via_layers(v, cu)
            for ly in vl:
                if ly in B:
                    paint(B[ly], sh, clr(net, v['net'], 'track', 'via', ly) + extra_clr + w / 2 + m)
            paint(VB, sh, max(clr(net, v['net'], 'via', 'via', ly) for ly in vl) + extra_clr + vsz / 2 + m)
        for hx_, hy_, hr, *VB in holes:                      # hole to hole, any net
            paint(VB[0] if VB else self.vblk, ('disk', hx_, hy_, hr), vdr / 2 + 0.25 + m)
        # board edge (as capsule obstacles) and outside the outline (scanline)
        ec = b.edge_clearance()
        for u, v in b.edge_segs:
            if near(bbox([u, v])):
                for ly in layers:
                    paint(self.blk[ly], ('cap', u, v, 0.0), ec + w / 2 + m)
                paint(self.vblk, ('cap', u, v, 0.0), ec + vsz / 2 + m)
        if b.rings:
            ring = PolyIndex([q for r in b.rings for q in r[:-1]]) if len(b.rings) == 1 else None
            for j in range(self.H):
                y = self.y0 + j * res
                ins = self.spans(ring, y) if ring else None
                for i in range(self.W):
                    x = self.x0 + i * res
                    if (not any(a0 <= x <= a1 for a0, a1 in ins)) if ring is not None else not inside((x, y), b.rings):
                        k = j * self.W + i
                        for ly in layers:
                            self.blk[ly][k] = 1
                        self.vblk[k] = 1
        # keepouts (rule areas): tracks / vias not allowed
        for lys, idx, novia, _nopour, notrk in _kos(b):
            if not near(idx.bbox) or not (notrk or novia):  # copper may not even touch the area
                continue
            edges = list(zip(idx.pts, idx.pts[1:] + idx.pts[:1]))
            for ras, R in ([(self.blk[ly], w / 2 + m) for ly in lys & set(layers)] if notrk else []) + \
                          ([(self.vblk, vsz / 2 + m)] if novia else []):
                self.fill_rows(idx, ras)
                for u, v in edges:
                    paint(ras, ('cap', u, v, 0.0), R)
        # foreign pours: a PLANE (GND, or a net filling > 20% of the board on that layer)
        # is passable at a cost - a route cuts it and the refill flows round; any other
        # pour is a power island and counts as copper, or a route splits it in two
        planes = _planes(b)
        for fl in b.fills:
            if (fl['net'] == net and net) or not near(fl['bbox']):
                continue
            if (fl['net'], fl['layer']) in planes or (cut and fl['layer'] in self.pour):
                if fl['layer'] in self.pour:                # --cut-islands: an island costs 4x more
                    self.fill_rows(_pidx(b, fl), self.pour[fl['layer']], 1 if (fl['net'], fl['layer']) in planes else 4)
                continue
            c = clr(net, fl['net'], 'track', 'zone', fl['layer']) + extra_clr
            ring = fl['pts']
            if fl['layer'] in self.blk:                     # a track would split the island...
                self.fill_rows(_pidx(b, fl), self.blk[fl['layer']])
                for u, v in zip(ring, ring[1:] + ring[:1]):
                    if near((min(u[0], v[0]), min(u[1], v[1]), max(u[0], v[0]), max(u[1], v[1]))):
                        paint(self.blk[fl['layer']], ('cap', u, v, 0.0), c + w / 2 + m)
            self.fill_rows(_pidx(b, fl), self.vpour)        # ...a via only punches a hole: a cost

    @staticmethod
    def spans(idx, y):
        """[(x0, x1)] where a horizontal line at y is inside the PolyIndex ring (even-odd)"""
        xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                    for x1, y1, x2, y2 in idx.rows.get(math.floor(y / idx.cell), ())
                    if (y1 > y) != (y2 > y))
        return list(zip(xs[::2], xs[1::2]))

    def fill_rows(self, idx, ras, val=1, keep=None):
        """mark every grid point inside the ring, row by row; `keep` (a raster) must be 0"""
        res, fb = self.res, idx.bbox
        out = []
        for j in range(max(0, int((fb[1] - self.y0) / res)), min(self.H, int((fb[3] - self.y0) / res) + 2)):
            for a0, a1 in self.spans(idx, self.y0 + j * res):
                for i in range(max(0, math.ceil((a0 - self.x0) / res)), min(self.W - 1, int((a1 - self.x0) / res)) + 1):
                    k = j * self.W + i
                    if ras is not None:
                        ras[k] = val
                    elif not keep[k]:
                        out.append(k)
        return out

    def xy(self, k):
        return self.x0 + (k % self.W) * self.res, self.y0 + (k // self.W) * self.res

    def cells_of(self, q):
        """({layer: set(cell)} where a track centre may end on copper of piece q, {(layer,
        cell): cost}) - the cost is 2x the distance to the anchor that end must stub to
        (a pad/via centre, a track's centreline; 0 in a pour), so ends prefer anchors"""
        out, cost, res = defaultdict(set), {}, self.res
        x1, y1 = self.x0 + (self.W - 1) * res, self.y0 + (self.H - 1) * res
        def mark(ly, bb, ok, ac):
            if ly not in self.blk or bb[2] < self.x0 or bb[0] > x1 or bb[3] < self.y0 or bb[1] > y1:
                return
            for j in range(max(0, int((bb[1] - self.y0) / res)), min(self.H, int((bb[3] - self.y0) / res) + 2)):
                for i in range(max(0, int((bb[0] - self.x0) / res)), min(self.W, int((bb[2] - self.x0) / res) + 2)):
                    k = j * self.W + i
                    x, y = self.xy(k)
                    if not self.blk[ly][k] and ok(x, y):
                        out[ly].add(k)
                        c = 2 * ac(x, y)
                        if c < cost.get((ly, k), 1e18):
                            cost[(ly, k)] = c
        cu = self.b.copper
        for f, p in q['pads']:
            hx, hy = p['sx'] / 2, p['sy'] / 2
            lys = set(cu) if p['drill'] else cu_layers(p['layers'], cu)
            for ly in lys:
                mark(ly, pad_box(p), lambda x, y, p=p, hx=hx, hy=hy:
                     (lambda u, v: abs(u) <= hx and abs(v) <= hy)(*_local(p, (x, y))),
                     lambda x, y, p=p: math.hypot(x - p['x'], y - p['y']))
        for v in q['vias']:
            r0 = v['size'] / 2
            for ly in via_layers(v, cu):
                mark(ly, (v['x'] - r0, v['y'] - r0, v['x'] + r0, v['y'] + r0),
                     lambda x, y, v=v, r0=r0: math.hypot(x - v['x'], y - v['y']) <= r0,
                     lambda x, y, v=v: math.hypot(x - v['x'], y - v['y']))
        for t in q['tracks']:
            hw = t['w'] / 2
            mark(t['layer'], (min(t['a'][0], t['b'][0]) - hw, min(t['a'][1], t['b'][1]) - hw,
                              max(t['a'][0], t['b'][0]) + hw, max(t['a'][1], t['b'][1]) + hw),
                 lambda x, y, t=t, hw=hw: pt_seg_dist((x, y), t['a'], t['b']) <= hw - 0.01,
                 lambda x, y, t=t: pt_seg_dist((x, y), t['a'], t['b']))
        for fl in q['fills']:
            fb = fl['bbox']
            if fl['layer'] in self.blk and fb[2] >= self.x0 and fb[0] <= x1 and fb[3] >= self.y0 and fb[1] <= y1:
                for k in self.fill_rows(_pidx(self.b, fl), None, keep=self.blk[fl['layer']]):
                    out[fl['layer']].add(k)
                    cost[(fl['layer'], k)] = 0.0
        return out, cost


def astar(g, src, dst, lcost, via_cost, turn, pour_k, c0=None, c1=None, rip=0.0):
    """multi-source A* over (layer, cell); returns [(layer, cell)] or None. c0/c1: extra
    cost of starting / ending at a cell (its distance to the anchor it must stub to),
    the end through a virtual sink so the cheapest finish wins, not the first reached.
    rip: cost per mm through g.sblk (and per via in g.svblk), a route this pass may rip"""
    W, res = g.W, g.res
    L = g.layers
    goal = {(ly, k) for ly, ks in dst.items() for k in ks}
    if not goal:
        return None
    gx = [k % W for _ly, k in goal]
    gy = [k // W for _ly, k in goal]
    bx0, bx1, by0, by1 = min(gx), max(gx), min(gy), max(gy)
    lmin = min(lcost.get(ly, 1.0) for ly in L)

    def h(k):
        x, y = k % W, k // W
        dx = max(bx0 - x, 0, x - bx1)
        dy = max(by0 - y, 0, y - by1)
        return res * lmin * (max(dx, dy) + (SQ2 - 1) * min(dx, dy))

    best, came, heap = {}, {}, []
    c0, c1 = c0 or {}, c1 or {}
    for ly, ks in src.items():
        for k in ks:
            c = c0.get((ly, k), 0.0)
            best[(ly, k)] = c
            came[(ly, k)] = None
            heapq.heappush(heap, (c + h(k), c, ly, k, -1))
    while heap:
        f, gc, ly, k, dr = heapq.heappop(heap)
        if ly == '':                                    # the sink: k is the goal cell it came from
            path, s = [], k
            while s is not None:
                path.append(s)
                s = came[s][0] if came[s] else None
            return path[::-1]
        if gc > best.get((ly, k), 1e18) + 1e-9:
            continue
        if (ly, k) in goal:
            heapq.heappush(heap, (gc + c1.get((ly, k), 0.0), gc + c1.get((ly, k), 0.0), '', (ly, k), -1))
            continue
        x, y = k % W, k // W
        blk, pr, lc = g.blk[ly], g.pour[ly], lcost.get(ly, 1.0)
        sb = g.sblk[ly] if g.sblk else None
        fo = g.follow.get(ly) if g.follow else None     # 0.05 mm units of |distance - pitch|
        for di, (dx, dy) in enumerate(DIRS):
            nx, ny = x + dx, y + dy
            if not (0 <= nx < W and 0 <= ny < g.H):
                continue
            nk = ny * W + nx
            if blk[nk]:
                continue
            step = res * (SQ2 if dx and dy else 1.0)
            c = gc + step * lc * (1 + pour_k * pr[nk]) + (turn * res if dr >= 0 and di != dr else 0)   # pr: 0/1/4
            if sb and sb[nk]:
                c += rip * step
            if fo is not None:
                c += step * g.follow_k * fo[nk] * 0.05
            if c < best.get((ly, nk), 1e18) - 1e-9:
                best[(ly, nk)] = c
                came[(ly, nk)] = ((ly, k), di)
                heapq.heappush(heap, (c + h(nk), c, ly, nk, di))
        if not g.vblk[k]:
            for l2 in L:
                if l2 != ly and not g.blk[l2][k]:
                    c = gc + via_cost * (2 if g.vpour[k] else 1) + (rip if g.svblk and g.svblk[k] else 0)
                    if c < best.get((l2, k), 1e18) - 1e-9:
                        best[(l2, k)] = c
                        came[(l2, k)] = ((ly, k), -1)
                        heapq.heappush(heap, (c + h(k), c, l2, k, -1))
    g.seen = best                                       # what a failure could reach: `_blockers` walls it
    return None


def _follow(g, segs, pitch, tol=0.02):
    """`route --pair`: per layer, how far each grid point sits from `pitch` away from the
    partner leg's tracks on that layer (segs [(layer, a, b)]), in 0.05 mm units capped at
    255 - astar charges it per mm, so the second leg runs alongside the first"""
    out = {}
    for ly in g.layers:
        ss = [(p0, p1) for l, p0, p1 in segs if l == ly]
        f = bytearray(b'\xff' * (g.W * g.H))
        if ss:
            bb = grow(bbox([q for s_ in ss for q in s_]), 3 * pitch)
            for j in range(max(0, int((bb[1] - g.y0) / g.res)), min(g.H, int((bb[3] - g.y0) / g.res) + 2)):
                y = g.y0 + j * g.res
                for i in range(max(0, int((bb[0] - g.x0) / g.res)), min(g.W, int((bb[2] - g.x0) / g.res) + 2)):
                    x = g.x0 + i * g.res
                    d = min(pt_seg_dist((x, y), p0, p1) for p0, p1 in ss)
                    f[j * g.W + i] = min(255, int(max(0.0, abs(d - pitch) - tol) / 0.05))
        out[ly] = f
    return out


def _escape(gn, gw, starts, c0, maxlen, goal=frozenset()):
    """neck-down: from `starts` ({layer: cells} on the pad, gn = the narrow grid) walk the
    narrow free cells of the same layer, at most `maxlen` mm, to the first cells where the
    wide grid gw is free too. ({(layer, cell): cost}, parent map, goal cell or None) - the
    cost is the start's anchor cost + 2x the neck length, so a short neck wins; reaching a
    `goal` cell (the far copper itself) ends it: the whole link is one short neck"""
    W, res = gn.W, gn.res
    dist, par, heap, out = {}, {}, [], {}
    for ly, ks in starts.items():
        for k in ks:
            dist[(ly, k)], par[(ly, k)] = 0.0, None
            heapq.heappush(heap, (0.0, ly, k))
    while heap:
        L, ly, k = heapq.heappop(heap)
        if L > dist[(ly, k)] + 1e-9:
            continue
        if (ly, k) in goal and par[(ly, k)] is not None:
            return out, par, (ly, k)
        if not gw.blk[ly][k]:
            st = k
            while par[(ly, st)] is not None:
                st = par[(ly, st)][1]
            out[(ly, k)] = c0.get((ly, st), 0.0) + 2 * L
            continue
        x, y = k % W, k // W
        for dx, dy in DIRS:
            nx, ny = x + dx, y + dy
            if not (0 <= nx < W and 0 <= ny < gn.H):
                continue
            nk = ny * W + nx
            nL = L + res * (SQ2 if dx and dy else 1.0)
            if gn.blk[ly][nk] or nL > maxlen or nL >= dist.get((ly, nk), 1e18) - 1e-9:
                continue
            dist[(ly, nk)], par[(ly, nk)] = nL, (ly, k)
            heapq.heappush(heap, (nL, ly, nk))
    return out, par, None


def _walk(par, s):
    """the neck from its start on the pad to cell s, [(layer, cell)]"""
    out = [s]
    while par[out[-1]] is not None:
        out.append(par[out[-1]])
    return out[::-1]


def _blockers(b, net, g, cells, n=5):
    """the foreign items whose clearance-grown copper covers `cells` [(layer, cell)]: what
    boxes a pin in. [(label, cells it covers)], most first"""
    from collections import Counter
    clr = _clearance_fn(b)[0]
    cu = b.copper
    pts = [(ly, g.xy(k)) for ly, k in cells]
    if not pts:
        return []
    bb = grow(bbox([p for _ly, p in pts]), 1.5)
    near = lambda q: bb[0] <= q[0] <= bb[2] and bb[1] <= q[1] <= bb[3]   # noqa: E731
    cand = []
    for f in b.placed():
        for p in f.pads:
            if p['net'] != net and p['kind'] != 'np_thru_hole' and near((p['x'], p['y'])):
                lys = set(cu) if p['drill'] else cu_layers(p['layers'], cu)
                sx, sy = p['sx'] / 2, p['sy'] / 2
                cand.append((f"{f.ref}.{p['num']} pad ({unesc_disp(p['net']) or 'no net'})", lys, p['net'],
                             lambda q, p=p, sx=sx, sy=sy: (lambda u, v: math.hypot(max(abs(u) - sx, 0), max(abs(v) - sy, 0)))(*_local(p, q))))
    for t in b.tracks:
        if t['net'] != net and (near(t['a']) or near(t['b']) or near(t['mid'])):
            cand.append((f"{unesc_disp(t['net']) or 'netless'} track on {t['layer']}", {t['layer']}, t['net'],
                         lambda q, t=t: pt_seg_dist(q, t['a'], t['b']) - t['w'] / 2))
    for v in b.vias:
        if v['net'] != net and near((v['x'], v['y'])):
            cand.append((f"{unesc_disp(v['net']) or 'netless'} via @{v['x']:.2f},{v['y']:.2f}", set(cu), v['net'],
                         lambda q, v=v: math.dist(q, (v['x'], v['y'])) - v['size'] / 2))
    ec = b.edge_clearance()
    c = Counter()
    for ly, q in pts[::max(1, len(pts) // 600)]:
        best = min(((d(q) - clr(net, n2), lab) for lab, lys, n2, d in cand if ly in lys), default=(9e9, ''))
        de = min((pt_seg_dist(q, u, v) - ec for u, v in b.edge_segs), default=9e9)
        if de <= g.w / 2 + 0.05 and de < best[0]:
            c['the board edge'] += 1
        else:
            c[best[1] if best[0] <= g.w / 2 + 0.05 else 'a rule area or a power-island pour'] += 1
    # copper first: an edge or a rule area bounds every search and says little about the plug
    return sorted(c.items(), key=lambda kv: (kv[0] in ('the board edge', 'a rule area or a power-island pour'), -kv[1]))[:n]


def to_copper(g, path):
    """grid path -> ([(layer, (x, y), (x, y))] segments, [(x, y)] vias), collinear runs merged"""
    segs, vias = [], []
    run = [path[0]]
    for s in path[1:] + [None]:
        if s is not None and s[0] == run[-1][0]:
            run.append(s)
            continue
        pts = [g.xy(k) for _ly, k in run]
        ly = run[0][0]
        keep = [pts[0]]
        for i in range(1, len(pts) - 1):
            (ax, ay), (bx, by), (cx, cy) = keep[-1], pts[i], pts[i + 1]
            if abs((bx - ax) * (cy - by) - (by - ay) * (cx - bx)) > 1e-9:
                keep.append(pts[i])
        if len(pts) > 1:
            keep.append(pts[-1])
        segs += [(ly, keep[i], keep[i + 1]) for i in range(len(keep) - 1)]
        if s is not None:
            vias.append(g.xy(s[1]))
            run = [s]
    return segs, vias


def _anchor(pt, ly, q, w, cu):
    """where a path end inside piece q's copper must land for KiCad to join them (an
    anchor: a pad or via centre, or a point on a track's centreline), and the widest
    stub that stays inside that copper; None when the end may stay (a pour)"""
    for f, p in q['pads']:
        lys = set(cu) if p['drill'] else cu_layers(p['layers'], cu)
        u, v = _local(p, pt)
        if ly in lys and abs(u) <= p['sx'] / 2 + 1e-6 and abs(v) <= p['sy'] / 2 + 1e-6:
            return (p['x'], p['y']), min(w, p['sx'], p['sy'])
    for v in q['vias']:
        if ly in via_layers(v, cu) and math.dist(pt, (v['x'], v['y'])) <= v['size'] / 2:
            return (v['x'], v['y']), min(w, v['size'])
    for t in q['tracks']:
        if t['layer'] == ly and pt_seg_dist(pt, t['a'], t['b']) <= t['w'] / 2 + 1e-6:
            dx, dy = t['b'][0] - t['a'][0], t['b'][1] - t['a'][1]
            L2 = dx * dx + dy * dy
            s_ = max(0.0, min(1.0, ((pt[0] - t['a'][0]) * dx + (pt[1] - t['a'][1]) * dy) / L2)) if L2 else 0.0
            return (t['a'][0] + s_ * dx, t['a'][1] + s_ * dy), min(w, t['w'])
    return None


def _ends(segs, g, path, src_q, dst_qs, w, cu):
    """segments with a stub from each path end to its anchor (a zero-length path, where
    the two pieces' copper already overlaps without KiCad joining them, becomes the
    two stubs alone)"""
    (l0, k0), (l1, k1) = path[0], path[-1]
    p0, p1 = g.xy(k0), g.xy(k1)
    pre, post = [], []
    a = _anchor(p0, l0, src_q, w, cu)
    if a and math.dist(a[0], p0) > 1e-6:
        pre = [(l0, a[0], p0, a[1])]
    dq = {'pads': [x for q in dst_qs for x in q['pads']], 'vias': [x for q in dst_qs for x in q['vias']],
          'tracks': [x for q in dst_qs for x in q['tracks']]}
    a = _anchor(p1, l1, dq, w, cu)
    if a and math.dist(a[0], p1) > 1e-6:
        post = [(l1, p1, a[0], a[1])]
    return pre + [(ly, u, v, w) for ly, u, v in segs] + post


def _blob(segs, hw, res=0.1):
    """outline [(x, y)] of the band within hw of segs [(p0, p1)]: the outer boundary of the
    union of res-squares, its staircases fitted with straight edges (a zone outline for `--pour`)"""
    xs, ys = [p[0] for s in segs for p in s], [p[1] for s in segs for p in s]
    x0, y0 = min(xs) - hw - 2 * res, min(ys) - hw - 2 * res
    W, H = int((max(xs) + hw - x0) / res) + 3, int((max(ys) + hw - y0) / res) + 3
    on = bytearray(W * H)
    for p0, p1 in segs:
        y_lo, y_hi, f = _iv(('cap', p0, p1, 0.0), hw)
        for j in range(max(0, math.ceil((y_lo - y0) / res)), min(H - 1, math.floor((y_hi - y0) / res)) + 1):
            iv = f(y0 + j * res)
            if iv:
                i0, i1 = max(0, math.ceil((iv[0] - x0) / res)), min(W - 1, math.floor((iv[1] - x0) / res))
                on[j * W + i0:j * W + i1 + 1] = ONES[:max(0, i1 - i0 + 1)]
    L = max(_trace(on, W, H), key=_area)
    return [(round(x0 + (ci - 0.5) * res, 4), round(y0 + (cj - 0.5) * res, 4)) for ci, cj in L]


def _area(L):
    return abs(sum(L[k - 1][0] * L[k][1] - L[k][0] * L[k - 1][1] for k in range(len(L)))) / 2


def _trace(on, W, H):
    """boundary loops of the set cells of a W x H raster, in cell-corner coordinates (corner
    (i, j) is the top-left of cell (i, j)), the set side on the left, corners where the edge
    does not turn dropped and staircases fitted (`simplify`, 1.25 cells). A hole gives a loop
    of its own (`fatten` slits holes open first)"""
    full = lambda i, j: 0 <= i < W and 0 <= j < H and on[j * W + i]   # noqa: E731
    nxt = {}                                         # corner -> next corner, the band on the left
    for j in range(H):
        for i in range(W):
            if on[j * W + i]:
                for (di, dj), a, b in (((0, -1), (i + 1, j), (i, j)), ((0, 1), (i, j + 1), (i + 1, j + 1)),
                                       ((-1, 0), (i, j), (i, j + 1)), ((1, 0), (i + 1, j + 1), (i + 1, j))):
                    if not full(i + di, j + dj):
                        nxt.setdefault(a, []).append(b)
    loops = []
    while nxt:
        s = next(iter(nxt))
        loop, c = [s], s
        while True:
            c2 = nxt[c].pop()
            if not nxt[c]:
                del nxt[c]
            if c2 == s:
                break
            loop.append(c2)
            c = c2
        loops.append(simplify([loop[k] for k in range(len(loop))       # a corner where the edge turns
                               if (loop[k][0] - loop[k - 1][0], loop[k][1] - loop[k - 1][1]) !=
                               (loop[(k + 1) % len(loop)][0] - loop[k][0], loop[(k + 1) % len(loop)][1] - loop[k][1])],
                              1.25))
    return loops


def simplify(L, eps):
    """a closed ring with every corner within eps of the edge that replaces it dropped
    (Douglas-Peucker): a raster staircase (1.25 cells holds any slope) becomes the straight
    or diagonal edge it stands for, an arc a few chords. A ring the cut would make cross
    itself tries eps/2, then stays as it was"""
    n = len(L)
    if n < 5:
        return L
    for e in (eps, eps / 2):
        far = max(range(n), key=lambda k: math.dist(L[k], L[0]))
        keep, stack = {0, far}, [(0, far), (far, n)]
        while stack:
            i, j = stack.pop()
            a, b = L[i % n], L[j % n]
            k, d = max(((k, pt_seg_dist(L[k], a, b)) for k in range(i + 1, j)), key=lambda kd: kd[1], default=(0, 0))
            if d > e:
                keep.add(k)
                stack += [(i, k), (k, j)]
        P = [L[k] for k in sorted(keep)]
        E = list(zip(P, P[1:] + P[:1]))
        if len(P) >= 3 and not any(_segd(*E[i], *E[j]) < 1e-9 for i in range(len(E))
                                   for j in range(i + 2, len(E) - (i == 0))):
            return P
    return L


def _runs(segs):
    """[(layer, p0, p1, w)] in path order -> its same-layer runs"""
    runs = [[s] for s in segs[:1]]
    for s in segs[1:]:
        (runs[-1].append(s) if s[0] == runs[-1][-1][0] else runs.append([s]))
    return runs


def _victims(b, net, segs, vias, vsz, vdr, laid, soft):
    """rids in `soft` whose copper the new route (segs, through vias) comes within the
    net-pair clearance of (+5 um, the grid's own margin), hole to hole 0.25 for vias"""
    clr, out = _clearance_fn(b)[0], set()
    for r in soft:
        m = laid[r]
        c = clr(net, m['net']) + 0.005
        for o in m['items']:
            if 'layer' in o:                                 # a track
                hit = any(ly == o['layer'] and _segd(p0, p1, o['a'], o['b']) < c + (sw + o['w']) / 2
                          for ly, p0, p1, sw in segs) or \
                      any(pt_seg_dist(v, o['a'], o['b']) < c + (vsz + o['w']) / 2 for v in vias)
            else:                                            # a through via
                q = (o['x'], o['y'])
                hit = any(pt_seg_dist(q, p0, p1) < c + (sw + o['size']) / 2 for _ly, p0, p1, sw in segs) or \
                      any(math.dist(v, q) < max(c + (vsz + o['size']) / 2, 0.25 + (vdr + o['drill']) / 2) for v in vias)
            if hit:
                out.add(r)
                break
    return out


def route_link(b, net, src_q, dst_qs, a, widths, vsz, vdr, layers, lcost, soft=frozenset(), follow=None):
    """one A* from piece src_q to any of dst_qs: per window (widened on failure) each
    width in turn, so a too-wide try only costs the small window. `soft`: rids of this
    pass's routes it may cross at --rip-cost (the caller rips what it crossed)"""
    sp = [(p[0], p[1]) for p in _points(src_q, b.copper)]
    dp = [(p[0], p[1]) for q in dst_qs for p in _points(q, b.copper)]
    s0, d0 = min(((s, d) for s in sp for d in dp), key=lambda sd: math.dist(*sd)) if len(sp) * len(dp) < 4e6 \
        else (sp[0], min(dp, key=lambda d: math.dist(d, sp[0])))
    o, covered, last = b.outline, True, None
    for margin in (a.margin, a.margin * 2.5, a.margin * 6):
      for w in widths:
        box = bbox([s0, d0])
        box = (max(o[0], box[0] - margin), max(o[1], box[1] - margin),
               min(o[2], box[2] + margin), min(o[3], box[3] + margin))
        box = (s0[0] - a.grid * math.floor((s0[0] - box[0]) / a.grid),       # a grid point on the
               s0[1] - a.grid * math.floor((s0[1] - box[1]) / a.grid), box[2], box[3])  # source pad
        g = Grid(b, net, box, a.grid, w, vsz, vdr, layers, cut=a.cut_islands, soft=soft)
        if follow:
            g.follow = _follow(g, *follow)
        src, c0 = g.cells_of(src_q)
        dst, c1 = defaultdict(set), {}
        for q in dst_qs:
            cells, cc = g.cells_of(q)
            for ly, ks in cells.items():
                dst[ly] |= ks
            for k, v in cc.items():
                c1[k] = min(v, c1.get(k, 1e18))
        if not any(src.values()):
            last = (g, w)
            continue
        covered = False
        path = astar(g, src, dst, lcost, a.via_cost, a.turn, a.pour_cost, c0, c1, a.rip_cost if soft else 0.0)
        if path:
            segs, vias = to_copper(g, path)
            segs = _ends(segs, g, path, src_q, dst_qs, w, b.copper)
            if not segs and not vias:
                continue
            return (segs, vias, margin, w), ''
        last = (g, w)
    # neck-down: a fine-pitch pad no class-width track can leave (a USB-C VBUS pin, a WSON
    # TVS) leaves at a narrow width for at most --neck-len, then widens; same at the far end
    neck_len = getattr(a, 'neck_len', 0.0) or 0.0
    necks = [getattr(a, 'neck', 0.0)] if getattr(a, 'neck', 0.0) else [0.4, 0.3, 0.25, 0.2]
    for w in widths if neck_len > 0 else ():
        for wn in [x for x in necks if x < w - 1e-9]:
            box = bbox([s0, d0])
            box = (max(o[0], box[0] - a.margin), max(o[1], box[1] - a.margin),
                   min(o[2], box[2] + a.margin), min(o[3], box[3] + a.margin))
            box = (s0[0] - a.grid * math.floor((s0[0] - box[0]) / a.grid),
                   s0[1] - a.grid * math.floor((s0[1] - box[1]) / a.grid), box[2], box[3])
            gw = Grid(b, net, box, a.grid, w, vsz, vdr, layers, cut=a.cut_islands, soft=soft)
            gn = Grid(b, net, box, a.grid, wn, vsz, vdr, layers, cut=a.cut_islands, soft=soft)
            srcn, c0n = gn.cells_of(src_q)
            if not any(srcn.values()):
                continue
            dst, c1 = defaultdict(set), {}
            dstn, c1n = defaultdict(set), {}
            for q in dst_qs:
                for G, D, C in ((gw, dst, c1), (gn, dstn, c1n)):
                    cells, cc = G.cells_of(q)
                    for ly, ks in cells.items():
                        D[ly] |= ks
                    for k, v in cc.items():
                        C[k] = min(v, C.get(k, 1e18))
            es, pars, hit = _escape(gn, gw, srcn, c0n, neck_len, {(ly, k) for ly, ks in dstn.items() for k in ks})
            if hit:                                      # the far copper is within one neck
                head = _walk(pars, hit)
                segs = [(ly, u, v, wn) for ly, u, v in to_copper(gn, head)[0]]
                dq = {'pads': [x for q in dst_qs for x in q['pads']], 'vias': [x for q in dst_qs for x in q['vias']],
                      'tracks': [x for q in dst_qs for x in q['tracks']]}
                for at, qq, pos in ((head[0], src_q, 0), (head[-1], dq, len(segs) + 1)):
                    an = _anchor(gn.xy(at[1]), at[0], qq, wn, b.copper)
                    if an and math.dist(an[0], gn.xy(at[1])) > 1e-6:
                        segs.insert(min(pos, len(segs)), (at[0], an[0], gn.xy(at[1]), an[1]) if pos == 0
                                    else (at[0], gn.xy(at[1]), an[0], an[1]))
                if segs:
                    return (segs, [], a.margin, w), ''
            ed, pard, _h = _escape(gn, gw, dstn, c1n, neck_len)
            dstw = {(ly, k) for ly, ks in dst.items() for k in ks}       # reached at full width
            for (ly, k), c in ed.items():
                if c < c1.get((ly, k), 1e18):
                    dst[ly].add(k)
                    c1[(ly, k)] = c
            src = defaultdict(set)
            for (ly, k) in es:
                src[ly].add(k)
            if not any(src.values()) or not any(dst.values()):
                continue
            path = astar(gw, src, dst, lcost, a.via_cost, a.turn, a.pour_cost, es, c1, a.rip_cost if soft else 0.0)
            if not path:
                continue
            head = _walk(pars, path[0])
            tail = _walk(pard, path[-1])[::-1] if path[-1] in ed and path[-1] not in dstw else [path[-1]]
            segs = [(ly, u, v, wn) for ly, u, v in (to_copper(gn, head)[0] if len(head) > 1 else [])]
            msegs, vias = to_copper(gw, path)
            segs += [(ly, u, v, w) for ly, u, v in msegs]
            segs += [(ly, u, v, wn) for ly, u, v in (to_copper(gn, tail)[0] if len(tail) > 1 else [])]
            (l0, k0), (l1, k1) = head[0], tail[-1]
            p0, p1 = gn.xy(k0), gn.xy(k1)
            dq = {'pads': [x for q in dst_qs for x in q['pads']], 'vias': [x for q in dst_qs for x in q['vias']],
                  'tracks': [x for q in dst_qs for x in q['tracks']]}
            pre = _anchor(p0, l0, src_q, wn if len(head) > 1 else w, b.copper)
            post = _anchor(p1, l1, dq, wn if len(tail) > 1 else w, b.copper)
            if pre and math.dist(pre[0], p0) > 1e-6:
                segs.insert(0, (l0, pre[0], p0, pre[1]))
            if post and math.dist(post[0], p1) > 1e-6:
                segs.append((l1, p1, post[0], post[1]))
            return (segs, vias, a.margin, w), ''
    why = ("foreign copper covers every cell of the source pad/copper (a stale track on it?)" if covered else
           f"no path within {a.margin * 6:g} mm of the gap at width {' / '.join(f'{w:g}' for w in widths)}")
    if last:                                             # name what boxes the pin in
        g, w = last
        if covered:
            cells = [(ly, k) for f, p in src_q['pads']
                     for ly in (set(b.copper) if p['drill'] else cu_layers(p['layers'], b.copper)) if ly in g.blk
                     for k in _pad_cells(g, p, ly)]
        else:
            cells = sorted({(ly, ny * g.W + nx) for (ly, k) in getattr(g, 'seen', {}) if ly
                            for dx, dy in DIRS for nx, ny in [((k % g.W) + dx, (k // g.W) + dy)]
                            if 0 <= nx < g.W and 0 <= ny < g.H and g.blk[ly][ny * g.W + nx]})
        walls = _blockers(b, net, g, cells)
        if walls:
            why += f"; at {w:g} mm it is {'covered' if covered else 'boxed in'} by " + \
                   ', '.join(f"{lab} ({c})" for lab, c in walls)
    return None, why


def _pad_cells(g, p, ly):
    """grid cells inside pad p (cells_of skips blocked ones; a covered pad has only those)"""
    hx, hy = p['sx'] / 2, p['sy'] / 2
    bb = pad_box(p)
    out = []
    for j in range(max(0, int((bb[1] - g.y0) / g.res)), min(g.H, int((bb[3] - g.y0) / g.res) + 2)):
        for i in range(max(0, int((bb[0] - g.x0) / g.res)), min(g.W, int((bb[2] - g.x0) / g.res) + 2)):
            x, y = g.x0 + i * g.res, g.y0 + j * g.res
            u, v = _local(p, (x, y))
            if abs(u) <= hx and abs(v) <= hy:
                out.append(j * g.W + i)
    return out


def rf_ref_layers(b):
    """inner layers right under an outer layer's RF-netclass traces: their reference plane"""
    rfl = {t['layer'] for t in b.tracks if len(b.copper) > 2 and t['layer'] in b.outer
           and 'RF' in b.netclass(t['net'] or '').upper()}
    return {b.copper[1] if ly == b.copper[0] else b.copper[-2] for ly in rfl} - b.outer


def c_route(b, a):
    if a.pending:                                   # every net airwires lists, shortest first
        gaps = []
        for n in sorted({p['net'] for f in b.placed() for p in f.pads if p['net']
                         and not p['net'].startswith('unconnected-')}):
            Q, L = links(b, n)
            if L:
                gaps.append((sum(d for d, *_x in L), n))
        a.args = [n for _g, n in sorted(gaps)]
        print(f"route --pending: {len(a.args)} net(s) with airwires")
    if not a.args:
        print("route NET [NET...] | --pending  [--width W] [--layers F.Cu,B.Cu] [-o OUT]"); return 1
    from kpcb_board import _resolve_net
    for x0, y0, x1, y1 in (tuple(float(v) for v in x.split(',')) for x in (a.avoid or '').split(';') if x.strip()):
        _kos(b).append((set(b.copper), PolyIndex([(x0, y0), (x1, y0), (x1, y1), (x0, y1)]), True, False, True))
        print(f"(--avoid {x0:g},{y0:g},{x1:g},{y1:g}: no track or via of this run there, every layer)")
    layers = [l for l in (a.layers.split(',') if a.layers else b.copper) if l in b.copper]
    lcost = {ly: (1.0 if ly in b.outer else a.inner_cost) for ly in b.copper}
    for ly in rf_ref_layers(b):          # the RF traces' return path: cutting it costs more
        lcost[ly] *= a.ref_cost
        print(f"({ly}: the RF traces' reference plane, cost x{lcost[ly]:g} vs inner x{a.inner_cost:g})")
    t = PcbText(b.path) if a.out else None
    prio = 1 + max(map(int, re.findall(r'\(priority (\d+)\)', t.txt if t else open(b.path).read())),
                   default=0) if a.pour else 0             # a pour band fills ahead of every zone
    key = lambda q: ' '.join(f"{f.ref}.{p['num']}" for f, p in q['pads'][:3])   # noqa: E731
    laid, rids, torn = {}, itertools.count(1), {}  # this run's links by rid: what a rip-up may undo
    if a.rip_board:                                 # --rip-board N: small unlocked signal nets already on
        npads = defaultdict(int)                    # the board are rip-up units too (a whole net at a time)
        for f in b.placed():
            for p in f.pads:
                npads[p['net']] += 1
        filled = {fl['net'] for fl in b.fills}
        units = defaultdict(list)
        for x in b.tracks + b.vias:
            units[x['net']].append(x)
        for n, items in units.items():
            nc = (b.netclass(n) or 'Default').upper()
            if not n or n == 'GND' or n in filled or not 2 <= npads[n] <= a.rip_board or any(x.get('locked') for x in items) \
                    or any(k in nc for k in ('PWR', 'RF', 'USB', 'DIFF')):
                continue
            r = next(rids)
            for x in items:
                x['rid'] = r
            laid[r] = {'net': n, 'items': items, 'edits': [], 'board': True, 'nseg': 0, 'nzone': 0, 'nvia': 0, 'len': 0.0}
        print(f"--rip-board {a.rip_board}: {sum(1 for m in laid.values() if m.get('board'))} small unlocked signal "
              f"net(s) on the board may be ripped and re-routed (all of a net's tracks and vias at once)")

    def settings(net):
        """(netclass dict, widths to try, via size, via drill)"""
        nc, cls = b.netclass(net) or 'Default', _netclass(b, net)
        widths = [a.width] if a.width else sorted({x for x in (cls.get('track_width'), 0.2, 0.15) if x},
                                                  reverse=True)
        if not a.width and nc == 'PWR_HIGH':                # the .kicad_dru floor is 1.0
            widths = [cls.get('track_width') or 1.5, 1.0]
        elif not a.width and nc == 'PWR_RAIL':
            widths = [cls.get('track_width') or 0.6, 0.4, 0.3]
        vsz, vdr = class_via(cls)
        if a.via:
            vsz, vdr = (float(x) for x in a.via.split('/'))
        return cls, widths, vsz, vdr

    def relay(r, m, edits=True):
        laid[r] = m
        for x in m['items']:
            (b.tracks if 'layer' in x else b.vias).append(x)
        if t and edits:
            t.edits.extend(m['edits'])
        for e in m.pop('dels', []):                     # a board unit put back: undo its deletion
            t.edits.remove(e)
        torn.pop(r, None)

    def lay(net, res, band, cls, vsz, vdr):
        """a link into the model (so the next one sees it) and the file; returns its rid"""
        segs, vias, _m, _w = res
        r, runs = next(rids), _runs(segs)
        m = {'net': net, 'items': [], 'edits': [], 'nseg': 0 if band else len(segs),
             'nzone': len(runs) if band else 0, 'nvia': len(vias),
             'len': 0.0 if band else sum(math.dist(p0, p1) for _l, p0, p1, _w in segs)}
        for ly, p0, p1, sw in segs:
            m['items'].append({'net': net, 'layer': ly, 'w': sw, 'a': p0, 'b': p1, 'len': math.dist(p0, p1),
                               'mid': ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2), 'uuid': '', 'rid': r})
            if t and not band:                          # a pour's corridor is model only
                m['edits'].append(t.segment(net, ly, sw, p0, p1))
        for x, y in vias:
            m['items'].append({'net': net, 'drill': vdr, 'size': vsz, 'x': x, 'y': y, 'uuid': '', 'rid': r,
                               'layers': [b.copper[0], b.copper[-1]]})
            if t:
                m['edits'].append(t.via(net, x, y, vsz, vdr, [b.copper[0], b.copper[-1]]))
        if band and t:
            m['edits'] += [t.zone(net, run[0][0], _blob([(p0, p1) for _l, p0, p1, _w in run], band / 2),
                                  prio, cls.get('clearance') or 0.2) for run in runs]
        relay(r, m, edits=False)
        return r

    def unlay(r):
        m = laid.pop(r)
        gone = {id(x) for x in m['items']}
        b.tracks[:] = [x for x in b.tracks if id(x) not in gone]
        b.vias[:] = [x for x in b.vias if id(x) not in gone]
        if m.get('board'):                              # copper that was on the board: delete it
            torn[r] = m
            if t:
                m['dels'] = [t.sub(x['uuid'], '') for x in m['items'] if x['uuid'] in t.span] + \
                    tear_edits(t, [x for x in m['items'] if 'layer' in x])
            return m
        for e in m['edits']:
            t.edits.remove(e)
        return m

    def show(net, label, res, tail='', ind=''):
        segs, vias, margin, w = res
        L = sum(math.dist(p0, p1) for _ly, p0, p1, _w in segs)
        print(f"{ind}{unesc_disp(net)}: {label} -> rest   w {w:g}  {L:.2f} mm, {len(vias)} via(s), "
              f"layers {'/'.join(sorted({s[0].split('.')[0] for s in segs}))}  (window +{margin:g} mm){tail}")
        nk = [(math.dist(p0, p1), sw) for _ly, p0, p1, sw in segs if sw < w - 1e-9]
        if nk and sum(x for x, _ in nk) > 0.3:          # pad stubs are narrower by nature; a neck is longer
            whole = all(sw < w - 1e-9 for *_x, sw in segs)
            print(f"{ind}    NECK: {sum(x for x, _ in nk):.2f} mm at {min(sw for _, sw in nk):g} mm "
                  + ("- the whole link, pad to the next copper" if whole else f"out of a fine-pitch pad, then {w:g} mm")
                  + (" (under the PWR_HIGH floor: DRC TRACK_WIDTH flags it)" if (b.netclass(net) or '') == 'PWR_HIGH' else ''))
        if a.verbose:
            for ly, p0, p1, sw in segs:
                print(f"      {ly:<7} {p0[0]:.3f},{p0[1]:.3f} -> {p1[0]:.3f},{p1[1]:.3f}"
                      + (f"  w {sw:g}" if sw != w else ''))
            for x, y in vias:
                print(f"      via     {x:.3f},{y:.3f}")

    def rejoin(net, want):
        """route a ripped net until it is back to `want` pieces: [(rid, label, res)], or None
        (and what it laid is undone)"""
        cls, widths, vsz, vdr = settings(net)
        got = []
        for _try in range(4):
            Q = piece_parts(b, net)
            if len(Q) <= want:
                return got
            s = min(Q[1:], key=lambda q: len(q['pads']))
            rv = route_link(b, net, s, [q for q in Q if q is not s], a, widths, vsz, vdr, layers, lcost)[0]
            if not rv:
                break
            got.append((lay(net, rv, 0.0, cls, vsz, vdr), key(s), rv))
        if len(piece_parts(b, net)) <= want:
            return got
        for r, *_x in got:
            unlay(r)
        return None

    def rip(net, src, others, cls, widths, vsz, vdr):
        """cross this run's links of other nets at --rip-cost, rip the ones the path comes too
        close to and route those nets again at once (no further rips). If one cannot rejoin,
        everything is put back as it was. ((res, [nets], [(rid, net, label, res)]), note)"""
        soft = {r for r, m in laid.items() if m['net'] != net}
        rs = route_link(b, net, src, others, a, widths, vsz, vdr, layers, lcost, soft)[0] if soft else None
        if not rs:
            return None, ''
        hit = _victims(b, net, rs[0], rs[1], vsz, vdr, laid, soft)
        vn = sorted({laid[r]['net'] for r in hit}, key=natkey)
        want = {v: len(piece_parts(b, v)) for v in vn}
        olds = {r: unlay(r) for r in hit}
        mine, redo = lay(net, rs, 0.0, cls, vsz, vdr), []
        for v in vn:
            got = rejoin(v, want[v])
            if got is None:
                for r, *_x in redo:
                    unlay(r)
                unlay(mine)
                for r, m in olds.items():
                    relay(r, m)
                return None, f"; ripping {unesc_disp(v)} did not help, it could not re-route (undone)"
            redo += [(r, v, lb, rv) for r, lb, rv in got]
        return (rs, vn, redo), ''

    rc, rips = 0, 0
    for name in a.args:
        net, cand = _resolve_net(b, name)
        if not net:
            print(f"{name}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); rc = 1; continue
        nc = b.netclass(net) or 'Default'
        if 'RF' in nc.upper() and not a.force:
            print(f"{unesc_disp(net)}: netclass {nc} - hand-route impedance-controlled nets "
                  f"(--force to route it anyway)"); rc = 1; continue
        cls, widths, vsz, vdr = settings(net)
        follow = None
        if a.pair:                                       # the other leg already routed: run alongside it
            mate = next((q[1] if q[0] == net else q[0] for q in _legs(b) if net in q), None)
            msegs = [(t['layer'], t['a'], t['b']) for t in b.tracks if mate and t['net'] == mate]
            if msegs:
                pw = cls.get('diff_pair_width') or cls.get('track_width') or 0.2
                follow = (msegs, pw + (cls.get('diff_pair_gap') or cls.get('clearance') or 0.15))
                widths = [a.width or pw]
                print(f"{unesc_disp(net)}: following {unesc_disp(mate)} at a {follow[1]:g} mm pitch (width {widths[0]:g})")
        done, last, failed = 0, None, set()
        while True:
            Q = piece_parts(b, net)
            todo = [q for q in Q[1:] if key(q) not in failed]
            if not todo:
                break
            if last is not None and len(Q) >= last:
                print(f"{unesc_disp(net)}: a link joined nothing KiCad would join - stopping this net"); rc = 2; break
            src = min(todo, key=lambda q: len(q['pads']))
            others = [q for q in Q if q is not src]
            label = key(src)
            res, why = route_link(b, net, src, others, a, widths, vsz, vdr, layers, lcost, follow=follow)
            ripped, band = None, 0.0
            if not res and rips < a.rips:
                ripped, note = rip(net, src, others, cls, widths, vsz, vdr)
                why += note
            if not res and not ripped and a.pour:       # no track fits: a zone band that the refill fits
                res = route_link(b, net, src, others, a, [a.pour], vsz, vdr, layers, lcost)[0]
                band = max(widths) if res else 0.0
            if not res and not ripped:                  # skip this piece, try the net's others
                print(f"{unesc_disp(net)}: {label} - FAILED: {why}"
                      + ('' if a.pour or a.width else "  (`--pour 0.3`: a zone band through a corridor "
                                                       "a track this wide cannot use)" if max(widths) >= 0.6 else ''))
                rc = 2
                failed.add(label)
                last = None
                continue
            last = len(Q)
            if ripped:                                  # rip() laid it, and the re-routes
                res, vn, redo = ripped
                rips += bool(vn)
                show(net, label, res, f"  - ripped up {', '.join(unesc_disp(v) for v in vn)}, re-routed:" if vn else '')
                for _r, v, lb, rv in redo:
                    show(v, lb, rv, ind='      ')
            else:
                show(net, label, res)
                if band:
                    print(f"    POUR: {len(_runs(res[0]))} zone(s), a {band:g} mm band on "
                          f"{'/'.join(r[0][0] for r in _runs(res[0]))} round a {res[3]:g} mm corridor "
                          f"(priority {prio}) - refill, then `airwires {unesc_disp(net)}` and `ampacity`")
                lay(net, res, band, cls, vsz, vdr)
            done += 1
            if done > 60:
                print(f"{unesc_disp(net)}: stopping after 60 links"); break
        if follow and done:                              # how well it ran alongside
            mate = next((q[1] if q[0] == net else q[0] for q in _legs(b) if net in q), None)
            T0 = [t for t in b.tracks if t['net'] == mate]
            T1 = [t for t in b.tracks if t['net'] == net and t.get('rid')]
            c1, gaps = _coupling(T1, T0, 2 * follow[1])
            L1 = sum(t['len'] for t in T1)
            if L1:
                gs = sorted(gaps)
                print(f"    coupled {c1:.2f} of {L1:.2f} mm to {unesc_disp(mate)}"
                      + (f", edge gap median {gs[len(gs) // 2]:.3f} mm" if gs else '') + "  (`pairs` for the skew)")
    if torn:
        print(f"ripped from the board: {', '.join(sorted({unesc_disp(m['net']) for m in torn.values()}, key=natkey))} "
              f"({sum(len(m['items']) for m in torn.values())} track(s)/via(s), re-routed above)")
    nl, nv, nz = (sum(m[k] for m in laid.values()) for k in ('nseg', 'nvia', 'nzone'))
    what = f"{nl} segment(s), {nv} via(s), {sum(m['len'] for m in laid.values()):.1f} mm" + (f", {nz} zone(s)" if nz else '')
    if t and (nl or nz):
        t.save(a.out)
        print(f"wrote {what} -> {a.out}\n"
              f"  verify: kdrc.py {a.out} --unconnected (refills zones; KiCad's DRC is the verdict)")
    elif nl or nz:
        print(f"(dry run: {what}; add -o OUT to write them)")
    return rc
