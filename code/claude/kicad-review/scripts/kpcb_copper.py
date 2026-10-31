"""kpcb.py copper connectivity of one net: pads, tracks (split where another
track tees into or crosses them), vias and zone-fill fragments in one graph. `net` counts
the pieces the copper is really in; `ampacity --from --to` walks the path
between two pads with the pours as ideal conductors (their necks are not
measured, and the output says so)."""
import math
from collections import defaultdict
from kpcb_board import _pidx, pt_seg_dist, xf, bbox, RHO_CU

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


def pad_dist(p, pt, rounded=False):
    """signed distance from pt to pad p's copper (< 0 inside): the anchor shape and, for a
    custom pad, each primitive polygon itself - not their bounding box. rounded: a roundrect's
    or oval's corners as drawn (Copper's joins, KiCad's test, the census heuristics' in_pad);
    else a sharp box (clearance-side callers, where generous is the safe direction)"""
    from kpcb_board import point_in_poly
    u, v = _local(p, pt)
    hx, hy = p['sx'] / 2, p['sy'] / 2
    if p['shape'] == 'circle':
        d = math.hypot(u, v) - hx
    else:
        r = p.get('rr', 0.0) if rounded else 0.0
        dx, dy = abs(u) - hx + r, abs(v) - hy + r
        d = math.hypot(max(dx, 0), max(dy, 0)) + min(max(dx, dy), 0) - r
    for poly in p['prims']:
        e = min(pt_seg_dist((u, v), poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly)))
        d = min(d, -e if point_in_poly((u, v), poly) else e)
    return d


def seg_pad_gap(p, a, c):
    """distance from segment ac to pad p's copper as drawn, 0 where they meet: a circle, a box
    with its rounded corners (the segment to the box shrunk by r, less r), each custom primitive"""
    from kpcb_board import point_in_poly, seg_box_dist, seg_seg_dist
    la, lc = _local(p, a), _local(p, c)
    hx, hy = p['sx'] / 2, p['sy'] / 2
    if p['shape'] == 'circle':
        d = max(0.0, pt_seg_dist((0, 0), la, lc) - hx)
    else:
        r = min(p.get('rr', 0.0), hx, hy)
        d = max(0.0, seg_box_dist(la, lc, (-hx + r, -hy + r, hx - r, hy - r)) - r)
    for poly in p['prims']:
        if point_in_poly(la, poly):
            return 0.0
        d = min([d] + [seg_seg_dist(la, lc, poly[i], poly[(i + 1) % len(poly)]) for i in range(len(poly))])
    return d


def pad_within(p, pt, d, rounded=False):
    """pad_dist(p, pt, rounded) <= d, a box round the pad's reach rejecting first (a census asks ~3M times)"""
    hx, hy = _reach(p) if p['prims'] else (p['sx'] / 2, p['sy'] / 2)
    r = hx + hy + d                                  # >= the reach's radius, at any pad angle
    if abs(pt[0] - p['x']) > r or abs(pt[1] - p['y']) > r:
        return False
    return pad_dist(p, pt, rounded) <= d


def _fill_cross(idx, t):
    """param (0..1) along track t where an outline edge of fill `idx` comes within its copper,
    walking the cells along the track, or None"""
    from kpcb_board import seg_seg_dist
    (ax, ay), (bx, by), c, hw = t['a'], t['b'], idx.cell, t['w'] / 2
    L = t['len'] or 1e-9
    n = max(1, math.ceil(L / c))
    seen = set()
    for k in range(n + 1):
        x, y = ax + (bx - ax) * k / n, ay + (by - ay) * k / n
        for gx in range(math.floor((x - hw) / c) - 1, math.floor((x + hw) / c) + 2):
            for gy in range(math.floor((y - hw) / c) - 1, math.floor((y + hw) / c) + 2):
                for e0, e1 in idx.grid.get((gx, gy), ()) if (gx, gy) not in seen else ():
                    if seg_seg_dist(t['a'], t['b'], e0, e1) <= hw + .01:
                        m = 4 * n                    # the nearest point of the track to that edge
                        return min(range(m + 1), key=lambda j: pt_seg_dist(
                            (ax + (bx - ax) * j / m, ay + (by - ay) * j / m), e0, e1)) / m
                seen.add((gx, gy))
    return None


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

    def __init__(self, b, net, strict=False, clips=True):
        """strict: join only where KiCad's join is robust - a track end whose whole cap is
        inside a pad or via, on another track's centreline end or body, a via/pad anchor on a
        track's body (`loops` judges removals so, never leaning on a cap that grazes a pad).
        clips=False: a track body whose centreline misses a pad does not join it (clip_only)"""
        self.b, self.net, self.strict = b, net, strict
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
            hw, ly = (-t['w'] / 2 if strict else t['w'] / 2), t['layer']
            for e in (t['a'], t['b']):
                ke = ekey(t, e)
                U.find(ke)
                for kind, i in set(near(ly, e)):
                    if kind == 'pad':
                        pd = self.pads[i][1]
                        if pd['prims']:                  # a custom pad: its real polygons
                            if pad_dist(pd, e) <= min(hw, -1e-3):
                                U.union(ke, ('pad', i))
                            continue
                        if not strict:               # the end cap overlaps the pad as drawn
                            if pad_dist(pd, e, True) <= hw:  # (rounded corners, a circle's rim)
                                U.union(ke, ('pad', i))
                            continue
                        u, v = _local(pd, e)
                        hx, hy = pd['_reach']
                        if abs(u) <= max(hx + hw, 1e-3) and abs(v) <= max(hy + hw, 1e-3) and pad_dist(pd, e, True) <= 1e-3:
                            U.union(ke, ('pad', i))
                    elif kind == 'via':
                        v = self.vias[i]
                        if math.hypot(v['x'] - e[0], v['y'] - e[1]) <= max((v['size'] or v['drill']) / 2 + hw, 1e-3):
                            U.union(ke, ('vl', i, ly))
                    elif i != n:
                        o = self.tracks[i]
                        lim = hw + o['w'] / 2 if not strict else min(t['w'], o['w']) / 2
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
        def tee_into(nodef, x, y, r, lys, pad=None):
            """pad (not strict): any overlap of the track's body with the pad's real shape, as
            KiCad joins it (CC2's diagonal clipped C33.1's corner, 2026-10-28), not just its
            centre's disk; a body whose centreline misses the pad (only its edge overlaps) goes
            in self.clips"""
            todo = []                                # decide every track first, then join:
            rr = math.hypot(*pad['_reach']) if pad else 0.0
            for ly in lys:                           # joining as we go made the outcome
                node = nodef(ly)                     # depend on set order (hash seed)
                cand = {x_ for c in cells((x - rr, y - rr, x + rr, y + rr)) for x_ in grid.get((ly, *c), ())} \
                    if pad else set(near(ly, (x, y)))
                for kind, i in cand:
                    if kind != 'trk':
                        continue
                    o, dep = self.tracks[i], None
                    if pt_seg_dist((x, y), o['a'], o['b']) > r + o['w'] / 2:
                        dep = o['w'] / 2 - seg_pad_gap(pad, o['a'], o['b']) if pad else -1
                        if dep < 0:
                            continue
                    if any(U.find(ekey(o, q)) == U.find(node) for q in (o['a'], o['b'])):
                        continue                     # already joined at an end
                    todo.append((i, ly, node))
                    if dep is not None and dep < o['w'] / 2:
                        if not clips:
                            todo.pop()
                            continue
                        self.clips.append((o, node[1], dep))
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
        for k, v in enumerate(self.vias):           # strict: the anchor itself on the track's body
            tee_into(lambda ly, k=k: ('vl', k, ly), v['x'], v['y'], 0 if strict else (v['size'] or v['drill']) / 2, self.vlay[k])
        self.clips = []                              # [(track, pad index, overlap mm)]: marginal body joins
        for i, (f, p) in enumerate(self.pads):
            tee_into(lambda ly, i=i: ('pad', i), p['x'], p['y'], 0 if strict else min(p['_reach']), self.play[i],
                     None if strict else p)
        for k, v in enumerate(self.vias):            # a via landing in a pad
            for kind, i in set(x for ly in self.vlay[k] for x in near(ly, (v['x'], v['y']))):
                if kind == 'pad' and self.play[i] & self.vlay[k]:
                    pd = self.pads[i][1]
                    u, w = _local(pd, (v['x'], v['y']))
                    hx, hy = pd['_reach']
                    if pad_dist(pd, (v['x'], v['y'])) <= 0 if pd['prims'] else abs(u) <= hx and abs(w) <= hy:
                        for ly in self.play[i] & self.vlay[k]:
                            U.union(('vl', k, ly), ('pad', i))
        # pours: a fill joins the pads, vias and tracks of its net that touch it,
        # and any other fill of the net on the same layer it touches (a GND zone
        # beside a GND CPWG zone). A track tees into a fill where they meet.
        fidx = [_pidx(b, fl) for fl in self.fills]
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
            for n, t in enumerate(self.tracks):      # a fill edge crossing the track's copper anywhere
                if n not in hit and t['layer'] == ly and inb(*t['mid'], t['len'] / 2 + t['w']):
                    s0 = _fill_cross(idx, t)         # (a narrow band across a long track: +3V3 In2)
                    if s0 is not None:
                        hit[n] = s0
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
    def clip_only(self):
        """[(track, fp, pad, overlap mm)]: the clips (a track body whose edge, not centreline,
        overlaps a pad, no end on it) that are the pad's only join to the rest of its piece"""
        if not self.clips:
            return []
        F, G = self.piece.find, Copper(self.b, self.net, self.strict, clips=False).piece.find
        part = lambda f: {i: frozenset(j for j in range(len(self.pads)) if f(('pad', j)) == f(('pad', i)))   # noqa: E731
                          for i in {i for _t, i, _d in self.clips}}
        a, b = part(F), part(G)
        return [(t, *self.pads[i], d) for t, i, d in self.clips if a[i] != b[i]]

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


# ---------------------------------------------------------------- dead copper

def _key_xy(k):
    return (k[2] * 0.01, k[3] * 0.01) if k[0] == 'pt' else None


PARALLEL = 0.5                                       # a branch meeting one pour this far apart is in parallel, mm
HEATSINK = 1.5                                       # a pad-less pour joined this close to a pad is its heatsink, mm
MICRO = 0.05                                         # a segment this short is a join, never a conductor, mm


def dead_copper(b, net, C=None, terminal=None):
    """copper of `net` that serves no pad: [{'tracks': [(track, dead mm, keep)], 'vias': [via],
    'fills': [fill], 'pads', 'mm', 'pour', 'layers', 'at': (x, y) | None, 'off': label}], one per
    branch. terminal(fp, pad): the pads that count as served (default all; `rf` passes the
    populated ones, so a branch to a DNP pad is a stub, its 'pads' naming what ends it).
    keep is None for a wholly dead track, else the (a, b) it keeps (a dead end cut back); 'stubs':
    [(track, cut point, via centre)], a cut end in a via off the track's line (_via_centred).

    Graph: Copper's ideal joins contracted (a pad, a fill and the track ends on them are one
    node), track pieces and via barrel halves the edges; a via's spoke on a layer where nothing
    meets it is not copper. Block-cut tree: a leaf block (one attach vertex) holding no pad but
    at its attach goes, until stable; a piece left as one block with < 2 pad nodes (a stub off
    a single pad, floating copper) goes too. So a pad-less run that is a cycle (two layers in
    parallel through two vias, a band round it) is dead when it hangs off ONE point, and a pour
    stitched by two vias on a through-going path is a parallel conductor, not dead, as is a run
    that leaves a pour and meets it again > PARALLEL mm away, or a track into a pad the pour
    already joins (a pour is one node: the in-pour census judges that copper); a pour
    joined only within HEATSINK mm of a pad (thermal vias) is its heatsink, never dead. A
    segment under MICRO mm is a join, not a conductor: it is left out (the ends it joins still
    join by Copper's end tolerance), so a via held on a layer only by a 14 um dot whose cap
    grazes the next track is dead, the dot with it (VCC_RF ee220847, 2026-10-27). Reads the
    saved fill: on a stale fill, trust only the track and via verdicts."""
    from types import SimpleNamespace
    from kpcb_move import is_arc
    micro = [t for t in b.tracks if t['net'] == net and t['len'] < MICRO and not is_arc(t)]
    C = C or Copper(SimpleNamespace(fps=b.fps, vias=b.vias, fills=b.fills, copper=b.copper,
                                    _pidx=b.__dict__.setdefault('_pidx', {}),    # the board's fill indexes
                                    tracks=[t for t in b.tracks if t['net'] == net and all(t is not m for m in micro)]), net)
    R = C.ideal.find
    size = defaultdict(int)
    for k in list(C.ideal):
        size[R(k)] += 1
    term = {R(('pad', i)) for i, (f, p) in enumerate(C.pads) if terminal is None or terminal(f, p)}
    E, loops = [], defaultdict(list)                 # (u, v, kind, obj, mm, key u, key v)
    for k0, k1, t, L in C.edges:
        u, v = R(k0), R(k1)
        if u != v:
            E.append((u, v, 'trk', t, L, k0, k1))
        else:                                        # both ends on one node (in one fill, one via):
            loops[u].append((t, L))                  # not a branch, but dead with that node
    for kl, kv, k in C.vedges:
        if size[R(kl)] > 1:                          # a spoke something meets
            E.append((R(kl), R(kv), 'via', C.vias[k], 0.0, kl, kv))
    adj = defaultdict(list)
    for e, (u, v, *_r) in enumerate(E):
        adj[u].append((v, e))
        adj[v].append((u, e))
    # biconnected components (iterative Tarjan over edges; parallel edges are a cycle)
    disc, low, blocks, st, clock = {}, {}, [], [], 0
    for root in sorted(adj, key=str):
        if root in disc:
            continue
        disc[root] = low[root] = clock
        clock += 1
        walk = [(root, -1, iter(adj[root]))]
        while walk:
            u, pe, nb = walk[-1]
            for v, e in nb:
                if e == pe:
                    continue
                if v not in disc:
                    st.append(e)
                    disc[v] = low[v] = clock
                    clock += 1
                    walk.append((v, e, iter(adj[v])))
                    break
                if disc[v] < disc[u]:
                    st.append(e)
                    low[u] = min(low[u], disc[v])
            else:
                walk.pop()
                if walk:
                    p = walk[-1][0]
                    low[p] = min(low[p], low[u])
                    if low[u] >= disc[p]:
                        blk = []
                        while True:
                            x = st.pop()
                            blk.append(x)
                            if x == pe:
                                break
                        blocks.append(blk)
    bv = [{w for e in blk for w in E[e][:2]} for blk in blocks]
    vb = defaultdict(set)
    for i, vs in enumerate(bv):
        for w in vs:
            vb[w].add(i)
    fillv = {R(('fill', j)) for j in range(len(C.fills))}

    padsof, fill_lys = defaultdict(list), defaultdict(set)
    for i, (f, p) in enumerate(C.pads):
        padsof[R(('pad', i))].append(p)
    for j, fl in enumerate(C.fills):
        fill_lys[R(('fill', j))].add(fl['layer'])

    def parallel(i, w):
        """block i meets node w (a pour, or pads a pour joins) at two of its pads, at a pad and
        elsewhere, or, w a pour, at points > PARALLEL mm apart while all of it stays within
        max(1, 0.3 x their spread) mm of the lines between them: a run back into the pour. A
        via cluster's band reaching 20 mm on (PPHV, 2026-10-27) is not thin: dead"""
        pts = [(_key_xy(k) or (E[e][3]['x'], E[e][3]['y']), E[e][3]['w'] / 2 if E[e][2] == 'trk' else 0.0)
               for e in blocks[i] for x, k in ((E[e][0], E[e][5]), (E[e][1], E[e][6])) if x == w]
        inp = [{id(p) for p in padsof[w] if pad_within(p, xy, hw + .01)} for xy, hw in pts]
        hit = set().union(*inp)
        if len(hit) > 1 or hit and not all(inp):
            return True
        lys = {k[1] if k[0] == 'pt' else k[-1] for e in blocks[i] for x, k in ((E[e][0], E[e][5]), (E[e][1], E[e][6]))
               if x == w}
        if len(lys & fill_lys[w]) > 1:
            return True                              # stitching two layers' pours (a THT pad made them one node)
        tp = list({(round(x, 2), round(y, 2)) for (x, y), _h in pts})
        sp = max((math.dist(p, q) for p in tp for q in tp), default=0.0)
        if w not in fillv or sp <= PARALLEL:
            return False
        lines = [(p, q) for n, p in enumerate(tp[:12]) for q in tp[n + 1:12]]
        D = max(1.0, 0.3 * sp)
        body = [_key_xy(k) or (E[e][3]['x'], E[e][3]['y']) for e in blocks[i] for k in E[e][5:7]]
        body += [q for j, fl in enumerate(C.fills) if R(('fill', j)) in bv[i] and R(('fill', j)) != w
                 for q in fl['pts'][::max(1, len(fl['pts']) // 200)]]
        return all(min(pt_seg_dist(q, a, c) for a, c in lines) <= D for q in body)
    alive, changed = set(range(len(blocks))), True
    while changed:
        changed = False
        for i in sorted(alive):
            att = {w for w in bv[i] if len(vb[w]) > 1}
            pads = {w for w in bv[i] if w in term} - att
            if len(att) == 1 and not pads and parallel(i, next(iter(att))):
                continue                             # pad to pad through one pour's node, or back into it
            if len(att) <= 1 and not pads or not att and len(pads) < 2:
                alive.discard(i)
                for w in bv[i]:
                    vb[w].discard(i)
                changed = True
    keep_v = {w for i in alive for w in bv[i]} | term
    dead = sorted(e for i in set(range(len(blocks))) - alive for e in blocks[i])
    if not dead:
        return []
    g = UF()                                         # branches: dead edges joined off the kept part
    for e in dead:
        g.find(('e', e))
        for w in E[e][:2]:
            if w not in keep_v:
                g.union(('e', e), ('v', w))
    grp = defaultdict(list)
    for e in dead:
        grp[g.find(('e', e))].append(e)
    pieces = defaultdict(list)                       # track -> its pieces in order, alive or not
    dset = set(dead)
    eid = {(id(E[e][3]), E[e][5], E[e][6]): e for e in range(len(E)) if E[e][2] == 'trk'}
    deadv = {w for e in dead for w in E[e][:2]} - keep_v
    for k0, k1, t, L in C.edges:
        e = eid.get((id(t), k0, k1))
        pieces[id(t)].append((k0, k1, e in dset if e is not None else R(k0) in deadv))
    vk = {id(v): k for k, v in enumerate(C.vias)}
    out = []
    for es in grp.values():
        trk, vias, fills, at, off, touch, stubs = {}, {}, [], None, None, [], []
        for e in es:
            u, v, kind, o, L, ku, kv = E[e]
            if kind == 'trk':
                trk[id(o)] = (o, trk.get(id(o), (o, 0.0))[1] + L)
            elif R(('via', vk[id(o)])) not in keep_v:
                vias[id(o)] = o
            for w, k in ((u, ku), (v, kv)):
                if w in keep_v:
                    touch.append((w, _key_xy(k) or (o['x'], o['y'])))
                    if at is None:
                        at, off = touch[-1][1], _label(C, R, w, touch[-1][1], k[1] if k[0] == 'pt' else k[-1])
        vs = {w for e in es for w in E[e][:2]} - keep_v
        fills = [fl for j, fl in enumerate(C.fills) if R(('fill', j)) in vs]
        if fills and touch and all(any(pad_dist(p, xy) <= HEATSINK for p in padsof[w]) for w, xy in touch):
            continue                                 # a pour hanging off a pad (vias in or beside it): its heatsink
        if not trk and not vias and sum(fl['area'] for fl in fills) < 4:
            continue                                 # a via's own patch of pour
        for w in vs:
            for t, L in loops[w]:
                trk[id(t)] = (t, trk.get(id(t), (t, 0.0))[1] + L)
        for t in micro:                              # a join left out of the graph: with its dead via
            if any(math.dist(e, (v['x'], v['y'])) < v['size'] / 2 for v in vias.values() for e in (t['a'], t['b'])):
                trk[id(t)] = (t, t['len'])
                pieces[id(t)] = [(None, None, True)]
        rows = []
        for o, L in trk.values():
            ps = pieces[id(o)]
            if all(d for *_k, d in ps):
                rows.append((o, L, None))
                continue
            live = [i for i, (*_k, d) in enumerate(ps) if not d]
            if live == list(range(live[0], live[-1] + 1)):   # a dead end: cut back to the last live piece
                a2, b2 = _key_xy(ps[live[0]][0]) or o['a'], _key_xy(ps[live[-1]][1]) or o['b']
                rows.append((o, L, _via_centred(b, o, (_on_line(o, a2) if live[0] else o['a'],
                                                       _on_line(o, b2) if live[-1] < len(ps) - 1 else o['b']), stubs)))
            else:
                rows.append((o, L, False))           # dead in the middle (a fill both sides): report only
        out.append({'tracks': rows, 'vias': list(vias.values()), 'fills': fills, 'stubs': stubs,
                    'pads': [f"{f.ref}.{p['num']}" + (' DNP' if getattr(f, 'dnp', False) else '')
                             for i, (f, p) in enumerate(C.pads) if R(('pad', i)) in vs], 'mm': sum(L for _o, L, _k in rows),
                    'pour': sum(fl['area'] for fl in fills), 'at': at, 'off': off or 'nothing (no pad on it)',
                    'layers': sorted({o['layer'] for o, *_r in rows} | {fl['layer'] for fl in fills},
                                     key=lambda ly: b.copper.index(ly) if ly in b.copper else 99)})
    return sorted(out, key=lambda d: -(d['mm'] + d['pour'] + len(d['vias'])))


def _via_centred(b, t, keep, stubs):
    """a cut-back end that lands in a same-net via goes to its centre when that is on t's line;
    off it (an end in a via off its centre is KiCad's TRACK_NOT_CENTERED_ON_VIA), to the on-line
    point nearest the centre, with (t, that point, centre) added to `stubs`: a same-width stub
    onto the centre, which the writer lays only where it clears (BT1- strap cut to 103.95,115.50
    + stub to via 103.95,115.60 by hand, kdrc 0 new, 2026-10-28). False: not cut"""
    from kpcb_board import pt_seg_dist
    from kpcb_move import via_layers
    out, add = list(keep), []
    for i, o in enumerate((t['a'], t['b'])):
        for v in b.vias if out[i] != o else ():
            c, e = (v['x'], v['y']), out[i]
            if v['net'] == t['net'] and math.dist(e, c) <= v['size'] / 2 and t['layer'] in via_layers(v, b.copper):
                if pt_seg_dist(c, t['a'], t['b']) > 1e-3:
                    P = _on_line(t, c)
                    if math.dist(P, out[1 - i]) < 1e-3:
                        return False                 # nothing of t left but the stub
                    out[i] = P
                    add.append((t, P, c))
                    break
                out[i] = c
    stubs += add
    return tuple(out)


def _on_line(t, p):
    """p (a 0.01 mm grid key) projected back onto track t's centreline"""
    (ax, ay), (bx, by) = t['a'], t['b']
    L2 = (bx - ax) ** 2 + (by - ay) ** 2 or 1e-12
    s = min(1.0, max(0.0, ((p[0] - ax) * (bx - ax) + (p[1] - ay) * (by - ay)) / L2))
    return (round(ax + s * (bx - ax), 4), round(ay + s * (by - ay), 4))


def _label(C, R, w, xy, ly):
    """what node w is where the branch meets it (on layer ly): a pad there, else a pour, a
    track, a via"""
    for i, (f, p) in enumerate(C.pads):
        if R(('pad', i)) == w and pad_dist(p, xy) <= 0.3:
            return f"{f.ref}.{p['num']}"
    for j, fl in sorted(enumerate(C.fills), key=lambda jf: jf[1]['layer'] != ly):
        if R(('fill', j)) == w:
            return f"{fl['layer']} pour"
    k = next((k for k in C.ideal if k[0] == 'pt' and R(k) == w), None)
    if k:
        return f"{k[1]} track"
    for k, v in enumerate(C.vias):
        if R(('via', k)) == w or any(R(('vl', k, ly)) == w for ly in C.vlay[k]):
            return f"via {v['x']:.2f},{v['y']:.2f}"
    return next((f"{f.ref}.{p['num']}" for i, (f, p) in enumerate(C.pads) if R(('pad', i)) == w), 'copper')


def dead_desc(d):
    """'In2.Cu/B.Cu 25.1 mm, 7 via, 84 mm2 pour off B.Cu pour @145.45,120.20'"""
    what = ', '.join(x for x in (f"{d['mm']:.1f} mm" if d['tracks'] else '', f"{len(d['vias'])} via" if d['vias'] else '',
                                 f"{d['pour']:.0f} mm2 pour" if d['fills'] else '') if x)
    return f"{'/'.join(d['layers']) or 'via'} {what} off {d['off']}" + \
        (f" @{d['at'][0]:.2f},{d['at'][1]:.2f}" if d['at'] else '')


def drop_layer(b, zone, ly):
    """the layers zone keeps when its whole `ly` fill can go, else None: no pad joins that fill
    and the net's pad partition is the same without it, so it only joins vias that are joined
    on another layer. A dead fragment of a multi-layer zone can't be trimmed where the same area
    is live on another layer (CHG_BAT dff6a707 F/B/In2, 2026-10-28: B.Cu dropped on a scratch
    copy, 0 new DRC, U8.22 -> Q11.1 3.81 mohm through the fills unchanged)"""
    z = next((z for z in b.zones if z.uuid == zone), None)
    if not z or ly not in z[1] or len(z[1]) < 2:
        return None
    C = Copper(b, z[0])
    mine = [j for j, fl in enumerate(C.fills) if fl.get('zone') == zone and fl['layer'] == ly]
    roots = {C.ideal.find(('fill', j)) for j in mine}
    if not mine or any(C.ideal.find(('pad', i)) in roots for i in range(len(C.pads))):
        return None
    parts = lambda c: {frozenset(q['pads']) for q in c.pieces() if q['pads']}       # noqa: E731
    keep, gone = b.fills, {id(C.fills[j]) for j in mine}
    b.fills = [fl for fl in keep if id(fl) not in gone]
    try:
        same = parts(Copper(b, z[0])) == parts(C)
    finally:
        b.fills = keep
    return [l for l in z[1] if l != ly] if same else None


def dead_uuids(d):
    """the branch's items as uuid prefixes: tracks (a cut-back one as 'cut UUID'), vias, zones
    wholly in it"""
    return ([('cut ' if k else '' if k is None else 'mid ') + t['uuid'][:8] for t, _L, k in d['tracks']]
            + [v['uuid'][:8] for v in d['vias']] + sorted({'zone ' + fl['zone'][:8] for fl in d['fills'] if fl.get('zone')}))


def plane_stubs(ds):
    """a plane net's dead branches that hold a track and no pour: a GND stub ending in the pour
    beside a pad the pour already joins (Ryan deleted 12 by hand, 2026-10-28). A pad-less pour
    island and the vias holding it are the pour's business (`zones --voids`), never a rip-up's"""
    return [d for d in ds if d['tracks'] and not d['fills']]


def dead_all(b, nets=None):
    """{net: dead_copper} over every routed net; a plane (GND, or a net pouring > 20% of the
    board) gives only its track stubs (plane_stubs). A read-only command that asks more than once
    sets b._dead_memo = {} first (`review`: check and the do-nothing census); writers edit b in
    place, so nothing is memoised unless asked"""
    from kpcb_route import _planes
    memo = b.__dict__.get('_dead_memo')
    key = None if nets is None else tuple(nets)
    if memo is not None and key in memo:
        return memo[key]
    planes = {n for n, _l in _planes(b)}
    todo = sorted({o['net'] for o in b.tracks + b.vias if o['net']}) if nets is None else nets
    out = {n: d for n in todo for d in [dead_copper(b, n)] for d in [plane_stubs(d) if n in planes else d] if d}
    if memo is not None:
        memo[key] = out
    return out
