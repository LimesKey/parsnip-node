#!/usr/bin/env python3
"""
kcap.py - MLCC effective-capacitance and voltage-stress-life comparator.
Stdlib only, works fully offline. Answers "higher voltage rating or higher
nominal capacitance?" for a given footprint, by modelling how much of a part's
printed capacitance survives DC bias derating, density-driven dielectric
thinning, temperature and aging - and how each candidate's life margin compares.

  kcap.py compare '10u/25V/X5R/0805' '10u/25V/X7R/1206' --vop 8.4 --temp 45
  kcap.py compare C19666 C1791     --vop 5.0            # LCSC C-numbers work too
  kcap.py solve MLCC --need 8uF --vop 8.4 --temp 45      # smallest/cheapest that clears it

A "spec string" is slash-separated, order-independent: CAP/VOLT/DIEL/PKG, e.g.
'10u/25V/X5R/0805' or '22nF/50V/C0G/0603'. An LCSC C-number resolves live via
part-search's part.py if that skill is installed alongside this one - optional;
everything else here works with no network at all.

The model (eff_C = C_nom * bias * densitySeverity * tempPenalty * agingPenalty):

  bias           DC-bias retained-fraction curve vs Vop/Vrated, per dielectric.
                 Real parts vary +/-15 points from this generalised fit -
                 override with --retained-a/--retained-b from a datasheet or
                 Murata SimSurfing number when it matters.
  densitySeverity how close C_nom sits to the catalog's density ceiling for
                 that pkg/voltage/dielectric (a part crammed to the ceiling has
                 thinner dielectric layers and derates harder under the same
                 bias fraction). C0G is never density-crammed, so this is 1.0
                 for C0G. --catalog-max-a/-b overrides the fitted ceiling;
                 --live queries part-search's LCSC client for a real one
                 (network, optional refinement only - never required).
  tempPenalty    Class II (X7R/X5R) only; 1.0 at or below 25 C.
  agingPenalty   Class II only; capacitance drift referenced to 1000 h
                 post-firing (where datasheet capacitance is actually
                 specified), not t=1h - crediting a "gain" below 1000 h would
                 be wrong, so it is floored at 1.0 there instead.

Voltage-stress life (Prokopowicz-Vaskas power law / Arrhenius, TDDB /
insulation-resistance wear-out ONLY - not flex cracking, thermal cycling or
moisture, which dominate real field failures):

  L = L_ref * (Vrated/Vop)^n * exp[(Ea/k)(1/Top - 1/Tref)]

L_ref/Tref/Ea are an ASSUMED illustrative reference point (default: 100,000 h
at the part's own rated voltage and 125 C, Ea=1.2 eV), not a datasheet-
certified figure - override with --lref-hours/--tref/--ea if a manufacturer
publishes real endurance-test numbers for the parts in question. Because Tref
is a single shared model constant, the Arrhenius term cancels exactly between
two candidates run at the same Vop/temp, so the printed A-vs-B ratio always
collapses to (Vrated_A/Vrated_B)^n regardless of Tref/Ea/L_ref - only each
part's own displayed absolute life depends on those assumptions.

Flags: --vop V (required) --temp C (25) --hours H (1000, since datasheet
capacitance) --life-n (3, range 2.2-5) --ea EV (1.2, range 0.9-1.7) --tref C (125)
--lref-hours H (100000) --retained-a/-b PCT --dim-a/-b L,W,H (mm, overrides
the case-code geometry table) --catalog-max-a/-b UF --live --fresh --json
--pkg P1,P2 --diel D1,D2 -n N (solve: search scope/result count) --need SPEC
(solve: minimum effective capacitance).
"""
import sys, os, re, math, json, argparse

# ---------------------------------------------------------------- bias curves

BIAS_X = [0, 0.2, 0.4, 0.5, 0.6, 0.8, 1.0]
CURVES = {
    'C0G': [1.00, 1.00, 0.99, 0.99, 0.98, 0.98, 0.97],
    'X7R': [1.00, 0.95, 0.88, 0.82, 0.78, 0.65, 0.50],
    'X5R': [1.00, 0.90, 0.72, 0.60, 0.50, 0.38, 0.22],
}

def interp(xs, ys, x):
    """Piecewise-linear lookup. For x beyond the table, continue the last
    segment's slope rather than clamping flat, floored at 0.05 - a part run
    over its rated voltage keeps losing retained capacitance, it doesn't stop."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        slope = (ys[-1] - ys[-2]) / (xs[-1] - xs[-2])
        return max(0.05, ys[-1] + slope * (x - xs[-1]))
    for i in range(len(xs) - 1):
        if xs[i] <= x <= xs[i + 1]:
            f = (x - xs[i]) / (xs[i + 1] - xs[i])
            return ys[i] + f * (ys[i + 1] - ys[i])

def clamp(v, lo, hi):
    return max(lo, min(hi, v))

# ---------------------------------------------------------------- case geometry
# L, W = confirmed against multiple independent manufacturer dimension tables
# (Murata/Kemet/Vishay generic EIA chart; cross-checked 2026-08). H is NOT a
# fixed per-case dimension in real parts - KEMET's own C1002 X7R datasheet
# (Table 1 + Table 2A "Chip Thickness" codes, pulled 2026-08-28) shows the real
# range per case is wide, since thickness scales with capacitance/layer count
# within one case code, e.g. 0805 spans 0.70-1.25mm across its thickness codes,
# 1210 spans 0.78-2.50mm. H below is the middle of that documented range for a
# common general-purpose part in each case - NOT the case maximum. For a
# specific real SKU, override with --dim-a/--dim-b L,W,H from its own drawing.
CASE = {
    '0201': (0.60, 0.30, 0.30),
    '0402': (1.00, 0.50, 0.50),   # KEMET Table 2A: only one thickness code, 0.50mm
    '0603': (1.60, 0.80, 0.80),   # KEMET Table 2A: 0.80mm across all grades
    '0805': (2.00, 1.25, 0.85),   # KEMET Table 2A range 0.70-1.25mm
    '1206': (3.20, 1.60, 0.95),   # KEMET Table 2A range 0.78-1.60mm
    '1210': (3.20, 2.50, 1.25),   # KEMET Table 2A range 0.78-2.50mm
    '1812': (4.50, 3.20, 1.60),   # KEMET Table 2A range 1.00-2.50mm
    '2220': (5.70, 5.00, 1.60),   # KEMET Table 2A range 1.00-2.40mm
}
PKG_RE = re.compile(r'\b(0201|0402|0603|0805|1206|1210|1808|1812|1825|2220|2225)\b')

# ---------------------------------------------------------------- density ceiling
# Fitted to live LCSC maxima, 0402-1206 / 6.3-50V, pulled 2026-08-20.

def x7r_ceiling_uF(vol_mm3, vr):
    """CV/volume held ~61-75 across all sampled parts - trust this one."""
    return 68.0 * vol_mm3 / vr

def x5r_ceiling_uF(vol_mm3, vr):
    """Rougher fit: miniaturisation R&D concentrates on 0402/0603, so the
    curve isn't as clean as X7R's. Piecewise power law with a high-voltage
    correction above 20V."""
    if vr <= 10:
        c = 52.4 * vol_mm3 ** 0.809
    else:
        c = 13.6 * vol_mm3 ** 0.607
        if vr > 20:
            c *= (20.0 / vr) ** 0.5
    return c

def default_ceiling_uF(diel, vr, vol_mm3):
    if diel == 'X7R':
        return x7r_ceiling_uF(vol_mm3, vr)
    if diel == 'X5R':
        return x5r_ceiling_uF(vol_mm3, vr)
    return None   # C0G is never density-crammed; agg forced to 0 by the caller

# ---------------------------------------------------------------- temp / aging

TMAX = {'X7R': 125.0, 'X5R': 85.0}                 # C0G has no stated derating here
AGING = {'X7R': 0.025, 'X5R': 0.07, 'C0G': 0.0}     # fractional retained-cap loss / decade-hour

# ---------------------------------------------------------------- the model

def effective(part, vop, tempC, hours, retained_pct=None, ceiling_uF=None):
    """part: {'cap' F, 'volt' V, 'diel', 'pkg', 'dims': (L,W,H) mm}. Returns a
    dict of every intermediate factor plus eff_uF / uF_per_mm3 / uF_per_mm2."""
    diel, vr, capF = part['diel'], part['volt'], part['cap']
    L, W, H = part['dims']
    vol = L * W * H
    x = (vop / vr) if vr else 0.0
    bias = (retained_pct / 100.0) if retained_pct is not None else interp(BIAS_X, CURVES[diel], x)
    cap_uF = capF * 1e6
    if diel == 'C0G':
        agg, density, ceil = 0.0, 1.0, None
    else:
        ceil = ceiling_uF if ceiling_uF is not None else default_ceiling_uF(diel, vr, vol)
        agg = clamp(cap_uF / ceil, 0.0, 1.5) if ceil and ceil > 0 else 0.0
        density = clamp(1 - agg * 0.8 * x, 0.15, 1.0)
    if diel == 'C0G':
        tempf = 1.0
    else:
        tmax = TMAX[diel]
        tempf = 1.0 if tempC <= 25 else 1 - 0.15 * clamp((tempC - 25) / (tmax - 25), 0.0, 1.0)
    k = AGING.get(diel, 0.0)
    agef = 1.0 if (hours < 1000 or k == 0) else max(0.0, 1 - k * math.log10(hours / 1000.0))
    eff_uF = cap_uF * bias * density * tempf * agef
    return {'x': x, 'bias': bias, 'density': density, 'tempf': tempf, 'agef': agef,
            'eff_uF': eff_uF, 'agg': agg, 'ceiling_uF': ceil, 'vol_mm3': vol,
            'area_mm2': L * W,
            'uF_per_mm3': (eff_uF / vol) if vol else None,
            'uF_per_mm2': (eff_uF / (L * W)) if (L * W) else None}

# ---------------------------------------------------------------- voltage-stress life

K_B_EV = 8.617333262e-5   # Boltzmann constant, eV/K

def margin_factor(vr, vop, n=3.0):
    return (vr / vop) ** n if vop > 0 else float('inf')

def life_hours(vr, vop, tempC, n=3.0, ea=1.2, tref_c=125.0, lref_hours=100000.0):
    """L_ref/T_ref/Ea are a single shared assumed reference point (see module
    docstring) - only the (Vrated/Vop)^n term differs per part at equal Vop/T,
    so the A-vs-B ratio always collapses to (Vr_A/Vr_B)^n regardless of them."""
    if vop <= 0:
        return float('inf')
    top_k, tref_k = tempC + 273.15, tref_c + 273.15
    arrh = math.exp((ea / K_B_EV) * (1.0 / top_k - 1.0 / tref_k))
    return lref_hours * margin_factor(vr, vop, n) * arrh

def fmt_si(x):
    if x is None:
        return '?'
    for mag, suf in ((1, ''), (1e-3, 'm'), (1e-6, 'u'), (1e-9, 'n'), (1e-12, 'p')):
        if abs(x) >= mag * 0.999:
            return f"{x/mag:.4g}{suf}"
    return f"{x:.4g}"

def trunc(s, n):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s if len(s) <= n else s[:n - 1] + '…'

# ---------------------------------------------------------------- spec parsing

_SI = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, 'µ': 1e-6, 'μ': 1e-6,
       'm': 1e-3, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9}
_SI_RE = re.compile(r'^([0-9]*\.?[0-9]+)\s*([pnuµμmkKMG]?)$')

def si(s):
    m = _SI_RE.fullmatch(str(s).strip())
    if not m:
        return None
    return float(m.group(1)) * _SI.get(m.group(2), 1.0)

DIEL_SET = {'C0G', 'NP0', 'X7R', 'X5R'}

def _norm_diel(s):
    u = re.sub(r'[^A-Za-z0-9]', '', str(s or '')).upper()
    for code in ('X7R', 'X5R', 'C0G', 'NP0'):
        if code in u:
            return 'C0G' if code == 'NP0' else code
    return None

def parse_spec(s):
    """'10u/25V/X5R/0805' -> {'cap':1e-5,'volt':25.0,'diel':'X5R','pkg':'0805'}.
    A bare LCSC C-number returns {'lcsc': 'C...'} instead, resolved by the caller."""
    s = s.strip()
    if re.fullmatch(r'[Cc]\d+', s):
        return {'lcsc': s.upper()}
    out = {}
    for tok in (t.strip() for t in s.split('/') if t.strip()):
        u = tok.upper()
        if PKG_RE.fullmatch(u):
            out['pkg'] = u
        elif u in DIEL_SET:
            out['diel'] = _norm_diel(u)
        elif re.fullmatch(r'[0-9.]+[pnuµμmkKMG]?[Vv]', tok):
            out['volt'] = si(tok[:-1])
        elif re.fullmatch(r'[0-9.]+[pnuµμ]?[Ff]?', tok) and si(tok.rstrip('Ff')) is not None:
            out['cap'] = si(tok.rstrip('Ff'))
        else:
            v = si(tok)
            if v is not None:
                out.setdefault('cap', v)
    return out

def _partsearch():
    """part-search's part.py, if that skill is installed alongside this one.
    None if not importable - kcap.py must still work fully offline on the
    fitted ceiling formulas; live catalog data is only ever an optional
    refinement, never a requirement."""
    try:
        import glob as _g
        cands = [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]
        cands += [os.path.dirname(x) for x in
                  _g.glob('/mnt/skills/*/*/scripts/part.py') +
                  _g.glob('/mnt/skills/*/*/part.py')]
        for c in cands:
            if c and c not in sys.path:
                sys.path.insert(0, c)
        # part.py is only the CLI since the split; the catalog calls live here
        from types import SimpleNamespace
        from part_core import lcsc_detail, lcsc_search
        from part_value import attr_of, enum
        return SimpleNamespace(lcsc_detail=lcsc_detail, lcsc_search=lcsc_search,
                               attr_of=attr_of, enum=enum)
    except Exception:
        return None

def resolve_part(spec_str, part_mod=None, fresh=False):
    """spec string -> {'cap' F, 'volt' V, 'diel', 'pkg'} (no dims yet - those
    come from the CASE table or a --dim-a/-b override, applied by the caller)."""
    parsed = parse_spec(spec_str)
    if 'lcsc' in parsed:
        if part_mod is None:
            raise ValueError(f"{spec_str}: looks like an LCSC C-number, but "
                              f"part-search's part.py isn't importable (install "
                              f"both skills side by side), and kcap.py needs it "
                              f"to resolve a C-number to real part attributes. "
                              f"Give a spec string instead, e.g. '10u/25V/X5R/0805'.")
        rec = part_mod.lcsc_detail(parsed['lcsc'], fresh)
        if not rec:
            raise ValueError(f"{spec_str}: not found on LCSC")
        params = rec.get('params') or []
        cap = part_mod.enum(part_mod.attr_of(params, 'cap'))
        volt = part_mod.enum(part_mod.attr_of(params, 'volt'))
        diel = _norm_diel(part_mod.attr_of(params, 'diel'))
        pkgtxt = rec.get('package') or part_mod.attr_of(params, 'pkg') or ''
        m = PKG_RE.search(pkgtxt.upper())
        parsed = {'cap': cap, 'volt': volt, 'diel': diel, 'pkg': m.group(1) if m else None}
    missing = [k for k in ('cap', 'volt', 'diel', 'pkg') if not parsed.get(k)]
    if missing:
        raise ValueError(f"{spec_str}: could not determine {', '.join(missing)} - "
                          f"give it explicitly, e.g. '10u/25V/X5R/0805'")
    if parsed['diel'] not in CURVES:
        raise ValueError(f"{spec_str}: dielectric {parsed['diel']!r} not recognised "
                          f"(know C0G, X7R, X5R)")
    return {'cap': parsed['cap'], 'volt': parsed['volt'], 'diel': parsed['diel'],
            'pkg': parsed['pkg'].upper()}

def dims_for(pkg, override=None):
    if override:
        return override
    if pkg not in CASE:
        raise ValueError(f"unknown package {pkg!r} - known: {', '.join(sorted(CASE))}; "
                          f"or give --dim-a/--dim-b L,W,H directly")
    return CASE[pkg]

def live_ceiling_uF(pkg, diel, vr, part_mod, fresh=False):
    """Live refinement of the fitted density ceiling: the largest real
    capacitance actually stocked in this pkg/diel/voltage bucket on LCSC.
    None on any failure - callers must fall back to the fitted formula, never
    block on this. --live only; never called by default."""
    try:
        rows, _ = part_mod.lcsc_search(f"MLCC {vr:g}V {pkg} {diel}", 60, fresh)
        best = None
        for r in rows[:40]:
            full = part_mod.lcsc_detail(r['sku'], fresh)
            if not full or pkg not in (full.get('package') or '').upper():
                continue
            c = part_mod.enum(part_mod.attr_of(full.get('params'), 'cap'))
            if c is not None and (best is None or c > best):
                best = c
        return best * 1e6 if best is not None else None
    except Exception:
        return None

# ---------------------------------------------------------------- compare

def c_compare(a, part_mod):
    try:
        pa = resolve_part(a.args[0], part_mod, a.fresh)
        pb = resolve_part(a.args[1], part_mod, a.fresh)
        pa['dims'] = tuple(float(x) for x in a.dim_a.split(',')) if a.dim_a else dims_for(pa['pkg'])
        pb['dims'] = tuple(float(x) for x in a.dim_b.split(',')) if a.dim_b else dims_for(pb['pkg'])
    except ValueError as e:
        print(str(e)); return 1

    ceil_a, ceil_b = a.catalog_max_a, a.catalog_max_b
    if a.live and part_mod is not None:
        if ceil_a is None and pa['diel'] != 'C0G':
            ceil_a = live_ceiling_uF(pa['pkg'], pa['diel'], pa['volt'], part_mod, a.fresh) or None
        if ceil_b is None and pb['diel'] != 'C0G':
            ceil_b = live_ceiling_uF(pb['pkg'], pb['diel'], pb['volt'], part_mod, a.fresh) or None
    elif a.live and part_mod is None:
        print("(--live ignored: part-search's part.py isn't importable)", file=sys.stderr)

    ea_ = effective(pa, a.vop, a.temp, a.hours, a.retained_a, ceil_a)
    eb_ = effective(pb, a.vop, a.temp, a.hours, a.retained_b, ceil_b)
    la = life_hours(pa['volt'], a.vop, a.temp, a.life_n, a.ea, a.tref, a.lref_hours)
    lb = life_hours(pb['volt'], a.vop, a.temp, a.life_n, a.ea, a.tref, a.lref_hours)
    ma = margin_factor(pa['volt'], a.vop, a.life_n)
    mb = margin_factor(pb['volt'], a.vop, a.life_n)

    if a.json:
        print(json.dumps({'vop': a.vop, 'temp': a.temp, 'hours': a.hours,
                          'A': {**pa, **ea_, 'life_hours': la, 'margin': ma},
                          'B': {**pb, **eb_, 'life_hours': lb, 'margin': mb}}, indent=1))
        return 0

    def label(p, spec):
        return f"{spec}  ({fmt_si(p['cap'])}F {p['volt']:g}V {p['diel']} {p['pkg']})"

    def yr(h):
        y = h / 8760.0
        return "> 10 yr (not limiting)" if y > 10 else f"{y:.2f} yr"

    print(f"Vop={a.vop:g} V   T={a.temp:g} C   t={a.hours:g} h post-firing\n")
    for tag, spec, p, e_, life, marg, ceil_over in (
            ('A', a.args[0], pa, ea_, la, ma, ceil_a),
            ('B', a.args[1], pb, eb_, lb, mb, ceil_b)):
        print(f"[{tag}] {label(p, spec)}")
        print(f"  retained (bias)   : {e_['bias']*100:.1f}%   (x=Vop/Vrated={e_['x']:.3f})")
        if p['diel'] != 'C0G':
            src = 'override/live' if ceil_over is not None else 'fitted'
            print(f"  density severity  : {e_['density']*100:.1f}%   "
                  f"(agg={e_['agg']*100:.0f}% of {src} ceiling {e_['ceiling_uF']:.3g} uF)")
        print(f"  temp / aging      : {e_['tempf']*100:.1f}% / {e_['agef']*100:.1f}%")
        print(f"  effective C       : {fmt_si(p['cap'])}F -> {fmt_si(e_['eff_uF']*1e-6)}F "
              f"({e_['eff_uF']:.4g} uF)")
        print(f"  volume / area     : {e_['vol_mm3']:.3g} mm^3 / {e_['area_mm2']:.3g} mm^2")
        print(f"  uF/mm^3 / uF/mm^2 : {e_['uF_per_mm3']:.4g} / {e_['uF_per_mm2']:.4g}")
        print(f"  voltage margin    : {marg:.2f}x (Vrated/Vop={p['volt']/a.vop:.2f})   "
              f"life @ {a.temp:g}C ~ {yr(life)}\n")

    ratio = (la / lb) if lb else float('inf')
    if abs(ratio - 1) <= 0.10:
        life_verdict = "voltage-stress life is comparable between A and B (not a deciding factor)"
    elif ratio > 1:
        life_verdict = f"A has ~{ratio:.2g}x the voltage-stress life of B"
    else:
        life_verdict = f"B has ~{1/ratio:.2g}x the voltage-stress life of A"
    winner = 'A' if ea_['eff_uF'] >= eb_['eff_uF'] else 'B'
    w, l = sorted((ea_['eff_uF'], eb_['eff_uF']), reverse=True)
    print(f"verdict: {winner} gives more effective capacitance "
          f"({fmt_si(w*1e-6)}F vs {fmt_si(l*1e-6)}F, +{(w - l) / max(l, 1e-12) * 100:.0f}%); "
          f"{life_verdict}.")
    print("(voltage-stress life covers TDDB/insulation-resistance wear-out only, not "
          "flex cracking, thermal cycling or moisture, which dominate real field "
          "failures; life_hours/L_ref/T_ref/Ea are an assumed illustrative reference, "
          "not a datasheet-certified figure.)")
    return 0

# ---------------------------------------------------------------- solve

def c_solve(a, part_mod):
    if part_mod is None:
        print("solve needs part-search's part.py importable for real catalog "
              "candidates (install both skills side by side). Use `compare` on "
              "specific spec strings instead for fully offline work.")
        return 1
    need_uF = si(a.need.rstrip('Ff'))
    if need_uF is None:
        print(f"--need {a.need!r}: could not parse as a capacitance"); return 1
    need_uF *= 1e6
    pkgs = [p.strip().upper() for p in (a.pkg or '0402,0603,0805,1206,1210').split(',') if p.strip()]
    diels = [d.strip().upper() for d in (a.diel or 'X7R,X5R').split(',') if d.strip()]
    kw = ' '.join(a.args) or 'MLCC'
    vmin = a.vop * 1.15   # a little headroom over Vop as the search floor

    import argparse
    from part_pick import apply_cons, build_pool, _pick_category
    from part_value import make_pred
    for d in diels:
        if d not in CURVES:
            print(f"(skipping unknown dielectric {d!r})", file=sys.stderr)
    diels = [d for d in diels if d in CURVES]
    best = {}                  # (pkg, cap, volt, diel) -> cheapest part of that spec
    for pkg in pkgs:
        if pkg not in CASE:
            print(f"(skipping unknown package {pkg!r})", file=sys.stderr); continue
        # pick's pool: JLC category + package + server-side nominal/voltage/dielectric
        # filter, in stock, cheapest first. A keyword search returned 40 parts of
        # every value (220 pF..10 uF) and "found nothing" that the catalog has.
        cons = [(n, lab, p) for n, spec in (('cap', f'>={need_uF:g}u'), ('volt', f'>={vmin:.3g}'),
                                             ('diel', ','.join(diels)), ('pkg', pkg))
                for p, lab in [make_pred(spec)]]
        pa = argparse.Namespace(args=[kw], cat=None, pkg=pkg, fresh=a.fresh, sort='price',
                                minstock=100, maxq=14, basic=False, source='jlc', jobs=8, pool=240)
        pa._jcat = _pick_category(pa)
        recs, _ = build_pool(pa, cons, [f"{kw} {pkg} {d}" for d in diels])
        for full in apply_cons(recs, cons)[0]:
            params = full.get('params') or []
            capv = part_mod.enum(part_mod.attr_of(params, 'cap'))
            voltv = part_mod.enum(part_mod.attr_of(params, 'volt'))
            dielv = _norm_diel(part_mod.attr_of(params, 'diel'))
            if not (capv and voltv and dielv in diels):
                continue
            part = {'cap': capv, 'volt': voltv, 'diel': dielv, 'pkg': pkg, 'dims': CASE[pkg]}
            e_ = effective(part, a.vop, a.temp, a.hours)
            lad = full.get('ladder') or []
            price = lad[0][1] if lad else None
            k = (pkg, capv, voltv, dielv)
            if e_['eff_uF'] >= need_uF and (k not in best or (price or 1e9) < (best[k][1] or 1e9)):
                best[k] = (e_['vol_mm3'], price, full.get('mpn'), full['sku'], part, e_)
    cands = sorted(best.values(), key=lambda c: (c[0], c[1] if c[1] is not None else 1e9))
    if not cands:
        print(f"solve: nothing found clearing {need_uF:g} uF effective at "
              f"Vop={a.vop:g}V T={a.temp:g}C across {','.join(pkgs)} x {','.join(diels)}. "
              f"Try a lower --need, more --pkg/--diel options, or a different keyword.")
        return 1
    if a.json:
        print(json.dumps([{'sku': c[3], 'mpn': c[2], 'price_usd': c[1],
                           'part': {k: v for k, v in c[4].items() if k != 'dims'},
                           **c[5]} for c in cands[:a.n]], indent=1))
        return 0
    print(f"solve: need >= {need_uF:g} uF effective at Vop={a.vop:g}V T={a.temp:g}C  "
          f"({len(cands)} candidate(s) across {','.join(pkgs)} x {','.join(diels)})\n")
    print(f"{'sku':<11} {'mpn':<24} {'pkg':<6} {'diel':<5} {'nom uF':>8} {'Vr':>5} "
          f"{'eff uF':>8} {'mm^3':>7} {'price':>9}")
    for vol, price, mpn, sku, part, e_ in cands[:a.n]:
        print(f"{sku:<11} {trunc(mpn,24):<24} {part['pkg']:<6} {part['diel']:<5} "
              f"{part['cap']*1e6:>8.3g} {part['volt']:>5g} {e_['eff_uF']:>8.3g} "
              f"{vol:>7.3g} {('$%.4f' % price) if price else '?':>9}")
    cheapest = min((c for c in cands if c[1] is not None), key=lambda c: c[1], default=None)
    if cheapest and cheapest[3] != cands[0][3]:
        print(f"\ncheapest that still clears the target: {cheapest[3]} {cheapest[2]} "
              f"(${cheapest[1]:.4f}, {cheapest[0]:.3g} mm^3) - smallest was {cands[0][3]}")
    return 0

# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('cmd', choices=['compare', 'solve'])
    ap.add_argument('args', nargs='*')
    ap.add_argument('--vop', type=float, default=None, help='operating voltage (required)')
    ap.add_argument('--temp', type=float, default=25.0, help='operating temperature, C')
    ap.add_argument('--hours', type=float, default=1000.0,
                    help='aging reference point, hours post-firing (default 1000, where '
                         'datasheet capacitance is specified)')
    ap.add_argument('--life-n', dest='life_n', type=float, default=3.0,
                    help='life voltage exponent (2.2-5)')
    ap.add_argument('--ea', type=float, default=1.2, help='life activation energy, eV (0.9-1.7)')
    ap.add_argument('--tref', type=float, default=125.0, help='life reference temperature, C')
    ap.add_argument('--lref-hours', type=float, default=100000.0,
                    help='assumed reference life at rated V and --tref, hours')
    ap.add_argument('--retained-a', type=float, default=None,
                    help='override part A bias-retained fraction, percent')
    ap.add_argument('--retained-b', type=float, default=None)
    ap.add_argument('--dim-a', default=None, help='L,W,H mm - override part A case geometry')
    ap.add_argument('--dim-b', default=None)
    ap.add_argument('--catalog-max-a', type=float, default=None,
                    help='override the fitted density ceiling for part A, uF')
    ap.add_argument('--catalog-max-b', type=float, default=None)
    ap.add_argument('--live', action='store_true',
                    help='refine the density ceiling from a live LCSC query (network; '
                         'optional - default is fully offline on the fitted formulas)')
    ap.add_argument('--need', default=None, help='solve: minimum effective capacitance, e.g. 8uF')
    ap.add_argument('--pkg', default=None, help='solve: comma list of case codes to search')
    ap.add_argument('--diel', default=None, help='solve: comma list of dielectrics to search')
    ap.add_argument('-n', dest='n', type=int, default=8, help='solve: result rows')
    ap.add_argument('--fresh', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('-h', '--help', action='store_true')
    a = ap.parse_args()
    if a.help:
        print(__doc__); return 0
    if a.vop is None:
        print("give --vop (the actual operating voltage this part sees)"); return 1
    part_mod = _partsearch()
    if a.cmd == 'compare':
        if len(a.args) < 2:
            print("compare needs two part specs, e.g. kcap.py compare "
                  "'10u/25V/X5R/0805' '10u/25V/X7R/1206' --vop 8.4")
            return 1
        return c_compare(a, part_mod)
    if not a.need:
        print("solve needs --need <capacitance>, e.g. --need 8uF"); return 1
    return c_solve(a, part_mod)

try:                      # piping to `head` should not print a traceback
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
