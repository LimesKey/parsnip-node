"""Lane jogs: a run that steps 0.05-1 mm sideways with a short leg and carries on parallel.
`smooth`'s DP joins existing vertices only, so it collapses a jog only when that is already
clear; `tracks` lists them (row `jog`) and `smooth --jogs` takes them.

A jog at vertices a, c of a chain (run P-a, leg a-c, run c-Q parallel to P-a) collapses two
ways: A extends the leg INTO the first run until it meets the second run's line (the first run
moves over), B the mirror. Either is taken when `Clear` passes the new copper. When exactly
one free GND stitching via blocks every way, `--jogs` lifts it, re-lays the chain, and puts the
via back at its own size at the nearest legal spot within 2.5 mm (stitch's search), or leaves
the jog when there is none. Jogs blocked by one routed signal via: `smooth --jogs --nudge`
drags that via (and its tracks' ends) by the <= 0.3 mm the collapse needs, when `movecheck
via` finds the nudge clean. One jog per chain per pass (the first that collapses): run again
for the next."""
import math
from kpcb_smooth import Clear, chains, simplify, ang, turn, length, seg_pad_dist, on_layer
from kpcb_board import pt_seg_dist, seg_seg_dist
from kpcb_zones import _clearance_fn

OFF = (0.05, 1.0)                                    # sideways step that counts as a jog, mm


def _x(p1, p2, p3, p4):
    """intersection of line p1-p2 with line p3-p4, or None if parallel"""
    d1, d2 = (p2[0] - p1[0], p2[1] - p1[1]), (p4[0] - p3[0], p4[1] - p3[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    t = ((p3[0] - p1[0]) * d2[1] - (p3[1] - p1[1]) * d2[0]) / den
    return (p1[0] + t * d1[0], p1[1] + t * d1[1])


def _fwd(p, x, q):
    """x lies on the ray p->q direction from p (not behind it), past p"""
    return (x[0] - p[0]) * (q[0] - p[0]) + (x[1] - p[1]) * (q[1] - p[1]) > 1e-6


def candidates(pts, k):
    """the collapses of the jog at pts[k], pts[k+1]: [(tag, new pts, changed polyline)]"""
    a, c = pts[k], pts[k + 1]
    out = []
    if k >= 2:
        x = _x(pts[k - 2], pts[k - 1], c, pts[k + 2])
        if x and _fwd(pts[k - 2], x, pts[k - 1]) and _fwd(x, pts[k + 2], c) or x and math.dist(x, c) < 1e-6:
            out.append(('A', simplify(pts[:k - 1] + [x] + pts[k + 1:]), [pts[k - 2], x, c]))
    if k + 3 <= len(pts) - 1:
        y = _x(pts[k + 3], pts[k + 2], pts[k - 1], a)
        if y and _fwd(pts[k + 3], y, pts[k + 2]) and _fwd(y, pts[k - 1], a) or y and math.dist(y, a) < 1e-6:
            out.append(('B', simplify(pts[:k + 1] + [y] + pts[k + 3:]), [a, y, pts[k + 3]]))
    return out


def capped(pts, k):
    """candidates no more than smooth's 9% longer"""
    return [c for c in candidates(pts, k) if length(c[1]) <= length(pts) * 1.09 + 0.02]


def find(b, skip, sel=None, every=False):
    """[(net, ly, w, order, pts, k, offset)] per jog: the first per chain, or every one"""
    out = []
    for net, ly, w, order, pts in chains(b, skip):
        if sel and not sel(net, ly, pts):
            continue
        for k in range(1, len(pts) - 2):
            a, c = pts[k], pts[k + 1]
            d1, d2 = ang(pts[k - 1], a), ang(c, pts[k + 2])
            if turn(d1, d2) > 1 or turn(d1, ang(a, c)) < 1:
                continue
            u = (math.cos(math.radians(d1)), math.sin(math.radians(d1)))
            off = abs((c[0] - a[0]) * u[1] - (c[1] - a[1]) * u[0])
            if OFF[0] <= off <= OFF[1] and capped(pts, k):
                out.append((net, ly, w, order, pts, k, off))
                if not every:
                    break
    return out


def blockers(b, net, ly, w, loc, clr=None):
    """foreign items inside the clearance of the changed polyline: [('via', v) | ('trk', t) |
    ('pad', (ref, pad))]. A geometric list for the report; Clear is the verdict"""
    clr = clr or _clearance_fn(b)[0]
    segs = list(zip(loc, loc[1:]))
    out = []
    for v in b.vias:
        if v['net'] != net:
            g = clr(net, v['net'], 'track', 'via', ly) + v['size'] / 2 + w / 2
            if any(pt_seg_dist((v['x'], v['y']), s0, s1) < g for s0, s1 in segs):
                out.append(('via', v))
    for t in b.tracks:
        if t['net'] != net and t['layer'] == ly:
            g = clr(net, t['net'], 'track', 'track', ly) + (t['w'] + w) / 2
            if any(seg_seg_dist(s0, s1, t['a'], t['b']) < g for s0, s1 in segs):
                out.append(('trk', t))
    for f in b.fps.values():
        for p in f.pads if f.placed else ():
            if p['net'] != net and on_layer(p, ly) and \
                    any(seg_pad_dist(s0, s1, p) < w / 2 + clr(net, p['net'], 'track', 'pad', ly) for s0, s1 in segs):
                out.append(('pad', (f.ref, p['num'])))
    return out


def describe(bl):
    if not bl:
        return 'boxed in (fill, keepout or edge)'
    kind, it = bl[0]
    one = (f"via {it['net'].split('/')[-1]}{' free' if it.get('free') else ''} {it['x']:.2f},{it['y']:.2f}" if kind == 'via'
           else f"track {it['net'].split('/')[-1]}" if kind == 'trk' else f"pad {it[0]}.{it[1]}")
    return one + (f" +{len(bl) - 1}" if len(bl) > 1 else '')


def verdict(b, net, ly, w, order, pts, k, skip=()):
    """which side collapses now, else what blocks each side (the `tracks` row); a promise of
    `--jogs` only when lifting the via really clears it"""
    ck, why = Clear(b, net, ly, w, pts, order), []
    for tag, new, loc in capped(pts, k):
        if ck.poly(loc):
            return f"fix: smooth --jogs ({tag})"
        if not all(ck.contact_ok(p, c) for p, c in zip(loc, loc[1:])):
            why.append(f"{tag}: would touch its own net's copper somewhere new")
            continue
        bl, note = blockers(b, net, ly, w, loc), ''
        if free_gnd(bl):
            b.vias.remove(bl[0][1])
            note = ' (--jogs moves it)' if Clear(b, net, ly, w, pts, order).poly(loc) else ''
            b.vias.append(bl[0][1])
        elif signal_via(bl) and bl[0][1]['net'] not in skip:
            note = ' (--jogs --nudge may drag it)'
        why.append(f"{tag}: {describe(bl)}{note}")
    return '; '.join(why)


def free_gnd(bl):
    return len(bl) == 1 and bl[0][0] == 'via' and bl[0][1]['net'] == 'GND' and bl[0][1].get('free')


def signal_via(bl):
    return len(bl) == 1 and bl[0][0] == 'via' and not bl[0][1].get('free')


def pass_jogs(b, skip, sel, nudge=False):
    """collapse jogs in memory, one per chain, each seen by the next. Returns (taken for
    write_chains, via moves [(via, (x, y), [(track as it was, new a, new b)])], report rows). A lifted
    GND via goes back through stitch's legal_spots; with nudge, a signal via is dragged"""
    from kpcb_power import legal_spots, net_fills
    clr, n0 = _clearance_fn(b)[0], len(b.tracks)
    taken, moves, rows, tomb, touched = [], [], [], {}, set()

    def lay(order, net, ly, w, new):
        for i in order:                              # the old chain leaves the model...
            tomb[i] = b.tracks[i]
            b.tracks[i] = dict(b.tracks[i], net='', layer='_gone')
        for s0, s1 in zip(new, new[1:]):             # ...and the new one joins it
            b.tracks.append({'net': net, 'layer': ly, 'w': w, 'a': s0, 'b': s1, 'len': math.dist(s0, s1),
                             'mid': ((s0[0] + s1[0]) / 2, (s0[1] + s1[1]) / 2), 'uuid': None, 'locked': False})

    def unlay(order, m):
        for i in order:
            b.tracks[i] = tomb.pop(i)
        del b.tracks[m:]

    for net, ly, w, order, pts, k, off in find(b, skip, sel, every=True):
        if any(i in tomb or id(b.tracks[i]) in touched for i in order):
            continue                                 # an earlier edit moved this chain's copper
        cands = capped(pts, k)
        ck = Clear(b, net, ly, w, pts, order)
        done = next(((tag, new, '') for tag, new, loc in cands if ck.poly(loc)), None)
        if done:
            lay(order, net, ly, w, done[1])
        for tag, new, loc in cands if not done else ():
            bl = blockers(b, net, ly, w, loc, clr)
            if free_gnd(bl):
                v = bl[0][1]
                b.vias.remove(v)
                if Clear(b, net, ly, w, pts, order).poly(loc):
                    m = len(b.tracks)
                    lay(order, net, ly, w, new)
                    spot = legal_spots(b, 'GND', v['x'], v['y'], 0.0, 2.5, v['size'], v['drill'], 1, False,
                                       net_fills(b, 'GND'), [(x['x'], x['y'], x['drill']) for x in b.vias])
                    if spot:
                        x, y, d = spot[0]
                        b.vias.append(dict(v, x=x, y=y))
                        moves.append((v, (x, y), []))
                        done = (tag, new, f", GND via {v['x']:.2f},{v['y']:.2f} -> {x:.2f},{y:.2f} ({d:.2f} mm)")
                        break
                    unlay(order, m)
                    rows.append((net, ly, pts[k], off, f"{tag}: lifting GND via {v['x']:.2f},{v['y']:.2f} clears it, "
                                                        f"but the via has no legal spot within 2.5 mm: left"))
                b.vias.append(v)
            elif nudge and signal_via(bl) and bl[0][1]['net'] not in skip:   # never an RF/USB/pair via
                got = _nudge(b, bl[0][1], net, ly, w, pts, order, loc)
                if got:
                    to, moved, note = got
                    moves.append((bl[0][1], to, [(dict(t), a2, b2) for t, a2, b2 in moved]))   # as they were
                    for t, a2, b2 in moved:              # the drag stays in the model
                        touched.add(id(t))
                        t.update(a=a2, b=b2, len=math.dist(a2, b2), mid=((a2[0] + b2[0]) / 2, (a2[1] + b2[1]) / 2))
                    bl[0][1]['x'], bl[0][1]['y'] = to
                    lay(order, net, ly, w, new)
                    done = (tag, new, ', ' + note)
                    break
        if done:
            taken.append((0.0, net, ly, w, order, pts, done[1]))
            rows.append((net, ly, pts[k], off, f"{done[0]}: collapsed{done[2]}"))
    for i, t in tomb.items():                            # the writers read the original tracks
        b.tracks[i] = t
    del b.tracks[n0:]
    return taken, moves, rows


def _nudge(b, v, net, ly, w, pts, order, loc):
    """((x, y), dragged [(track, a, b)], note) when dragging signal via v (its track ends with
    it) <= 0.3 mm straight away from the changed polyline is clean by movecheck's via drag and
    clears the collapse, else None"""
    from kpcb_move import via_drag
    c = (v['x'], v['y'])
    _d, s0, s1 = min((pt_seg_dist(c, p0, p1), p0, p1) for p0, p1 in zip(loc, loc[1:]))
    L = math.dist(s0, s1) or 1.0
    nx, ny = -(s1[1] - s0[1]) / L, (s1[0] - s0[0]) / L
    if (c[0] - s0[0]) * nx + (c[1] - s0[1]) * ny < 0:
        nx, ny = -nx, -ny
    for step in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3):
        to = (round(c[0] + nx * step, 4), round(c[1] + ny * step, 4))
        new, moved = via_drag(b, v, to)
        if new:
            continue
        keep = [(t, dict(t)) for t, _a, _b in moved]
        v['x'], v['y'] = to                              # try the collapse with the via (and its tracks) moved
        for t, a2, b2 in moved:
            t.update(a=a2, b=b2)
        ok = Clear(b, net, ly, w, pts, order).poly(loc)
        v['x'], v['y'] = c
        for t, old in keep:
            t.update(a=old['a'], b=old['b'])
        if ok:
            return to, moved, f"{v['net'].split('/')[-1]} via nudged {step:.2f} mm to {to[0]:.3f},{to[1]:.3f}"
    return None
