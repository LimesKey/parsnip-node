"""part.py value parsing: engineering values, attribute-name resolution and the
constraint grammar (pure, no network)."""
import re, math

# ================================================================ value parsing

_MUL = {'p': 1e-12, 'n': 1e-9, 'u': 1e-6, '\u00b5': 1e-6, '\u03bc': 1e-6, 'm': 1e-3,
        'R': 1.0, 'r': 1.0, 'k': 1e3, 'K': 1e3, 'M': 1e6, 'G': 1e9, 'T': 1e12}
_TAIL = re.compile(r'(ohms?|\u2126|F|V(?:DC|AC)?|A|W|Hz|H|%|s)\s*$', re.I)

def enum(s):
    """Engineering value -> float SI. '4.7uF'->4.7e-6, '4u7'->4.7e-6, '100nF'->1e-7,
    '25V'->25, '10k'->1e4, '4R7'->4.7, '±20%'->20. None when not numeric."""
    if s is None:
        return None
    t = str(s).strip().replace('\u00b1', '').replace(',', '')
    # '10V~35V' -> '10V'; LCSC appends test conditions after '@' on most discrete
    # specs ('5m\u03a9@10V', '450mV@1A', '15A@8/20us') - without this cut every
    # --rdson/--vf/--ir constraint rejected every real part
    t = re.split(r'[~/@]', t)[0].strip()
    if not t:
        return None
    for _ in range(3):
        t2 = _TAIL.sub('', t).strip()
        if t2 == t:
            break
        t = t2
    m = re.fullmatch(r'(\d+)\s*([pnu\u00b5\u03bcmkKMGRrT])\s*(\d+)', t)
    if m:
        return float(f"{m.group(1)}.{m.group(3)}") * _MUL.get(m.group(2), 1.0)
    m = re.fullmatch(r'([-+]?\d*\.?\d+)\s*([pnu\u00b5\u03bcmkKMGRrT]?)', t)
    if not m:
        return None
    return float(m.group(1)) * (_MUL.get(m.group(2), 1.0) if m.group(2) else 1.0)

def fmt_si(x, unit=''):
    if x is None:
        return '?'
    for mag, suf in ((1e9, 'G'), (1e6, 'M'), (1e3, 'k'), (1, ''), (1e-3, 'm'),
                     (1e-6, 'u'), (1e-9, 'n'), (1e-12, 'p')):
        if abs(x) >= mag * 0.999:
            return f"{x/mag:.10g}{suf}{unit}"
    return f"{x/1e-12:.10g}p{unit}" if x else f"0{unit}"     # 0.06pF, not 6e-14 (enum can't read it)

def _norm(s):
    return re.sub(r'[^a-z0-9]', '', (s or '').lower())

# Shorthand -> the parameter names LCSC and JLC actually use. Fuzzy substring match
# is applied after these, so an unlisted synonym still usually resolves.
ALIAS = {
    'cap': ['capacitance'], 'res': ['resistance'], 'ind': ['inductance'],
    'volt': ['voltagerated', 'voltagerating', 'ratedvoltage', 'voltage',
             'withstandvoltage', 'voltagedc'],
    'tol': ['tolerance'], 'diel': ['temperaturecoefficient', 'dielectric'],
    'pkg': ['package'], 'esr': ['esr'], 'power': ['powerrating', 'power'],
    'current': ['ratedcurrent', 'currentrating', 'current'],
    'freq': ['frequency'], 'temp': ['operatingtemperature', 'temperature'],
    'type': ['type'], 'dcr': ['dcresistance', 'dcr'],
    # diodes / FETs / inductors: LCSC does not call these "voltage" or "current"
    'vr': ['voltagedcreversevr', 'reversevoltage', 'voltagedcreverse'],
    'vf': ['voltageforwardvfif', 'forwardvoltage'],
    'ifwd': ['currentrectified', 'currentaverage', 'forwardcurrent'],
    'ir': ['reverseleakagecurrentir', 'reverseleakagecurrent'],
    'vds': ['draintosourcevoltage', 'drainsourcevoltagevdss', 'vdss'],
    'id': ['currentcontinuousdrain', 'continuousdraincurrent'],
    'vgsth': ['gatethresholdvoltage'],
    'rdson': ['drainsourceonresistancerdson', 'rdson'],
    'isat': ['saturationcurrent', 'currentsaturation'],
    'irms': ['currentrating', 'ratedcurrent'],
    # TVS / regulators
    'vrwm': ['reversestandoffvoltage'], 'vc': ['clampingvoltage'],
    'vout': ['outputvoltage'], 'iout': ['outputcurrent'],
}
_UNIT_OF = {'cap': 'F', 'res': '\u2126', 'ind': 'H', 'volt': 'V', 'current': 'A',
            'power': 'W', 'freq': 'Hz', 'vr': 'V', 'vf': 'V', 'ifwd': 'A',
            'vds': 'V', 'isat': 'A', 'irms': 'A', 'id': 'A', 'vgsth': 'V',
            'vrwm': 'V', 'vc': 'V', 'vout': 'V', 'iout': 'A'}
# Every shorthand gets its own --flag. Anything not here is still reachable with
# --w NAME=SPEC, which also accepts the verbatim LCSC attribute name.
FLAG_ATTRS = ('cap', 'res', 'ind', 'volt', 'pkg', 'diel', 'tol', 'current', 'power',
              'freq', 'temp', 'type', 'dcr', 'vr', 'vf', 'ifwd', 'ir', 'vds', 'id',
              'vgsth', 'rdson', 'isat', 'irms', 'esr', 'vrwm', 'vc', 'vout', 'iout')

def attr_hit(params, name):
    """(resolved parameter name, value) or (None, None). Resolution order:
    exact alias, then prefix match, then containment. Ties break on shortest name
    then alphabetically, so the answer never depends on parameter ordering - two
    parts in the same pool must resolve `volt` to the same attribute."""
    keys = ALIAS.get(name.lower()) or [_norm(name)]
    pn = [(_norm(k), k, v) for k, v in (params or []) if k]
    for k in keys:
        for n, orig, v in pn:
            if n == k:
                return orig, v
    cands = []
    for k in keys:
        if len(k) <= 3:
            continue
        for n, orig, v in pn:
            if n.startswith(k):
                cands.append((0, len(n), n, orig, v))
            elif k in n:
                cands.append((1, len(n), n, orig, v))
    n0 = _norm(name)
    # raw-name containment only for verbatim --w names. A shorthand's ALIAS list
    # is its definition: 'res' as a bare substring matched 'Gate THRESHOLD Voltage'
    # and 'cap' matched a FET's Ciss, which is how `alt` of a MOSFET went empty.
    if len(n0) > 2 and name.lower() not in ALIAS:
        for n, orig, v in pn:
            if n0 in n or n in n0:
                cands.append((2, len(n), n, orig, v))
    if not cands:
        return None, None
    cands.sort()
    return cands[0][3], cands[0][4]

def attr_of(params, name):
    return attr_hit(params, name)[1]

def _exact(params, short):
    """(name, value) whose normalised name IS one of the shorthand's aliases -
    unlike attr_hit, never a fuzzy neighbour such as a FET's Ciss for 'cap'."""
    keys = ALIAS.get(short, [short])
    return next(((k, v) for k, v in params or [] if _norm(k) in keys), (None, None))

def _short(name):
    """8-char column label: the shorthand this is an alias of, else its '(Vr)' tag."""
    n = _norm(name)
    k = next((k for k, al in ALIAS.items() if n in al), None)
    tags = re.findall(r'\((\w[^()]*(?:\(\w+\))?)\)', name)      # 'Vgs(th)', 'Id', 'Vf@If'
    return k or (tags[-1] if tags and len(name) > 8 else name)

def make_pred(spec):
    """Constraint spec -> (predicate over raw string, label).
      '4.7u..100u'  inclusive range      '>=25' '<=50' '>1k' '<10'
      'X7R,X5R'     any-of, numeric-aware ('25' matches '25V')
      '~ceramic'    case-insensitive substring
      '!X7R'        negated any-of"""
    s = str(spec).strip()
    neg = s.startswith('!')
    if neg:
        s = s[1:].strip()
    if s.startswith('~'):
        needle = s[1:].lower()
        f = lambda v: needle in str(v or '').lower()
    else:
        m = re.fullmatch(r'(.+?)\.\.(.+)', s)
        if m and enum(m.group(1)) is not None and enum(m.group(2)) is not None:
            lo, hi = enum(m.group(1)), enum(m.group(2))
            def f(v, lo=lo, hi=hi):
                x = enum(v)
                return x is not None and lo * 0.999 <= x <= hi * 1.001
        else:
            m = re.fullmatch(r'(>=|<=|>|<|=)\s*(.+)', s)
            if m and enum(m.group(2)) is not None:
                op, lim = m.group(1), enum(m.group(2))
                def f(v, op=op, lim=lim):
                    x = enum(v)
                    if x is None:
                        return False
                    return {'>=': x >= lim * 0.999, '<=': x <= lim * 1.001,
                            '>': x > lim, '<': x < lim,
                            '=': abs(x - lim) <= abs(lim) * 1e-3}[op]
            else:
                alts = [t.strip() for t in s.split(',') if t.strip()]
                def f(v, alts=alts, whole=_norm(s) if len(alts) > 1 else None):
                    # _norm() drops the decimal point, so '2.2uF' and '22uF' collapse
                    # to the same string. Anything numeric MUST compare numerically and
                    # must not fall through to the text branch.
                    x = enum(v)
                    if whole and x is None and _norm(v) == whole:
                        return True     # a value with a comma in it: 'SMD,11.5x10mm'
                    for a in alts:
                        ax = enum(a)
                        if ax is not None:
                            if x is not None and abs(x - ax) <= abs(ax) * 1e-3:
                                return True
                            continue
                        if _norm(a) and _norm(a) == _norm(v):
                            return True
                    return False
    return ((lambda v: not f(v)) if neg else f), spec

# E6/E12 preferred values, used to fan a range out into per-value keyword queries.
E6 = [1.0, 1.5, 2.2, 3.3, 4.7, 6.8]
E12 = [1.0, 1.2, 1.5, 1.8, 2.2, 2.7, 3.3, 3.9, 4.7, 5.6, 6.8, 8.2]
# Voltage ratings MLCCs are actually made in. Used to turn '>=25' into query text.
STD_V = [2.5, 4, 6.3, 10, 16, 25, 35, 50, 63, 100, 200, 250, 500, 630, 1000]

def series_in(lo, hi, e12=False, cap=14):
    """Preferred values in [lo,hi]. Range searches need this: LCSC ranks by keyword
    relevance only, so one query for '4.7u..100u' would never surface the 22u parts."""
    if lo is None or hi is None or lo <= 0:
        return []
    out, dec = [], 10 ** math.floor(math.log10(lo))
    while dec <= hi * 10:
        for m in (E12 if e12 else E6):
            v = m * dec
            if lo * 0.999 <= v <= hi * 1.001:
                out.append(v)
        dec *= 10
    return out[:cap]
