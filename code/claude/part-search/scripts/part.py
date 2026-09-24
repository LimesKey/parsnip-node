#!/usr/bin/env python3
"""
part.py - distributor part search and selection for LCSC, JLCPCB and DigiKey.
Stdlib only, no pip install.

Why this exists: LCSC has no public API, its documented-looking endpoints are
Akamai-blocked, and its search accepts NO filter or sort arguments and returns rows
with NO parameters. Everything parametric therefore has to be assembled here. Run
`part.py selftest` first in any new session; it says in one line which providers are
alive, so you never debug endpoints by hand again.

Choosing a part (constraints, ranges, cheapest-first)
  part.py pick MLCC --cap 4.7u..100u --volt '>=25' --pkg 0805 --diel X7R,X5R
  part.py pick MLCC --cap 10u --volt '>=25' --pkg 0805 --basic
  part.py pick 'schottky diode' --vr '>=40' --ifwd '>=1' --pkg SOD-123
  part.py pick TVS --pkg 'SMA(DO-214AC)' --vrwm '>=20' --vc '<=40'
  part.py pick --cat 'I/O Expanders' --pkg TSSOP-16      # explicit category
  part.py pick 'schottky diode' --pkg SOD-123 --fields   # what are the attrs called?
  part.py alt C115844 --qty 100        same category + package, equal-or-better
                                       on every rated parameter, cheaper/stocked

Looking a part up
  part.py show C18164413 C14709        ladder, stock, params, verified datasheet
  part.py ds C42409135                 verified datasheet URL (actually fetched)
  part.py ds C42409135 --save          ...and download it to docs/datasheets/<MPN>.pdf
                                       (--dir DIR elsewhere) + `kdoc.py index` it
  part.py compare C1525 C52923         side-by-side parameter table
  part.py search 'TPS61033'            keyword only; use `pick` for constraints

Costing a board
  part.py bom meshtastic.net --qty 10  LCSC retail price for a whole netlist
  part.py jlc meshtastic.net           JLC Basic vs Extended + $3/line assembly fees
  part.py check meshtastic.net --qty 10  bom + jlc + missing-code triage, one table
  part.py fpcheck meshtastic.net       KiCad footprint + value vs the LCSC part, per part

Constraint grammar (every --flag and every --w NAME=SPEC)
  22uF          equal, numeric: never matches 2.2uF
  4.7u..100u    inclusive range        >=25 <=50 >1k <10   comparison
  X7R,X5R       any-of, numeric-aware  ~ceramic  substring  !X5R  negated
  4u7 100nF 4R7 10k 25V +-20% all parse; 10V~35V takes the first figure.

Attribute shorthands, resolved onto LCSC's real parameter names
  --cap --res --ind --volt --pkg --diel --tol --current --power --freq --temp
  --type --dcr --esr --vr --vf --ifwd --ir --vds --id --vgsth --rdson --isat
  --irms --vrwm --vc --vout --iout   (--capacitance --package --voltage ... also work)
  Anything else: --w 'Voltage - DC Reverse (Vr)=>=40'. `pick` prints a `resolved:`
  line whenever a shorthand mapped to a differently-named attribute - check it.

Useful flags
  --offline      selftest: the offline logic checks only (no network), exit 1 on failure
  --qty N        unit + extended price at that quantity, MOQ/multiple applied
  --sort         price (default) | stock | cap | volt
  --basic        JLC Basic library only, i.e. no $3 Extended line fee
  --fields       list attribute names/values instead of filtering
  --minstock N   pick/alt drop parts with less LCSC stock (default 100)
  --cat TEXT     pick: JLC category filter (unique part of the name); pick
                 infers it from the keyword when one word names one category
  --source       jlc (default: JLC index, server-side package/category filter
                 and price sort) | lcsc (keyword search + detail) | both
  --pool N       max LCSC parts to fetch detail for, --source lcsc (default 240)
  --maxq N       max keyword sub-queries when fanning out a range (default 14)
  --e12          fan a range over E12 preferred values instead of E6
  --attrs        show every parameter, not just the headline ones
  --json         machine-readable output
  --fresh        bypass the disk cache for this call
  --provider lcsc|jlc|digikey|all  (search/show only; pick does not use DigiKey)
  --instock      search: drop zero-stock hits   --anystock  pick: no stock floor
  -n N           result count (default 8)

PRICES COME FROM TWO CATALOGS AND MUST NOT BE ADDED TOGETHER. show/compare/bom/
pick/alt are LCSC retail (pick re-prices its JLC-pooled rows from LCSC). jlc,
pick --basic and the JLC rows in `search` are JLCPCB assembly-catalog prices.
Say which one you are quoting.

DigiKey needs free credentials from developer.digikey.com (create an app, use the
Production "Product Information V4" API). Then:
  export DIGIKEY_CLIENT_ID=...
  export DIGIKEY_CLIENT_SECRET=...
or put them in ~/.config/partsearch/config.json as {"digikey_client_id": "...",
"digikey_client_secret": "..."}. LCSC and JLC need nothing.

Cache: ~/.cache/partsearch (override PARTSEARCH_CACHE), 24 h TTL. Cache hits are
free, so re-querying the same part in a later chat costs nothing.
"""
import sys, os, re, json, time, math, argparse, hashlib, subprocess
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
    if not fresh and os.path.exists(p) and time.time() - os.path.getmtime(p) < ttl:
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

# ---------------------------------------------------------------- rendering

def line_for(r, a):
    nat = r.get('currency') or 'USD'
    qty = a.qty
    if qty:
        q = buy_qty(qty, r.get('moq'), r.get('multiple'))
        up = price_at(r.get('ladder') or [], q)
        pr = f"{dmoney1(up, nat, a)}@{q}" + (f" ={dmoney1(up*q, nat, a)}" if up else '')
    else:
        lad = r.get('ladder') or []
        pr = f"{dmoney1(lad[0][1], nat, a)}@{lad[0][0]}" if lad else 'no price'
    stock = r.get('stock')
    stock = f"{stock:,}" if isinstance(stock, int) else str(stock or '?')
    return (f"{r['source']:<8} {trunc(r.get('sku'),13):<13} {trunc(r.get('mpn'),24):<24} "
            f"{trunc(r.get('mfr'),16):<16} {trunc(r.get('package'),14):<14} "
            f"{stock:>10}  {pr:<18} {trunc(r.get('desc'),46)}")

HEADER = (f"{'src':<8} {'sku':<13} {'mpn':<24} {'mfr':<16} {'package':<14} "
          f"{'stock':>10}  {'price':<18} desc")

def show_full(r, a):
    print(f"\n=== {r.get('mpn')}   [{r['source']} {r.get('sku')}]")
    print(f"  mfr       : {r.get('mfr')}")
    print(f"  desc      : {trunc(r.get('desc'), 200)}")
    print(f"  category  : {r.get('category') or '?'}")
    print(f"  package   : {r.get('package')}   packaging: {r.get('packaging') or '?'}")
    if not getattr(a, 'nojlc', False) and re.fullmatch(r'C\d+', str(r.get('sku') or '')):
        jlc_annotate([r], getattr(a, 'jobs', 8), a.fresh)
        print("  jlc asm   : " + {'base': 'BASIC (no $3 Extended line fee)',
                                  'expand': 'Extended (US$3 one-off line fee)',
                                  'none': 'not in the JLC assembly library'}
              .get(r.get('library'), '?'))
    st = r.get('stock')
    extra = ', '.join(f"{k}={v:,}" for k, v in (r.get('stock_detail') or {}).items()
                      if isinstance(v, int))
    print(f"  stock     : {st:,}" if isinstance(st, int) else f"  stock     : {st}",
          f"({extra})" if extra else '')
    print(f"  moq       : {r.get('moq')}   multiple: {r.get('multiple')}   "
          f"reel: {r.get('reel_qty') or '?'}")
    for k in ('lifecycle', 'rohs', 'eccn'):
        if r.get(k) not in (None, ''):
            print(f"  {k:<10}: {r[k]}")
    lad = r.get('ladder') or []
    nat = r.get('currency') or 'USD'
    if lad:
        print("  price     : " + '  '.join(f"{b}+:{dmoney1(p, nat, a)}" for b, p in lad))
        if nat != a.currency and conv(1, nat, a.currency) is not None:
            print("  native    : " + '  '.join(f"{b}+:{money(p, nat)}" for b, p in lad))
    if a.qty:
        q = buy_qty(a.qty, r.get('moq'), r.get('multiple'))
        up = price_at(lad, q)
        note = '' if q == a.qty else f"  (rounded up from {a.qty} for MOQ/multiple)"
        print(f"  @qty {a.qty:<5}: buy {q} x {dmoney1(up, nat, a)} = "
              f"{dmoney(up*q, nat, a) if up else '?'}{note}")
    ps = r.get('params') or []
    if ps:
        keep = ps if a.attrs else ps[:8]
        print(f"  params    : ({len(ps)} total)" + ('' if a.attrs or len(ps) <= 8 else ' use --attrs for all'))
        for k, v in keep:
            print(f"      {trunc(k,28):<28} {trunc(v,60)}")
    print(f"  page      : {r.get('url')}")
    if not a.nods:
        url, tried = best_datasheet(r)
        if url:
            print(f"  datasheet : {url}   VERIFIED")
        else:
            print("  datasheet : NONE VERIFIED")
        for label, u, ok, note in tried:
            if not ok:
                print(f"      failed  {label}: {note}\n              {trunc(u,110)}")

# ---------------------------------------------------------------- commands

def c_selftest(a):
    try:
        off = f"OK    pick/alt parsing {_pick_selftest()}, fpcheck matcher {_fpcheck_selftest()}"
    except AssertionError as e:
        off = f"FAIL  {e}"
    if a.offline:
        print(f"  offline       {off}")
        return 1 if off.startswith('FAIL') else 0
    print(f"  offline       {off}\n\nprovider endpoint status\n")
    d = http(LCSC_DETAIL.format(code='C1525'))
    ok = bool((d or {}).get('result'))
    print(f"  LCSC detail   {'OK  ' if ok else 'DEAD'}  {LCSC_DETAIL.format(code='C1525')}")
    if ok:
        print(f"                -> {d['result']['productModel']} / {d['result']['brandNameEn']}")
    s, tot = lcsc_search('TPS61033', 3, fresh=True)
    print(f"  LCSC search   {'OK  ' if s else 'DEAD'}  {LCSC_SEARCH}  ({len(s)} hits)")
    if s:
        print(f"                -> {s[0]['sku']} {s[0]['mpn']}")
    j, _ = jlc_search('', 3, fresh=True, category='MOSFETs', pkg='SOT-23', cheapest=True)
    print(f"  JLC search    {'OK  ' if j else 'DEAD'}  {JLC_SEARCH.split('/api/')[0]}/...selectSmtComponentList"
          f"  (pick's pool; category+package+price filters {'live' if j else 'NOT answering'})")
    fac = jlc_facets('MOSFETs', 'SOT-23', fresh=True)
    ja, _ = jlc_search('', 3, fresh=True, category='MOSFETs', pkg='SOT-23',
                       attrs=[{'Drain to Source Voltage': ['30V']}])
    ok4 = bool(fac.get('Drain to Source Voltage')) and bool(ja) and all(
        ('Drain to Source Voltage', '30V') in r['params'] for r in ja)
    print(f"  JLC facets    {'OK  ' if ok4 else 'DEAD'}  {JLC_FACETS.split('/api/')[0]}/...filterComponentAttribute"
          f"  (pick's server-side attribute filter; {len(fac)} MOSFET attributes"
          f"{', value filter honoured' if ok4 else ', attribute filter NOT honoured - pick falls back to local filtering'})")
    tok, note = dk_token(a.fresh)
    print(f"  DigiKey auth  {'OK  ' if tok else 'n/a '}  {note}")
    if tok:
        r, note2 = dk_search('TPS61033', 3, a)
        print(f"  DigiKey srch  {'OK  ' if r else 'DEAD'}  {note2}")
        if r:
            print(f"                -> {r[0]['sku']} {r[0]['mpn']}")
    ok2, note3 = verify_pdf('https://datasheet.lcsc.com/datasheet/pdf/'
                            '02336ea48ea44ca18c72517dd3cb7b47.pdf')
    print(f"  datasheet chk {'OK  ' if ok2 else 'DEAD'}  {note3}")
    fx = fx_rates(fresh=True)
    cad = (fx or {}).get('rates', {}).get('CAD')
    print(f"  fx rates      {'OK  ' if cad else 'DEAD'}  "
          + (f"1 USD = {cad:.4f} CAD ({fx['src']}, {fx['date']})" if cad else
             'both fx endpoints unreachable; prices shown native-only'))
    print(f"\n  cache dir     {CACHE}")
    print("\nif LCSC shows DEAD, the endpoint moved: re-probe from a browser devtools\n"
          "network tab on lcsc.com and update LCSC_DETAIL / LCSC_SEARCH at the top of this file.")

def resolve(spec, a):
    """C-code -> LCSC detail. Anything else -> best search hit per provider."""
    if re.fullmatch(r'C\d+', spec.strip().upper()):
        r = lcsc_detail(spec, a.fresh)
        return [r] if r else []
    m = re.search(r'_(C\d+)\.html', spec)
    if m:
        r = lcsc_detail(m.group(1), a.fresh)
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

def c_search(a):
    rows, notes = [], []
    kw = ' '.join(a.args)
    if a.provider in ('all', 'lcsc'):
        r, tot = lcsc_search(kw, a.n * (4 if a.instock else 1), a.fresh)
        if a.instock:
            r = [x for x in r if (x.get('stock') or 0) > 0][:a.n]
        rows += r
        notes.append(f"LCSC {len(r)}/{tot}" + (' in stock only' if a.instock else ''))
    if a.provider in ('all', 'jlc'):
        # LCSC ranks on MPN text, so category words ('test point', 'LDO') come back
        # as junk. JLC's index matches category names and carries a spec string.
        r, tot = jlc_search(kw, a.n, a.fresh, instock=a.instock)
        have = {x['sku'] for x in rows}
        r = [x for x in r if x['sku'] not in have]
        rows += r
        notes.append(f"JLC {len(r)}/{tot}")
    if a.provider in ('all', 'digikey'):
        r, note = dk_search(kw, a.n * (3 if a.instock else 1), a)
        if a.instock:
            r = [x for x in r if (x.get('stock') or 0) > 0][:a.n]
        rows += r
        notes.append(f"DigiKey {len(r)} ({note})")
    if a.json:
        print(json.dumps(rows, indent=1)); return
    print(f"search: {kw}    [{'; '.join(notes)}]\n")
    print(HEADER)
    for r in rows:
        print(line_for(r, a))
    if not rows:
        print("  no hits. try the bare MPN without package/qualifier words, or run "
              "`part.py selftest`")
    else:
        print("\n`part.py show <sku>` for price ladder, params and a verified datasheet"
              + ("\nJLC rows show the JLC assembly-catalog price; `show` gives LCSC retail"
                 if any(r['source'] == 'JLC' for r in rows) else ''))

def c_show(a):
    recs, bad = [], False
    for spec in a.args:
        got = resolve(spec, a)
        if not got:
            print(f"{spec}: not found. C-codes must exist on LCSC; for an MPN try "
                  f"`search` first, or `selftest` if every lookup is failing")
            bad = True
            continue
        recs += got
    if a.json:
        for r in recs:
            if not a.nods:
                r['datasheet'], _ = best_datasheet(r)
        print(json.dumps(recs, indent=1)); return
    if a.table:
        if not recs:
            return 1 if bad else 0
        print(HEADER)
        for r in recs:
            print(line_for(r, a))
        return 1 if bad and not recs else 0
    for r in recs:
        show_full(r, a)
    return 1 if bad and not recs else 0

def c_ds(a):
    for spec in a.args:
        for r in resolve(spec, a) or []:
            url, tried = best_datasheet(r)
            print(f"{r.get('mpn')} [{r['source']} {r.get('sku')}]")
            for label, u, ok, note in tried:
                print(f"  {'OK    ' if ok else 'BROKEN'} {label}: {note}")
                print(f"         {u}")
            print(f"  -> {url or 'no working datasheet; product page: ' + str(r.get('url'))}\n")
            if a.save and url:
                save_datasheet(r, url, a)

def save_datasheet(r, url, a):
    """Fetch the whole verified PDF, file it as <dir>/<MPN>.pdf and index it with
    kicad-review's kdoc.py, so pinout / abs-max checks go straight to
    `kdoc.py grep -d <MPN>` instead of a hand curl + pdftotext."""
    d = a.dir or ('docs/datasheets' if os.path.isdir('docs/datasheets') else '.')
    name = re.sub(r'[^\w.+-]+', '_', r.get('mpn') or r.get('sku') or 'datasheet').strip('_') + '.pdf'
    out = os.path.join(d, name)
    if os.path.exists(out) and not a.fresh:
        print(f"  saved     : {out} already exists (--fresh to re-download)")
    else:
        blob, status, _ = http('https:' + url if url.startswith('//') else url,
                               raw=True, retries=1, timeout=60)
        if status not in (200, 206) or blob[:4] != b'%PDF':
            print(f"  saved     : NOT saved - full fetch gave HTTP {status}, "
                  f"{'no %PDF header' if blob else 'empty body'}")
            return
        os.makedirs(d, exist_ok=True)
        with open(out, 'wb') as fh:
            fh.write(blob)
        print(f"  saved     : {out}  ({len(blob) // 1024} KiB)")
    import glob as _g
    here = os.path.dirname(os.path.abspath(__file__))
    kdoc = next(iter(_g.glob(os.path.join(here, '..', '..', '*', 'scripts', 'kdoc.py')) +
                     _g.glob('/mnt/skills/*/*/scripts/kdoc.py')), None)
    if not kdoc:
        print("  indexed   : kdoc.py not found; `kdoc.py index` it by hand"); return
    p = subprocess.run([sys.executable, kdoc, 'index', out], capture_output=True, text=True)
    print(f"  indexed   : {(p.stdout.strip() or p.stderr.strip())[:120]}")

def c_compare(a):
    recs = []
    for spec in a.args:
        recs += resolve(spec, a)[:1]
    if not recs:
        print("nothing to compare"); return
    if a.json:
        print(json.dumps(recs, indent=1)); return
    keys, seen = [], set()
    for r in recs:
        for k, _ in (r.get('params') or []):
            if k and k not in seen:
                seen.add(k); keys.append(k)
    w = max(18, min(30, max((len(str(k)) for k in keys), default=18)))
    cols = [trunc(r.get('mpn'), 22) for r in recs]
    print(f"{'':<{w}} " + ' '.join(f"{c:<24}" for c in cols))
    def row(label, vals):
        print(f"{trunc(label,w):<{w}} " + ' '.join(f"{trunc(v,23):<24}" for v in vals))
    row('source/sku', [f"{r['source']} {r.get('sku')}" for r in recs])
    row('manufacturer', [r.get('mfr') for r in recs])
    row('package', [r.get('package') for r in recs])
    row('stock', [f"{r.get('stock'):,}" if isinstance(r.get('stock'), int) else r.get('stock') for r in recs])
    q = a.qty or 1
    row(f'unit @{q}', [dmoney1(price_at(r.get('ladder') or [],
                                        buy_qty(q, r.get('moq'), r.get('multiple'))),
                               r.get('currency') or 'USD', a) for r in recs])
    row('moq / mult', [f"{r.get('moq')} / {r.get('multiple')}" for r in recs])
    row('lifecycle', [r.get('lifecycle') for r in recs])
    print()
    for k in keys:
        vals = []
        for r in recs:
            d = {kk: vv for kk, vv in (r.get('params') or [])}
            vals.append(d.get(k, '-'))
        if len({str(v) for v in vals}) > 1 or a.attrs:
            row(k, vals)
    print("\n(only differing parameters shown; --attrs for all)")

CODE_RE = re.compile(r'\bC\d{3,}\b')

def c_bom(a):
    src = a.args[0] if a.args else '-'
    txt = sys.stdin.read() if src == '-' else open(src, encoding='utf-8', errors='replace').read()
    want, nocode = {}, []
    if src.endswith('.net'):
        parsed = False
        nl = load_knet(src)        # prefer kcommon.py's real S-expression parser
        if nl is not None:
            for ref, c in nl.comps.items():
                if c['dnp'] or not c['in_bom']:
                    continue
                if re.fullmatch(r'C\d+', c['lcsc'] or ''):
                    want.setdefault(c['lcsc'], []).append(ref)
                elif c['prefix'] not in ('H', 'TP'):
                    nocode.append(ref)
            parsed = True
        else:
            print("(kcommon.py not importable, falling back to regex)", file=sys.stderr)
        if not parsed:
            for m in re.finditer(r'\(comp\s+\(ref "([^"]+)"\)(.*?)(?=\(comp\s+\(ref|\(libparts)', txt, re.S):
                ref, blob = m.group(1), m.group(2)
                if '"dnp"' in blob:
                    continue
                cm = re.search(r'\(name "LCSC(?: Part)?"\)\s*\(value "(C\d+)"\)', blob)
                if cm:
                    want.setdefault(cm.group(1), []).append(ref)
                else:
                    nocode.append(ref)
    else:
        for line in txt.splitlines():
            m = CODE_RE.search(line)
            if m:
                n = re.search(r'\bx?(\d+)\s*$', line.strip())
                want.setdefault(m.group(0), []).extend(['?'] * (int(n.group(1)) if n else 1))
    if not want:
        print("no LCSC codes found. netlist needs an 'LCSC Part' property, or pass a "
              "text file with one C-code per line"); return
    boards = a.qty or 1
    rows, total, missing = [], 0.0, []
    for code, refs in sorted(want.items()):
        r = lcsc_detail(code, a.fresh)
        if not r:
            missing.append(code); continue
        per = len(refs)
        need = per * boards
        q = buy_qty(need, r.get('moq'), r.get('multiple'))
        up = price_at(r.get('ladder') or [], q)
        ext = (up or 0) * q
        total += ext
        rows.append({'lcsc': code, 'mpn': r.get('mpn'), 'refs': refs, 'per_board': per,
                     'need': need, 'buy': q, 'unit': up, 'ext': ext,
                     'stock': r.get('stock'), 'package': r.get('package')})
    if a.json:
        print(json.dumps({'boards': boards, 'lines': rows, 'total_usd': round(total, 4),
                          'total_display': {'currency': a.currency,
                                            'amount': round(conv(total, 'USD', a.currency) or total, 4)},
                          'unresolved': missing, 'no_lcsc_code': sorted(nocode)}, indent=1)); return
    print(f"{boards} board(s), {len(rows)} distinct LCSC parts\n")
    print(f"{'lcsc':<12} {'mpn':<26} {'pkg':<12} {'/bd':>4} {'buy':>7} {'unit':>10} "
          f"{'ext':>9}  {'stock':>10}  refs")
    for r in sorted(rows, key=lambda x: -x['ext']):
        low = '  LOW STOCK' if isinstance(r['stock'], int) and r['stock'] < r['buy'] else ''
        print(f"{r['lcsc']:<12} {trunc(r['mpn'],26):<26} {trunc(r['package'],12):<12} "
              f"{r['per_board']:>4} {r['buy']:>7} {dmoney1(r['unit'],'USD',a):>10} "
              f"{dmoney1(r['ext'],'USD',a):>9}  "
              f"{r['stock']:>10,}  {' '.join(r['refs'][:6])}{low}")
    ct = conv(total, 'USD', a.currency)
    if a.currency != 'USD' and ct is not None:
        _FX['used'] = True
        print(f"\ntotal parts cost: {money(ct, a.currency)} ({money(total,'USD')}) "
              f"for {boards} board(s)  ({money(ct/boards, a.currency)}/board)")
    else:
        print(f"\ntotal parts cost: {money(total,'USD')} for {boards} board(s)  "
              f"({money(total/boards,'USD')}/board)")
    print("excludes DNP parts, shipping, tax, PCB and assembly")
    if missing:
        print(f"unresolved LCSC codes: {' '.join(missing)}")
    if nocode:
        print(f"\n{len(nocode)} placed component(s) have NO LCSC part number and are not "
              f"costed here:\n  {' '.join(sorted(nocode))}")

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
        vals = [v for v in facets[pname] if v not in ('', '-') and pred(v)]
        counts[name] = sum(facets[pname][v] for v in vals)
        if vals:
            attrs.append({pname: vals})
        else:
            unmet.append(name)
    return attrs, counts, unmet

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
        try:
            hits, _ = jlc_search(r['sku'], 3, fresh)
        except Exception:
            return
        h = next((x for x in hits if x['sku'] == r['sku']), None)
        r['library'] = h.get('library') if h else 'none'
        if h:
            r['jcat'] = h.get('category')      # JLC's exact name, for a category pool
    with cf.ThreadPoolExecutor(max_workers=jobs) as ex:
        list(ex.map(one, todo))
    return recs

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
    return f"{x:g}{unit}"

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
                def f(v, alts=alts):
                    # _norm() drops the decimal point, so '2.2uF' and '22uF' collapse
                    # to the same string. Anything numeric MUST compare numerically and
                    # must not fall through to the text branch.
                    x = enum(v)
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
        hits = _cat_hits(a.cat, categories(a.fresh))
        exact = [c for c in hits if c.lower() == a.cat.lower()]
        if len(exact or hits) > 1:
            sys.exit(f"--cat {a.cat!r} matches {len(hits)} categories:\n  " + '\n  '.join(hits[:20]))
        return (exact or hits or [a.cat])[0]
    text = ' '.join(a.args).strip()
    if not text:
        return None
    cats = categories(a.fresh)
    whole = _cat_hits(text, cats)
    if len(whole) == 1:
        return whole[0]
    # several: the one that IS the keyword ('MOSFET' -> MOSFETs, not the SiC
    # one), else the only one that has --pkg at all (MLCC 0805 -> SMD, not Leaded)
    same = [c for c in whole if c.lower().rstrip('s') == text.lower().rstrip('s')]
    if len(same) == 1:
        return same[0]
    pkg = a.pkg if a.pkg and not re.search(r'[,~!<>]|\.\.', a.pkg) else None
    if pkg and 1 < len(whole) <= 6:
        stocked = [c for c in whole if jlc_facets(c, pkg, a.fresh)]
        if len(stocked) == 1:
            return stocked[0]
    uniq = {h[0] for w in text.split() if len(w) >= 3 for h in [_cat_hits(w, cats)] if len(h) == 1}
    return uniq.pop() if len(uniq) == 1 else None

def _pkg_literal(cons):
    """The --pkg value when it names one exact package (JLC filters that server-side)."""
    lab = next((str(lab) for n, lab, _ in cons if n == 'pkg'), '')
    return lab if lab and not re.search(r'[,~!<>]|\.\.', lab) else None

def _stock_ok(rows, a):
    """Rows with stock >= --minstock; counts the rest for the pick header."""
    keep = [r for r in rows if (r.get('stock') or 0) >= a.minstock]
    a._lowstock = getattr(a, '_lowstock', 0) + len(rows) - len(keep)
    return keep

_EOL_RE = re.compile(r'\bEOL\b|NRND|not recommended|discontinu|obsolete', re.I)

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
           r'|^frequency$|load capacitance|colou?r'),
    ('skip', r'breakdown|temperature|feature|capacitance|charge|surge|ciss|coss|crss'),
    ('le', r'rds|resistance|dcr|esr|forward(?!.*current)|leakage|clamping|quiescent'
           r'|supply current|dropout|threshold|tolerance|stability'),
    ('ge', r'voltage|current|power|dissipation|vgs'),
)

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
    # numeric limits first (they become the table columns), secondary ratings last
    holds.sort(key=lambda h: (h[1][:1] not in '<>',
                              bool(re.search(r'dissipation|^vgs$', h[0].lower()))))
    a.w = list(a.w) + [f"{n}={s}" for n, s in holds]
    jlc_annotate([b], a.jobs, a.fresh)
    cat = (b.get('category') or '').split('>')[-1].strip()
    a._jcat = b.get('jcat')
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

def c_jlc(a):
    """Basic vs Extended for C-codes already chosen. Extended costs $3 USD per unique
    line on a JLC assembly order, so this is the fee-triage command."""
    codes, refs = [], {}
    for x in a.args:
        if x.endswith('.net') and os.path.exists(x):
            # A bare regex would take capacitor refdes C108/C221 for LCSC codes. Only
            # the "LCSC Part" property is a code, so parse rather than pattern-match.
            nl = load_knet(x)
            if nl is None:
                print(f"jlc: could not parse {x} (kcommon.py not importable)"); return 1
            for ref, c in nl.comps.items():
                if c['dnp'] or not c['in_bom']:
                    continue
                if re.fullmatch(r'C\d+', c['lcsc'] or ''):
                    codes.append(c['lcsc'])
                    refs.setdefault(c['lcsc'], []).append(ref)
        elif os.path.exists(x):
            codes += re.findall(r'\bC\d{3,}\b', open(x, encoding='utf8', errors='ignore').read())
        else:
            codes += re.findall(r'\bC\d{3,}\b', x.upper())
    codes = list(dict.fromkeys(codes))
    if not codes:
        print("jlc: give C-codes or a file containing them"); return 1
    qty = a.qty or 1
    ext = 0
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        res = list(ex.map(lambda c: (c, jlc_search(c, 3, a.fresh)[0]), codes))
    rf = lambda c: (' ' + trunc(','.join(refs[c]), 22)) if refs.get(c) else ''
    print(f"{'code':<11} {'lib':<6} {'stock':>10}  {'unit@'+str(qty):<12} {'part':<28} note")
    for code, hits in res:
        h = next((x for x in hits if x['sku'] == code), None)
        if not h:
            print(f"{code:<11} {'-':<6} {'-':>10}  {'-':<12} {'':<28} "
                  f"not in JLC assembly library{rf(code)}")
            continue
        if h.get('library') == 'expand':
            ext += 1
        up = price_at(h.get('ladder') or [], buy_qty(qty, h.get('moq'), h.get('multiple')))
        st = h.get('stock')
        print(f"{code:<11} {('BASIC' if h.get('library')=='base' else 'ext'):<6} "
              f"{(f'{st:,}' if isinstance(st,int) else '?'):>10}  "
              f"{dmoney1(up,'USD',a):<12} {trunc(h.get('mpn'),28):<28} "
              f"{trunc(h.get('desc'),30)}{rf(code)}")
    if ext:
        print(f"\n  {ext} Extended line(s) -> {dmoney1(3.0*ext,'USD',a)} in JLC setup fees "
              f"(US$3 per unique Extended part, charged once per order)")
    return 0

def c_check(a):
    """One-shot sourcing triage for a whole board: LCSC pricing + JLC Basic/
    Extended status + missing-LCSC-code triage in a single table and a single
    pass over the netlist, instead of running `bom` then `jlc` separately and
    cross-referencing the two by eye. Same 'refuse to regex a .net' rule as
    `jlc`: a bare C\\d+ scan would mistake capacitor refdes (C108) for LCSC
    codes, so this needs knet.py to parse the 'LCSC Part' property properly."""
    if not a.args or not a.args[0].endswith('.net'):
        print("check: give a .net file, e.g. part.py check board.net --qty 5"); return 1
    src = a.args[0]
    nl = load_knet(src)
    if nl is None:
        print(f"check: could not parse {src} (kcommon.py not importable)"); return 1
    want, nocode = {}, []
    for ref, c in nl.comps.items():
        if c['dnp'] or not c['in_bom']:
            continue
        if re.fullmatch(r'C\d+', c['lcsc'] or ''):
            want.setdefault(c['lcsc'], []).append(ref)
        elif c['prefix'] not in ('H', 'TP'):
            nocode.append(ref)
    if not want:
        print("no LCSC codes found. netlist needs an 'LCSC Part' property"); return 1
    boards = a.qty or 1

    def one(item):
        code, refs = item
        r = lcsc_detail(code, a.fresh)
        jhits, _ = jlc_search(code, 3, a.fresh)
        jh = next((x for x in jhits if x['sku'] == code), None)
        return code, refs, r, jh
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(one, sorted(want.items())))

    rows, total, missing, ext_n = [], 0.0, [], 0
    for code, refs, r, jh in results:
        if not r:
            missing.append(code); continue
        per = len(refs)
        need = per * boards
        q = buy_qty(need, r.get('moq'), r.get('multiple'))
        up = price_at(r.get('ladder') or [], q)
        ext = (up or 0) * q
        total += ext
        lib = jh.get('library') if jh else None
        if lib == 'expand':
            ext_n += 1
        rows.append({'lcsc': code, 'mpn': r.get('mpn'), 'refs': refs, 'per_board': per,
                     'need': need, 'buy': q, 'unit': up, 'ext': ext, 'stock': r.get('stock'),
                     'package': r.get('package'),
                     'jlc': {'base': 'BASIC', 'expand': 'ext'}.get(lib, 'not in JLC lib' if jh else '?')})
    if a.json:
        print(json.dumps({'boards': boards, 'lines': rows,
                          'total_parts_usd': round(total, 4),
                          'extended_line_fee_usd': round(3.0 * ext_n, 2),
                          'unresolved': missing, 'no_lcsc_code': sorted(nocode)}, indent=1))
        return
    print(f"{boards} board(s), {len(rows)} distinct LCSC parts\n")
    print(f"{'lcsc':<12} {'mpn':<24} {'pkg':<10} {'/bd':>4} {'buy':>7} {'unit':>10} "
          f"{'ext':>9}  {'stock':>9}  {'jlc':<14} refs")
    for r in sorted(rows, key=lambda x: -x['ext']):
        low = '  LOW STOCK' if isinstance(r['stock'], int) and r['stock'] < r['buy'] else ''
        st = r['stock']
        print(f"{r['lcsc']:<12} {trunc(r['mpn'],24):<24} {trunc(r['package'],10):<10} "
              f"{r['per_board']:>4} {r['buy']:>7} {dmoney1(r['unit'],'USD',a):>10} "
              f"{dmoney1(r['ext'],'USD',a):>9}  "
              f"{(f'{st:,}' if isinstance(st,int) else '?'):>9}  {r['jlc']:<14} "
              f"{' '.join(r['refs'][:6])}{low}")
    ct = conv(total, 'USD', a.currency)
    if a.currency != 'USD' and ct is not None:
        _FX['used'] = True
        print(f"\nparts cost: {money(ct, a.currency)} ({money(total,'USD')}) for {boards} board(s)")
    else:
        print(f"\nparts cost: {money(total,'USD')} for {boards} board(s)")
    if ext_n:
        print(f"assembly  : {ext_n} Extended line(s) -> {dmoney1(3.0*ext_n,'USD',a)} in JLC "
              f"setup fees (US$3/unique Extended part, charged once per order)")
    print("excludes shipping, tax, PCB fabrication and DNP parts")
    if missing:
        print(f"\nunresolved LCSC codes: {' '.join(missing)}")
    if nocode:
        print(f"\n{len(nocode)} placed component(s) have NO LCSC part number - not costed "
              f"and cannot go to JLC assembly:\n  {' '.join(sorted(nocode))}")
    return 0

# ============================================================ footprint check
# Cross-check the KiCad footprint chosen on each symbol against LCSC's package
# ('encapStandard') for that part number. Chip sizes and matching leaded
# families decide themselves; only genuinely divergent naming needs the alias
# table; the rest go to REVIEW for a human/LLM to eyeball - by design a small
# minority. Conservative on purpose: only OK on a high-confidence rule, only
# MISMATCH when two concrete different sizes/lead-counts can be named, else
# REVIEW. A false REVIEW is a cheap glance; a silent false OK is a board bug.
# Names below are ground-truthed against KiCad 10 stock libraries.

_CHIP_SIZES = {'01005','0201','0402','0603','0805','1008','1206','1210',
               '1218','1806','1812','2010','2225','2512','1225'}
# imperial chip codes as they appear inside a KiCad footprint name
_CHIP_RE = re.compile(r'(?<!\d)(01005|0201|0402|0603|0805|1008|1206|1210|1218'
                      r'|1806|1812|2010|2225|2512|1225)(?!\d)')

# LCSC package (normalised, UPPER) -> token that should appear in the KiCad
# footprint name. ONLY families whose two naming systems genuinely diverge.
# Grow this from the REVIEW bucket, one line per newly-seen divergence.
_PKG_ALIAS = {
    'DO-214AC': 'SMA', 'DO-214AA': 'SMB', 'DO-214AB': 'SMC',
    'DO-215AA': 'SMB', 'DO-215AB': 'SMC',
    'SC-70': 'SOT-323', 'SC-70-5': 'SOT-353', 'SC-70-6': 'SOT-363',
    # tantalum EIA case letter (LCSC) -> EIA metric body (KiCad CP_EIA-####)
    'CASE-A': 'EIA-3216', 'CASE-B': 'EIA-3528',
    'CASE-C': 'EIA-6032', 'CASE-D': 'EIA-7343',
}
_FAM_DEFAULT = {'SOT-23': 3}          # bare 'SOT-23' means the 3-lead body

def _fp_base(fp):
    """'Package_TO_SOT_SMD:SOT-23-5_HandSoldering' -> 'SOT-23-5' (UPPER)."""
    name = re.sub(r'\.kicad_mod$', '', fp.split(':')[-1], flags=re.I)
    while True:                       # peel stacked variant tails
        new = re.sub(r'_(?:Hand[-_ ]?Sold\w*|Pad[0-9x.]+mm|Modified'
                     r'|ThermalVias|Mask[0-9x.]+mm|MountingHoles?)$', '',
                     name, flags=re.I)
        if new == name:
            return name.upper()
        name = new

def _fam(tok):
    """('SOT-23',5) for SOT-23-5; ('SOT-23',None) for SOT-23; ('SOIC',8) for
    SOIC-8; ('QFN',32) for QFN-32-.... U/V/T/W/P-QFN all canonicalise to plain
    QFN: those lead-in letters are body-THICKNESS/finish marketing variants
    (JEDEC/vendor nomenclature, e.g. ON Semi's QFN/VQFN/TQFN/UQFN/WQFN guide),
    never a land-pattern difference for the same lead count and body size - so
    unifying them cannot hide a real footprint bug the way merging unrelated
    lead-frame families (DFN/SON vs QFN) could. None if not a recognised
    leaded family."""
    m = re.match(r'^([UVTWP]?QFN|SOT-\d+|SC-\d+|SOIC|SO|TSSOP|HTSSOP|SSOP|VSSOP'
                 r'|MSOP|TSOP|DFN|WSON|TQFP|LQFP|QFP|PSOP|HSOP|SOP)'
                 r'[-_]?(\d+)?', tok)
    if not m:
        return None
    fam = 'QFN' if re.fullmatch(r'[UVTWP]?QFN', m.group(1)) else m.group(1)
    return (fam, int(m.group(2)) if m.group(2) else None)

def _bcontains(hay, needle):
    """needle sits in hay on a token boundary and is NOT immediately followed by
    a lead-count digit (so 'SOT-23' does not match 'SOT-23-5')."""
    for m in re.finditer(re.escape(needle), hay):
        i, j = m.start(), m.end()
        before = i == 0 or not hay[i-1].isalnum()
        nxt = hay[j] if j < len(hay) else ''
        bad = nxt.isdigit() or (nxt in '-_' and j+1 < len(hay) and hay[j+1].isdigit())
        if before and not bad:
            return True
    return False

def _fp_sig(fp):
    """A body signature so 'R_0402' and 'R_0402_HandSolder' compare equal (same
    body, different pad), while 0402 vs 0603 differ. Used to decide whether one
    LCSC code really sits on two *different* bodies."""
    k = _fp_base(fp)
    m = _CHIP_RE.search(k)
    if m:
        return ('chip', m.group(1))
    return _fam(k) or ('name', k)

def _fp_match(pkg, fp, mpn=None):
    """-> ('ok'|'mismatch'|'review', reason)."""
    p = (pkg or '').strip().upper()
    if not fp:
        return ('review', 'no footprint set on the symbol')
    k = _fp_base(fp)

    # 0) footprint is named after the MPN (connectors, modules, MPN-specific ICs).
    # Drop LCSC's trailing packaging qualifiers first: 'S4B-XH-SM4-TB(LF)(SN)'.
    if mpn:
        mn = re.sub(r'[^A-Z0-9]', '', re.sub(r'\(.*', '', mpn.upper()))
        kn = re.sub(r'[^A-Z0-9]', '', k)
        if (len(mn) >= 6 and mn in kn) or (len(kn) >= 8 and kn in mn):
            return ('ok', f'footprint named for MPN {mpn}')

    if not p:
        return ('review', 'LCSC has no package field')

    if p in _CHIP_SIZES:                          # 1) two-terminal chip size
        found = set(_CHIP_RE.findall(k))
        if p in found:
            return ('ok', p)
        if found:
            return ('mismatch', f'LCSC {p} vs KiCad {"/".join(sorted(found))}')
        return ('review', f'LCSC size {p}, KiCad name has no chip size: {k}')

    canon = _PKG_ALIAS.get(p, p)
    pf, kf = _fam(canon), _fam(k)                 # 2) lead-count-aware family
    if pf and kf and pf[0] == kf[0]:
        pp = pf[1] if pf[1] is not None else _FAM_DEFAULT.get(pf[0])
        kp = kf[1] if kf[1] is not None else _FAM_DEFAULT.get(kf[0])
        if pp is None or kp is None or pp == kp:
            return ('ok', f'{canon} ~ {k}')
        return ('mismatch', f'LCSC {p} ({pp}-lead) vs KiCad {k} ({kp}-lead)')

    if _bcontains(k, canon) or _bcontains(canon, k):   # 3) boundary substring
        return ('ok', f'{canon} ~ {k}')

    return ('review', f'LCSC "{pkg}"  vs  KiCad "{fp.split(":")[-1]}"')  # 4) human

def _val_check(prefix, sym_value, params):
    """Symbol Value vs LCSC's resistance/capacitance/inductance parameter.
    -> ('ok'|'mismatch'|'skip', reason). 'skip' when either side isn't a single
    parseable value (ICs, arrays, 0-ohm with no param, missing attribute) - the
    conservative default, so only a real value disagreement is ever flagged."""
    attr = {'R': 'res', 'C': 'cap', 'L': 'ind'}.get(prefix)
    if not attr:
        return ('skip', '')
    sv, lv_raw = enum(sym_value), attr_of(params, attr)
    lv = enum(lv_raw)
    if sv is None or lv is None:
        return ('skip', '')
    unit = _UNIT_OF.get(attr, '')
    if sv == 0 or lv == 0:                       # 0-ohm jumper etc: exact only
        if sv == lv:
            return ('ok', '')
        return ('mismatch', f'value: sym {sym_value} vs LCSC {lv_raw}')
    if abs(sv - lv) <= 0.02 * max(abs(sv), abs(lv)):    # 2% absorbs 470nF==0.47uF
        return ('ok', '')
    return ('mismatch', f'value: sym {fmt_si(sv, unit)} vs LCSC part {fmt_si(lv, unit)}')

def _fpcheck_selftest():
    cases = [
        ('0402', 'Resistor_SMD:R_0402_1005Metric', 'ok'),
        ('0402', 'Resistor_SMD:R_0402_1005Metric_Pad0.72x0.64mm_HandSolder', 'ok'),
        ('0805', 'Capacitor_SMD:C_1206_3216Metric', 'mismatch'),
        ('SOT-23', 'Package_TO_SOT_SMD:SOT-23', 'ok'),
        ('SOT-23', 'Package_TO_SOT_SMD:SOT-23-5', 'mismatch'),
        ('SOT-23-5', 'Package_TO_SOT_SMD:SOT-23-5', 'ok'),
        ('SOT-23-5', 'Package_TO_SOT_SMD:SOT-23', 'mismatch'),
        ('SOD-123', 'Diode_SMD:D_SOD-123', 'ok'),
        ('SOD-323', 'Diode_SMD:D_SOD-323', 'ok'),
        ('DO-214AC', 'Diode_SMD:D_SMA', 'ok'),
        ('SOIC-8', 'Package_SO:SOIC-8_3.9x4.9mm_P1.27mm', 'ok'),
        ('TSSOP-16', 'Package_SO:TSSOP-16_4.4x5mm_P0.65mm', 'ok'),
        ('QFN-32', 'Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm', 'ok'),
        # QFN thickness/finish prefixes (V/T/U/W/P) are marketing variants of
        # the same land pattern, not a footprint difference - SKILL-BACKLOG.md
        ('WQFN-24', 'Package_DFN_QFN:VQFN-24_4x4mm_P0.5mm', 'ok'),
        ('TQFN-16', 'Package_DFN_QFN:QFN-16-1EP_3x3mm_P0.5mm', 'ok'),
        ('UQFN-20', 'Package_DFN_QFN:WQFN-20-1EP_4x4mm_P0.5mm', 'ok'),
        ('WQFN-24', 'Package_DFN_QFN:QFN-32-1EP_5x5mm_P0.5mm', 'mismatch'),  # lead count still enforced
        ('SC-70', 'Package_TO_SOT_SMD:SOT-323_SC-70', 'ok'),
        ('', 'Diode_SMD:D_SOD-123', 'review'),
        ('WEIRD-99', 'Foo:Bar_XYZ', 'review'),
        # footprint named for the MPN clears even when packages read differently
        ('SMD,P=2.5mm', 'Connector:JST_XH_S4B-XH-SM4-TB_1x04-1MP', 'ok', 'S4B-XH-SM4-TB(LF)(SN)'),
        ('SMD,25.5x18mm', 'RF:ESP32-S3-WROOM-1', 'ok', 'ESP32-S3-WROOM-1-N16R8'),
        # divergent package nomenclature stays in review (the real 5%)
        ('DFN-8(3x3)', 'Package_DFN_QFN:PQFN-8_L3.1-W3.1', 'review', 'AON7534'),
    ]
    for c in cases:
        pkg, fp, want = c[0], c[1], c[2]
        mpn = c[3] if len(c) > 3 else None
        got = _fp_match(pkg, fp, mpn)[0]
        assert got == want, f"{pkg!r} vs {fp!r}: got {got}, want {want}"
    vcases = [
        ('C', '10µ', [('Capacitance', '10µF')], 'ok'),
        ('C', '10µ', [('Capacitance', '1µF')], 'mismatch'),
        ('R', '113k', [('Resistance', '113kΩ')], 'ok'),
        ('R', '10', [('Resistance', '10Ω')], 'ok'),
        ('C', '0.47uF', [('Capacitance', '470nF')], 'ok'),   # 470nF == 0.47uF
        ('C', '10µ', [], 'skip'),                            # no param to compare
        ('U', 'ESP32-S3', [('x', 'y')], 'skip'),             # not R/C/L
    ]
    for prefix, val, params, want in vcases:
        got = _val_check(prefix, val, params)[0]
        assert got == want, f"val {prefix} {val!r}: got {got}, want {want}"
    n = len(cases) + len(vcases)
    return f"{n}/{n}"

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
    ]
    # JLC server-side filter: facet values the local predicate accepts, '-' never
    cons = [(n, lab, make_pred(lab)[0]) for n, lab in (('volt', '>=25'), ('cap', '10u'))]
    fac = {'Voltage Rated': {'10V': 5, '25V': 7, '50V': 3, '-': 2}, 'Capacitance': {'1uF': 4, '10uF': 9}}
    checks.append(_server_attrs(cons, fac) == (
        [{'Voltage Rated': ['25V', '50V']}, {'Capacitance': ['10uF']}], {'volt': 10, 'cap': 9}, []))
    checks.append(_server_attrs(cons[:1], {'Voltage Rated': {'10V': 5}})[2] == ['volt'])
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

def _conf_key(pkg, footprint):
    """Normalised (LCSC package, KiCad footprint) key for the confirmed store,
    matching the matcher's own normalisation. Variant tails on the footprint are
    stripped, so one confirmation also covers its HandSolder/Pad variants."""
    return ((pkg or '').strip().upper(), _fp_base(footprint or ''))

def _load_confirmed(src):
    """fpcheck.json beside the netlist: {"confirmed":[{lcsc_pkg,footprint,...}]}.
    These are package-nomenclature pairs a human/LLM has verified are the same
    body (LCSC 'DFN-8(3x3)' == KiCad 'PQFN-8...'), so fpcheck stops flagging them.
    Returns (key set, path, loaded doc); missing/broken -> empty (nothing confirmed
    yet). Path sits beside the board, so it commits and persists across sessions."""
    path = os.path.join(os.path.dirname(os.path.abspath(src)), 'fpcheck.json')
    try:
        with open(path, encoding='utf8') as fh:
            doc = json.load(fh)
        keys = {_conf_key(e.get('lcsc_pkg'), e.get('footprint'))
                for e in doc.get('confirmed', [])}
        return keys, path, doc
    except Exception:
        return set(), path, {'confirmed': []}

def c_fpcheck(a):
    """Cross-check each part's KiCad footprint against LCSC's package for its
    part number. Also flags one LCSC code sitting on >1 footprint (a single MPN
    has one body size, so that is a copy-paste error). Reuses knet's parser and
    lcsc_detail's 24 h cache; no netlist arg -> runs the offline matcher self-test."""
    if not a.args or not a.args[0].endswith('.net'):
        print(f"footprint matcher self-test: {_fpcheck_selftest()} ok\n"
              f"usage: part.py fpcheck board.net  [--show-ok] [--json] [--fresh]")
        return 0
    src = a.args[0]
    nl = load_knet(src)
    if nl is None:
        print(f"fpcheck: could not parse {src} (kcommon.py not importable)"); return 1
    confirmed, cpath, cdoc = _load_confirmed(src)
    want, nocode = {}, []              # code -> {(footprint, value, prefix): [refs]}
    for ref, c in nl.comps.items():
        if c['dnp'] or not c['in_bom']:
            continue
        code = c['lcsc'] or ''
        if re.fullmatch(r'C\d+', code):
            # only R/C/L carry a checkable numeric value; for everything else the
            # Value field is a label (BOOT, ESP_RESET), so don't let it split rows
            val = (c['value'] or '') if c['prefix'] in ('R', 'C', 'L') else ''
            key = (c['footprint'] or '', val, c['prefix'])
            want.setdefault(code, {}).setdefault(key, []).append(ref)
        elif c['prefix'] not in ('H', 'TP'):
            nocode.append(ref)
    if not want:
        print("no LCSC codes found. netlist needs an 'LCSC Part' property"); return 1

    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        details = dict(ex.map(lambda code: (code, lcsc_detail(code, a.fresh)),
                              sorted(want)))

    ok, mism, review, unresolved, eol = [], [], [], [], []
    for code in sorted(want):
        r = details.get(code)
        if not r:
            unresolved.append(code); continue
        pkg, mpn, params = r.get('package'), r.get('mpn'), r.get('params')
        life = (r.get('lifecycle') or '').strip()
        if _EOL_RE.search(life):
            eol.append({'lcsc': code, 'mpn': mpn, 'lifecycle': life,
                        'refs': sorted({x for g in want[code].values() for x in g})})
        multi = len({_fp_sig(fp) for (fp, _v, _p) in want[code]}) > 1   # 2+ bodies
        for (fp, value, prefix), refs in sorted(want[code].items()):
            fpv, fpwhy = _fp_match(pkg, fp, mpn)
            vv, vwhy = _val_check(prefix, value, params)
            reasons = ([fpwhy] if fpv == 'mismatch' else []) + \
                      ([vwhy] if vv == 'mismatch' else [])
            if reasons:
                verdict, why = 'mismatch', '; '.join(reasons)
            elif multi and fpv == 'ok':
                verdict, why = 'review', fpwhy + '  (same LCSC code also on another body)'
            elif fpv == 'review':
                if _conf_key(pkg, fp) in confirmed:
                    verdict, why = 'ok', 'confirmed equivalent (fpcheck.json)'
                else:
                    verdict, why = 'review', fpwhy
            else:
                verdict, why = 'ok', fpwhy
            row = {'lcsc': code, 'mpn': mpn, 'lcsc_pkg': pkg, 'value': value,
                   'footprint': fp.split(':')[-1], 'why': why, 'refs': sorted(refs)}
            {'ok': ok, 'mismatch': mism, 'review': review}[verdict].append(row)

    if a.confirm:                      # record verified REVIEW pairs, do not print buckets
        req = list(dict.fromkeys(a.args[1:]))
        if not req:
            print("fpcheck --confirm: list the LCSC codes (currently in REVIEW) you have\n"
                  "verified, e.g. fpcheck board.net --confirm C115844 C233771 --note '...'")
            return 1
        rev_by_code = {}
        for row in review:
            if 'another body' in row['why']:      # multi-body review is not a nomenclature call
                continue
            rev_by_code.setdefault(row['lcsc'], []).append(row)
        added, skipped = [], []
        for code in req:
            rows = rev_by_code.get(code)
            if not rows:
                skipped.append(code); continue
            for row in rows:
                if _conf_key(row['lcsc_pkg'], row['footprint']) in confirmed:
                    continue
                confirmed.add(_conf_key(row['lcsc_pkg'], row['footprint']))
                cdoc.setdefault('confirmed', []).append(
                    {'lcsc_pkg': row['lcsc_pkg'], 'footprint': row['footprint'],
                     'example': code, 'note': a.note or '', 'added': time.strftime('%Y-%m-%d')})
                added.append((code, row['lcsc_pkg'], row['footprint']))
        with open(cpath, 'w', encoding='utf8') as fh:
            json.dump(cdoc, fh, indent=1, ensure_ascii=False)
            fh.write('\n')
        print(f"confirmed {len(added)} pair(s) -> {cpath}")
        for code, pkg, fp in added:
            print(f"  {code}  {pkg!r} ~ {fp!r}")
        if skipped:
            print(f"skipped (not a nomenclature-REVIEW code right now): {' '.join(skipped)}")
        return 0

    if a.json:
        print(json.dumps({'ok': ok, 'mismatch': mism, 'review': review, 'eol': eol,
                          'unresolved': unresolved, 'no_lcsc_code': sorted(nocode)},
                         indent=1))
        return 2 if mism else 0

    print(f"footprint + value vs LCSC part - {len(want)} LCSC parts, "
          f"{len(mism)} mismatch, {len(review)} review, {len(ok)} ok")
    def emit(title, rows):
        if not rows:
            return
        print(f"\n{title}")
        for r in rows:
            refs = ' '.join(r['refs'][:8]) + (' ...' if len(r['refs']) > 8 else '')
            print(f"  {r['lcsc']:<11} {trunc(r['mpn'] or '?',20):<20} "
                  f"[{trunc(r['value'] or '?',8)}] {r['why']}")
            print(f"  {'':<11} {'':<20} {'':<10} {refs}")
    emit("MISMATCH (footprint or value disagrees with the LCSC part):", mism)
    emit("REVIEW (rules could not decide - eyeball these):", review)
    if a.show_ok:
        emit("OK:", ok)
    if eol:
        print("\nEOL / not-recommended parts assigned (sourcing risk, not a footprint bug):")
        for e in eol:
            print(f"  {e['lcsc']:<11} {trunc(e['mpn'] or '?',20):<20} {e['lifecycle']}"
                  f"   {' '.join(e['refs'][:8])}")
    if unresolved:
        print(f"\nunresolved LCSC codes (no LCSC data): {' '.join(unresolved)}")
    if nocode:
        print(f"\n{len(nocode)} placed part(s) have no LCSC code (not checked): "
              f"{trunc(' '.join(sorted(nocode)), 200)}")
    return 2 if mism else 0

CMDS = {'selftest': c_selftest, 'search': c_search, 'show': c_show, 'ds': c_ds,
        'compare': c_compare, 'bom': c_bom, 'pick': c_pick, 'alt': c_alt,
        'jlc': c_jlc, 'check': c_check, 'fpcheck': c_fpcheck}

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('cmd', nargs='?', choices=list(CMDS))
    ap.add_argument('args', nargs='*')
    ap.add_argument('-n', type=int, default=8)
    ap.add_argument('--qty', type=int, default=None)
    ap.add_argument('--provider', default='all', choices=['all', 'lcsc', 'jlc', 'digikey'])
    ap.add_argument('--site', default='CA', help='DigiKey locale site (CA, US, ...)')
    ap.add_argument('--currency', default='CAD', help='DigiKey currency')
    ap.add_argument('--attrs', action='store_true')
    ap.add_argument('--table', action='store_true',
                    help='for `show` with multiple SKUs: one compact row per part '
                         '(sku/mpn/mfr/pkg/stock/price/desc) instead of a verbose block')
    ap.add_argument('--instock', action='store_true', help='drop zero-stock results')
    ap.add_argument('--nods', action='store_true', help='skip datasheet verification')
    ap.add_argument('--save', action='store_true',
                    help='ds: download the verified PDF and index it with kdoc.py')
    ap.add_argument('--dir', default='', help='ds --save: target folder (default docs/datasheets)')
    ap.add_argument('--fresh', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--show-ok', dest='show_ok', action='store_true',
                    help='fpcheck: also list the footprints that matched')
    ap.add_argument('--confirm', action='store_true',
                    help='fpcheck: record the listed REVIEW codes as confirmed-equivalent in fpcheck.json')
    ap.add_argument('--note', default='', help='fpcheck --confirm: provenance note stored with each entry')
    # pick / alt. The long synonyms are the names sessions actually guessed.
    syn = {'cap': ['--capacitance'], 'res': ['--resistance'], 'ind': ['--inductance'],
           'volt': ['--voltage'], 'pkg': ['--package'], 'diel': ['--dielectric'],
           'tol': ['--tolerance']}
    for f in FLAG_ATTRS:
        ap.add_argument('--' + f, *syn.get(f, []), dest=f, default=None,
                        help='pick constraint: VALUE | LO..HI | >=X | A,B | ~text | !A')
    ap.add_argument('--minstock', '--min-stock', '--stock-min', type=int, default=100,
                    help='pick/alt: drop parts with less stock (default 100)')
    ap.add_argument('--cat', '--category', default=None,
                    help="pick: JLC category filter, any unique part of its name ('tvs', "
                         "'I/O Expanders'); pick infers one from the keyword when a word names one")
    ap.add_argument('--w', action='append', default=[], metavar='NAME=SPEC',
                    help='pick constraint on any other attribute (repeatable)')
    ap.add_argument('--sort', default='price', choices=['price', 'stock', 'cap', 'volt'])
    ap.add_argument('--source', default='jlc', choices=['lcsc', 'jlc', 'both'],
                    help='candidate pool: jlc (default) = JLC index with server-side '
                         'package/category/price sort; lcsc = keyword search + detail. '
                         'Prices shown are LCSC retail either way (except --basic)')
    ap.add_argument('--basic', action='store_true', help='JLC Basic parts only (no $3 line fee)')
    ap.add_argument('--nojlc', action='store_true', help='skip the Basic/Extended annotation')
    ap.add_argument('--offline', action='store_true',
                    help='selftest: offline logic checks only, no network (exit 1 on failure)')
    ap.add_argument('--anystock', action='store_true', help='pick/alt: no stock floor at all (--minstock 0)')
    ap.add_argument('--fields', action='store_true', help='list attribute names/values, do not filter')
    ap.add_argument('--e12', action='store_true', help='fan a range over E12 not E6')
    ap.add_argument('--pool', type=int, default=240, help='max LCSC parts to detail (default 240)')
    ap.add_argument('--maxq', type=int, default=14, help='max keyword sub-queries for a range')
    ap.add_argument('--jobs', type=int, default=8)
    ap.add_argument('-h', '--help', action='store_true')
    a, extra = ap.parse_known_args()
    stray = [x for x in extra if x.startswith('-')]
    if stray:
        import difflib
        known = {'--value': ['--cap, --res or --ind']}      # difflib says --table
        tips = [f"{s} -> {m[0]}" for s in stray for m in
                [known.get(s.split('=')[0]) or difflib.get_close_matches(
                    s.split('=')[0], list(ap._option_string_actions), 1, 0.75)] if m]
        sys.exit(f"part.py: unknown flag(s) {' '.join(stray)}"
                 + (f"   did you mean: {', '.join(tips)}" if tips else '')
                 + "\n  a limit goes in the value, not the flag name: --volt '>=50', "
                   "--cap 4.7u..22u, --minstock 100.\n  any LCSC attribute: --w 'Clamping Voltage=<=30'. "
                   "`part.py --help` lists every flag.")
    a.args += [x for x in extra if not x.startswith('-')]   # keywords after flags
    if a.help or not a.cmd:
        print(__doc__); return 0
    if a.anystock:
        a.minstock = 0
    a.currency = (a.currency or 'CAD').upper()
    rc = CMDS[a.cmd](a) or 0
    note = fx_note()
    if note and not a.json:
        print(f"\n  {note}")
    return rc

try:                      # piping to `head` should not print a traceback
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
