"""part.py core: config/cache, HTTP, FX and money, datasheet verification, and the
LCSC / DigiKey / JLC catalog clients every command shares."""
import sys, os, re, json, time, hashlib
import concurrent.futures as cf
import urllib.request, urllib.parse, urllib.error

UA = ('Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) '
      'Chrome/126.0.0.0 Safari/537.36')
CACHE = os.environ.get('PARTSEARCH_CACHE',
                       os.path.expanduser('~/.cache/partsearch'))
CFG = os.path.expanduser('~/.config/partsearch/config.json')
TTL = 24 * 3600

# LCSC endpoints that actually work as of the last selftest.
# The obvious ones (wmsc.lcsc.com/wmsc/*, /ftps/wm/search/global) return 403/404.
LCSC_DETAIL = 'https://wmsc.lcsc.com/ftps/wm/product/detail?productCode={code}'
LCSC_SEARCH = 'https://easyeda.com/api/eda/product/search'      # POST, form-encoded
LCSC_PAGE = 'https://www.lcsc.com/product-detail/{code}.html'
# the EasyEDA footprint LCSC links to a C-code (the land JLC assembles on). CloudFront
# 403s after ~150 quick calls, so fpcheck asks only for leadless parts, 2 at a time
EE_COMP = 'https://easyeda.com/api/products/{code}/components?version=6.4.19.5'
DK_TOKEN = 'https://api.digikey.com/v1/oauth2/token'
DK_KEYWORD = 'https://api.digikey.com/products/v4/search/keyword'
DK_DETAIL = 'https://api.digikey.com/products/v4/search/{pn}/productdetails'

# ---------------------------------------------------------------- config/cache

def cfg(key, env):
    v = os.environ.get(env)
    if v:
        return v
    try:
        return json.load(open(CFG)).get(key)
    except Exception:
        return None

def _cpath(tag, key):
    os.makedirs(CACHE, exist_ok=True)
    h = hashlib.sha1(f"{tag}|{key}".encode()).hexdigest()[:20]
    return os.path.join(CACHE, f"{tag}_{h}.json")

def cached(tag, key, fn, fresh=False, ttl=TTL):
    p = _cpath(tag, key)
    # PARTSEARCH_FROZEN: every cached entry is fresh, so `selftest --golden` compares
    # code, not a price that moved when an entry expired between record and check
    if not fresh and os.path.exists(p) and (os.environ.get('PARTSEARCH_FROZEN')
                                            or time.time() - os.path.getmtime(p) < ttl):
        try:
            return json.load(open(p))
        except Exception:
            pass
    v = fn()
    # http() returns {'_error': ...} on failure; caching that would replay a
    # transient outage for the whole TTL
    if v is not None and not (isinstance(v, dict) and '_error' in v):
        try:
            with open(p, 'w') as f:
                json.dump(v, f)
        except Exception:
            pass
    return v

# ---------------------------------------------------------------- http

def http(url, data=None, headers=None, method=None, timeout=25, retries=2, raw=False,
         rng=None):
    hdr = {'User-Agent': UA, 'Accept': 'application/json, */*'}
    hdr.update(headers or {})
    if rng:
        hdr['Range'] = f'bytes={rng[0]}-{rng[1]}'
    body = data
    if isinstance(data, dict):
        if hdr.get('Content-Type', '').startswith('application/json'):
            body = json.dumps(data).encode()
        else:
            hdr.setdefault('Content-Type', 'application/x-www-form-urlencoded')
            body = urllib.parse.urlencode(data).encode()
    elif isinstance(data, str):
        body = data.encode()
    last = None
    for i in range(retries + 1):
        try:
            req = urllib.request.Request(url, data=body, headers=hdr, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                blob = r.read()
                return (blob, r.status, dict(r.headers)) if raw else json.loads(blob or b'{}')
        except urllib.error.HTTPError as e:
            last = f"HTTP {e.code}"
            if raw:
                return (e.read()[:2000], e.code, dict(e.headers or {}))
            if e.code in (400, 401, 403, 404):
                try:
                    return json.loads(e.read() or b'{}')
                except Exception:
                    return {'_error': last}
        except Exception as e:
            last = str(e)[:120]
        time.sleep(0.7 * (i + 1))
    return (b'', 0, {}) if raw else {'_error': last or 'failed'}

# ---------------------------------------------------------------- fx

FX_PRIMARY = 'https://open.er-api.com/v6/latest/USD'
FX_FALLBACK = 'https://api.frankfurter.dev/v1/latest?base=USD'
_FX = {'loaded': False, 'data': None, 'used': False}

def fx_rates(fresh=False):
    """USD-based rate table {'CAD': 1.37, ...}, cached 24 h. None if unreachable."""
    if _FX['loaded'] and not fresh:
        return _FX['data']
    def go():
        d = http(FX_PRIMARY, timeout=12, retries=1)
        if isinstance(d, dict) and d.get('rates'):
            return {'rates': d['rates'], 'src': 'open.er-api.com',
                    'date': (d.get('time_last_update_utc') or '')[:16]}
        d = http(FX_FALLBACK, timeout=12, retries=1)
        if isinstance(d, dict) and d.get('rates'):
            r = dict(d['rates']); r['USD'] = 1.0
            return {'rates': r, 'src': 'frankfurter.dev', 'date': d.get('date', '')}
        return None
    v = cached('fx', 'usd-base', go, fresh)
    _FX['loaded'], _FX['data'] = True, v
    return v

def conv(x, frm, to):
    """Convert x from currency `frm` to `to`. None if x is None or rate unknown."""
    if x is None or frm == to:
        return x
    fx = fx_rates()
    if not fx:
        return None
    r = fx['rates']
    if frm not in r or to not in r:
        return None
    return x / r[frm] * r[to]

def fx_note():
    """One-line provenance for the rate actually used; '' until a conversion happened."""
    if not _FX['used'] or not _FX['data']:
        return ''
    d = _FX['data']
    cad = d['rates'].get('CAD')
    return (f"fx: 1 USD = {cad:.4f} CAD ({d['src']}, {d['date']}, cached <=24 h)"
            if cad else f"fx: {d['src']} {d['date']}")

# ---------------------------------------------------------------- helpers

def price_at(ladder, qty):
    """ladder = [(break_qty, unit_price)] sorted. Returns unit price for qty."""
    best = None
    for b, p in sorted(ladder):
        if qty >= b:
            best = p
    return best if best is not None else (ladder[0][1] if ladder else None)

def buy_qty(qty, moq, mult):
    q = max(qty, moq or 1)
    if mult and mult > 1:
        q = ((q + mult - 1) // mult) * mult
    return q

SYM = {'USD': 'US$', 'CAD': 'C$', 'EUR': 'EUR ', 'GBP': 'GBP ', 'JPY': 'JPY '}

def money(x, code='USD'):
    if x is None:
        return '?'
    sym = SYM.get(code, (code + ' ') if code else '$')
    return f"{sym}{x:.5f}".rstrip('0').rstrip('.') if x < 0.1 else f"{sym}{x:.4f}".rstrip('0').rstrip('.')

def dmoney(x, native, a):
    """Price for display: converted to a.currency, native appended when different.
    'C$0.17 (US$0.124)'. Falls back to native alone if FX is unreachable."""
    if x is None:
        return '?'
    if native == a.currency:
        return money(x, native)
    cx = conv(x, native, a.currency)
    if cx is None:
        return money(x, native) + '!'          # ! = FX unavailable, native shown
    _FX['used'] = True
    return f"{money(cx, a.currency)} ({money(x, native)})"

def dmoney1(x, native, a):
    """Converted only, no native tail - for table columns."""
    if x is None:
        return '?'
    if native == a.currency:
        return money(x, native)
    cx = conv(x, native, a.currency)
    if cx is None:
        return money(x, native) + '!'
    _FX['used'] = True
    return money(cx, a.currency)

def trunc(s, n):
    s = re.sub(r'\s+', ' ', str(s or '')).strip()
    return s if len(s) <= n else s[:n - 1] + '\u2026'

# ---------------------------------------------------------------- datasheet check

def verify_pdf(url, timeout=20):
    """Actually fetch the first bytes. Returns (ok, note). Catches dead/expiring links."""
    if not url:
        return False, 'no url given'
    if url.startswith('//'):
        url = 'https:' + url
    blob, status, hdrs = http(url, raw=True, rng=(0, 1023), retries=1, timeout=timeout)
    ctype = (hdrs.get('Content-Type') or hdrs.get('content-type') or '').lower()
    if status in (200, 206) and blob[:4] == b'%PDF':
        return True, f"{status} application/pdf, {hdrs.get('Content-Length','?')} B in first chunk"
    if status in (200, 206) and 'pdf' in ctype:
        return True, f"{status} {ctype} (no %PDF magic in first chunk)"
    if status in (200, 206) and 'html' in ctype:
        return False, f"{status} but served HTML, probably a login/landing page"
    return False, f"HTTP {status or 'no response'} {ctype}"

def best_datasheet(rec, timeout=20):
    """Try every datasheet candidate until one verifies. Never returns a broken link
    without saying so."""
    tried = []
    for label, url in rec.get('datasheet_candidates', []):
        if not url:
            continue
        ok, note = verify_pdf(url, timeout)
        tried.append((label, url, ok, note))
        if ok:
            return url, tried
    return None, tried

# ---------------------------------------------------------------- LCSC

def lcsc_detail(code, fresh=False):
    code = code.strip().upper()
    if not re.fullmatch(r'C\d+', code):
        return None
    def go():
        d = http(LCSC_DETAIL.format(code=code))
        return d.get('result')
    r = cached('lcscdet', code, go, fresh)
    if not r:
        return None
    ladder = [(int(p['ladder']), float(p.get('usdPrice') or p.get('productPrice') or 0))
              for p in (r.get('productPriceList') or []) if p.get('ladder')]
    params = [(p.get('paramNameEn') or p.get('paramName'), p.get('paramValueEn') or p.get('paramValue'))
              for p in (r.get('paramVOList') or [])]
    cats = [c.get('catalogNameEn') for c in (r.get('parentCatalogList') or [])]
    cats = [c for c in cats if c] + [r.get('catalogName')]
    return {
        'source': 'LCSC',
        'sku': r.get('productCode'),
        'mpn': r.get('productModel'),
        'mfr': r.get('brandNameEn'),
        'desc': r.get('productDescEn') or r.get('productNameEn'),
        'package': r.get('encapStandard'),
        'category': ' > '.join(dict.fromkeys([c for c in cats if c])),
        'stock': r.get('stockNumber'),
        'stock_detail': {'domestic': (r.get('domesticStockVO') or {}).get('total'),
                         'overseas': (r.get('overseasStockVO') or {}).get('total')},
        'moq': r.get('minBuyNumber'),
        'multiple': r.get('split'),
        'reel_qty': r.get('minPacketNumber'),
        'packaging': r.get('productArrange'),
        'rohs': r.get('isEnvironment'),
        'lifecycle': r.get('productCycle'),
        'eccn': r.get('eccn'),
        'currency': r.get('currencyType') or 'USD',
        'ladder': sorted(ladder),
        'params': params,
        'url': LCSC_PAGE.format(code=code),
        'datasheet_candidates': [('lcsc pdfUrl', r.get('pdfUrl')),
                                 ('lcsc pdfLinkUrl', r.get('pdfLinkUrl'))],
    }

def lcsc_search(keyword, n=8, fresh=False):
    def go():
        return http(LCSC_SEARCH, data={'keyword': keyword, 'needAggs': 'false',
                                       'currPage': 1, 'pageSize': max(n, 5)},
                    headers={'Referer': 'https://easyeda.com/'})
    d = cached('lcscsearch', f"{keyword}|{n}", go, fresh)
    res = (d or {}).get('result') or {}
    out = []
    for p in (res.get('productList') or [])[:n]:
        ladder = []
        for row in (p.get('price') or []):
            try:
                ladder.append((int(row[0]), float(row[1])))
            except Exception:
                pass
        out.append({'source': 'LCSC', 'sku': p.get('number'), 'mpn': p.get('mpn'),
                    'mfr': p.get('manufacturer'), 'package': p.get('package'),
                    'stock': p.get('stock'), 'ladder': sorted(ladder),
                    'desc': '', 'currency': 'USD',
                    'url': 'https://www.lcsc.com' + (p.get('url') or ''),
                    'datasheet_candidates': []})
    return out, res.get('total', len(out))

# ---------------------------------------------------------------- DigiKey

def dk_token(fresh=False):
    cid, sec = cfg('digikey_client_id', 'DIGIKEY_CLIENT_ID'), cfg('digikey_client_secret', 'DIGIKEY_CLIENT_SECRET')
    if not cid or not sec:
        return None, 'no credentials (set DIGIKEY_CLIENT_ID / DIGIKEY_CLIENT_SECRET)'
    p = _cpath('dktok', cid)
    if not fresh and os.path.exists(p):
        try:
            t = json.load(open(p))
            if t.get('expires_at', 0) - 60 > time.time():
                return t['access_token'], 'cached token'
        except Exception:
            pass
    d = http(DK_TOKEN, data={'client_id': cid, 'client_secret': sec,
                             'grant_type': 'client_credentials'})
    if not d.get('access_token'):
        return None, f"token refused: {trunc(d.get('error_description') or d.get('_error') or d, 90)}"
    d['expires_at'] = time.time() + float(d.get('expires_in', 600))
    json.dump(d, open(p, 'w'))
    return d['access_token'], 'new token'

def dk_headers(tok, a):
    return {'Authorization': f'Bearer {tok}',
            'X-DIGIKEY-Client-Id': cfg('digikey_client_id', 'DIGIKEY_CLIENT_ID'),
            'X-DIGIKEY-Locale-Site': a.site,
            'X-DIGIKEY-Locale-Language': 'en',
            'X-DIGIKEY-Locale-Currency': a.currency,
            'Content-Type': 'application/json'}

def _dk_norm(p, currency):
    var = (p.get('ProductVariations') or [{}])
    v = next((x for x in var if (x.get('PackageType') or {}).get('Name', '').lower().startswith(('cut', 'bulk'))), var[0])
    ladder = [(int(s.get('BreakQuantity', 1)), float(s.get('UnitPrice', 0)))
              for s in (v.get('StandardPricing') or []) if s.get('UnitPrice') is not None]
    params = [(x.get('ParameterText') or x.get('Parameter'), x.get('ValueText') or x.get('Value'))
              for x in (p.get('Parameters') or [])]
    cat = p.get('Category') or {}
    catname = cat.get('Name', '')
    if cat.get('ChildCategories'):
        catname += ' > ' + (cat['ChildCategories'][0] or {}).get('Name', '')
    return {
        'source': 'DigiKey',
        'sku': v.get('DigiKeyProductNumber') or p.get('DigiKeyProductNumber'),
        'mpn': p.get('ManufacturerProductNumber') or p.get('ManufacturerPartNumber'),
        'mfr': (p.get('Manufacturer') or {}).get('Name'),
        'desc': (p.get('Description') or {}).get('ProductDescription') or p.get('DetailedDescription'),
        'package': (v.get('PackageType') or {}).get('Name'),
        'category': catname,
        'stock': p.get('QuantityAvailable'),
        'stock_detail': {'variation': v.get('QuantityAvailableforPackageType')},
        'moq': v.get('MinimumOrderQuantity'),
        'multiple': v.get('StandardPackage'),
        'packaging': (v.get('PackageType') or {}).get('Name'),
        'lifecycle': (p.get('ProductStatus') or {}).get('Status'),
        'rohs': (p.get('Classifications') or {}).get('RohsStatus'),
        'currency': currency,
        'ladder': sorted(ladder),
        'params': params,
        'url': p.get('ProductUrl'),
        'datasheet_candidates': [('digikey DatasheetUrl', p.get('DatasheetUrl')),
                                 ('digikey PrimaryDatasheet', p.get('PrimaryDatasheet'))],
    }

def dk_search(keyword, n, a):
    tok, note = dk_token(a.fresh)
    if not tok:
        return [], note
    def go():
        return http(DK_KEYWORD, data={'Keywords': keyword, 'Limit': n, 'Offset': 0},
                    headers=dk_headers(tok, a))
    d = cached('dksearch', f"{keyword}|{n}|{a.site}|{a.currency}", go, a.fresh)
    if d.get('_error') or 'Products' not in d:
        return [], trunc(d.get('detail') or d.get('title') or d.get('_error') or 'no Products in response', 100)
    return [_dk_norm(p, a.currency) for p in d['Products'][:n]], f"{d.get('ProductsCount','?')} matches"

def dk_detail(pn, a):
    tok, note = dk_token(a.fresh)
    if not tok:
        return None
    def go():
        return http(DK_DETAIL.format(pn=urllib.parse.quote(pn, safe='')),
                    headers=dk_headers(tok, a), method='GET')
    d = cached('dkdet', f"{pn}|{a.site}|{a.currency}", go, a.fresh)
    p = d.get('Product') or (d.get('Products') or [None])[0]
    return _dk_norm(p, a.currency) if p else None

def resolve(spec, a):
    """C-code -> LCSC detail, else JLC's record (assembly-only parts LCSC does not
    sell retail). Anything else -> best search hit per provider."""
    m = re.fullmatch(r'C\d+', spec.strip().upper()) or re.search(r'_(C\d+)\.html', spec)
    if m:
        code = m.group(m.lastindex or 0).upper()
        r = lcsc_detail(code, a.fresh) or jlc_detail(code, a.fresh)
        return [r] if r else []
    out = []
    if a.provider in ('all', 'lcsc'):
        hits, _ = lcsc_search(spec, 1, a.fresh)
        if hits:
            full = lcsc_detail(hits[0]['sku'], a.fresh)
            out.append(full or hits[0])
    if a.provider in ('all', 'digikey'):
        hits, _ = dk_search(spec, 1, a)
        if hits:
            out.append(hits[0])
    return out

# ================================================================ JLCPCB catalog
# Why this is here at all, given LCSC is preferred: LCSC's search endpoint returns
# rows with NO parameters (mpn/package/stock/price only) and accepts no filter or
# sort arguments - probed exhaustively, see the endpoint table in SKILL.md. The JLC
# assembly endpoint returns up to 200 rows per call WITH parsed attributes, the
# price ladder, stock and Basic-vs-Extended, and filters package/category and sorts
# by price server-side. So JLC is the search index (pick's default pool) and LCSC
# stays the price of record: pick re-prices the rows it shows from LCSC detail.
# JLC prices are assembly-catalog prices, NOT LCSC retail. Never mix the two.

JLC_SEARCH = ('https://jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/'
              'smtGood/selectSmtComponentList')
JLC_FACETS = ('https://jlcpcb.com/api/overseas-pcb-order/v1/componentSearch/'
              'filterComponentAttribute')
JLC_HDR = {'Content-Type': 'application/json', 'Referer': 'https://jlcpcb.com/parts'}

def jlc_search(keyword, n=200, fresh=False, library=None, instock=False,
               pkg=None, category=None, cheapest=False, page=1, attrs=None):
    """Returns ([rec], total). Recs use the same shape as lcsc_detail where they
    overlap, with 'library' = 'base' (JLC Basic) | 'expand' (Extended, $3/line).
    Server-side filters (probed 2026-09-23, endpoints.md): pkg = exact package
    string, category = JLC's exact category name (a rec's 'category'), cheapest =
    price ascending, attrs = [{attribute: [exact values, any-of]}, ...] ANDed.
    The index covers ~7.2M parts, i.e. the LCSC catalog."""
    body = {'currentPage': page, 'pageSize': min(max(int(n), 1), 200), 'keyword': keyword}
    if library:
        body['componentLibraryType'] = library
    if instock:
        body['stockFlag'] = True
    if pkg:
        body['componentSpecificationList'] = [pkg]
    if category:
        body['secondSortName'] = category      # request's second = response's first
    if cheapest:
        body['sortMode'], body['sortASC'] = 'PRICE_SORT', 'ASC'
    if attrs:
        body['componentAttributeList'] = attrs
    def go():
        d = http(JLC_SEARCH, data=body, headers=JLC_HDR)
        return d if (d or {}).get('data') else None       # an error page is not a result
    d = cached('jlc', f"{keyword}|{n}|{library}|{instock}|{pkg}|{category}|{cheapest}|{page}"
               + (f"|{json.dumps(attrs, sort_keys=True)}" if attrs else ''), go, fresh)
    pi = ((d or {}).get('data') or {}).get('componentPageInfo') or {}
    out = []
    for i in (pi.get('list') or []):
        code = i.get('componentCode')
        if not code:
            continue
        ladder = sorted((int(p['startNumber']), float(p['productPrice']))
                        for p in (i.get('componentPrices') or [])
                        if p.get('startNumber') is not None and p.get('productPrice') is not None)
        params = [(x.get('attribute_name_en'), x.get('attribute_value_name'))
                  for x in (i.get('attributes') or []) if x.get('attribute_name_en')]
        out.append({'source': 'JLC', 'sku': code, 'mpn': i.get('componentModelEn'),
                    'mfr': i.get('componentBrandEn'), 'package': i.get('componentSpecificationEn'),
                    'desc': i.get('describe') or i.get('erpComponentName') or '',
                    'currency': 'USD',
                    'category': i.get('componentTypeEn') or '',
                    'stock': i.get('stockCount'), 'ladder': ladder, 'params': params,
                    'library': i.get('componentLibraryType'),
                    'moq': i.get('minPurchaseNum'), 'multiple': i.get('leastPatchNumber'),
                    'url': LCSC_PAGE.format(code=code),
                    'datasheet_candidates': [('jlc dataManualUrl', i.get('dataManualUrl')),
                                             ('jlc official', i.get('dataManualOfficialLink'))]})
    return out, pi.get('total', len(out))

def jlc_detail(code, fresh=False):
    """JLC's record for one C-code, or None. Also the only record of a JLC-assembly-
    only part (C408408, C51912672), which lcsc_detail cannot find. Its datasheet
    candidates are often lcsc.com HTML viewers: best_datasheet rejects those."""
    try:
        return next((x for x in jlc_search(code, 3, fresh)[0] if x['sku'] == code), None)
    except Exception:
        return None

def _facet_query(cat_id=None, pkg=None):
    """Request body of JLC's parametric sidebar (captured from jlcpcb.com/parts
    in a browser, 2026-09-23)."""
    return {'baseQueryDto': {'componentBrandList': [], 'packageTypeList': [],
                             'componentSpecificationList': [pkg] if pkg else [],
                             'componentTypeIdList': [cat_id] if cat_id else [],
                             'orderLibraryTypeList': [], 'filterType': None,
                             'productTypeIdList': [], 'keyword': None, 'queryShelveStatus': None},
            'catalogLevel': 2, 'nowCondition': '', 'paramList': [], 'queryString': None}

def jlc_category_ids(fresh=False):
    """{JLC leaf category name: id}, ~850 of them, from one unfiltered facet call
    (2.5 MB, ~2 s), cached 7 days. The per-category facet call needs the id."""
    def go():
        d = http(JLC_FACETS, data=_facet_query(), headers=JLC_HDR, timeout=60)
        pt = ((d or {}).get('data') or {}).get('productTypeAggs') or []
        return {s['name']: int(s['key']) for p in pt for s in p.get('subAggs') or []} or None
    return cached('jlccatids', 'v1', go, fresh, ttl=7 * 24 * 3600) or {}

def jlc_facets(category, pkg=None, fresh=False):
    """{attribute: {value: part count}}: every value JLC's parametric sidebar
    offers across one category (+ exact package). {} for an unknown category."""
    cid = jlc_category_ids(fresh).get(category)
    if cid is None:
        return {}
    def go():
        d = http(JLC_FACETS, data=_facet_query(cid, pkg), headers=JLC_HDR)
        pl = ((d or {}).get('data') or {}).get('paramList')
        return None if pl is None else {p['key']: {v['key']: v.get('docCount') or 0
                                                   for v in p.get('subAggs') or []} for p in pl}
    return cached('jlcfacets', f"{cid}|{pkg}", go, fresh) or {}

def jlc_lib_map(keywords, fresh=False, library=None, pkg=None, category=None,
                cheapest=False, instock=False, budget=1, attrs=None):
    """{C-code: rec} across several keyword queries, 200 rows per call. With a
    price sort, extra pages (up to `budget` calls in total) walk further up the
    price list instead of re-reading relevance-ranked noise."""
    m = {}
    pages = max(1, min(3, budget // max(len(keywords), 1))) if cheapest else 1
    for kw in keywords:
        for pg in range(1, pages + 1):
            try:
                rows, tot = jlc_search(kw, 200, fresh, library=library, pkg=pkg,
                                       category=category, cheapest=cheapest,
                                       instock=instock, page=pg, attrs=attrs)
            except Exception:
                break
            for r in rows:
                m.setdefault(r['sku'], r)
            if len(rows) < 200 or pg * 200 >= (tot or 0):
                break
    return m

def jlc_annotate(recs, jobs=8, fresh=False):
    """Authoritative Basic/Extended per part, one lookup per C-code (cached). The
    keyword map is not reliable for this: JLC keyword relevance drops parts that
    exist in the library, which shows up as a false '-'."""
    todo = [r for r in recs if not r.get('library')]
    if not todo:
        return recs
    def one(r):
        h = jlc_detail(r['sku'], fresh)
        r['library'] = h.get('library') if h else 'none'
        if h:
            r['jcat'] = h.get('category')      # JLC's exact name, for a category pool
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        list(ex.map(one, todo))
    return recs

_EOL_RE = re.compile(r'\bEOL\b|NRND|not recommended|discontinu|obsolete', re.I)

def load_knet(src):
    """kicad-review's netlist parser, if that skill is installed. None if not."""
    try:
        import glob as _g
        here = os.path.dirname(os.path.abspath(__file__))
        cands = [here, os.path.dirname(os.path.abspath(src)), os.getcwd()]
        cands += [os.path.dirname(x) for x in
                  # sibling skill in this repo's layout: code/claude/*/scripts/
                  _g.glob(os.path.join(here, '..', '..', '*', 'scripts', 'kcommon.py')) +
                  _g.glob('/mnt/skills/*/*/scripts/kcommon.py') +
                  _g.glob('/mnt/skills/*/*/kcommon.py')]
        for c in cands:
            if c and c not in sys.path:
                sys.path.insert(0, c)
        import kcommon
        return kcommon.Netlist(src)
    except Exception:
        return None
