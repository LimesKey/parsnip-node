"""part.py `pick` and `alt`: the candidate pool, constraints and the drop-in table."""
import sys, re, json
import concurrent.futures as cf
from part_core import (buy_qty, cached, dk_categories, dk_detail, _dk_norm, dk_query, dmoney1, _EOL_RE,
                       http, jlc_annotate, jlc_facets, jlc_lib_map, lcsc_detail, lcsc_search,
                       LCSC_SEARCH, price_at, resolve, trunc)
from part_value import (alias_keys, attr_hit, attr_of, canon, enum, _exact, FLAG_ATTRS, fmt_si,
                        make_pred, _norm, norm_val, series_in, _short, STD_V, _UNIT_OF)

def facet_name(facets, name):
    """The facet attribute `name` filters on, or None. Every catalog spelling of a
    shorthand counts and the most populated wins: LDOs carry JLC's 'standby
    current' on 7,187 parts and 'Quiescent Current' on 1; Power Inductors 'Current
    - Saturation (Isat)' on 14 and '...Saturation(Isat)' on 79k."""
    pop = lambda n: sum(c for v, c in facets[n].items() if v not in ('', '-'))   # noqa: E731
    ks = alias_keys(name)
    same = [n for n in facets if _norm(n) in ks] or \
        [n for n in facets if any(len(k) > 3 and _norm(n).startswith(k) for k in ks)]
    # listed values decide: DigiKey's 'Voltage - Output (Max)' is '-' on every fixed LDO
    pname = max(same, key=pop) if same else attr_hit([(n, '') for n in facets], name)[0]
    return max((n for n in facets if _norm(n) == _norm(pname)), key=pop) if pname else None

def _server_attrs(cons, facets):
    """(componentAttributeList, {constraint: parts meeting it alone}, [unmet],
    {constraint: facet name}, [not in the facets]). JLC matches attribute values
    exactly, so each constraint becomes the list of sidebar values the local
    predicate accepts - ranges, >= and any-of all run here, the server only
    intersects. A constraint whose attribute is not in the facets is left to the
    local filter (and named, so pick can say so); 'unmet' ones no listed value meets."""
    attrs, counts, unmet, res, missing = [], {}, [], {}, []
    for name, lab, pred in cons:
        if name == 'pkg':
            continue
        pname = facet_name(facets, name)
        if not pname:
            missing.append(name)
            continue
        res[name] = pname
        vals = [v for v in facets[pname] if v not in ('', '-') and pred(v)]
        counts[name] = sum(facets[pname][v] for v in vals)
        if vals:
            attrs.append({pname: vals})
        else:
            unmet.append(name)
    return attrs, counts, unmet, res, missing

def _near_names(facets, name, k=3):
    """facet names most like `name`, with part counts: the hint for a --w name the
    category does not have (otherwise every candidate silently fails it)"""
    import difflib
    n0, w0 = _norm(name), set(re.findall(r'[a-z]+', name.lower()))
    sc = sorted(((difflib.SequenceMatcher(None, n0, _norm(n)).ratio()
                  + .3 * bool(w0 & set(re.findall(r'[a-z]+', n.lower()))), n) for n in facets), reverse=True)
    return [(n, sum(facets[n].values())) for r, n in sc[:k] if r >= .5]

# sort direction by attribute name: these first, then alt's table ('ge' = bigger is better)
_SORT_RULES = (('le', r'noise|standby|quiescent|junction capacitance'),
               ('ge', r'rejection|psrr|^capacitance$|inductance'))

def sort_desc(name):
    """True when a bigger value is better for `name` (sort descending)."""
    rule = next((r for r, pat in _SORT_RULES + _ALT_RULES if re.search(pat, name.lower())), 'le')
    return rule == 'ge'

def _merit(r, name):
    x = enum(attr_of(r.get('params'), name))
    return None if x is None else abs(x)       # P-channel -30 V outranks -20 V

def _best_values(facets, pname, desc, pred=None):
    """[(value, parts)] of one facet attribute, best first, unparseable dropped"""
    vals = [(abs(enum(v)), v, c) for v, c in facets[pname].items()
            if v not in ('', '-') and enum(v) is not None and (pred is None or pred(v))]
    vals.sort(key=lambda t: -t[0] if desc else t[0])
    return [(v, c) for _x, v, c in vals]

# ================================================================ pick engine

def _cons_from_args(a):
    """[(attr, spec, pred)] from the convenience flags and every --w NAME=SPEC."""
    cons = []
    for name in FLAG_ATTRS:
        v = getattr(a, name, None)
        if v:
            p, lab = make_pred(v)
            cons.append((name, lab, p))
    for w in (a.w or []):
        if '=' not in w:
            print(f"  ignoring --w {w!r}: expected NAME=SPEC", file=sys.stderr)
            continue
        k, v = w.split('=', 1)
        p, lab = make_pred(v)
        cons.append((k.strip(), lab, p))
    return cons

def _keywords(a, cons):
    """Base keyword plus a fan-out over preferred values when a range was given."""
    base = ' '.join(a.args).strip()
    exp = []
    for name, lab, _ in cons:
        if name not in ('cap', 'res', 'ind'):
            continue
        m = re.fullmatch(r'(.+?)\.\.(.+)', str(lab).strip())
        if not m:
            continue
        vals = series_in(enum(m.group(1)), enum(m.group(2)), a.e12, a.maxq)
        exp = [fmt_si(v, _UNIT_OF.get(name, '')) for v in vals]
        break
    if not exp:                       # single value? still worth putting in the query
        for name, lab, _ in cons:
            if name in ('cap', 'res', 'ind') and enum(lab) is not None:
                exp = [fmt_si(enum(lab), _UNIT_OF.get(name, ''))]
                break
    # Second axis. Without this, a '>=25V' filter is applied only AFTER detailing, and
    # the pool fills with the cheap 6.3/10/16 V parts that LCSC ranks first - they all
    # then fail the filter and the result is empty. Putting the voltage in the query
    # text makes the pool passable in the first place.
    ax2 = ['']
    volt = next((lab for n, lab, _ in cons if n == 'volt'), None)
    if volt:
        m = re.fullmatch(r'>=?\s*(.+)', str(volt).strip())
        rng = re.fullmatch(r'(.+?)\.\.(.+)', str(volt).strip())
        if m and enum(m.group(1)) is not None:
            lo = enum(m.group(1))
            ax2 = [f"{int(v) if v == int(v) else v}V" for v in STD_V if lo * 0.999 <= v <= lo * 4]
        elif rng and enum(rng.group(1)) is not None:
            lo, hi = enum(rng.group(1)), enum(rng.group(2))
            ax2 = [f"{int(v) if v == int(v) else v}V" for v in STD_V if lo * 0.999 <= v <= hi * 1.001]
        elif enum(volt) is not None:
            ax2 = [f"{fmt_si(enum(volt), 'V')}"]
        ax2 = ax2[:4] or ['']
    diel = next((lab for n, lab, _ in cons if n == 'diel'), '')
    d1 = str(diel).split(',')[0].strip() if diel and not str(diel).startswith(('~', '!')) else ''
    pkg = next((lab for n, lab, _ in cons if n == 'pkg' and ',' not in str(lab)), '')
    tail = ' '.join(x for x in (base, str(pkg) if pkg else '', d1) if x).strip()
    kws = [f"{v} {w} {tail}".strip().replace('  ', ' ')
           for v in (exp or ['']) for w in ax2]
    kws = [k for k in dict.fromkeys(kws) if k]
    kws = kws[:a.maxq] or ([tail] if tail else [])
    # JLC's matcher is far stricter than LCSC's: a free-text token like "MLCC" that
    # is not in its index zeroes the whole query, especially with the base-library
    # filter on. Give it bare values only; its Basic library is small enough that
    # one query per value is complete coverage anyway.
    # An exact package goes to JLC as a server-side filter, not a keyword token;
    # then an empty keyword is valid (category/package alone define the pool).
    # A JLC category filter replaces the free text, which would only AND-narrow it.
    pl, jcat = _pkg_literal(cons), getattr(a, '_jcat', None)
    jtail = ' '.join(x for x in ('' if jcat else base, d1) if x) if pl or jcat else tail
    jkws = list(dict.fromkeys(exp)) if exp else ([jtail] if jtail or pl or jcat else [])
    if pkg and exp and not pl:
        jkws = [f"{v} {pkg}" for v in exp]
    return kws, (jkws[:a.maxq] or kws)

# lighting (not indicator) LEDs: a class neither catalogue has a category for
LIGHT_LED = re.compile(r'(?=.*\bLEDs?\b)(?=.*\b(white|warm|cool|neutral|lighting|illumination|'
                       r'high[- ]?power|mid[- ]?power|\d{4}\s?K|CRI|lumens?|lm)\b)', re.I)

def categories(fresh=False):
    """LCSC/JLC category names (the names JLC's category filter takes), from the
    'Category' facet of a few broad LCSC searches: ~475 names, cached 7 days."""
    def go():
        names = set()
        for kw in ('1', 'SMD', 'IC', 'resistor capacitor diode transistor connector'):
            d = http(LCSC_SEARCH, data={'keyword': kw, 'needAggs': 'true', 'currPage': 1,
                                        'pageSize': 1}, headers={'Referer': 'https://easyeda.com/'})
            for f in ((d or {}).get('result') or {}).get('paramList') or []:
                if f.get('parameterName') == 'Category':
                    names |= set(f.get('parameterValueList') or [])
        return sorted(names) or None
    return cached('lcsccats', 'v1', go, fresh, ttl=7 * 24 * 3600) or []

def _cat_hits(text, cats):
    """Category names containing `text` as whole words, plural allowed ('diode' ->
    'Schottky Diodes', 'tvs' -> '...(TVS/ESD)', but 'led' never -> 'Leaded')."""
    rx = re.compile(r'\b' + r'\W+'.join(map(re.escape, text.lower().split())) + r'\w{0,2}\b')
    return [c for c in cats if rx.search(c.lower())]

# application words -> JLC categories, first preferred. Word overlap alone sent
# 'load switch' to 'Force Sensors, Load Cells' and 'current limit switch' to the
# mechanical 'Limit Switches'. Checked against real parts 2026-10-02: TPS2553/
# TPS22945/LM66100 Power Distribution Switches, TPS25947 Surge Protection Devices,
# LM74700/MAX40200 ORing Controllers, BQ25798/MAX17320/TP4056 Battery Management,
# LM61460/TPS61033 DC-DC Converters, TXS0108E Translators, BGA725 RF Amplifiers.
APP_CATS = {
    'load switch': ['Power Distribution Switches'],
    'power switch': ['Power Distribution Switches'],
    'high side switch': ['Power Distribution Switches'],
    'current limit switch': ['Power Distribution Switches'],
    'current limited switch': ['Power Distribution Switches'],
    'efuse': ['Surge Protection Devices (SPDs)', 'Power Distribution Switches'],
    'e-fuse': ['Surge Protection Devices (SPDs)', 'Power Distribution Switches'],
    'hot swap': ['Surge Protection Devices (SPDs)', 'Power Distribution Switches'],
    'ideal diode': ['ORing Controllers', 'Power Distribution Switches'],
    'buck': ['DC-DC Converters'], 'boost': ['DC-DC Converters'], 'buck-boost': ['DC-DC Converters'],
    'buck boost': ['DC-DC Converters'], 'dc-dc': ['DC-DC Converters'], 'dcdc': ['DC-DC Converters'],
    'switching regulator': ['DC-DC Converters'],
    'buck controller': ['DC-DC Controllers'], 'boost controller': ['DC-DC Controllers'],
    'charger': ['Battery Management'], 'battery charger': ['Battery Management'],
    'fuel gauge': ['Battery Management'], 'bms': ['Battery Management'],
    'level shifter': ['Translators, Level Shifters'], 'level translator': ['Translators, Level Shifters'],
    'lna': ['RF Amplifiers', 'Low Noise Amplifiers (LNA) - RF'],
    'low noise amplifier': ['RF Amplifiers', 'Low Noise Amplifiers (LNA) - RF'],
    'saw': ['SAW Filters'], 'saw filter': ['SAW Filters'],
    'supervisor': ['Supervisor and Reset ICs'], 'reset ic': ['Supervisor and Reset ICs'],
    'voltage detector': ['Supervisor and Reset ICs'], 'voltage monitor': ['Supervisor and Reset ICs'],
    'current sense amplifier': ['Current Sense Amplifiers'], 'current monitor': ['Current Sense Amplifiers'],
}

def _app_cats(text):
    """APP_CATS entry for the longest application phrase in `text`, or []"""
    t = ' ' + re.sub(r'\s+', ' ', text.lower()) + ' '
    hit = max((k for k in APP_CATS if re.search(r'(?<![\w-])' + re.escape(k) + r'(?![\w-])', t)),
              key=len, default=None)
    return APP_CATS[hit] if hit else []

def _pick_category(a):
    """JLC category for the pool: --cat (unique substring match, else verbatim), or
    inferred from the pick keyword when its phrase, or every word of it that names
    exactly one category, points at one name. JLC's keyword matcher is AND-over-
    tokens and skips category names ('TVS' + SMA -> 0 of 2802 SMA parts), so a
    category filter is the only complete pool for a part type."""
    if a.cat:
        cats = categories(a.fresh)
        hits = _cat_hits(a.cat, cats)
        exact = [c for c in hits if c.lower() == a.cat.lower()]
        if len(exact or hits) > 1:
            sys.exit(f"--cat {a.cat!r} matches {len(hits)} categories:\n  " + '\n  '.join(hits[:20]))
        if cats and not hits:          # sent verbatim, JLC returns 0 and pick falls to LCSC
            import difflib
            near = difflib.get_close_matches(a.cat, cats, 5, 0.5)
            sys.exit(f"--cat {a.cat!r} is no JLC category"
                     + (":  did you mean " + ', '.join(repr(c) for c in near) if near else ''))
        return (exact or hits or [a.cat])[0]
    text = ' '.join(a.args).strip()
    if not text:
        return None
    cats = categories(a.fresh)
    app = _app_cats(text)
    whole = app or _cat_hits(text, cats)
    a._catamb = whole                  # kept for c_pick's hint when none is chosen
    if len(whole) == 1:
        return whole[0]
    # several: the one that IS the keyword ('MOSFET' -> MOSFETs, not the SiC
    # one), else the only one that has --pkg at all (MLCC 0805 -> SMD, not Leaded)
    same = [c for c in whole if c.lower().rstrip('s') == text.lower().rstrip('s')]
    if len(same) == 1:
        return same[0]
    pkg = _one_pkg(a.pkg)
    if pkg and 1 < len(whole) <= 6:
        stocked = {c: f for c in whole for f in [jlc_facets(c, pkg, a.fresh)] if f}
        if len(stocked) == 1:
            return next(iter(stocked))
        # several stock it ('resistor' 0402: chip, shunt, array): take the dominant
        # one by facet part count. Multi-valued attributes inflate the sums, but
        # alike in every category, so only the ratio is trusted.
        size = sorted(((max(sum(v.values()) for v in f.values()), c) for c, f in stocked.items()),
                      reverse=True)
        if len(size) > 1 and size[0][0] >= 10 * size[1][0]:
            return size[0][1]
    if app:
        return app[0]                  # the table's preferred one; the header names it
    uniq = {h[0] for w in text.split() if len(w) >= 3 for h in [_cat_hits(w, cats)] if len(h) == 1}
    return uniq.pop() if len(uniq) == 1 else None

def _one_pkg(s):
    """s when it names one exact package, else None. A comma normally means any-of
    ('0402,0603'), but JLC names sized parts 'SMD,11.5x10mm' / 'Plugin,P=5mm':
    WORD,<something with a size> is one name."""
    if not s or re.search(r'[~!<>]|\.\.', s):
        return None
    return s if ',' not in s or re.fullmatch(r'[A-Za-z]+,[^,]*(\d[xX*]|mm|=)[^,]*', s) else None

def _pkg_literal(cons):
    """The --pkg value when it names one exact package (JLC filters that server-side)."""
    return _one_pkg(next((str(lab) for n, lab, _ in cons if n == 'pkg'), ''))

def _stock_ok(rows, a):
    """Rows with stock >= --minstock; counts the rest for the pick header."""
    keep = [r for r in rows if (r.get('stock') or 0) >= a.minstock]
    a._lowstock = getattr(a, '_lowstock', 0) + len(rows) - len(keep)
    return keep

def _lcsc_reprice(recs, a):
    """JLC is the better index, LCSC retail is the price of record: swap LCSC's
    ladder/stock/MOQ/lifecycle into JLC-pooled rows, one cached detail call each.
    A row LCSC cannot detail keeps source 'JLC' and is labelled in the table."""
    todo = [r for r in recs if r.get('source') == 'JLC']
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        for r, d in zip(todo, ex.map(lambda r: lcsc_detail(r['sku'], a.fresh), todo)):
            if d:
                r.update({k: d.get(k) for k in ('source', 'mfr', 'ladder', 'stock', 'moq',
                                                  'multiple', 'currency', 'lifecycle', 'url',
                                                  'datasheet_candidates')})
    return recs

def build_pool(a, cons, keywords, jkeywords=None):
    """Candidates with parameters. jlc (default): JLC's ~7.2M-part index with
    server-side package/category/stock filters and a price sort, attributed, one
    call per 200 rows. lcsc: LCSC keyword search (relevance on MPN text) + one
    detail call per part. Each falls back to the other when it finds nothing;
    `both` unions them. Measured 2026-09-23 on MOSFET/MLCC/schottky/LDO picks:
    jlc was cheaper-or-equal on all four and 4x faster cold."""
    if a.source == 'digikey':
        return dk_pool(a, cons)
    note, jkeywords = [], jkeywords or keywords
    # the server sort is the order pages walk in: price (any other sort ranks the
    # pool locally) or stock. A relevance page was 200 of 523 matching LDOs.
    jkw = dict(pkg=_pkg_literal(cons), category=getattr(a, '_jcat', None),
               cheapest='stock' if a.sort == 'stock' else True, instock=a.minstock > 0, budget=a.maxq)
    exact = []                          # set when JLC filtered on the attributes

    def jlc(library=None):
        # with a category, every constraint the sidebar can express goes to JLC as
        # an exact value list, so the pool IS the matching parts, cheapest first,
        # instead of the cheapest 600 of the category filtered afterwards
        attrs, kws, best, sp = None, jkeywords, None, None
        if jkw['category'] and not getattr(a, '_noattr', False):
            fac = jlc_facets(jkw['category'], jkw['pkg'], a.fresh)
            if fac:
                attrs, a._facet_counts, unmet, a._resolved, miss = _server_attrs(cons, fac)
                a._missing = {m: _near_names(fac, m) for m in miss}
                exact.append(True)
                sp = facet_name(fac, a._sortattr) if getattr(a, '_sortattr', None) else None
                a._coverage = _coverage(fac, list(a._resolved.items()) + ([(a._sortattr, sp)] if sp else []),
                                        _jlc_total(jkw, a))
                if unmet:
                    note.append(f"no {' / '.join(unmet)} value in '{jkw['category']}'"
                                f"{' ' + jkw['pkg'] if jkw['pkg'] else ''} meets the limit")
                    return []
                if sp:     # an attribute sort: send only its best values, so the pool is the true top
                    a._sortp = sp
                    best = _best_values(fac, sp, a._desc,
                                        next((p for n, _l, p in cons if a._resolved.get(n) == sp), None))
                kws = [''] if attrs or best else kws
        fetch = lambda at: list(jlc_lib_map(kws, a.fresh, library=library, attrs=at,     # noqa: E731
                                            **jkw).values())
        if best:
            target = 400
            while True:            # widen until the other limits leave enough rows
                pre, cum = [], 0
                for v, c in best:
                    pre.append(v)
                    cum += c
                    if cum >= target:
                        break
                rows = fetch([x for x in (attrs or []) if sp not in x] + [{sp: pre}])
                if len(pre) == len(best) or sum((r.get('stock') or 0) >= a.minstock for r in rows) >= 3 * a.n:
                    break
                target *= 4
            note.append(f"best-first on {sp}: {len(pre)} of {len(best)} values")
        else:
            rows = fetch(attrs)
        rows = _stock_ok(rows, a)
        note.append(f"jlc {'base library ' if library else ''}{len(rows)}"
                    + (f" in '{jkw['category']}'" if jkw['category'] else '')
                    + (f", {len(attrs)} limit(s) server-side" if attrs else ''))
        return rows

    def lcsc():
        # search rows carry no parameters, so the cheapest --pool in-stock hits are
        # fetched by product/detail one at a time (cached, parallel)
        seen = {}
        for kw in keywords:
            try:
                rows, _ = lcsc_search(kw, 200, a.fresh)
            except Exception as e:
                note.append(f"lcsc '{kw}' failed: {str(e)[:40]}")
                continue
            for r in rows:
                seen.setdefault(r['sku'], r)
        cands = _stock_ok(list(seen.values()), a)
        pkg = next((p for n, _, p in cons if n == 'pkg'), None)
        if pkg:                       # free prefilter: search rows carry `package`
            cands = [c for c in cands if pkg(c.get('package'))]
        # cheapest-first before truncation, so the pool cap never hides a low price
        cands.sort(key=lambda c: min((p for _, p in (c.get('ladder') or [])),
                                     default=float('inf')))
        cands = cands[:a.pool]
        note.append(f"lcsc {len(seen)} hits -> {len(cands)} detailed")
        with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
            return [r for r in ex.map(lambda c: lcsc_detail(c['sku'], a.fresh), cands) if r]

    if a.basic:
        # "Basic" is a JLC library concept; LCSC has no such field and no way to filter
        # on it. Pool straight from the base library: server-side filter, ~1 call per
        # keyword, fully attributed, and it is authoritative rather than inferred.
        return jlc('base'), '; '.join(note)
    first, second = (lcsc, jlc) if a.source == 'lcsc' else (jlc, lcsc)
    recs = first()
    if exact and not recs and a.source != 'both':
        return recs, '; '.join(note)    # JLC's own count says none exist: LCSC can't beat it
    if a.source == 'both' or not recs:
        if a.source != 'both':
            note.append(f"{first.__name__} empty, fell back to {second.__name__}")
        have = {r['sku'] for r in recs}
        recs += [r for r in second() if r['sku'] not in have]
    return recs, '; '.join(note)

def _jlc_total(jkw, a):
    """parts in the category (+ package), any stock: the coverage denominator"""
    from part_core import jlc_search
    try:
        return jlc_search('', 1, a.fresh, pkg=jkw['pkg'], category=jkw['category'])[1]
    except Exception:
        return 0

def _coverage(fac, named, tot):
    """[(name, facet name, listed, '-', total)] for each attribute listed on under
    85% of the category (+ package): a part that does not list it fails a limit
    on it and sorts last, which silently shrinks the pool (LDO SOT-23-5 Noise:
    '-' on 2,579 of 7,268, absent on ~1,700 more)."""
    out = []
    for name, pn in named:
        v = fac.get(pn) or {}
        dash = v.get('-', 0) + v.get('', 0)
        listed = sum(v.values()) - dash
        if tot and listed < .85 * tot:
            out.append((name, pn, listed, dash, tot))
    return out

def _dk_category(a):
    """(DigiKey leaf category id, name) to pool from, or (None, why): --cat against
    DigiKey's names, the --like part's own category, the JLC category when DigiKey
    has one of that name, else the commonest leaf among keyword hits."""
    cats = dk_categories(a)
    if not cats:
        return None, 'no DigiKey credentials or category list (see endpoints.md)'
    if a.cat:
        hits = _cat_hits(a.cat, list(cats))
        hits = [c for c in hits if c.lower() == a.cat.lower()] or hits
        return (cats[hits[0]][0], hits[0]) if len(hits) == 1 else \
            (None, f"--cat {a.cat!r} matches {len(hits)} DigiKey categories: " + ', '.join(hits[:8]))
    if getattr(a, '_like_mpn', None):
        r = dk_detail(a._like_mpn, a)
        leaf = (r or {}).get('category', '').split(' > ')[-1]
        if leaf in cats:
            return cats[leaf][0], leaf
    jc = getattr(a, '_jcat', None)
    if jc in cats:
        return cats[jc][0], jc
    text = ' '.join(x for x in a.args if x).strip()
    if not text:
        return None, 'give a keyword or --cat (DigiKey category names differ from JLC\'s)'
    from collections import Counter
    d = dk_query({'Keywords': text, 'Limit': 50, 'Offset': 0}, a)
    leaves = Counter(_dk_norm(p, a.currency)['category'].split(' > ')[-1] for p in d.get('Products') or [])
    leaf = next((c for c, _n in leaves.most_common() if c in cats), None)
    return (cats[leaf][0], leaf) if leaf else (None, f"no DigiKey category found for {text!r}; give --cat")

def dk_pool(a, cons):
    """pick's pool from DigiKey's v4 KeywordSearch: category, package, every limit
    and an attribute sort's best values go server-side as parameter value ids (the
    same predicate-over-facet-values as JLC's sidebar), price or stock order, 50
    rows a call. Prices are DigiKey's (cut tape) in --currency."""
    cid, cname = _dk_category(a)
    if not cid:
        return [], f"digikey: {cname}"
    flt = {'CategoryFilter': [{'Id': str(cid)}], 'MinimumQuantityAvailable': a.minstock}
    if a.minstock > 0:
        flt['SearchOptions'] = ['InStock']
    d = dk_query({'Keywords': '', 'Limit': 1, 'Offset': 0, 'FilterOptionsRequest': flt}, a)
    if 'FilterOptions' not in d:
        return [], f"digikey: {trunc(d.get('detail') or d.get('_error') or d, 90)}"
    pf = d['FilterOptions'].get('ParametricFilters') or []
    fac = {f['ParameterName']: {v['ValueName']: v.get('ProductCount') or 0 for v in f.get('FilterValues') or []}
           for f in pf}
    ids = {f['ParameterName']: (f['ParameterId'], {v['ValueName']: v['ValueId'] for v in f.get('FilterValues') or []})
           for f in pf}
    attrs, a._facet_counts, unmet, a._resolved, miss = _server_attrs(cons, fac)
    a._missing, a._jcat = {m: _near_names(fac, m) for m in miss}, cname
    pk = next((p for n, _l, p in cons if n == 'pkg'), None)
    if pk:              # DigiKey files the case under two names; LCSC's string matches the first
        hit = next(((pn, v) for pn in ('Supplier Device Package', 'Package / Case')
                    for v in [[x for x in fac.get(pn, {}) if pk(x)]] if v), None)
        if hit:
            attrs.append({hit[0]: hit[1]})
            a._resolved['pkg'] = hit[0]
        else:
            unmet.append('pkg')
    sp = facet_name(fac, a._sortattr) if getattr(a, '_sortattr', None) else None
    a._coverage = _coverage(fac, list(a._resolved.items()) + ([(a._sortattr, sp)] if sp else []),
                            d.get('ProductsCount') or 0)
    note = f"digikey '{cname}' ({cid})"
    if unmet:
        return [], note + f": no {' / '.join(unmet)} value meets the limit"
    order = {'Field': 'QuantityAvailable', 'SortOrder': 'Descending'} if a.sort == 'stock' else \
        {'Field': 'Price', 'SortOrder': 'Ascending'}

    def fetch(at):
        f = dict(flt, ParameterFilterRequest={'CategoryFilter': {'Id': str(cid)}, 'ParameterFilters': [
            {'ParameterId': ids[n][0], 'FilterValues': [{'Id': ids[n][1][v]} for v in vals]}
            for x in at for n, vals in x.items()]})
        out, tot = [], 0
        for k in range(min(a.maxq, 8)):      # 50 a call; DigiKey allows 1,000 calls a day
            r = dk_query({'Keywords': '', 'Limit': 50, 'Offset': 50 * k, 'FilterOptionsRequest': f,
                          'SortOptions': order}, a)
            out += [_dk_norm(p, a.currency) for p in r.get('Products') or []]
            tot = r.get('ProductsCount') or tot
            if len(r.get('Products') or []) < 50 or len(out) >= tot:
                break
        return out, tot
    if sp:             # best values first, widened until the other limits leave enough rows
        a._sortp = sp
        best = _best_values(fac, sp, a._desc, next((p for n, _l, p in cons if a._resolved.get(n) == sp), None))
        target = 200
        while True:
            pre, cum = [], 0
            for v, c in best:
                pre.append(v)
                cum += c
                if cum >= target:
                    break
            recs, tot = fetch([x for x in attrs if sp not in x] + [{sp: pre}])
            if len(pre) == len(best) or len(recs) >= 3 * a.n:
                break
            target *= 4
        note += f", best-first on {sp}: {len(pre)} of {len(best)} values"
    else:
        recs, tot = fetch(attrs)
    return _stock_ok(recs, a), note + f": {len(recs)} of {tot}, {len(attrs)} limit(s) server-side"

def apply_cons(recs, cons):
    """(passing recs, {constraint: candidates failing it}, near misses). Every
    failure counts, so one candidate can add to several constraints; a near miss
    fails exactly one: [(rec, constraint, spec, its value or None)]."""
    out, why, near = [], {}, []
    for r in recs:
        bad = []
        for name, lab, pred in cons:
            v = attr_of(r.get('params'), name)
            if name == 'pkg' and v is None:
                v = r.get('package')
            if v is None or not pred(v):
                bad.append((name, lab, v))
                why[name] = why.get(name, 0) + 1
        if not bad:
            out.append(r)
        elif len(bad) == 1:
            near.append((r,) + bad[0])
    return out, why, near

def _like(a):
    """--like C..: category and package from an exemplar, limits from the user, so
    'something like TPS2553 but lower RDS(on)' needs no category name and none of
    alt's equal-or-better holds. `--pkg '~'` drops the package."""
    b = (resolve(a.like, a) or [None])[0]
    if not b:
        sys.exit(f"pick --like: {a.like} not found")
    jlc_annotate([b], a.jobs, a.fresh)
    a._jcat = b.get('jcat') or (b.get('category') if b['source'] == 'JLC' else None)
    if not a._jcat:
        sys.exit(f"pick --like: {b.get('sku')} is in no JLC category; give --cat")
    a.pkg = a.pkg or b.get('package')
    a._like_mpn = b.get('mpn')
    if a.source != 'digikey':
        a.source = 'jlc'
    a._exclude = {b.get('sku')}
    print(f"like {b.get('sku')} {b.get('mpn')} ({b.get('mfr')}): '{a._jcat}', package {a.pkg}")
    if not a.args:
        a.args = ['']

def _sortkey(a, unit):
    if a.sort == 'price':
        return lambda r: (unit(r) is None, unit(r) or 0)
    if a.sort == 'stock':
        return lambda r: -(r.get('stock') or 0)
    name = getattr(a, '_sortp', None) or a._sortattr
    return lambda r: (_merit(r, name) is None, -(_merit(r, name) or 0) if a._desc else (_merit(r, name) or 0),
                      unit(r) is None, unit(r) or 0)

def _pareto(rows, unit, name, desc):
    """rows no other row beats on both price and `name`, cheapest first"""
    out, top = [], None
    for r in sorted((r for r in rows if unit(r) is not None and _merit(r, name) is not None),
                    key=lambda r: (unit(r), -_merit(r, name) if desc else _merit(r, name))):
        m = _merit(r, name)
        if top is None or (m > top if desc else m < top):
            out.append(r)
            top = m
    return out

def spec_table(recs, a):
    """`compare`'s table: one row per quantity, not per spelling (LCSC 'Supply
    Current (Iq)', JLC 'standby current' and DigiKey 'Current - Quiescent (Iq)'
    share a row), values in SI with their test condition, LCSC/JLC/DigiKey records
    side by side. Differing rows only unless --attrs."""
    keys, label = [], {}
    for r in recs:
        for k, _ in (r.get('params') or []):
            if k and canon(k) not in label:
                label[canon(k)] = k
                keys.append(canon(k))
    w = max(18, min(30, max((len(str(label[k])) for k in keys), default=18)))
    print(f"{'':<{w}} " + ' '.join(f"{trunc(r.get('mpn'), 22):<24}" for r in recs))
    def row(lab, vals):
        print(f"{trunc(lab, w):<{w}} " + ' '.join(f"{trunc(v, 23):<24}" for v in vals))
    row('source/sku', [f"{r['source']} {r.get('sku')}" for r in recs])
    row('manufacturer', [r.get('mfr') for r in recs])
    row('package', [r.get('package') for r in recs])
    row('stock', [f"{r.get('stock'):,}" if isinstance(r.get('stock'), int) else r.get('stock') for r in recs])
    q = a.qty or 1
    row(f'unit @{q}', [dmoney1(price_at(r.get('ladder') or [], buy_qty(q, r.get('moq'), r.get('multiple'))),
                               r.get('currency') or 'USD', a) for r in recs])
    row('moq / mult', [f"{r.get('moq')} / {r.get('multiple')}" for r in recs])
    row('lifecycle', [r.get('lifecycle') for r in recs])
    print()
    def by_canon(r):           # two spellings, one row: a listed value beats a '-'
        d = {}
        for kk, vv in r.get('params') or []:
            if d.get(canon(kk)) in (None, '', '-'):
                d[canon(kk)] = vv
        return d
    cv = [by_canon(r) for r in recs]
    for k in keys:
        vals = [norm_val(d.get(k, '-')) for d in cv]
        if len({str(v) for v in vals}) > 1 or a.attrs:
            row(label[k], vals)
    print("\n(" + ('every parameter' if a.attrs else 'only differing parameters shown; --attrs for all')
          + "; values in SI units with their test condition after @)")

def _rname(a, c):
    """the attribute a column or limit was filtered on (the server's facet name)"""
    return (getattr(a, '_resolved', None) or {}).get(c, c)

def _cnum(sku):
    return int(sku[1:]) if re.fullmatch(r'C\d+', sku or '') else float('inf')

def _cores(mpn):
    """the MPN with one maker's affix cut ('TLV74333PDBVR-TP' -> 'TLV74333PDBVR'),
    the strings to search for the part it copies"""
    m = (mpn or '').upper().strip()
    out = [re.sub(r'[-_/#][A-Z0-9]{1,4}$', '', m), re.sub(r'^[A-Z]{1,4}[-_]', '', m)]
    return [c for c in dict.fromkeys(out) if c != m and len(c) >= 5]

def clone_of(row, cands):
    """The part `row` likely copies: another maker's MPN that the row's MPN strictly
    contains, listed on LCSC earlier (a lower C-number). Equal MPNs are second
    sources (BAT54S, AMS1117), not tagged; the C-number keeps Torex's own
    XC6206P332MR-G from reading as a copy of a later XC6206P332MR."""
    mpn, n, best = (row.get('mpn') or '').upper(), _cnum(row.get('sku')), None
    for c in cands:
        cm = (c.get('mpn') or '').upper()
        if len(cm) < 5 or cm == mpn or cm not in mpn or _cnum(c.get('sku')) >= n \
                or (c.get('mfr') or '').lower() == (row.get('mfr') or '').lower():
            continue
        if best is None or (len(cm), -_cnum(c.get('sku'))) > (len(best['mpn']), -_cnum(best.get('sku'))):
            best = c
    return best

def _tag_clones(rows, a):
    """row['_clone'] = the original's record: a clone's
    best-in-class figure (800 nA Iq vs TI's 34 uA) is the least trustworthy number
    in a merit ranking. One cached LCSC search per affix of each shown row."""
    def one(r):
        cands = [c for core in _cores(r.get('mpn')) for c in lcsc_search(core, 10, a.fresh)[0]]
        o = clone_of(r, cands) if cands else None
        if o:
            r['_clone'] = lcsc_detail(o['sku'], a.fresh) or o
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        list(ex.map(one, [r for r in rows if re.fullmatch(r'C\d+', r.get('sku') or '')]))

def c_pick(a):
    if getattr(a, 'xcheck', False) and a.source != 'digikey':
        import argparse
        a._xbase = argparse.Namespace(**dict({k: v for k, v in vars(a).items() if not k.startswith('_')},
                                             source='digikey', xcheck=False, w=list(a.w)))
    if getattr(a, 'like', None):
        _like(a)
    cons = _cons_from_args(a)
    if not getattr(a, '_jcat', None) and (a.cat or a.source != 'lcsc'):
        a._jcat = _pick_category(a)
    kws, jkws = _keywords(a, cons)
    if a.source == 'digikey':
        a.nojlc = True                 # JLC's Basic/Extended says nothing about a DigiKey row
    if not (kws or jkws or a.source == 'digikey'):
        print("pick: give a keyword and/or at least one constraint, e.g.\n"
              "  part.py pick MLCC --cap 4.7u..100u --volt '>=25' --pkg 0805 --diel X7R")
        return 1
    par = getattr(a, 'pareto', None)
    if par:
        a.sort = par
    if a.sort not in ('price', 'stock'):
        from part_value import ALIAS
        a._sortattr = a.sort
        a._desc = sort_desc(ALIAS.get(a.sort.lower(), [a.sort.lower()])[0]) ^ bool(getattr(a, 'desc', False))
    recs, note = build_pool(a, cons, kws, jkws)
    if par:                            # the best-by-attribute pool + the cheapest pool
        a.sort, sa, a._sortattr = 'price', a._sortattr, None
        more, note2 = build_pool(a, cons, kws, jkws)
        a._sortattr = sa
        have = {r['sku'] for r in recs}
        recs += [r for r in more if r['sku'] not in have]
        note += '; ' + note2
    if a.fields:                                  # cheap discovery pass
        names = {}
        for r in recs:
            for k, v in (r.get('params') or []):
                names.setdefault(k, {})
                names[k][v] = names[k].get(v, 0) + 1
        print(f"attributes across {len(recs)} candidates  [{note}]\n")
        top18 = sorted(names.items(), key=lambda kv: -sum(kv[1].values()))[:18]
        w = min(48, max((len(k) for k, _ in top18), default=20))   # full names: they are --w input
        for k, vals in top18:
            top = sorted(vals.items(), key=lambda x: -x[1])[:8]
            print(f"  {trunc(k,w):<{w}} {trunc(', '.join(v for v,_ in top), 80)}")
        return 0
    # filter on the facet attribute the server used: a row listing two spellings
    # must be judged on the same one
    fcons = [((getattr(a, '_resolved', None) or {}).get(n, n), lab, p) for n, lab, p in cons]
    hits, why, near = apply_cons(recs, fcons)
    excl = getattr(a, '_exclude', None) or set()
    selfhit = [h for h in hits if h.get('sku') in excl]
    hits = [h for h in hits if h.get('sku') not in excl]
    if a.basic:
        hits = [h for h in hits if h.get('library') == 'base']
    qty = a.qty or 100
    def unit(r):
        q = buy_qty(qty, r.get('moq'), r.get('multiple'))
        return price_at(r.get('ladder') or [], q)
    pname = getattr(a, '_sortp', None) or getattr(a, '_sortattr', None)
    if par:
        hits = _pareto(hits, unit, pname, a._desc)
    keyf = _sortkey(a, unit) if not par else (lambda r: (unit(r) is None, unit(r) or 0))
    hits.sort(key=keyf)
    eol = 0
    if not a.basic:               # --basic quotes the JLC assembly catalog on purpose
        hits = _stock_ok(_lcsc_reprice(hits[:max(3 * a.n, 24)], a), a)
        eol = sum(1 for h in hits if _EOL_RE.search(h.get('lifecycle') or ''))
        hits = [h for h in hits if not _EOL_RE.search(h.get('lifecycle') or '')]
        if par:                   # LCSC retail can reorder the JLC-priced front
            hits = _pareto(hits, unit, pname, a._desc)
        hits.sort(key=keyf)
    hits = hits[:a.n]
    _tag_clones(hits, a)
    if not a.nojlc:
        jlc_annotate(hits, a.jobs, a.fresh)
    if a.json:
        print(json.dumps(hits, indent=1)); return 0 if hits else 1
    cols = [n for n, _, _ in cons if n != 'pkg'][:4]
    if pname and all(_norm(c) != _norm(pname) and getattr(a, '_resolved', {}).get(c) != pname for c in cols):
        cols = ([pname] + cols)[:4]
    if not cols:
        # no --cap/--volt/--diel-style constraint was given (e.g. a --basic whole-
        # library dump), so there's nothing in `cons` to build columns from. Fall
        # back to whatever attributes are actually most common across the result
        # set, the same way `--fields` picks what to show - always emit resolved
        # attribute columns for detailed rows rather than leaving the table bare.
        freq = {}
        for r in (hits or recs)[:60]:
            for k, _ in (r.get('params') or []):
                freq[k] = freq.get(k, 0) + 1
        cols = [k for k, _ in sorted(freq.items(), key=lambda kv: -kv[1])[:4]]
    sw = max([11] + [len(r.get('sku') or '') for r in hits])     # DigiKey numbers run to 26
    hdr = (f"{'sku':<{sw}} {'mpn':<22} {'mfr':<14} {'pkg':<8} "
           + ''.join(f"{trunc(_short(c),8):<9}" for c in cols)
           + f"{'stock':>9}  {'unit@'+str(qty):<12} {'ext':<10} jlc")
    shown = jkws if a.basic or a.source == 'jlc' else kws
    drop = ([f"{a._lowstock} below --minstock {a.minstock}"] if getattr(a, '_lowstock', 0) else []) \
        + ([f"{eol} EOL/NRND"] if eol else [])
    print(f"pick: {' | '.join(k or '(category/package only)' for k in shown[:3])}"
          f"{' ...' if len(shown)>3 else ''}   [{note}; {len(hits)} shown"
          + (f"; dropped {', '.join(drop)}" if drop else '') + "]")
    if pname:
        print(f"  {'pareto' if par else 'sorted'}: {pname}, {'highest' if a._desc else 'lowest'} best"
              + (" (rows no other row beats on both price and it)" if par else '')
              + "; --desc flips")
    for name, near_ in (getattr(a, '_missing', None) or {}).items():
        print(f"  no attribute {name!r} in '{a._jcat}': it filters locally, where most parts don't list "
              f"it and fail" + (": did you mean " + ', '.join(f"{n!r} ({c:,} parts)" for n, c in near_)
                                if near_ else ''))
    cov = getattr(a, '_coverage', None) or []
    if cov:            # one line: alt holds a dozen attributes
        print(f"  coverage: of {cov[0][4]:,} parts in the category, only "
              + ', '.join(f"~{100 * listed // tot}% list {pn}" for _n, pn, listed, _d, tot in cov)
              + "; the rest can't pass a limit on it and sort last")
    res = {k: {v} for k, v in (getattr(a, '_resolved', None) or {}).items() if _norm(k) != _norm(v)}
    for r in (hits or recs[:40]):
        for name, _, _ in cons:
            if name in (getattr(a, '_resolved', None) or {}):
                continue
            got = attr_hit(r.get('params'), name)[0]
            if got and _norm(got) != _norm(name):
                res.setdefault(name, set()).add(got)
    amb = {k: v for k, v in res.items() if v}
    if not getattr(a, '_jcat', None) and len(getattr(a, '_catamb', [])) > 1:
        print(f"  category: {' '.join(a.args)!r} names {len(a._catamb)} JLC categories, none chosen, "
              f"so the pool is keyword-only; --cat picks one: "
              + ', '.join(repr(c) for c in a._catamb[:8]))
    elif not getattr(a, '_jcat', None) and a.args and not getattr(a, '_catamb', None):
        print(f"  category: no JLC category is named {' '.join(a.args)!r}, so the pool is keyword-only "
              f"and limits only filter what the keyword happens to find")
    if LIGHT_LED.search(' '.join(a.args)):
        print("  note: JLC and LCSC have no category for white/lighting LEDs (2026-09-27: LED ones are "
              "Indication, IR, RGB, UV, COB), and discrete emitters (SST-20, XHP50, LH351D, 3030/5050 "
              "mid-power) sit at 0 LCSC stock, so CCT/CRI/flux cannot be filtered here. DigiKey is the "
              "source for this class (set DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET); COBs: "
              "--cat 'Chip On Board (COB) Light Sources'.")
    if amb:
        print("  resolved: " + ';  '.join(
            f"{k} -> {' | '.join(sorted(v)[:3])}" for k, v in sorted(amb.items())))
    if not hits and getattr(a, '_facet_counts', None):
        print(f"  parts in '{a._jcat}'{' ' + _pkg_literal(cons) if _pkg_literal(cons) else ''} "
              "meeting each limit on its own (any stock): "
              + ', '.join(f"{k} {v:,}" for k, v in sorted(a._facet_counts.items(), key=lambda x: x[1])))
    if not hits and getattr(a, '_facet_counts', None) is not None and not getattr(a, '_noattr', False):
        a._noattr = True       # an exact pool holds no near misses: widen it to find them
        recs, _ = build_pool(a, cons, kws, jkws)
        _, why, near = apply_cons(recs, fcons)
    if why and not hits:
        print("  candidates failing each limit (one part can fail several): "
              + ', '.join(f"{k}({v})" for k, v in sorted(why.items(), key=lambda x: -x[1])))
    if selfhit and not hits:
        print(f"  the only match was {selfhit[0].get('sku')} itself")
    near = sorted((n for n in near if n[0].get('sku') not in excl),
                  key=lambda n: (unit(n[0]) is None, unit(n[0]) or 0))[:5] if not hits else []
    print()
    print(hdr)
    for r in hits:
        nat = r.get('currency') or 'USD'
        q = buy_qty(qty, r.get('moq'), r.get('multiple'))
        up = unit(r)
        vals = ''.join(f"{trunc(attr_of(r.get('params'), _rname(a, c)), 8):<9}" for c in cols)
        st = r.get('stock')
        lib = {'base': 'BASIC', 'expand': 'ext'}.get(r.get('library'), '-')
        if r.get('source') == 'JLC' and not a.basic:
            lib += '  JLC price (no LCSC detail)'
        print(f"{r.get('sku',''):<{sw}} {trunc(r.get('mpn'),22):<22} {trunc(r.get('mfr'),14):<14} "
              f"{trunc(r.get('package'),8):<8} {vals}"
              f"{(f'{st:,}' if isinstance(st,int) else '?'):>9}  "
              f"{dmoney1(up, nat, a):<12} {dmoney1(up*q if up else None, nat, a):<10} {lib}")
        if r.get('_clone'):
            o = r['_clone']
            ov = [f"{_short(c)} {v}" for c in cols for v in [attr_of(o.get('params'), _rname(a, c))] if v]
            print(f"{'':<{sw}} ^ clone? of {o.get('mfr')} {o.get('mpn')} ({o.get('sku')})"
                  + (f": {', '.join(ov)} there" if ov else '')
                  + " - trust its figures only after its own datasheet")
    if not hits:
        print("  nothing matched. `--fields` lists the attribute names and values that "
              "are actually present, or loosen one constraint.")
        if near:
            _lcsc_reprice([n[0] for n in near], a)
            print("\n  near misses - each fails exactly ONE limit, cheapest first:")
            for r, name, lab, v in near:
                up = unit(r)
                print(f"    {r.get('sku',''):<11} {trunc(r.get('mpn'),22):<22} "
                      f"{trunc(r.get('mfr'),14):<14} {dmoney1(up, r.get('currency') or 'USD', a):<10} "
                      f"stock {r.get('stock') or 0:>9,}   {name} = {v if v is not None else '(not listed)'}"
                      f"  (limit {lab})")
        if getattr(a, '_xbase', None):     # nothing on LCSC is exactly when DigiKey matters
            _xcheck(a)
        return 1
    print("\n`part.py show <sku>` for the ladder and a verified datasheet"
          + ("" if a.nojlc else "   jlc: BASIC = no $3 Extended line fee"))
    a._shown = hits
    if getattr(a, '_xbase', None):
        _xcheck(a)
    return 0

def _xcheck(a):
    """--xcheck: the same limits on DigiKey, then both shortlists' top 3 in one spec
    table. No verdict: which is better is a judgement over the whole table."""
    b = a._xbase
    print(f"\n{'=' * 30} DigiKey, same limits {'=' * 30}")
    c_pick(b)
    both = (getattr(a, '_shown', None) or [])[:3] + (getattr(b, '_shown', None) or [])[:3]
    if both:
        print(f"\n{'=' * 30} shortlist spec table {'=' * 30}")
        spec_table(both, a)

# same class or better: an X7R may replace an X5R, never the reverse
_DIEL_RANK = ['Y5V', 'Z5U', 'X7T', 'X6S', 'X5R', 'X7S', 'X7R', 'C0G', 'NP0']

# alt: which way "at least as good" runs for a parameter, by its LCSC name. First
# pattern wins; 'skip' = not held (secondary specs, ranges, test conditions). A
# name matching nothing is held equal when its value is text (colour, shielding,
# polarity wording) and not held when it is a number nobody ranked.
_ALT_RULES = (
    ('eq', r'^output voltage|^type$|polarity|^number|configuration|channels|output type'
           r'|^frequency$|load capacitance|colou?r|zener voltage\(nom|resistance @|b constant \('),
    ('le', r'^capacitance$|junction capacitance'),     # a TVS/ESD's C (an MLCC's is held exact)
    ('skip', r'temperature|feature|capacitance|charge|surge|ciss|coss|crss|\(range\)'),
    ('le', r'rds|resistance|dcr|esr|forward(?!.*current)|leakage|clamping|quiescent'
           r'|supply current|standby current|dropout|threshold|tolerance|stability|impedance\(zz'),
    ('ge', r'voltage|current|power|dissipation|vgs|breakdown'),
)
# an NTC lists B at up to four reference temperatures, most alternates only one:
# hold the first listed of these, not all of them
_B_PREF = ('25/50', '25/85', '25/100', '25/75')

def _alt_hold(name, value):
    """--w spec that keeps a replacement at least as good on one parameter, or None."""
    if value in (None, '', '-'):
        return None
    x = enum(value)
    rule = next((r for r, pat in _ALT_RULES if re.search(pat, name.lower())),
                'eq' if x is None else 'skip')
    if rule == 'eq':
        return str(value)
    if rule in ('ge', 'le') and x is not None:
        if x < 0:                                  # P-channel: -30 V beats -20 V
            rule = 'le' if rule == 'ge' else 'ge'
        return ('>=' if rule == 'ge' else '<=') + fmt_si(x)
    return None

def c_alt(a):
    """Cheaper/stocked/Basic equivalents of a part already on the board: same JLC
    category and package, equal-or-better on every parameter _alt_hold ranks."""
    if not a.args:
        print("alt: give a C-code, e.g. part.py alt C45783 --qty 100"); return 1
    base = resolve(a.args[0], a)
    if not base:
        print(f"alt: {a.args[0]} not found"); return 1
    b = base[0]
    p = b.get('params') or []
    a.pkg = a.pkg or b.get('package')
    used = set()
    for k in ('cap', 'res', 'ind', 'volt', 'tol', 'diel'):   # passives: exact names only
        name, v = _exact(p, k)
        if not name or v in ('', '-'):
            continue
        used.add(name)
        x, d = enum(v), str(v).strip().upper()
        if getattr(a, k, None) is not None:
            continue                                         # the caller's spec wins
        if k in ('cap', 'res', 'ind'):
            setattr(a, k, v)                                 # exact value
        elif k in ('volt', 'tol') and x:
            setattr(a, k, ('>=' if k == 'volt' else '<=') + fmt_si(x))
        elif k == 'diel' and d in _DIEL_RANK:
            a.diel = ','.join(_DIEL_RANK[_DIEL_RANK.index(d):])
    mine = {_norm(w.split('=', 1)[0]) for w in a.w}
    holds = [(n, s) for n, v in p if n not in used and _norm(n) not in mine
             for s in [_alt_hold(n, v)] if s]
    bs = sorted((h for h in holds if h[0].lower().startswith('b constant (')),
                key=lambda h: next((i for i, t in enumerate(_B_PREF)
                                    if t in re.sub(r'[^\d/]', '', h[0])), 9))
    holds = [h for h in holds if h not in bs[1:]]
    if re.search(r'TVS|ESD', b.get('category') or '') and not _exact(p, 'cap')[0]:
        print(f"note: {b.get('sku')} lists no capacitance, so none is held - on an RF or "
              f"high-speed line check each candidate's C in its datasheet")
    # numeric limits first (they become the table columns), secondary ratings last
    holds.sort(key=lambda h: (h[1][:1] not in '<>',
                              bool(re.search(r'dissipation|^vgs$', h[0].lower()))))
    a.w = list(a.w) + [f"{n}={s}" for n, s in holds]
    jlc_annotate([b], a.jobs, a.fresh)
    cat = (b.get('category') or '').split('>')[-1].strip()
    a._jcat = b.get('jcat') or (b.get('category') if b['source'] == 'JLC' else None)
    if a._jcat:        # JLC's exact category + package beats LCSC keyword relevance
        a.source = 'jlc'
    if a.args[1:]:
        a.args = a.args[1:]
    else:
        a.args = [''] if a._jcat else [cat or (b.get('mpn') or '')]
    q0 = buy_qty(a.qty or 100, b.get('moq'), b.get('multiple'))
    up0 = price_at(b.get('ladder') or [], q0)
    flags = [f"--{k} {getattr(a, k)!r}" for k in FLAG_ATTRS if getattr(a, k, None)]
    print(f"alt of {b.get('sku')} {b.get('mpn')} ({a._jcat or cat})   "
          f"now {dmoney1(up0, b.get('currency') or 'USD', a)}@{q0}, "
          f"{ {'base':'BASIC','expand':'ext'}.get(b.get('library'), 'not in JLC lib') }\n"
          f"  holding (rerun `pick` with a subset of these to loosen):\n    "
          + '\n    '.join(flags + [f"--w {w!r}" for w in a.w]) + "\n")
    a._exclude = {b.get('sku')}
    return c_pick(a)

def _pick_selftest():
    """Offline checks for the pick/alt value logic. Each line is a bug that shipped."""
    fet = [('Gate Threshold Voltage (Vgs(th))', '2.2V'), ('Ciss-Input Capacitance', '1.037nF'),
           ('Drain to Source Voltage', '30V'), ('RDS(on)', '5mΩ@10V'), ('Type', 'N-Channel'),
           ('Operating Temperature', '-55℃~+150℃'), ('Configuration', '-')]
    checks = [
        abs(enum('5mΩ@10V') - 5e-3) < 1e-12,          # '@' test conditions parse
        abs(enum('450mV@1A') - 0.45) < 1e-12,
        enum('15A@8/20us') == 15.0 and enum('10V~35V') == 10.0,
        attr_hit(fet, 'res') == (None, None),           # not 'thRESHOLD'
        attr_hit(fet, 'vds')[0] == 'Drain to Source Voltage',
        _exact(fet, 'cap') == (None, None),             # a FET's Ciss is no cap value
        {n: _alt_hold(n, v) for n, v in fet} == {
            'Gate Threshold Voltage (Vgs(th))': '<=2.2', 'Ciss-Input Capacitance': None,
            'Drain to Source Voltage': '>=30', 'RDS(on)': '<=5m', 'Type': 'N-Channel',
            'Operating Temperature': None, 'Configuration': None},
        _alt_hold('Drain to Source Voltage', '-30V') == '<=-30',      # P-channel
        _alt_hold('Emitted Color', 'Green') == 'Green',                # unranked text: equal
        _alt_hold('standby current', '34uA') == '<=34u',               # JLC's LDO Iq: lower is better
        make_pred('<=5m')[0]('4.6mΩ@4.5V') and not make_pred('<=5m')[0]('8mΩ@10V'),
        # category words: whole word, plural ok, 'led' is not 'Leaded'
        _cat_hits('led', ['LED Drivers', 'Multilayer Ceramic Capacitors MLCC - Leaded']) == ['LED Drivers'],
        _cat_hits('tvs', ['ESD and Surge Protection (TVS/ESD)', 'Crystals']) == ['ESD and Surge Protection (TVS/ESD)'],
        _cat_hits('test point', ['Test Points / Test Rings', 'Test Clips']) == ['Test Points / Test Rings'],
        # JLC's sized package names carry a comma: one literal, not an any-of
        _one_pkg('SMD,11.5x10mm') == 'SMD,11.5x10mm' and _one_pkg('0402,0603') is None,
        make_pred('SMD,11.5x10mm')[0]('SMD,11.5x10mm') and not make_pred('SMD,11.5x10mm')[0]('SMD,4x4mm'),
        # alt holds: Vz / R25 / B equal, Vbr up, a TVS's C down (sub-pF must round-trip)
        [_alt_hold(n, v) for n, v in (('Zener Voltage(Nom)', '15V'), ('Zener Voltage(Range)', '14.25V~15.75V'),
                                      ('Resistance @ 25℃', '10kΩ'), ('B Constant (25℃/50℃)', '3380K'),
                                      ('Voltage - Breakdown', '15V'), ('Capacitance', '0.06pF'))]
        == ['15V', None, '10kΩ', '3380K', '>=15', '<=0.06p'],
        make_pred('<=0.06p')[0]('0.05pF') and not make_pred('<=0.06p')[0]('0.5pF'),
    ]
    # JLC server-side filter: facet values the local predicate accepts, '-' never
    cons = [(n, lab, make_pred(lab)[0]) for n, lab in (('volt', '>=25'), ('cap', '10u'))]
    fac = {'Voltage Rated': {'10V': 5, '25V': 7, '50V': 3, '-': 2}, 'Capacitance': {'1uF': 4, '10uF': 9}}
    checks.append(_server_attrs(cons, fac)[:3] == (
        [{'Voltage Rated': ['25V', '50V']}, {'Capacitance': ['10uF']}], {'volt': 10, 'cap': 9}, []))
    checks.append(_server_attrs(cons[:1], {'Voltage Rated': {'10V': 5}})[2] == ['volt'])
    # two spellings of one attribute: filter on the one most parts use
    isat = [('isat', '>=8', make_pred('>=8')[0])]
    checks.append(_server_attrs(isat, {'Current - Saturation (Isat)': {'9A': 1},
                                       'Current - Saturation(Isat)': {'8A': 50, '2A': 9}})[0]
                  == [{'Current - Saturation(Isat)': ['8A']}])
    # every failure counts (not just the first), and a one-failure part is a near miss
    recs = [{'sku': 'A', 'params': [('Voltage Rated', '10V'), ('Capacitance', '1uF')]},
            {'sku': 'B', 'params': [('Voltage Rated', '50V'), ('Capacitance', '1uF')]},
            {'sku': 'C', 'params': [('Voltage Rated', '50V'), ('Capacitance', '10uF')]}]
    ok, why, near = apply_cons(recs, cons)
    checks.append([r['sku'] for r in ok] == ['C'] and why == {'volt': 1, 'cap': 2}
                  and [(n[0]['sku'], n[1], n[3]) for n in near] == [('B', 'cap', '1uF')])
    # every catalog spelling of a shorthand: the most populated facet wins
    ldo = {'standby current': {'34uA': 9, '1uA': 7180}, 'Quiescent Current': {'-': 1}, 'Noise': {'-': 5}}
    checks.append([facet_name(ldo, n) for n in ('iq', 'Supply Current (Iq)', 'Quiescent Current', 'Noise', 'zz')]
                  == ['standby current'] * 3 + ['Noise', None])
    checks.append(_server_attrs([('iq', '<=1u', make_pred('<=1u')[0]), ('Ground Current', '<=1u',
                                                                        make_pred('<=1u')[0])], ldo)[3:]
                  == ({'iq': 'standby current'}, ['Ground Current']))
    checks.append(_near_names(ldo, 'Ground Current')[0][0] == 'standby current')
    # sort direction from the name, and the pareto front (price up, merit strictly better)
    checks.append([sort_desc(n) for n in ('standby current', 'capacitance', 'voltagerated',
                                          'drainsourceonresistancerdson', 'powersupplyrejectionratiopsrr')]
                  == [False, True, True, False, True])
    pr = [{'sku': k, 'p': p, 'params': [('standby current', v)]}
          for k, p, v in (('A', 1, '50uA'), ('B', 2, '60uA'), ('C', 3, '5uA'), ('D', 3, '1uA'), ('E', 4, '2uA'))]
    checks.append([r['sku'] for r in _pareto(pr, lambda r: r['p'], 'standby current', False)] == ['A', 'D'])
    # application words name the category; word overlap sent 'load switch' to load cells
    checks.append([_app_cats(t)[:1] for t in ('load switch', 'current limit switch', 'sawtooth', 'eFuse 5V')]
                  == [['Power Distribution Switches']] * 2 + [[], ['Surge Protection Devices (SPDs)']])
    # clones: strict containment, another maker, listed earlier
    ti = {'sku': 'C408972', 'mpn': 'TLV74333PDBVR', 'mfr': 'Texas Instruments'}
    checks.append(_cores('TLV74333PDBVR-TP') == ['TLV74333PDBVR']
                  and clone_of({'sku': 'C49451989', 'mpn': 'TLV74333PDBVR-TP', 'mfr': 'TECH PUBLIC'}, [ti]) == ti
                  and clone_of({'sku': 'C5446', 'mpn': 'XC6206P332MR-G', 'mfr': 'TOREX'},
                               [{'sku': 'C9000000', 'mpn': 'XC6206P332MR', 'mfr': 'X'}]) is None
                  and clone_of({'sku': 'C9', 'mpn': 'BAT54S', 'mfr': 'A'}, [{'sku': 'C1', 'mpn': 'BAT54S', 'mfr': 'B'}]) is None)
    bad = [i for i, ok in enumerate(checks, 1) if not ok]
    assert not bad, f"pick self-test failed check(s) {bad}"
    return f"{len(checks)}/{len(checks)}"
