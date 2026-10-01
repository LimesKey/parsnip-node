#!/usr/bin/env python3
"""
ksheet.py - schematic SHEET geometry: where a symbol sits on its .kicad_sch, the
net every wire and label belongs to, readability lint ERC never raises, and a
cropped picture of any region. The .kicad_sch files beside the netlist are read;
nets come from the netlist, so a drawing is always named the way the board is.

  ksheet.py FILE.net sch REF [-r MM]       sheet, at/rot/mirror, body, each pin's
                                           end + side + net, and every symbol,
                                           wire, label, junction within R mm (5)
  ksheet.py FILE.net lint [SHEET] [--around REF] [-r MM]
                                           readability findings, one sheet or all
  ksheet.py FILE.net view REF [-r MM] [-o OUT.png]
  ksheet.py FILE.net view SHEET --box X0,Y0,X1,Y1 [-o OUT.png]
                                           crop of kicad-cli's own SVG plot -> PNG

SHEET is a file (bms.kicad_sch) or a sheet name (BMS, /Root/GNSS/). Coordinates
are sheet mm, +y down, as KiCad shows them in the status bar. `side` is where the
wire leaves a pin (L/R/U/D). A wire endpoint, pin, label or junction touching a
wire connects to it; two wires crossing without a junction do not.

lint rules (all readability - the netlist is not wrong, the drawing misleads):
  WIREBODY  a wire runs through a symbol's body (a shunt cap drawn as series)
  GAPLINE   collinear wires of different nets with an empty gap <= 2.54 mm
            between them (reads as one line: a bypassed part)
  PINJOG    a net runs one step off its pin's row and jogs at the pin
  CROWD     other objects crowd an IC's pin ends (--crowd MM, 2.54)

`view` plots every sheet once per schematic save (cached in
~/.cache/kicad-review/svg), crops the viewBox (mm) and rasterises with
rsvg-convert. Exit: 0 ok, 1 not found, 2 lint found a WARN, 3 tool failure.
"""
import sys, os, re, math, glob, hashlib, subprocess, argparse
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kcommon import (Netlist, kid, kids, val, load_sexp, sym_geometry, natkey,  # noqa: E402
                     print_findings, kicad_cli, sch_files, CACHE)

RULES = {
    'WIREBODY': 'a wire runs through a symbol body',
    'GAPLINE': 'different nets end-to-end on one line with an empty gap',
    'PINJOG': 'net runs off its pin row and jogs at the pin',
    'CROWD': "other objects crowd an IC's pin ends",
}
F = lambda x: float(x)                                      # noqa: E731


def K(x, y):
    """point key: KiCad stores 0.01 mm grid coordinates"""
    return (round(x * 100), round(y * 100))


class UF(dict):
    def find(self, a):
        self.setdefault(a, a)
        while self[a] != a:
            self[a] = self[self[a]]
            a = self[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self[ra] = rb


def inside(p, s):
    """point strictly inside axis-aligned segment s (diagonals: never)"""
    (x0, y0), (x1, y1) = s
    x, y = p
    if abs(x0 - x1) < 1e-3 and abs(x - x0) < 1e-3:
        return min(y0, y1) + 1e-3 < y < max(y0, y1) - 1e-3
    if abs(y0 - y1) < 1e-3 and abs(y - y0) < 1e-3:
        return min(x0, x1) + 1e-3 < x < max(x0, x1) - 1e-3
    return False


def box_dist(a, b):
    return math.hypot(max(0, a[0] - b[2], b[0] - a[2]), max(0, a[1] - b[3], b[1] - a[3]))


def seg_box(s):
    (x0, y0), (x1, y1) = s
    return (min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))


class Sheet:
    """One .kicad_sch file: symbols (power ones too), wires, labels, junctions,
    no-connects, and a net name per connected cluster from the netlist."""

    def __init__(self, nl, base, path=None):
        si = nl.sch()
        il = si.inst_list(base)                    # a sheet used twice: one Sheet per use
        self.path, suuid = next(((p, u) for p, u in il if p == path), il[0])
        self.nl, self.base = nl, base
        tree = load_sexp(os.path.join(os.path.dirname(os.path.abspath(nl.path)), base))
        lib = si.libsyms.get(base, {})
        self.syms = {}                                        # ref -> merged over units
        for inst in kids(tree, 'symbol'):
            lid = val(inst, 'lib_id')
            at = kid(inst, 'at')
            if not lid or lid not in lib or not at:
                continue
            ref, value = si.inst_ref(inst, suuid), si._prop(inst, 'Value')
            m = kid(inst, 'mirror')
            rot, mir = F(at[3]) if len(at) > 3 else 0.0, m[1] if m and len(m) > 1 else ''
            unit = int(val(inst, 'unit') or 1)
            pins, body = sym_geometry(lib[lid], unit, F(at[1]), F(at[2]), rot, mir)
            s = self.syms.setdefault(ref, {'ref': ref, 'value': value, 'lib': lid, 'pins': {},
                                           'bodies': [], 'at': (F(at[1]), F(at[2])), 'rot': rot,
                                           'mir': mir, 'units': [],
                                           'power': ref.startswith('#') or bool(kid(lib[lid], 'power'))})
            s['pins'].update(pins)
            s['units'].append(unit)
            if body:
                s['bodies'].append(body)
        self.wires = []
        for w in kids(tree, 'wire'):
            xy = [(F(q[1]), F(q[2])) for q in kids(kid(w, 'pts') or [], 'xy')]
            self.wires += [(a, b) for a, b in zip(xy, xy[1:]) if K(*a) != K(*b)]
        self.labels = [(t, lb[1], F(kid(lb, 'at')[1]), F(kid(lb, 'at')[2]))
                       for t in ('label', 'global_label', 'hierarchical_label')
                       for lb in kids(tree, t)]
        for sh in kids(tree, 'sheet'):                        # sheet pins act as labels
            self.labels += [('sheet_pin', p[1], F(kid(p, 'at')[1]), F(kid(p, 'at')[2]))
                            for p in kids(sh, 'pin')]
        self.junctions = [(F(kid(j, 'at')[1]), F(kid(j, 'at')[2])) for j in kids(tree, 'junction')]
        self.ncs = [(F(kid(j, 'at')[1]), F(kid(j, 'at')[2])) for j in kids(tree, 'no_connect')]
        self._connect()

    def _connect(self):
        uf = UF()
        for a, b in self.wires:
            uf.union(K(*a), K(*b))
        pts = {K(*p): p for s in self.wires for p in s}
        for s in self.syms.values():
            for x, y, _side, _nm in s['pins'].values():
                pts[K(x, y)] = (x, y)
        for _t, _n, x, y in self.labels:
            pts[K(x, y)] = (x, y)
        for p in self.junctions + self.ncs:
            pts[K(*p)] = p
        # ponytail: points x wires scan, ~0.2 s on the biggest sheet; bucket by
        # coordinate if a sheet ever gets 10x bigger
        for k, p in pts.items():
            uf.find(k)
            for s in self.wires:
                if inside(p, s):
                    uf.union(k, K(*s[0]))
        self.uf = uf
        votes = defaultdict(Counter)
        for s in self.syms.values():
            if s['power']:
                continue
            for num, (x, y, _side, _nm) in s['pins'].items():
                n = self.nl.cpins.get(s['ref'], {}).get(num)
                if n:
                    votes[uf.find(K(x, y))][n] += 1
        names = defaultdict(Counter)
        for s in self.syms.values():
            if s['power'] and not s['ref'].startswith('#FLG'):
                for x, y, _side, _nm in s['pins'].values():
                    names[uf.find(K(x, y))][s['value']] += 1
        for t, n, x, y in self.labels:
            names[uf.find(K(x, y))][n if t == 'global_label' else self.path + n] += 1
        self.netof = {}
        for root in set(votes) | set(names):
            v = votes.get(root)
            if v:
                self.netof[root] = ' | '.join(sorted(v)) + ' (?)' if len(v) > 1 else next(iter(v))
            else:
                self.netof[root] = names[root].most_common(1)[0][0]

    def net(self, p):
        return self.netof.get(self.uf.find(K(*p)), 'unconnected')

    def extent(self, ref):
        """body boxes plus every pin end: what `-r` is measured from"""
        s = self.syms[ref]
        xs = [p[0] for p in s['pins'].values()] + [b[i] for b in s['bodies'] for i in (0, 2)]
        ys = [p[1] for p in s['pins'].values()] + [b[i] for b in s['bodies'] for i in (1, 3)]
        return (min(xs), min(ys), max(xs), max(ys)) if xs else (*s['at'], *s['at'])


def sheet_of(nl, spec):
    """REF or SHEET (file, 'BMS', '/Root/GNSS/') -> (Sheet, ref or None)"""
    si = nl.sch()
    if not si.files:
        sys.exit(f"no .kicad_sch beside {nl.path}")
    if spec in si.place:
        return Sheet(nl, *si.place[spec][0][:2]), spec
    want = spec.strip('/').split('/')[-1].lower()
    for base in si.sheetpath:
        for path, _u in si.inst_list(base):
            if spec in (base, path) or want in (base.lower(), base.lower().rsplit('.', 1)[0],
                                                path.strip('/').split('/')[-1].lower()):
                return Sheet(nl, base, path), None
    sys.exit(f"no symbol or sheet named {spec!r} (sheets: {', '.join(sorted(si.sheetpath))})")


def fmt(p):
    return f"{p[0]:g},{p[1]:g}"


# ---------------------------------------------------------------- sch REF

def c_sch(nl, a):
    sh, ref = sheet_of(nl, a.target)
    if not ref:
        print(f"{a.target} is a sheet; `sch` wants a REF (try `lint {a.target}`)"); return 1
    s = sh.syms[ref]
    others = [p for p in nl.sch().place[ref][1:] if p[0] != sh.base]
    print(f"{ref} [{s['value']}]  {s['lib']}")
    print(f"  sheet {sh.base} ({sh.path})  at {fmt(s['at'])} mm  rot {s['rot']:g}  "
          f"mirror {s['mir'] or '-'}  unit {','.join(map(str, s['units']))}"
          + (f"  (+ units on {', '.join(sorted({p[0] for p in others}))})" if others else ''))
    for b in s['bodies']:
        print(f"  body {fmt(b[:2])} - {fmt(b[2:])} mm")
    print(f"  {'pin':<5} {'name':<12} side  {'end (mm)':<17} net")
    for num, (x, y, side, nm) in sorted(s['pins'].items(), key=lambda z: natkey(z[0])):
        print(f"  {num:<5} {nm[:12]:<12} {side:<5} {fmt((x, y)):<17} {sh.net((x, y))}")
    ext, r = sh.extent(ref), a.r
    near = []
    for o in sh.syms.values():
        if o['ref'] == ref:
            continue
        d = box_dist(ext, sh.extent(o['ref']))
        if d <= r:
            what = 'power' if o['power'] else 'symbol'
            pins = ', '.join(f"{n} {sh.net(p[:2])}" for n, p in sorted(o['pins'].items(),
                                                                         key=lambda z: natkey(z[0])))
            near.append((d, f"{what:<8} {o['ref'] if not o['power'] else o['value']:<10} "
                            f"at {fmt(o['at'])}  {pins}"))
    for w in sh.wires:
        d = box_dist(ext, seg_box(w))
        if d <= r:
            near.append((d, f"{'wire':<8} {fmt(w[0])} - {fmt(w[1])}  {sh.net(w[0])}"))
    for t, n, x, y in sh.labels:
        d = box_dist(ext, (x, y, x, y))
        if d <= r:
            near.append((d, f"{'label':<8} {n} ({t}) at {fmt((x, y))}  {sh.net((x, y))}"))
    for p in sh.junctions:
        if box_dist(ext, (*p, *p)) <= r:
            near.append((box_dist(ext, (*p, *p)), f"{'junction':<8} {fmt(p)}  {sh.net(p)}"))
    for p in sh.ncs:
        if box_dist(ext, (*p, *p)) <= r:
            near.append((box_dist(ext, (*p, *p)), f"{'nc':<8} {fmt(p)}"))
    print(f"\nwithin {r:g} mm of the body + pin ends ({len(near)}):")
    for d, line in sorted(near, key=lambda z: z[0]):
        print(f"  {d:5.2f}  {line}")
    return 0


# ---------------------------------------------------------------- lint

def lint_sheet(sh, crowd=2.54, box=None):
    """findings for one sheet; `box` limits them to a region"""
    out = []
    def keep(*pts):
        return box is None or any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in pts)
    def f(rule, msg, refs=(), sev='WARN'):
        out.append({'severity': sev, 'rule': rule, 'msg': f"{sh.base}: {msg}", 'refs': list(refs)})
    real = [s for s in sh.syms.values() if not s['power']]
    # WIREBODY: a wire through a body interior (0.1 mm in). One that ends at the
    # symbol's own pin and leaves the way the pin faces is fine: an LED's arrows
    # put its pin ends inside the graphics box.
    OUT = {'L': (-1, 0), 'R': (1, 0), 'U': (0, -1), 'D': (0, 1)}
    def leaves(w, s):
        for e, far in ((w[0], w[1]), (w[1], w[0])):
            for x, y, side, _nm in s['pins'].values():
                if K(x, y) == K(*e):
                    ox, oy = OUT[side]
                    if (far[0] - x) * ox + (far[1] - y) * oy > 0:
                        return True
        return False
    for w in sh.wires:
        wb = seg_box(w)
        for s in real:
            if leaves(w, s):
                continue
            for b in s['bodies']:
                ib = (b[0] + .1, b[1] + .1, b[2] - .1, b[3] - .1)
                if ib[0] < ib[2] and ib[1] < ib[3] and box_dist(wb, ib) == 0 and keep(*w):
                    f('WIREBODY', f"wire {fmt(w[0])}-{fmt(w[1])} ({sh.net(w[0])}) runs through "
                                  f"{s['ref']}'s body - it reads as wired in series", [s['ref']])
    # GAPLINE: sort collinear segments along their line, check each facing pair
    lines = defaultdict(list)
    for w in sh.wires:
        (x0, y0), (x1, y1) = w
        if K(x0, 0)[0] == K(x1, 0)[0]:
            lines[('v', K(x0, 0)[0])].append((min(y0, y1), max(y0, y1), w))
        elif K(0, y0)[1] == K(0, y1)[1]:
            lines[('h', K(0, y0)[1])].append((min(x0, x1), max(x0, x1), w))
    pinat = {K(p[0], p[1]): s['ref'] for s in real for p in s['pins'].values()}
    for (o, c), segs in lines.items():
        segs.sort(key=lambda z: z[0])
        for (lo0, hi0, w0), (lo1, hi1, w1) in zip(segs, segs[1:]):
            gap = lo1 - hi0
            if not 0.01 < gap <= 2.55 or sh.net(w0[0]) == sh.net(w1[0]):      # <= 2 x 50 mil
                continue
            c_mm = c / 100
            e0, e1 = ((c_mm, hi0), (c_mm, lo1)) if o == 'v' else ((hi0, c_mm), (lo1, c_mm))
            g = seg_box((e0, e1))
            if pinat.get(K(*e0)) and pinat.get(K(*e0)) == pinat.get(K(*e1)):
                continue                                       # a part's two pins: in series
            if any(box_dist(g, b) == 0 for s in sh.syms.values() for b in s['bodies']) \
                    or not keep(e0, e1):
                continue                                       # a part or power symbol sits in the gap
            f('GAPLINE', f"{sh.net(w0[0])} ends at {fmt(e0)} and {sh.net(w1[0])} starts {gap:.2f} mm "
                         f"further on the same line - reads as one wire",
              [r for r in (pinat.get(K(*e0)), pinat.get(K(*e1))) if r])
    # PINJOG: the wire at a pin leaves sideways by one 50 mil step, then runs
    # parallel (a 2.54 mm sideways stub is the usual tie between two pins)
    ends = defaultdict(list)
    for w in sh.wires:
        ends[K(*w[0])].append(w); ends[K(*w[1])].append(w)
    for s in real:
        for num, (x, y, side, nm) in s['pins'].items():
            horiz = side in 'LR'
            for w in ends.get(K(x, y), []):
                far = w[1] if K(*w[0]) == K(x, y) else w[0]
                dx, dy = far[0] - x, far[1] - y
                if (abs(dx) > .01) == horiz or math.hypot(dx, dy) > 1.28 or not keep((x, y)):
                    continue                                   # leaves along the pin: fine
                if any((abs(v[0][1] - v[1][1]) < .01) == horiz for v in ends.get(K(*far), []) if v is not w):
                    f('PINJOG', f"{s['ref']}.{num} ({nm or '~'}) at {fmt((x, y))}: {sh.net((x, y))} "
                                f"runs {math.hypot(dx, dy):.2f} mm off the pin's row and jogs at "
                                f"the pin", [s['ref']])
    # CROWD: objects close to an IC's pin ends (not AT one). Another part's pin
    # always counts; a bend, junction or label only on a net other than the pin's
    # (its own label or strap is the normal way to draw it).
    objs = [(p, f"junction {fmt(p)}", True) for p in sh.junctions]
    objs += [((x, y), f"label {n}", True) for _t, n, x, y in sh.labels]
    for o in real:
        objs += [((p[0], p[1]), f"{o['ref']}.{n}", False) for n, p in o['pins'].items()]
    for k, ws in ends.items():                                 # wire bends
        if len(ws) == 2 and (ws[0][0][0] == ws[0][1][0]) != (ws[1][0][0] == ws[1][1][0]):
            objs.append(((k[0] / 100, k[1] / 100), f"wire bend {k[0]/100:g},{k[1]/100:g}", True))
    for s in real:
        if len(s['pins']) < 6:
            continue
        mine = {K(p[0], p[1]) for p in s['pins'].values()}
        hits = set()
        for num, (x, y, _side, _nm) in s['pins'].items():
            for p, what, netted in objs:
                if K(*p) in mine or what.startswith(s['ref'] + '.') \
                        or (netted and sh.net(p) == sh.net((x, y))):
                    continue
                d = math.hypot(p[0] - x, p[1] - y)
                if d <= crowd and keep(p):
                    hits.add(f"{what} ({d:.2f} mm from pin {num})")
        if hits:
            f('CROWD', f"{s['ref']}: {len(hits)} object(s) within {crowd:g} mm of its pin ends: "
                       + '; '.join(sorted(hits)), [s['ref']], 'INFO')
    return out


def c_lint(nl, a):
    si = nl.sch()
    box = None
    if a.around:
        sh, _ = sheet_of(nl, a.around)
        e = sh.extent(a.around)
        box, sheets = (e[0] - a.r, e[1] - a.r, e[2] + a.r, e[3] + a.r), [sh]
    elif a.target:
        sheets = [sheet_of(nl, a.target)[0]]
    else:
        sheets = [Sheet(nl, os.path.basename(b)) for b in si.files]
    F_ = [x for sh in sheets for x in lint_sheet(sh, a.crowd, box)]
    only = {r.strip().upper() for r in a.only.split(',') if r.strip()}
    skip = {r.strip().upper() for r in a.skip.split(',') if r.strip()}
    F_ = [x for x in F_ if (not only or x['rule'] in only) and x['rule'] not in skip]
    scope = f"around {a.around} ({a.r:g} mm)" if a.around else ', '.join(sh.base for sh in sheets)
    n = Counter(x['severity'] for x in F_)
    print_findings(F_, f"schematic lint {scope}: {n['WARN']} warn, {n['INFO']} info",
                   rules=RULES, cap=a.max)
    if not F_:
        print("no findings")
    return 2 if n['WARN'] else 0


# ---------------------------------------------------------------- view

def plots(nl):
    """kicad-cli's SVG of every sheet, re-plotted once per schematic save"""
    d = os.path.dirname(os.path.abspath(nl.path))
    files = sch_files(d)
    tag = hashlib.sha1(('|'.join(f"{f}:{os.path.getmtime(f)}" for f in files)).encode()).hexdigest()[:12]
    out = os.path.join(CACHE, 'svg', tag)
    if not glob.glob(os.path.join(out, '*.svg')):
        os.makedirs(out, exist_ok=True)
        root = nl.source if os.path.isfile(nl.source or '') else files[0]
        r = subprocess.run([kicad_cli(root), 'sch', 'export', 'svg', '-e', '-o', out, root],
                           capture_output=True, text=True)
        if r.returncode:
            sys.exit(f"kicad-cli sch export svg failed:\n{r.stdout}{r.stderr}")
    return out


def c_view(nl, a):
    sh, ref = sheet_of(nl, a.target)
    if a.box:
        box = tuple(float(v) for v in a.box.split(','))
    elif ref:
        e = sh.extent(ref)
        box = (e[0] - a.r, e[1] - a.r, e[2] + a.r, e[3] + a.r)
    else:
        print("give a REF, or a SHEET with --box X0,Y0,X1,Y1"); return 1
    d = plots(nl)
    stem, name = sh.base.rsplit('.', 1)[0], sh.path.strip('/').split('/')[-1]
    svg = next((p for p in (os.path.join(d, stem + '.svg'),) if os.path.exists(p)), None) \
        or next(iter(glob.glob(os.path.join(d, f'*-{name}.svg'))), None)
    if not svg:
        print(f"no plot for {sh.base} in {d}"); return 3
    txt = open(svg, encoding='utf-8').read()
    w, h = box[2] - box[0], box[3] - box[1]
    txt = re.sub(r'width="[^"]*" height="[^"]*" viewBox="[^"]*"',
                 f'width="{w:g}mm" height="{h:g}mm" viewBox="{box[0]:g} {box[1]:g} {w:g} {h:g}"',
                 txt, count=1)
    out = a.out or os.path.join(CACHE, f"view_{ref or name}.png")
    tmp = out.rsplit('.', 1)[0] + '.svg'
    open(tmp, 'w', encoding='utf-8').write(txt)
    if out.endswith('.svg'):
        print(out); return 0
    r = subprocess.run(['rsvg-convert', '-b', 'white', '-w', str(int(w * a.px)), '-o', out, tmp],
                       capture_output=True, text=True)
    if r.returncode:
        print(f"rsvg-convert failed ({r.stderr.strip() or 'not installed?'}); the SVG is {tmp}")
        return 3
    print(f"{out}  ({sh.base}, {fmt(box[:2])} - {fmt(box[2:])} mm)")
    return 0


# ---------------------------------------------------------------- CLI

def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('file')
    ap.add_argument('cmd', choices=['sch', 'lint', 'view'])
    ap.add_argument('target', nargs='?', default='')
    ap.add_argument('-r', type=float, default=None, help='radius mm (sch 5, lint --around 10, view 10)')
    ap.add_argument('--around', default='', help='lint: only the region around this REF')
    ap.add_argument('--crowd', type=float, default=2.54, help='lint CROWD distance, mm')
    ap.add_argument('--box', default='', help='view: X0,Y0,X1,Y1 sheet mm')
    ap.add_argument('--px', type=float, default=20, help='view: pixels per mm (20)')
    ap.add_argument('-o', '--out', default='')
    ap.add_argument('--max', type=int, default=25)
    ap.add_argument('--only', default='', help='lint: comma list of rules to keep')
    ap.add_argument('--skip', default='', help='lint: comma list of rules to drop')
    a = ap.parse_args()
    a.r = a.r if a.r is not None else {'sch': 5.0}.get(a.cmd, 10.0)
    if a.cmd in ('sch', 'view') and not a.target:
        print(f"{a.cmd} needs a REF" + (" or SHEET --box" if a.cmd == 'view' else '')); return 1
    nl = Netlist(a.file)
    return {'sch': c_sch, 'lint': c_lint, 'view': c_view}[a.cmd](nl, a)


if __name__ == '__main__':
    try:
        import signal
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except Exception:
        pass
    sys.exit(main() or 0)
