"""PROTOTYPE (2026-10-18, needs shapely+numpy, see SKILL-BACKLOG "pour-aware R"). Pad-to-pad DC resistance of one net through its REAL copper (refilled pours, tracks,
pads, via barrels), by a finite-difference grid solve. Scratch tool, not the skill.
  uv run --no-project --with shapely --with numpy python copper_resistance.py BOARD NET PAD_A PAD_B [AMPS] [--h 0.1] [--inner-um 15.2]
Prints R, Vdrop and I^2R. Pads A (V=1) and B (V=0) are Dirichlet regions."""
import sys, math
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
import numpy as np
import shapely
from shapely.geometry import LineString, Polygon, box as sbox
from kpcb_board import Board, pad_box
from kcommon import load_sexp, kids, kid, val

RHO = 1.72e-8          # ohm*m copper
PLATE = 25e-6          # via barrel plating


def args():
    a = sys.argv[1:]
    h = float(a[a.index('--h') + 1]) if '--h' in a else 0.1
    inner = float(a[a.index('--inner-um') + 1]) * 1e-3 if '--inner-um' in a else None
    pos = [x for i, x in enumerate(a) if not x.startswith('--') and (i == 0 or not a[i - 1].startswith('--'))]
    return pos[0], pos[1], pos[2], pos[3], float(pos[4]) if len(pos) > 4 else 1.0, h, inner


def main():
    src, net, pa, pb, amps, h, inner = args()
    b = Board(src)
    lays = b.copper
    th = {l: (b.thick.get(l) or (0.035 if l in b.outer else 0.0152)) for l in lays}
    if inner:
        th.update({l: inner for l in lays if l not in b.outer})
    # dielectric gaps between consecutive copper layers (mm) from the stackup
    z, gaps, last = 0.0, [], None
    for name, typ, t, er in b.stack:
        if typ == 'copper':
            if last is not None:
                gaps.append(z)
            z, last = 0.0, name
        elif last is not None and t:
            z += t
    geoms = {l: [] for l in lays}
    for zn in kids(load_sexp(src), 'zone'):
        if val(zn, 'net') != net or kid(kid(zn, 'attr') or [], 'teardrop'):
            continue
        for fp in kids(zn, 'filled_polygon'):
            q = [(float(p[1]), float(p[2])) for p in kids(kid(fp, 'pts'), 'xy')]
            if len(q) >= 3 and val(fp, 'layer') in geoms:
                geoms[val(fp, 'layer')].append(Polygon(q).buffer(0))
    for t in b.tracks:
        if t['net'] == net and t['layer'] in geoms:
            geoms[t['layer']].append(LineString([t['a'], t['b']]).buffer(t['w'] / 2, quad_segs=4))
    pads = {}
    for f in b.fps.values():
        for p in f.pads:
            if p.get('net') != net:
                continue
            g = sbox(*pad_box(p))
            pl = [l for l in lays if l in (p.get('layers') or []) or '*.Cu' in (p.get('layers') or [])]
            for l in pl:
                geoms[l].append(g)
            pads[f'{f.ref}.{p["num"]}'] = (g, pl)
    vias = [v for v in b.vias if v['net'] == net]
    from shapely.geometry import Point
    for v in vias:                       # a through via is an annular pad on every layer
        for l in lays:
            geoms[l].append(Point(v['x'], v['y']).buffer(max(v['size'], 2 * 0.1) / 2))
    allg = shapely.union_all([g for l in lays for g in geoms[l]])
    x0, y0, x1, y1 = allg.bounds
    x0, y0 = x0 - h, y0 - h
    nx, ny = int((x1 - x0) / h) + 2, int((y1 - y0) / h) + 2
    xs = x0 + (np.arange(nx) + 0.5) * h
    ys = y0 + (np.arange(ny) + 0.5) * h
    X, Y = np.meshgrid(xs, ys)
    L = len(lays)
    cu = np.zeros((L, ny, nx), bool)
    for k, l in enumerate(lays):
        if geoms[l]:
            cu[k] = shapely.contains_xy(shapely.union_all(geoms[l]), X, Y)
    def region(name):
        g, pl = pads[name]
        m = np.zeros_like(cu)
        for l in pl:
            k = lays.index(l)
            m[k] = shapely.contains_xy(g, X, Y) & cu[k]
        return m
    A, B = region(pa), region(pb)
    assert A.any() and B.any(), 'pad not on the grid'
    gl = np.array([th[l] * 1e-3 / RHO for l in lays])          # sheet conductance S per square
    # edge conductances (between neighbours, both copper)
    gx = (cu[:, :, 1:] & cu[:, :, :-1]) * gl[:, None, None]
    gy = (cu[:, 1:, :] & cu[:, :-1, :]) * gl[:, None, None]
    # via couplings between consecutive layers
    vi, vk, vg = [], [], []
    for v in vias:
        c, r = int((v['x'] - x0) / h), int((v['y'] - y0) / h)
        if not (0 <= c < nx and 0 <= r < ny):
            continue
        gb = PLATE * math.pi * v['drill'] * 1e-3 / RHO       # S*m (barrel cross-section/rho)
        for k in range(L - 1):
            if cu[k, r, c] and cu[k + 1, r, c]:
                vi.append((k, r, c)); vk.append((k + 1, r, c)); vg.append(gb / (max(gaps[k] if k < len(gaps) else 0.2, 0.05) * 1e-3))
    vi, vk, vg = np.array(vi, int).reshape(-1, 3), np.array(vk, int).reshape(-1, 3), np.array(vg)
    fixed = A | B
    free = cu & ~fixed
    V0 = A.astype(float)

    def lap(V):            # -div(g grad V) applied to V
        out = np.zeros_like(V)
        d = gx * (V[:, :, 1:] - V[:, :, :-1]); out[:, :, 1:] += d; out[:, :, :-1] -= d
        d = gy * (V[:, 1:, :] - V[:, :-1, :]); out[:, 1:, :] += d; out[:, :-1, :] -= d
        if len(vg):
            d = vg * (V[tuple(vk.T)] - V[tuple(vi.T)])
            np.add.at(out, tuple(vk.T), d); np.add.at(out, tuple(vi.T), -d)
        return out
    # diagonal for a Jacobi preconditioner
    D = np.zeros_like(V0)
    D[:, :, 1:] += gx; D[:, :, :-1] += gx; D[:, 1:, :] += gy; D[:, :-1, :] += gy
    if len(vg):
        np.add.at(D, tuple(vk.T), vg); np.add.at(D, tuple(vi.T), vg)
    D[D == 0] = 1
    # solve lap(V0 + U) = 0 on free cells
    rhs = -lap(V0) * free
    U = np.zeros_like(V0)
    r = rhs - lap(U) * free
    z = r / D; p = z.copy(); rz = (r * z).sum()
    nb = math.sqrt((rhs * rhs).sum()) or 1
    for it in range(20000):
        Ap = lap(p) * free
        a = rz / ((p * Ap).sum() or 1e-300)
        U += a * p; r -= a * Ap
        if math.sqrt((r * r).sum()) < 1e-9 * nb:
            break
        z = r / D; rz2 = (r * z).sum(); p = z + (rz2 / rz) * p; rz = rz2
    V = V0 + U * free
    I = (lap(V) * A).sum()                                    # current leaving the source region
    R = 1 / I if I > 0 else float('inf')
    print(f'{net}: {pa} -> {pb}  R = {R*1e3:.2f} mohm   at {amps:g} A: Vdrop {R*amps*1e3:.0f} mV, P {R*amps*amps*1e3:.0f} mW'
          f'   ({nx}x{ny}x{L} grid h={h}, {len(vias)} vias, {it} it)')


main()
