#!/usr/bin/env python3
"""
kverify.py - is the working tree the same circuit as a git ref? The before/after
check for a project-structure edit (sheet rename, reorder, page shift, a GUI
save that should only have moved things). One PASS/FAIL line per check.

  kverify.py [DIR] [--ref HEAD] [--pages 1,2,3] [--no-erc]

  netlist  kmerge both trees, `knet diff`: no part, field, connection or net name
           may change
  ERC      kicad-cli ERC on both: violations keyed by type + item descriptions
           (no coordinates, no sheet names, so a move or a rename is not a change);
           only added / removed ones are reported
  sync     `kpcb.py sync` on both (board vs its own netlist), paths and dates
           stripped: the board must not drift either
  pages    `sch export pdf` of the working tree: each page's title block Id must
           run 1..N in print order
  sheets   (INFO) per changed .kicad_sch, a coordinate-delta summary: "933
           coordinates moved, all by (-2.54,+0)" vets a GUI save at a glance;
           "structure changed" means more than coordinates moved
  render   with --pages: those working-tree pages against the ref page with the
           same sheet path, rasterised at 50 dpi, pixels differing > 40/255; the
           bounding box of the change in sheet mm

Both trees are frozen copies in a temp dir (KiCad may be saving the real one).
Exit: 0 all PASS, 2 a FAIL, 3 a tool failed.
"""
import sys, os, re, glob, json, shutil, fnmatch, tempfile, subprocess, argparse
from collections import Counter

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from kcommon import kicad_cli, top_level_sheets, parse_args  # noqa: E402

KICAD = ('*.kicad_sch', '*.kicad_pro', '*.kicad_pcb', '*.kicad_dru', 'sym-lib-table', 'fp-lib-table')
COORD = re.compile(r'\((at|xy|start|end|mid|center) (-?[\d.]+) (-?[\d.]+)')


def sh(args, **kw):
    return subprocess.run(args, capture_output=True, text=True, **kw)


def freeze(d, ref, out):
    """the project's KiCad files (and lib/) at ref, or as on disk when ref is None"""
    os.makedirs(out)
    if ref is None:
        for pat in KICAD:
            for f in glob.glob(os.path.join(d, pat)):
                if not os.path.basename(f).startswith(('_autosave-', '~')):
                    shutil.copy2(f, out)
        if os.path.isdir(os.path.join(d, 'lib')):
            os.symlink(os.path.join(d, 'lib'), os.path.join(out, 'lib'))
        return out
    pre = sh(['git', '-C', d, 'rev-parse', '--show-prefix']).stdout.strip()
    names = sh(['git', '-C', d, 'ls-tree', '-r', '-z', '--name-only', f'{ref}:{pre}']).stdout.split('\0')
    want = [n for n in names if '/' not in n and any(fnmatch.fnmatch(n, p) for p in KICAD)]
    want += ['lib'] * any(n.startswith('lib/') for n in names)
    if not want:
        raise RuntimeError(f"no KiCad files at {ref}:{pre}")
    tar = subprocess.run(['git', '-C', d, 'archive', f'{ref}:{pre}', '--'] + want, capture_output=True)
    if tar.returncode:
        raise RuntimeError(tar.stderr.decode()[:200])
    subprocess.run(['tar', '-x', '-C', out], input=tar.stdout, check=True)
    return out


def root_of(d):
    """the sheet named after the .kicad_pro (kicad-cli reaches every top-level
    sheet from it), else the first top-level sheet, else the only one"""
    for p in sorted(glob.glob(os.path.join(d, '*.kicad_pro'))):
        if os.path.isfile(p[:-4] + '_sch'):
            return p[:-4] + '_sch'
    tl = top_level_sheets(d)
    return os.path.join(d, tl[0][0]) if tl else sorted(glob.glob(os.path.join(d, '*.kicad_sch')))[0]


def netlist(d):
    out = os.path.join(d, 'kverify.net')
    r = sh([sys.executable, os.path.join(HERE, 'kmerge.py'), out, root_of(d)])
    if r.returncode or not os.path.isfile(out):
        raise RuntimeError(f"kmerge failed in {d}: {(r.stderr or r.stdout)[-200:]}")
    return out


def c_netlist(ref, now):
    r = sh([sys.executable, os.path.join(HERE, 'knet.py'), now['net'], 'diff', ref['net']],
           env={**os.environ, 'KREVIEW_NO_REGEN': '1'})
    counts = [(m.group(1), int(m.group(2))) for m in re.finditer(r'=== (.*?) \((\d+)\) ===', r.stdout)]
    if not counts:
        return 'FAIL', f"knet diff gave no summary: {(r.stderr or r.stdout)[-160:]}"
    bad = [f"{n} {k}" for k, n in counts if n and not k.startswith('pins that only gained')]
    return ('FAIL', '; '.join(bad)) if bad else ('PASS', "0 changes")


def erc_keys(d):
    out = os.path.join(d, 'kverify-erc.json')
    root = root_of(d)
    r = sh([kicad_cli(root), 'sch', 'erc', '--format', 'json', '--units', 'mm', '-o', out, root])
    if not os.path.isfile(out):
        raise RuntimeError(f"ERC failed in {d}: {(r.stderr or r.stdout)[-200:]}")
    vs = [v for s in json.load(open(out)).get('sheets', []) for v in s.get('violations', [])]
    return Counter((v.get('type'), tuple(sorted(i.get('description', '') for i in v.get('items', []))))
                   for v in vs)


def c_erc(ref, now):
    a, b = erc_keys(ref['dir']), erc_keys(now['dir'])
    add, gone = b - a, a - b
    msg = f"{sum(add.values())} added, {sum(gone.values())} removed ({sum((a & b).values())} same)"
    eg = [f"+ {t}: {'; '.join(i)[:90]}" for t, i in list(add)[:3]] + \
         [f"- {t}: {'; '.join(i)[:90]}" for t, i in list(gone)[:3]]
    return ('FAIL' if add or gone else 'PASS'), msg + ''.join(f"\n        {x}" for x in eg)


def sync_text(t):
    pcb = sorted(glob.glob(os.path.join(t['dir'], '*.kicad_pcb')))
    if not pcb:
        return None
    r = sh([sys.executable, os.path.join(HERE, 'kpcb.py'), pcb[0], 'sync', t['net']],
           env={**os.environ, 'KREVIEW_NO_REGEN': '1'})
    txt = (r.stdout + r.stderr).replace(t['dir'], '<dir>')
    return [x for x in txt.splitlines() if not re.search(r'\d{4}-\d\d-\d\d|\d\d:\d\d:\d\d', x)]


def c_sync(ref, now):
    a, b = sync_text(ref), sync_text(now)
    if a is None or b is None:
        return 'INFO', "no .kicad_pcb in one tree"
    if a == b:
        return 'PASS', f"kpcb sync output identical ({len(b)} lines)"
    import difflib
    d = [x for x in difflib.unified_diff(a, b, n=0, lineterm='') if x[:1] in '+-' and x[:3] not in ('+++', '---')]
    return 'FAIL', f"{len(d)} line(s) differ" + ''.join(f"\n        {x[:110]}" for x in d[:6])


def pdf_pages(d):
    """[(page index, Id n, of N, sheet path)] from `sch export pdf`'s title blocks"""
    pdf = os.path.join(d, 'kverify.pdf')
    root = root_of(d)
    sh([kicad_cli(root), 'sch', 'export', 'pdf', '-o', pdf, root])
    if not os.path.isfile(pdf):
        raise RuntimeError(f"pdf export failed in {d}")
    n = int(re.search(r'Pages:\s+(\d+)', sh(['pdfinfo', pdf]).stdout).group(1))
    out = []
    for i in range(1, n + 1):
        t = sh(['pdftotext', '-f', str(i), '-l', str(i), pdf, '-']).stdout
        m, p = re.search(r'Id: (\d+)/(\d+)', t), re.search(r'Sheet: (\S.*)', t)
        out.append((i, int(m.group(1)) if m else 0, int(m.group(2)) if m else 0, p.group(1).strip() if p else '?'))
    return pdf, out


def c_pages(now):
    now['pdf'], now['pages'] = pdf_pages(now['dir'])
    pages = now['pages']
    ids = [p[1] for p in pages]
    seq = ' '.join(f"{p[1]}{p[3]}" for p in pages)
    ok = ids == list(range(1, len(pages) + 1)) and all(p[2] == len(pages) for p in pages)
    return ('PASS' if ok else 'FAIL'), f"{len(pages)} pages, Ids {'run 1..N' if ok else 'OUT OF ORDER'}: {seq[:300]}"


PAGE = re.compile(r'\(page "([^"]*)"\)')


def delta_summary(a, b):
    """'N coordinates moved, all by (dx,dy)' / '... by K offsets: ...' / 'structure
    changed', with page renumbering ('page 6 -> 9') split out first"""
    pa, pb = PAGE.findall(a), PAGE.findall(b)
    pages = ''
    if len(pa) == len(pb) and pa != pb:
        pages = 'page ' + ', '.join(f"{x} -> {y}" for x, y in zip(pa, pb) if x != y) + '; '
        a, b = PAGE.sub('(page)', a), PAGE.sub('(page)', b)
    return pages + _moves(a, b)


def _moves(a, b):
    ca, cb = COORD.findall(a), COORD.findall(b)
    if len(ca) == len(cb) and COORD.sub('(C)', a) == COORD.sub('(C)', b):
        moves = Counter((round(float(y[1]) - float(x[1]), 4), round(float(y[2]) - float(x[2]), 4))
                        for x, y in zip(ca, cb) if x[1:] != y[1:])
        n = sum(moves.values())
        if not n:
            return "other text changed, no coordinates" if a != b else "nothing else changed"
        if len(moves) == 1:
            (dx, dy), = moves
            return f"{n} coordinates moved, all by ({dx:+g},{dy:+g})"
        return f"{n} coordinates moved by {len(moves)} offsets: " + ', '.join(
            f"({dx:+g},{dy:+g}) x{c}" for (dx, dy), c in moves.most_common(4))
    import difflib
    d = sum(1 for x in difflib.unified_diff(a.splitlines(), b.splitlines(), n=0, lineterm='')
            if x[:1] in '+-' and x[:3] not in ('+++', '---'))
    return f"structure changed ({d} diff lines)"


def c_sheets(ref, now):
    fa = {os.path.basename(f) for f in glob.glob(os.path.join(ref['dir'], '*.kicad_sch'))}
    fb = {os.path.basename(f) for f in glob.glob(os.path.join(now['dir'], '*.kicad_sch'))}
    out = []
    for f in sorted(fa | fb):
        if f not in fa or f not in fb:
            out.append(f"{f}: {'added' if f in fb else 'removed'}")
            continue
        a, b = (open(os.path.join(t['dir'], f), encoding='utf-8').read() for t in (ref, now))
        if a != b:
            out.append(f"{f}: {delta_summary(a, b)}")
    return 'INFO', ('; '.join(out) if out else 'no .kicad_sch changed')


def ppm(pdf, page, dpi=50):
    """(w, h, bytes RGB) of one page, via pdftoppm's binary PPM"""
    raw = subprocess.run(['pdftoppm', '-r', str(dpi), '-f', str(page), '-l', str(page), pdf],
                         capture_output=True).stdout
    m = re.match(rb'P6\s+(\d+)\s+(\d+)\s+(\d+)\s', raw)
    return int(m.group(1)), int(m.group(2)), raw[m.end():]


def c_render(ref, now, pages, dpi=50):
    if 'pdf' not in ref:
        ref['pdf'], ref['pages'] = pdf_pages(ref['dir'])
    rp = {p[3]: p[0] for p in ref['pages']}
    out = []
    for n in pages:
        np_ = next((p for p in now['pages'] if p[1] == n), None)
        if not np_:
            out.append(('FAIL', f"p{n}", "no such page")); continue
        if np_[3] not in rp:
            out.append(('INFO', f"p{n} {np_[3]}", "no page with this sheet path at the ref")); continue
        (w, h, a), (w2, h2, b) = ppm(ref['pdf'], rp[np_[3]], dpi), ppm(now['pdf'], np_[0], dpi)
        if (w, h) != (w2, h2):
            out.append(('FAIL', f"p{n} {np_[3]}", f"page size {w}x{h} -> {w2}x{h2} px")); continue
        xs, ys = [], []
        for i in range(0, len(a), 3):
            if max(abs(a[i] - b[i]), abs(a[i + 1] - b[i + 1]), abs(a[i + 2] - b[i + 2])) > 40:
                xs.append(i // 3 % w); ys.append(i // 3 // w)
        mm = 25.4 / dpi
        out.append(('PASS', f"p{n} {np_[3]}", "0 px differ") if not xs else
                   ('FAIL', f"p{n} {np_[3]}", f"{len(xs)} px differ in {min(xs) * mm:.0f},{min(ys) * mm:.0f} - "
                                              f"{max(xs) * mm:.0f},{max(ys) * mm:.0f} mm"))
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('dir', nargs='?', default='.')
    ap.add_argument('--ref', default='HEAD')
    ap.add_argument('--pages', default='', help='render-diff these working-tree pages, e.g. 1,2,3')
    ap.add_argument('--no-erc', action='store_true', help='skip the two ERC runs (~20 s)')
    ap.add_argument('--keep', action='store_true', help='keep the temp trees and print where')
    a = parse_args(ap)
    d = os.path.abspath(a.dir)
    tmp = tempfile.mkdtemp(prefix='kverify-')
    bad = False
    try:
        ref = {'dir': freeze(d, a.ref, os.path.join(tmp, 'ref'))}
        now = {'dir': freeze(d, None, os.path.join(tmp, 'now'))}
        print(f"kverify: {d} working tree vs {a.ref}")
        for t in (ref, now):
            t['net'] = netlist(t['dir'])
        checks = [('netlist', lambda: c_netlist(ref, now))]
        if not a.no_erc:
            checks.append(('ERC', lambda: c_erc(ref, now)))
        checks += [('sync', lambda: c_sync(ref, now)), ('pages', lambda: c_pages(now)),
                   ('sheets', lambda: c_sheets(ref, now))]
        for name, fn in checks:
            st, msg = fn()
            bad |= st == 'FAIL'
            print(f"{st:<5} {name:<8} {msg}", flush=True)
        if a.pages:
            for st, name, msg in c_render(ref, now, [int(x) for x in a.pages.split(',') if x.strip()]):
                bad |= st == 'FAIL'
                print(f"{st:<5} render {name}: {msg}")
    except (RuntimeError, subprocess.CalledProcessError, OSError) as e:
        print(f"kverify: {e}", file=sys.stderr)
        return 3
    finally:
        if a.keep:
            print(f"(trees kept in {tmp})")
        else:
            shutil.rmtree(tmp, ignore_errors=True)
    return 2 if bad else 0


if __name__ == '__main__':
    sys.exit(main() or 0)
