"""Exact any-angle clearance for a short centreline polyline of width w on one layer: segment-to-item
distances against the .kicad_dru type rules (track 0.127 outer / 0.1 inner, pad 0.1, via/zone/edge
as the netclass, RF classes and NetName pair rules never relaxed), via/PTH/NPTH hole clearances
(0.2 / 0.28 / 0.2), board edge, track keepouts and other nets' fills. No grid, no margin."""
import math
from kpcb_board import PolyIndex, pt_seg_dist
from kpcb_route import _kos
from kpcb_move import via_layers
from kpcb_copper import _local
import smooth2 as S

EPS = -0.002                                    # a 2 um safety margin: accept only d >= need + w/2 + 2 um


def seg_rect_dist(p, q, pad):
    """distance from segment pq to a pad's rectangle (roundrect/oval read as the rect: conservative)"""
    if pad['shape'] == 'circle':
        return max(0.0, pt_seg_dist((pad['x'], pad['y']), p, q) - pad['sx'] / 2)
    a, b = _local(pad, p), _local(pad, q)
    hx, hy = pad['sx'] / 2, pad['sy'] / 2
    # clip test: does the segment enter the box?
    n = max(2, int(math.dist(a, b) / 0.005) + 1)
    best = 1e9
    for k in range(n + 1):
        u = (a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n)
        dx, dy = max(abs(u[0]) - hx, 0.0), max(abs(u[1]) - hy, 0.0)
        best = min(best, math.hypot(dx, dy))
        if best == 0:
            break
    return best


class Exact:
    def __init__(self, b, net, ly, w, box, mine=()):
        self.b, self.net, self.ly, self.w = b, net, ly, w
        clr0, _, _ = S._clr0(b)
        dflt = clr0('', '')
        outer = ly in ('F.Cu', 'B.Cu')
        near = lambda bb: bb[2] >= box[0] and bb[0] <= box[2] and bb[3] >= box[1] and bb[1] <= box[3]  # noqa: E731

        def typed(other, kind):
            c = clr0(net, other)
            if c > dflt + 1e-9:
                return c
            return min(c, 0.127 if outer else 0.1) if kind == 'track' else min(c, 0.1) if kind == 'pad' else c
        self.trk, self.vias, self.pads, self.holes, self.fills = [], [], [], [], []
        for t in b.tracks:
            if t['layer'] == ly and t['net'] != net and t.get('uuid') not in mine:
                hw = t['w'] / 2
                if near((min(t['a'][0], t['b'][0]) - hw, min(t['a'][1], t['b'][1]) - hw, max(t['a'][0], t['b'][0]) + hw, max(t['a'][1], t['b'][1]) + hw)):
                    self.trk.append((t['a'], t['b'], hw + typed(t['net'], 'track')))
        for v in b.vias:
            if near((v['x'] - 1, v['y'] - 1, v['x'] + 1, v['y'] + 1)):
                if v['net'] != net and ly in via_layers(v, b.copper):
                    self.vias.append(((v['x'], v['y']), v['size'] / 2 + typed(v['net'], 'via')))
                self.holes.append(((v['x'], v['y']), v['drill'] / 2 + (0.2 if v['net'] != net else 0)))
        for f in b.fps.values():
            if not f.placed:
                continue
            for p in f.pads:
                if not near((p['x'] - 3, p['y'] - 3, p['x'] + 3, p['y'] + 3)):
                    continue
                if p['drill'] and p['net'] != net:
                    self.holes.append(((p['x'], p['y']), p['drill'] / 2 + (0.2 if p['kind'] == 'np_thru_hole' else 0.28)))
                if p['kind'] == 'np_thru_hole' or p['net'] == net and p['net']:
                    continue
                if p['drill'] or ly in p['layers'] or (outer and '*.Cu' in p['layers']):
                    self.pads.append((p, typed(p['net'], 'pad')))
        for fl in b.fills:
            if fl['layer'] == ly and fl['net'] != net and near(fl['bbox']):
                self.fills.append((PolyIndex(fl['pts']), clr0(net, fl['net'])))
        self.edge = [(u, v) for u, v in b.edge_segs if near((min(u[0], v[0]), min(u[1], v[1]), max(u[0], v[0]), max(u[1], v[1])))]
        self.ec = b.edge_clearance()
        self.kos = [idx for lys, idx, _nv, _np, notrk in _kos(b) if notrk and ly in lys and near(idx.bbox)]

    def seg(self, p, q):
        hw = self.w / 2
        for a, b_, need in self.trk:
            if S.seg_dist(p, q, a, b_) < need + hw - EPS:
                return False
        for c, need in self.vias:
            if pt_seg_dist(c, p, q) < need + hw - EPS:
                return False
        for c, need in self.holes:
            if pt_seg_dist(c, p, q) < need + hw - EPS:
                return False
        for pad, need in self.pads:
            if seg_rect_dist(p, q, pad) < need + hw - EPS:
                return False
        for u, v in self.edge:
            if S.seg_dist(p, q, u, v) < self.ec + hw - EPS:
                return False
        n = max(2, int(math.dist(p, q) / 0.02) + 1)
        pts = [(p[0] + (q[0] - p[0]) * k / n, p[1] + (q[1] - p[1]) * k / n) for k in range(n + 1)]
        for pi, need in self.fills:
            if any(pi.touches(x, need + hw) for x in pts):
                return False
        for idx in self.kos:
            if any(idx.touches(x, hw) for x in pts):
                return False
        return True

    def poly(self, pts):
        return all(self.seg(pts[i], pts[i + 1]) for i in range(len(pts) - 1))
