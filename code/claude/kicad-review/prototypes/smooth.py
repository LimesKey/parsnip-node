#!/usr/bin/env python3
"""smooth.py BOARD [-o OUT] [--nets A,B] [--layers L,..] [--box X0,Y0,X1,Y1] [--list N] [--overlaps]:
pull routed track chains tight and octilinear. BOARD must sit in a `kdrc.py scratch` copy
(netclasses and .kicad_dru pairs come from the .kicad_pro beside it). Re-run until it
reports 0, alternating with --overlaps, then `kdrc.py COPY drc --refill-save --baseline`.
`smooth.py --selftest` checks the geometry.

A chain is a run of same-net, same-layer, same-width segments between anchors (a pad,
a via, a junction of 3+ ends, a width change, a T into another track, an arc or a
locked track). Each is re-laid by a DP over its own vertices: any vertex may be joined
to any later one by a straight segment or a 45-degree dogleg, if that copper is clear
of every foreign item under the router's own model (kpcb_route.Grid: netclass +
.kicad_dru pair clearances, pads as drawn, edge, keepouts, power islands), or lies on
the chain's old centreline. Cost = length + bends (45: 0.5, 90: 3, acute: 15) + 2 and
10% per off-45 segment; the old chain is always a candidate, so nothing gets worse. A
result more than 9% longer is refused: octilinear costs at most 8.3% over a straight
any-angle line, so R and L grow only where an odd-angle run became a 45 dogleg. Left-over
90/acute corners are then chamfered as deep as clears. Proposals of different nets
are checked against each other; the bigger saving wins. New copper may touch its own
net's other copper only within 0.5 mm of where the old chain did (a sense tap must not
gain a second contact); GND is exempt.

RF, USB and diff-pair nets are skipped (impedance, coupling). Teardrops on changed
segments are deleted; kicad-cli's refill (`--refill-save`) re-creates them.
ponytail: per-chain Grid build at 0.02 mm, ~60 s for parsnip; a shared per-net window if it gets slow."""
import sys, os, math, re, argparse
from collections import defaultdict
sys.path.insert(0, __file__.rsplit('/', 2)[0] + '/scripts')
from kpcb_board import Board, PolyIndex, bbox, pad_box, pt_seg_dist
from kpcb_copper import _local
from kpcb_route import Grid, _iv, _legs, _pad_shape
from kpcb_move import via_layers
from kpcb_zones import _clearance_fn
from kpcb_edit import PcbText, tear_edits, num

SKIP_CLASS = ('RF_50OHM', 'RF_50OHM_MS', 'USB_DIFF')
TOL = 0.0015                                        # node merge, "on the old centreline"
q = lambda p: (round(p[0], 3), round(p[1], 3))      # noqa: E731


def ang(a, b):
    return math.degrees(math.atan2(b[1] - a[1], b[0] - a[0]))


def turn(d1, d2):
    return abs((d2 - d1 + 180) % 360 - 180)


def octi(a, b):
    r = ang(a, b) % 45
    return min(r, 45 - r) < 0.3


def bend(th):
    return 0 if th < 0.5 else 0.5 if th < 46 else 3.0 if th < 91 else 15.0


def cost(pts):
    c = sum(math.dist(pts[i], pts[i + 1]) * (1 if octi(pts[i], pts[i + 1]) else 1.1) + (0 if octi(pts[i], pts[i + 1]) else 2.0)
            for i in range(len(pts) - 1))
    return c + sum(bend(turn(ang(pts[i - 1], pts[i]), ang(pts[i], pts[i + 1]))) for i in range(1, len(pts) - 1))


def seg_dist(p, q_, r, s):
    return min(pt_seg_dist(p, r, s), pt_seg_dist(q_, r, s), pt_seg_dist(r, p, q_), pt_seg_dist(s, p, q_)) \
        if not _cross(p, q_, r, s) else 0.0


def _cross(p, q_, r, s):
    d = lambda a, b, c: (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])  # noqa: E731
    return d(p, q_, r) * d(p, q_, s) < 0 and d(r, s, p) * d(r, s, q_) < 0


def in_pad(p, pt, grow=0.0):
    if p['shape'] == 'circle':
        return math.dist(pt, (p['x'], p['y'])) <= p['sx'] / 2 + grow
    u, v = _local(p, pt)
    return abs(u) <= p['sx'] / 2 + grow and abs(v) <= p['sy'] / 2 + grow


def chains(b, skip):
    """[(net, layer, w, [track idx in order], [points])] between anchors"""
    segs = defaultdict(list)                        # (net, layer) -> idx
    arcs = set()
    for i, t in enumerate(b.tracks):
        if t['net']:
            segs[(t['net'], t['layer'])].append(i)
            if math.dist(t['mid'], ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)) > 1e-4:
                arcs.add(i)
    pads = defaultdict(list)
    for f in b.fps.values():
        if f.placed:
            for p in f.pads:
                if p['net']:
                    pads[p['net']].append(p)
    vias = defaultdict(list)
    for v in b.vias:
        vias[v['net']].append(v)
    out = []
    for (net, ly), idx in segs.items():
        if net in skip:
            continue
        lpads = [p for p in pads[net] if p['drill'] or ly in p['layers'] or
                 (ly in ('F.Cu', 'B.Cu') and '*.Cu' in p['layers'])]
        ends = defaultdict(list)
        for i in idx:
            ends[q(b.tracks[i]['a'])].append(i)
            ends[q(b.tracks[i]['b'])].append(i)
        frozen = {i for i in idx if i in arcs or b.tracks[i]['locked']}
        anchor = set()
        for n, lst in ends.items():
            ts = [b.tracks[i] for i in lst]
            if len(lst) != 2 or ts[0]['w'] != ts[1]['w'] or \
                    any(math.dist(n, (v['x'], v['y'])) <= v['size'] / 2 for v in vias[net]) or \
                    any(in_pad(p, n) for p in lpads):
                anchor.add(n)
            for i in idx:                           # a T into another track's interior
                t = b.tracks[i]
                if i not in lst and pt_seg_dist(n, t['a'], t['b']) <= t['w'] / 2 and \
                        min(math.dist(n, t['a']), math.dist(n, t['b'])) > t['w'] / 2:
                    frozen.add(i)
                    anchor.add(n)
        for i in idx:                               # body over a pad/via it does not end on
            t = b.tracks[i]
            L = max(1, int(t['len'] / 0.05))
            pts = [(t['a'][0] + (t['b'][0] - t['a'][0]) * k / L, t['a'][1] + (t['b'][1] - t['a'][1]) * k / L)
                   for k in range(L + 1)]
            for p in lpads:
                if abs(p['x'] - t['mid'][0]) > t['len'] / 2 + 5 or abs(p['y'] - t['mid'][1]) > t['len'] / 2 + 5:
                    continue
                if not (in_pad(p, t['a']) or in_pad(p, t['b'])) and any(in_pad(p, x, t['w'] / 2) for x in pts):
                    frozen.add(i)
            for v in vias[net]:
                c = (v['x'], v['y'])
                if math.dist(c, t['a']) > v['size'] / 2 and math.dist(c, t['b']) > v['size'] / 2 and \
                        pt_seg_dist(c, t['a'], t['b']) <= (v['size'] + t['w']) / 2:
                    frozen.add(i)
        for i in frozen:
            anchor |= {q(b.tracks[i]['a']), q(b.tracks[i]['b'])}
        seen = set(frozen)
        for n0 in sorted(anchor):
            for i0 in ends[n0]:
                if i0 in seen:
                    continue
                order, pts, n = [], [n0], n0
                i = i0
                while True:
                    seen.add(i)
                    order.append(i)
                    t = b.tracks[i]
                    n = q(t['b']) if q(t['a']) == n else q(t['a'])
                    pts.append(n)
                    if n in anchor:
                        break
                    nxt = [j for j in ends[n] if j != i]
                    if not nxt or nxt[0] in seen:
                        break
                    i = nxt[0]
                # exact coordinates: the anchors' own, not the rounded keys
                ex = [b.tracks[order[0]]['a'] if q(b.tracks[order[0]]['a']) == pts[0] else b.tracks[order[0]]['b']]
                for k, i in enumerate(order):
                    t = b.tracks[i]
                    ex.append(t['b'] if q(t['a']) == pts[k] else t['a'])
                out.append((net, ly, b.tracks[order[0]]['w'], order, ex))
    return out


RES = 0.02                                          # grid pitch; the nearest point is <= RES/sqrt2 away


class Clear:
    """is a centreline segment clear for this chain: every sample's nearest grid point free
    under the router's model grown by RES/sqrt2 (so a free point is free wherever it rounds
    from), or the sample on the chain's old centreline"""

    def __init__(self, b, net, ly, w, pts, order):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        box = (min(xs) - 0.6, min(ys) - 0.6, max(xs) + 0.6, max(ys) + 0.6)
        self.g = g = Grid(b, net, box, RES, w, 0.6, 0.3, [ly], extra_clr=RES * 0.8)
        self.blk, self.old = g.blk[ly], list(zip(pts, pts[1:]))
        # same-net copper the chain must not newly touch (a sense tap or feedback top that
        # touched its power copper somewhere new would move its sense point): `own` is that
        # copper grown by w/2, `allow` 0.5 mm round where the old chain already touched it.
        # The anchors' own pads/vias are exempt; GND is exempt (the plane is everywhere).
        N = g.W * g.H
        self.own, self.allow = bytearray(N), bytearray(N)
        if net == 'GND':
            return
        R, ends = w / 2 + 0.005, (pts[0], pts[-1])
        near = lambda bb: bb[2] >= box[0] and bb[0] <= box[2] and bb[3] >= box[1] and bb[1] <= box[3]  # noqa: E731
        mine = {b.tracks[i]['uuid'] for i in order}
        for f in b.fps.values():
            for p in f.pads if f.placed else ():
                if p['net'] == net and (p['drill'] or ly in p['layers']) and near(pad_box(p)) and \
                        not any(in_pad(p, e) for e in ends):
                    _paint(g, self.own, _pad_shape(p, pad_box(p)), R)
        for v in b.vias:
            if v['net'] == net and ly in via_layers(v, b.copper) and \
                    all(math.dist(e, (v['x'], v['y'])) > v['size'] / 2 for e in ends):
                _paint(g, self.own, ('disk', v['x'], v['y'], v['size'] / 2), R)
        for t in b.tracks:
            if t['net'] == net and t['layer'] == ly and t['uuid'] not in mine and \
                    near(bbox([t['a'], t['b']])):
                _paint(g, self.own, ('cap', t['a'], t['b'], t['w'] / 2), R)
        for fl in b.fills:
            if fl['net'] == net and fl['layer'] == ly and near(fl['bbox']):
                g.fill_rows(PolyIndex(fl['pts']), self.own)
                ring = fl['pts']
                for u, v in zip(ring, ring[1:] + ring[:1]):
                    _paint(g, self.own, ('cap', u, v, 0.0), R)
        for a, c in self.old:
            n = max(1, math.ceil(math.dist(a, c) / (RES / 2)))
            for k in range(n + 1):
                p = (a[0] + (c[0] - a[0]) * k / n, a[1] + (c[1] - a[1]) * k / n)
                if self.own[self._k(p)]:
                    _paint(g, self.allow, ('disk', p[0], p[1], 0.0), 0.5)

    def _k(self, p):
        g = self.g
        i, j = round((p[0] - g.x0) / g.res), round((p[1] - g.y0) / g.res)
        return j * g.W + i if 0 <= i < g.W and 0 <= j < g.H else 0

    def on_old(self, p):
        return any(pt_seg_dist(p, a, c) <= 2e-4 for a, c in self.old)     # TOL here let 0.0007 mm through

    def free(self, p):
        g = self.g
        i, j = round((p[0] - g.x0) / g.res), round((p[1] - g.y0) / g.res)
        return 0 <= i < g.W and 0 <= j < g.H and not self.blk[j * g.W + i]

    def seg(self, a, c):
        n = max(1, math.ceil(math.dist(a, c) / (RES / 2)))
        for k in range(n + 1):
            p = (a[0] + (c[0] - a[0]) * k / n, a[1] + (c[1] - a[1]) * k / n)
            if not self.free(p) and not self.on_old(p):
                return False
            k = self._k(p)
            if self.own[k] and not self.allow[k]:
                return False
        return True

    def poly(self, pts):
        return all(self.seg(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _paint(g, ras, shape, R):
    """the Grid's own scanline painter: shape grown by R into raster ras"""
    y_lo, y_hi, f = _iv(shape, R)
    for j in range(max(0, math.ceil((y_lo - g.y0) / g.res)), min(g.H - 1, math.floor((y_hi - g.y0) / g.res)) + 1):
        iv = f(g.y0 + j * g.res)
        if iv:
            i0 = max(0, math.ceil((iv[0] - g.x0) / g.res - 1e-9))
            i1 = min(g.W - 1, math.floor((iv[1] - g.x0) / g.res + 1e-9))
            if i0 <= i1:
                ras[j * g.W + i0:j * g.W + i1 + 1] = b'\x01' * (i1 - i0 + 1)


def connectors(a, c):
    """straight, or the two 45-degree doglegs, from a to c"""
    dx, dy = c[0] - a[0], c[1] - a[1]
    out = [[a, c]]
    if octi(a, c):
        return out
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    ax, ay = abs(dx), abs(dy)
    if ax >= ay:
        mids = [(a[0] + sx * (ax - ay), a[1]), (a[0] + sx * ay, a[1] + sy * ay)]
    else:
        mids = [(a[0], a[1] + sy * (ay - ax)), (a[0] + sx * ax, a[1] + sy * ax)]
    return out + [[a, m, c] for m in mids]


def tidy(pts):
    """drop zero-length and collinear interior points"""
    out = [pts[0]]
    for p in pts[1:]:
        if math.dist(p, out[-1]) > 1e-4:
            out.append(p)
    k = 1
    while k < len(out) - 1:
        if turn(ang(out[k - 1], out[k]), ang(out[k], out[k + 1])) < 0.05:
            del out[k]
        else:
            k += 1
    return out


def solve(pts, ck, span=30):
    """DP over the old vertices: state (vertex, incoming direction)"""
    n = len(pts) - 1
    best = {(0, None): (0.0, None)}                 # (j, dir) -> (cost, (prev state, connector))
    for i in range(n):
        states = [(s, v) for s, v in best.items() if s[0] == i]
        if not states:
            continue
        for j in range(i + 1, min(n, i + span) + 1):
            for con in connectors(pts[i], pts[j]):
                con = tidy(con)
                if len(con) < 2:
                    continue
                if j > i + 1 or len(con) > 2 or con[-1] != pts[j]:
                    if not ck.poly(con):
                        continue
                inner = cost(con)
                d0, d1 = ang(con[0], con[1]), ang(con[-2], con[-1])
                for (si, din), (c0, _bp) in states:
                    c = c0 + inner + (bend(turn(din, d0)) if din is not None else 0)
                    key = (j, round(d1, 1))
                    if c < best.get(key, (1e18,))[0]:
                        best[key] = (c, ((si, din), con))
    ends = [(v[0], s) for s, v in best.items() if s[0] == n]
    if not ends:
        return pts
    s = min(ends)[1]
    parts = []
    while best[s][1]:
        prev, con = best[s][1]
        parts.append(con)
        s = (prev[0], prev[1])
    out = [pts[0]]
    for con in reversed(parts):
        out += con[1:]
    return tidy(out)


def chamfer(pts, ck, w, fixed_ends=True):
    """cut each remaining 90-degree or sharper corner with the deepest 45-degree leg that clears"""
    pts = list(pts)
    k = 1
    while k < len(pts) - 1:
        a, v, c = pts[k - 1], pts[k], pts[k + 1]
        th = turn(ang(a, v), ang(v, c))
        if th < 89 or th > 91 or not (octi(a, v) and octi(v, c)):
            k += 1
            continue
        L1, L2 = math.dist(a, v), math.dist(v, c)
        m1 = L1 if k == 1 else L1 / 2
        m2 = L2 if k == len(pts) - 2 else L2 / 2
        d = min(m1, m2, 4 * w + 0.5)
        done = False
        while d >= max(0.6 * w, 0.1):
            p1 = (v[0] + (a[0] - v[0]) * d / L1, v[1] + (a[1] - v[1]) * d / L1)
            p2 = (v[0] + (c[0] - v[0]) * d / L2, v[1] + (c[1] - v[1]) * d / L2)
            if ck.seg(p1, p2):
                pts[k:k + 1] = [p1, p2]
                done = True
                break
            d *= 0.8
        k += 2 if done else 1
    return tidy(pts)


def overlaps(b, skip):
    """[(net, layer, 'del', i) | (net, layer, 'cut', i, end, new point)] for same-net, same-layer
    pairs leaving a node in the same direction, shorter one ti, longer tj, collinear.
    A hairpin tip (nothing else at the node): ti goes and tj is cut back to the first thing
    attached to it (a via or pad its centreline enters, a track end on it); never when a
    same-net pour touches the cut span. Elsewhere: ti goes if no wider than tj (covered),
    else tj's doubled span is cut back to ti's far end. Only copper goes."""
    ends, pads, vias = defaultdict(list), defaultdict(list), defaultdict(list)
    for i, t in enumerate(b.tracks):
        if t['net'] and t['net'] not in skip:
            ends[(t['net'], t['layer'], q(t['a']))].append(i)
            ends[(t['net'], t['layer'], q(t['b']))].append(i)
    for f in b.fps.values():
        for p in f.pads if f.placed else ():
            if p['net']:
                pads[p['net']].append(p)
    for v in b.vias:
        vias[v['net']].append(v)
    far = lambda t, n: t['b'] if q(t['a']) == n else t['a']  # noqa: E731
    out, used = [], set()
    for (net, ly, n), lst in ends.items():
        lpads = [p for p in pads[net] if p['drill'] or ly in p['layers']]
        for x in range(len(lst)):
            for y in range(len(lst)):
                i, j = lst[x], lst[y]
                ti, tj = b.tracks[i], b.tracks[j]
                if i == j or i in used or j in used or ti['len'] > tj['len'] or (ti['len'] == tj['len'] and i > j) \
                        or ti['len'] < 1e-3 or turn(ang(n, far(ti, n)), ang(n, far(tj, n))) > 1 \
                        or pt_seg_dist(far(ti, n), tj['a'], tj['b']) > 0.005:
                    continue
                tip = len(lst) == 2 and not any(in_pad(p, n) for p in lpads) and \
                    not any(math.dist(n, (v['x'], v['y'])) <= v['size'] / 2 for v in vias[net]) and \
                    not any(pt_seg_dist(n, t['a'], t['b']) <= t['w'] / 2 for k, t in enumerate(b.tracks)
                            if k not in (i, j) and t['net'] == net and t['layer'] == ly)
                if not tip:
                    if ti['w'] <= tj['w']:
                        out.append((net, ly, 'del', i))
                    elif tj['len'] - ti['len'] < 1e-3:            # same span, tj the narrower
                        out.append((net, ly, 'del', j))
                    else:
                        out.append((net, ly, 'cut', j, n, far(ti, n)))
                    used |= {i, j}
                    continue
                # the tip: how far along tj from n is the first attachment?
                fj, L = far(tj, n), tj['len']
                at = lambda s: (n[0] + (fj[0] - n[0]) * s / L, n[1] + (fj[1] - n[1]) * s / L)  # noqa: E731
                cuts = []
                for v in vias[net]:
                    c = (v['x'], v['y'])
                    s = ((c[0] - n[0]) * (fj[0] - n[0]) + (c[1] - n[1]) * (fj[1] - n[1])) / L
                    if 0 < s <= L and math.dist(at(s), c) <= v['size'] / 2 and ly in via_layers(v, b.copper):
                        cuts.append(s)
                for k, t in enumerate(b.tracks):
                    if k in (i, j) or t['net'] != net or t['layer'] != ly:
                        continue
                    for e in (t['a'], t['b']):
                        s = ((e[0] - n[0]) * (fj[0] - n[0]) + (e[1] - n[1]) * (fj[1] - n[1])) / L
                        if 0 < s <= L and math.dist(at(s), e) <= tj['w'] / 2:
                            cuts.append(s)
                steps = max(1, int(L / 0.02))
                for p in lpads:
                    hit = [k * L / steps for k in range(steps + 1) if in_pad(p, at(k * L / steps))]
                    graze = any(in_pad(p, at(k * L / steps), tj['w'] / 2) for k in range(steps + 1))
                    if hit:
                        cuts.append(hit[0])
                    elif graze:
                        cuts.append(-1)                         # touches without entering: leave it
                s = min(cuts, default=L)
                if s <= 0 or s >= L - 1e-3:
                    continue
                span = [at(k * s / 20) for k in range(21)]
                if any(fl['net'] == net and fl['layer'] == ly and any(p in PolyIndex(fl['pts']) for p in span)
                       for fl in b.fills if fl['bbox'][0] <= max(n[0], fj[0]) and fl['bbox'][2] >= min(n[0], fj[0])
                       and fl['bbox'][1] <= max(n[1], fj[1]) and fl['bbox'][3] >= min(n[1], fj[1])):
                    continue
                out += [(net, ly, 'del', i), (net, ly, 'cut', j, n, at(s))]
                used |= {i, j}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('board')
    ap.add_argument('-o', '--out')
    ap.add_argument('--nets', default='')
    ap.add_argument('--layers', default='')
    ap.add_argument('--box', default='')
    ap.add_argument('--list', type=int, default=15)
    ap.add_argument('--overlaps', action='store_true', help='only drop doubled copper and trim hairpin tips')
    a = ap.parse_args()
    b = Board(a.board)
    if not os.path.exists(os.path.splitext(a.board)[0] + '.kicad_pro'):
        raise SystemExit("no .kicad_pro beside the board: netclasses and .kicad_dru pair clearances would be "
                         "default, RF nets unprotected. Run it on a `kdrc.py scratch` copy.")
    skip = {n for pair in _legs(b) for n in pair}
    skip |= {n for n in {t['net'] for t in b.tracks} if (b.netclass(n) or 'Default') in SKIP_CLASS}
    want = set(a.nets.split(',')) if a.nets else None
    lays = set(a.layers.split(',')) if a.layers else None
    box = tuple(float(v) for v in a.box.split(',')) if a.box else None
    if a.overlaps:
        ops = [o for o in overlaps(b, skip) if (not want or o[0] in want) and (not lays or o[1] in lays)]
        for o in ops[:a.list]:
            t = b.tracks[o[3]]
            print(f"  {o[2]:3s} {o[0]:24s} {o[1]:6s} w{t['w']:g} {q(t['a'])}->{q(t['b'])}" +
                  (f"  end {q(o[4])} -> {q(o[5])}" if o[2] == 'cut' else ''))
        print(f"overlaps: {sum(o[2] == 'del' for o in ops)} deleted, {sum(o[2] == 'cut' for o in ops)} cut back")
        if a.out:
            t = PcbText(a.board)
            for o in ops:
                tr = b.tracks[o[3]]
                if o[2] == 'del':
                    t.sub(tr['uuid'], '')
                    continue
                tag = 'start' if q(tr['a']) == q(o[4]) else 'end'
                txt, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(o[5][0])} {num(o[5][1])})", t.text(tr['uuid']), count=1)
                assert c == 1
                t.sub(tr['uuid'], txt)
            n = len(tear_edits(t, [b.tracks[o[3]] for o in ops]))
            t.save(a.out)
            print(f"wrote {a.out}: {len(ops)} edit(s), {n} teardrop(s) dropped")
        return 0
    clr = _clearance_fn(b)[0]
    props = []
    chs = chains(b, skip)
    for net, ly, w, order, pts in chs:
        if (want and net not in want) or (lays and ly not in lays):
            continue
        if box and not all(box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3] for p in pts):
            continue
        c0 = cost(pts)
        if len(pts) == 2 and octi(*pts):
            continue
        ck = Clear(b, net, ly, w, pts, order)
        new = chamfer(solve(pts, ck), ck, w)
        L0 = sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        L1 = sum(math.dist(new[i], new[i + 1]) for i in range(len(new) - 1))
        c1 = cost(new)
        if c1 < c0 - 0.05 and L1 <= L0 * 1.09 + 0.02:      # octilinear is <= 8.3% longer than any-angle
            props.append((c0 - c1, net, ly, w, order, pts, new, L0, L1))
    # proposals of different nets must clear each other
    props.sort(key=lambda p: -p[0])
    taken = []
    for p in props:
        segs = list(zip(p[6], p[6][1:]))
        bad = False
        for o in taken:
            if o[2] != p[2] or o[1] == p[1]:
                continue
            need = clr(p[1], o[1]) + (p[3] + o[3]) / 2
            if any(seg_dist(s0, s1, t0, t1) < need for s0, s1 in segs for t0, t1 in zip(o[6], o[6][1:])):
                bad = True
                break
        if not bad:
            taken.append(p)
    print(f"chains {len(chs)}, improved {len(props)}, kept after cross-check {len(taken)}")
    def census(pl):
        c = defaultdict(int)
        for pts in pl:
            for i in range(1, len(pts) - 1):
                th = turn(ang(pts[i - 1], pts[i]), ang(pts[i], pts[i + 1]))
                c['45' if th < 46 else '90' if th < 91 else 'acute'] += 1
            c['odd'] += sum(1 for i in range(len(pts) - 1) if not octi(pts[i], pts[i + 1]))
            c['segs'] += len(pts) - 1
            c['mm'] += sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
        return dict(c)
    print('before', census([p[5] for p in taken]))
    print('after ', census([p[6] for p in taken]))
    for p in taken[:a.list]:
        print(f"  {p[1]:28s} {p[2]:6s} w{p[3]:g}  {len(p[5]) - 1}->{len(p[6]) - 1} segs  "
              f"{p[7]:.2f}->{p[8]:.2f} mm  at {p[5][0][0]:.2f},{p[5][0][1]:.2f}")
    if not a.out:
        return 0
    t = PcbText(a.board)
    kept, dead = 0, []
    for _g, net, ly, w, order, pts, new, _L0, _L1 in taken:
        old = {frozenset((q(b.tracks[i]['a']), q(b.tracks[i]['b']))): i for i in order}
        newsegs = list(zip(new, new[1:]))
        gone = set(order)
        fresh = []
        for s in newsegs:
            k = frozenset((q(s[0]), q(s[1])))
            if k in old:
                gone.discard(old.pop(k))
            else:
                fresh.append(s)
        # an anchor-end segment that only changed length keeps its uuid (and teardrop)
        for end, i in ((new[0], order[0]), (new[-1], order[-1])):
            if i not in gone:
                continue
            tr = b.tracks[i]
            for s in fresh:
                if q(end) not in (q(s[0]), q(s[1])):
                    continue
                o_far = tr['b'] if q(tr['a']) == q(end) else tr['a']
                n_far = s[1] if q(s[0]) == q(end) else s[0]
                if abs(turn(ang(end, o_far), ang(end, n_far))) < 0.05:
                    txt = t.text(tr['uuid'])
                    tag = 'end' if q(tr['a']) == q(end) else 'start'
                    txt, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(n_far[0])} {num(n_far[1])})", txt, count=1)
                    assert c == 1
                    t.sub(tr['uuid'], txt)
                    gone.discard(i)
                    fresh.remove(s)
                    kept += 1
                break
        for i in gone:
            t.sub(b.tracks[i]['uuid'], '')
            dead.append(b.tracks[i])
        for s in fresh:
            t.segment(net, ly, w, s[0], s[1])
    dropped = len(tear_edits(t, dead))               # once: two chains can share a teardrop
    t.save(a.out)
    print(f"wrote {a.out}: {len(taken)} chains, {kept} end segments kept their uuid, "
          f"{dropped} teardrop(s) dropped")
    return 0


def selftest():
    """the geometry on open board (every candidate clear): staircases collapse, 90s chamfer"""
    class Open:
        seg = poly = staticmethod(lambda *a: True)
    assert tidy([(0, 0), (1, 0), (2, 0)]) == [(0, 0), (2, 0)]
    stair = [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2), (3, 2)]
    new = solve(stair, Open())
    assert len(new) <= 3 and all(octi(new[i], new[i + 1]) for i in range(len(new) - 1)), new
    assert cost(new) < cost(stair)
    L = chamfer([(0, 0), (2, 0), (2, 2)], Open(), 0.2)
    assert len(L) == 4 and all(turn(ang(L[i - 1], L[i]), ang(L[i], L[i + 1])) < 46 for i in (1, 2)), L
    assert connectors((0, 0), (3, 1))[1:] == [[(0, 0), (2, 0), (3, 1)], [(0, 0), (1, 1), (3, 1)]]
    print('selftest ok')


if __name__ == '__main__':
    if sys.argv[1:] == ['--selftest']:
        sys.exit(selftest())
    sys.exit(main())
