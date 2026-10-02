"""kpcb.py `movecheck`: what moving a part (or one via) would newly break - foreign
copper inside the clearance, copper too close to the edge, a courtyard overlap -
measured before vs after so only NEW hits print. It never writes the board."""
import math, re
from kcommon import unesc_disp
from kpcb_board import bbox, grow, hit, pad_box, poly_dist, pt_box_dist, seg_box_dist, xf
from kpcb_zones import _clearance_fn


def cu_layers(layers, cu):
    """copper layers an item is on: '*.Cu' or a through span means all of them"""
    if any(l.startswith('*') for l in layers):
        return set(cu)
    return {l for l in layers if l in cu}


def via_layers(v, cu):
    ix = sorted(cu.index(l) for l in v['layers'] if l in cu)
    return set(cu[ix[0]:ix[-1] + 1]) if len(ix) > 1 else set(cu)


class World:
    """the board's copper and courtyards as obstacles, minus what moves with the item:
    the part's own pads and the tracks ending on them (those drag), or the via"""

    def __init__(self, b, ref=None, via=None):
        self.b, cu = b, b.copper
        self.clr = _clearance_fn(b)[0]
        self.ec = b.edge_clearance()
        f = b.fps.get(ref)
        self.pads = [(g.ref, p['num'], p['net'], cu_layers(p['layers'], cu) if not p['drill'] else set(cu),
                      pad_box(p)) for g in b.placed() if g is not f for p in g.pads
                     if p['kind'] != 'np_thru_hole']
        own = set()
        for i, t in enumerate(b.tracks):
            ends = (t['a'], t['b'])
            if f and any(t['net'] == p['net'] and pt_box_dist(e, pad_box(p)) <= t['w'] / 2
                         for p in f.pads for e in ends):
                own.add(i)
            if via is not None and t['net'] == via['net'] and \
                    any(math.dist(e, (via['x'], via['y'])) <= via['size'] / 2 for e in ends):
                own.add(i)
        self.own = own
        self.tracks = [t for i, t in enumerate(b.tracks) if i not in own]
        self.vias = [v for v in b.vias if v is not via]
        self.parts = [g for g in b.placed() if g is not f and not b.is_hole(g)]

    def hits(self, items, outline=None, back=False):
        """{key: message} for items [(name, net, layers, box)] and an optional moved
        courtyard (rings): every foreign object inside its clearance"""
        out = {}
        if not items:
            return out
        zone = grow(bbox([q for *_x, bb in items for q in (bb[:2], bb[2:])]), 2.0)
        for name, net, L, bb in items:
            for ref, num, n2, L2, b2 in self.pads:
                if (net and n2 == net) or not (L & L2) or not hit(zone, b2):
                    continue
                need, gap = self.clr(net, n2), _box_gap(bb, b2)
                if gap < need - 1e-6:
                    out[('pad', ref, num, name)] = (f"{name} {gap:.2f} mm from {ref}.{num} "
                                                     f"({unesc_disp(n2) or 'no net'}), needs {need:.2f}")
            for t in self.tracks:
                if (net and t['net'] == net) or t['layer'] not in L or \
                        not hit(zone, (min(t['a'][0], t['b'][0]) - t['w'], min(t['a'][1], t['b'][1]) - t['w'],
                                       max(t['a'][0], t['b'][0]) + t['w'], max(t['a'][1], t['b'][1]) + t['w'])):
                    continue
                need, gap = self.clr(net, t['net']), seg_box_dist(t['a'], t['b'], bb) - t['w'] / 2
                if gap < need - 1e-6:
                    out[('trk', id(t), name)] = (f"{name} {max(gap, 0):.2f} mm from a {unesc_disp(t['net'])} "
                                                 f"track on {t['layer']} @{t['mid'][0]:.2f},{t['mid'][1]:.2f}, "
                                                 f"needs {need:.2f}")
            for v in self.vias:
                if (net and v['net'] == net) or not (L & via_layers(v, self.b.copper)) or \
                        not hit(zone, grow((v['x'], v['y'], v['x'], v['y']), v['size'])):
                    continue
                need, gap = self.clr(net, v['net']), pt_box_dist((v['x'], v['y']), bb) - v['size'] / 2
                if gap < need - 1e-6:
                    out[('via', id(v), name)] = (f"{name} {max(gap, 0):.2f} mm from a {unesc_disp(v['net'])} "
                                                 f"via @{v['x']:.2f},{v['y']:.2f}, needs {need:.2f}")
            if self.b.edge_segs:
                gap = min(seg_box_dist(u, w, bb) for u, w in self.b.edge_segs)
                if gap < self.ec - 1e-6:
                    out[('edge', name)] = f"{name} {gap:.2f} mm from the board edge, needs {self.ec:g}"
        if outline:
            ob = bbox([q for r in outline for q in r])
            for g in self.parts:
                if g.back == back and hit(grow(ob, .01), g.crtyd):
                    d, ov = poly_dist(outline, g.outline)
                    if ov:
                        out[('crt', g.ref)] = f"courtyard overlaps {g.ref} ({g.value})"
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
    w = World(b, ref=f.ref)
    cur_items, cur_rings = placed_as(f, f.x, f.y, f.rot, b.copper)
    before = w.hits(cur_items, cur_rings, f.back)
    if a.scan:
        return _scan(b, a, f, w, before)
    try:
        x, y = float(a.args[1]), float(a.args[2])
        rot = float(a.args[3]) if len(a.args) > 3 else None
    except (IndexError, ValueError):
        print("movecheck REF X Y [ROT]  (board mm, KiCad's page origin)"); return 1
    items, rings = placed_as(f, x, y, rot, b.copper)
    new, gone = _report(before, w.hits(items, rings, f.back))
    print(f"movecheck {f.ref} -> {x:.3f},{y:.3f}" + (f" rot {rot:g}" if rot is not None else '')
          + f"   (now {f.x:.3f},{f.y:.3f} rot {f.rot:g}; dx {x - f.x:+.3f} dy {y - f.y:+.3f})")
    print(f"  clearances: netclasses + .kicad_dru net pairs; copper to edge {w.ec:g} mm; pads as boxes; "
          f"{len(w.own)} own track(s) drag, not checked")
    for k in sorted(new, key=lambda k: new[k]):
        print(f"  NEW   {new[k]}")
    if gone:
        print(f"  cleared {len(gone)} hit(s) the current spot has")
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
        items, rings = placed_as(f, x, y, None, b.copper)
        new, _gone = _report(before, w.hits(items, rings, f.back))
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


def _via(b, a):
    try:
        (x, y), (nx, ny) = [tuple(float(c) for c in s.split(',')) for s in a.args[1:3]]
    except ValueError:
        print("movecheck via X,Y NX,NY"); return 1
    v = min(b.vias, key=lambda v: math.dist((v['x'], v['y']), (x, y)), default=None)
    if v is None or math.dist((v['x'], v['y']), (x, y)) > 0.15:
        print(f"no via within 0.15 mm of {x:g},{y:g}"); return 1
    w = World(b, via=v)
    tied = [t for t in b.tracks if t['net'] == v['net'] and
            any(math.dist(e, (v['x'], v['y'])) <= v['size'] / 2 for e in (t['a'], t['b']))]
    r, L = v['size'] / 2, via_layers(v, b.copper)
    box = lambda cx, cy: (cx - r, cy - r, cx + r, cy + r)                  # noqa: E731
    before = w.hits([('via', v['net'], L, box(v['x'], v['y']))])
    new, gone = _report(before, w.hits([('via', v['net'], L, box(nx, ny))]))
    print(f"movecheck via {unesc_disp(v['net'])} {v['size']:g}/{v['drill']:g} mm at "
          f"{v['x']:.3f},{v['y']:.3f} -> {nx:.3f},{ny:.3f}")
    if tied:
        print(f"  tied: {len(tied)} track(s) end on it ("
              + ', '.join(f"{t['layer']} {t['w']:g} mm" for t in tied[:4])
              + "): moving it drags them, so re-check their new path")
    else:
        print("  tied: nothing - a bare stitching via, free to move (the pour reconnects it on refill)")
    for k in sorted(new, key=lambda k: new[k]):
        print(f"  NEW   {new[k]}")
    print(f"  => {'CLEAR: nothing new' if not new else f'{len(new)} new hit(s)'}")
    return 2 if new else 0
