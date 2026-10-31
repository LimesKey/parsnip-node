"""kpcb.py `smooth`: clean up routed copper on a scratch copy - staircases, odd-angle runs,
unchamfered 90s, acute corners, doubled segments and hairpin spurs. One pass per run,
-o OUT writes it (`--all` repeats every pass until a round changes nothing); then
`kdrc.py OUT drc --refill-save --baseline BEFORE.json` is the verdict.

  smooth            per chain, a DP over its own vertices: any vertex may join any later
                    one by a straight segment or a 45 dogleg if clear, then leftover 90s
                    are chamfered as deep as clears
  smooth --overlaps doubled same-net copper deleted, hairpin tips cut back (kept when it is a
                    via's only centred end); then two same-width runs that enter a small SMD
                    pad at different points, joined only through it and bridge stubs inside
                    it, re-laid as one chain reaching as deep into the pad as the old copper
  smooth --corners  every visible corner >= 46 deg at a node of exactly two tracks (any
                    widths, so width steps too): P1/P2 on the two legs (0.05 mm steps,
                    <= 1.5 mm), joined straight or by a 45 dogleg, every new turn <= 45
  smooth --square   an off-45 segment ending inside a same-net pad slides that end
                    within the pad onto the nearest octilinear line (<= 0.5 mm)
  smooth --pads     a dead-end trace whose cap only reaches a same-net pad's edge (centreline
                    end outside it or < 0.05 mm in) is run onto the pad centre at its own width: its
                    end slid along its line, its last segment re-laid, else a stub
  smooth --twins    two same-net runs side by side into one pad (a star) re-laid as a daisy
                    chain: the run from the farther start deleted, that start joined to the
                    nearer one's start (not in --all: it changes the topology)
  smooth --micro    segments under 0.05 mm (`tracks` `stub`): one joining something at both ends
                    collapsed into its neighbours (their shared ends moved together), one with a
                    free end deleted only when other same-net copper wholly covers it
  smooth --jogs     lane jogs collapsed (kpcb_jogs.py), lifting and re-placing one blocking free
                    GND via; --nudge also drags one blocking signal via <= 0.3 mm (not in --all)

A chain is a run of same-net, same-layer, same-width segments between anchors: a pad or
via its end cap touches (KiCad joins on the cap, not the centreline end), a junction of
3+ ends, a width change, a T into another track, an arc or a locked track; a segment whose
body crosses a pad or via it does not end on is frozen. Cost = length + bends (45: 0.5,
90: 3, acute: 15) + 2 and 10% per off-45 segment; the old chain is always a candidate. A
result more than 9% longer is refused (octilinear is at most 8.3% over any-angle).

Clearance (`Clear`): route's Grid at 0.02 mm, grown by 0.8 cell so a free grid point is
free wherever it rounds from - netclass, .kicad_dru pair AND type rules (track 0.127 outer
/ 0.1 inner, pad 0.1), edge, keepouts, power islands as copper, planes passable (the
refill flows round). Where the grid refuses, `Exact` decides: segment-to-item distances,
no grid, 2 um margin that only ever shrinks what is accepted. New copper may touch its
own net's other copper only within 0.5 mm of where the old chain did (a sense tap keeps
its one contact); GND is exempt. Proposals of different nets are checked against each
other, the bigger saving wins. RF, USB and diff-pair nets are skipped. Teardrops on
changed tracks are deleted; the refill re-creates those whose track is <= 0.9x the
pad/via (td_width_to_size_filter_ratio).
ponytail: one Grid per chain at 0.02 mm, ~60 s on parsnip; a shared per-net window if it gets slow."""
import glob, math, os, re
from collections import defaultdict, Counter
from kpcb_board import Board, PolyIndex, _pidx, bbox, pad_box, pt_seg_dist, seg_seg_dist, seg_box_dist
from kpcb_copper import _local, pad_dist, pad_within
from kpcb_route import Grid, _iv, _legs, _pad_shape, _planes, _kos
from kpcb_move import via_layers, is_arc
from kpcb_zones import _clearance_fn
from kpcb_edit import PcbText, tear_edits, num

RES = 0.02                                          # Clear's grid pitch
OFFS = (0.25, 0.75, 1 / 3, 2 / 3)                   # off-centre H+45+H splits solve() tries last
TWIN = 1.0                                          # side by side this far = twins
GRAZE = 0.05                                        # a centreline end this close inside a pad edge grazes it
EPS = 0.002                                         # Exact accepts d >= need + w/2 + 2 um
q = lambda p: (round(p[0], 3), round(p[1], 3))      # noqa: E731


def skipped(b):
    """nets smooth/widen never touch: diff-pair legs and RF/USB/DIFF netclasses (impedance)"""
    out = {n for pair in _legs(b) for n in pair}
    return out | {n for n in {t['net'] for t in b.tracks} if n and
                  any(k in (b.netclass(n) or '').upper() for k in ('RF', 'USB', 'DIFF'))}


# ---------------------------------------------------------------- geometry

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


def length(pts):
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def in_pad(p, pt, grow=0.0):
    """pt inside pad p's copper as drawn (roundrect/oval corners round, custom pads their real
    polygons) grown by `grow`; within 1 um of the edge is on it (an end laid on D7.5's edge
    reads 0.2 um out against the drawn shape)"""
    return pad_within(p, pt, grow + 1e-3, rounded=True)


def on_layer(p, ly):
    return p['drill'] or ly in p['layers'] or (ly in ('F.Cu', 'B.Cu') and '*.Cu' in p['layers'])


def connectors(a, c, fs=(0.5,)):
    """straight, the two 45-degree doglegs, then straight + 45 + straight with the 45 in
    the middle (an off-45 pad->via escape boxed in for both doglegs: J7.A9 VBUS, 2026-10-27;
    one bend more, so the DP takes it only when a dogleg cannot; fs: where the 45 sits along
    the straight part, 0.5 = centred), from a to c"""
    dx, dy = c[0] - a[0], c[1] - a[1]
    out = [[a, c]]
    if octi(a, c):
        return out
    sx, sy = math.copysign(1, dx), math.copysign(1, dy)
    ax, ay = abs(dx), abs(dy)
    if ax >= ay:
        mids = [(a[0] + sx * (ax - ay), a[1]), (a[0] + sx * ay, a[1] + sy * ay)]
        h3 = [[(a[0] + sx * (ax - ay) * f, a[1]), (a[0] + sx * ((ax - ay) * f + ay), c[1])] for f in fs]
    else:
        mids = [(a[0], a[1] + sy * (ay - ax)), (a[0] + sx * ax, a[1] + sy * ax)]
        h3 = [[(a[0], a[1] + sy * (ay - ax) * f), (c[0], a[1] + sy * ((ay - ax) * f + ax))] for f in fs]
    return out + [[a, m, c] for m in mids] + [[a] + m2 + [c] for m2 in h3]


def simplify(pts):
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


def census(polys):
    """turn/segment tally over polylines: 45, 90, acute, off-45 segments, mm"""
    c = Counter()
    for pts in polys:
        for i in range(1, len(pts) - 1):
            th = turn(ang(pts[i - 1], pts[i]), ang(pts[i], pts[i + 1]))
            c['45' if th < 46 else '90' if th < 91 else 'acute'] += 1
        c['off45'] += sum(1 for i in range(len(pts) - 1) if not octi(pts[i], pts[i + 1]))
        c['segs'] += len(pts) - 1
        c['mm'] += length(pts)
    c['mm'] = round(c['mm'], 2)
    return dict(c)


# ---------------------------------------------------------------- the board, indexed

class Index:
    """per-net pads (placed), vias, track ends by (net, layer, node) and tracks by (net, layer)"""

    def __init__(self, b):
        self.pads, self.vias = defaultdict(list), defaultdict(list)
        for f in b.fps.values():
            for p in f.pads if f.placed else ():
                if p['net']:
                    self.pads[p['net']].append(p)
        for v in b.vias:
            self.vias[v['net']].append(v)
        self.ends, self.trk = defaultdict(list), defaultdict(list)
        for i, t in enumerate(b.tracks):
            if t['net']:
                self.trk[(t['net'], t['layer'])].append(i)
                self.ends[(t['net'], t['layer'], q(t['a']))].append(i)
                self.ends[(t['net'], t['layer'], q(t['b']))].append(i)

    def on_via(self, net, ly, pt, grow=0.0, cu=None):
        return any(math.dist(pt, (v['x'], v['y'])) <= v['size'] / 2 + grow and (cu is None or ly in via_layers(v, cu))
                   for v in self.vias[net])

    def on_pad(self, net, ly, pt, grow=0.0):
        return any(on_layer(p, ly) and in_pad(p, pt, grow) for p in self.pads[net])


def chains(b, skip, ix=None, only=None):
    """[(net, layer, w, [track idx in order], [points])] between anchors (`only`: these nets)"""
    ix = ix or Index(b)
    segs = defaultdict(list)
    for i, t in enumerate(b.tracks):
        if t['net'] and t['net'] not in skip and (only is None or t['net'] in only):
            segs[(t['net'], t['layer'])].append(i)
    out = []
    for (net, ly), idx in segs.items():
        lpads = [p for p in ix.pads[net] if on_layer(p, ly)]
        ends = defaultdict(list)
        for i in idx:
            ends[q(b.tracks[i]['a'])].append(i)
            ends[q(b.tracks[i]['b'])].append(i)
        frozen = {i for i in idx if is_arc(b.tracks[i]) or b.tracks[i]['locked']}
        anchor = set()
        hop = lambda i: {j for e in (b.tracks[i]['a'], b.tracks[i]['b']) for j in ends[q(e)]}     # noqa: E731
        near2 = {i: {k for j in hop(i) for k in hop(j)} for i in idx}  # its own path, two hops each way
        vs = [v for v in ix.vias[net] if ly in via_layers(v, b.copper)]

        def held(n, lst, hw):
            """an end cap that only overlaps a pad or via is KiCad's connection to it - unless a
            track within two hops along this path ends on the via's centre / inside the pad (then
            the overlap is a tight bend grazing what the path reaches anyway); a node inside a pad
            or via, unless its other track is a stub to that via's centre / wholly in that pad
            ending in a dead end or junction there (the chain then ends in it)"""
            far = [b.tracks[i]['b'] if q(b.tracks[i]['a']) == n else b.tracks[i]['a'] for i in lst]
            own = [e for i in lst for j in near2[i] for e in (b.tracks[j]['a'], b.tracks[j]['b'])]
            for v in vs:
                c = (v['x'], v['y'])
                d = math.dist(n, c)
                if d <= 1e-3 or d <= v['size'] / 2 + hw and not any(math.dist(e, c) <= 1e-3 for e in own) or \
                        d <= v['size'] / 2 + 1e-6 and not any(math.dist(e, c) <= 1e-3 for e in far):
                    return True                      # inside it, not a stub away from its centre
            for p in lpads:
                if in_pad(p, n):                     # the stub's far end must itself be an anchor (a dead
                    if not any(in_pad(p, e) and len(ends[q(e)]) != 2 for e in far):   # end or junction): else
                        return True                  # the DP cut past U6.5 and disconnected it
                elif in_pad(p, n, hw) and not any(in_pad(p, e) for e in own if q(e) != n):
                    return True
            return False
        for n, lst in ends.items():
            ts = [b.tracks[i] for i in lst]
            hw = max(t['w'] for t in ts) / 2
            if len(lst) != 2 or ts[0]['w'] != ts[1]['w'] or held(n, lst, hw):
                anchor.add(n)
            for i in idx:                           # a T into another track's interior (not a cap of its
                t = b.tracks[i]                     # own path's next bends grazing it: a tight zigzag)
                if i not in lst and not near2[i] & set(lst) and pt_seg_dist(n, t['a'], t['b']) <= t['w'] / 2 + hw and \
                        min(math.dist(n, t['a']), math.dist(n, t['b'])) > t['w'] / 2 and \
                        not any(in_pad(p, n, hw) and (in_pad(p, t['a'], t['w'] / 2) or in_pad(p, t['b'], t['w'] / 2))
                                for p in lpads):
                    frozen.add(i)                   # (both in one pad: the pad joins them, not t's body)
                    anchor.add(n)
        for i in idx:                               # body over a pad/via it does not end on
            t = b.tracks[i]
            L = max(1, int(t['len'] / 0.05))
            pts = [(t['a'][0] + (t['b'][0] - t['a'][0]) * k / L, t['a'][1] + (t['b'][1] - t['a'][1]) * k / L)
                   for k in range(L + 1)]
            for p in lpads:
                if abs(p['x'] - t['mid'][0]) > t['len'] / 2 + 5 or abs(p['y'] - t['mid'][1]) > t['len'] / 2 + 5:
                    continue
                if not (in_pad(p, t['a'], t['w'] / 2) or in_pad(p, t['b'], t['w'] / 2)) and \
                        any(in_pad(p, x, t['w'] / 2) for x in pts):
                    frozen.add(i)
            for v in ix.vias[net]:
                c = (v['x'], v['y'])
                if math.dist(c, t['a']) > (v['size'] + t['w']) / 2 and math.dist(c, t['b']) > (v['size'] + t['w']) / 2 and \
                        pt_seg_dist(c, t['a'], t['b']) <= (v['size'] + t['w']) / 2:
                    frozen.add(i)
        for i in frozen:
            anchor |= {q(b.tracks[i]['a']), q(b.tracks[i]['b'])}
        seen = set(frozen)
        for n0 in sorted(anchor):
            for i0 in ends[n0]:
                if i0 in seen:
                    continue
                order, pts, n, i = [], [n0], n0, i0
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
                ex = [b.tracks[order[0]]['a'] if q(b.tracks[order[0]]['a']) == pts[0] else b.tracks[order[0]]['b']]
                for k, i in enumerate(order):       # exact coordinates, not the rounded keys
                    t = b.tracks[i]
                    ex.append(t['b'] if q(t['a']) == pts[k] else t['a'])
                out.append((net, ly, b.tracks[order[0]]['w'], order, ex))
    return out


# ---------------------------------------------------------------- clearance

def _hole_clr(b):
    """{'via', 'pth', 'npth'}: the hole_clearance a track keeps from each kind of hole - the
    .kicad_pro minimum (0.2), raised by each .kicad_dru rule whose condition names that kind
    (A.Type == 'Via', A.isPlated(), NPTH), every kind by a rule naming none. One max for all
    read the PTH 0.28 at J7's NPTH pegs (0.2) and boxed in the VBUS escape Ryan laid."""
    import glob, json
    d, floor = os.path.dirname(os.path.abspath(b.path)), 0.2
    for pro in glob.glob(os.path.join(d, '*.kicad_pro')):
        try:
            floor = max(floor, float(json.load(open(pro))['board']['design_settings']['rules']['min_hole_clearance']))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    out = dict.fromkeys(('via', 'pth', 'npth'), floor)
    for dru in glob.glob(os.path.join(d, '*.kicad_dru')):
        txt = open(dru, encoding='utf-8', errors='replace').read()
        for m in re.finditer(r'\(constraint hole_clearance \(min ([\d.]+)mm\)\)(?:\s*\(condition "([^"]*)"\))?', txt):
            cond = m[2] or ''
            ks = [k for k, hit in (('via', "Type == 'Via'" in cond), ('npth', 'NPTH' in cond),
                                   ('pth', 'isPlated()' in cond and 'NPTH' not in cond)) if hit] or list(out)
            for k in ks:
                out[k] = max(out[k], float(m[1]))
    return out


def seg_pad_dist(a, c, pad):
    """segment pq to a pad's copper: a circle exactly, a rect in its own frame exactly,
    roundrect/oval as their rect and a custom pad as its board box (both conservative)"""
    if pad['shape'] == 'circle' and not pad['prims']:
        return max(0.0, pt_seg_dist((pad['x'], pad['y']), a, c) - pad['sx'] / 2)
    if pad['prims']:
        return seg_box_dist(a, c, pad_box(pad))
    hx, hy = pad['sx'] / 2, pad['sy'] / 2
    return seg_box_dist(_local(pad, a), _local(pad, c), (-hx, -hy, hx, hy))


class Exact:
    """any-angle clearance of a centreline polyline of width w on one layer: segment-to-item
    distances under the typed rules (_clearance_fn kinds), hole clearance to other nets'
    holes, the edge, track keepouts and other nets' fills that are not planes (a plane
    refills round the edit, as route assumes). No grid; a margin only shrinks acceptance.
    box: where the centreline may run; items are gathered out to w/2 + 1 mm past it (a
    4 mm track's cap reaches 2 mm past its centreline box: a neighbour there was missed)."""

    def __init__(self, b, net, ly, w, box):
        self.w = w
        r = w / 2 + 1.0
        box = (box[0] - r, box[1] - r, box[2] + r, box[3] + r)
        clr = _clearance_fn(b)[0]
        hc, planes = _hole_clr(b), _planes(b)
        near = lambda bb: bb[2] >= box[0] and bb[0] <= box[2] and bb[3] >= box[1] and bb[1] <= box[3]  # noqa: E731
        self.trk, self.disks, self.pads, self.fills = [], [], [], []
        for t in b.tracks:
            hw, bb = t['w'] / 2, bbox([t['a'], t['b']])
            if t['layer'] == ly and t['net'] != net and near((bb[0] - hw, bb[1] - hw, bb[2] + hw, bb[3] + hw)):
                self.trk.append((t['a'], t['b'], hw + clr(net, t['net'], 'track', 'track', ly)))
        for v in b.vias:
            if v['net'] != net and near((v['x'] - 1, v['y'] - 1, v['x'] + 1, v['y'] + 1)):
                if ly in via_layers(v, b.copper):
                    self.disks.append(((v['x'], v['y']), v['size'] / 2 + clr(net, v['net'], 'track', 'via', ly)))
                self.disks.append(((v['x'], v['y']), v['drill'] / 2 + hc['via']))
        for f in b.fps.values():
            for p in f.pads if f.placed else ():
                pb = pad_box(p)                       # a battery-holder pad is 9 mm: never by its centre
                if not near((pb[0] - 0.5, pb[1] - 0.5, pb[2] + 0.5, pb[3] + 0.5)) or (p['net'] == net and net):
                    continue
                if p['drill']:
                    self.disks.append(((p['x'], p['y']), p['drill'] / 2 + hc['npth' if p['kind'] == 'np_thru_hole' else 'pth']))
                if p['kind'] != 'np_thru_hole' and on_layer(p, ly):
                    self.pads.append((p, clr(net, p['net'], 'track', 'pad', ly)))
        for fl in b.fills:
            if fl['layer'] == ly and fl['net'] != net and (fl['net'], ly) not in planes and near(fl['bbox']):
                self.fills.append((_pidx(b, fl), clr(net, fl['net'], 'track', 'zone', ly)))
        for net2, ly2, ring, sw, filled in b.cushapes:  # copper drawn as graphics, as route's Grid
            if ly2 == ly and not (net2 == net and net) and near(bbox(ring)):
                c = clr(net, net2)
                self.trk += [(u, v, sw / 2 + c) for u, v in zip(ring, ring[1:])]
                if filled:
                    self.fills.append((PolyIndex(ring), c))
        self.edge = [(u, v) for u, v in b.edge_segs if near(bbox([u, v]))]
        self.ec = b.edge_clearance()
        self.kos = [idx for lys, idx, _nv, _np, notrk in _kos(b) if notrk and ly in lys and near(idx.bbox)]

    def seg(self, p, c):
        hw = self.w / 2 + EPS
        if any(seg_seg_dist(p, c, a, e) < need + hw for a, e, need in self.trk) or \
                any(pt_seg_dist(o, p, c) < need + hw for o, need in self.disks) or \
                any(seg_pad_dist(p, c, pad) < need + hw for pad, need in self.pads) or \
                any(seg_seg_dist(p, c, u, v) < self.ec + hw for u, v in self.edge):
            return False
        n = max(2, int(math.dist(p, c) / 0.02) + 1)          # samples 0.02 apart: tol + 0.01
        pts = [(p[0] + (c[0] - p[0]) * k / n, p[1] + (c[1] - p[1]) * k / n) for k in range(n + 1)]
        return not any(pi.touches(x, need + hw + 0.01) for pi, need in self.fills for x in pts) and \
            not any(idx.touches(x, hw + 0.01) for idx in self.kos for x in pts)

    def poly(self, pts):
        return all(self.seg(pts[i], pts[i + 1]) for i in range(len(pts) - 1))

    def slack(self, p, c):
        """the worst distance - need over the items (no margin, no fills or keepouts): >= 0 is clear"""
        hw = self.w / 2
        return min([seg_seg_dist(p, c, a, e) - need - hw for a, e, need in self.trk]
                   + [pt_seg_dist(o, p, c) - need - hw for o, need in self.disks]
                   + [seg_pad_dist(p, c, pad) - need - hw for pad, need in self.pads]
                   + [seg_seg_dist(p, c, u, v) - self.ec - hw for u, v in self.edge], default=9.0)

    def soft(self, p, c):
        """clear of other nets' non-plane fills and track keepouts (seg's second half)"""
        hw = self.w / 2 + EPS
        n = max(2, int(math.dist(p, c) / 0.02) + 1)
        pts = [(p[0] + (c[0] - p[0]) * k / n, p[1] + (c[1] - p[1]) * k / n) for k in range(n + 1)]
        return not any(pi.touches(x, need + hw + 0.01) for pi, need in self.fills for x in pts) and \
            not any(idx.touches(x, hw + 0.01) for idx in self.kos for x in pts)


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


class Clear:
    """is a centreline segment clear for this chain: the grid first (every sample's nearest
    point free, or on the old centreline), Exact where the grid refuses; and new copper may
    touch the net's other copper only near where the old chain did"""

    def __init__(self, b, net, ly, w, pts, order, res=RES):
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        self.box = box = (min(xs) - 0.6, min(ys) - 0.6, max(xs) + 0.6, max(ys) + 0.6)
        self.b, self.net, self.ly, self.w, self.res = b, net, ly, w, res
        self.g = g = Grid(b, net, box, res, w, 0.6, 0.3, [ly], extra_clr=res * 0.8, vias=False)
        self.blk, self.old, self._ex = g.blk[ly], list(zip(pts, pts[1:])), None
        # same-net copper the chain must not newly touch (a sense tap that touched its power
        # copper somewhere new would move its sense point): `own` is that copper grown by w/2,
        # `allow` 0.5 mm round where the old chain already touched it. The anchors' own
        # pads/vias are exempt; GND is exempt (the plane is everywhere).
        N = g.W * g.H
        self.own, self.allow = bytearray(N), bytearray(N)
        if net == 'GND':
            return
        R, ends = w / 2 + 0.005, (pts[0], pts[-1])
        near = lambda bb: bb[2] >= box[0] and bb[0] <= box[2] and bb[3] >= box[1] and bb[1] <= box[3]  # noqa: E731
        mine = {b.tracks[i]['uuid'] for i in order}
        for f in b.fps.values():
            for p in f.pads if f.placed else ():
                if p['net'] == net and on_layer(p, ly) and near(pad_box(p)) and not any(in_pad(p, e, w / 2) for e in ends):
                    _paint(g, self.own, _pad_shape(p, pad_box(p)), R)
        for v in b.vias:
            if v['net'] == net and ly in via_layers(v, b.copper) and \
                    all(math.dist(e, (v['x'], v['y'])) > (v['size'] + w) / 2 for e in ends):
                _paint(g, self.own, ('disk', v['x'], v['y'], v['size'] / 2), R)
        for t in b.tracks:
            if t['net'] == net and t['layer'] == ly and t['uuid'] not in mine and near(bbox([t['a'], t['b']])):
                _paint(g, self.own, ('cap', t['a'], t['b'], t['w'] / 2), R)
        for fl in b.fills:
            if fl['net'] == net and fl['layer'] == ly and near(fl['bbox']):
                g.fill_rows(_pidx(b, fl), self.own)
                ring = fl['pts']
                for u, v in zip(ring, ring[1:] + ring[:1]):
                    _paint(g, self.own, ('cap', u, v, 0.0), R)
        for a, c in self.old:
            for p in self._samples(a, c):
                if self.own[self._k(p)]:
                    _paint(g, self.allow, ('disk', p[0], p[1], 0.0), 0.5)

    @property
    def exact(self):
        if self._ex is None:
            bx = self.box
            self._ex = Exact(self.b, self.net, self.ly, self.w, (bx[0] - 1, bx[1] - 1, bx[2] + 1, bx[3] + 1))
        return self._ex

    def _samples(self, a, c):
        n = max(1, math.ceil(math.dist(a, c) / (self.res / 2)))
        return [(a[0] + (c[0] - a[0]) * k / n, a[1] + (c[1] - a[1]) * k / n) for k in range(n + 1)]

    def _k(self, p):
        g = self.g
        i, j = round((p[0] - g.x0) / g.res), round((p[1] - g.y0) / g.res)
        return j * g.W + i if 0 <= i < g.W and 0 <= j < g.H else -1

    def on_old(self, p):
        return any(pt_seg_dist(p, a, c) <= 2e-4 for a, c in self.old)

    def contact_ok(self, a, c):
        for p in self._samples(a, c):
            k = self._k(p)
            if k >= 0 and self.own[k] and not self.allow[k]:
                return False
        return True

    def seg(self, a, c):
        if not self.contact_ok(a, c):
            return False
        for p in self._samples(a, c):
            k = self._k(p)
            if (k < 0 or self.blk[k]) and not self.on_old(p):
                return self.exact.seg(a, c)
        return True

    def poly(self, pts):
        return all(self.seg(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


# ---------------------------------------------------------------- chain pass

def solve(pts, ck, span=30):
    """DP over the old vertices: state (vertex, incoming direction)"""
    n = len(pts) - 1
    best = {(0, None): (0.0, None)}
    for i in range(n):
        states = [(s, v) for s, v in best.items() if s[0] == i]
        for j in range(i + 1, min(n, i + span) + 1):
            bent = False
            for con in connectors(pts[i], pts[j]) + [None] + connectors(pts[i], pts[j], OFFS)[4:]:
                if con is None:                         # the off-centre 45s only when no dogleg cleared
                    if bent or octi(pts[i], pts[j]):
                        break
                    continue
                con = simplify(con)
                if len(con) < 2:
                    continue
                if (j > i + 1 or len(con) > 2 or con[-1] != pts[j]) and not ck.poly(con):
                    continue
                bent |= len(con) == 3
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
        s = prev
    out = [pts[0]]
    for con in reversed(parts):
        out += con[1:]
    return simplify(out)


def chamfer(pts, ck, w):
    """cut each remaining 90-degree corner with the deepest 45-degree leg that clears"""
    pts = list(pts)
    k = 1
    while k < len(pts) - 1:
        a, v, c = pts[k - 1], pts[k], pts[k + 1]
        th = turn(ang(a, v), ang(v, c))
        if th < 89 or th > 91 or not (octi(a, v) and octi(v, c)):
            k += 1
            continue
        L1, L2 = math.dist(a, v), math.dist(v, c)
        d = min(L1 if k == 1 else L1 / 2, L2 if k == len(pts) - 2 else L2 / 2, 4 * w + 0.5)
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
    return simplify(pts)


def touched(b, ix, net, ly, w, pts):
    """ids of the same-net pads (on ly) and vias a width-w polyline's copper overlaps"""
    segs = list(zip(pts, pts[1:]))
    return {id(p) for p in ix.pads[net] if on_layer(p, ly) and any(seg_pad_dist(a, c, p) <= w / 2 for a, c in segs)} | \
        {id(v) for v in ix.vias[net] if ly in via_layers(v, b.copper) and
         any(pt_seg_dist((v['x'], v['y']), a, c) <= (v['size'] + w) / 2 for a, c in segs)}


def keeps(b, ix, net, ly, w, old, new):
    """the re-laid polyline still touches every same-net pad and via the old one did: the
    safety net under the anchors (a relaxed one once let a chain cut past U6.5, 2026-10-27)"""
    return not touched(b, ix, net, ly, w, old) - touched(b, ix, net, ly, w, new)


def pass_chains(b, skip, sel):
    """[(saving, net, layer, w, order, old pts, new pts)], cross-checked between nets"""
    clr = _clearance_fn(b)[0]
    ix = Index(b)
    props, chs = [], chains(b, skip, ix)
    for net, ly, w, order, pts in chs:
        if not sel(net, ly, pts) or (len(pts) == 2 and octi(*pts)):
            continue
        ck = Clear(b, net, ly, w, pts, order)
        new = chamfer(solve(pts, ck), ck, w)
        c0, c1 = cost(pts), cost(new)
        if c1 < c0 - 0.05 and length(new) <= length(pts) * 1.09 + 0.02 and keeps(b, ix, net, ly, w, pts, new):
            props.append((c0 - c1, net, ly, w, order, pts, new))
    props.sort(key=lambda p: -p[0])
    taken = []
    for p in props:
        segs = list(zip(p[6], p[6][1:]))
        if not any(o[2] == p[2] and o[1] != p[1] and
                   any(seg_seg_dist(s0, s1, t0, t1) < clr(p[1], o[1], 'track', 'track', p[2]) + (p[3] + o[3]) / 2
                       for s0, s1 in segs for t0, t1 in zip(o[6], o[6][1:])) for o in taken):
            taken.append(p)
    return taken, len(chs)


def write_chains(b, t, taken):
    """re-lay each chain: an anchor-end segment that only changed length keeps its uuid"""
    kept, dead = 0, []
    for _g, net, ly, w, order, pts, new in taken:
        old = {frozenset((q(b.tracks[i]['a']), q(b.tracks[i]['b']))): i for i in order}
        gone, fresh = set(order), []
        for s in zip(new, new[1:]):
            k = frozenset((q(s[0]), q(s[1])))
            if k in old:
                gone.discard(old.pop(k))
            else:
                fresh.append(s)
        for end, i in ((new[0], order[0]), (new[-1], order[-1])):
            if i not in gone:
                continue
            tr = b.tracks[i]
            for s in fresh:
                if q(end) not in (q(s[0]), q(s[1])):
                    continue
                o_far = tr['b'] if q(tr['a']) == q(end) else tr['a']
                n_far = s[1] if q(s[0]) == q(end) else s[0]
                if turn(ang(end, o_far), ang(end, n_far)) < 0.05:
                    tag = 'end' if q(tr['a']) == q(end) else 'start'
                    txt, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(n_far[0])} {num(n_far[1])})", t.text(tr['uuid']), count=1)
                    assert c == 1
                    t.sub(tr['uuid'], txt)
                    gone.discard(i)
                    fresh.remove(s)
                    kept += 1                         # same anchor, same direction: its teardrop stays
                break
        for i in gone:
            t.sub(b.tracks[i]['uuid'], '')
            dead.append(b.tracks[i])
        for s in fresh:
            t.segment(net, ly, w, s[0], s[1])
    return kept, dead


# ---------------------------------------------------------------- overlaps

def pass_overlaps(b, skip, ix=None):
    """[(net, layer, 'del', i) | (net, layer, 'cut', i, end, new point)] for same-net,
    same-layer pairs leaving a node in the same direction, shorter ti, longer tj, collinear.
    A hairpin tip (nothing else at the node): ti goes and tj is cut back to the first thing
    attached to it; never when a same-net pour touches the cut span. Elsewhere ti goes if
    no wider than tj (covered), else tj's doubled span is cut back to ti's far end."""
    ix = ix or Index(b)
    far = lambda t, n: t['b'] if q(t['a']) == n else t['a']  # noqa: E731
    out, used = [], set()
    bylay = defaultdict(list)
    for k, t in enumerate(b.tracks):
        bylay[(t['net'], t['layer'])].append(k)

    def orphans_via(i, net, ly):
        """deleting track i leaves a via with same-net track ends inside it but none on its
        centre: KiCad's TRACK_NOT_CENTERED_ON_VIA (+BATT In1 102.66,126.52, 2026-10-26)"""
        for e in (b.tracks[i]['a'], b.tracks[i]['b']):
            for v in ix.vias[net]:
                c = (v['x'], v['y'])
                if math.dist(e, c) > 1e-3 or ly not in via_layers(v, b.copper):
                    continue
                ins = [x for k in bylay[(net, ly)] if k != i for x in (b.tracks[k]['a'], b.tracks[k]['b'])
                       if math.dist(x, c) <= v['size'] / 2]
                if ins and all(math.dist(x, c) > 1e-3 for x in ins):
                    return True
        return False
    for (net, ly, n), lst in ix.ends.items():
        if net in skip:
            continue
        lpads = [p for p in ix.pads[net] if on_layer(p, ly)]
        for i in lst:
            for j in lst:
                ti, tj = b.tracks[i], b.tracks[j]
                if i == j or i in used or j in used or ti['len'] > tj['len'] or (ti['len'] == tj['len'] and i > j) \
                        or ti['len'] < 1e-3 or is_arc(ti) or is_arc(tj) or turn(ang(n, far(ti, n)), ang(n, far(tj, n))) > 1 \
                        or pt_seg_dist(far(ti, n), tj['a'], tj['b']) > 0.005:
                    continue
                tip = len(lst) == 2 and not any(in_pad(p, n, tj['w'] / 2) for p in lpads) and \
                    not ix.on_via(net, ly, n, tj['w'] / 2) and \
                    not any(pt_seg_dist(n, t['a'], t['b']) <= t['w'] / 2 for k, t in enumerate(b.tracks)
                            if k not in (i, j) and t['net'] == net and t['layer'] == ly)
                if not tip:
                    if ti['w'] <= tj['w']:
                        if orphans_via(i, net, ly):
                            continue
                        out.append((net, ly, 'del', i))
                    elif tj['len'] - ti['len'] < 1e-3:
                        if orphans_via(j, net, ly):
                            continue
                        out.append((net, ly, 'del', j))
                    else:
                        out.append((net, ly, 'cut', j, n, far(ti, n)))
                    used |= {i, j}
                    continue
                fj, L = far(tj, n), tj['len']
                at = lambda s: (n[0] + (fj[0] - n[0]) * s / L, n[1] + (fj[1] - n[1]) * s / L)  # noqa: E731
                cuts = []
                for v in ix.vias[net]:
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
                    if hit:
                        cuts.append(hit[0])
                    elif any(in_pad(p, at(k * L / steps), tj['w'] / 2) for k in range(steps + 1)):
                        cuts.append(-1)                         # the cap touches it: KiCad's connection
                s = min(cuts, default=L)
                if s <= 0 or s >= L - 1e-3 or orphans_via(i, net, ly):
                    continue
                span = [at(k * s / 20) for k in range(21)]
                if any(fl['net'] == net and fl['layer'] == ly and any(p in _pidx(b, fl) for p in span)
                       for fl in b.fills if fl['bbox'][0] <= max(n[0], fj[0]) and fl['bbox'][2] >= min(n[0], fj[0])
                       and fl['bbox'][1] <= max(n[1], fj[1]) and fl['bbox'][3] >= min(n[1], fj[1])):
                    continue
                out += [(net, ly, 'del', i), (net, ly, 'cut', j, n, at(s))]
                used |= {i, j}
    return out


def write_overlaps(b, t, ops):
    for o in ops:
        tr = b.tracks[o[3]]
        if o[2] == 'del':
            t.sub(tr['uuid'], '')
            continue
        tag = 'start' if q(tr['a']) == q(o[4]) else 'end'
        txt, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(o[5][0])} {num(o[5][1])})", t.text(tr['uuid']), count=1)
        assert c == 1
        t.sub(tr['uuid'], txt)
    return [b.tracks[o[3]] for o in ops]


# ---------------------------------------------------------------- runs joined only through a pad

def pad_joins(b, skip, ix=None, chs=None):
    """[(net, layer, pad label, pad, A, B, bridges)]: an SMD pad (<= 4 mm2) that exactly two
    same-width chains enter at different points (an end inside it or its cap on it), joined only
    through its copper and 'bridge' chains wholly inside it (a 1.4 mm stub, 2 um slivers: +5V at C40.1, 2026-10-27), no via
    of the net on it. A ends in the pad, B starts there (chains oriented)."""
    ix = ix or Index(b)
    chs = chs if chs is not None else chains(b, skip, ix)
    lab = {id(p): f"{f.ref}.{p['num']}" for f in b.placed() for p in f.pads}
    at = defaultdict(list)                           # (net, ly, id(pad)) -> [chain]
    pads = {}
    for ch in chs:
        net, ly, _w, _o, pts = ch
        for p in ix.pads[net]:
            if not p['drill'] and on_layer(p, ly) and p['sx'] * p['sy'] <= 4 and \
                    (in_pad(p, pts[0], ch[2] / 2) or in_pad(p, pts[-1], ch[2] / 2)):
                at[(net, ly, id(p))].append(ch)
                pads[id(p)] = p
    out = []
    for (net, ly, pid), lst in at.items():
        p = pads[pid]
        def rests(e, w):
            """e ends on p: inside it, or a dead end whose cap is on it, nearer p than any other
            pad of the net (C58.1's sliver ends 12 um past the corner arc; inside Q11.3 is Q11.3's).
            A junction there is not (a wide run's node within w/2 continues into its entry)"""
            if in_pad(p, e):
                return True
            k = ix.ends[(net, ly, q(e))]
            return in_pad(p, e, w / 2) and len(k) == 1 and not ix.on_via(net, ly, e, w / 2) and \
                min((o for o in ix.pads[net] if on_layer(o, ly)), key=lambda o: pad_dist(o, e, True)) is p

        def on(e, w):
            """rests, and for a cap not lying on a track that itself reaches into p (U16's run
            ending on its 2-3 strap joins the strap directly, not through the pad)"""
            return in_pad(p, e) or rests(e, w) and not any(
                pt_seg_dist(e, t['a'], t['b']) <= (w + t['w']) / 2 and (in_pad(p, t['a']) or in_pad(p, t['b']))
                for j in ix.trk[(net, ly)] for t in [b.tracks[j]] if j not in ix.ends[(net, ly, q(e))])

        def side(c):
            """None: c does not enter p (it rests on p at both ends: a bridge, or it is off p at
            both); else whether its pad end is its last point"""
            if rests(c[4][0], c[2]) and rests(c[4][-1], c[2]):
                return None
            s0, s1 = on(c[4][0], c[2]), on(c[4][-1], c[2])
            return None if s0 == s1 else s1
        ent = [c for c in lst if side(c) is not None]
        if len(ent) != 2 or ent[0][2] != ent[1][2] or net in skip or \
                any(seg_pad_dist((v['x'], v['y']), (v['x'], v['y']), p) <= v['size'] / 2
                    for v in ix.vias[net] if ly in via_layers(v, b.copper)):
            continue
        A, B = (c if side(c) else (c[0], c[1], c[2], c[3][::-1], c[4][::-1]) for c in ent)
        B = (B[0], B[1], B[2], B[3][::-1], B[4][::-1])
        mine = {i for c in ent for i in c[3]}
        brg, other = [], False                       # bridges: tracks (chained or frozen) wholly inside
        for i, t in enumerate(b.tracks):
            if i in mine or t['net'] != net or t['layer'] != ly or not (in_pad(p, t['a']) or in_pad(p, t['b'])):
                continue
            if rests(t['a'], t['w']) and rests(t['b'], t['w']) and not t['locked'] and not is_arc(t):
                brg.append(i)
            else:
                other = True
        if q(A[4][-1]) == q(B[4][0]) or other:
            continue                                 # coincident (a plain node), or a third entry
        out.append((net, ly, lab[pid], p, A, B, brg))
    return out


def pass_merges(b, skip, sel):
    """pad_joins() re-laid as ONE chain through the pad (A, the pad, B: the DP may cut across it),
    kept only if its centreline reaches as deep into the pad as the old copper (bridges too) did: merely
    inside left grazes, a run 0.05 outside J12.2's edge clipping its corner (0.26 deep vs 0.53),
    along U1.12's top edge, R23.2's end slid to its lower edge (2026-10-28). Returns taken as
    pass_chains does"""
    taken, ix = [], Index(b)
    for net, ly, _lab, p, A, B, brg in pad_joins(b, skip, ix):
        pts = A[4] + B[4]
        if not sel(net, ly, pts):
            continue
        order = A[3] + brg + B[3]
        ck = Clear(b, net, ly, A[2], pts, order)
        new = chamfer(solve(pts, ck), ck, A[2])
        deep = lambda segs: max(-pad_dist(p, (u[0] + (v[0] - u[0]) * k / 40, u[1] + (v[1] - u[1]) * k / 40), True)  # noqa: E731
                                for u, v in segs for k in range(41))
        old = list(zip(A[4], A[4][1:])) + list(zip(B[4], B[4][1:])) + [(b.tracks[i]['a'], b.tracks[i]['b']) for i in brg]
        through = deep(zip(new, new[1:])) >= deep(old) - 0.01
        if through and cost(new) < cost(pts) - 0.05 and length(new) <= length(pts) * 1.09 + 0.02 and \
                keeps(b, ix, net, ly, A[2], pts, new):
            taken.append((cost(pts) - cost(new), net, ly, A[2], order, pts, new))
    return taken


# ---------------------------------------------------------------- twins

def twins(b, skip, ix=None, chs=None):
    """[(net, layer, pad label, far chain, near chain, parallel mm, at)]: two same-net chains
    ending in one pad, running side by side (< 1 mm apart, within 10 deg) for > TWIN mm outside
    it (I2C_PMIC_SDA: U5.16 and a via both ran to R37.2, 2026-10-27); signal nets only. Each
    chain oriented to end in the pad; `near` is the one whose start is nearer the pad"""
    from kcommon import rail_voltage
    ix = ix or Index(b)
    chs = chs if chs is not None else chains(b, skip, ix)
    lab = {id(p): f"{f.ref}.{p['num']}" for f in b.placed() for p in f.pads}
    poured = {fl['net'] for fl in b.fills}
    at = defaultdict(list)
    for ch in chs:
        net, ly, w, order, pts = ch
        if net in poured or net == 'GND' or 'PWR' in (b.netclass(net) or '').upper() or \
                rail_voltage(net.split('/')[-1]) is not None:
            continue                                 # power: parallel copper is lower R (as `loops`)
        for p in ix.pads[net]:
            if on_layer(p, ly):
                for end in (-1, 0):
                    if in_pad(p, pts[end], w / 2) and not in_pad(p, pts[-1 - end], w / 2):
                        at[(net, ly, id(p))].append((ch if end == -1 else (net, ly, w, order[::-1], pts[::-1]), p))
    out = []
    for (net, ly, _pid), lst in at.items():
        for k1 in range(len(lst)):
            for k2 in range(k1 + 1, len(lst)):
                (c1, p), c2 = lst[k1], lst[k2][0]
                C = (p['x'], p['y'])
                par, mid = 0.0, None
                for u, v in zip(c1[4], c1[4][1:]):
                    L = math.dist(u, v)
                    for s in range(max(1, int(L / 0.05))):
                        x = (u[0] + (v[0] - u[0]) * (s + 0.5) / max(1, int(L / 0.05)),
                             u[1] + (v[1] - u[1]) * (s + 0.5) / max(1, int(L / 0.05)))
                        if in_pad(p, x):
                            continue
                        if any(pt_seg_dist(x, a2, b2) < 1.0 and min(turn(ang(u, v), ang(a2, b2)), turn(ang(u, v), ang(b2, a2)))
                               <= 10 for a2, b2 in zip(c2[4], c2[4][1:])):
                            par += L / max(1, int(L / 0.05))
                            mid = mid or x
                if par > TWIN:
                    far, near = (c1, c2) if math.dist(c1[4][0], C) >= math.dist(c2[4][0], C) else (c2, c1)
                    out.append((net, ly, lab[id(p)], far, near, par, mid))
    return out


def pass_twins(b, skip, sel):
    """each twins() pair re-laid as a daisy chain: the far chain deleted, its start joined to
    the near chain's start (straight or a 45 dogleg, its own width) where that clears and
    is shorter than what it replaces. [(net, layer, label, far chain, connector)]"""
    done, used = [], set()
    for net, ly, lab, far, near, par, mid in twins(b, skip):
        S1, S2, w = far[4][0], near[4][0], far[2]
        if used & (set(far[3]) | set(near[3])) or not sel(net, ly, far[4] + [S2]) or \
                any(b.tracks[i]['locked'] for i in far[3]):
            continue
        ck = Clear(b, net, ly, w, [S1, S2] + far[4], far[3])
        ck.old = [(S1, S1), (S2, S2)]                # new copper: may touch the net only at its two ends
        con = next((simplify(c) for c in sorted(connectors(S1, S2), key=len)
                    if ck.poly(simplify(c)) and length(simplify(c)) < length(far[4]) - 0.05), None)
        if con:
            used |= set(far[3]) | set(near[3])
            done.append((net, ly, lab, far, con))
    return done


# ---------------------------------------------------------------- corners and square

def covered(b, ix, net, ly, pt, w, mine):
    """pt's w/2 disk is inside the UNION of other same-net copper (pad, via, wider track,
    fill): every one of 9 points (centre + 8 on the rim) lies in some item"""
    ring = [pt] + [(pt[0] + w / 2 * math.cos(k * math.pi / 4), pt[1] + w / 2 * math.sin(k * math.pi / 4)) for k in range(8)]
    trk = [t for i, t in enumerate(b.tracks) if i not in mine and t['net'] == net and t['layer'] == ly and t['w'] > w
           and pt_seg_dist(pt, t['a'], t['b']) <= t['w'] / 2 + w]
    fl = [_pidx(b, f) for f in b.fills if f['net'] == net and f['layer'] == ly and
          f['bbox'][0] <= pt[0] <= f['bbox'][2] and f['bbox'][1] <= pt[1] <= f['bbox'][3]]
    pads = [p for p in ix.pads[net] if on_layer(p, ly)]
    vias = ix.vias[net]
    return all(any(in_pad(p, x) for p in pads) or any(math.dist(x, (v['x'], v['y'])) <= v['size'] / 2 for v in vias)
               or any(pt_seg_dist(x, t['a'], t['b']) <= t['w'] / 2 for t in trk) or any(x in pi for pi in fl)
               for x in ring)


def leg_in_cap(t, o):
    """t's whole capsule lies inside o's (a 0.1 mm stub under a 3 mm track's end cap): o's
    centreline within o.w/2 - t.w/2 of both of t's ends (the shrunk capsule is convex)"""
    r = (o['w'] - t['w']) / 2
    return r >= 0 and not is_arc(t) and not is_arc(o) and \
        all(pt_seg_dist(e, o['a'], o['b']) <= r + 1e-6 for e in (t['a'], t['b']))


def corner_nodes(b, skip, ix):
    """[(turn, net, layer, node, i1, i2)] sharpest first: visible-candidate corners >= 46 deg
    at nodes of exactly two tracks, not on a pad or via (an end cap that only overlaps one is
    KiCad's connection to it), no T, no arc or locked leg"""
    out = []
    for (net, ly, n), lst in ix.ends.items():
        if net in skip or len(lst) != 2:
            continue
        t1, t2 = b.tracks[lst[0]], b.tracks[lst[1]]
        if any(is_arc(t) or t['locked'] or t['len'] < 1e-4 for t in (t1, t2)):
            continue
        hw = max(t1['w'], t2['w']) / 2 + 0.01
        if ix.on_pad(net, ly, n, hw) or ix.on_via(net, ly, n, hw):
            continue
        far1 = t1['b'] if q(t1['a']) == n else t1['a']
        far2 = t2['b'] if q(t2['a']) == n else t2['a']
        th = turn(ang(far1, n), ang(n, far2))
        if th >= 46:
            out.append((th, net, ly, n, lst[0], lst[1]))
    return sorted(out, reverse=True)


def cut_corner(b, net, ly, V, i1, i2, res=0.01):
    """the cheapest clear cut of corner V: (cost, connector points) or None"""
    t1, t2 = b.tracks[i1], b.tracks[i2]
    A = t1['b'] if q(t1['a']) == q(V) else t1['a']
    C = t2['b'] if q(t2['a']) == q(V) else t2['a']
    L1, L2 = math.dist(A, V), math.dist(V, C)
    w = max(t1['w'], t2['w'])
    if any(i not in (i1, i2) and t['net'] == net and t['layer'] == ly and pt_seg_dist(V, t['a'], t['b']) <= t['w'] / 2
           for i, t in enumerate(b.tracks)):
        return None                                   # another track's body runs through V: a T
    ck = Clear(b, net, ly, w, [A, V, C], [i1, i2], res)
    steps = lambda L: [k * 0.05 for k in range(1, int(min(L - 0.02, 1.5) / 0.05) + 1)]  # noqa: E731
    best = None
    for d1 in steps(L1):
        P1 = (V[0] + (A[0] - V[0]) * d1 / L1, V[1] + (A[1] - V[1]) * d1 / L1)
        for d2 in steps(L2):
            P2 = (V[0] + (C[0] - V[0]) * d2 / L2, V[1] + (C[1] - V[1]) * d2 / L2)
            for con in connectors(P1, P2):
                con = simplify(con)
                if len(con) < 2:
                    continue
                poly = [A] + con + [C]
                if max(turn(ang(poly[k - 1], poly[k]), ang(poly[k], poly[k + 1])) for k in range(1, len(poly) - 1)) >= 46:
                    continue
                c = cost(poly)
                if (best is None or c < best[0]) and ck.poly(con):
                    best = (c, con)
    return best


def pass_corners(b, skip, sel, hidden=False):
    """cut corners in sequence (each cut is in the model before the next is priced): the
    in-memory tracks change; returns [(turn, net, layer, node, connector)]"""
    ix, done = Index(b), []
    for th, net, ly, V, i1, i2 in corner_nodes(b, skip, ix):
        t1, t2 = b.tracks[i1], b.tracks[i2]
        if not sel(net, ly, [V]) or (not hidden and (covered(b, ix, net, ly, V, max(t1['w'], t2['w']), {i1, i2})
                                                     or leg_in_cap(t1, t2) or leg_in_cap(t2, t1))):
            continue
        r = cut_corner(b, net, ly, V, i1, i2)
        if not r:
            continue
        con, w = r[1], max(t1['w'], t2['w'])
        for t, P in ((t1, con[0]), (t2, con[-1])):          # pull each leg back to its cut point
            t['a' if q(t['a']) == q(V) else 'b'] = P
            _refresh(t)
        for u, v in zip(con, con[1:]):
            b.tracks.append(_new(net, ly, w, u, v))
        done.append((th, net, ly, V, con))
    return done


def pass_square(b, skip, sel):
    """slide the pad end of each off-45 segment onto the nearest octilinear line from its
    other end, inside the same pad (kept >= w/4 in, <= 0.5 mm moved), the other tracks at
    that end moving with it; returns [(net, layer, old end, new end)]"""
    ix, done = Index(b), []
    for i, t in enumerate(list(b.tracks)):
        if not t['net'] or t['net'] in skip or t['len'] < 0.05 or t['locked'] or octi(t['a'], t['b']) or is_arc(t) \
                or not sel(t['net'], t['layer'], [t['a'], t['b']]) or covered(b, ix, t['net'], t['layer'], t['mid'], t['w'], {i}):
            continue
        best = None
        for E, F in ((t['a'], t['b']), (t['b'], t['a'])):
            hosts = [p for p in ix.pads[t['net']] if on_layer(p, t['layer']) and in_pad(p, E)]
            if not hosts:
                continue
            for k in range(8):
                if turn(ang(F, E), k * 45) > 20:
                    continue
                u = (math.cos(math.radians(k * 45)), math.sin(math.radians(k * 45)))
                s = (E[0] - F[0]) * u[0] + (E[1] - F[1]) * u[1]
                E2 = (F[0] + u[0] * s, F[1] + u[1] * s)
                if s > 0.05 and math.dist(E, E2) <= 0.5 and any(in_pad(p, E2, -t['w'] / 4) for p in hosts) and \
                        (best is None or math.dist(E, E2) < best[0]):
                    best = (math.dist(E, E2), E, E2, F)
        if not best:
            continue
        _d, E, E2, F = best
        co = [j for j, u in enumerate(b.tracks) if j != i and u['net'] == t['net'] and u['layer'] == t['layer']
              and q(E) in (q(u['a']), q(u['b']))]
        ck = Clear(b, t['net'], t['layer'], t['w'], [F, E], [i] + co, 0.01)
        if not ck.seg(F, E2) or any(not Clear(b, t['net'], t['layer'], b.tracks[j]['w'], [E, F], [i] + co, 0.01).seg(
                E2, b.tracks[j]['b'] if q(b.tracks[j]['a']) == q(E) else b.tracks[j]['a']) for j in co):
            continue
        for j in [i] + co:
            u = b.tracks[j]
            u['a' if q(u['a']) == q(E) else 'b'] = E2
            _refresh(u)
        done.append((t['net'], t['layer'], E, E2))
    return done


def pad_ends(b, ix, skip, inside=False):
    """[(track idx, end, 'REF.PIN', pad)]: a dead end (no other same-net track or via within
    reach) whose cap touches a same-net pad but whose centreline end is outside it or within
    GRAZE of its edge. KiCad joins on a graze; a reviewer sees a trace that stops at the pad
    edge. inside: also ends deeper in, >= 0.05 mm off centre - fine as laid (81 of 83 on
    bdbbaac; running them to the centre only added hooks at JP12.3/JP2.1 and a jog at J15.1)"""
    lab = {id(p): f"{f.ref}.{p['num']}" for f in b.fps.values() for p in f.pads}
    by = defaultdict(list)
    for i, t in enumerate(b.tracks):
        by[(t['net'], t['layer'])].append(i)
    out = []
    for i, t in enumerate(b.tracks):
        if not t['net'] or t['net'] in skip or is_arc(t) or t['len'] < 1e-4:
            continue
        for E in (t['a'], t['b']):
            hosts = [p for p in ix.pads[t['net']] if on_layer(p, t['layer']) and in_pad(p, E, t['w'] / 2)]
            if not hosts or any(math.dist(E, (p['x'], p['y'])) < 0.05 for p in hosts) or \
                    ix.on_via(t['net'], t['layer'], E, t['w'] / 2) or \
                    any(j != i and pt_seg_dist(E, b.tracks[j]['a'], b.tracks[j]['b']) <= (t['w'] + b.tracks[j]['w']) / 2
                        for j in by[(t['net'], t['layer'])]):
                continue
            p = min(hosts, key=lambda p: pad_dist(p, E, True))
            if inside or pad_dist(p, E, True) > -GRAZE:
                out.append((i, E, lab[id(p)], p))
    return out


def pass_pads(b, skip, sel):
    """bring each pad_ends() end onto its pad centre at the track's own width (narrower
    breaks a track_width rule). The first that clears, no leg under 0.05 mm: the end slid
    along the track's line when that line runs through the centre (out to it, or back from
    an overshoot); the last segment re-laid from its far end F, octilinear first, where F
    joins one other track and the turn there stays <= 45; a stub from the end, octilinear
    first, never an acute hook. Returns
    [(net, layer, end, centre, 'REF.PIN')]"""
    ix, done = Index(b), []
    for i, E, lab, p in pad_ends(b, ix, skip):
        t = b.tracks[i]
        if t['locked'] or not sel(t['net'], t['layer'], [E]):
            continue
        net, ly, w, C = t['net'], t['layer'], t['w'], (p['x'], p['y'])
        eE = 'a' if q(t['a']) == q(E) else 'b'
        F = t['b' if eE == 'a' else 'a']
        dx, dy = E[0] - F[0], E[1] - F[1]
        legs_ok = lambda P: all(math.dist(P[k], P[k + 1]) >= 0.05 for k in range(len(P) - 1))  # noqa: E731
        off45 = lambda P: sum(not octi(P[k], P[k + 1]) for k in range(len(P) - 1))          # noqa: E731
        ck = Clear(b, net, ly, w, [F, E], [i])
        if abs(dx * (C[1] - F[1]) - dy * (C[0] - F[0])) / t['len'] < 0.005 and \
                dx * (C[0] - F[0]) + dy * (C[1] - F[1]) > 0.05 * t['len'] and ck.seg(F, C):
            t[eE] = C                        # its line runs through the centre: slide the end along
            _refresh(t)
            done.append((net, ly, E, C, lab))
            continue
        at_F = [b.tracks[j] for j in ix.ends[(net, ly, q(F))] if j != i]
        G = at_F[0]['b'] if len(at_F) == 1 and q(at_F[0]['a']) == q(F) else at_F[0]['a'] if len(at_F) == 1 else None
        relay = sorted((P for P in connectors(F, C) if G and legs_ok(P) and turn(ang(G, F), ang(F, P[1])) <= 45), key=off45)
        P = next((P for P in relay if ck.poly(P)), None)
        if P:                                # re-lay the last segment from F onto the centre
            t[eE] = P[1]
            _refresh(t)
            b.tracks += [_new(net, ly, w, u, v) for u, v in zip(P[1:], P[2:])]
            done.append((net, ly, E, C, lab))
            continue
        ck = Clear(b, net, ly, w, [E, C], [i])
        ck.old = [(E, E)]                    # [E, C] only sizes the window: the stub is new copper
        stubs = sorted((P for P in connectors(E, C) if legs_ok(P) and turn(ang(F, E), ang(E, P[1])) <= 90), key=off45)
        P = next((P for P in stubs if ck.poly(P)), None)
        if P:
            b.tracks += [_new(net, ly, w, u, v) for u, v in zip(P, P[1:])]
            done.append((net, ly, E, C, lab))
    return done


def _refresh(t):
    t['len'] = math.dist(t['a'], t['b'])
    t['mid'] = ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)
    t['_moved'] = True


def _new(net, ly, w, a, c):
    return {'net': net, 'layer': ly, 'w': w, 'a': a, 'b': c, 'len': math.dist(a, c),
            'mid': ((a[0] + c[0]) / 2, (a[1] + c[1]) / 2), 'uuid': None, 'locked': False}


def write_moved(b, t, orig):
    """write the in-memory track changes of pass_corners/pass_square: moved ends by uuid,
    new segments added; returns the original tracks whose teardrops must go"""
    old = {o['uuid']: o for o in orig.tracks}
    moved = [u for u in b.tracks if u.get('_moved') and u['uuid']]
    for u in moved:
        s = t.text(u['uuid'])
        for tag, P in (('start', u['a']), ('end', u['b'])):
            s, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(P[0])} {num(P[1])})", s, count=1)
            assert c == 1
        t.sub(u['uuid'], s)
    for u in b.tracks:
        if u['uuid'] is None:
            t.segment(u['net'], u['layer'], u['w'], u['a'], u['b'])
    return [old[u['uuid']] for u in moved]


# ---------------------------------------------------------------- micro

MICRO = 0.05                                        # a segment this short is a join, not a run


def pass_micro(b, skip, sel):
    """segments under MICRO mm (`tracks` row `stub`), each in the model before the next:
    ('merge', net, ly, i, P, Q): both ends join something (a track end, a via centre, a pad) -
    the tracks ending at Q moved onto P (the via-centre end, else the pad end, else the busier
    one), the segment deleted, so a join stays a join (on a via centre = the same KiCad nm: a
    sliver 0.9 um / 1.4 nm long from a centre was deleted and left its neighbour off it,
    TRACK_NOT_CENTERED_ON_VIA at UART0_RX and R59.1, 2026-10-28) - each moved track no closer to other
    copper than it was (Exact.slack: it turns a hair, its far side gains up to ~35 um); ('del', ..., i):
    zero length, or a free end (or both ends in one via) and the segment wholly under other
    same-net copper (then all it touches its cover touches); ('left', ..., i, why) else. A segment next to one already
    edited waits for the next round"""
    ix, out, busy = Index(b), [], set()
    for i, t in enumerate(b.tracks):
        if t['len'] >= MICRO or not t['net'] or t['net'] in skip or t['locked'] or is_arc(t) or i in busy \
                or not sel(t['net'], t['layer'], [t['a'], t['b']]):
            continue
        net, ly = t['net'], t['layer']
        ends = []
        for e in (t['a'], t['b']):
            oth = [j for j in ix.ends[(net, ly, q(e))] if j != i]
            onv = any(math.dist(e, (v['x'], v['y'])) <= 5e-7 for v in ix.vias[net] if ly in via_layers(v, b.copper))
            onp = ix.on_pad(net, ly, e)
            ends.append((e, oth, onv, onp))
        near = {j for _e, oth, *_r in ends for j in oth}
        if near & busy:
            continue
        (A, oa, va, pa), (B, ob, vb, pb) = ends
        if t['len'] < 1e-4 and va == vb or (not (oa or va or pa) or not (ob or vb or pb) or va and vb) and \
                covered(b, ix, net, ly, t['mid'], t['w'], {i}):
            out.append(('del', net, ly, i))
            busy |= near | {i}
            continue
        if not (oa or va or pa) or not (ob or vb or pb):
            out.append(('left', net, ly, i, 'a free end the rest of its net does not cover'))
            continue
        if va and vb:
            out.append(('left', net, ly, i, 'both ends on a via centre'))
            continue
        rank = lambda o, v, p: (v, p, len(o))       # noqa: E731
        ea, eb = (A, oa, va, pa), (B, ob, vb, pb)
        tries = [(ea, eb), (eb, ea)] if rank(oa, va, pa) >= rank(ob, vb, pb) else [(eb, ea), (ea, eb)]
        for (P, _op, _vp, _pp), (Q, oq, vq, pq) in tries:
            if vq or pq and tries[0][1][0] != Q:     # vacate a via centre never, a pad end only first
                continue
            moved = _onto(b, net, ly, oq, P, Q)
            if moved is not None:
                for u, na, nb in moved:
                    u['a'], u['b'] = na, nb
                    _refresh(u)
                out.append(('merge', net, ly, i, P, Q))
                busy |= near | {i}
                break
        else:
            out.append(('left', net, ly, i, 'moving its neighbours onto either end breaks clearance or tilts one off 0/45/90'))
    return out


def _onto(b, net, ly, oq, P, Q):
    """[(track, new a, new b)]: the tracks ending at Q moved onto P, each no closer to other copper
    than it was (Exact.slack; it turns a hair, its far side gains up to ~35 um) and still on
    0/45/90 (octi, as `tracks` counts off-45) if it was, else None"""
    moved = []
    for j in oq:
        u = b.tracks[j]
        if u['locked'] or is_arc(u):
            return None
        na, nb = (P, u['b']) if q(u['a']) == q(Q) else (u['a'], P)
        if math.dist(na, nb) < 1e-4 or octi(u['a'], u['b']) and not octi(na, nb):
            return None                      # tilted off 0/45/90: a 0.05 mm leg at 21 deg (bdbbaac, 2026-10-28)
        box = (min(na[0], nb[0], u['a'][0], u['b'][0]), min(na[1], nb[1], u['a'][1], u['b'][1]),
               max(na[0], nb[0], u['a'][0], u['b'][0]), max(na[1], nb[1], u['a'][1], u['b'][1]))
        ex = Exact(b, net, ly, u['w'], box)
        if ex.slack(na, nb) < min(0.0, ex.slack(u['a'], u['b'])) - 1e-9 or not ex.soft(na, nb) and ex.soft(u['a'], u['b']):
            return None
        moved.append((u, na, nb))
    return moved


# ---------------------------------------------------------------- command

def one_pass(path, kind, a, out):
    """run one pass on the board at `path`, write `out` if it changed; returns edits made"""
    b = Board(path)
    skip = skipped(b)
    want = set(a.args) or None
    lays = set(a.layers.split(',')) if a.layers else None
    box = tuple(float(v) for v in a.box.split(',')) if a.box else None
    def sel(net, ly, pts):
        return (not want or net in want or net.split('/')[-1] in want) and (not lays or ly in lays) and \
            (not box or all(box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3] for p in pts))
    t = PcbText(path)
    if kind == 'overlaps':
        ops = [o for o in pass_overlaps(b, skip) if sel(o[0], o[1], [b.tracks[o[3]]['a']])]
        for o in ops[:a.max]:
            tr = b.tracks[o[3]]
            print(f"  {o[2]:3s} {o[0].split('/')[-1]:24s} {o[1]:6s} w{tr['w']:g} {q(tr['a'])}->{q(tr['b'])}"
                  + (f"  end -> {q(o[5])}" if o[2] == 'cut' else ''))
        print(f"overlaps: {sum(o[2] == 'del' for o in ops)} deleted, {sum(o[2] == 'cut' for o in ops)} cut back")
        dead, n = write_overlaps(b, t, ops), len(ops)
    elif kind == 'twins':
        done = pass_twins(b, skip, sel)
        print(f"twins: {len(done)} pair(s) of same-pad runs re-laid as a daisy chain")
        dead = []
        for net, ly, lab, far, con in done:
            print(f"  {net.split('/')[-1]:28s} {ly:6s} to {lab}: {length(far[4]):.2f} mm from {far[4][0][0]:.2f},"
                  f"{far[4][0][1]:.2f} -> {length(con):.2f} mm to {con[-1][0]:.2f},{con[-1][1]:.2f}")
            for i in far[3]:
                t.sub(b.tracks[i]['uuid'], '')
                dead.append(b.tracks[i])
            for u, v in zip(con, con[1:]):
                t.segment(net, ly, far[2], u, v)
        n = len(done)
    elif kind == 'merge':
        taken = pass_merges(b, skip, sel)
        print(f"merge: {len(taken)} pair(s) of runs joined only through a pad re-laid as one chain")
        for p in taken[:a.max]:
            print(f"  {p[1].split('/')[-1]:28s} {p[2]:6s} w{p[3]:g}  {len(p[4])} trk -> {len(p[6]) - 1} seg  "
                  f"{length(p[5]):.2f}->{length(p[6]):.2f} mm  at {p[5][0][0]:.2f},{p[5][0][1]:.2f}")
        _kept, dead = write_chains(b, t, taken)
        n = len(taken)
    elif kind == 'corners':
        orig = Board(path)
        done = pass_corners(b, skip, sel, a.hidden)
        print(f"corners: {len(done)} cut")
        for th, net, ly, V, con in done[:a.max]:
            print(f"  {net.split('/')[-1]:28s} {ly:6s} {th:5.0f} deg at {V[0]:.3f},{V[1]:.3f}  -> {len(con) - 1} seg(s)")
        dead, n = write_moved(b, t, orig), len(done)
    elif kind == 'square':
        orig = Board(path)
        done = pass_square(b, skip, sel)
        print(f"square: {len(done)} pad end(s) slid onto a 45")
        for net, ly, E, E2 in done[:a.max]:
            print(f"  {net.split('/')[-1]:28s} {ly:6s} {E[0]:.3f},{E[1]:.3f} -> {E2[0]:.3f},{E2[1]:.3f} "
                  f"({math.dist(E, E2) * 1000:.0f} um)")
        dead, n = write_moved(b, t, orig), len(done)
    elif kind == 'pads':
        orig = Board(path)
        left = len(pad_ends(b, Index(b), skip))
        done = pass_pads(b, skip, sel)
        print(f"pads: {len(done)} of {left} trace end(s) at a pad edge brought onto the pad centre")
        for net, ly, E, C, lab in done[:a.max]:
            print(f"  {net.split('/')[-1]:28s} {ly:6s} {lab:10s} {E[0]:.3f},{E[1]:.3f} -> {C[0]:.3f},{C[1]:.3f} "
                  f"({math.dist(E, C):.2f} mm)")
        dead, n = write_moved(b, t, orig), len(done)
    elif kind == 'micro':
        orig = Board(path)
        ops = pass_micro(b, skip, sel)
        cnt = Counter(o[0] for o in ops)
        print(f"micro: {cnt['merge']} segment(s) < {MICRO:g} mm merged into a neighbour, {cnt['del']} deleted "
              f"(under other copper), {cnt['left']} left")
        for o in ops[:a.max]:
            tr = b.tracks[o[3]]
            print(f"  {o[0]:5s} {o[1].split('/')[-1]:24s} {o[2]:6s} w{tr['w']:g} {tr['len'] * 1000:.0f} um at "
                  f"{tr['a'][0]:.3f},{tr['a'][1]:.3f}" + (f"  {o[4]}" if o[0] == 'left' else ''))
        gone = [orig.tracks[o[3]] for o in ops if o[0] != 'left']
        for tr in gone:
            t.sub(tr['uuid'], '')
        dead, n = write_moved(b, t, orig) + gone, len(gone)
    elif kind == 'jogs':
        from kpcb_jogs import pass_jogs
        taken, moves, rows = pass_jogs(b, skip, sel, a.nudge)
        print(f"jogs: {sum('collapsed' in r[4] for r in rows)} collapsed" + (f", {len(moves)} via(s) moved" if moves else ''))
        for net, ly, xy, off, verdict in rows[:a.max]:
            print(f"  {net.split('/')[-1]:28s} {ly:6s} {off:.2f} mm at {xy[0]:.3f},{xy[1]:.3f}  {verdict}")
        _kept, dead = write_chains(b, t, taken)
        for v, (x, y), dragged in moves:
            txt, c = re.subn(r'\(at [^)]*\)', f"(at {num(x)} {num(y)})", t.text(v['uuid']), count=1)
            assert c == 1
            t.sub(v['uuid'], txt)
            for tr, a2, b2 in dragged:
                txt = t.text(tr['uuid'])
                for tag, P in (('start', a2), ('end', b2)):
                    txt, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(P[0])} {num(P[1])})", txt, count=1)
                    assert c == 1
                t.sub(tr['uuid'], txt)
                dead.append(tr)                      # its teardrop at the via's old spot
        n = len(taken)
    else:
        taken, nch = pass_chains(b, skip, sel)
        print(f"chains: {nch}, re-laid {len(taken)}   before {census([p[5] for p in taken])}")
        print(f"{'':>27}after  {census([p[6] for p in taken])}")
        for p in taken[:a.max]:
            print(f"  {p[1].split('/')[-1]:28s} {p[2]:6s} w{p[3]:g}  {len(p[5]) - 1}->{len(p[6]) - 1} segs  "
                  f"{length(p[5]):.2f}->{length(p[6]):.2f} mm  at {p[5][0][0]:.2f},{p[5][0][1]:.2f}")
        _kept, dead = write_chains(b, t, taken)
        n = len(taken)
    if n and out:
        tear = len(tear_edits(t, dead))
        t.save(out)
        print(f"  {n} edit(s), {tear} teardrop(s) dropped")
    return n


def no_project(b, cmd):
    """smooth/widen/loops price clearance with the netclasses and .kicad_dru rules, read by
    directory glob: without a .kicad_pro beside the board every net is Default, silently"""
    if glob.glob(os.path.join(os.path.dirname(os.path.abspath(b.path)), '*.kicad_pro')):
        return False
    print(f"{cmd} needs the project beside the board (netclasses, .kicad_dru rules): run it on a "
          "`kdrc.py BOARD scratch DIR` copy")
    return True


def c_smooth(b, a):
    if no_project(b, 'smooth'):
        return 1
    kinds = [k for k in ('micro', 'overlaps', 'corners', 'square', 'pads', 'jogs', 'twins') if getattr(a, k, False)] or ['chains']
    if a.overlaps:
        kinds[kinds.index('overlaps') + 1:1] = ['merge']
    if a.all:
        kinds = ['micro', 'chains', 'overlaps', 'merge', 'corners', 'square', 'pads']
    if os.path.abspath(a.out or b.path) == os.path.abspath(b.path) and a.out:
        print("smooth writes a copy: -o OUT must not be the input"); return 1
    # passes chain through two fixed temp names beside the board (the project's rules must sit
    # beside every board read; one parse-cache snapshot per name)
    stem = os.path.join(os.path.dirname(os.path.abspath(b.path)), '.' + os.path.basename(b.path))
    tmp = [f"{stem}.smooth{k}.kicad_pcb" for k in (0, 1)]
    src, total, k = b.path, 0, 0
    for rnd in range(4 if a.all and a.out else 1):
        changed = 0
        for kind in kinds:
            if a.all:
                print(f"--- round {rnd + 1}: {kind}")
            nxt = tmp[k % 2] if a.out else None
            n = one_pass(src, kind, a, nxt)
            if n and nxt:
                src, k = nxt, k + 1
            changed += n
        total += changed
        if not changed:
            break
    if a.out and src != b.path:
        os.replace(src, a.out)
        print(f"wrote {a.out}: {total} edit(s) in all; verify: kdrc.py {a.out} drc --refill-save --baseline BEFORE.json")
    elif a.out:
        print("nothing to change; no file written")
    for f in tmp:
        if os.path.exists(f):
            os.remove(f)
    return 0


def selftest():
    """the geometry on open board (every candidate clear): staircases collapse, 90s chamfer"""
    class Open:
        seg = poly = staticmethod(lambda *a: True)
    assert simplify([(0, 0), (1, 0), (2, 0)]) == [(0, 0), (2, 0)]
    stair = [(0, 0), (1, 0), (1, 1), (2, 1), (2, 2), (3, 2)]
    new = solve(stair, Open())
    assert len(new) <= 3 and all(octi(new[i], new[i + 1]) for i in range(len(new) - 1)), new
    assert cost(new) < cost(stair)
    L = chamfer([(0, 0), (2, 0), (2, 2)], Open(), 0.2)
    assert len(L) == 4 and all(turn(ang(L[i - 1], L[i]), ang(L[i], L[i + 1])) < 46 for i in (1, 2)), L
    assert connectors((0, 0), (3, 1))[1:] == [[(0, 0), (2, 0), (3, 1)], [(0, 0), (1, 1), (3, 1)],
                                              [(0, 0), (1, 0), (2, 1), (3, 1)]]
    pad = {'shape': 'rect', 'prims': [], 'x': 0, 'y': 0, 'sx': 1, 'sy': 1, 'prot': 0}
    assert abs(seg_pad_dist((2, -1), (2, 1), pad) - 1.5) < 1e-9 and seg_pad_dist((-2, 0), (2, 0), pad) == 0
    return 'smooth geometry ok'
