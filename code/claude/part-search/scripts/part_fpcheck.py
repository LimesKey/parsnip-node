"""part.py `fpcheck`: KiCad footprint + value vs the LCSC part, and the EasyEDA land check."""
import os, re, json, time, math, glob
import concurrent.futures as cf
from part_core import cached, EE_COMP, _EOL_RE, http, jlc_detail, lcsc_detail, load_knet, trunc
from part_value import attr_of, enum, fmt_si, _UNIT_OF

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

_DIM = r'(\d+(?:\.\d+)?)\s*[xX*]\s*(\d+(?:\.\d+)?)'

def _body_dims(name, kicad):
    """Sorted body (a, b) mm stated in a package/footprint name, or None. LCSC:
    'DFN-8(3x3)', 'SMD,10.8x10mm'. KiCad: '_2x3mm_', 'L3.1-W3.1' - never the
    'EP0.61x2.2mm' pad or a 'P0.5mm' pitch."""
    if kicad:
        m = (re.search(r'(?<![A-Z\d.])' + _DIM + r'(?:X[\d.]+)?MM', name, re.I)
             or re.search(r'L(\d+(?:\.\d+)?)-W(\d+(?:\.\d+)?)', name, re.I))
    else:
        m = re.search(r'[(,]' + _DIM + r'(?:mm)?\)?$', name, re.I)
    return tuple(sorted(float(x) for x in m.groups())) if m else None

def _fp_match(pkg, fp, mpn=None):
    """-> ('ok'|'mismatch'|'review', reason). A name match whose stated body sizes
    disagree (LCSC 'DFN-8(3x3)' on KiCad 'DFN-8_2x2mm') is a mismatch."""
    v, why = _fp_match_name(pkg, fp, mpn)
    k = _fp_base(fp or '')
    a, b = _body_dims(pkg or '', False), _body_dims(k, True)
    if not (a and b):
        return v, why
    if any(abs(x - y) > 0.3 for x, y in zip(a, b)):
        return (('mismatch', f'body {a[0]:g}x{a[1]:g} mm (LCSC {pkg}) vs {b[0]:g}x{b[1]:g} mm ({k})')
                if v == 'ok' else (v, why))
    # same base name ('WQFN-38' in '..._WQFN-38-2EP_6x4mm') and same body: no human needed
    base = re.sub(r'\(.*|,.*', '', (pkg or '').upper()).strip()
    pat = r'[-_ ]?'.join(map(re.escape, re.findall(r'[A-Z]+|\d+', base)))
    if v == 'review' and pat and re.search(r'(?<![A-Z0-9])' + pat + r'(?!\d)', k):
        return ('ok', f'{base} {a[0]:g}x{a[1]:g} mm ~ {k}')
    return v, why

def _fp_match_name(pkg, fp, mpn=None):
    """-> ('ok'|'mismatch'|'review', reason) from the names alone."""
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
        # named for the series the MPN starts with: L_Sunlord_MWSA0503S for
        # MWSA0503S-2R2MT (a same-series other size is no prefix: MWSA1003S)
        if any(len(t) >= 6 and re.search(r'\d', t) and re.search(r'[A-Z]', t) and mn.startswith(t)
               for t in re.split(r'[^A-Z0-9]+', k)):
            return ('ok', f'footprint named for the series of MPN {mpn}')

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
    # 'SOD-523(SC-79)': a parenthetical that is another package NAME (not '(3x3)')
    m = re.fullmatch(r'(.+?)\(([A-Z]+-?\d+[A-Z]?)\)', canon)
    if m and any(_bcontains(k, t) for t in m.groups()):
        return ('ok', f'{canon} ~ {k}')

    return ('review', f'LCSC "{pkg}"  vs  KiCad "{fp.split(":")[-1]}"')  # 4) human

def _mpn_same(a, b):
    """Two MPNs name the same part: equal once punctuation goes, or one is the other
    plus a packaging tail ('TPN2R203NC' ~ 'TPN2R203NC,L1Q(M)')."""
    a, b = (re.sub(r'[^A-Z0-9]', '', (x or '').upper()) for x in (a, b))
    return a.startswith(b) or b.startswith(a)

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
        ('SMD,5.4x5.2mm', 'Inductor_SMD:L_Sunlord_MWSA0503S', 'ok', 'MWSA0503S-2R2MT'),
        ('SMD,10.8x10mm', 'Inductor_SMD:L_Sunlord_MWSA1003S', 'review', 'CMLO1040H2R2MTT'),
        # divergent package nomenclature stays in review (the real 5%)
        ('DFN-8(3x3)', 'Package_DFN_QFN:PQFN-8_L3.1-W3.1', 'review', 'AON7534'),
        # stated body sizes: disagree -> mismatch, agree with the same base name -> ok
        ('DFN-8(3x3)', 'Package_DFN_QFN:DFN-8_2x2mm_P0.5mm', 'mismatch'),
        ('WQFN-38(6x4)', 'Texas_REF0038A_WQFN-38-2EP_6x4mm_P0.4', 'ok'),
        ('X2-SON-8(1x1.4)', 'X2SON-8_1.4x1mm_P0.35mm', 'ok'),
        ('UDFN-8(2x3)', 'DFN-8-1EP_2x3mm_P0.5mm_EP0.61x2.2mm', 'review'),   # EP is no body size
        ('SOD-523(SC-79)', 'Diode_SMD:D_SOD-523', 'ok'),
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
    # land geometry: the U6 case (CAT24C256HU4 EasyEDA land vs LTC DDB footprint)
    ltc = [[str(i), -0.94 if i < 5 else 0.94, 0.25 * (2 * ((i - 1) % 4) - 3) * (1 if i < 5 else -1), 0.87, 0.25]
           for i in range(1, 9)] + [['9', 0, 0, 0.61, 2.2]]
    cat = [[str(i), -1.43 if i < 5 else 1.43, 0.5 * ((i - 1) % 4) - 0.75, 0.5, 0.28] for i in range(1, 9)] \
        + [['9', 0, 0, 1.4, 1.6]]
    assert 'largest pad 0.61x2.20 vs 1.40x1.60' in _land_diff(ltc, cat), _land_diff(ltc, cat)
    assert _land_diff(ltc, ltc) == ''
    assert _mpn_same('TPN2R203NC', 'TPN2R203NC,L1Q(M)') and not _mpn_same('CSD25480F3', 'CSD25481F4')
    n = len(cases) + len(vcases) + 3
    return f"{n}/{n}"

def ee_land(code, fresh=False):
    """[[num, x, y, w, h]] mm, copper pads of the EasyEDA footprint for a C-code;
    [] when EasyEDA has none ("Component not found"); None when the fetch failed
    (CloudFront 403s after ~150 quick calls: one retry after a pause, then give up
    so a rerun fills it). Cached 30 days: a published land does not change."""
    def go():
        for wait in (0, 10):
            time.sleep(wait)
            d = http(EE_COMP.format(code=code), headers={'Referer': 'https://easyeda.com/'})
            if '_error' not in (d or {'_error': 1}):
                break
        else:
            return d
        ds = (((d or {}).get('result') or {}).get('packageDetail') or {}).get('dataStr') or {}
        out = []
        for sh in ds.get('shape') or []:
            f = sh.split('~')       # PAD~shape~x~y~w~h~layer~net~num~holeR~pts~rot~...
            if f[0] == 'PAD' and len(f) > 11:
                w, h = float(f[4]) * 0.254, float(f[5]) * 0.254      # 1 unit = 10 mil
                if round(float(f[11] or 0)) % 180 == 90:
                    w, h = h, w
                out.append([f[8], float(f[2]) * 0.254, float(f[3]) * 0.254, w, h])
        return out
    v = cached('eeland1', code, go, fresh, ttl=30 * 24 * 3600)
    return v if isinstance(v, list) else None

# where the land IS the body: a same-name match can still be the wrong size or
# orientation (UDFN-8 2x3 pins on the 2 mm edges vs LTC DDB on the 3 mm edges)
_LEADLESS = re.compile(r'DFN|QFN|SON|LGA|BGA|CSP|WLB|PicoStar', re.I)

def _fp_pads(f):
    """Board footprint's copper pads, [[num, x, y, w, h]] in its own frame."""
    r = math.radians(f.rot)
    c, s = math.cos(r), math.sin(r)
    out = []
    for p in f.pads:
        if not any(l.endswith('.Cu') for l in p['layers']):
            continue                                  # paste-only apertures
        w, h = (p['sy'], p['sx']) if round(p['prot'] - f.rot) % 180 == 90 else (p['sx'], p['sy'])
        dx, dy = p['x'] - f.x, p['y'] - f.y
        out.append([p['num'], dx * c - dy * s, dx * s + dy * c, w, h])
    return out

def _land_diff(mine, theirs):
    """'' when two lands agree, else why. Compares the copper extent and the
    largest pad (the EP, or a PicoStar drain), both sorted so rotation cannot
    matter. Thresholds from all 21 leadless parts on parsnip (2026-09-23): real
    same-package lands differ <= 20% in the largest pad and <= 16% in extent."""
    def sig(pads):
        ext = sorted((max(p[1] + p[3] / 2 for p in pads) - min(p[1] - p[3] / 2 for p in pads),
                      max(p[2] + p[4] / 2 for p in pads) - min(p[2] - p[4] / 2 for p in pads)))
        big = max(pads, key=lambda p: p[3] * p[4])
        return ext, sorted(big[3:5])
    (ea, pa), (eb, pb) = sig(mine), sig(theirs)
    off = lambda a, b, rel: any(abs(x - y) > max(0.08, rel * max(x, y)) for x, y in zip(a, b))
    why = []
    if off(pa, pb, 0.25):
        why.append(f"largest pad {pa[0]:.2f}x{pa[1]:.2f} vs {pb[0]:.2f}x{pb[1]:.2f} mm")
    if off(ea, eb, 0.20):
        why.append(f"copper extent {ea[0]:.2f}x{ea[1]:.2f} vs {eb[0]:.2f}x{eb[1]:.2f} mm")
    return ('land differs from LCSC\'s EasyEDA footprint: ' + '; '.join(why)) if why else ''

def _load_board(src, pcb):
    """kicad-review's Board for --pcb, else the one .kicad_pcb beside the netlist."""
    if not pcb:
        c = glob.glob(os.path.join(os.path.dirname(os.path.abspath(src)), '*.kicad_pcb'))
        pcb = c[0] if len(c) == 1 else None
    if not pcb:
        return None, None
    try:
        import kpcb_board                      # load_knet() already put its dir on sys.path
        return kpcb_board.Board(pcb), pcb
    except Exception:
        return None, pcb

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
              f"usage: part.py fpcheck board.net  [--pcb FILE] [--no-land] [--show-ok] [--json] [--fresh]")
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
        details = dict(ex.map(lambda code: (code, lcsc_detail(code, a.fresh)
                                            or jlc_detail(code, a.fresh)), sorted(want)))
    # pad geometry, leadless parts only: the name check cannot see size/orientation
    board, pcb = (None, None) if a.noland else _load_board(src, a.pcb)
    lands = {}
    if board:
        todo = sorted({code for code, g in want.items() for (fp, _v, _p) in g
                       if _LEADLESS.search(fp)})
        with cf.ThreadPoolExecutor(max_workers=2) as ex:
            lands = dict(zip(todo, ex.map(lambda c: ee_land(c, a.fresh), todo)))

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
            # MPN field vs the code's part: a swapped LCSC Part or a stale MPN, and
            # assembly places what the code says
            mf = sorted({m for x in refs for m in [nl.comps[x]['props'].get('MPN')]
                         if m and mpn and not _mpn_same(m, mpn)})
            reasons = ([fpwhy] if fpv == 'mismatch' else []) + \
                      ([vwhy] if vv == 'mismatch' else []) + \
                      ([f"MPN field {'/'.join(mf)} vs the code's {mpn}"] if mf else [])
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
            f0 = board.fps.get(sorted(refs)[0]) if board else None
            land = _land_diff(_fp_pads(f0), lands[code]) if f0 and f0.pads and lands.get(code) else ''
            if land and verdict == 'ok':
                verdict, why = 'review', land + (f'  (name: {why})' if why else '')
            elif land:
                why += '; ' + land
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
            if 'another body' in row['why'] or 'land differs' in row['why']:
                continue        # a body/land question, not a nomenclature call
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

    # a code on two footprints or two values is two rows
    print(f"footprint + value vs LCSC part - {len(ok) + len(mism) + len(review)} rows "
          f"({len(want)} LCSC codes): {len(mism)} mismatch, {len(review)} review, {len(ok)} ok")
    if a.noland:
        pass
    elif not board:
        print(f"  (land check skipped: {'could not load ' + pcb if pcb else 'no single .kicad_pcb beside the netlist; --pcb FILE'})")
    else:
        none = sorted(c for c, v in lands.items() if v == [])
        fail = sorted(c for c, v in lands.items() if v is None)
        print(f"  land check: {len(lands) - len(none) - len(fail)} leadless part(s) vs EasyEDA pads "
              f"on {os.path.basename(pcb)}"
              + (f"; EasyEDA has no land for {' '.join(none)}" if none else '')
              + (f"; fetch FAILED for {' '.join(fail)} (unchecked - rerun later)" if fail else ''))
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
        print(f"\nunresolved codes (neither LCSC nor JLC has them): {' '.join(unresolved)}")
    if nocode:
        print(f"\n{len(nocode)} placed part(s) have no LCSC code (not checked): "
              f"{trunc(' '.join(sorted(nocode)), 200)}")
    return 2 if mism else 0
