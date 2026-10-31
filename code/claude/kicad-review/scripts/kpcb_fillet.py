"""Zone outlines KiCad users can edit: a hand-rounded outline (arcs as short chord runs or
0.1 mm raster staircases fitted by `simplify`) becomes sharp corners where the tangent lines
meet, plus `(smoothing fillet) (radius 0.2)` so KiCad redraws the arcs. A convex run's lines
circumscribe it (the cap only gains a sliver); a concave run under BITE_R (a GND-pad halo
bite) gets corners on the zone side, so the bite only grows; a wider concave run (a rounded
inner corner) keeps a vertex every <= 45 deg, since corners there would thin the neck it was
rounding. `fillet_outline` checks each result against a copy of KiCad's own fillet and keeps
the faceted outline when it would cross itself, lose copper or grow into a bite."""
import math, re

FILLET = 0.2          # the radius KiCad redraws every corner with
BITE_R = 0.9          # concave arcs under this radius are the GND-pad halos: they only grow


def ang(a, b, c):
    """signed turn at b, degrees"""
    v1, v2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    return math.degrees(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))


def circum(a, b, c):
    (ax, ay), (bx, by), (cx, cy) = a, b, c
    d = 2 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = ((ax * ax + ay * ay) * (by - cy) + (bx * bx + by * by) * (cy - ay) + (cx * cx + cy * cy) * (ay - by)) / d
    uy = ((ax * ax + ay * ay) * (cx - bx) + (bx * bx + by * by) * (ax - cx) + (cx * cx + cy * cy) * (bx - ax)) / d
    return ux, uy, math.dist((ux, uy), a)


def area(P):
    return sum(P[i - 1][0] * P[i][1] - P[i][0] * P[i - 1][1] for i in range(len(P))) / 2


def isect(p, d, q, e):
    """intersection of lines p + t d and q + s e (None when parallel)"""
    den = d[0] * e[1] - d[1] * e[0]
    if abs(den) < 1e-9:
        return None
    t = ((q[0] - p[0]) * e[1] - (q[1] - p[1]) * e[0]) / den
    return p[0] + t * d[0], p[1] + t * d[1]


def chains(P, max_edge=0.6, max_turn=25.0):
    """[(s, e, grp, sign)]: runs of curve vertices (|turn| 1..max_turn deg, both edges < max_edge,
    one turn direction); s/e are the vertices either side of the run"""
    n = len(P)
    sg = [0] * n
    for i in range(n):
        a, b, c = P[i - 1], P[i], P[(i + 1) % n]
        t = ang(a, b, c)
        if 1.0 < abs(t) <= max_turn and max(math.dist(a, b), math.dist(b, c)) < max_edge:
            sg[i] = 1 if t > 0 else -1
    start = next((i for i in range(n) if sg[i] == 0), None)
    if start is None:
        return []
    out, grp = [], []
    for k in range(n + 1):
        i = (start + k) % n
        if grp and (sg[i] != sg[grp[-1]] or k == n):
            out.append(((grp[0] - 1) % n, (grp[-1] + 1) % n, grp, sg[grp[-1]]))
            grp = []
        if k < n and sg[i]:
            grp.append(i)
    return out


def unround(P, max_turn=45.0, long_edge=0.6, sharp=25.0):
    """(Q, bite polygons): each curve chain -> corners where support lines meet, one line per
    <= max_turn deg of turn. The lines circumscribe the chain on its outer side, so a convex
    cap only gains a sliver and a concave bite only grows."""
    n = len(P)
    drop, at, bites = set(), {}, []
    ccw = area(P) > 0
    for s, e, grp, sign in chains(P):
        T = sum(ang(P[i - 1], P[i], P[(i + 1) % n]) for i in grp)
        ts, te = ang(P[s - 1], P[s], P[grp[0]]), ang(P[grp[-1]], P[e], P[(e + 1) % n])
        # a run end is a tangent point (dropped, its straight edge extended) when the edge
        # beyond it is long and the turn there is small and the same way
        tan_s = math.dist(P[s - 1], P[s]) >= long_edge and abs(ts) <= sharp and ts * sign >= 0
        tan_e = math.dist(P[e], P[(e + 1) % n]) >= long_edge and abs(te) <= sharp and te * sign >= 0
        if tan_s:
            T += ts
        if tan_e:
            T += te
        if abs(T) < 4:
            drop.update(grp)
            continue
        if (sign > 0) != ccw:
            cc = circum(P[s], P[grp[len(grp) // 2]], P[e])
            if cc and cc[2] >= BITE_R:
                # a rounded inner corner, not a GND-pad bite: keep a vertex on the arc every
                # <= max_turn deg. Chords of a concave arc only add copper; corners would thin
                # the neck it was rounding (SOLAR B.Cu, PPHV In2 split on 2026-10-26)
                acc = 0.0
                for i in grp:
                    acc += abs(ang(P[i - 1], P[i], P[(i + 1) % n]))
                    if acc >= max_turn * 0.9:
                        acc = 0.0
                    else:
                        drop.add(i)
                continue
            bites.append([P[i] for i in [s] + grp + [e]])
        d0 = (P[s][0] - P[s - 1][0], P[s][1] - P[s - 1][1]) if tan_s else (P[grp[0]][0] - P[s][0], P[grp[0]][1] - P[s][1])
        d1 = (P[(e + 1) % n][0] - P[e][0], P[(e + 1) % n][1] - P[e][1]) if tan_e else (P[e][0] - P[grp[-1]][0], P[e][1] - P[grp[-1]][1])
        a0 = math.atan2(d0[1], d0[0])
        k = max(1, math.ceil(abs(T) / max_turn))
        pts = [P[i] for i in [s] + grp + [e]]
        lines = [(P[s], d0)]
        for j in range(1, k):
            th = a0 + math.radians(T) * j / k
            d = (math.cos(th), math.sin(th))
            nrm = (d[1], -d[0]) if sign > 0 else (-d[1], d[0])     # outer side of the turn
            sp = max(pts, key=lambda q: q[0] * nrm[0] + q[1] * nrm[1])
            lines.append((sp, d))
        lines.append((P[e], d1))
        corners = [isect(p, d, q, f) for (p, d), (q, f) in zip(lines, lines[1:])]
        if any(c is None for c in corners):
            continue
        drop.update(grp)
        if tan_s:
            drop.add(s)
        if tan_e:
            drop.add(e)
        at[s] = (not tan_s, corners)
    out = []
    for i in range(n):
        if i in at:
            ks, cs = at[i]
            if ks:
                out.append(P[i])
            out += cs
        elif i not in drop:
            out.append(P[i])
    return clean(out), bites


def clean(Q, tol=0.002):
    """drop repeated points and points within tol of the straight line through their neighbours"""
    Q = [tuple(p) for p in Q]
    changed = True
    while changed and len(Q) > 3:
        changed = False
        for i in range(len(Q)):
            a, b, c = Q[i - 1], Q[i], Q[(i + 1) % len(Q)]
            ab, ac = math.dist(a, b), math.dist(a, c)
            if ab < 1e-3:
                del Q[i]
                changed = True
                break
            if ac > 1e-9:
                dev = abs((c[0] - a[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (c[1] - a[1])) / ac
                dot = (b[0] - a[0]) * (c[0] - a[0]) + (b[1] - a[1]) * (c[1] - a[1])
                if dev < tol and 0 < dot < ac * ac:
                    del Q[i]
                    changed = True
                    break
    return Q


def _pseg(p, a, b):
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    s = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2)) if L2 else 0.0
    return math.dist(p, (a[0] + s * dx, a[1] + s * dy))


def _ring_dist(p, Q):
    return min(_pseg(p, Q[i - 1], Q[i]) for i in range(len(Q)))


def octilinear(P, tol=0.05, grid=0.05):
    """P with every edge that lies within tol of a 0/45/90 line put on that line, the line on
    the grid (a row or column of it, a diagonal through its points), runs on one line merged
    and each corner re-cut where its two lines meet: a raster trace's 43.6-deg staircase fit
    becomes the 45 it stands for and its corners sit on the grid, as a hand-drawn zone's do.
    An edge further off keeps its own line; two parallel neighbours meet in a square step at
    their old corner. P itself when the result would cross itself or stray > 2 tol from P"""
    P = clean(P)
    n = len(P)
    if n < 4:
        return P
    lines = []                                   # (key, point, unit dir); key (k, c) for a snapped line
    for i in range(n):
        a, b = P[i], P[(i + 1) % n]
        dx, dy = b[0] - a[0], b[1] - a[1]
        ln = math.hypot(dx, dy)
        th = math.degrees(math.atan2(dy, dx))
        k = round(th / 45)
        mx, my = (a[0] + b[0]) / 2, (a[1] + b[1]) / 2
        if ln / 2 * abs(math.sin(math.radians(th - 45 * k))) > tol:
            lines.append((None, (mx, my), (dx / ln, dy / ln)))
            continue
        u = (round(math.cos(math.radians(45 * k)), 12), round(math.sin(math.radians(45 * k)), 12))
        k %= 4
        if k == 0:
            c = round(my / grid) * grid; pt = (mx, c)
        elif k == 2:
            c = round(mx / grid) * grid; pt = (c, my)
        elif k == 1:                             # x - y = c
            c = round((mx - my) / grid) * grid; t = (mx - my - c) / 2; pt = (mx - t, my + t)
        else:                                    # x + y = c
            c = round((mx + my) / grid) * grid; t = (mx + my - c) / 2; pt = (mx - t, my - t)
        lines.append(((k, round(c / grid)), pt, u))
    s = next((i for i in range(n) if lines[i][0] is None or lines[i][0] != lines[i - 1][0]), None)
    if s is None:
        return P
    merged, olds = [], []                        # one line per run, and the old corner ending the run
    for j in range(n):
        i = (s + j) % n
        if merged and lines[i][0] is not None and lines[i][0] == merged[-1][0]:
            olds[-1] = P[(i + 1) % n]
            continue
        merged.append(lines[i])
        olds.append(P[(i + 1) % n])
    m = len(merged)
    if m < 3:
        return P
    Q = []
    for j in range(m):
        p1, u1 = merged[j][1:]
        p2, u2 = merged[(j + 1) % m][1:]
        cr = u1[0] * u2[1] - u1[1] * u2[0]
        if abs(cr) < 1e-9:                       # parallel: a square step through the old corner
            o = olds[j]
            for p, u in ((p1, u1), (p2, u2)):
                t = (o[0] - p[0]) * u[0] + (o[1] - p[1]) * u[1]
                Q.append((p[0] + t * u[0], p[1] + t * u[1]))
            continue
        t = ((p2[0] - p1[0]) * u2[1] - (p2[1] - p1[1]) * u2[0]) / cr
        Q.append((p1[0] + t * u1[0], p1[1] + t * u1[1]))
    Q = clean([(round(x, 4), round(y, 4)) for x, y in Q], 1e-4)
    if len(Q) < 3 or not simple(Q) or any(_ring_dist(q, P) > 2 * tol for q in Q) \
            or any(_ring_dist(p, Q) > 2 * tol for p in P):
        return P
    return Q


def simple(Q):
    """no two non-adjacent edges cross"""
    n = len(Q)
    def cross(p, q, r):
        return (q[0] - p[0]) * (r[1] - p[1]) - (q[1] - p[1]) * (r[0] - p[0])
    for i in range(n):
        a, b = Q[i], Q[(i + 1) % n]
        for j in range(i + 2, n):
            if i == 0 and j == n - 1:
                continue
            c, d = Q[j], Q[(j + 1) % n]
            if (cross(a, b, c) > 0) != (cross(a, b, d) > 0) and (cross(c, d, a) > 0) != (cross(c, d, b) > 0):
                return False
    return True


def kicad_fillet(Q, r, err=0.005):
    """SHAPE_POLY_SET::chamferFilletPolygon(FILLETED): every corner, radius limited so the
    arc uses at most half of each adjacent edge"""
    out, n = [], len(Q)
    for i in range(n):
        x1, y1 = Q[i]
        xa, ya = Q[i - 1][0] - x1, Q[i - 1][1] - y1
        xb, yb = Q[(i + 1) % n][0] - x1, Q[(i + 1) % n][1] - y1
        la, lb = math.hypot(xa, ya), math.hypot(xb, yb)
        cosine = max(-1.0, min(1.0, (xa * xb + ya * yb) / (la * lb)))
        if cosine <= -1 + 1e-12 or cosine >= 1 - 1e-12:
            out.append((x1, y1))
            continue
        denom = math.sqrt(2.0 / (1 + cosine) - 1)
        radius = min(r, 0.5 * la * denom, 0.5 * lb * denom)
        k = radius / math.sqrt(.5 * (1 - cosine))
        lab = math.hypot(xa / la + xb / lb, ya / la + yb / lb)
        xc, yc = x1 + k * (xa / la + xb / lb) / lab, y1 + k * (ya / la + yb / lb) / lab
        k2 = radius / denom
        xs, ys = x1 + k2 * xa / la, y1 + k2 * ya / la
        xe, ye = x1 + k2 * xb / lb, y1 + k2 * yb / lb
        a0, a1 = math.atan2(ys - yc, xs - xc), math.atan2(ye - yc, xe - xc)
        d = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        seg = max(1, math.ceil(abs(d) / (2 * math.acos(max(-1, 1 - err / max(radius, 1e-6))))))
        out += [(xc + radius * math.cos(a0 + d * j / seg), yc + radius * math.sin(a0 + d * j / seg)) for j in range(seg + 1)]
    return out


def _inside(P, gx, gy):
    """even-odd point-in-polygon on the grid gx x gy (numpy rows = y): each edge flips the
    cells left of it in its own row band only"""
    import numpy as np
    c = np.zeros((len(gy), len(gx)), bool)
    for (x1, y1), (x2, y2) in zip(P, P[1:] + P[:1]):
        if y1 == y2:
            continue
        j0, j1 = np.searchsorted(gy, min(y1, y2)), np.searchsorted(gy, max(y1, y2))
        if j0 < j1:
            xi = x1 + (gy[j0:j1] - y1) * (x2 - x1) / (y2 - y1)
            c[j0:j1] ^= gx[None, :] < xi[:, None]
    return c


def compare(P, F, bites, res=0.01):
    """(mm2 added, mm2 removed, mm2 added inside a concave bite) going from outline P to F"""
    import numpy as np
    xs, ys = [p[0] for p in P + F], [p[1] for p in P + F]
    gx, gy = np.arange(min(xs) - .05, max(xs) + .05, res), np.arange(min(ys) - .05, max(ys) + .05, res)
    a, b = _inside(P, gx, gy), _inside(F, gx, gy)
    added, inb = b & ~a, np.zeros(a.shape, bool)
    for B in bites:
        inb |= _inside(B, gx, gy)
    A = res * res
    return added.sum() * A, (a & ~b).sum() * A, (added & inb).sum() * A


def fillet_outline(P, r=FILLET):
    """(outline, radius or None, note): the sharp outline KiCad's fillet at r redraws as P
    (+ a sliver), or P itself and None when there is nothing to unround or the result would
    cross itself, lose > 0.25 mm2 or grow into a GND-pad bite"""
    P = [tuple(p) for p in P]
    if not chains(P):                            # a hand-drawn polygon: its corners stay sharp
        return P, None, 'no arc runs'
    Q, bites = unround(P)
    if len(Q) < 3 or not simple(Q):
        return P, None, 'the sharp outline would cross itself: left faceted'
    add, rem, inbite = compare(P, kicad_fillet(Q, r), bites)
    if rem > 0.25 or inbite > 0.002:
        return P, None, f"fillet would lose {rem:.3f} mm2 / grow {inbite:.4f} mm2 into a bite: left faceted"
    return Q, r, f"{len(P)} -> {len(Q)} pts, outline +{add:.3f} -{rem:.3f} mm2"


def smooth_fill(blk, r):
    """a zone block with `(smoothing fillet) (radius r)` in its fill block (any old smoothing
    replaced), or None when it has no `(fill yes` block"""
    fm = re.search(r'\n\t\t\(fill yes\n(.*?)\n\t\t\)', blk, re.S)
    if not fm:
        return None
    body = re.sub(r'\n\t\t\t\((smoothing|radius) [^)]*\)', '', '\n' + fm[1])
    sm = f"\n\t\t\t(smoothing fillet)\n\t\t\t(radius {r:g})"
    i = body.find('\n\t\t\t(island_removal_mode')          # where KiCad's own save puts it
    body = body[:i] + sm + body[i:] if i >= 0 else body + sm
    return blk[:fm.start(1)] + body[1:] + blk[fm.end(1):]
