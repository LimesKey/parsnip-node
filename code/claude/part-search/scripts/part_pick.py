"""part.py `pick` and `alt`: the candidate pool, constraints and the drop-in table."""
import sys, re, json
import concurrent.futures as cf
from part_core import (buy_qty, cached, dmoney1, _EOL_RE, http, jlc_annotate, jlc_facets,
                       jlc_lib_map, lcsc_detail, lcsc_search, LCSC_SEARCH, price_at, resolve,
                       trunc)
from part_value import (attr_hit, attr_of, enum, _exact, FLAG_ATTRS, fmt_si, make_pred, _norm,
                        series_in, _short, STD_V, _UNIT_OF)

def _server_attrs(cons, facets):
    """(componentAttributeList, {constraint: parts meeting it alone}, [unmet]).
    JLC matches attribute values exactly, so each constraint becomes the list of
    sidebar values the local predicate accepts - ranges, >= and any-of all run
    here, the server only intersects. A constraint whose attribute is not in the
    facets is left to the local filter; 'unmet' ones no listed value meets."""
    names = [(n, '') for n in facets]
    attrs, counts, unmet = [], {}, []
    for name, lab, pred in cons:
        pname = attr_hit(names, name)[0] if name != 'pkg' else None
        if not pname:
            continue
        # one attribute, two spellings: Power Inductors has 'Current - Saturation
        # (Isat)' on 14 parts and '...Saturation(Isat)' on 79k; filter on the big one
        pname = max((n for n in facets if _norm(n) == _norm(pname)),
                    key=lambda n: sum(facets[n].values()))
        vals = [v for v in facets[pname] if v not in ('', '-') and pred(v)]
        counts[name] = sum(facets[pname][v] for v in vals)
        if vals:
            attrs.append({pname: vals})
        else:
            unmet.append(name)
    return attrs, counts, unmet

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
    whole = _cat_hits(text, cats)
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
    note, jkeywords = [], jkeywords or keywords
    jkw = dict(pkg=_pkg_literal(cons), category=getattr(a, '_jcat', None),
               cheapest=a.sort == 'price', instock=a.minstock > 0, budget=a.maxq)
    exact = []                          # set when JLC filtered on the attributes

    def jlc(library=None):
        # with a category, every constraint the sidebar can express goes to JLC as
        # an exact value list, so the pool IS the matching parts, cheapest first,
        # instead of the cheapest 600 of the category filtered afterwards
        attrs, kws = None, jkeywords
        if jkw['category'] and not getattr(a, '_noattr', False):
            fac = jlc_facets(jkw['category'], jkw['pkg'], a.fresh)
            if fac:
                attrs, a._facet_counts, unmet = _server_attrs(cons, fac)
                exact.append(True)
                if unmet:
                    note.append(f"no {' / '.join(unmet)} value in '{jkw['category']}'"
                                f"{' ' + jkw['pkg'] if jkw['pkg'] else ''} meets the limit")
                    return []
                kws = [''] if attrs else kws
        rows = _stock_ok(list(jlc_lib_map(kws, a.fresh, library=library, attrs=attrs,
                                          **jkw).values()), a)
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

def c_pick(a):
    cons = _cons_from_args(a)
    if not getattr(a, '_jcat', None) and (a.cat or a.source != 'lcsc'):
        a._jcat = _pick_category(a)
    kws, jkws = _keywords(a, cons)
    if not kws:
        print("pick: give a keyword and/or at least one constraint, e.g.\n"
              "  part.py pick MLCC --cap 4.7u..100u --volt '>=25' --pkg 0805 --diel X7R")
        return 1
    recs, note = build_pool(a, cons, kws, jkws)
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
    hits, why, near = apply_cons(recs, cons)
    excl = getattr(a, '_exclude', None) or set()
    selfhit = [h for h in hits if h.get('sku') in excl]
    hits = [h for h in hits if h.get('sku') not in excl]
    if a.basic:
        hits = [h for h in hits if h.get('library') == 'base']
    qty = a.qty or 100
    def unit(r):
        q = buy_qty(qty, r.get('moq'), r.get('multiple'))
        return price_at(r.get('ladder') or [], q)
    keyf = {'price': lambda r: (unit(r) is None, unit(r) or 0),
            'stock': lambda r: -(r.get('stock') or 0),
            'cap':   lambda r: -(enum(attr_of(r.get('params'), 'cap')) or 0),
            'volt':  lambda r: -(enum(attr_of(r.get('params'), 'volt')) or 0)}
    hits.sort(key=keyf.get(a.sort, keyf['price']))
    eol = 0
    if not a.basic:               # --basic quotes the JLC assembly catalog on purpose
        hits = _stock_ok(_lcsc_reprice(hits[:max(3 * a.n, 24)], a), a)
        eol = sum(1 for h in hits if _EOL_RE.search(h.get('lifecycle') or ''))
        hits = [h for h in hits if not _EOL_RE.search(h.get('lifecycle') or '')]
        hits.sort(key=keyf.get(a.sort, keyf['price']))
    hits = hits[:a.n]
    if not a.nojlc:
        jlc_annotate(hits, a.jobs, a.fresh)
    if a.json:
        print(json.dumps(hits, indent=1)); return 0 if hits else 1
    cols = [n for n, _, _ in cons if n != 'pkg'][:4]
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
    hdr = (f"{'sku':<11} {'mpn':<22} {'mfr':<14} {'pkg':<8} "
           + ''.join(f"{trunc(_short(c),8):<9}" for c in cols)
           + f"{'stock':>9}  {'unit@'+str(qty):<12} {'ext':<10} jlc")
    shown = jkws if a.basic or a.source == 'jlc' else kws
    drop = ([f"{a._lowstock} below --minstock {a.minstock}"] if getattr(a, '_lowstock', 0) else []) \
        + ([f"{eol} EOL/NRND"] if eol else [])
    print(f"pick: {' | '.join(k or '(category/package only)' for k in shown[:3])}"
          f"{' ...' if len(shown)>3 else ''}   [{note}; {len(hits)} shown"
          + (f"; dropped {', '.join(drop)}" if drop else '') + "]")
    res = {}
    for r in (hits or recs[:40]):
        for name, _, _ in cons:
            got = attr_hit(r.get('params'), name)[0]
            if got and _norm(got) != _norm(name):
                res.setdefault(name, set()).add(got)
    amb = {k: v for k, v in res.items() if v}
    if not getattr(a, '_jcat', None) and len(getattr(a, '_catamb', [])) > 1:
        print(f"  category: {' '.join(a.args)!r} names {len(a._catamb)} JLC categories, none chosen, "
              f"so the pool is keyword-only; --cat picks one: "
              + ', '.join(repr(c) for c in a._catamb[:8]))
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
        _, why, near = apply_cons(recs, cons)
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
        vals = ''.join(f"{trunc(attr_of(r.get('params'), c), 8):<9}" for c in cols)
        st = r.get('stock')
        lib = {'base': 'BASIC', 'expand': 'ext'}.get(r.get('library'), '-')
        if r.get('source') == 'JLC' and not a.basic:
            lib += '  JLC price (no LCSC detail)'
        print(f"{r.get('sku',''):<11} {trunc(r.get('mpn'),22):<22} {trunc(r.get('mfr'),14):<14} "
              f"{trunc(r.get('package'),8):<8} {vals}"
              f"{(f'{st:,}' if isinstance(st,int) else '?'):>9}  "
              f"{dmoney1(up, nat, a):<12} {dmoney1(up*q if up else None, nat, a):<10} {lib}")
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
        return 1
    print("\n`part.py show <sku>` for the ladder and a verified datasheet"
          + ("" if a.nojlc else "   jlc: BASIC = no $3 Extended line fee"))
    return 0

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
           r'|supply current|dropout|threshold|tolerance|stability|impedance\(zz'),
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
    checks.append(_server_attrs(cons, fac) == (
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
    bad = [i for i, ok in enumerate(checks, 1) if not ok]
    assert not bad, f"pick self-test failed check(s) {bad}"
    return f"{len(checks)}/{len(checks)}"
