"""kpcb.py copper connectivity of one net: pads, tracks (split where another
track tees into or crosses them), vias and zone-fill fragments in one graph. `net` counts
the pieces the copper is really in; `ampacity --from --to` walks the path
between two pads with the pours as ideal conductors (their necks are not
measured, and the output says so)."""
import math
from collections import defaultdict
from kpcb_board import PolyIndex, pt_seg_dist, xf, bbox, RHO_CU

CELL = 1.0                                   # spatial hash, mm


def _q(p):
    return (round(p[0] / 0.01), round(p[1] / 0.01))


class UF(dict):
    def find(self, a):
        self.setdefault(a, a)
        while self[a] != a:
            self[a] = self[self[a]]
            a = self[a]
        return a

    def union(self, a, c):
        ra, rc = self.find(a), self.find(c)
        if ra != rc:
            self[ra] = rc


def _local(p, pt):
    """board point -> pad frame (inverse of kpcb_board.xf at the pad's angle)"""
    t = math.radians(p['prot'])
    c, s = math.cos(t), math.sin(t)
    dx, dy = pt[0] - p['x'], pt[1] - p['y']
    return dx * c - dy * s, dx * s + dy * c


def _reach(p):
    hx, hy = p['sx'] / 2, p['sy'] / 2
    for poly in p['prims']:                  # custom pads: the anchor is tiny
        for u, v in poly:
            hx, hy = max(hx, abs(u)), max(hy, abs(v))
    return hx, hy


def _around(x, y, r, frame=None):
    """centre + 8 points just outside radius r: where thermal spokes land"""
    pts = [(x, y)]
    for k in range(8):
        a = k * math.pi / 4
        u, v = (r + .1) * math.cos(a), (r + .1) * math.sin(a)
        pts.append(xf(u, v, x, y, frame) if frame is not None else (x + u, y + v))
    return pts


class Copper:
    """nodes: ('pad', i), ('fill', j), ('pt', layer, qx, qy), and per via a
    centre ('via', k) with a spoke ('vl', k, layer) on each layer it spans.
    `ideal` joins what is one piece of metal (a track end on a pad, a via in a
    pour); `edges` are the track pieces and `vedges` the barrel halves, each a
    resistive series element."""

    def __init__(self, b, net):
        self.b, self.net = b, net
        cu = b.copper
        self.pads = [(f, p) for f in b.fps.values() for p in f.pads if p['net'] == net]
        self.vias = [v for v in b.vias if v['net'] == net]
        self.tracks = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
        self.fills = [fl for fl in b.fills if fl['net'] == net]
        U = self.ideal = UF()
        grid = defaultdict(list)                     # (layer, cx, cy) -> items

        def cells(bb):
            for cx in range(math.floor(bb[0] / CELL), math.floor(bb[2] / CELL) + 1):
                for cy in range(math.floor(bb[1] / CELL), math.floor(bb[3] / CELL) + 1):
                    yield cx, cy

        def near(ly, pt):
            return grid.get((ly, math.floor(pt[0] / CELL), math.floor(pt[1] / CELL)), ())

        self.play = []
        for i, (f, p) in enumerate(self.pads):
            th = p['drill'] > 0 or any(l.startswith('*') for l in p['layers'])
            lys = set(cu) if th else {l for l in p['layers'] if l in cu}
            hx, hy = _reach(p)
            p['_reach'] = (hx, hy)
            self.play.append(lys)
            r = math.hypot(hx, hy)
            for ly in lys:
                for c in cells((p['x'] - r, p['y'] - r, p['x'] + r, p['y'] + r)):
                    grid[(ly, *c)].append(('pad', i))
            U.find(('pad', i))
        self.vlay = []
        used = set(cu) | {t['layer'] for t in self.tracks} | {fl['layer'] for fl in self.fills}
        for k, v in enumerate(self.vias):
            ls = sorted(cu.index(l) for l in v['layers'] if l in cu)
            through = len(ls) < 2 or (ls[0] == 0 and ls[-1] == len(cu) - 1)
            lys = used if through else set(cu[ls[0]:ls[-1] + 1])
            self.vlay.append(lys)
            r = (v['size'] or v['drill'] + .3) / 2
            for ly in lys:
                for c in cells((v['x'] - r, v['y'] - r, v['x'] + r, v['y'] + r)):
                    grid[(ly, *c)].append(('via', k))
            U.find(('via', k))
            for ly in lys:
                U.find(('vl', k, ly))
        for n, t in enumerate(self.tracks):
            hw = t['w'] / 2
            (ax, ay), (bx, by) = t['a'], t['b']
            for c in cells((min(ax, bx) - hw, min(ay, by) - hw, max(ax, bx) + hw, max(ay, by) + hw)):
                grid[(t['layer'], *c)].append(('trk', n))

        def ekey(t, e):
            return ('pt', t['layer'], *_q(e))
        splits = defaultdict(list)                   # track -> [(param, key)] tee points
        for n, t in enumerate(self.tracks):
            hw, ly = t['w'] / 2, t['layer']
            for e in (t['a'], t['b']):
                ke = ekey(t, e)
                U.find(ke)
                for kind, i in set(near(ly, e)):
                    if kind == 'pad':
                        u, v = _local(self.pads[i][1], e)
                        hx, hy = self.pads[i][1]['_reach']
                        if abs(u) <= hx + hw and abs(v) <= hy + hw:
                            U.union(ke, ('pad', i))
                    elif kind == 'via':
                        v = self.vias[i]
                        if math.hypot(v['x'] - e[0], v['y'] - e[1]) <= (v['size'] or v['drill']) / 2 + hw:
                            U.union(ke, ('vl', i, ly))
                    elif i != n:
                        o = self.tracks[i]
                        lim = hw + o['w'] / 2
                        if pt_seg_dist(e, o['a'], o['b']) > lim:
                            continue
                        end = next((q for q in (o['a'], o['b']) if math.dist(q, e) <= lim), None)
                        if end is not None:
                            U.union(ke, ekey(o, end))
                        else:                        # tees into o mid-span: split o there
                            L = math.dist(o['a'], o['b']) or 1
                            tpar = ((e[0] - o['a'][0]) * (o['b'][0] - o['a'][0]) +
                                    (e[1] - o['a'][1]) * (o['b'][1] - o['a'][1])) / L / L
                            splits[i].append((tpar, ke))
        # two tracks whose centrelines cross mid-span are one piece of copper (KiCad
        # merges it); an end on a body is a tee above, so only true crossings are left
        for n, t in enumerate(self.tracks):
            (ax, ay), (bx, by) = t['a'], t['b']
            hw = t['w'] / 2
            near_trk = {i for c in cells((min(ax, bx) - hw, min(ay, by) - hw, max(ax, bx) + hw, max(ay, by) + hw))
                        for kind, i in grid.get((t['layer'], *c), ()) if kind == 'trk' and i > n}
            for i in sorted(near_trk):
                o = self.tracks[i]
                (cx, cy), (dx, dy) = o['a'], o['b']
                den = (bx - ax) * (dy - cy) - (by - ay) * (dx - cx)
                if abs(den) < 1e-12:
                    continue
                s_ = ((cx - ax) * (dy - cy) - (cy - ay) * (dx - cx)) / den
                u_ = ((cx - ax) * (by - ay) - (cy - ay) * (bx - ax)) / den
                pt = (ax + s_ * (bx - ax), ay + s_ * (by - ay))
                lim = hw + o['w'] / 2
                if not (0 < s_ < 1 and 0 < u_ < 1) or \
                        min(math.dist(pt, q) for q in (t['a'], t['b'], o['a'], o['b'])) <= lim:
                    continue
                k = ('pt', t['layer'], *_q(pt))
                U.find(k)
                splits[n].append((s_, k))
                splits[i].append((u_, k))
        # a via or pad sitting on a track's body (not its end) joins it there
        def tee_into(nodef, x, y, r, lys):
            todo = []                                # decide every track first, then join:
            for ly in lys:                           # joining as we go made the outcome
                node = nodef(ly)                     # depend on set order (hash seed)
                for kind, i in set(near(ly, (x, y))):
                    if kind != 'trk':
                        continue
                    o = self.tracks[i]
                    if pt_seg_dist((x, y), o['a'], o['b']) > r + o['w'] / 2:
                        continue
                    if any(U.find(ekey(o, q)) == U.find(node) for q in (o['a'], o['b'])):
                        continue                     # already joined at an end
                    todo.append((i, ly, node))
            for i, ly, node in todo:
                o = self.tracks[i]
                L = math.dist(o['a'], o['b']) or 1
                tpar = min(1.0, max(0.0, ((x - o['a'][0]) * (o['b'][0] - o['a'][0]) +
                                          (y - o['a'][1]) * (o['b'][1] - o['a'][1])) / L / L))
                kt = ('pt', ly, *_q((o['a'][0] + tpar * (o['b'][0] - o['a'][0]),
                                     o['a'][1] + tpar * (o['b'][1] - o['a'][1]))))
                U.union(kt, node)
                splits[i].append((tpar, kt))
        for i, (f, p) in enumerate(self.pads):       # overlapping pads (a fused lead)
            hx, hy = p['_reach']
            for kind, j in set(x for ly in self.play[i] for x in near(ly, (p['x'], p['y']))):
                if kind == 'pad' and j > i and self.play[i] & self.play[j]:
                    q = self.pads[j][1]
                    qx, qy = q['_reach']
                    c = [_local(p, xf(sx * qx, sy * qy, q['x'], q['y'], q['prot']))
                         for sx in (-1, 1) for sy in (-1, 1)]
                    if min(u for u, _ in c) < hx - 1e-3 and max(u for u, _ in c) > -hx + 1e-3 and \
                            min(v for _, v in c) < hy - 1e-3 and max(v for _, v in c) > -hy + 1e-3:
                        U.union(('pad', i), ('pad', j))
        for k, v in enumerate(self.vias):
            tee_into(lambda ly, k=k: ('vl', k, ly), v['x'], v['y'], (v['size'] or v['drill']) / 2, self.vlay[k])
        for i, (f, p) in enumerate(self.pads):
            tee_into(lambda ly, i=i: ('pad', i), p['x'], p['y'], min(p['_reach']), self.play[i])
        for k, v in enumerate(self.vias):            # a via landing in a pad
            for kind, i in set(x for ly in self.vlay[k] for x in near(ly, (v['x'], v['y']))):
                if kind == 'pad' and self.play[i] & self.vlay[k]:
                    u, w = _local(self.pads[i][1], (v['x'], v['y']))
                    hx, hy = self.pads[i][1]['_reach']
                    if abs(u) <= hx and abs(w) <= hy:
                        for ly in self.play[i] & self.vlay[k]:
                            U.union(('vl', k, ly), ('pad', i))
        # pours: a fill joins the pads, vias and tracks of its net that touch it,
        # and any other fill of the net on the same layer it touches (a GND zone
        # beside a GND CPWG zone). A track tees into a fill where they meet.
        fidx = [PolyIndex(fl['pts']) for fl in self.fills]
        for j, fl in enumerate(self.fills):
            U.find(('fill', j))
            idx, ly, fb = fidx[j], fl['layer'], fl['bbox']
            def inb(x, y, r=0.0):
                return fb[0] - r <= x <= fb[2] + r and fb[1] - r <= y <= fb[3] + r
            for i, (f, p) in enumerate(self.pads):
                hx, hy = p['_reach']
                if ly in self.play[i] and inb(p['x'], p['y'], max(hx, hy) + .2) and \
                        any(q in idx for q in _around(p['x'], p['y'], max(hx, hy), p['prot'])):
                    U.union(('pad', i), ('fill', j))
            for k, v in enumerate(self.vias):
                r = (v['size'] or v['drill']) / 2
                if ly in self.vlay[k] and inb(v['x'], v['y'], r + .2) and \
                        any(q in idx for q in _around(v['x'], v['y'], r)):
                    U.union(('vl', k, ly), ('fill', j))
            hit = {}
            for n, t in enumerate(self.tracks):
                if t['layer'] == ly and inb(*t['mid'], t['len'] / 2 + t['w']):
                    s0 = next((s for s in (0, 1, .5, .25, .75)
                               if (t['a'][0] + s * (t['b'][0] - t['a'][0]),
                                   t['a'][1] + s * (t['b'][1] - t['a'][1])) in idx), None)
                    if s0 is not None:
                        hit[n] = s0
            for x, y in fl['pts']:                   # fill edge lying on a track's copper
                for kind, n in near(ly, (x, y)):
                    if kind == 'trk' and n not in hit:
                        t = self.tracks[n]
                        if pt_seg_dist((x, y), t['a'], t['b']) <= t['w'] / 2 + .01:
                            L = t['len'] or 1
                            hit[n] = min(1.0, max(0.0, ((x - t['a'][0]) * (t['b'][0] - t['a'][0]) +
                                                        (y - t['a'][1]) * (t['b'][1] - t['a'][1])) / L / L))
            for n, s0 in hit.items():
                t = self.tracks[n]
                if s0 in (0, 1):
                    kt = ekey(t, t['a'] if s0 == 0 else t['b'])
                else:
                    kt = ('pt', ly, *_q((t['a'][0] + s0 * (t['b'][0] - t['a'][0]),
                                         t['a'][1] + s0 * (t['b'][1] - t['a'][1]))))
                    splits[n].append((s0, kt))
                U.union(kt, ('fill', j))
            for j2 in range(j):
                g, gb = self.fills[j2], self.fills[j2]['bbox']
                if g['layer'] != ly or U.find(('fill', j2)) == U.find(('fill', j)) \
                        or not (gb[0] <= fb[2] + .01 and fb[0] <= gb[2] + .01
                                and gb[1] <= fb[3] + .01 and fb[1] <= gb[3] + .01):
                    continue
                # the small outline's points against the big one, then only the big
                # one's points that fall in the small one's box
                (sp, sb, si), (lp, _b, li) = sorted(((fl['pts'], fb, idx), (g['pts'], gb, fidx[j2])),
                                                    key=lambda z: len(z[0]))
                if any(li.touches(q) for q in sp) or \
                        any(si.touches(q) for q in lp if sb[0] - .01 <= q[0] <= sb[2] + .01
                            and sb[1] - .01 <= q[1] <= sb[3] + .01):
                    U.union(('fill', j2), ('fill', j))
        # track pieces between tee points = the series elements
        self.edges = []                              # (key a, key b, track, length)
        for n, t in enumerate(self.tracks):
            pts = [(0.0, ekey(t, t['a']))] + sorted(splits[n]) + [(1.0, ekey(t, t['b']))]
            for (p0, k0), (p1, k1) in zip(pts, pts[1:]):
                self.edges.append((k0, k1, t, max(0.0, p1 - p0) * t['len']))
        self.vedges = [(('vl', k, ly), ('via', k), k) for k in range(len(self.vias)) for ly in self.vlay[k]]
        # connected pieces: ideal joins plus every track piece and barrel
        C = UF()
        for k in U:
            C.union(k, U.find(k))
        for k0, k1, *_ in self.edges + self.vedges:
            C.union(k0, k1)
        self.piece = C

    # ---------------------------------------------------------------- pieces
    def pieces(self):
        """[{'pads': [...], 'fills': [...], 'vias': n, 'tracks': n, 'at': (x, y)}],
        the piece holding the most pads first"""
        P = defaultdict(lambda: {'pads': [], 'fills': [], 'vias': 0, 'tracks': 0, 'xy': []})
        F = self.piece.find
        for i, (f, p) in enumerate(self.pads):
            q = P[F(('pad', i))]
            q['pads'].append(f"{f.ref}.{p['num']}")
            q['xy'].append((p['x'], p['y']))
        for j, fl in enumerate(self.fills):
            q = P[F(('fill', j))]
            q['fills'].append((fl['layer'], fl['area']))
            q['xy'].append(((fl['bbox'][0] + fl['bbox'][2]) / 2, (fl['bbox'][1] + fl['bbox'][3]) / 2))
        for k, v in enumerate(self.vias):
            q = P[F(('via', k))]
            q['vias'] += 1
            q['xy'].append((v['x'], v['y']))
        seen = set()
        for k0, _k1, t, _L in self.edges:
            if id(t) not in seen:
                seen.add(id(t))
                P[F(k0)]['tracks'] += 1
                P[F(k0)]['xy'].append(t['mid'])
        out = []
        for q in P.values():
            e = bbox(q.pop('xy'))
            q['at'] = ((e[0] + e[2]) / 2, (e[1] + e[3]) / 2)
            out.append(q)
        return sorted(out, key=lambda q: (-len(q['pads']), -sum(a for _l, a in q['fills'])))

    # ---------------------------------------------------------------- s-t path
    def pad_node(self, spec):
        ref, _, num = spec.partition('.')
        for i, (f, p) in enumerate(self.pads):
            if f.ref == ref and p['num'] == num:
                return ('pad', i)
        return None

    def sunk(self, node, w, skip=None):
        """Is this end of a w-mm piece held near ambient? A pad or a pour, or a
        junction whose other track copper totals >= 3x the width. Not a lone via:
        the copper meeting it carries the same current and heats too."""
        R = self.ideal.find
        if not hasattr(self, '_sinks'):
            self._sinks = {R(('pad', i)) for i in range(len(self.pads))} | \
                          {R(('fill', j)) for j in range(len(self.fills))}
            self._wide = defaultdict(list)
            for k0, k1, t, _L in self.edges:
                for k in (k0, k1):
                    self._wide[R(k)].append(t)
        r = R(node)
        return r in self._sinks or sum(t['w'] for t in self._wide[r] if t is not skip) >= 3 * w

    def flow(self, s, t, amps, thick, board_h, plating):
        """Nodal solve with `amps` in at pad node s and out at t: tracks and via
        barrels are resistors, pours and pads ideal (zero ohm). None when s and t
        are not joined, else (ohm s-t, [(kind, obj, length, amps, u, w)], fills).
        Current splits by conductance, the way it really does, so a thin strand
        paralleled by a fat one carries its share, not its capacity."""
        R, C = self.ideal.find, self.piece.find
        if C(s) != C(t):
            return None
        el = []                                  # (u, w, siemens, kind, obj, length)
        for k0, k1, tr, L in self.edges:
            u, w = R(k0), R(k1)
            if u != w:
                el.append((u, w, tr['w'] * thick(tr['layer']) / (RHO_CU * max(L, .01)), 'trk', tr, L))
        for k0, k1, k in self.vedges:
            v = self.vias[k]
            el.append((R(k0), R(k1), math.pi * v['drill'] * plating / (RHO_CU * board_h / 2),
                       'via', v, board_h / 2))
        S, T, piece = R(s), R(t), C(s)
        fills = sorted({(fl['layer'], round(fl['area'])) for j, fl in enumerate(self.fills)
                        if C(('fill', j)) == piece})
        if S == T:
            return 0.0, [], fills
        idx = {}
        for u, w, *_ in el:
            for n in (u, w):
                if n != T and n not in idx and C(n) == piece:
                    idx[n] = len(idx)
        N = len(idx)
        diag, nb = [0.0] * N, [[] for _ in range(N)]
        for u, w, g, *_ in el:
            for p, q in ((u, w), (w, u)):
                if p in idx:
                    diag[idx[p]] += g
                    if q in idx:
                        nb[idx[p]].append((idx[q], g))
        # Jacobi-preconditioned conjugate gradient on the grounded Laplacian
        x, r = [0.0] * N, [0.0] * N
        r[idx[S]] = amps
        z = [ri / di for ri, di in zip(r, diag)]
        p, rz = z[:], sum(a * b for a, b in zip(r, z))
        for _ in range(20 * N + 200):
            Ap = [diag[i] * p[i] - sum(g * p[j] for j, g in nb[i]) for i in range(N)]
            alpha = rz / sum(a * b for a, b in zip(p, Ap))
            x = [xi + alpha * pi for xi, pi in zip(x, p)]
            r = [ri - alpha * ai for ri, ai in zip(r, Ap)]
            if math.sqrt(sum(ri * ri for ri in r)) < 1e-9 * amps:
                break
            z = [ri / di for ri, di in zip(r, diag)]
            rz2 = sum(a * b for a, b in zip(r, z))
            p, rz = [zi + rz2 / rz * pi for zi, pi in zip(z, p)], rz2
        V = {n: x[i] for n, i in idx.items()}
        V[T] = 0.0
        out = [(kind, obj, L, abs(g * (V[u] - V[w])), u, w)
               for u, w, g, kind, obj, L in el if u in V and w in V]
        return V[S] / amps, out, fills
