"""Turn hand-rounded zone outlines (arcs approximated by short chords) into sharp-cornered
polygons + KiCad `(smoothing fillet) (radius R)`, so the outline is editable in KiCad.

  zfillet.py IN.kicad_pcb OUT.kicad_pcb [UUID-prefix ...]   (default: zones named route*/fatten*)

Each arc run (consecutive short chords on one circle) becomes 1..4 corners where the tangent
lines meet: the incoming/outgoing straight edge when it is (nearly) tangent, else the circle's
tangent at the run end. KiCad's fillet at radius R then redraws the arc. Concave runs (bites
round GND pads) become corners on the zone side, so a bite only grows. Every zone is checked
against a Python copy of KiCad's SHAPE_POLY_SET fillet: copper added / removed vs the original
outline, and copper added inside any concave bite (must be ~0)."""
import math, re, sys
from collections import Counter
import numpy as np
from matplotlib.path import Path

sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_edit import PcbText, num


def ang(a, b, c):
    """signed turn at b, degrees"""
    v1, v2 = (b[0] - a[0], b[1] - a[1]), (c[0] - b[0], c[1] - b[1])
    return math.degrees(math.atan2(v1[0] * v2[1] - v1[1] * v2[0], v1[0] * v2[0] + v1[1] * v2[1]))


def circum(a, b, c):
    ax, ay = a; bx, by = b; cx, cy = c
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
                acc, last = 0.0, None
                for i in grp:
                    acc += abs(ang(P[i - 1], P[i], P[(i + 1) % n]))
                    if acc >= max_turn * 0.9:
                        acc, last = 0.0, i
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
    Q = [tuple(p) for p in Q]
    changed = True
    while changed and len(Q) > 3:
        changed = False
        for i in range(len(Q)):
            a, b, c = Q[i - 1], Q[i], Q[(i + 1) % len(Q)]
            ab, ac = math.dist(a, b), math.dist(a, c)
            if ab < 1e-3:
                del Q[i]; changed = True; break
            if ac > 1e-9:
                dev = abs((c[0] - a[0]) * (a[1] - b[1]) - (a[0] - b[0]) * (c[1] - a[1])) / ac
                dot = (b[0] - a[0]) * (c[0] - a[0]) + (b[1] - a[1]) * (c[1] - a[1])
                if dev < tol and 0 < dot < ac * ac:
                    del Q[i]; changed = True; break
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
            out.append((x1, y1)); continue
        denom = math.sqrt(2.0 / (1 + cosine) - 1)
        radius = min(r, 0.5 * la * denom, 0.5 * lb * denom)
        k = radius / math.sqrt(.5 * (1 - cosine))
        lab = math.hypot(xa / la + xb / lb, ya / la + yb / lb)
        xc, yc = x1 + k * (xa / la + xb / lb) / lab, y1 + k * (ya / la + yb / lb) / lab
        k2 = radius / denom
        xs, ys = x1 + k2 * xa / la, y1 + k2 * ya / la
        xe, ye = x1 + k2 * xb / lb, y1 + k2 * yb / lb
        a0 = math.atan2(ys - yc, xs - xc); a1 = math.atan2(ye - yc, xe - xc)
        d = (a1 - a0 + math.pi) % (2 * math.pi) - math.pi
        seg = max(1, math.ceil(abs(d) / (2 * math.acos(max(-1, 1 - err / max(radius, 1e-6))))))
        out += [(xc + radius * math.cos(a0 + d * j / seg), yc + radius * math.sin(a0 + d * j / seg)) for j in range(seg + 1)]
    return out


def compare(P, F, bites, res=0.01):
    xs = [p[0] for p in P + F]; ys = [p[1] for p in P + F]
    gx = np.arange(min(xs) - .05, max(xs) + .05, res); gy = np.arange(min(ys) - .05, max(ys) + .05, res)
    X, Y = np.meshgrid(gx, gy); pts = np.c_[X.ravel(), Y.ravel()]
    a, b = Path(P).contains_points(pts), Path(F).contains_points(pts)
    added, removed = (b & ~a), (a & ~b)
    inb = np.zeros(len(pts), bool)
    for B in bites:
        inb |= Path(B).contains_points(pts)
    A = res * res
    return added.sum() * A, removed.sum() * A, (added & inb).sum() * A


FILLET = 0.2
BITE_R = 0.9          # concave arcs under this radius are the GND-pad halos: they only grow
ZRE = re.compile(r'(\n\t\t\(polygon\n\t\t\t\(pts\n)(.*?)(\n\t\t\t\)\n\t\t\))', re.S)


def main(src, dst, want):
    t = PcbText(src)
    tot = Counter()
    for uid, (s, e) in t.span.items():
        blk = t.txt[s:e]
        if not blk.lstrip().startswith('(zone') or '(teardrop' in blk:
            continue
        name = re.search(r'\n\t\t\(name "([^"]*)"\)', blk)
        name = name[1] if name else ''
        if want and not any(uid.startswith(w) for w in want):
            continue
        if not want and not name.startswith(('route', 'fatten')):
            continue
        pols = list(ZRE.finditer(blk))
        assert len(pols) == 1, (uid, len(pols))
        m = pols[0]
        P = [(float(x), float(y)) for x, y in re.findall(r'\(xy (\S+) (\S+)\)', m[2])]
        assert '(arc' not in m[2] and len(P) >= 3
        Q, bites = unround(P)
        R = FILLET
        if not simple(Q):
            print(f"!! {uid[:8]} {name}: self-crossing result, left as is"); continue
        F = kicad_fillet(Q, R)
        add, rem, inbite = compare(P, F, bites)
        flag = ' !!' if inbite > 0.002 or add > 0.25 or rem > 0.25 else ''
        print(f"{uid[:8]} {name[:28]:28s} {len(P):4d} -> {len(Q):3d} pts  R={R:<5g} +{add:.3f} -{rem:.3f} mm2  bite+{inbite:.4f}{flag}")
        tot['before'] += len(P); tot['after'] += len(Q); tot['add'] += add; tot['rem'] += rem; tot['n'] += 1
        xy = ''.join(f"\t\t\t\t(xy {num(x)} {num(y)})\n" for x, y in Q).rstrip('\n')
        nb = blk[:m.start(2)] + xy + blk[m.end(2):]
        fm = re.search(r'\n\t\t\(fill yes\n(.*?)\n\t\t\)', nb, re.S)
        assert fm, uid
        body = re.sub(r'\n\t\t\t\((smoothing|radius) [^)]*\)', '', '\n' + fm[1])
        body = body.replace('\n\t\t\t(island_removal_mode', f"\n\t\t\t(smoothing fillet)\n\t\t\t(radius {num(R)})\n\t\t\t(island_removal_mode", 1)
        assert '(smoothing fillet)' in body, uid
        nb = nb[:fm.start(1)] + body[1:] + nb[fm.end(1):]
        nb, nf = re.subn(r'\n\t\t\(filled_polygon\n.*?\n\t\t\)', '', nb, flags=re.S)
        t.sub(uid, nb)
    print(f"{tot['n']} zones, {tot['before']} -> {tot['after']} outline points, outline area +{tot['add']:.2f} / -{tot['rem']:.2f} mm2")
    t.save(dst)


if __name__ == '__main__':
    main(sys.argv[1], sys.argv[2], sys.argv[3:])
