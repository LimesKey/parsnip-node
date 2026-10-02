"""kpcb.py `freebox`: where a W x H silk box fits on one side - clear of courtyards,
through-hole pads and existing silk (logos, board art, refdes and other footprint text),
>= 1 mm inside the edge - best spot per
free region."""
import math
from kpcb_board import pad_box, pt_seg_dist

RES = 0.25                                       # raster cell, mm


def occupancy(b, back, edge=1.0):
    """(grid of 0/1 rows, x0, y0): 1 = outside the board, within `edge` of it, or
    under an obstacle on that side"""
    o = b.outline
    x0, y0 = o[0], o[1]
    nx, ny = math.ceil((o[2] - x0) / RES), math.ceil((o[3] - y0) / RES)
    g = [bytearray(b'\x01') * nx for _ in range(ny)]
    for j in range(ny):                          # inside the outline: even-odd scanline
        y = y0 + (j + .5) * RES
        xs = sorted(a[0] + (y - a[1]) * (c[0] - a[0]) / (c[1] - a[1])
                    for a, c in b.edge_segs if (a[1] <= y) != (c[1] <= y))
        for s, e in zip(xs[0::2], xs[1::2]):
            i0, i1 = max(0, math.ceil((s - x0) / RES - .5)), min(nx, math.floor((e - x0) / RES - .5) + 1)
            g[j][i0:i1] = bytes(max(0, i1 - i0))
    def mark(bx):
        i0, i1 = max(0, math.floor((bx[0] - x0) / RES)), min(nx, math.ceil((bx[2] - x0) / RES))
        for j in range(max(0, math.floor((bx[1] - y0) / RES)), min(ny, math.ceil((bx[3] - y0) / RES))):
            g[j][i0:i1] = b'\x01' * max(0, i1 - i0)
    for a, c in b.edge_segs:                     # the edge band
        for j in range(max(0, math.floor((min(a[1], c[1]) - edge - y0) / RES)),
                       min(ny, math.ceil((max(a[1], c[1]) + edge - y0) / RES))):
            for i in range(max(0, math.floor((min(a[0], c[0]) - edge - x0) / RES)),
                           min(nx, math.ceil((max(a[0], c[0]) + edge - x0) / RES))):
                if pt_seg_dist((x0 + (i + .5) * RES, y0 + (j + .5) * RES), a, c) < edge:
                    g[j][i] = 1
    side = 'B.' if back else 'F.'
    for f in b.placed():
        if f.back == back and f.pads:
            mark(f.crtyd)
        for p in f.pads:
            if p['drill'] or any(l.startswith('*') for l in p['layers']):
                mark(pad_box(p))
        for lay, bx in f.arts + f.texts:
            if lay.startswith(side):
                mark(bx)
    for lay, bx in b.silk:
        if lay.startswith(side):
            mark(bx)
    return g, x0, y0


def c_freebox(b, a):
    try:
        side, w, h = a.args[0].lower()[:1], float(a.args[1]), float(a.args[2])
        assert side in 'fb'
    except (IndexError, ValueError, AssertionError):
        print("freebox SIDE W H   (SIDE f|b, box mm)"); return 1
    if not b.edge_segs:
        print("no board outline"); return 1
    g, x0, y0 = occupancy(b, side == 'b')
    ny, nx = len(g), len(g[0])
    S = [[0] * (nx + 1) for _ in range(ny + 1)]  # summed-area table of the occupancy
    for j in range(ny):
        run, row, up = 0, S[j + 1], S[j]
        for i in range(nx):
            run += g[j][i]
            row[i + 1] = up[i + 1] + run
    def empty(i0, j0, i1, j1):                   # inclusive cells; off-grid is occupied
        if i0 < 0 or j0 < 0 or i1 >= nx or j1 >= ny:
            return False
        return S[j1 + 1][i1 + 1] - S[j0][i1 + 1] - S[j1 + 1][i0] + S[j0][i0] == 0
    lo_x, hi_x = math.floor(.5 - w / 2 / RES), math.ceil(.5 + w / 2 / RES) - 1
    lo_y, hi_y = math.floor(.5 - h / 2 / RES), math.ceil(.5 + h / 2 / RES) - 1
    fit = {(i, j) for j in range(ny) for i in range(nx)
           if empty(i + lo_x, j + lo_y, i + hi_x, j + hi_y)}
    def margin(i, j):                            # extra cells the box can grow, clear
        k, top = 0, 40
        while k < top:
            m = (k + top + 1) // 2
            if empty(i + lo_x - m, j + lo_y - m, i + hi_x + m, j + hi_y + m):
                k = m
            else:
                top = m - 1
        return k * RES
    regions, seen = [], set()
    for c in sorted(fit):
        if c in seen:
            continue
        comp, todo = [], [c]
        seen.add(c)
        while todo:
            i, j = todo.pop()
            comp.append((i, j))
            for q in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
                if q in fit and q not in seen:
                    seen.add(q); todo.append(q)
        mx, my = sum(p[0] for p in comp) / len(comp), sum(p[1] for p in comp) / len(comp)
        best = max(comp, key=lambda q: (margin(*q), -abs(q[0] - mx) - abs(q[1] - my)))   # ties: middle
        regions.append((margin(*best), best, len(comp)))
    regions.sort(key=lambda r: (-r[0], -r[2]))
    print(f"freebox {side.upper()} {w:g} x {h:g} mm: clear of {side.upper()} courtyards, through-hole pads and "
          f"{side.upper()} silk (logos, board art, footprint text), >= 1 mm inside the edge; {RES:g} mm raster")
    if not regions:
        print("  nowhere: no centre clears every obstacle. Try a smaller box or the other side.")
        return 1
    print(f"  {len(regions)} region(s), best centre per region by margin (how far the box could grow and stay clear):")
    for n, (m, (i, j), sz) in enumerate(regions[:a.max], 1):
        print(f"  {n:>3}  centre {x0 + (i + .5) * RES:7.2f},{y0 + (j + .5) * RES:<7.2f}  margin {m:4.2f} mm  "
              f"(region {sz * RES * RES:6.1f} mm2 of centres)")
    if len(regions) > a.max:
        print(f"  ... +{len(regions) - a.max} smaller region(s) - raise --max")
    return 0
