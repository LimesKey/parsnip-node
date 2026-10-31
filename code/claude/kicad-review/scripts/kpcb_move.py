"""kpcb.py `movecheck`: what moving a part (or one via) would newly break - foreign
copper inside the clearance, copper too close to the edge, a courtyard overlap, and
the tracks the move drags swinging into foreign copper - measured before vs after so
only NEW hits print. It never writes the board. `drag_set`/`drag_plan` are KiCad's
Drag (D) model, shared with the `move` writer; `--no-drag` is KiCad's Move (M)."""
import math, re
from collections import defaultdict
from kcommon import unesc_disp
from kpcb_board import bbox, grow, hit, pad_box, poly_dist, pt_box_dist, pt_seg_dist, seg_box_dist, seg_seg_dist, xf
from kpcb_copper import _local, pad_dist
from kpcb_zones import _clearance_fn


def cu_layers(layers, cu):
    """copper layers an item is on: '*.Cu' or a through span means all of them"""
    if any(l.startswith('*') for l in layers):
        return set(cu)
    return {l for l in layers if l in cu}


def via_layers(v, cu):
    ix = sorted(cu.index(l) for l in v['layers'] if l in cu)
    return set(cu[ix[0]:ix[-1] + 1]) if len(ix) > 1 else set(cu)


def _on_pad(p, e):
    u, v = _local(p, e)
    return abs(u) <= p['sx'] / 2 + 1e-6 and abs(v) <= p['sy'] / 2 + 1e-6


def fp_local(f, pt):
    """a board point in f's own frame (the inverse of xf at f's placement)"""
    dx, dy = pt[0] - f.x, pt[1] - f.y
    if not f.rot:
        return dx, dy
    t = math.radians(f.rot)
    c, s = math.cos(t), math.sin(t)
    return dx * c - dy * s, dx * s + dy * c


def is_arc(t):
    return math.dist(t['mid'], ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)) > 1e-4


def drag_set(b, f):
    """(ends, vias, stay) - what KiCad's Drag (D) takes along when f moves. vias: the
    same-net vias inside its pads (they move with the pad). ends [(track, 'a'|'b')]: segment
    ends on one of its pads ON A LAYER THAT PAD HAS (a through pad: every layer), or on a
    via that moves; an end on a via that stays is tied to it and stays too. stay: such ends
    on an arc (an arc cannot bend), counted."""
    cu = b.copper
    vias = [v for v in b.vias if v['net'] and any(p['net'] == v['net'] and _on_pad(p, (v['x'], v['y'])) for p in f.pads)]
    vid = {id(v) for v in vias}
    vnet = defaultdict(list)
    for v in b.vias:
        vnet[v['net']].append(v)
    ends, stay = [], 0
    for t in b.tracks:
        if not t['net']:
            continue
        mine = []
        for k in ('a', 'b'):
            e = t[k]
            on = [v for v in vnet[t['net']] if math.dist(e, (v['x'], v['y'])) <= v['size'] / 2
                  and t['layer'] in via_layers(v, cu)]
            if on:
                if any(id(v) in vid for v in on):
                    mine.append(k)
            elif any(p['net'] == t['net'] and (p['drill'] or t['layer'] in cu_layers(p['layers'], cu)) and _on_pad(p, e)
                     for p in f.pads):
                mine.append(k)
        if mine and is_arc(t):
            stay += len(mine)
        else:
            ends += [(t, k) for k in mine]
    return ends, vias, stay


def pad_ends(b, f):
    """(ends, vias in its pads): drag_set's ends that sit on f's pads themselves, not on a via
    in them - what KiCad's Move (M) leaves behind, the via and its own ends staying together"""
    ends, vias, stay = drag_set(b, f)
    onv = lambda t, k: any(v['net'] == t['net'] and math.dist(t[k], (v['x'], v['y'])) <= v['size'] / 2   # noqa: E731
                           for v in vias)
    return [(t, k) for t, k in ends if not onv(t, k)], vias, stay


def drag_plan(b, f, x, y, rot):
    """drag_set with destinations for f at x,y,rot: ({id(track): (track, new a, new b)},
    [(via, new x, new y)], stay)"""
    r2 = rot if rot is not None else f.rot
    T = lambda p: xf(*fp_local(f, p), x, y, r2)               # noqa: E731
    ends, vias, stay = drag_set(b, f)
    segs = {}
    for t, k in ends:
        _t, a, c = segs.get(id(t), (t, t['a'], t['b']))
        segs[id(t)] = (t, T(a) if k == 'a' else a, T(c) if k == 'b' else c)
    return segs, [(v, *T((v['x'], v['y']))) for v in vias], stay


class Hit(str):
    """a hit's message carrying its gap (mm), so a caller can tell one that got worse"""
    def __new__(cls, msg, gap):
        h = super().__new__(cls, msg)
        h.gap = gap
        return h


class World:
    """the board's copper and courtyards as obstacles, minus what moves with the item:
    the part's own pads and the tracks ending on them (those drag), or the via. drag=False:
    KiCad's Move (M), its tracks and in-pad vias stay put and are obstacles like the rest"""

    def __init__(self, b, ref=None, via=None, skip=(), drag=True):
        self.b, cu, self.drag = b, b.copper, drag
        self.clr = _clearance_fn(b)[0]
        self.ec = b.edge_clearance()
        movers = [g for g in (b.fps.get(r) for r in (ref, *skip)) if g]     # what moves: not an obstacle
        self.pads = [(g.ref, p['num'], p['net'], cu_layers(p['layers'], cu) if not p['drill'] else set(cu),
                      pad_box(p)) for g in b.placed() if g not in movers for p in g.pads
                     if p['kind'] != 'np_thru_hole']
        own, moving = set(), {id(via)} if via is not None else set()
        for g in movers if drag else ():                 # what drags with them (drag_set)
            ends, vs, _stay = drag_set(b, g)
            own |= {id(t) for t, _k in ends}
            moving |= {id(v) for v in vs}
            own |= {id(t) for t in b.tracks if is_arc(t) and t['net'] and
                    any(p['net'] == t['net'] and _on_pad(p, e) for p in g.pads for e in (t['a'], t['b']))}
        for t in b.tracks:
            if via is not None and t['net'] == via['net'] and \
                    any(math.dist(e, (via['x'], via['y'])) <= via['size'] / 2 for e in (t['a'], t['b'])):
                own.add(id(t))
        self.own = own
        self.tracks = [t for t in b.tracks if id(t) not in own]
        self.vias = [v for v in b.vias if id(v) not in moving]
        self.parts = [g for g in b.placed() if g not in movers and not b.is_hole(g)]
        # 2 mm buckets, so a hit test reads only the copper around it
        self.vl = {id(v): via_layers(v, cu) for v in self.vias}
        self.grid = {}
        def put(kind, obj, bb):
            for cx in range(math.floor(bb[0] / 2), math.floor(bb[2] / 2) + 1):
                for cy in range(math.floor(bb[1] / 2), math.floor(bb[3] / 2) + 1):
                    self.grid.setdefault((cx, cy), []).append((kind, obj))
        for pd in self.pads:
            put('p', pd, pd[4])
        for t in self.tracks:
            put('t', t, (min(t['a'][0], t['b'][0]) - t['w'], min(t['a'][1], t['b'][1]) - t['w'],
                         max(t['a'][0], t['b'][0]) + t['w'], max(t['a'][1], t['b'][1]) + t['w']))
        for v in self.vias:
            put('v', v, (v['x'] - v['size'], v['y'] - v['size'], v['x'] + v['size'], v['y'] + v['size']))

    def near(self, zone):
        """the pads, tracks and vias whose bucket meets the zone box, each once"""
        seen, out = set(), {'p': [], 't': [], 'v': []}
        for cx in range(math.floor(zone[0] / 2), math.floor(zone[2] / 2) + 1):
            for cy in range(math.floor(zone[1] / 2), math.floor(zone[3] / 2) + 1):
                for kind, obj in self.grid.get((cx, cy), ()):
                    if id(obj) not in seen:
                        seen.add(id(obj))
                        out[kind].append(obj)
        return out

    def hits(self, items, outline=None, back=False):
        """{key: message} for items [(name, net, layers, box)] and an optional moved
        courtyard (rings): every foreign object inside its clearance"""
        out = {}
        if not items:
            return out
        zone = grow(bbox([q for *_x, bb in items for q in (bb[:2], bb[2:])]), 2.0)
        near = self.near(zone)
        for name, net, L, bb in items:
            k = 'via' if name.startswith('via') else 'pad'
            # a via is a disk, not its bounding square (that reads ~0.12 mm too close on a diagonal)
            c, r = ((bb[0] + bb[2]) / 2, (bb[1] + bb[3]) / 2), (bb[2] - bb[0]) / 2
            disk = k == 'via'
            for ref, num, n2, L2, b2 in near['p']:
                if (net and n2 == net) or not (L & L2) or not hit(zone, b2):
                    continue
                need = max(self.clr(net, n2, k, 'pad', ly) for ly in L & L2)
                gap = pt_box_dist(c, b2) - r if disk else _box_gap(bb, b2)
                if gap < need - 1e-6:
                    out[('pad', ref, num, name)] = Hit(f"{name} {gap:.2f} mm from {ref}.{num} "
                                                     f"({unesc_disp(n2) or 'no net'}), needs {need:.2f}", gap)
            for t in near['t']:
                if (net and t['net'] == net) or t['layer'] not in L or \
                        not hit(zone, (min(t['a'][0], t['b'][0]) - t['w'], min(t['a'][1], t['b'][1]) - t['w'],
                                       max(t['a'][0], t['b'][0]) + t['w'], max(t['a'][1], t['b'][1]) + t['w'])):
                    continue
                need = self.clr(net, t['net'], k, 'track', t['layer'])
                gap = (pt_seg_dist(c, t['a'], t['b']) - r if disk else seg_box_dist(t['a'], t['b'], bb)) - t['w'] / 2
                if gap < need - 1e-6:
                    out[('trk', id(t), name)] = Hit(f"{name} {max(gap, 0):.2f} mm from a {unesc_disp(t['net'])} "
                                                 f"track on {t['layer']} @{t['mid'][0]:.2f},{t['mid'][1]:.2f}, "
                                                 f"needs {need:.2f}", gap)
            for v in near['v']:
                if (net and v['net'] == net) or not (L & self.vl[id(v)]) or \
                        not hit(zone, grow((v['x'], v['y'], v['x'], v['y']), v['size'])):
                    continue
                need = max(self.clr(net, v['net'], k, 'via', ly) for ly in L & self.vl[id(v)])
                gap = (math.dist((v['x'], v['y']), c) - r if disk else pt_box_dist((v['x'], v['y']), bb)) - v['size'] / 2
                if gap < need - 1e-6:
                    out[('via', id(v), name)] = Hit(f"{name} {max(gap, 0):.2f} mm from a {unesc_disp(v['net'])} "
                                                 f"via @{v['x']:.2f},{v['y']:.2f}, needs {need:.2f}", gap)
            if self.b.edge_segs:
                gap = min(pt_seg_dist(c, u, w) - r if disk else seg_box_dist(u, w, bb) for u, w in self.b.edge_segs)
                if gap < self.ec - 1e-6:
                    out[('edge', name)] = Hit(f"{name} {gap:.2f} mm from the board edge, needs {self.ec:g}", gap)
        if outline:
            ob = bbox([q for r in outline for q in r])
            for g in self.parts:
                if g.back == back and hit(grow(ob, .01), g.crtyd):
                    d, ov = poly_dist(outline, g.outline)
                    if ov:
                        out[('crt', g.ref)] = Hit(f"courtyard overlaps {g.ref} ({g.value})", 0.0)
        return out


    def seg_hits(self, segs):
        """{key: message} for dragged track segments [(name, net, layer, a, b, w)]: foreign
        pads, tracks and vias inside the clearance, and the edge"""
        out = {}
        if not segs:
            return out
        near = self.near(grow(bbox([q for s in segs for q in s[3:5]]), 2.0))
        for name, net, ly, a, c, w in segs:
            for ref, num, n2, L2, b2 in near['p']:
                if (net and n2 == net) or ly not in L2:
                    continue
                need, gap = self.clr(net, n2, 'track', 'pad', ly), seg_box_dist(a, c, b2) - w / 2
                if gap < need - 1e-6:
                    out[('spad', ref, num, name)] = Hit(f"dragged {name} {max(gap, 0):.2f} mm from {ref}.{num} "
                                                      f"({unesc_disp(n2) or 'no net'}), needs {need:.2f}", gap)
            for t in near['t']:
                if (net and t['net'] == net) or t['layer'] != ly:
                    continue
                need, gap = self.clr(net, t['net'], 'track', 'track', ly), seg_seg_dist(a, c, t['a'], t['b']) - (w + t['w']) / 2
                if gap < need - 1e-6:
                    out[('strk', id(t), name)] = Hit(f"dragged {name} {max(gap, 0):.2f} mm from a {unesc_disp(t['net'])} "
                                                  f"track on {ly} @{t['mid'][0]:.2f},{t['mid'][1]:.2f}, needs {need:.2f}", gap)
            for v in near['v']:
                if (net and v['net'] == net) or ly not in self.vl[id(v)]:
                    continue
                need, gap = self.clr(net, v['net'], 'track', 'via', ly), pt_seg_dist((v['x'], v['y']), a, c) - (w + v['size']) / 2
                if gap < need - 1e-6:
                    out[('svia', id(v), name)] = Hit(f"dragged {name} {max(gap, 0):.2f} mm from a {unesc_disp(v['net'])} "
                                                  f"via @{v['x']:.2f},{v['y']:.2f}, needs {need:.2f}", gap)
            if self.b.edge_segs:
                gap = min(seg_seg_dist(a, c, u, e) for u, e in self.b.edge_segs) - w / 2
                if gap < self.ec - 1e-6:
                    out[('sedge', name)] = Hit(f"dragged {name} {max(gap, 0):.2f} mm from the board edge, needs {self.ec:g}", gap)
        return out

    def move_hits(self, f, x, y, rot):
        """every hit f has at x,y,rot (None: where it is): its pads and courtyard, the vias
        inside its pads, and the segments its move drags"""
        if x is None:
            x, y, rot = f.x, f.y, f.rot
        segs, vias, _stay = drag_plan(self.b, f, x, y, rot) if self.drag else ({}, [], 0)
        items, rings = placed_as(f, x, y, rot, self.b.copper)
        items += [(f"via@{v['x']:.2f},{v['y']:.2f}", v['net'], via_layers(v, self.b.copper),
                   (vx - v['size'] / 2, vy - v['size'] / 2, vx + v['size'] / 2, vy + v['size'] / 2)) for v, vx, vy in vias]
        out = self.hits(items, rings, f.back)
        out.update(self.seg_hits([(f"{t['net'].split('/')[-1]} {t['layer']} @{t['mid'][0]:.2f},{t['mid'][1]:.2f}",
                                   t['net'], t['layer'], a, c, t['w']) for t, a, c in segs.values()]))
        return out


def _box_gap(a, b):
    dx = max(0.0, a[0] - b[2], b[0] - a[2])
    dy = max(0.0, a[1] - b[3], b[1] - a[3])
    return math.hypot(dx, dy)


def placed_as(f, x, y, rot, cu):
    """(pad items, courtyard rings) for footprint f sitting at x,y,rot"""
    dr = (rot - f.rot) if rot is not None else 0.0
    T = lambda px, py: xf(px - f.x, py - f.y, x, y, dr)        # noqa: E731
    items = []
    for p in f.pads:
        if p['kind'] == 'np_thru_hole':                      # a hole, no copper
            continue
        q = dict(p, prot=p['prot'] + dr)
        q['x'], q['y'] = T(p['x'], p['y'])
        L = set(cu) if p['drill'] else cu_layers(p['layers'], cu)
        if L:
            items.append((f"{f.ref}.{p['num']}", p['net'], L, pad_box(q)))
    rings = [[T(*pt) for pt in r] for r in f.outline]
    return items, rings


def _report(before, after):
    new = {k: v for k, v in after.items() if k not in before}
    gone = [k for k in before if k not in after]
    return new, gone


def c_movecheck(b, a):
    if a.args[:1] == ['via']:
        return _via(b, a)
    if not a.args or a.args[0] not in b.fps:
        print("movecheck REF X Y [ROT] | REF --scan x=X y=Y0..Y1 | via X,Y NX,NY"); return 1
    f = b.fps[a.args[0]]
    w = World(b, ref=f.ref, drag=not a.no_drag)
    before = w.move_hits(f, None, None, None)
    if a.scan:
        return _scan(b, a, f, w, before)
    try:
        x, y = float(a.args[1]), float(a.args[2])
        rot = float(a.args[3]) if len(a.args) > 3 else None
    except (IndexError, ValueError):
        print("movecheck REF X Y [ROT]  (board mm, KiCad's page origin)"); return 1
    new, gone = _report(before, w.move_hits(f, x, y, rot))
    print(f"movecheck {f.ref} -> {x:.3f},{y:.3f}" + (f" rot {rot:g}" if rot is not None else '')
          + f"   (now {f.x:.3f},{f.y:.3f} rot {f.rot:g}; dx {x - f.x:+.3f} dy {y - f.y:+.3f})")
    print(f"  clearances: netclasses + .kicad_dru net pairs and type rules; copper to edge {w.ec:g} mm; pads as "
          + (f"boxes; {len(w.own)} own track(s) drag, checked at their new position" if w.drag else
             "boxes; --no-drag (Move, M): its tracks and in-pad vias stay, obstacles like the rest"))
    for k in sorted(new, key=lambda k: new[k]):
        print(f"  NEW   {new[k]}")
    if gone:
        print(f"  cleared {len(gone)} hit(s) the current spot has")
    if not w.drag:
        ends, vs, stay = pad_ends(b, f)
        if ends or stay or vs:
            print(f"  left behind: {len(ends) + stay} track end(s) on its pads, {len(vs)} via(s) in them - "
                  "re-link them to the moved pads")
    print(f"  => {'CLEAR: nothing new' if not new else f'{len(new)} new hit(s)'}"
          + (f"  ({len(before)} at the current spot, unchanged)" if before and not new else ''))
    return 2 if new else 0


def _scan(b, a, f, w, before):
    """slide f's centre along x=X (y range) or y=Y (x range); print the clear stretches"""
    spec = dict(re.findall(r'([xy])=([-\d.]+(?:\.\.[-\d.]+)?)', ' '.join(a.scan)))
    rng = [k for k, v in spec.items() if '..' in v]
    if len(spec) != 2 or len(rng) != 1:
        print("--scan wants one fixed axis and one range: x=113.3 y=81..87"); return 1
    ax = rng[0]
    lo, hi = sorted(float(v) for v in spec[ax].split('..'))
    fixed = float(spec['y' if ax == 'x' else 'x'])
    step = max(a.step, 0.005)
    rows, t = [], lo
    while t <= hi + 1e-9:
        x, y = (t, fixed) if ax == 'x' else (fixed, t)
        new, _gone = _report(before, w.move_hits(f, x, y, None))
        rows.append((t, new))
        t = round(t + step, 6)
    print(f"movecheck {f.ref} --scan {ax} {lo:g}..{hi:g} at {'y' if ax == 'x' else 'x'}={fixed:g}, "
          f"step {step:g}   (now {f.x:.3f},{f.y:.3f})")
    i = 0
    while i < len(rows):
        j = i
        while j + 1 < len(rows) and bool(rows[j + 1][1]) == bool(rows[i][1]):
            j += 1
        s0, s1 = rows[i][0], rows[j][0]
        if not rows[i][1]:
            print(f"  {ax} {s0:8.3f} .. {s1:8.3f}  clear  (middle {ax}={(s0 + s1) / 2:.3f})")
        else:
            why = sorted({m.split(' mm from ')[-1].split(', needs')[0] if ' mm from ' in m else m
                          for _t, n in rows[i:j + 1] for m in n.values()})
            print(f"  {ax} {s0:8.3f} .. {s1:8.3f}  blocked by {', '.join(why[:4])}"
                  + (f" +{len(why) - 4}" if len(why) > 4 else ''))
        i = j + 1
    return 0


def via_plan(b, v, to, keep=frozenset(), tracks=None):
    """[(track, new a, new b)]: KiCad's Drag of via v to `to`: every end inside the via goes to
    `to`, an end on the rim stays, as does a track in `keep` (ids). A track with both ends inside
    drags only its nearer one when its far end joins copper that stays (a kept or rim track end, a
    pad: it then stretches to keep that join), else it folds to `to` (zero length: the writer
    deletes it; dragging one end of a chain of in-via slivers left the next one dangling, JP13
    2026-10-28). tracks: the candidates (default every track), for a search asking per spot"""
    c, r = (v['x'], v['y']), v['size'] / 2
    pool = [t for t in (b.tracks if tracks is None else tracks) if t['net'] == v['net']]
    ins = lambda e: math.dist(e, c) < r - 1e-3                                   # noqa: E731
    mine = [t for t in pool if id(t) not in keep and (ins(t['a']) or ins(t['b']))]
    drag = {id(t) for t in mine}
    def stays(t, e):
        tol = t['w'] / 2
        return any(o is not t and o['layer'] == t['layer'] and math.dist(e, q) <= min(tol, o['w'] / 2)
                   and (id(o) not in drag or not ins(q)) for o in pool for q in (o['a'], o['b'])) or \
            any(p['net'] == v['net'] and (p['drill'] or t['layer'] in p['layers']) and pad_dist(p, e, True) <= 0
                for f in b.placed() for p in f.pads)
    moved = []
    for t in mine:
        ia, ib = ins(t['a']), ins(t['b'])
        if ia and ib:
            far = 'b' if math.dist(t['b'], c) >= math.dist(t['a'], c) else 'a'
            if stays(t, t[far]):
                ia, ib = far == 'b', far == 'a'
        moved.append((t, to if ia else t['a'], to if ib else t['b']))
    return moved


def via_drag(b, v, to, w=None, keep=frozenset()):
    """(new hits {key: message}, [(track, new a, new b)]): dragging via v to `to` with the track
    ends on it (via_plan), against what it and those tracks already hit where they are. w: a
    World(b, via=v) built once for a search over many spots (one per spot took ~4 min)"""
    w = w or World(b, via=v)
    c, r, L = (v['x'], v['y']), v['size'] / 2, via_layers(v, b.copper)
    on = w.__dict__.setdefault('_vdt', {}).setdefault(id(v), [t for t in b.tracks if t['net'] == v['net']])
    moved = via_plan(b, v, to, keep, on)
    box = lambda p: (p[0] - r, p[1] - r, p[0] + r, p[1] + r)                       # noqa: E731
    segs = lambda lst: [(f"{t['net'].split('/')[-1]} {t['layer']} track {k + 1}", t['net'], t['layer'], a, e, t['w'])  # noqa: E731
                        for k, (t, a, e) in enumerate(lst)]
    memo = w.__dict__.setdefault('_vdb', {})
    key = (id(v), frozenset(keep))
    if key not in memo:                              # what it hits where it stands: once per via
        memo[key] = w.hits([('via', v['net'], L, box(c))])
        memo[key].update(w.seg_hits(segs([(t, t['a'], t['b']) for t, _a, _b in moved])))
    after = w.hits([('via', v['net'], L, box(to))])
    after.update(w.seg_hits(segs(moved)))
    was = memo[key]                                  # a hit it already had counts when it got worse
    return {k: m for k, m in after.items() if k not in was or m.gap < was[k].gap - 1e-4}, moved


def _via(b, a):
    try:
        (x, y), (nx, ny) = [tuple(float(c) for c in s.split(',')) for s in a.args[1:3]]
    except ValueError:
        print("movecheck via X,Y NX,NY"); return 1
    v = min(b.vias, key=lambda v: math.dist((v['x'], v['y']), (x, y)), default=None)
    if v is None or math.dist((v['x'], v['y']), (x, y)) > 0.15:
        print(f"no via within 0.15 mm of {x:g},{y:g}"); return 1
    new, moved = via_drag(b, v, (nx, ny))
    print(f"movecheck via {unesc_disp(v['net'])} {v['size']:g}/{v['drill']:g} mm at "
          f"{v['x']:.3f},{v['y']:.3f} -> {nx:.3f},{ny:.3f}")
    if moved:
        print(f"  tied: {len(moved)} track(s) end on it ("
              + ', '.join(f"{t['layer']} {t['w']:g} mm" for t, *_e in moved[:4])
              + "): dragged with it, their new path checked too")
    else:
        print("  tied: nothing - a bare stitching via, free to move (the pour reconnects it on refill)")
    for k in sorted(new, key=lambda k: new[k]):
        print(f"  NEW   {new[k]}")
    print(f"  => {'CLEAR: nothing new' if not new else f'{len(new)} new hit(s)'}")
    return 2 if new else 0
