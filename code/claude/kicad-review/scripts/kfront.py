#!/usr/bin/env python3
"""
kfront.py - keep a schematic's front matter (cover page list, block diagram,
power tree) true to the design it quotes. Those pages are hand-drawn text that
names ~60 refdes, part numbers and I2C addresses; refdes move during layout and
nothing in ERC looks at text.

  kfront.py NET check [--all]
      every refdes, "PART (REF)" pair and I2C address written on the part-less
      sheets (text, text boxes, table cells; --all: every sheet's notes too):
        ERROR  a refdes the netlist does not have (ranges J1-J4 expanded)
        ERROR  "BQ25798 (U8)" where U8's value/MPN is not a BQ25798
        ERROR  an address line ("0x6B", "0x36, 0x0B") under a "PART (REF)" that
               `knet i2c` resolves differently from the strap pins
  kfront.py NET pages
      the cover's PAGE/SHEET table against the .kicad_pro hierarchy (page numbers
      depth-first, as kproj renumbers them) and each row's #N page link; prints
      the true list when it has gone stale

Exit: 0 clean, 2 an ERROR or a stale table, 3 bad input.
"""
import sys, os, re, json, argparse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kcommon import Netlist, load_sexp, kids, kid, val, parse_args  # noqa: E402

REF = re.compile(r'\b([A-Z]{1,3})(\d+)(?:\s*-\s*\1?(\d+))?\b')
ADDR = re.compile(r'^\s*0x[0-9A-Fa-f]{2}(\s*,\s*0x[0-9A-Fa-f]{2})*\s*$')


def texts(path):
    """[(x, y, text)] of a sheet's text, text boxes and table cells"""
    t = load_sexp(path)
    out = []
    for node in kids(t, 'text') + kids(t, 'text_box') + [c for tab in kids(t, 'table')
                                                         for c in kids(kid(tab, 'cells') or [], 'table_cell')]:
        at = kid(node, 'at')
        if len(node) > 1 and isinstance(node[1], str) and at:
            out.append((float(at[1]), float(at[2]), node[1].replace('\\n', '\n')))
    return out


def refs_in(s, prefixes):
    """refdes tokens in s whose prefix the netlist uses; J1-J4 expands"""
    out = []
    for m in REF.finditer(s):
        if m.group(1) not in prefixes:
            continue
        a, b = int(m.group(2)), int(m.group(3) or m.group(2))
        out += [f"{m.group(1)}{i}" for i in range(a, b + 1)] if 0 <= b - a <= 20 else [m.group(0)]
    return out


def _alnum(s):
    return re.sub(r'[^A-Z0-9]', '', str(s).upper())


def part_matches(nl, ref, words):
    """does any part-number-like word name ref's value or MPN? None: no such word"""
    c = nl.comps[ref]
    names = {_alnum(c['value'])} | {_alnum(v) for k, v in c['props'].items()
                                    if isinstance(v, str) and k.upper() in ('MPN', 'MANUFACTURER PART NUMBER')}
    cands = [w for w in words if re.search(r'[A-Za-z]', w) and re.search(r'\d', w) and len(_alnum(w)) >= 5]
    if not cands:
        return None
    return any(n.startswith(_alnum(w)) or _alnum(w).startswith(n) for w in cands for n in names if n)


def c_check(nl, a):
    si = nl.sch()
    d = os.path.dirname(os.path.abspath(nl.path))
    files = sorted(f for f in si.sheetpath if a.all or not any(
        c.get('sheet') == si.sheetpath[f] for c in nl.comps.values()))
    if not files:
        print("no part-less sheet found (give --all to check every sheet's text)"); return 1
    prefixes = {c['prefix'] for c in nl.comps.values()}
    import knet
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(d, 'knet.json')))
    except (OSError, ValueError):
        pass
    table = dict(knet.I2C_PARTS, **(cfg.get('i2c') or {}))
    P = knet.power_nets(nl)
    errs, seen = [], 0
    for f in files:
        T = texts(os.path.join(d, f))
        for x, y, s in T:
            for line in s.splitlines():
                for r in refs_in(line, prefixes):
                    seen += 1
                    if r not in nl.comps:
                        errs.append(f"{f} ({x:.0f},{y:.0f}): {r} is not in the netlist  [{line.strip()[:60]}]")
                for m in re.finditer(r'([^()\n]*)\(([^()]*)\)', line):
                    # "PART (REF, REF)": the parenthesis holds refdes only, and the
                    # part is named in the clause right before it
                    if REF.sub('', m.group(2)).strip(' ,;/') or not refs_in(m.group(2), prefixes):
                        continue
                    rs = [r for r in refs_in(m.group(2), prefixes) if r in nl.comps]
                    pre = [w for w in re.split(r'[\s,;/]+', re.split(r'[:;]', m.group(1))[-1])
                           if w and not refs_in(w, prefixes)]
                    for r in rs:
                        ok = part_matches(nl, r, pre)
                        if ok is False:
                            errs.append(f"{f} ({x:.0f},{y:.0f}): \"{line.strip()[:50]}\" but {r} is "
                                        f"{nl.comps[r]['value']}")
            if ADDR.match(s):
                want = {int(v, 16) for v in re.findall(r'0x([0-9A-Fa-f]{2})', s)}
                above = [(y - y2, t) for x2, y2, t in T if abs(x2 - x) < 2 and 0 < y - y2 <= 4 and '(' in t]
                if not above:
                    continue
                t = min(above)[1]
                rs = [r for r in refs_in(t, prefixes) if r in nl.comps]
                for r in rs[:1]:
                    got, _why = knet.i2c_address(nl, r, P, table)
                    if got is not None and set(got) != want:
                        errs.append(f"{f} ({x:.0f},{y:.0f}): {s.strip()} under \"{t.strip()}\" but `knet i2c` "
                                    f"gives {', '.join(f'0x{g:02X}' for g in got) or 'UNRESOLVED'} for {r}")
    print(f"kfront check: {len(files)} sheet(s) ({', '.join(files)}), {seen} refdes quoted")
    for e in errs:
        print(f"  ERROR  {e}")
    print("  clean" if not errs else f"\n{len(errs)} error(s)")
    return 2 if errs else 0


def c_pages(nl, a):
    import kproj
    d = os.path.dirname(os.path.abspath(nl.path))
    try:
        pj = kproj.Project(d)
    except kproj.Refuse as e:
        print(f"kfront: {e}"); return 3
    truth = [(str(i), w[2]) for i, w in enumerate(pj.walk(), 1)]
    found = None
    for t in pj.tls:
        for tab in kids(load_sexp(os.path.join(d, t['filename'])), 'table'):
            n = int(val(tab, 'column_count') or 0)
            cells = kids(kid(tab, 'cells') or [], 'table_cell')
            rows = [cells[i:i + n] for i in range(0, len(cells), n)] if n else []
            if rows and [str(c[1]).upper() for c in rows[0][:2]] == ['PAGE', 'SHEET']:
                found = (t['filename'], rows[1:])
                break
        if found:
            break
    if not found:
        print("no table with a PAGE / SHEET header on a top-level sheet"); return 1
    fn, rows = found
    got = [(str(r[0][1]), str(r[1][1]), val(kid(r[0], 'effects') or [], 'href')) for r in rows if r[0][1]]
    bad = []
    for i, (pg, nm, href) in enumerate(got):
        if i >= len(truth) or (pg, nm) != truth[i]:
            bad.append(f"row {i + 1}: {pg} {nm}" + (f", page {truth[i][0]} is {truth[i][1]}" if i < len(truth) else
                                                     ", no such page"))
        if href and href != f"#{pg}":
            bad.append(f"row {i + 1}: page {pg} links to {href}")
    if len(got) < len(truth):
        bad.append("missing: " + ', '.join(f"{p} {n}" for p, n in truth[len(got):]))
    print(f"kfront pages: {fn} lists {len(got)} page(s), the project has {len(truth)}")
    for b in bad:
        print(f"  STALE  {b}")
    if bad:
        print("\ntrue page list:\n" + '\n'.join(f"  {p:>3}  {n}" for p, n in truth))
        return 2
    print("  matches the hierarchy, links ok")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('net')
    ap.add_argument('cmd', choices=['check', 'pages'])
    ap.add_argument('--all', action='store_true', help="check: every sheet's text, not just part-less ones")
    a = parse_args(ap)
    try:
        nl = Netlist(a.net)
    except FileNotFoundError:
        print(f"no such netlist: {a.net}", file=sys.stderr); return 3
    return {'check': c_check, 'pages': c_pages}[a.cmd](nl, a)


if __name__ == '__main__':
    sys.exit(main() or 0)
