#!/usr/bin/env python3
"""
kdrc.py - authoritative DRC + ERC for a KiCad board, via kicad-cli (KiCad 8-10).

kpcb.py `check` is geometric placement heuristics only - it prints, in its own
words, "no routing, no DRC". This is the missing half: it runs KiCad's OWN
Design Rules Check (which honours parsnip.kicad_dru automatically) and its
Electrical Rules Check, then folds the results into the same finding format as
knet/kpcb so the output reads the same way.

  kdrc.py FILE.kicad_pcb            DRC + ERC (default)
  kdrc.py FILE.kicad_pcb drc       DRC only
  kdrc.py FILE.kicad_pcb erc       ERC only, every root schematic

DRC runs with --refill-zones so results reflect the current pours, but never
with --save-board: the zones are refilled in memory only, the .kicad_pcb on
disk is never modified. It then runs a second pass on the fill AS SAVED (what
`export gerbers` writes). A violation found only in that pass is reported as
DRC:STALE_FILL: the board file's fill is out of date, so refill (B) and save
before exporting. `--no-stale-check` skips the second pass (~5 s).

The kicad-cli binary is picked per file (kcommon.kicad_cli): a board or sheet
saved by 10.99 nightly gets kicad-cli-nightly; $KICAD_CLI overrides.

ERC runs per ROOT schematic: the .kicad_pro's top_level_sheets, else every
*.kicad_sch beside the board that no other one pulls in as a sub-sheet. A root
already covered by an earlier report is skipped: kicad-cli-nightly checks every
top-level sheet from the first root, stable kicad-cli (10.0.x) only the one it
is given.

  !! per-root (stable) ERC is BLIND to cross-root nets. A pin powered or driven
  through a global label whose driver lives on another root (I2C_HOST_*, USB
  D+/-, the shared rails) reads as undriven - power_pin_not_driven, pin_to_pin.
  Those are multi-root false positives: verify with `knet.py parsnip-merged.net
  around REF`. A single report that covers every top-level sheet (nightly) has
  no such blind spot, and kdrc says which case you got.

Config: kdrc.json beside the board, same shape and precedence as kpcb.json.
  {"suppress": ["ERC:LIB_SYMBOL_MISMATCH", "DRC:SILK_OVERLAP:U1"],
   "erc_roots": ["parsnip.kicad_sch", "battery.kicad_sch"],   # optional
   "max": 25}
A bare "ERC:RULE" mutes the whole rule; "ERC:RULE:TOKEN" mutes only findings
whose refs or message contain TOKEN. Rule names are the kicad-cli violation
`type`, upper-cased, prefixed DRC: or ERC:.

A DRC:CLEARANCE finding at an actual 0.0 mm, or any DRC:SHORTING_ITEMS
(copper touching, a real short) is NEVER suppressed, no matter what kdrc.json says - a mid-layout suppress rule
that happens to also catch a real short must not hide it pre-fab. Its message
is tagged "0.0mm ACTUAL!" up front so it survives the 70-char line truncation.

`kdrc.py --selftest` runs the offline suppression-logic check (no board or
kicad-cli needed).

Exit: 0 clean, 2 an ERROR-severity finding survived suppression, 3 bad input /
kicad-cli failure.
"""
import sys, os, re, json, tempfile, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kcommon import (print_findings, suppressed, trunc, kicad_cli,
                         top_level_sheets, sch_files)
except ImportError:                                     # pragma: no cover
    print("kdrc.py needs kcommon.py beside it (shared finding formatter)",
          file=sys.stderr)
    sys.exit(3)

SEV = {'error': 'ERROR', 'warning': 'WARN', 'exclusion': 'INFO', 'info': 'INFO'}

# one-line legend per common violation type; unknown types just show the type
LEGEND = {
    'DRC:CLEARANCE': 'copper closer than the rule allows',
    'DRC:SHORTING_ITEMS': 'copper of two nets touching - a real short',
    'DRC:STALE_FILL': 'only in the SAVED zone fill: exported gerbers carry it until you refill + save',
    'DRC:TRACK_NOT_CENTERED_ON_VIA': 'track end not on the via centre',
    'DRC:CONNECTION_WIDTH': 'copper connection narrower than the rule minimum',
    'DRC:HOLE_TO_HOLE': 'two drills closer than the rule allows',
    'DRC:SOLDER_MASK_BRIDGE': 'mask aperture bridges two different nets',
    'DRC:SILK_OVERLAP': 'silkscreen over pad/other silk',
    'DRC:SILK_OVER_COPPER': 'silkscreen over exposed copper',
    'DRC:COURTYARDS_OVERLAP': 'two courtyards intersect',
    'DRC:HOLE_CLEARANCE': 'drilled hole too close to copper/hole',
    'DRC:TRACK_WIDTH': 'track narrower than the rule allows',
    'DRC:VIA_DIAMETER': 'via outside the allowed size',
    'DRC:ANNULAR_WIDTH': 'via/pad annular ring too thin',
    'DRC:COPPER_EDGE_CLEARANCE': 'copper too close to the board edge',
    'DRC:STARVED_THERMAL': 'thermal relief spokes cannot carry current',
    'ERC:POWER_PIN_NOT_DRIVEN': 'power input pin sees no power output driving it',
    'ERC:PIN_NOT_DRIVEN': 'input pin sees no output driving it (per-root ERC: may be cross-root)',
    'ERC:PIN_TO_PIN': 'two pins whose types conflict (per-root ERC: may be cross-root)',
    'ERC:LIB_SYMBOL_MISMATCH': 'symbol differs from library - deliberate edits here',
    'ERC:FOOTPRINT_FILTER': "footprint not in the symbol's filter list",
    'ERC:FOUR_WAY_JUNCTION': 'four wires meet at one point (style)',
    'ERC:SINGLE_GLOBAL_LABEL': 'global label used once (per-root ERC: may join another root; whole-project: a real dangling label)',
    'ERC:ISOLATED_PIN_LABEL': 'label/pin isolated within this root',
    'ERC:UNCONNECTED': 'unrouted / unconnected item',
}

REF = re.compile(r'\b([A-Z]{1,3}\d+)\b')                # J15, U1, C133, TH5...
ZERO_CLEAR_RE = re.compile(r'actual\s+0(?:\.0+)?\s*mm', re.I)


def refs_of(items):
    """Pull refdes tokens out of a violation's item descriptions, in order,
    de-duped. Net tokens like Net-(Q6-G) legitimately name Q6, so we keep them."""
    out = []
    for it in items or []:
        for m in REF.findall(it.get('description', '')):
            if m not in out:
                out.append(m)
    return out


def loc_of(items):
    """A short 'x,y' for the first item that has a position, for the message."""
    for it in items or []:
        p = it.get('pos')
        if p and 'x' in p:
            return f"@{p['x']:.1f},{p['y']:.1f}"
    return ''


def vio_finding(v, prefix):
    rule = f"{prefix}:{v.get('type', 'unknown').upper()}"
    sev = SEV.get(v.get('severity', 'warning'), 'WARN')
    items = v.get('items', [])
    refs = refs_of(items)
    parts = [it.get('description', '') for it in items[:2]]
    raw_desc = v.get('description', '') or ''
    zero_clear = bool(ZERO_CLEAR_RE.search(raw_desc))
    force = zero_clear or v.get('type') == 'shorting_items'    # a short never goes silent
    # "Clearance violation (rule 'X' clearance 0.1270 mm; actual 0.1000 mm)" loses
    # the number to truncation; lead with it instead
    m = re.search(r"\(rule '([^']*)'.*?([\d.]+) mm; actual ([\d.]+) mm\)", raw_desc)
    if m:
        raw_desc = f"actual {float(m[3]):g} < {float(m[2]):g} mm ({m[1]})"
    body = trunc(raw_desc or rule, 70)
    tail = '; '.join(trunc(p, 34) for p in parts if p)
    # the 70-char truncation above can cut "actual N mm" off a long rule name
    # before it reaches the reader - a 0.0 mm hit is a real short, so tag it
    # where truncation can never remove it (everything below is post-trunc).
    msg = ('0.0mm ACTUAL! ' if zero_clear else '') + body
    if refs:
        msg += ' [' + ' '.join(refs[:4]) + ']'
    if prefix == 'DRC':                       # board coords locate a violation;
        loc = loc_of(items)                   # ERC positions are sheet-local junk
        if loc:                               # and would also block msg folding
            msg += ' ' + loc
    if tail and not refs:
        msg += ' - ' + tail
    return {'severity': sev, 'rule': rule, 'msg': msg, 'refs': refs, 'zero_clear': force}


def run_cli(args, tag):
    """Run kicad-cli, return the parsed JSON report or exit 3 on failure."""
    fd, path = tempfile.mkstemp(suffix='.json', prefix='kdrc_')
    os.close(fd)
    try:
        r = subprocess.run(args + ['-o', path], capture_output=True, text=True)
        if not os.path.getsize(path):
            sys.stderr.write(f"kicad-cli {tag} produced no report:\n"
                             f"{r.stderr or r.stdout}\n")
            sys.exit(3)
        return json.load(open(path))
    except FileNotFoundError:
        sys.stderr.write("kicad-cli not on PATH\n"); sys.exit(3)
    finally:
        try: os.remove(path)
        except OSError: pass


def discover_roots(board):
    """Root schematics = the .kicad_pro's top_level_sheets (primary first), else
    every *.kicad_sch beside the board minus the ones some other schematic
    pulls in as a sub-sheet (via a Sheetfile property)."""
    d = os.path.dirname(os.path.abspath(board)) or '.'
    tls = [os.path.join(d, fn) for fn, _ in top_level_sheets(d)]
    if tls and all(os.path.exists(f) for f in tls):
        return tls
    schs = sch_files(d)
    included = set()
    for s in schs:
        try:
            txt = open(s, encoding='utf-8', errors='replace').read()
        except OSError:
            continue
        for m in re.findall(r'\(property "Sheetfile" "([^"]+)"', txt):
            included.add(os.path.normpath(os.path.join(d, m)))
    return [s for s in schs if os.path.normpath(s) not in included]


def _vkey(v):
    return (v.get('type'), v.get('description'),
            tuple(i.get('description') for i in v.get('items', [])))


def do_drc(board, a):
    args = [kicad_cli(board), 'pcb', 'drc', '--format', 'json', '--units', 'mm']
    if a.all:
        args.append('--severity-all')
    if a.parity:
        args.append('--schematic-parity')
    d = run_cli(args + ['--refill-zones', board], 'DRC')
    F = [vio_finding(v, 'DRC') for v in d.get('violations', [])]
    # the fill on disk is what gerbers export; anything only it has is stale
    if not a.no_stale_check:
        have = {_vkey(v) for v in d.get('violations', [])}
        for v in run_cli(args + [board], 'DRC (saved fill)').get('violations', []):
            if _vkey(v) not in have:
                f = vio_finding(v, 'DRC')
                f['msg'] = f"{v.get('type', '?')}: {f['msg']}"
                f['rule'] = 'DRC:STALE_FILL'
                F.append(f)
    unconn = d.get('unconnected_items', [])
    if a.unconnected:
        F += [vio_finding(v, 'DRC') if v.get('type') else
              {'severity': 'INFO', 'rule': 'DRC:UNCONNECTED',
               'msg': trunc(v.get('description', 'unconnected'), 70)
                      + (' [' + ' '.join(refs_of(v.get('items', []))[:4]) + ']'
                         if refs_of(v.get('items', [])) else ''),
               'refs': refs_of(v.get('items', []))}
              for v in unconn]
    if a.parity:
        F += [vio_finding(v, 'DRC') for v in d.get('schematic_parity', [])]
    return F, len(unconn)


def do_erc(board, a):
    """Returns (findings, roots actually run, whole) - whole = one report covered
    every top-level sheet, so cross-root nets were visible to ERC."""
    roots = a.erc_roots or discover_roots(board)
    names = dict(top_level_sheets(os.path.dirname(os.path.abspath(board)) or '.'))
    F, seen, ran, covered = [], set(), [], set()
    for root in roots:
        if not os.path.exists(root):
            sys.stderr.write(f"(erc root not found, skipped: {root})\n"); continue
        if names.get(os.path.basename(root)) in covered:
            continue                               # nightly: already in an earlier report
        args = [kicad_cli(root), 'sch', 'erc', '--format', 'json', '--units', 'mm']
        if a.all:
            args.append('--severity-all')
        d = run_cli(args + [root], f'ERC {os.path.basename(root)}')
        ran.append(root)
        covered |= {sh.get('path', '').strip('/').split('/')[0] for sh in d.get('sheets', [])}
        for sh in d.get('sheets', []):
            for v in sh.get('violations', []):
                f = vio_finding(v, 'ERC')
                key = (f['rule'], f['msg'])          # dedupe identical cross-root hits
                if key in seen:
                    continue
                seen.add(key)
                F.append(f)
    whole = len(ran) == 1 and bool(names) and set(names.values()) <= covered
    return F, ran, whole


def apply_suppress(F, supp):
    """Split findings by the kdrc.json suppress config, except an actual-0.0mm
    clearance finding is never suppressed - a real short must never go silent
    pre-fab regardless of what a suppress rule happens to match. Returns
    (kept, n_suppressed, forced) where `forced` is the zero-clearance findings
    that a rule would have hidden but didn't."""
    would_supp = [f for f in F if suppressed(f, supp)]
    forced = [f for f in would_supp if f.get('zero_clear')]
    kept = [f for f in F if f.get('zero_clear') or not suppressed(f, supp)]
    return kept, len(would_supp) - len(forced), forced


def _selftest():
    """Offline check of the suppression logic - no kicad-cli or board needed."""
    mk = lambda actual: {'description':
        f"Clearance violation (rule 'Pad to Track' clearance 0.1000 mm; actual {actual} mm)",
        'severity': 'error', 'type': 'clearance', 'items': []}
    fz = vio_finding(mk('0.0000'), 'DRC')
    fn = vio_finding(mk('0.0750'), 'DRC')
    checks = [
        (fz['zero_clear'] is True, "0.0000 mm not flagged zero_clear"),
        (fn['zero_clear'] is False, "0.0750 mm wrongly flagged zero_clear"),
        (fz['msg'].startswith('0.0mm ACTUAL!'), "zero-clear tag missing from msg"),
    ]
    supp_all = {'DRC:CLEARANCE': {''}}          # a bare-rule suppress: mutes everything
    kept, n_supp, forced = apply_suppress([fz, fn], supp_all)
    checks += [
        (fz in kept and fn not in kept, "zero-clearance finding was suppressed"),
        (n_supp == 1 and len(forced) == 1, "suppressed/forced counts wrong"),
    ]
    fs = vio_finding({'description': 'Items shorting two nets', 'severity': 'error',
                      'type': 'shorting_items', 'items': []}, 'DRC')
    kept, _, _ = apply_suppress([fs], {'DRC:SHORTING_ITEMS': {''}})
    checks.append((fs in kept, "shorting_items finding was suppressed"))
    fails = [msg for ok, msg in checks if not ok]
    for msg in fails:
        print(f"FAIL  {msg}")
    print(f"kdrc selftest: {len(checks) - len(fails)}/{len(checks)} passed")
    return 1 if fails else 0


def load_cfg(board):
    cp = os.path.join(os.path.dirname(os.path.abspath(board)), 'kdrc.json')
    try:
        return json.load(open(cp)) if os.path.exists(cp) else {}
    except (OSError, ValueError) as e:
        sys.stderr.write(f"(ignoring malformed kdrc.json: {e})\n"); return {}


def main():
    if '--selftest' in sys.argv[1:]:            # no board needed, so check before argparse
        return _selftest()
    import argparse
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('cmd', nargs='?', default='both', choices=['both', 'drc', 'erc'])
    ap.add_argument('--max', type=int, default=None, help='cap lines per rule (default 25)')
    ap.add_argument('--only', default='', help='comma list of RULE names to keep')
    ap.add_argument('--skip', default='', help='comma list of RULE names to drop')
    ap.add_argument('--all', action='store_true',
                    help='pass --severity-all to kicad-cli (ignores .kicad_pro severities)')
    ap.add_argument('--parity', action='store_true',
                    help='add DRC schematic-parity (noisy: uses the single project root)')
    ap.add_argument('--unconnected', action='store_true',
                    help='include DRC unconnected_items (mid-layout unrouted noise)')
    ap.add_argument('--no-suppress', action='store_true', help='ignore kdrc.json suppress list')
    ap.add_argument('--no-stale-check', action='store_true',
                    help='skip the second DRC pass on the saved zone fill')
    ap.add_argument('--rules', action='store_true', help='print the rule legend')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--selftest', action='store_true',
                    help='run the offline suppression-logic self-test and exit (no board needed)')
    a = ap.parse_args()

    if not os.path.exists(a.file):
        sys.stderr.write(f"no such file: {a.file}\n"); return 3
    cfg = load_cfg(a.file)
    if a.max is None:
        a.max = cfg.get('max', 25)
    a.erc_roots = cfg.get('erc_roots')

    a.suppress = {}
    if not a.no_suppress:
        for entry in cfg.get('suppress') or []:
            # PREFIX:TYPE mutes a whole rule; PREFIX:TYPE:TOKEN mutes by ref/text
            p = entry.split(':')
            if len(p) >= 3:
                rule, tok = f"{p[0]}:{p[1]}", ':'.join(p[2:])
            else:
                rule, tok = entry, ''
            a.suppress.setdefault(rule.strip().upper(), set()).add(tok.strip())

    only = {s.strip().upper() for s in a.only.split(',') if s.strip()}
    skip = {s.strip().upper() for s in a.skip.split(',') if s.strip()}

    F, unconn, roots, whole = [], 0, None, False
    if a.cmd in ('both', 'drc'):
        df, unconn = do_drc(a.file, a)
        F += df
    if a.cmd in ('both', 'erc'):
        ef, roots, whole = do_erc(a.file, a)
        F += ef

    if only:
        F = [f for f in F if f['rule'] in only]
    if skip:
        F = [f for f in F if f['rule'] not in skip]
    F, supp, forced = apply_suppress(F, a.suppress)

    if a.json:
        print(json.dumps(F, indent=1))
        return 2 if any(f['severity'] == 'ERROR' for f in F) else 0

    from collections import defaultdict
    n = defaultdict(int)
    for f in F:
        n[f['severity']] += 1
    scope = a.cmd.upper() if a.cmd != 'both' else 'DRC+ERC'
    hdr = f"{a.file}: {scope}  {n['ERROR']} error, {n['WARN']} warn, {n['INFO']} info"
    if roots:
        hdr += (f"\nERC roots ({len(roots)}): " + ', '.join(os.path.basename(r) for r in roots)
                + ("  (one report covers every top-level sheet)" if whole else ""))
    if a.cmd in ('both', 'drc') and not a.unconnected:
        hdr += f"\nDRC: {unconn} unconnected_items hidden (unrouted, mid-layout) - `--unconnected` to show"
    print_findings(F, hdr + '\n', rules=LEGEND, cap=a.max)
    if not F:
        print("no findings")
    if supp:
        print(f"\n({supp} finding(s) suppressed via kdrc.json - `--no-suppress` to see them)")
    if forced:
        print(f"\n!! {len(forced)} finding(s) matched a kdrc.json suppress rule but are shown "
              f"anyway: actual 0.0 mm clearance (copper touching / a real short) is never "
              f"hidden pre-fab.")
    if a.rules or not F:
        print("\nrules: " + ', '.join(f"{k}={v}" for k, v in sorted(LEGEND.items())))
    stale = sum(1 for f in F if f['rule'] == 'DRC:STALE_FILL')
    if stale:
        print(f"\n!! SAVED ZONE FILL IS STALE: {stale} violation(s) exist only in the fill on "
              f"disk,\n   which is what `export gerbers` writes. Refill all zones (B) and save "
              f"before exporting.")
    if a.cmd in ('both', 'erc') and not whole:
        print("\n!! per-root ERC cannot see cross-root nets. power_pin_not_driven / "
              "pin_to_pin\n   on a global-label net (I2C_HOST_*, USB D+/-, shared rails) "
              "is a three-root\n   false positive - confirm with `knet.py parsnip-merged.net "
              "around REF`.")
    return 2 if n['ERROR'] else 0


if __name__ == '__main__':
    sys.exit(main())
