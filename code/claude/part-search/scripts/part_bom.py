"""part.py `bom`, `jlc` and `check`: whole-board costing and JLC assembly triage."""
import sys, os, re, json
import concurrent.futures as cf
from part_core import (buy_qty, conv, dmoney1, _FX, jlc_detail, lcsc_detail, load_knet, money,
                       price_at, trunc)

CODE_RE = re.compile(r'\bC\d{3,}\b')

def _missing(missing, a, jlc=None):
    """Codes LCSC could not detail, split into JLC-assembly-only parts (LCSC does
    not sell them retail; JLC catalog price, never added to an LCSC total) and codes
    neither catalog knows."""
    jlc = jlc if jlc is not None else {c: jlc_detail(c, a.fresh) for c in missing}
    only = [c for c in missing if jlc.get(c)]
    for c in only:
        h = jlc[c]
        up = price_at(h.get('ladder') or [], 1)
        print(f"JLC assembly only (no LCSC retail, not in the total): {c} {h.get('mpn')}  "
              f"{dmoney1(up, 'USD', a)}@1 JLC catalog, JLC stock {h.get('stock')}")
    rest = [c for c in missing if c not in only]
    if rest:
        print(f"unresolved codes (neither LCSC nor JLC has them): {' '.join(rest)}")

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
                          'unresolved': missing, 'jlc_only': [c for c in missing if jlc_detail(c, a.fresh)],
                          'no_lcsc_code': sorted(nocode)}, indent=1)); return
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
    _missing(missing, a)
    if nocode:
        print(f"\n{len(nocode)} placed component(s) have NO LCSC part number and are not "
              f"costed here:\n  {' '.join(sorted(nocode))}")

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
        res = list(ex.map(lambda c: (c, jlc_detail(c, a.fresh)), codes))
    rf = lambda c: (' ' + trunc(','.join(refs[c]), 22)) if refs.get(c) else ''
    print(f"{'code':<11} {'lib':<6} {'stock':>10}  {'unit@'+str(qty):<12} {'part':<28} note")
    for code, h in res:
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
        return code, refs, lcsc_detail(code, a.fresh), jlc_detail(code, a.fresh)
    with cf.ThreadPoolExecutor(max_workers=a.jobs) as ex:
        results = list(ex.map(one, sorted(want.items())))

    rows, total, missing, ext_n = [], 0.0, [], 0
    jlc = {code: jh for code, _, _, jh in results}
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
                          'unresolved': missing, 'jlc_only': [c for c in missing if jlc.get(c)],
                          'no_lcsc_code': sorted(nocode)}, indent=1))
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
        print()
        _missing(missing, a, jlc)
    if nocode:
        print(f"\n{len(nocode)} placed component(s) have NO LCSC part number - not costed "
              f"and cannot go to JLC assembly:\n  {' '.join(sorted(nocode))}")
    return 0
