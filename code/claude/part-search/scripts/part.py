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
  part.py ds C42409135 --save          ...and download it to docs/datasheets/<PART>.pdf,
                                       PART = base part number (--name to set it,
                                       --dir DIR elsewhere) + `kdoc.py index` it
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
  --golden DIR   selftest --golden DIR board.net: record 16 real outputs, then diff them
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
import sys, os, re, json, glob, argparse, subprocess
from part_core import (best_datasheet, buy_qty, CACHE, conv, dk_search, dk_token, dmoney,
                       dmoney1, fx_note, fx_rates, http, jlc_annotate, JLC_FACETS, jlc_facets,
                       jlc_detail, JLC_SEARCH, jlc_search, LCSC_DETAIL, lcsc_search, LCSC_SEARCH,
                       load_knet, money, price_at, resolve, trunc, verify_pdf)
from part_value import FLAG_ATTRS
from part_pick import c_alt, c_pick, _pick_selftest
from part_bom import c_bom, c_check, c_jlc
from part_fpcheck import c_fpcheck, _fpcheck_selftest

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
    if r['source'] == 'JLC':
        print("  note      : JLC assembly only - LCSC does not sell it retail; price and "
              "stock below are the JLC catalog's")
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

GOLDEN = [
    "pick MLCC --cap 4.7u..100u --volt >=25 --pkg 0805 --diel X7R,X5R",
    "pick MLCC --cap 10u --volt >=25 --pkg 0805 --basic",
    "pick schottky_diode --vr >=40 --ifwd >=1 --pkg SOD-123",
    "pick TVS --pkg SMA(DO-214AC) --vrwm >=20 --vc <=40",
    "pick resistor --res 10k --pkg 0402",
    "pick inductor --ind 2.2u --isat >=8",
    "alt C115844 --qty 100", "alt C85402",
    "show C1525 C14709 --nods", "show C408408 --nods", "compare C1525 C52923",
    "search TPS61033", "fpcheck {net}", "bom {net} --qty 5", "jlc {net}",
    "check {net} --qty 5",
]

def golden(d, net):
    """Record GOLDEN outputs into d, or diff against what d holds (kicad-review's
    selftest.py pattern). PARTSEARCH_FROZEN keeps every cached reply valid, so a
    pure refactor must be byte-identical; a cache miss is fetched and kept."""
    import difflib
    rec = not os.path.isdir(d) or not os.listdir(d)
    os.makedirs(d, exist_ok=True)
    env = dict(os.environ, PARTSEARCH_FROZEN='1')
    bad = 0
    for i, cmd in enumerate(GOLDEN):
        argv = [x.replace('_', ' ') for x in cmd.format(net=net).split()]
        p = subprocess.run([sys.executable, os.path.abspath(__file__)] + argv,
                           capture_output=True, text=True, env=env)
        out = f"{p.stdout}{p.stderr}\nexit={p.returncode}\n"
        f = os.path.join(d, f"{i:02d}.txt")
        if rec:
            open(f, 'w').write(out)
            continue
        old = open(f).read() if os.path.exists(f) else ''
        if old != out:
            bad += 1
            diff = list(difflib.unified_diff(old.splitlines(), out.splitlines(), n=0, lineterm=''))
            print(f"CHANGED  {cmd}  ({len(diff)} diff lines)\n"
                  + '\n'.join('    ' + x for x in diff[2:14]))
        else:
            print(f"same     {cmd}")
    print(f"\nrecorded {len(GOLDEN)} outputs in {d}" if rec else
          f"\n{len(GOLDEN) - bad}/{len(GOLDEN)} unchanged")
    return 1 if bad else 0

def c_selftest(a):
    if a.golden:
        if not a.args:
            print("selftest --golden DIR board.net"); return 1
        return golden(a.golden, a.args[0])
    try:
        ti = [('lcsc pdfUrl', 'https://www.ti.com.cn/cn/lit/ds/symlink/esd501.pdf?ts=17')]
        assert [_ds_name(r) for r in ({'mpn': 'ESD501DPYR', 'datasheet_candidates': ti},
                                      {'mpn': 'TPN2R203NC,L1Q(M)'}, {'mpn': 'MAX17320G22+T'})] \
            == ['ESD501', 'TPN2R203NC', 'MAX17320G22'], 'ds --save names'
        off = f"OK    pick/alt parsing {_pick_selftest()}, fpcheck matcher {_fpcheck_selftest()}, ds names 3/3"
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
          "network tab on lcsc.com and update LCSC_DETAIL / LCSC_SEARCH at the top of part_core.py.")

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
            print(f"{spec}: not found on LCSC or in JLC's library. For an MPN try "
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
                if a.name and len(a.args) > 1:
                    print("  saved     : NOT saved - --name names one part; give one spec"); continue
                save_datasheet(r, url, a)

def _ds_dir(r, root='docs/datasheets'):
    """Default ds --save folder. When root is split per schematic sheet
    (docs/datasheets/<sheet>/), the folder named by a word of the part's sheet in
    the netlist ('/Root/GNSS/' -> gnss, '/USB Interface/' -> usb). None, after
    saying why, when that is not one folder: guessing files it where kdoc and the
    next reader will not look."""
    if not os.path.isdir(root):
        return '.'
    subs = sorted(x for x in os.listdir(root) if os.path.isdir(os.path.join(root, x)))
    if not subs:
        return root
    nets = glob.glob('*-merged.net') or glob.glob('*.net')
    nl = load_knet(nets[0]) if len(nets) == 1 else None
    sheets = sorted({c['sheet'] for c in (nl.comps.values() if nl else [])
                     if c['lcsc'] == r.get('sku') or (r.get('mpn') and c['value'] == r.get('mpn'))})
    hit = {x for sh in sheets for x in subs if x in re.findall(r'[a-z0-9]+', sh.lower())}
    if len(hit) == 1:
        return os.path.join(root, hit.pop())
    print(f"  saved     : NOT saved - {root} is split per sheet ({' '.join(subs)}); "
          + (f"{r.get('sku')} is on {', '.join(sheets)}, " if sheets else 'part not in the netlist, ')
          + f"pass --dir {root}/<sheet>")
    return None

def _ds_name(r):
    """Datasheet file name, docs/datasheets' convention: the base part number,
    uppercase. A ti.com lit/ds/symlink/<x> or lit/gpn/<x> link (LCSC's or JLC's)
    names the datasheet itself (BQ25798RQMR -> BQ25798); otherwise the MPN cut at
    an orderable tail (',115' '(LF)' '+T' '#PBF'), which leaves a passive's value
    suffix alone. Vendor variant codes (NEO-F10N-00B) need --name."""
    cands = list(r.get('datasheet_candidates') or [])
    if re.fullmatch(r'C\d+', r.get('sku') or '') and r.get('source') != 'JLC':
        cands += (jlc_detail(r['sku']) or {}).get('datasheet_candidates') or []
    for _, u in cands:
        m = re.search(r'ti\.com(?:\.cn)?/(?:\w+/)?lit/(?:ds/symlink|gpn)/([\w.-]+?)(?:\.pdf)?(?:[?#]|$)',
                      u or '')
        if m:
            return m.group(1).upper()
    base = re.split(r'[,(#+]', r.get('mpn') or r.get('sku') or 'datasheet')[0]
    return re.sub(r'[^\w.-]+', '_', base).strip('_').upper()

def save_datasheet(r, url, a):
    """Fetch the whole verified PDF, file it as <dir>/<PART>.pdf and index it with
    kicad-review's kdoc.py, so pinout / abs-max checks go straight to
    `kdoc.py grep -d <PART>` instead of a hand curl + pdftotext."""
    d = a.dir or _ds_dir(r)
    if not d:
        return
    name = (re.sub(r'\.pdf$', '', a.name, flags=re.I) if a.name else _ds_name(r)) + '.pdf'
    # an existing file under another case is the same datasheet (ESD351.pdf)
    name = next((f for f in (os.listdir(d) if os.path.isdir(d) else [])
                 if f.lower() == name.lower()), name)
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
    ap.add_argument('--name', default='', help='ds --save: file name for the one part given '
                    '(default: base part number, e.g. BQ25798; companion docs <PART>_<DocType>)')
    ap.add_argument('--dir', default='', help='ds --save: target folder (default docs/datasheets, or its '
                    'per-sheet subfolder from the netlist)')
    ap.add_argument('--fresh', action='store_true')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--show-ok', dest='show_ok', action='store_true',
                    help='fpcheck: also list the footprints that matched')
    ap.add_argument('--confirm', action='store_true',
                    help='fpcheck: record the listed REVIEW codes as confirmed-equivalent in fpcheck.json')
    ap.add_argument('--note', default='', help='fpcheck --confirm: provenance note stored with each entry')
    ap.add_argument('--pcb', default='', help='fpcheck: board for the pad-land check '
                    '(default: the one .kicad_pcb beside the netlist)')
    ap.add_argument('--no-land', dest='noland', action='store_true',
                    help='fpcheck: skip the EasyEDA pad-land check (offline)')
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
    ap.add_argument('--golden', default='', metavar='DIR',
                    help='selftest --golden DIR board.net: record, then diff, real outputs')
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
