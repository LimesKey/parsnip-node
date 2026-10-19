"""kpcb.py `place REF`: where a part fits, ranked by how short its connections get.

Each side is rasterised once (`--step`, 0.1 mm): the courtyards of the placed parts on
it, every through-hole pad (both sides), and everything outside the outline shrunk by
--edge. A summed-area table then answers "does REF's courtyard fit here" for every
grid point, rotation and side in O(boxes): the courtyard as a few boxes (_bands), so
an L-shaped one can wrap round a neighbour. Each fit is scored by ratsnest: per pad, the
distance to the nearest same-net copper already on the board (pads, tracks, vias;
a rail weighs 0.3, it can drop a via into its plane; +1.5 mm when that copper is an
SMD pad on the other side), plus the pull of `--near REF` (its pads on REF's nets) or,
without it, of the part's signal-net neighbours. The best are copper-checked with
movecheck's World (foreign pads/tracks/vias inside the clearance, copper to edge,
courtyard outline overlap), so each line reads CLEAR or names its first hit.
`-o OUT` writes the first clear one through `move` (`--pick N` takes the Nth)."""
import math
from array import array
from collections import defaultdict
from kcommon import unesc_disp, rail_voltage
from kpcb_board import PolyIndex, inside, pad_box, xf
from kpcb_move import World, cu_layers
from kpcb_edit import PcbText, flip_text, place_text, _flipped
from kpcb_route import _iv, _pad_shape

FLIPL = {'F.Cu': 'B.Cu', 'B.Cu': 'F.Cu'}


def as_placed(f, x, y, rot, flip, cu):
    """(pad items, courtyard rings, pad centres) of f at x,y,rot, flipped first if asked:
    a flip mirrors x about the part's own position and makes the rotation 180 - r"""
    r1 = (180 - f.rot) if flip else f.rot
    dr = rot - r1
    def T(px, py):
        if flip:
            px = 2 * f.x - px
        return xf(px - f.x, py - f.y, x, y, dr)
    items, cent = [], []
    for p in f.pads:
        if p['kind'] == 'np_thru_hole':
            continue
        q = dict(p, prot=((180 - p['prot']) if flip else p['prot']) + dr)
        q['x'], q['y'] = T(p['x'], p['y'])
        L = set(cu) if p['drill'] else {FLIPL.get(l, l) if flip else l for l in cu_layers(p['layers'], cu)}
        if L:
            items.append((f"{f.ref}.{p['num']}", p['net'], L, pad_box(q)))
        cent.append((p, q['x'], q['y'], L))
    rings = [[T(*pt) for pt in r] for r in f.outline]
    return items, rings, cent


class SideMap:
    """occupancy raster of one side + its summed-area table"""

    def __init__(self, b, back, skip, res, edge, nets=()):
        o = b.outline
        self.x0, self.y0, self.res = o[0], o[1], res
        self.W, self.H = int((o[2] - o[0]) / res) + 2, int((o[3] - o[1]) / res) + 2
        W, H = self.W, self.H
        occ = bytearray(W * H)
        ring = PolyIndex([q for r in b.rings for q in r[:-1]]) if len(b.rings) == 1 else None
        for j in range(H):                                   # outside the outline (and --edge in)
            y = self.y0 + j * res
            ins = _spans(ring, y) if ring else None
            for i in range(W):
                x = self.x0 + i * res
                if not (any(a <= x <= c for a, c in ins) if ring else inside((x, y), b.rings)):
                    occ[j * W + i] = 1
        if edge > 0:                                         # grow the outside in by `edge`
            k = max(1, int(round(edge / res)))
            grown = bytearray(occ)
            for j in range(H):
                for i in range(W):
                    if occ[j * W + i]:
                        for jj in range(max(0, j - k), min(H, j + k + 1)):
                            row = jj * W
                            grown[row + max(0, i - k):row + min(W, i + k + 1)] = b'\x01' * (min(W, i + k + 1) - max(0, i - k))
            occ = grown
        for g in b.placed():
            if g.ref in skip:
                continue
            if g.back == back and not (g.art and not g.pads):
                for r in g.outline:
                    self._fill(occ, PolyIndex(r[:-1] if r[0] == r[-1] else r))
            for p in g.pads:                                 # a through pad blocks both sides
                if p['drill'] and g.back != back:
                    bb = pad_box(p)
                    self._box(occ, bb)
        self.occ = occ
        self.S = self._sat(occ)
        # foreign copper on this side's outer layer, grown by the default clearance, one
        # raster per net of the part: a pad may sit on its own net's copper, no other
        L = b.copper[-1] if back else b.copper[0]
        objs = []
        for g in b.placed():
            if g.ref not in skip:
                objs += [(p['net'], _pad_shape(p, pad_box(p))) for p in g.pads if p['kind'] != 'np_thru_hole'
                         and (p['drill'] or L in cu_layers(p['layers'], b.copper))]
        objs += [(t['net'], ('cap', t['a'], t['b'], t['w'] / 2)) for t in b.tracks if t['layer'] == L]
        objs += [(v['net'], ('disk', v['x'], v['y'], v['size'] / 2)) for v in b.vias]
        self.C = {}
        for n in set(nets) | {''}:
            cu = bytearray(W * H)
            for on, sh in objs:
                if on and on == n:
                    continue
                y_lo, y_hi, fn = _iv(sh, 0.15)
                for jj in range(max(0, math.floor((y_lo - self.y0) / res)), min(H - 1, math.ceil((y_hi - self.y0) / res)) + 1):
                    iv = fn(self.y0 + jj * res)
                    if iv:
                        i0, i1 = max(0, math.floor((iv[0] - self.x0) / res)), min(W - 1, math.ceil((iv[1] - self.x0) / res))
                        if i0 <= i1:
                            cu[jj * W + i0:jj * W + i1 + 1] = b'\x01' * (i1 - i0 + 1)
            self.C[n] = self._sat(cu)

    def _sat(self, ras):
        W, H = self.W, self.H
        S = array('i', [0]) * ((W + 1) * (H + 1))            # summed-area table
        for j in range(H):
            run = 0
            for i in range(W):
                run += ras[j * W + i]
                S[(j + 1) * (W + 1) + i + 1] = S[j * (W + 1) + i + 1] + run
        return S

    def _fill(self, occ, idx):
        res, W = self.res, self.W
        fb = idx.bbox
        for j in range(max(0, int((fb[1] - self.y0) / res)), min(self.H, int((fb[3] - self.y0) / res) + 2)):
            for a, c in _spans(idx, self.y0 + j * res):
                i0, i1 = max(0, math.floor((a - self.x0) / res)), min(W - 1, math.ceil((c - self.x0) / res))
                if i0 <= i1:
                    occ[j * W + i0:j * W + i1 + 1] = b'\x01' * (i1 - i0 + 1)

    def _box(self, occ, bb):
        res, W = self.res, self.W
        i0, i1 = max(0, math.floor((bb[0] - self.x0) / res)), min(W - 1, math.ceil((bb[2] - self.x0) / res))
        for j in range(max(0, math.floor((bb[1] - self.y0) / res)), min(self.H, math.ceil((bb[3] - self.y0) / res) + 1)):
            if i0 <= i1:
                occ[j * W + i0:j * W + i1 + 1] = b'\x01' * (i1 - i0 + 1)

    def free(self, bb, S=None):
        """is the box clear of everything (inclusive cells)? S: which table (courtyards)"""
        res, W = self.res, self.W + 1
        i0, j0 = math.floor((bb[0] - self.x0) / res), math.floor((bb[1] - self.y0) / res)
        i1, j1 = math.ceil((bb[2] - self.x0) / res), math.ceil((bb[3] - self.y0) / res)
        if i0 < 0 or j0 < 0 or i1 >= self.W or j1 >= self.H:
            return False
        S = S or self.S
        return S[(j1 + 1) * W + i1 + 1] - S[j0 * W + i1 + 1] - S[(j1 + 1) * W + i0] + S[j0 * W + i0] == 0


def _spans(idx, y):
    xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                for x1, y1, x2, y2 in idx.rows.get(math.floor(y / idx.cell), ())
                if (y1 > y) != (y2 > y))
    return list(zip(xs[::2], xs[1::2]))


def _bands(rings, res):
    """the courtyard as boxes (x0, y0, x1, y1) that cover it: one slab per pair of vertex
    ys, split into <= 8 while an edge in it slants. An L-shaped courtyard is two boxes,
    not its bounding box (which refused every spot tucked into the L)."""
    out = []
    for r in rings:
        pts = r[:-1] if r[0] == r[-1] else r
        idx = PolyIndex(pts)
        E = list(zip(pts, pts[1:] + pts[:1]))
        ys = sorted({p[1] for p in pts})
        for ya, yb in zip(ys, ys[1:]):
            slant = any(abs(u[0] - v[0]) > 1e-9 for u, v in E if min(u[1], v[1]) <= ya and max(u[1], v[1]) >= yb)
            n = max(1, min(8, math.ceil((yb - ya) / res))) if slant else 1
            for k in range(n):
                u, v = ya + (yb - ya) * k / n, ya + (yb - ya) * (k + 1) / n
                ivs = sorted(_spans(idx, u + 1e-6) + _spans(idx, v - 1e-6))
                merged = []
                for lo, hi in ivs:                   # union of the two rows' spans
                    if merged and lo <= merged[-1][1]:
                        merged[-1][1] = max(merged[-1][1], hi)
                    else:
                        merged.append([lo, hi])
                out += [(lo, u, hi, v) for lo, hi in merged]
    out.sort(key=lambda q: (q[0], q[2], q[1]))
    keep = []
    for q in out:                                    # stack equal-width boxes
        if keep and abs(keep[-1][0] - q[0]) < 1e-9 and abs(keep[-1][2] - q[2]) < 1e-9 and abs(keep[-1][3] - q[1]) < 1e-9:
            keep[-1] = (q[0], keep[-1][1], q[2], q[3])
        else:
            keep.append(q)
    return keep


def _is_rail(b, net, fanout):
    return rail_voltage(net.split('/')[-1]) is not None or net == 'GND' or len(b.nets.get(net, ())) > fanout


def c_place(b, a):
    if not a.args or a.args[0] not in b.fps:
        print("place REF [--near REF|X,Y] [-r 8] [--side f|b|both] [--step 0.1] [-o OUT [--pick N]]"); return 1
    f = b.fps[a.args[0]]
    cu = b.copper
    nets = {p['net'] for p in f.pads if p['net'] and not p['net'].startswith('unconnected-')}
    # what pulls: --near REF's pads on our nets (else its centre), or our signal neighbours
    pull, nside = [], None
    if a.near:
        g = b.fps.get(a.near)
        if g:
            nside = g.back
            pull = [(p['x'], p['y'], p['net']) for p in g.pads if p['net'] in nets and p['net'] != 'GND'] \
                or [(g.x, g.y, None)]
        else:
            try:
                x, y = (float(v) for v in a.near.split(','))
                pull = [(x, y, None)]
            except ValueError:
                print(f"--near {a.near}: not a ref or X,Y"); return 1
    else:
        pull = [(p['x'], p['y'], p['net']) for g in b.placed() if g is not f for p in g.pads
                if p['net'] in nets and not _is_rail(b, p['net'], a.fanout)]
    if not pull:
        print(f"{f.ref}: every net is a rail ({', '.join(sorted(unesc_disp(n) for n in nets))}) - "
              f"say what it serves: --near REF"); return 1
    ax = sum(p[0] for p in pull) / len(pull)
    ay = sum(p[1] for p in pull) / len(pull)
    # same-net copper already on the board, per net, bucketed in 1 mm cells
    tgt = defaultdict(lambda: defaultdict(list))
    def put(n, x, y, side):
        tgt[n][(math.floor(x), math.floor(y))].append((x, y, side))
    for g in b.placed():
        if g is not f:
            for p in g.pads:
                if p['net'] in nets:
                    put(p['net'], p['x'], p['y'], None if p['drill'] else g.back)
    for t in b.tracks:
        if t['net'] in nets:
            for q in (t['a'], t['b'], t['mid']):
                put(t['net'], q[0], q[1], None)
    for v in b.vias:
        if v['net'] in nets:
            put(v['net'], v['x'], v['y'], None)

    def nearest(n, X, Y, back, through):
        """distance to the nearest same-net copper (+1.5 mm for an SMD pad on the other side)"""
        G, gx, gy, best = tgt.get(n), math.floor(X), math.floor(Y), None
        if not G:
            return None
        for r in range(0, 140):
            if best is not None and best <= r - 1:
                break
            for cx in range(gx - r, gx + r + 1):
                for cy in (range(gy - r, gy + r + 1) if cx in (gx - r, gx + r) else (gy - r, gy + r)):
                    for tx, ty, ts in G.get((cx, cy), ()):
                        d = math.hypot(X - tx, Y - ty) + (1.5 if ts is not None and ts != back and not through else 0)
                        if best is None or d < best:
                            best = d
        return best

    sides = {'f': [False], 'b': [True], 'both': [False, True]}.get((a.side or 'both').lower(), [False, True])
    rots = [float(r) for r in (a.rots or '0,90,180,270').split(',')]
    res, cstep = 0.1, 0.25                                  # raster, candidate pitch
    maps = {s: SideMap(b, s, {f.ref}, res, a.edge, nets) for s in sides}
    R = a.radius if a.radius and a.radius > 0 else 8.0
    cands = []
    for back in sides:
        flip = back != f.back
        for rot in rots:
            items0, rings, cent = as_placed(f, 0.0, 0.0, rot, flip, cu)
            rbs = _bands(rings, res) if rings else []
            pbs = [(net or '', bb) for _n, net, _L, bb in items0]
            for gy in range(-int(R / cstep), int(R / cstep) + 1):
                for gx in range(-int(R / cstep), int(R / cstep) + 1):
                    if gx * gx + gy * gy > (R / cstep) ** 2:
                        continue
                    x, y = round((ax + gx * cstep) / 0.05) * 0.05, round((ay + gy * cstep) / 0.05) * 0.05
                    M = maps[back]
                    if not all(M.free((rb[0] + x, rb[1] + y, rb[2] + x, rb[3] + y)) for rb in rbs) or \
                            not all(M.free((p[0] + x, p[1] + y, p[2] + x, p[3] + y), M.C.get(n, M.C[''])) for n, p in pbs):
                        continue
                    score, rat = 0.0, 0.0
                    for p, px, py, L in cent:
                        n = p['net']
                        if n not in nets:
                            continue
                        X, Y = px + x, py + y
                        best = nearest(n, X, Y, back, bool(p['drill']))
                        if best is None:
                            continue
                        score += (0.3 if _is_rail(b, n, a.fanout) else 1.0) * best
                        rat += best
                        near = [math.hypot(X - qx, Y - qy) for qx, qy, qn in pull if a.near and qn == n]
                        if near:                     # --near: hug that part's pin on this net, its side
                            score += min(near) + (2.0 if nside is not None and back != nside and not p['drill'] else 0)
                    score += 0.2 * math.hypot(x - ax, y - ay)
                    cands.append((score, x, y, rot, back, rat))
    if not cands:
        print(f"{f.ref}: no spot within {R:g} mm of {ax:.2f},{ay:.2f} on {'/'.join('B' if s else 'F' for s in sides)} "
              f"fits its courtyard - widen -r or --side both"); return 2
    cands.sort(key=lambda c: c[0])
    w = World(b, ref=f.ref)
    shown, ok, seen = 0, [], set()
    print(f"place {f.ref} ({f.value}, {f.fp.split(':')[-1]})  near {ax:.2f},{ay:.2f}"
          f"{' (' + a.near + ')' if a.near else ''}   {len(cands)} fitting spot(s) within {R:g} mm, best first")
    print(f"  {'#':>2} {'x':>8} {'y':>8} {'rot':>4} side  score  ratsnest  copper")
    for c in cands:
        score, x, y, rot, back, rat = c
        key = (round(x, 1), round(y, 1), rot, back)
        if key in seen:
            continue
        seen.add(key)
        items, rings, _c = as_placed(f, x, y, rot, back != f.back, cu)
        hits = w.hits(items, rings, back)
        if not hits:
            ok.append(c)
        if shown < a.max or (not ok and shown < 60):
            msg = 'CLEAR' if not hits else sorted(hits.values())[0][:70]
            print(f"  {len(ok) if not hits else '-':>2} {x:8.2f} {y:8.2f} {rot:4g}   {'B' if back else 'F'}  {score:6.2f}  {rat:7.2f}  {msg}")
            shown += 1
        if len(ok) >= max(a.pick, 1) and shown >= a.max:
            break
    if not ok:
        print("  none of these is copper-clear; widen -r, try --side both, or clear the copper first"); return 2
    if not a.out:
        print("  (dry run; -o OUT writes #1, --pick N another)"); return 0
    score, x, y, rot, back, rat = ok[min(a.pick, len(ok)) - 1]
    t = PcbText(b.path)
    s = t.text(f.uuid)
    flip = back != f.back
    if flip:
        s = flip_text(s, f)
    t.sub(f.uuid, place_text(s, _flipped(f) if flip else f, x, y, rot))
    t.save(a.out)
    print(f"  wrote {f.ref} -> {x:.3f},{y:.3f} rot {rot:g} {'B' if back else 'F'}{' (flipped)' if flip else ''} -> {a.out}")
    return 0
