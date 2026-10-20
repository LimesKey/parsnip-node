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
  kfront.py NET pages [-o OUT.kicad_sch]
      the cover's PAGE/SHEET table against the .kicad_pro hierarchy (page numbers
      depth-first, as kproj renumbers them) and each row's #N page link; prints
      the true list when it has gone stale. -o writes the cover with the table
      rebuilt from the hierarchy (rows added/dropped, each CONTENTS cell kept with
      its sheet, links #N) - a copy, never the sheet itself
  kfront.py NET draw powertree|i2c -o OUT.kicad_sch [--page N] [--title T]
      a page generated from `knet powertree` (sources -> converters -> rails ->
      loads, one row per hop chain, colour by rail voltage) or `knet i2c` (each bus
      with its controller, pull-ups, targets and their resolved addresses). A new
      file: put it in place on a scratch copy, then `kverify.py` and `kfront check`

Writing a .kicad_sch is a project edit: OUT must not be a git-tracked file.
Exit: 0 clean, 2 an ERROR or a stale table, 3 bad input.
"""
import sys, os, re, json, math, argparse, uuid, subprocess

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
    import knet_power
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(d, 'knet.json')))
    except (OSError, ValueError):
        pass
    table = dict(knet_power.I2C_PARTS, **(cfg.get('i2c') or {}))
    P = knet_power.power_nets(nl)
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
                    got, _why = knet_power.i2c_address(nl, r, P, table)
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
    if a.out:
        if os.path.exists(a.out) and _tracked(a.out):
            print(f"{a.out} is git-tracked (a project sheet): write a copy and move it in on a scratch project"); return 3
        old = write_table(os.path.join(d, fn), truth, a.out)
        print(f"  wrote {a.out}: the table rebuilt from the hierarchy ({old} -> {len(truth)} rows, CONTENTS kept per "
              f"sheet). Check: copy it over {fn} on a scratch project, `kfront.py NET pages` and `kverify.py DIR`")
        return 0
    if bad:
        print("\ntrue page list:\n" + '\n'.join(f"  {p:>3}  {n}" for p, n in truth) + "\n(-o OUT writes the cover with it)")
        return 2
    print("  matches the hierarchy, links ok")
    return 0


# ---------------------------------------------------------------- writers

def _tracked(path):
    """is this path under git (a real project file)? the writers refuse those"""
    r = subprocess.run(['git', '-C', os.path.dirname(os.path.abspath(path)) or '.', 'ls-files', '--error-unmatch',
                        os.path.abspath(path)], capture_output=True)
    return r.returncode == 0


def _q(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def _u():
    return f'(uuid "{uuid.uuid4()}")'


def write_table(src, truth, out):
    """the cover file with its PAGE/SHEET table rebuilt from truth [(page, name)]"""
    txt = open(src, encoding='utf-8').read()
    for m in re.finditer(r'\n\t\(table\n.*?\n\t\)', txt, re.S):
        blk = m.group(0)
        cells = re.findall(r'\n\t\t\t\(table_cell .*?\n\t\t\t\)', blk, re.S)
        n = int(re.search(r'\(column_count (\d+)\)', blk)[1])
        head = [re.match(r'\n\t\t\t\(table_cell "([^"]*)"', c)[1].upper() for c in cells[:2]]
        if head == ['PAGE', 'SHEET']:
            break
    else:
        raise SystemExit("no table with a PAGE / SHEET header")
    rows = [cells[i:i + n] for i in range(0, len(cells), n)]
    text = lambda c: re.match(r'\n\t\t\t\(table_cell "((?:[^"\\]|\\.)*)"', c)[1]   # noqa: E731
    contents = {text(r[1]): [text(c) for c in r[2:]] for r in rows[1:]}
    hs = [float(h) for h in re.search(r'\(row_heights ([^)]*)\)', blk)[1].split()]
    rh = hs[1] if len(hs) > 1 else hs[0]
    y0 = float(re.search(r'\(at \S+ (\S+) ', rows[0][0])[1])
    new = []
    for i, (pg, name) in enumerate(truth):
        tpl = rows[i + 1] if i + 1 < len(rows) else rows[-1]
        vals = [pg, name] + (contents.get(name) or [''] * (n - 2))
        for c, v in zip(tpl, vals):
            c = re.sub(r'(\n\t\t\t\(table_cell )"(?:[^"\\]|\\.)*"', lambda q: q[1] + _q(v), c, count=1)
            c = re.sub(r'\(at (\S+) \S+ ', lambda q: f"(at {q[1]} {y0 + rh * (i + 1):g} ", c, count=1)
            c = re.sub(r'\(href "#\d+"\)', f'(href "#{pg}")', c)
            if i + 1 >= len(rows):
                c = re.sub(r'\(uuid "[^"]*"\)', _u(), c)
            new.append(c)
    nb = blk.replace(''.join(cells), ''.join(cells[:n]) + ''.join(new))
    nb = re.sub(r'\(row_heights [^)]*\)', f"(row_heights {' '.join(f'{h:g}' for h in [hs[0]] + [rh] * len(truth))})", nb)
    open(out, 'w', encoding='utf-8').write(txt.replace(blk, nb))
    return len(rows) - 1


RAILC = [(7.0, (200, 0, 0)), (4.5, (180, 0, 180)), (3.0, (0, 90, 220)), (0.0, (0, 140, 60))]


class Page:
    """KiCad schematic graphics (10.x syntax) for a generated page"""

    def __init__(self):
        self.out = []

    def text(self, x, y, s, size=1.27, bold=False, col=None, justify='left'):
        c = f' (color {col[0]} {col[1]} {col[2]} 1)' if col else ''
        self.out.append(f'(text {_q(s)} (exclude_from_sim no) (at {x:g} {y:g} 0) (effects (font (size {size} {size})'
                        + (' (bold yes)' if bold else '') + f'{c}) (justify {justify})) {_u()})')

    def rect(self, x, y, w, h, col=None, width=0.25):
        c = f' (color {col[0]} {col[1]} {col[2]} 1)' if col else ''
        self.out.append(f'(rectangle (start {x:g} {y:g}) (end {x + w:g} {y + h:g}) (stroke (width {width}) (type solid){c}) '
                        f'(fill (type none)) {_u()})')

    def line(self, pts, col=None, arrow=False, width=0.25):
        c = f' (color {col[0]} {col[1]} {col[2]} 1)' if col else ''
        self.out.append(f'(polyline (pts {" ".join(f"(xy {x:g} {y:g})" for x, y in pts)}) (stroke (width {width}) (type solid){c}) '
                        f'(fill (type none)) {_u()})')
        if arrow:
            (ax, ay), (bx, by) = pts[-2], pts[-1]
            L = math.hypot(bx - ax, by - ay) or 1
            dx, dy = (bx - ax) / L, (by - ay) / L
            p1, p2 = (bx - 1.4 * dx - 0.5 * dy, by - 1.4 * dy + 0.5 * dx), (bx - 1.4 * dx + 0.5 * dy, by - 1.4 * dy - 0.5 * dx)
            fill = f'(fill (type color) (color {col[0]} {col[1]} {col[2]} 1))' if col else '(fill (type outline))'
            self.out.append(f'(polyline (pts (xy {bx:g} {by:g}) (xy {p1[0]:.3f} {p1[1]:.3f}) (xy {p2[0]:.3f} {p2[1]:.3f}) '
                            f'(xy {bx:g} {by:g})) (stroke (width 0.1) (type solid){c}) {fill} {_u()})')

    def write(self, out, like, title, page, paper='A3'):
        head = '(kicad_sch\n\t(version 20260830)\n\t(generator "eeschema")\n\t(generator_version "10.99")\n'
        if like and os.path.exists(like):
            src = open(like, encoding='utf-8').read()
            head = src[:src.index('\t(uuid')]
        body = '\n\t'.join(self.out)
        open(out, 'w', encoding='utf-8').write(
            f'{head}\t(uuid "{uuid.uuid4()}")\n\t(paper "{paper}")\n\t(title_block\n\t\t(title {_q(title)})\n'
            f'\t\t(date "${{DATE}}")\n\t\t(rev "${{REV}}")\n\t)\n\t(lib_symbols)\n\t{body}\n'
            f'\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "{page}")\n\t\t)\n\t)\n)\n')
        return len(self.out)


def _vcol(v):
    return next((c for lim, c in RAILC if v is not None and v >= lim), (110, 110, 110))


def draw_powertree(nl, a, pg):
    import knet_power
    r = knet_power.powertree_rows(nl, a.cfg)
    if r is None:
        raise SystemExit("no power source found")
    items, lost = r
    X0, Y0, XS, YS, YMAX = 15, 30, 9, 6.0, 280
    y, col_x, rail_at, n = Y0, X0, {}, 0          # rail_at: net -> (x, y) its children hang from
    pg.text(X0, 18, 'Power tree, generated by kfront from knet powertree (sources -> converters -> rails -> loads)', 1.5, True)
    for it in items:
        if y > YMAX:                              # next column
            col_x, y = col_x + 135, Y0
        if it[0] == 'src':
            _k, lab, net, info = it
            y += 3
            pg.rect(col_x, y - 2.5, 4 + 1.0 * len(lab), 5, (0, 0, 0), 0.35)
            pg.text(col_x + 2, y, lab, 1.27, True)
            nx = col_x + 6 + 1.0 * len(lab)
            pg.line([(col_x + 4 + 1.0 * len(lab), y), (nx, y)], _vcol(info['v']), True)
            pg.text(nx + 1, y, knet_power.net_line(net, info), 1.27, True, _vcol(info['v']))
            rail_at[net] = (col_x + 3, y)
        else:
            _k, depth, chain, net, info = it
            up = next((x for x in reversed(items[:items.index(it)])
                       if (x[0] == 'src' and depth == 0) or (x[0] == 'hop' and x[1] == depth - 1)), None)
            parent = rail_at.get(up[2] if up and up[0] == 'src' else up[3] if up else None)
            x = col_x + 3 + XS * (depth + 1)
            if parent and parent[0] >= col_x - 1:
                pg.line([parent, (parent[0], y), (x, y)], (90, 90, 90))
            label = ' -> '.join(t for _r, t in chain)
            w = 3 + 1.02 * len(label)
            pg.rect(x, y - 2.2, w, 4.4, (90, 90, 90), 0.2)
            pg.text(x + 1.5, y, label, 1.1)
            pg.line([(x + w, y), (x + w + 5, y)], _vcol(info['v']), True)
            pg.text(x + w + 6, y, knet_power.net_line(net, info), 1.1, info['v'] is not None, _vcol(info['v']))
            rail_at[net] = (x + 3, y + 2.2)
        y += YS
        n += 1
    if lost:
        pg.text(X0, min(y + 4, 290), 'not reached from any source: ' + ', '.join(lost), 1.1)
    return n


def draw_i2c(nl, a, pg):
    import knet_power
    buses = knet_power.i2c_report(nl, a.cfg)
    if not buses:
        raise SystemExit("no net named like SDA")
    pg.text(15, 18, 'I2C buses, generated by kfront from knet i2c (addresses resolved from the strap pins)', 1.5, True)
    y = 34
    for bus in buses:
        if not bus['targets'] and not bus['ctrl']:
            continue
        ctrl = '; '.join(f"{r} {v}" for r, v, _p in bus['ctrl']) or 'no controller'
        pg.rect(15, y - 4, 60, 8, (0, 0, 0), 0.35)
        pg.text(17, y - 1, ctrl[:44], 1.27, True)
        pg.text(17, y + 2, f"{bus['name'].split('/')[-1]} / {bus['scl'].split('/')[-1]}"[:48], 1.0)
        xe = 85 + 48 * max(1, len(bus['targets']))
        pg.line([(75, y), (xe, y)], (0, 90, 220), width=0.5)
        for k, (ref, value, addrs, _why) in enumerate(bus['targets']):
            x = 85 + 48 * k
            at = ', '.join(f"0x{ad:02X}" for ad in addrs) if addrs else ('?' if addrs is None else 'unresolved')
            dup = any(ref in refs for _ad, refs in bus['dups'])
            pg.line([(x + 20, y), (x + 20, y + 6)], (0, 90, 220))
            pg.rect(x, y + 6, 40, 11, (200, 0, 0) if dup else (0, 0, 0), 0.3)
            pg.text(x + 2, y + 9, f"{ref} {value}"[:30], 1.1, True)
            pg.text(x + 2, y + 13, at + ('  DUPLICATE' if dup else ''), 1.27, False, (200, 0, 0) if dup else None)
        pg.text(85, y - 3, 'pull-ups: ' + ('; '.join(bus['pulls']) or 'NONE')
                + (f"   connectors: {' '.join(bus['conn'])}" if bus['conn'] else ''), 1.0)
        y += 34
    return len(buses)


def c_draw(nl, a):
    what = (a.what or [''])[0]
    if what not in ('powertree', 'i2c') or not a.out:
        print("draw powertree|i2c -o OUT.kicad_sch [--page N] [--title T]"); return 1
    if os.path.exists(a.out) and _tracked(a.out):
        print(f"{a.out} is git-tracked (a project sheet): write a new file on a scratch copy"); return 3
    import kcommon
    pg = Page()
    cfg = {}
    try:
        cfg = json.load(open(os.path.join(os.path.dirname(os.path.abspath(nl.path)), 'knet.json')))
    except (OSError, ValueError):
        pass
    a.cfg = cfg
    n = draw_powertree(nl, a, pg) if what == 'powertree' else draw_i2c(nl, a, pg)
    d = os.path.dirname(os.path.abspath(nl.path))
    like = os.path.join(d, kcommon.project_root(d)) if kcommon.project_root(d) else None   # its header: same version
    k = pg.write(a.out, like, a.title or ('Power Tree' if what == 'powertree' else 'I2C Buses'), a.page or '1')
    print(f"kfront draw {what}: {n} row(s), {k} graphic item(s) -> {a.out}\n"
          f"  put it in place on a scratch copy (kproj), then kverify.py DIR and kfront.py NET check")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('net')
    ap.add_argument('cmd', choices=['check', 'pages', 'draw'])
    ap.add_argument('what', nargs='*', help='draw: powertree or i2c')
    ap.add_argument('--all', action='store_true', help="check: every sheet's text, not just part-less ones")
    ap.add_argument('-o', '--out', default='', help='pages: write the cover with the table rebuilt; draw: the new page')
    ap.add_argument('--page', default='', help='draw: the page number written in the new sheet (1)')
    ap.add_argument('--title', default='', help='draw: the title block title')
    a = parse_args(ap)
    try:
        nl = Netlist(a.net)
    except FileNotFoundError:
        print(f"no such netlist: {a.net}", file=sys.stderr); return 3
    return {'check': c_check, 'pages': c_pages, 'draw': c_draw}[a.cmd](nl, a)


if __name__ == '__main__':
    sys.exit(main() or 0)
