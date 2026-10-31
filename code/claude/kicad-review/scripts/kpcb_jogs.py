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
for the next. No collapse may squeeze a plane channel under its min width (`neck_ok`), each
jog seeing the ones laid before it."""
import math
from kpcb_smooth import Clear, Index, chains, keeps, simplify, ang, turn, length, seg_pad_dist, on_layer
from kpcb_board import pt_seg_dist, seg_seg_dist
from kpcb_zones import _clearance_fn
from kpcb_move import via_layers

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


def partial(pts, k, tag, f):
    """collapse `tag` taken a fraction f of the way: the moving run slides f x the offset
    sideways, its two bends sliding along their own lines (A: the lead-in and the jog leg; B
    mirrored), so the jog shrinks to (1 - f) x. (new pts, changed polyline) or None"""
    a, c = pts[k], pts[k + 1]
    if tag == 'A' and k >= 2:
        a2 = (a[0] + f * (c[0] - a[0]), a[1] + f * (c[1] - a[1]))
        d = (a[0] - pts[k - 1][0], a[1] - pts[k - 1][1])
        p1 = _x(pts[k - 2], pts[k - 1], a2, (a2[0] + d[0], a2[1] + d[1]))
        if p1 and _fwd(pts[k - 2], p1, pts[k - 1]) and _fwd(p1, a2, a):
            return simplify(pts[:k - 1] + [p1, a2] + pts[k + 1:]), [pts[k - 2], p1, a2, c]
    if tag == 'B' and k + 3 <= len(pts) - 1:
        c2 = (c[0] + f * (a[0] - c[0]), c[1] + f * (a[1] - c[1]))
        d = (pts[k + 2][0] - c[0], pts[k + 2][1] - c[1])
        q2 = _x(pts[k + 3], pts[k + 2], c2, (c2[0] + d[0], c2[1] + d[1]))
        if q2 and _fwd(pts[k + 3], q2, pts[k + 2]) and _fwd(q2, c2, c):
            return simplify(pts[:k + 1] + [c2, q2] + pts[k + 3:]), [a, c2, q2, pts[k + 3]]
    return None


def slide(pts, m, d):
    """run pts[m]-pts[m+1] moved by vector d, its ends sliding along the neighbouring
    segments' lines (a pair's other leg following a slide). (new pts, changed polyline) or None"""
    if m < 1 or m + 2 > len(pts) - 1:
        return None
    u, v = (pts[m][0] + d[0], pts[m][1] + d[1]), (pts[m + 1][0] + d[0], pts[m + 1][1] + d[1])
    p0, p1 = _x(pts[m - 1], pts[m], u, v), _x(pts[m + 2], pts[m + 1], u, v)
    if not p0 or not p1 or math.dist(p0, pts[m]) > 3 * math.hypot(*d) + 0.05 or \
            math.dist(p1, pts[m + 1]) > 3 * math.hypot(*d) + 0.05:
        return None                                  # a neighbour nearly parallel to the shift
    return simplify(pts[:m] + [p0, p1] + pts[m + 2:]), [pts[m - 1], p0, p1, pts[m + 2]]


STEPS = (0.9, 0.8, 0.7, 0.6, 0.5, 0.4, 0.3, 0.2)  # partial fractions, most first


def find(b, skip, sel=None, every=False, chs=None):
    """[(net, ly, w, order, pts, k, offset)] per jog: the first per chain, or every one
    (chs: chains(b, skip) already walked)"""
    out = []
    for net, ly, w, order, pts in chs if chs is not None else chains(b, skip):
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
    for tag in 'AB':
        f = next((f for f in STEPS for r in [partial(pts, k, tag, f)] if r and ck.poly(r[1])), None)
        if f:
            why.append(f"fix: smooth --jogs slides {tag} {f:.0%} of the way")
            break
    return '; '.join(why)


NECK = 0.25                                          # a plane strip under this between two items is a neck, mm


def neck_ok(b, net, ly, w, old, new, order=(), clr=None):
    """the collapse squeezes no plane channel into a neck: for every other item on ly (a track,
    other nets' vias, pads and fills) near the new polyline where a plane fills between them, refuse
    when the new gap leaves the plane (its zone outline: a strip the old fill dropped can come
    back) a strip of 0..NECK mm (two plane clearances <= gap < them + NECK) of another width than
    before - a strip near the zone's min thickness is kept only in part, a sliver under the min
    width: GPIO_48 In1 slid 0.16 mm, its strip by R59's via 0.035 (dropped) -> 0.195 mm, 0.078 mm
    left (CONNECTION_WIDTH, the 2026-10-27 log's real cause, bisected 2026-10-28). Closing the
    channel outright is no neck. And refuse one that adds a bend inside its own via's or
    pad's antipad off the centre (a cusp: GPIO_4 In1 left its via by a 0.15 mm stub, then 45 deg).
    Laid copper is in the model, so a second jog sees the first"""
    from kpcb_route import _planes
    from kpcb_board import PolyIndex
    pn = [n for n, l in _planes(b) if l == ly and n != net]
    if not pn:
        return True
    clr = clr or _clearance_fn(b)[0]
    zones = [PolyIndex(z.outline) for z in b.zones if z[0] in pn and not z.keepout and z.outline
             and (ly in z[1] or '*.Cu' in z[1])]  # where the plane may fill: a strip the old fill dropped can come back
    segs_o, segs_n = list(zip(old, old[1:])), list(zip(new, new[1:]))
    bb = (min(x for x, _ in new) - 1, min(y for _, y in new) - 1, max(x for x, _ in new) + 1, max(y for _, y in new) + 1)
    inb = lambda p: bb[0] <= p[0] <= bb[2] and bb[1] <= p[1] <= bb[3]       # noqa: E731
    mine = {id(b.tracks[i]) for i in order}
    items = [('track', t['net'], t['w'] / 2, lambda s, t=t: seg_seg_dist(s[0], s[1], t['a'], t['b']), t['mid'])
             for t in b.tracks if t['layer'] == ly and t['net'] not in pn and id(t) not in mine and t['net']
             and (inb(t['a']) or inb(t['b']) or inb(t['mid']))]
    items += [('via', v['net'], v['size'] / 2, lambda s, v=v: pt_seg_dist((v['x'], v['y']), s[0], s[1]), (v['x'], v['y']))
              for v in b.vias if v['net'] != net and v['net'] not in pn and inb((v['x'], v['y']))]
    items += [('pad', p['net'], 0.0, lambda s, p=p: seg_pad_dist(s[0], s[1], p), (p['x'], p['y']))
              for f in b.placed() for p in f.pads if p['net'] != net and p['net'] not in pn and on_layer(p, ly)
              and inb((p['x'], p['y']))]
    for fl in b.fills:                               # another net's band: the plane clears it too (-BATT In1, 2026-10-28)
        fb = fl['bbox']
        if fl['layer'] == ly and fl['net'] not in pn and fl['net'] != net and \
                fb[2] >= bb[0] and fb[0] <= bb[2] and fb[3] >= bb[1] and fb[1] <= bb[3]:
            edges = [(u, v) for u, v in zip(fl['pts'], fl['pts'][1:] + fl['pts'][:1]) if inb(u) or inb(v)]
            if edges:
                near = min(edges, key=lambda e: min(pt_seg_dist(q, e[0], e[1]) for q in new))
                items.append(('zone', fl['net'], 0.0, lambda s, E=edges: min(seg_seg_dist(s[0], s[1], u, v) for u, v in E),
                              ((near[0][0] + near[1][0]) / 2, (near[0][1] + near[1][1]) / 2)))
    # a bend inside its own via's or pad's antipad (other than on the centre) leaves the plane a
    # cusp between the antipad and the track's clearance (GPIO_4 In1, 2026-10-28 repro)
    cz = max(clr(net, q, 'track', 'zone', ly) for q in pn) + w / 2
    anch = [((v['x'], v['y']), v['size'] / 2) for v in b.vias if v['net'] == net and inb((v['x'], v['y']))]
    pads = [p for f in b.placed() for p in f.pads if p['net'] == net and on_layer(p, ly) and inb((p['x'], p['y']))]
    from kpcb_copper import pad_dist
    def cusps(pts):
        return sum(1 for k in range(1, len(pts) - 1) if turn(ang(pts[k - 1], pts[k]), ang(pts[k], pts[k + 1])) > 1 and (
            any(1e-3 < math.dist(pts[k], o) < r + cz for o, r in anch) or
            any(math.dist(pts[k], (p['x'], p['y'])) > 1e-3 and pad_dist(p, pts[k]) < cz for p in pads)))
    if cusps(new) > cusps(old):
        return False
    for kind, n2, r, dist, at in items:
        c = max(clr(net, q, 'track', 'zone', ly) + clr(n2, q, kind, 'zone', ly) for q in pn)
        g_new = min(dist(sg) for sg in segs_n) - w / 2 - r
        if not c <= g_new < c + NECK:
            continue                                 # the plane flows full width, or not at all
        g_old = min(dist(sg) for sg in segs_o) - w / 2 - r
        if abs(g_new - g_old) < 0.02:
            continue                                 # the same strip as before
        (ax, ay), (bx, by) = min(segs_n, key=dist)    # the channel's middle: is the plane there?
        L2 = (bx - ax) ** 2 + (by - ay) ** 2 or 1e-12
        u = min(1.0, max(0.0, ((at[0] - ax) * (bx - ax) + (at[1] - ay) * (by - ay)) / L2))
        mid = ((ax + u * (bx - ax) + at[0]) / 2, (ay + u * (by - ay) + at[1]) / 2)
        if any(mid in ix for ix in zones):
            return False
    return True


def free_gnd(bl):
    return len(bl) == 1 and bl[0][0] == 'via' and bl[0][1]['net'] == 'GND' and bl[0][1].get('free')


def signal_via(bl):
    return len(bl) == 1 and bl[0][0] == 'via' and not bl[0][1].get('free')


def pass_jogs(b, skip, sel, nudge=False):
    """collapse jogs in memory, one per chain, each seen by the next. Returns (taken for
    write_chains, via moves [(via, (x, y), [(track as it was, new a, new b)])], report rows). A lifted
    GND via goes back through stitch's legal_spots; with nudge, a signal via is dragged"""
    from kpcb_power import legal_spots, net_fills
    clr, n0, ix = _clearance_fn(b)[0], len(b.tracks), Index(b)
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
        cands = [c for c in capped(pts, k) if keeps(b, ix, net, ly, w, pts, c[1])]
        ck = Clear(b, net, ly, w, pts, order)
        nk = lambda new: neck_ok(b, net, ly, w, pts, new, order, clr)   # noqa: E731
        done = next(((tag, new, '') for tag, new, loc in cands if ck.poly(loc) and nk(new)), None)
        if done:
            lay(order, net, ly, w, done[1])
        for tag, new, loc in cands if not done else ():
            bl = blockers(b, net, ly, w, loc, clr)
            if free_gnd(bl):
                v = bl[0][1]
                b.vias.remove(v)
                if Clear(b, net, ly, w, pts, order).poly(loc) and nk(new):
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
                got = _nudge(b, bl[0][1], net, ly, w, pts, order, loc, touched)
                if got:                              # the neck judged with the via where it goes
                    vv = bl[0][1]
                    c0, vv['x'], vv['y'] = (vv['x'], vv['y']), *got[0]
                    if not nk(new) or not all(neck_ok(b, vv['net'], l2, vv['size'], [c0, c0], [got[0], got[0]], (), clr)
                                              for l2 in via_layers(vv, b.copper)):   # its antipad moved on every layer
                        bl[0][1]['x'], bl[0][1]['y'] = c0
                        got = None
                if got:
                    to, moved, note = got
                    moves.append((bl[0][1], to, [(dict(t), a2, b2) for t, a2, b2 in moved]))   # as they were
                    for t, a2, b2 in moved:              # the drag stays in the model
                        touched.add(id(t))
                        t.update(a=a2, b=b2, len=math.dist(a2, b2), mid=((a2[0] + b2[0]) / 2, (a2[1] + b2[1]) / 2))
                    bl[0][1]['x'], bl[0][1]['y'] = to
                    touched.add(id(bl[0][1]))
                    lay(order, net, ly, w, new)
                    done = (tag, new, ', ' + note)
                    break
        if not done:                                 # partial: slide the run as far as clears
            done = next(((tag, r[0], f" {f:.0%} of the way (the jog is now {off * (1 - f):.2f} mm)")
                         for f in STEPS for tag in 'AB' for r in [partial(pts, k, tag, f)]
                         if r and keeps(b, ix, net, ly, w, pts, r[0]) and ck.poly(r[1]) and nk(r[0])), None)
            if done:
                lay(order, net, ly, w, done[1])
        if done:
            taken.append((0.0, net, ly, w, order, pts, done[1]))
            rows.append((net, ly, pts[k], off, f"{done[0]}: collapsed{done[2]}"))
    _pairs(b, sel, taken, rows, lay, unlay, tomb)
    for i, t in tomb.items():                            # the writers read the original tracks
        b.tracks[i] = t
    del b.tracks[n0:]
    return taken, moves, rows


def mate_runs(b, mate, ly, run, fresh):
    """the mate leg's tracks parallel to `run` within 1 mm, nearest first, each as a mini
    chain ([prev, it, next] track idx, [prev far end, its ends, next far end]): one plain
    neighbour at each end (no pad, via or junction), so it can slide between them"""
    from kpcb_smooth import Index, q
    ix, out = Index(b), []
    for i, t in enumerate(b.tracks):
        if t['net'] != mate or t['layer'] != ly or fresh([i]) or t['len'] < 1e-3 or \
                min(turn(ang(*run), ang(t['a'], t['b'])), turn(ang(*run), ang(t['b'], t['a']))) > 1:
            continue
        dd = seg_seg_dist(run[0], run[1], t['a'], t['b'])
        if dd > 1.0:
            continue
        nb = []
        for e in (t['a'], t['b']):
            lst = [j for j in ix.ends[(mate, ly, q(e))] if j != i]
            if len(lst) != 1 or ix.on_pad(mate, ly, e) or ix.on_via(mate, ly, e) or fresh(lst) or \
                    b.tracks[lst[0]]['w'] != t['w']:
                break
            u = b.tracks[lst[0]]
            nb.append((lst[0], u['b'] if q(u['a']) == q(e) else u['a']))
        if len(nb) == 2:
            out.append((dd, [nb[0][0], i, nb[1][0]], [nb[0][1], t['a'], t['b'], nb[1][1]]))
    return [(o, p) for _d, o, p in sorted(out)]


def _pairs(b, sel, taken, rows, lay, unlay, tomb):
    """diff-pair legs (skipped above) as a unit: a jog in one leg collapses, fully or in
    part, only with the other leg's parallel run (<= 1 mm off) slid by the same vector, so
    the pair gap holds; both re-laid or neither (C_USB_B_P/N, 2026-10-27)"""
    from kpcb_route import _legs
    fresh = lambda order: any(i in tomb or b.tracks[i].get('uuid') is None for i in order)   # noqa: E731
    for P, N in _legs(b):
        if 'RF' in (b.netclass(P) or '').upper():
            continue
        both = lambda net, ly, pts, P=P, N=N: net in (P, N) and sel(net, ly, pts)        # noqa: E731
        for net, ly, w, order, pts, k, off in find(b, set(), both, every=True):
            if fresh(order):
                continue
            mate = N if net == P else P
            got = None
            for f in (1.0,) + STEPS:
                for tag in 'AB':
                    r = partial(pts, k, tag, f)
                    if not r or not keeps(b, Index(b), net, ly, w, pts, r[0]):
                        continue
                    old, nw = ((pts[k - 1], pts[k]), (r[1][1], r[1][2])) if tag == 'A' else \
                        ((pts[k + 1], pts[k + 2]), (r[1][1], r[1][2]))
                    d = (nw[1][0] - old[1][0], nw[1][1] - old[1][1]) if tag == 'A' else \
                        (nw[0][0] - old[0][0], nw[0][1] - old[0][1])
                    for mord, mpts in mate_runs(b, mate, ly, old, fresh):
                        s2 = slide(mpts, 1, d)
                        if not s2:
                            continue
                        mw = b.tracks[mord[1]]['w']
                        n0 = len(b.tracks)
                        lay(mord, mate, ly, mw, s2[0])                # the mate moved, then this leg
                        if Clear(b, net, ly, w, pts, order).poly(r[1]):
                            n1 = len(b.tracks)
                            lay(order, net, ly, w, r[0])
                            if Clear(b, mate, ly, mw, mpts, mord).poly(s2[1]):
                                got = (f, tag, r[0], mord, mw, mpts, s2[0])
                                break
                            unlay(order, n1)
                        unlay(mord, n0)
                    if got:
                        break
                if got:
                    break
            if got:
                f, tag, new, mord, mw, mpts, mnew = got
                taken.append((0.0, net, ly, w, order, pts, new))
                taken.append((0.0, mate, ly, mw, mord, mpts, mnew))
                rows.append((net, ly, pts[k], off, f"{tag}: collapsed {f:.0%} of the way, {mate.split('/')[-1]} slid "
                                                  f"alongside (pair gap held)"))


def _nudge(b, v, net, ly, w, pts, order, loc, touched=()):
    """((x, y), dragged [(track, a, b)], note) when dragging signal via v (its track ends with
    it) <= 0.3 mm straight away from the changed polyline is clean by movecheck's via drag and
    clears the collapse, else None. Never a via already nudged this pass, nor one a track laid
    this pass (no uuid yet: the writer cannot drag it) or an already-dragged track ends on"""
    from kpcb_move import via_drag
    c = (v['x'], v['y'])
    if id(v) in touched or any(t['net'] == v['net'] and (t.get('uuid') is None or id(t) in touched)
                               and min(math.dist(t['a'], c), math.dist(t['b'], c)) < v['size'] / 2 - 1e-3
                               for t in b.tracks):     # the tracks via_drag would drag
        return None
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
