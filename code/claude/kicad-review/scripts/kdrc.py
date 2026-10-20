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
  kdrc.py FILE.kicad_pcb flags     PWR_FLAG audit: each flag's net, and whether
                                   ERC needs it (LOAD-BEARING) or not (REDUNDANT)
  kdrc.py FILE.kicad_pcb doctor    kicad-cli versions, the project's newest file
                                   format, what a double-click opens (WARN if stable
                                   would get a 10.99 file), KiCad running / locks

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
   "erc_roots": ["parsnip.kicad_sch", "charger.kicad_sch"],   # optional
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
import sys, os, re, json, glob, tempfile, subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kcommon import (print_findings, suppressed, trunc, kicad_cli,
                         top_level_sheets, sch_files, parse_args, doctor, rules_warning)
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
    'DRC:ITEMS_NOT_ALLOWED': 'item inside a keepout / rule area that forbids it',
    'DRC:MALFORMED_COURTYARD': 'courtyard is not one closed outline',
    'DRC:MISSING_COURTYARD': 'footprint has no courtyard',
    'DRC:NPTH_INSIDE_COURTYARD': "NPTH hole inside another footprint's courtyard",
    'DRC:PTH_INSIDE_COURTYARD': "plated hole inside another footprint's courtyard",
    'DRC:ISOLATED_COPPER': 'zone fill island tied to no pad (dead copper)',
    'DRC:LIB_FOOTPRINT_MISMATCH': 'footprint differs from its library copy',
    'DRC:FOOTPRINT_FILTERS_MISMATCH': "footprint not in the symbol's footprint filters",
    'DRC:SILK_EDGE_CLEARANCE': 'silkscreen too close to the board edge',
    'DRC:NET_CONFLICT': "pad net differs from the schematic's",
    'DRC:EXTRA_FOOTPRINT': 'footprint with no schematic symbol',
    'DRC:MISSING_FOOTPRINT': 'schematic symbol with no footprint on the board',
    'DRC:TRACK_DANGLING': 'track end connected to nothing',
    'DRC:VIA_DANGLING': 'via connected on fewer than two layers',
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
    de-duped. The [net] in "Pad 2 [Net-(U8-SW2)] of U8" is skipped: it read as
    "U8 SW2" (a pin name for a ref), and labelling the net changed the refs, so a
    rename looked like a new finding. The "of U8" still names the part."""
    out = []
    for it in items or []:
        for m in REF.findall(re.sub(r'\[[^\]]*\]', '', it.get('description', ''))):
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
    # the number to truncation; lead with it instead. Same for the netclass form
    # "(netclass 'Default' clearance ...)", which used to print as "actual 0~".
    m = re.search(r"\(((?:'[^']*'|[^()'])*?)\s*([\d.]+) mm; actual ([\d.]+) mm\)", raw_desc)
    if m:
        lim, act = float(m[2]), float(m[3])
        what = re.sub(r"^rule '([^']*)'.*", r"\1", m[1])
        raw_desc = f"actual {act:g} {'<' if act < lim else '>'} {lim:g} mm ({what})"
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
    return {'severity': sev, 'rule': rule, 'msg': msg, 'refs': refs, 'zero_clear': force,
            'items': [{'desc': it.get('description', ''),
                       'pos': [it['pos']['x'], it['pos']['y']] if 'x' in (it.get('pos') or {}) else None}
                      for it in items]}


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


def _ikey(i):
    """an item's identity for --baseline: its description, except a text item's, which can
    hold a variable (${CURRENT_DATE} turned 2 new + 2 gone at midnight): its uuid"""
    d = i.get('description', '')
    return ('text', i.get('uuid')) if re.match(r"(PCB|Footprint) text '|Text box '", d) and i.get('uuid') else d


BY_UUID = {'track_width'}       # the item's own measure is the violation: a widened track is the same one


def _vkey(v):
    if v.get('type') in BY_UUID and all(i.get('uuid') for i in v.get('items', [])):
        return (v.get('type'), tuple(i['uuid'] for i in v.get('items', [])))
    return (v.get('type'), v.get('description'),
            tuple(_ikey(i) for i in v.get('items', [])))


def _tracked(path):
    """is this file under git (the real board)? --refill-save refuses those"""
    r = subprocess.run(['git', '-C', os.path.dirname(os.path.abspath(path)), 'ls-files',
                        '--error-unmatch', os.path.abspath(path)], capture_output=True)
    return r.returncode == 0


def _against(d, a):
    """keep only what the --baseline report lacks (a multiset on type + descriptions,
    positions ignored), and say what changed"""
    from collections import Counter
    old = json.load(open(a.baseline))
    out, changed = {}, []
    for key in ('violations', 'unconnected_items'):
        have = Counter(_vkey(v) for v in old.get(key, []))
        was = {_vkey(v): v.get('description', '') for v in old.get(key, [])}
        new = []
        for v in d.get(key, []):
            if have[_vkey(v)] > 0:
                have[_vkey(v)] -= 1
                if v.get('type') in BY_UUID and was[_vkey(v)] != v.get('description', ''):
                    changed.append((was[_vkey(v)], v))
            else:
                new.append(v)
        out[key] = (new, sum(have.values()), len(old.get(key, [])))
    (nv, gv, ov), (nu, gu, ou) = out['violations'], out['unconnected_items']
    a.base_note = (f"vs {os.path.basename(a.baseline)}: violations {ov} -> {len(d.get('violations', []))} "
                   f"({len(nv)} new, {gv} gone" + (f", {len(changed)} changed" if changed else '')
                   + f"); unconnected {ou} -> {len(d.get('unconnected_items', []))} "
                   f"({len(nu)} new, {gu} gone). Only the NEW ones are listed.")
    for w, v in changed[:6]:
        it = (v.get('items') or [{}])[0].get('description', '')
        a.base_note += f"\n  changed {v['type']}: {w} -> {v.get('description', '')}  ({it[:50]})"
    if len(changed) > 6:
        a.base_note += f"\n  ... +{len(changed) - 6} more changed (same item uuid, new measure)"
    capped = sorted({t for rep in (old, d) for t, c in Counter(v.get('type') for v in rep.get('violations', [])).items()
                     if c >= 199})
    if capped:
        a.base_note += (f"\n!! {', '.join(capped)} hit kicad-cli's 199-per-type cap in this or the baseline report: "
                        f"its new/gone lists compare two arbitrary samples of 199, not the board")
    return dict(d, violations=nv, unconnected_items=nu)


def do_drc(board, a):
    args = [kicad_cli(board), 'pcb', 'drc', '--format', 'json', '--units', 'mm']
    if a.all:
        args.append('--severity-all')
    if a.parity:
        args.append('--schematic-parity')
    if a.refill_save and _tracked(board):
        sys.stderr.write(f"--refill-save writes {board}, which git tracks (the real board): refused. "
                         f"Use it on a scratch copy.\n")
        sys.exit(3)
    d = run_cli(args + ['--refill-zones'] + (['--save-board'] if a.refill_save else []) + [board], 'DRC')
    if a.report:
        with open(a.report, 'w') as fh:
            json.dump(d, fh)
    from collections import Counter
    every = d.get('violations', []) + (d.get('schematic_parity', []) if a.parity else [])
    capped = sorted(t for t, c in Counter(v.get('type') for v in every).items() if c >= 199)
    a.cap_note = (f"!! kicad-cli stops at 199 violations per type: {', '.join(capped)} hit it, so the real "
                  f"count is higher") if capped else ''
    if 'track_width' in capped:
        n, approx = track_width_count(board)
        a.cap_note += (f"; track_width counted on the board: {n} segment(s) under their rule"
                       + (f" (approx: skipped rule(s) {', '.join(approx)} with conditions it cannot evaluate)"
                          if approx else ''))
    if a.parity:
        d = dict(d, schematic_parity=_parity_real(d.get('schematic_parity', []), board, a))
    if a.baseline:
        d = _against(d, a)
    F = [vio_finding(v, 'DRC') for v in d.get('violations', [])]
    # the fill on disk is what gerbers export; anything only it has is stale
    if not a.no_stale_check and not a.refill_save and not a.baseline:
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


def _parity_real(vs, board, a):
    """Drop the NET_CONFLICTs that are only kicad-cli's naming: its schematic side
    names a Root-hierarchy net /Rails/X where the GUI-written board has /Root/Rails/X
    (~178 on parsnip). Real conflicts stay."""
    d = os.path.dirname(os.path.abspath(board)) or '.'
    roots = {n for _, n in top_level_sheets(d) if n}
    drawn = set()                                 # every refdes some sheet beside the board places
    for f in sch_files(d):
        drawn |= set(re.findall(r'\(reference "([^"]+)"\)', open(f, encoding='utf-8', errors='replace').read()))
    keep, gone, extra = [], 0, 0
    for v in vs:
        m = re.match(r"Pad net \((.*)\) doesn't match net given by schematic \((.*)\)$", v.get('description', ''))
        if v.get('type') == 'net_conflict' and m:
            pad, sch = m[1].split('/'), m[2]
            if len(pad) > 2 and pad[0] == '' and pad[1] in roots and '/' + '/'.join(pad[2:]) == sch:
                gone += 1
                continue
        fp = re.match(r'Footprint (\S+)', (v.get('items') or [{}])[0].get('description', ''))
        if v.get('type') == 'extra_footprint' and fp and fp[1] in drawn:
            extra += 1                            # on another top-level sheet the CLI did not read
            continue
        keep.append(v)
    if gone or extra:
        a.cap_note = (a.cap_note + '\n' if a.cap_note else '') + \
            (f"parity: dropped {gone} NET_CONFLICT(s) (/<root>/X on the board vs /X from kicad-cli, the "
             f"same net) and {extra} EXTRA_FOOTPRINT(s) whose ref a sheet beside the board places "
             f"(the CLI's parity reads one hierarchy) - CLI artifacts")
    return keep


def track_width_count(board):
    """(n, skipped rules): track segments narrower than their track_width minimum,
    counted on the board - kicad-cli reports 199 per type at most. The minimum is the
    last matching .kicad_dru track_width rule (KiCad evaluates last-first; `layer`
    inner/outer/<name>, condition terms A.Type / A.NetClass / A.NetName == '...'
    joined by &&), else the .kicad_pro min_track_width. A rule with any other term
    is skipped and named."""
    from kpcb_board import Board
    d = os.path.dirname(os.path.abspath(board)) or '.'
    floor = 0.0
    for pro in sorted(f for f in os.listdir(d) if f.endswith('.kicad_pro')):
        try:
            floor = float(json.load(open(os.path.join(d, pro)))['board']['design_settings']['rules']['min_track_width'])
        except (OSError, ValueError, KeyError, TypeError):
            pass
        break
    rules, skipped = [], []
    for dru in sorted(f for f in os.listdir(d) if f.endswith('.kicad_dru')):
        txt = re.sub(r'(?m)^\s*#.*$', '', open(os.path.join(d, dru), encoding='utf-8', errors='replace').read())
        for blk in re.split(r'\(rule\s+', txt)[1:]:
            mn = re.search(r'\(constraint\s+track_width[^()]*(?:\((?!min)[^()]*\)[^()]*)*\(min\s+([\d.]+)\s*mm\)', blk)
            if not mn:
                continue
            name = (re.match(r'"([^"]*)"|(\S+)', blk) or [None, '?'])
            name = name[1] or name[2]
            lay = re.search(r'\(layer\s+"?([\w.]+)"?\)', blk)
            cond = re.search(r'\(condition\s+"((?:\\.|[^"\\])*)"\)', blk)
            terms = []
            for t in (cond[1].split('&&') if cond else []):
                tm = re.fullmatch(r"\s*A\.(Type|NetClass|NetName)\s*==\s*'([^']*)'\s*", t)
                if not tm or '||' in t:
                    terms = None
                    break
                terms.append((tm[1], tm[2]))
            if terms is None:
                skipped.append(name)
                continue
            rules.append((float(mn[1]), lay[1] if lay else '', terms))
    b = Board(board)

    def need(t):
        for mn, lay, terms in reversed(rules):
            outer = t['layer'] in ('F.Cu', 'B.Cu')
            if lay and not (lay == 'outer' and outer or lay == 'inner' and not outer or lay == t['layer']):
                continue
            if all(v == {'Type': 'Track', 'NetClass': b.netclass(t['net'] or ''), 'NetName': t['net']}[k]
                   for k, v in terms):
                return mn
        return floor
    return sum(1 for t in b.tracks if t['w'] and t['w'] < need(t) - 1e-6), skipped


def do_erc(board, a, raw=None):
    """Returns (findings, roots actually run, whole) - whole = one report covered
    every top-level sheet, so cross-root nets were visible to ERC. `raw` (a list)
    also collects the kicad-cli violations as reported."""
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
                if raw is not None:
                    raw.append(v)
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
    fc = vio_finding({'description': "Clearance violation (netclass 'Default' clearance 0.1500 mm; "
                      "actual 0.1000 mm)", 'severity': 'error', 'type': 'clearance',
                      'items': [{'description': 'Via [-BATT]', 'pos': {'x': 1, 'y': 2}},
                                {'description': 'Pad 2 of BT2'}]}, 'DRC')
    checks.append((fc['msg'].startswith("actual 0.1 < 0.15 mm (netclass 'Default'")
                   and len(fc['items']) == 2, "netclass clearance number or items lost"))
    t, refs = _inert_flags(
        '(kicad_sch (lib_symbols (symbol "power:PWR_FLAG" (power global) (property "Reference" "#FLG")'
        ' (symbol "PWR_FLAG_0_0" (pin power_out line (name "x(") (number "1"))))'
        ' (symbol "power:+3V3" (power global) (property "Reference" "#PWR")'
        ' (symbol "+3V3_0_1" (pin power_in line))))'
        ' (symbol (lib_id "power:PWR_FLAG") (property "Reference" "#FLG01") (property "Value" "PWR_FLAG"))'
        ' (symbol (lib_id "power:+3V3") (property "Reference" "#PWR01") (property "Value" "+3V3")'
        ' (instances (project "p" (path "/x" (reference "#PWR02"))))))')
    checks.append((t.count('(power global)') == 1 and '(pin passive' in t and '(pin power_in' in t
                   and '"FLG01"' in t and '"#FLG' not in t
                   and refs == {'#FLG01': 'PWR_FLAG', '#PWR01': '+3V3', '#PWR02': '+3V3'},
                   f"flags: PWR_FLAG not made inert, or power refs wrong: {refs}"))
    checks.append((refs_of([{'description': 'Pad 2 [Net-(U8-SW2)] of U8 on F.Cu'},
                            {'description': 'Track [/Charger/SW2] on F.Cu'}]) == ['U8'],
                   "refs read out of a [net] name"))
    import argparse, shutil
    td = tempfile.mkdtemp(prefix='kdrc_st_')
    try:
        open(os.path.join(td, 't.kicad_pro'), 'w').write(
            '{"schematic": {"top_level_sheets": [{"filename": "t.kicad_sch", "name": "Root"}]},'
            ' "board": {"design_settings": {"rules": {"min_track_width": 0.1}}},'
            ' "net_settings": {"netclass_patterns": [{"pattern": "P*", "netclass": "PWR"}]}}')
        open(os.path.join(td, 't.kicad_sch'), 'w').write('(kicad_sch (symbol (instances (project "t" (path "/x" (reference "C9"))))))')
        open(os.path.join(td, 't.kicad_dru'), 'w').write(
            '(version 1)\n# (rule "commented" (constraint track_width (min 9mm)))\n'
            '(rule "outer" (layer outer) (constraint track_width (min 0.127mm)) (condition "A.Type == \'Track\'"))\n'
            '(rule "pwr" (constraint track_width (min 1.0mm)) (condition "A.NetClass == \'PWR\'"))\n'
            '(rule "odd" (constraint track_width (min 5mm)) (condition "A.insideArea(\'x\')"))\n')
        seg = lambda net, lay, w: f'(segment (start 0 0) (end 1 0) (width {w}) (layer "{lay}") (net "{net}") (uuid "u{net}{w}"))'
        open(os.path.join(td, 't.kicad_pcb'), 'w').write(
            '(kicad_pcb (version 20240108) (generator "pcbnew") (layers (0 "F.Cu" signal) (1 "In1.Cu" signal)'
            ' (2 "B.Cu" signal) (25 "Edge.Cuts" user)) (gr_rect (start -5 -5) (end 5 5) (layer "Edge.Cuts")) '
            + ' '.join((seg('P1', 'F.Cu', .8), seg('P1', 'In1.Cu', 1.2), seg('S', 'F.Cu', .12),
                        seg('S', 'In1.Cu', .12), seg('S', 'B.Cu', .2))) + ')')
        n, skipped = track_width_count(os.path.join(td, 't.kicad_pcb'))
        checks.append(((n, skipped) == (2, ['odd']), f"track_width count {n} {skipped} != 2 ['odd']"))
        a = argparse.Namespace(cap_note='')
        pv = _parity_real([{'type': 'net_conflict', 'description': "Pad net (/Root/A/X) doesn't match net given by schematic (/A/X)"},
                           {'type': 'net_conflict', 'description': "Pad net (/Root/X) doesn't match net given by schematic (/Y)"},
                           {'type': 'extra_footprint', 'items': [{'description': 'Footprint C9'}]},
                           {'type': 'extra_footprint', 'items': [{'description': 'Footprint C8'}]}],
                          os.path.join(td, 't.kicad_pcb'), a)
        checks.append(([v.get('description') or v['items'][0]['description'] for v in pv]
                       == ["Pad net (/Root/X) doesn't match net given by schematic (/Y)", 'Footprint C8'],
                       "parity: /Root naming artifact or other-sheet extra footprint kept, or a real one dropped"))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    tx = lambda txt: {'type': 'text_height', 'description': 'Text height', 'items':          # noqa: E731
                      [{'description': f"PCB text '{txt}' on F.Silkscreen", 'uuid': 'u1'}]}
    tw = lambda w: {'type': 'track_width', 'description': f'actual {w} < 1 mm', 'items':     # noqa: E731
                    [{'description': 'Track [A] on F.Cu, length 2 mm', 'uuid': 't1'}]}
    a2 = argparse.Namespace(baseline=os.path.join(tempfile.gettempdir(), 'kdrc_tw_base.json'))
    json.dump({'violations': [tw(0.5)]}, open(a2.baseline, 'w'))
    r2 = _against({'violations': [tw(0.7)]}, a2)
    checks.append((not r2['violations'] and '1 changed' in a2.base_note and '0.5 < 1 mm -> actual 0.7' in a2.base_note,
                   "baseline: a widened under-floor track (same uuid) read as new + gone, not changed"))
    checks.append((_vkey(tx('v2 2026-10-18')) == _vkey(tx('v2 2026-10-19'))
                   and _ikey({'description': 'Track [A] on F.Cu', 'uuid': 'x'}) == 'Track [A] on F.Cu',
                   "baseline: a text item's changed text made it a new violation (key it by uuid)"))
    td = tempfile.mkdtemp()
    try:
        json.dump({'net_settings': {'classes': [{'name': 'Default'}]}}, open(os.path.join(td, 'p.kicad_pro'), 'w'))
        w1 = rules_warning(td)
        open(os.path.join(td, 'p.kicad_dru'), 'w').write('(version 1)')
        checks.append((w1.startswith('WARN') and rules_warning(td) == '',
                       "rules_warning: a default-only project not flagged, or one with a .kicad_dru flagged"))
    finally:
        shutil.rmtree(td, ignore_errors=True)
    fails = [msg for ok, msg in checks if not ok]
    for msg in fails:
        print(f"FAIL  {msg}")
    print(f"kdrc selftest: {len(checks) - len(fails)}/{len(checks)} passed")
    return 1 if fails else 0


_TOK = re.compile(r'"(?:\\.|[^"\\])*"|[()]')


def _sexp_end(t, i):
    """Index just past the S-expression opening at t[i] (strings may hold parens)."""
    depth = 0
    for m in _TOK.finditer(t, i):
        if m.group() == '(':
            depth += 1
        elif m.group() == ')':
            depth -= 1
            if not depth:
                return m.end()
    return len(t)


def _inert_flags(t):
    """(.kicad_sch text with every PWR_FLAG made an ordinary part with a passive
    pin, {ref: value} of every #-ref instance). The netlist drops power symbols,
    so the flag loses (power) to be listed (as FLGn); passive, ERC stops counting
    it as a driver. ERC names a power symbol by ref only, hence the value map."""
    edits, skip, refs = [], 0, {}
    for m in re.finditer(r'\(symbol\s', t):
        if m.start() < skip:
            continue                              # a lib symbol's sub-unit
        skip = _sexp_end(t, m.start())
        b = t[m.start():skip]
        rf = re.search(r'\(property "Reference" "([^"]*)"', b)
        if re.match(r'\(symbol\s+"', b):          # lib_symbols entry
            if rf and rf[1].startswith('#FLG'):
                b = re.sub(r'\(power(?:\s+\w+)?\)', '', b, count=1)
                edits.append((m.start(), skip, b.replace('(pin power_out', '(pin passive')))
        elif rf:
            v = re.search(r'\(property "Value" "([^"]*)"', b)
            for r in {rf[1], *re.findall(r'\(reference "([^"]*)"', b)}:
                if r.startswith('#'):
                    refs[r] = v[1] if v else ''
    for i, j, b in reversed(edits):
        t = t[:i] + b + t[j:]
    return t.replace('"#FLG', '"FLG'), refs


def do_flags(path, a):
    """PWR_FLAG audit. Whole-project ERC twice (as is, and on a scratch copy with
    every flag inert); a power_pin_not_driven only the second run has marks a net
    that needs its flag. The copy's kmerge netlist says where each flag sits."""
    import shutil, glob, argparse
    from kcommon import Netlist
    d = os.path.dirname(os.path.abspath(path)) or '.'
    pwr = {}
    with tempfile.TemporaryDirectory(prefix='kdrc_flags_') as tmp:
        # ponytail: sheets in the project dir only; a Sheetfile in a subfolder fails ERC
        for f in sch_files(d):
            t, r = _inert_flags(open(f, encoding='utf-8').read())
            pwr.update(r)
            with open(os.path.join(tmp, os.path.basename(f)), 'w', encoding='utf-8') as fh:
                fh.write(t)
        for f in glob.glob(os.path.join(d, '*.kicad_pro')) + glob.glob(os.path.join(d, 'sym-lib-table')):
            shutil.copy(f, tmp)
        flags = sorted(r[1:] for r in pwr if r.startswith('#FLG'))
        if not flags:
            print(f"no PWR_FLAG symbols in {d}"); return 0
        ns = argparse.Namespace(erc_roots=None, all=True)
        base, inert = [], []
        _, _, whole = do_erc(path, ns, base)
        do_erc(os.path.join(tmp, os.path.basename(path)), ns, inert)
        roots = discover_roots(os.path.join(tmp, 'x'))
        net = os.path.join(tmp, 'flags.net')
        r = subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            'kmerge.py'), net, roots[0]], capture_output=True, text=True)
        if r.returncode or not os.path.exists(net):
            sys.stderr.write(f"kmerge of the flag copy failed:\n{r.stderr or r.stdout}\n"); return 3
        nl = Netlist(net)

    def net_of(ref, pin):
        if not ref.startswith('#'):
            return nl.cpins.get(ref, {}).get(pin)
        v = pwr.get(ref)                          # a power symbol's net is its Value
        return v if v in nl.nets else next((n for n in nl.nets if n.rsplit('/', 1)[-1] == v), None)

    def undriven(vs):
        return {tuple(i.get('description', '') for i in v.get('items', [])) for v in vs
                if v.get('type') == 'power_pin_not_driven'}
    need = {}                                     # net -> the pins ERC calls undriven
    for k in undriven(inert) - undriven(base):
        for desc in k:
            m = re.match(r'Symbol (\S+) Pin (\S+)', desc)
            if m:
                need.setdefault(net_of(m[1], m[2]) or f'? ({m[1]})', []).append(
                    m[1] if m[1].startswith('#') else f'{m[1]}.{m[2]}')
    rows, seen = [], set()
    for f in flags:
        fn = next(iter(nl.cpins.get(f, {}).values()), None)
        if fn is None:
            v, why = 'MISSING', 'not in the netlist of the copy (transform failed?)'
        elif fn.startswith('unconnected-'):
            v, why = 'REDUNDANT', 'connected to nothing'
        elif fn in seen and fn in need:
            v, why = 'DUPLICATE', 'another flag already sits on this net'
        elif fn in need:
            v, why = 'LOAD-BEARING', f"undriven without it: {' '.join(sorted(need[fn])[:4])}"
        else:
            drv = [f"{x['ref']}.{x['pin']}" for x in nl.nets[fn]
                   if x['type'] == 'power_out' and not x['ref'].startswith('FLG')]
            v, why = 'REDUNDANT', (f"driven by {' '.join(drv[:3])} (power_out)" if drv
                                   else 'ERC passes without it')
        seen.add(fn)
        rows.append({'flag': f, 'net': fn, 'sheet': nl.comps.get(f, {}).get('sheet', '?'),
                     'verdict': v, 'why': why})
    orphan = sorted(set(need) - seen)             # undriven only because a flag moved?
    if a.json:
        print(json.dumps({'flags': rows, 'unexplained': orphan}, indent=1)); return 0
    from collections import Counter
    c = Counter(r['verdict'] for r in rows)
    print(f"PWR_FLAG audit: {len(rows)} flag(s) - " + ', '.join(f"{n} {k.lower()}" for k, n in c.items())
          + ("\n(whole-project ERC)" if whole else
             "\n!! per-root ERC: a flag on a cross-root net may read LOAD-BEARING only here"))
    w = max(len(r['net'] or '?') for r in rows)
    for r in sorted(rows, key=lambda r: (r['verdict'], r['net'] or '')):
        print(f"  {r['verdict']:<12} {r['flag']:<7} {r['net'] or '?':<{w}}  {r['sheet']:<16} {r['why']}")
    if orphan:
        print(f"\nundriven with the flags inert but no flag on the net: {' '.join(orphan)}")
    return 0


# fab-capability limits: a 'warning' here reads as optional while the fab rejects it
FAB_RULES = {'clearance', 'hole_clearance', 'hole_to_hole', 'annular_width', 'track_width',
             'via_diameter', 'drill_out_of_range', 'copper_edge_clearance', 'edge_clearance',
             'connection_width', 'shorting_items', 'solder_mask_bridge', 'copper_sliver',
             'pth_inside_courtyard', 'npth_inside_courtyard', 'courtyards_overlap',
             'holes_co_located', 'starved_thermal'}


def demoted(board):
    """Header lines naming rules the .kicad_pro demoted: 'ignore' ones vanish from
    every report, a 'warning' fab limit reads as optional. [] when none."""
    d = os.path.dirname(os.path.abspath(board)) or '.'
    out = []
    for pro in sorted(f for f in os.listdir(d) if f.endswith('.kicad_pro')):
        try:
            p = json.load(open(os.path.join(d, pro)))
        except (OSError, ValueError):
            continue
        for tag, sect in (('DRC', p.get('board', {}).get('design_settings', {})), ('ERC', p.get('erc', {}))):
            rs = sect.get('rule_severities') or {}
            ign = sorted(k for k, v in rs.items() if v == 'ignore')
            warn = sorted(k for k, v in rs.items() if v == 'warning' and k in FAB_RULES)
            if ign or warn:
                out.append(f"{tag} demoted in {pro}:"
                           + (f" IGNORED {', '.join(ign)}" if ign else '')
                           + (';' if ign and warn else '')
                           + (f" warning-only fab limits {', '.join(warn)}" if warn else '')
                           + " - `--all` checks them at full severity")
        break
    return out


def do_scratch(board, a):
    """`scratch DIR [--from OTHER.kicad_pcb]`: a working copy of the project for the kpcb
    writers - the board under the project's own stem (or kicad-cli runs default rules), the
    .kicad_pro, .kicad_dru, sheets, netlists, tool configs and lib tables, and a symlink for
    every directory the lib tables name under ${KIPRJMOD} - so DRC there checks the real rules."""
    import shutil
    src = os.path.dirname(os.path.abspath(board))
    if not a.dest:
        print("scratch DIR [--from OTHER.kicad_pcb]"); return 1
    dest = os.path.abspath(a.dest)
    if os.path.isdir(dest) and os.listdir(dest):
        sys.stderr.write(f"{dest} is not empty: refused (a scratch copy starts clean)\n"); return 3
    if dest == src:
        sys.stderr.write("DIR is the project itself: refused\n"); return 3
    pros = sorted(glob.glob(os.path.join(src, '*.kicad_pro')))
    stem = os.path.basename(pros[0])[:-len('.kicad_pro')] if pros else os.path.splitext(os.path.basename(board))[0]
    os.makedirs(dest, exist_ok=True)
    got = []
    for pat in ('*.kicad_pro', '*.kicad_dru', '*.kicad_sch', '*.net', 'k*.json', 'fp-lib-table', 'sym-lib-table'):
        for f in sorted(glob.glob(os.path.join(src, pat))):
            if os.path.basename(f).startswith(('_autosave-', '~')):
                continue
            shutil.copy2(f, dest)
            got.append(os.path.basename(f))
    pcb = a.from_board or board
    shutil.copy2(pcb, os.path.join(dest, stem + '.kicad_pcb'))
    links = set()
    for tab in ('fp-lib-table', 'sym-lib-table'):
        p = os.path.join(src, tab)
        if os.path.exists(p):
            links |= set(re.findall(r'\$\{KIPRJMOD\}/([^/")]+)', open(p).read()))
    for d in sorted(links):
        if os.path.isdir(os.path.join(src, d)):
            os.symlink(os.path.join(src, d), os.path.join(dest, d))
    print(f"scratch copy of {src} -> {dest}\n  board: {stem}.kicad_pcb (from {os.path.abspath(pcb)})\n"
          f"  copied: {', '.join(got)}\n  linked: {', '.join(sorted(links)) or '-'}")
    rw = rules_warning(dest)
    if rw:
        print('  !! ' + rw)
    print(f"  next: kdrc.py {os.path.join(dest, stem + '.kicad_pcb')} drc --refill-save --report {os.path.join(dest, 'r0.json')}")
    return 0


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
    ap.add_argument('cmd', nargs='?', default='both', choices=['both', 'drc', 'erc', 'flags', 'doctor', 'scratch'])
    ap.add_argument('dest', nargs='?', default='', help='for `scratch`: the new directory')
    ap.add_argument('--from', dest='from_board', default='',
                    help='for `scratch`: put this board (e.g. one worked elsewhere) in the copy instead of FILE')
    ap.add_argument('--max', type=int, default=None, help='cap lines per rule (default 25)')
    ap.add_argument('--only', default='', help='comma list of RULE names to keep')
    ap.add_argument('--skip', default='', help='comma list of RULE names to drop')
    ap.add_argument('--all', action='store_true',
                    help='pass --severity-all to kicad-cli (ignores .kicad_pro severities)')
    ap.add_argument('--parity', action='store_true',
                    help='add DRC schematic-parity (the CLI reads one hierarchy: its /Root naming '
                         'NET_CONFLICTs and other-sheet EXTRA_FOOTPRINTs are dropped, counted in a note)')
    ap.add_argument('--unconnected', action='store_true',
                    help='include DRC unconnected_items (mid-layout unrouted noise)')
    ap.add_argument('--no-suppress', action='store_true', help='ignore kdrc.json suppress list')
    ap.add_argument('--no-stale-check', action='store_true',
                    help='skip the second DRC pass on the saved zone fill')
    ap.add_argument('--refill-save', action='store_true',
                    help='save the refilled board (scratch copies only: refused for a git-tracked file)')
    ap.add_argument('--report', default='', help='write the raw kicad-cli DRC JSON here (a later --baseline)')
    ap.add_argument('--baseline', default='',
                    help='an earlier --report: list only violations/unconnected items it does not have')
    ap.add_argument('--rules', action='store_true', help='print the rule legend')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--selftest', action='store_true',
                    help='run the offline suppression-logic self-test and exit (no board needed)')
    a = parse_args(ap)

    if not os.path.exists(a.file):
        sys.stderr.write(f"no such file: {a.file}\n"); return 3
    if a.cmd == 'flags':
        return do_flags(a.file, a)
    if a.cmd == 'scratch':
        return do_scratch(a.file, a)
    if a.cmd == 'doctor':
        lines = doctor(os.path.dirname(os.path.abspath(a.file)))
        print('\n'.join(lines))
        return 2 if any('WARN' in x for x in lines) else 0
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
    if getattr(a, 'base_note', ''):
        hdr += '\nDRC ' + a.base_note
    if getattr(a, 'cap_note', ''):
        hdr += '\n' + a.cap_note
    if a.cmd in ('both', 'drc') and not a.unconnected:
        hdr += f"\nDRC: {unconn} unconnected_items hidden (unrouted, mid-layout) - `--unconnected` to show"
    if not a.all:
        hdr += ''.join('\n' + ln for ln in demoted(a.file)
                       if a.cmd == 'both' or ln.startswith(a.cmd.upper()))
    rw = rules_warning(os.path.dirname(os.path.abspath(a.file))) if a.cmd in ('both', 'drc') else ''
    if rw:
        hdr += '\n!! ' + rw
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
