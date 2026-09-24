#!/usr/bin/env python3
"""kzo.py - characteristic impedance of a PCB trace, for kpcb.py `rf`.

  microstrip(w, h, er, t)   Hammerstad closed form, uncoated, no side ground
  field_zo(w, h, er, ...)   2D finite-difference field solver on the cross-
                            section: grounded coplanar (CPWG), microstrip,
                            stripline, with or without solder mask

The closed forms for CPWG (Wadell/Ghione) assume the backing plane is far vs
the slot (h >> w + 2s). On a 4-layer board the plane is 0.2 mm down and the slot
~1 mm wide, where they read ABOVE microstrip - physically backwards, since side
grounds can only add capacitance. So CPWG gets solved, not looked up.

Method (quasi-static, TEM): Laplace div(er grad V) = 0 on a graded rectilinear
grid over half the cross-section (x = 0 is a symmetry plane), strip at 1 V,
grounds at 0, open sides as Neumann walls far away. Per-length capacitance from
the field energy, C = sum over grid edges of er * (dV)^2 * (face / length) in
units of eps0; done once with the real dielectrics (C) and once in air (C0):
Zo = eta0 / sqrt(C * C0), eeff = C / C0. Checked in `python3 kzo.py --selftest`
against three references: stripline (exact, Cohn), CPW on a thick substrate
(exact conformal map) and microstrip (Hammerstad, ~1%).

  python3 kzo.py W S H ER [T] [--mask TM ERM]     one CPWG number, mm
"""
import math, sys

ETA0 = 376.730313668            # free-space impedance, ohm (= 1 / (c eps0))

def microstrip(w, h, er, t=0.0):
    """(Zo ohm, eeff) of an uncoated microstrip: Hammerstad's closed form with
    the Wheeler strip-thickness width correction. ~1-2% against a field
    solver; solder mask on top typically pulls Zo down another 1-2 ohm."""
    u = w / h
    eeff = (er + 1) / 2 + (er - 1) / 2 * ((1 + 12 / u) ** -0.5 + (0.04 * (1 - u) ** 2 if u < 1 else 0))
    if t > 0:
        u += t / (math.pi * h) * (1 + math.log(2 * h / t))
    if u <= 1:
        z = 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    else:
        z = 120 * math.pi / (math.sqrt(eeff) * (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return z, eeff

def _axis(keys, dmin, dmax, g=1.5):
    """Grid lines through every key coordinate, dmin apart at each key and
    growing by g per step up to dmax in between (fine at conductor edges, where
    the field is singular; coarse far away)."""
    keys = sorted({round(k, 9) for k in keys})
    out = [keys[0]]
    for a, b in zip(keys, keys[1:]):
        mid, lo, hi = (a + b) / 2, [], []
        for x0, sgn, acc in ((a, 1, lo), (b, -1, hi)):
            x, d = x0, dmin
            while sgn * (mid - (x + sgn * d)) > 0:
                x += sgn * d
                acc.append(x)
                d = min(d * g, dmax)
        out += lo + hi[::-1] + [b]
    return out

try:
    _dot = math.sumprod                         # 3.12+: C-speed dot product
except AttributeError:                          # pragma: no cover
    import operator
    _dot = lambda a, b: sum(map(operator.mul, a, b))

def _capacitance(xs, ys, eps, fixed):
    """Energy capacitance (units of eps0, half domain) with the V=1 conductor at
    1 V, from the 5-point finite-volume stencil solved directly: the matrix is
    symmetric positive-definite with a bandwidth of one grid row, so a skyline
    Cholesky costs rows x width^2 and does not care that the graded grid's cells
    are 300:1 (which stalls SOR). eps(i, j) is the relative permittivity of cell
    [xs[i], xs[i+1]] x [ys[j], ys[j+1]]; fixed(i, j) is the node's potential if it
    lies on a conductor, else None. Missing neighbours are Neumann (the x = 0
    symmetry plane, the far walls)."""
    nx, ny = len(xs), len(ys)
    dx = [xs[i + 1] - xs[i] for i in range(nx - 1)]
    dy = [ys[j + 1] - ys[j] for j in range(ny - 1)]
    E = [[eps(i, j) for j in range(ny - 1)] for i in range(nx - 1)]
    def e(i, j):
        return E[i][j] if 0 <= i < nx - 1 and 0 <= j < ny - 1 else 0.0
    def wx(i):
        return dx[i] if 0 <= i < nx - 1 else 0.0
    def wy(j):
        return dy[j] if 0 <= j < ny - 1 else 0.0
    # coupling of node (i,j) to its east and north neighbour: face / length * er
    aE = [[(e(i, j - 1) * wy(j - 1) + e(i, j) * wy(j)) / 2 / dx[i] if i < nx - 1 else 0.0
           for j in range(ny)] for i in range(nx)]
    aN = [[(e(i - 1, j) * wx(i - 1) + e(i, j) * wx(i)) / 2 / dy[j] if j < ny - 1 else 0.0
           for j in range(ny)] for i in range(nx)]
    V = [0.0] * (nx * ny)
    idx = {}                                   # grid node -> unknown number, row-major
    for j in range(ny):
        for i in range(nx):
            f = fixed(i, j)
            if f is None:
                idx[j * nx + i] = len(idx)
            else:
                V[j * nx + i] = f
    n = len(idx)
    first, rows, diag, rhs = [0] * n, [None] * n, [0.0] * n, [0.0] * n
    for k, p in idx.items():
        j, i = divmod(k, nx)
        nb = ((k + 1, aE[i][j]) if i < nx - 1 else None, (k - 1, aE[i - 1][j]) if i else None,
              (k + nx, aN[i][j]) if j < ny - 1 else None, (k - nx, aN[i][j - 1]) if j else None)
        low = {}
        for q in nb:
            if q is None or q[1] == 0:
                continue
            diag[p] += q[1]
            if q[0] in idx:
                if idx[q[0]] < p:
                    low[idx[q[0]]] = -q[1]
            else:
                rhs[p] += q[1] * V[q[0]]
        first[p] = min(low, default=p)
        row = [0.0] * (p - first[p] + 1)
        for c, a in low.items():
            row[c - first[p]] = a
        row[-1] = diag[p]
        rows[p] = row
    # skyline Cholesky, in place: rows[p][c - first[p]] becomes L[p][c]
    for p in range(n):
        fp, Lp = first[p], rows[p]
        for c in range(fp, p):
            fc, Lc = first[c], rows[c]
            k0 = max(fp, fc)
            Lp[c - fp] = (Lp[c - fp] - _dot(Lp[k0 - fp:c - fp], Lc[k0 - fc:c - fc])) / Lc[-1]
        Lp[-1] = math.sqrt(Lp[-1] - _dot(Lp[:-1], Lp[:-1]))
    y = rhs[:]
    for p in range(n):                                   # L y = rhs
        fp, Lp = first[p], rows[p]
        y[p] = (y[p] - _dot(Lp[:-1], y[fp:p])) / Lp[-1]
    for p in range(n - 1, -1, -1):                       # L^T x = y
        fp, Lp = first[p], rows[p]
        y[p] /= Lp[-1]
        xp = y[p]
        for c in range(fp, p):
            y[c] -= Lp[c - fp] * xp
    for k, p in idx.items():
        V[k] = y[p]
    return sum(aE[i][j] * (V[j * nx + i] - V[j * nx + i + 1]) ** 2 for j in range(ny) for i in range(nx - 1)) \
        + sum(aN[i][j] * (V[j * nx + i] - V[(j + 1) * nx + i]) ** 2 for j in range(ny - 1) for i in range(nx))

_MEMO = {}

def _half_caps(w, h, er, t, s, mask, top, fill_er, res, grow):
    """(C, C0) of HALF the cross-section, units of eps0: strip half-width w/2 at
    x in [0, w/2], ground from w/2 + s outward (s None: none). Memoised; the air
    solve C0 does not depend on er, mask or fill, so it is shared across them."""
    feat = min(x for x in (w, h, s or w, t or w) if x > 0)
    reach = 4 * (w + 2 * min(s or 9e9, 2 * h)) + 12 * h      # how far the field is modelled
    x_edge, x_gnd = w / 2, (w / 2 + s) if s else None
    X = w / 2 + reach
    tm, erm = mask if mask else (0.0, 1.0)
    Ytop = (h + top) if top else (h + t + reach)
    xk = [0.0, x_edge, X] + ([x_gnd] if s else [])
    yk = [0.0, h, h + t, Ytop] + ([h + t + tm, h + tm] if tm else [])
    dmin = feat / (8 * res)
    xs = _axis(xk, dmin, reach / 10, grow)
    ys = _axis(yk, dmin, reach / 10, grow)
    cond = lambda x: x <= x_edge + 1e-9 or (s is not None and x >= x_gnd - 1e-9)
    def eps(i, j):
        xm, ym = (xs[i] + xs[i + 1]) / 2, (ys[j] + ys[j + 1]) / 2
        if ym < h:
            return er
        if top:
            return fill_er or er
        if tm and ((cond(xm) and ym < h + t + tm) or (not cond(xm) and ym < h + tm)):
            return erm                   # conformal mask: on the copper and in the gaps
        return 1.0
    def fixed(i, j):
        x, y = xs[i], ys[j]
        if y <= 1e-12 or (top and y >= Ytop - 1e-12):
            return 0.0
        if h - 1e-12 <= y <= h + t + 1e-12:
            if x <= x_edge + 1e-9:
                return 1.0
            if s is not None and x >= x_gnd - 1e-9:
                return 0.0
        return None
    g = tuple(round(v, 6) if v else v for v in (w, h, t, s, top, res, grow)) + (tm,)
    k0, k1 = g + ('air',), g + (er, mask, fill_er)
    if k0 not in _MEMO:
        _MEMO[k0] = _capacitance(xs, ys, lambda i, j: 1.0, fixed)
    if k1 not in _MEMO:
        _MEMO[k1] = _capacitance(xs, ys, eps, fixed)
    return _MEMO[k1], _MEMO[k0]

def field_zo(w, h, er, t=0.035, s=None, mask=None, top=None, fill_er=None, res=1.0, grow=1.5):
    """(Zo ohm, eeff) of a trace from the 2D field solution, all lengths mm.
      w, t      strip width and copper thickness
      h, er     dielectric to the reference plane below
      s         gap to a same-layer ground either side (CPWG); None = none.
                A (left, right) pair for unequal gaps: each half is solved with
                its own gap and the halves summed - exact when equal, and within
                0.1% of a full no-symmetry solve for 0.15/1.0 mm on this stackup
      mask      (thickness, er) solder mask over the strip, the side grounds
                and the substrate in the gaps; None = uncoated
      top       a second ground plane `top` mm above the strip's base, with
                fill_er (default er) between: stripline. None = open air
      res       edge refinement: grid spacing at conductor edges is the smallest
                feature / (8 res). Copper thickness is usually that feature, so
                res 1 reads ~1% LOW on a real trace (vs res 4, growth 1.15), in
                ~0.1 s per solve; a zero-thickness strip needs res ~8
      grow      grid growth per step away from an edge (1.3: ~2x the lines)"""
    t = max(t, 0.0)
    sl, sr = s if isinstance(s, (tuple, list)) else (s, s)
    halves = [_half_caps(w, h, er, t, g, mask, top, fill_er, res, grow) for g in {sl: 0, sr: 0}]
    if len(halves) == 1:
        halves *= 2
    c, c0 = sum(x[0] for x in halves), sum(x[1] for x in halves)
    return ETA0 / math.sqrt(c * c0), c / c0

def _K(k):
    """Complete elliptic integral of the first kind, modulus k (AGM)."""
    a, b = 1.0, math.sqrt(1 - k * k)
    while abs(a - b) > 1e-15:
        a, b = (a + b) / 2, math.sqrt(a * b)
    return math.pi / (2 * a)

def _selftest():
    """Solver vs references. Returns [(label, solver Zo, reference Zo)]."""
    out = []
    # stripline, t = 0, strip centred between planes 2b apart (b = 0.5 mm), er 4.4: exact
    k = 1 / math.cosh(math.pi * 0.3 / (4 * 0.5))
    ref = 30 * math.pi / math.sqrt(4.4) * _K(k) / _K(math.sqrt(1 - k * k))
    out.append(("stripline w0.3 b1.0 er4.4 t0", field_zo(0.3, 0.5, 4.4, t=0, top=0.5, res=8, grow=1.3)[0], ref))
    # CPW on a substrate much thicker than the slot, t = 0: exact conformal map,
    # eeff = (er+1)/2 (the far plane at h = 30 mm barely loads it)
    k = 0.3 / (0.3 + 2 * 0.2)
    ref = 30 * math.pi / math.sqrt(2.7) * _K(math.sqrt(1 - k * k)) / _K(k)
    out.append(("CPW w0.3 s0.2 er4.4 t0 (thick sub)", field_zo(0.3, 30.0, 4.4, t=0, s=0.2, res=8, grow=1.3)[0], ref))
    # microstrip, the kpcb selftest geometry: Hammerstad is itself ~1%
    out.append(("microstrip w0.36 h0.203 er4.4 t35u",
                field_zo(0.36, 0.203, 4.4, t=0.035)[0], microstrip(0.36, 0.203, 4.4, 0.035)[0]))
    return out

if __name__ == '__main__':
    args = sys.argv[1:]
    if not args or args[0] in ('-h', '--help'):
        print(__doc__); sys.exit(0)
    if args[0] == '--selftest':
        bad = 0
        for lab, z, ref in _selftest():
            err = (z - ref) / ref * 100
            bad += abs(err) > 2.0
            print(f"{'ok  ' if abs(err) <= 2.0 else 'FAIL'}  {lab:<40} solver {z:6.2f}  ref {ref:6.2f}  {err:+.2f}%")
        sys.exit(1 if bad else 0)
    m = None
    if '--mask' in args:
        i = args.index('--mask')
        m = (float(args[i + 1]), float(args[i + 2]))
        del args[i:i + 3]
    w, s, h, er = map(float, args[:4])
    t = float(args[4]) if len(args) > 4 else 0.035
    z, ee = field_zo(w, h, er, t=t, s=s if s > 0 else None, mask=m)
    print(f"Zo {z:.2f} ohm  eeff {ee:.3f}   (w {w} s {s} h {h} er {er} t {t}"
          + (f", mask {m[0]} mm er {m[1]}" if m else ", uncoated") + ")")
