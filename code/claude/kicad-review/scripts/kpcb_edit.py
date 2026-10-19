"""kpcb.py board writers. Each writes a NEW file (-o OUT, never the input) by text
surgery on KiCad's own formatting - only the edited spans change, every match count
is asserted - and leaves the verdict to KiCad: `kdrc.py OUT --unconnected` on the copy
(it refills the zones first).

  fixlines -o OUT              undo the 10.99 fp_line frame bug (Board._fix_lines)
  renet -o OUT                 netless tracks/vias whose ends touch one net take it
  move REF X Y [ROT] -o OUT    re-place a footprint (same side; pads/text turn with it)

`route` and `place` (kpcb_route.py, kpcb_place.py) write through PcbText too."""
import math, os, re, uuid
from collections import defaultdict
from kcommon import unesc_disp
from kpcb_board import xf, pad_box, pt_seg_dist
from kpcb_copper import UF, _local, _reach, _around
from kpcb_move import cu_layers, via_layers


def num(v):
    """a coordinate as KiCad writes it: <= 6 decimals, no trailing zeros"""
    s = f"{v:.6f}".rstrip('0').rstrip('.')
    return '0' if s in ('', '-0') else s


def qs(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"') + '"'


def norm_ang(a):
    a = math.fmod(a, 360.0)
    a = a - 360 if a > 180 else a + 360 if a <= -180 else a
    return 0.0 if abs(a) < 1e-9 else a


class PcbText:
    """The board as text, edited by span. A top-level item opens on a `\\n\\t(` line and
    closes on the next `\\n\\t)` line (KiCad's formatter; nothing deeper sits at one
    tab), so footprints, tracks and vias are found by uuid without re-serialising
    anything that was not edited. New tracks/vias go in before the first zone."""

    def __init__(self, path):
        self.path = path
        with open(path, encoding='utf-8') as fh:
            self.txt = fh.read()
        self.span, self.edits = {}, []
        for m in re.finditer(r'\n\t\((footprint|segment|arc|via)\b', self.txt):
            s = m.start() + 1
            e = self.txt.index('\n\t)', s) + 3
            u = re.search(r'\(uuid "([^"]+)"\)', self.txt[s:e])     # first uuid = the item's own
            if u:
                self.span[u.group(1)] = (s, e)
        z = self.txt.find('\n\t(zone')
        self.tail = (z if z >= 0 else self.txt.rindex('\n)')) + 1
        self._tears = None

    @property
    def tears(self):
        """teardrop zones [(start, end, net, layer, outline points)], found on first use"""
        if self._tears is None:
            self._tears = []
            for m in re.finditer(r'\n\t\(zone\n', self.txt):
                s = m.start() + 1
                e = self.txt.index('\n\t)', s) + 3
                blk = self.txt[s:e]
                if '(teardrop' not in blk:
                    continue
                net = re.search(r'\(net "((?:[^"\\]|\\.)*)"\)', blk)
                ly = re.search(r'\(layer "([^"]+)"\)', blk)
                pol = re.search(r'\(polygon\n\t+\(pts\n(.*?)\n\t+\)', blk, re.S)
                pts = [(float(x), float(y)) for x, y in re.findall(r'\(xy (\S+) (\S+)\)', pol[1])] if pol else []
                if net and ly and len(pts) >= 3:
                    self._tears.append((s, e, net[1].replace('\\"', '"'), ly[1], pts))
        return self._tears

    def text(self, uid):
        s, e = self.span[uid]
        return self.txt[s:e]

    def sub(self, uid, new):
        s, e = self.span[uid]
        if not new and self.txt[e:e + 1] == '\n':            # a deletion takes its line break too
            e += 1
        self.edits.append((s, e, new))

    def add(self, item):
        """queue a new item; returns the edit, so `edits.remove` can withdraw it (a rip-up)"""
        self.edits.append((self.tail, self.tail, item))
        return self.edits[-1]

    def segment(self, net, layer, w, a, b):
        return self.add(f"\t(segment\n\t\t(start {num(a[0])} {num(a[1])})\n\t\t(end {num(b[0])} {num(b[1])})\n"
                 f"\t\t(width {num(w)})\n\t\t(layer {qs(layer)})\n\t\t(net {qs(net)})\n"
                 f"\t\t(uuid \"{uuid.uuid4()}\")\n\t)\n")

    def via(self, net, x, y, size, drill, layers):
        return self.add(f"\t(via\n\t\t(at {num(x)} {num(y)})\n\t\t(size {num(size)})\n\t\t(drill {num(drill)})\n"
                 f"\t\t(layers {' '.join(qs(l) for l in layers)})\n\t\t(net {qs(net)})\n"
                 f"\t\t(uuid \"{uuid.uuid4()}\")\n\t)\n")

    def zone(self, net, layer, pts, priority, clearance=0.2, min_w=0.25):
        """an unfilled solid-connect zone (the refill draws its copper)"""
        xy = ''.join(f"\t\t\t\t(xy {num(x)} {num(y)})\n" for x, y in pts)
        return self.add(f"\t(zone\n\t\t(net {qs(net)})\n\t\t(layer {qs(layer)})\n\t\t(uuid \"{uuid.uuid4()}\")\n"
                        f"\t\t(name {qs('route ' + net)})\n\t\t(hatch edge 0.5)\n\t\t(priority {priority})\n"
                        f"\t\t(connect_pads yes\n\t\t\t(clearance {num(clearance)})\n\t\t)\n\t\t(min_thickness {num(min_w)})\n"
                        f"\t\t(fill yes\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n"
                        f"\t\t\t(island_removal_mode 0)\n\t\t)\n\t\t(polygon\n\t\t\t(pts\n{xy}\t\t\t)\n\t\t)\n\t)\n")

    def save(self, out):
        if os.path.abspath(out) == os.path.abspath(self.path):
            raise SystemExit("refusing to overwrite the input board: write a copy (-o OUT), "
                             "check it with kdrc.py, then move it into place")
        parts, pos = [], 0
        for s, e, new in sorted(self.edits, key=lambda x: (x[0], x[1])):
            if s < pos:
                raise SystemExit("internal: overlapping edits, nothing written")
            parts += [self.txt[pos:s], new]
            pos = e
        parts.append(self.txt[pos:])
        with open(out, 'w', encoding='utf-8') as fh:
            fh.write(''.join(parts))


def _need_out(a, cmd):
    if not a.out:
        print(f"{cmd} writes a copy: add -o OUT.kicad_pcb (the input is never overwritten)")
        return True
    return False


# ---------------------------------------------------------------- fixlines

FPLINE = re.compile(r'(\t\t\(fp_line\n\t\t\t\(start )(\S+) (\S+)(\)\n\t\t\t\(end )(\S+) (\S+)\)')


def c_fixlines(b, a):
    bad = [f for f in b.fps.values() if f.badlines]
    if not bad:
        print("no footprint has fp_lines in the wrong frame - nothing to fix"); return 0
    if _need_out(a, 'fixlines'):
        return 1
    t, n = PcbText(b.path), 0
    for f in bad:
        kx, ky = f.scale
        def fix(m, f=f, kx=kx, ky=ky):
            (x0, y0), (x1, y1) = (xf(float(u) * kx, float(v) * ky, f.x, f.y, f.rot)
                                  for u, v in ((m[2], m[3]), (m[5], m[6])))
            return f"{m[1]}{num(x0)} {num(y0)}{m[4]}{num(x1)} {num(y1)})"
        new, k = FPLINE.subn(fix, t.text(f.uuid))
        if k != f.badlines:
            raise SystemExit(f"{f.ref}: matched {k} fp_line(s) but parsed {f.badlines}; "
                             f"the file layout changed, nothing written")
        t.sub(f.uuid, new)
        n += k
    t.save(a.out)
    print(f"fixlines: {n} fp_line(s) in {len(bad)} footprint(s) put back in their footprint frame -> {a.out}\n"
          f"  check: kpcb.py {a.out} review (no frame WARNING), then kdrc.py {a.out}")
    return 0


# ---------------------------------------------------------------- renet

def _touch(A, B):
    """do copper items A and B join (KiCad anchor rules: a track end on a pad, a via or
    another track's body; a via on a pad or a track body), on a layer they share"""
    (ka, x), (kb, y) = A, B
    if ka == 'p':
        (ka, x), (kb, y) = B, A
    if ka == 't' and kb == 't':
        if x['layer'] != y['layer']:
            return False
        lim = (x['w'] + y['w']) / 2
        return any(pt_seg_dist(e, y['a'], y['b']) <= lim for e in (x['a'], x['b'])) or \
            any(pt_seg_dist(e, x['a'], x['b']) <= lim for e in (y['a'], y['b']))
    if {ka, kb} == {'t', 'v'}:
        t, v = (x, y) if ka == 't' else (y, x)
        return pt_seg_dist((v['x'], v['y']), t['a'], t['b']) <= v['size'] / 2 + t['w'] / 2
    if ka == 'v' and kb == 'v':
        return math.dist((x['x'], x['y']), (y['x'], y['y'])) < (x['size'] + y['size']) / 2
    p = y                                                    # kb == 'p'
    hx, hy = _reach(p)
    if ka == 't':
        hw = x['w'] / 2
        for e in (x['a'], x['b']):
            u, v = _local(p, e)
            if abs(u) <= hx + hw and abs(v) <= hy + hw:
                return True
        return pt_seg_dist((p['x'], p['y']), x['a'], x['b']) <= min(hx, hy) + hw
    u, v = _local(p, (x['x'], x['y']))                       # a via in a pad
    return abs(u) <= hx and abs(v) <= hy


def c_renet(b, a):
    """Tracks and vias with no net (their net was renamed or deleted under them on an
    Update PCB) take the net their ends touch. A cluster of joined netless copper that
    touches exactly one net gets it; two or more nets is a short or a stale crossing,
    left netless and listed; none is floating copper, listed for deletion. `--drop`
    deletes both kinds: a stale route bridging two nets is ripped up for re-routing."""
    cu, C = b.copper, 1.0
    NL = [('t', t) for t in b.tracks if not t['net'] and t['w'] > 0] + \
         [('v', v) for v in b.vias if not v['net']]
    if not NL:
        print("no netless track or via - nothing to do"); return 0
    grid = defaultdict(list)

    def cells(bb, lys):
        for ly in lys:
            for cx in range(math.floor(bb[0] / C), math.floor(bb[2] / C) + 1):
                for cy in range(math.floor(bb[1] / C), math.floor(bb[3] / C) + 1):
                    yield (ly, cx, cy)

    def geo(it):
        k, o = it
        if k == 't':
            hw = o['w'] / 2
            return (min(o['a'][0], o['b'][0]) - hw, min(o['a'][1], o['b'][1]) - hw,
                    max(o['a'][0], o['b'][0]) + hw, max(o['a'][1], o['b'][1]) + hw), {o['layer']}
        if k == 'v':
            r = o['size'] / 2
            return (o['x'] - r, o['y'] - r, o['x'] + r, o['y'] + r), via_layers(o, cu)
        return pad_box(o), (set(cu) if o['drill'] else cu_layers(o['layers'], cu))

    allc = [('p', p) for f in b.fps.values() for p in f.pads if p['kind'] != 'np_thru_hole' and p['net']] + \
           [('t', t) for t in b.tracks if t['net'] and t['w'] > 0] + [('v', v) for v in b.vias if v['net']] + NL
    for it in allc:
        bb, lys = geo(it)
        for c in cells(bb, lys):
            grid[c].append(it)
    U, contact = UF(), defaultdict(set)
    key = lambda it: (it[0], id(it[1]))                     # noqa: E731
    for it in NL:
        U.find(key(it))
        bb, lys = geo(it)
        seen = set()
        for c in cells(bb, lys):
            for o in grid.get(c, ()):
                if o[1] is it[1] or key(o) in seen:
                    continue
                seen.add(key(o))
                obb, olys = geo(o)
                if not (lys & olys) or not _touch(it, o):
                    continue
                if o[1]['net']:
                    contact[key(it)].add(o[1]['net'])
                else:
                    U.union(key(it), key(o))
    clusters = defaultdict(list)
    for it in NL:
        clusters[U.find(key(it))].append(it)
    t, assigned, amb, flo = PcbText(b.path), defaultdict(lambda: [0, 0]), [], []
    for items in clusters.values():
        nets = set().union(*(contact[key(it)] for it in items))
        at = items[0][1]['a'] if items[0][0] == 't' else (items[0][1]['x'], items[0][1]['y'])
        if len(nets) != 1:
            (amb if nets else flo).append((items, sorted(nets), at))
            continue
        net = nets.pop()
        for k, o in items:
            s = t.text(o['uuid'])
            if s.count('(net "")') != 1:
                raise SystemExit(f"a netless {k} at {at} does not read (net \"\") once; nothing written")
            t.sub(o['uuid'], s.replace('(net "")', f'(net {qs(net)})'))
            assigned[net][0 if k == 't' else 1] += 1
    nt, nv = sum(x[0] for x in assigned.values()), sum(x[1] for x in assigned.values())
    print(f"renet: {len(NL)} netless item(s) in {len(clusters)} cluster(s): {nt} track(s) + {nv} via(s) "
          f"get the one net they touch ({len(assigned)} net(s))")
    for net, (k, v) in sorted(assigned.items(), key=lambda kv: -sum(kv[1]))[:a.max]:
        print(f"  {unesc_disp(net):<34} {k:>3} trk {v:>2} via")
    if len(assigned) > a.max:
        print(f"  ... +{len(assigned) - a.max} more net(s)")
    tag = 'DROP' if a.drop else 'LEFT'
    for items, nets, at in amb:
        print(f"  {tag}  {len(items)} item(s) @{at[0]:.2f},{at[1]:.2f} touch {len(nets)} nets "
              f"({', '.join(unesc_disp(n) for n in nets[:4])}): a stale route or a short - re-route it")
    if flo:
        print(f"  {tag}  {sum(len(i) for i, *_ in flo)} floating item(s) in {len(flo)} cluster(s), touching no net: "
              + ', '.join(f"@{p[0]:.2f},{p[1]:.2f}" for *_x, p in flo[:6]))
    if a.drop:
        for items, *_x in amb + flo:
            for _k, o in items:
                t.sub(o['uuid'], '')
    if not assigned and not (a.drop and (amb or flo)):
        return 0
    if _need_out(a, 'renet'):
        return 1
    t.save(a.out)
    print(f"  -> {a.out}; check with kdrc.py {a.out} --unconnected")
    return 0


# ---------------------------------------------------------------- ripup

def c_ripup(b, a):
    """Delete tracks and vias so the router can redo them: those KiCad's DRC flagged
    (--drc REPORT.json from `kdrc.py --report`, violation --types), and/or every track
    and via of the named nets (inside --box). Then prune what that leaves dangling on
    the affected nets - a track end touching nothing of its net, a via joining fewer
    than two things, repeated until stable - and the teardrops on removed copper."""
    import json
    from kpcb_board import PolyIndex
    sel = set()
    if a.drc:
        types = set((a.types or 'shorting_items,clearance,hole_clearance,hole_to_hole,'
                     'copper_edge_clearance,track_dangling,via_dangling,items_not_allowed').split(','))
        for v in json.load(open(a.drc)).get('violations', []):
            if v.get('type') in types:
                sel.update(it.get('uuid') for it in v.get('items', []))
    box = tuple(float(x) for x in a.box.split(',')) if a.box else None
    def inbox(pts):
        return box is None or any(box[0] <= x <= box[2] and box[1] <= y <= box[3] for x, y in pts)
    names = set()
    if a.args:
        from kpcb_board import _resolve_net
        for nm in a.args:
            net, cand = _resolve_net(b, nm)
            if not net:
                print(f"{nm}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); return 1
            names.add(net)
    elif not a.drc:
        print("ripup NET... [--box X0,Y0,X1,Y1] | --drc REPORT.json [--types T,T] [-o OUT]"); return 1
    kill = {id(t) for t in b.tracks if (t['uuid'] in sel or t['net'] in names) and inbox([t['a'], t['b']])}
    kill |= {id(v) for v in b.vias if (v['uuid'] in sel or v['net'] in names) and inbox([(v['x'], v['y'])])}
    first = len(kill)
    nets = {o['net'] for o in b.tracks + b.vias if id(o) in kill}
    # prune: per affected net, drop dangling copper until nothing changes
    fills = defaultdict(list)
    for fl in b.fills:
        if fl['net'] in nets:
            fills[(fl['net'], fl['layer'])].append(PolyIndex(fl['pts']))
    pads = defaultdict(list)
    for f in b.fps.values():
        for p in f.pads:
            if p['net'] in nets:
                pads[p['net']].append(('p', p))
    cu = b.copper
    def cell_keys(bb):
        for cx in range(math.floor(bb[0]), math.floor(bb[2]) + 1):
            for cy in range(math.floor(bb[1]), math.floor(bb[3]) + 1):
                yield cx, cy
    def box_of(x):
        k, o = x
        if k == 't':
            hw = o['w'] / 2 + .05
            return (min(o['a'][0], o['b'][0]) - hw, min(o['a'][1], o['b'][1]) - hw,
                    max(o['a'][0], o['b'][0]) + hw, max(o['a'][1], o['b'][1]) + hw)
        if k == 'v':
            r = o['size'] / 2 + .05
            return (o['x'] - r, o['y'] - r, o['x'] + r, o['y'] + r)
        return pad_box(o)
    def on(x):
        k, o = x
        return {o['layer']} if k == 't' else via_layers(o, cu) if k == 'v' else \
            (set(cu) if o['drill'] else cu_layers(o['layers'], cu))
    while True:
        live = [('t', t) for t in b.tracks if t['net'] in nets and id(t) not in kill and t['w'] > 0] + \
               [('v', v) for v in b.vias if v['net'] in nets and id(v) not in kill]
        G = defaultdict(list)                        # (net, cx, cy) -> items, 1 mm cells
        for x in live + [pp for n in nets for pp in pads[n]]:
            for c in cell_keys(box_of(x)):
                G[(x[1]['net'], *c)].append(x)
        def near(net, bb):
            seen = set()
            for c in cell_keys(bb):
                for x in G.get((net, *c), ()):
                    if id(x[1]) not in seen:
                        seen.add(id(x[1]))
                        yield x
        dead = set()
        for it in live:
            k, o = it
            net = o['net']
            if k == 't':
                for e in (o['a'], o['b']):
                    if any(e in idx for idx in fills.get((net, o['layer']), ())):
                        continue
                    stub = ('t', {'layer': o['layer'], 'w': o['w'], 'a': e, 'b': e})
                    hw = o['w'] / 2
                    other = o['b'] if e is o['a'] else o['a']
                    if not any(x[1] is not o and o['layer'] in on(x) and
                               (_joins(o, e, other, x[1]) if x[0] == 't' else _touch(stub, x))
                               for x in near(net, (e[0] - hw, e[1] - hw, e[0] + hw, e[1] + hw))):
                        dead.add(id(o)); break
            else:                                    # KiCad: a via must join two or more LAYERS
                lys = via_layers(o, cu)
                got = set()
                for x in near(net, box_of(it)):
                    if x[1] is not o and (on(x) & lys) and _touch(('v', o), x):
                        got |= on(x) & lys
                got |= {ly for ly in lys for idx in fills.get((net, ly), ())
                        if any(q in idx for q in _around(o['x'], o['y'], o['size'] / 2))}
                if len(got) < 2:
                    dead.add(id(o))
        if not dead:
            break
        kill |= dead
    gone = [o for o in b.tracks + b.vias if id(o) in kill]
    t = PcbText(b.path)
    for o in gone:
        if o['uuid'] in t.span:
            t.sub(o['uuid'], '')
    tear = 0
    trk = [o for o in gone if 'layer' in o and 'a' in o]
    for s0, e0, net, ly, pts in t.tears:
        idx = None
        for o in trk:
            if o['net'] != net or o['layer'] != ly:
                continue
            idx = idx or PolyIndex(pts)
            L = max(1, int(o['len'] / 0.1))
            if any((o['a'][0] + (o['b'][0] - o['a'][0]) * i / L, o['a'][1] + (o['b'][1] - o['a'][1]) * i / L) in idx
                   for i in range(L + 1)):
                t.edits.append((s0, e0 + (1 if t.txt[e0:e0 + 1] == '\n' else 0), ''))
                tear += 1
                break
    per = defaultdict(lambda: [0, 0])
    for o in gone:
        per[o['net']][0 if 'layer' in o and 'a' in o else 1] += 1
    print(f"ripup: {first} item(s) selected, {len(gone) - first} more pruned as dangling, "
          f"{tear} teardrop(s) with them: {sum(x[0] for x in per.values())} track(s) + "
          f"{sum(x[1] for x in per.values())} via(s) on {len(per)} net(s)")
    for net, (k, v) in sorted(per.items(), key=lambda kv: -sum(kv[1]))[:a.max]:
        print(f"  {unesc_disp(net) or '(no net)':<34} {k:>3} trk {v:>2} via")
    if len(per) > a.max:
        print(f"  ... +{len(per) - a.max} more net(s)")
    if not gone:
        return 0
    if _need_out(a, 'ripup'):
        return 1
    t.save(a.out)
    print(f"  -> {a.out}; then airwires / route the nets, and kdrc.py --refill-save --baseline")
    return 0


def _joins(t, e, other, u):
    """does track t's end e really join track u: at a shared endpoint that is not also
    t's other end (two overlapping slivers are a dead end, not a junction), or as a T
    into u's interior"""
    tol = min(t['w'], u['w']) / 2
    for f in (u['a'], u['b']):
        if math.dist(e, f) <= tol:
            return math.dist(other, f) > tol or math.dist(other, u['b'] if f is u['a'] else u['a']) > tol and \
                math.dist(e, other) > tol
    if pt_seg_dist(e, u['a'], u['b']) > u['w'] / 2:
        return False
    L = math.dist(u['a'], u['b'])
    return L > 0 and min(math.dist(e, u['a']), math.dist(e, u['b'])) > u['w'] / 2


# ---------------------------------------------------------------- move

def _flipped(f):
    """a shallow stand-in for f after an in-place flip: what place_text needs (rotation)"""
    class G:
        pass
    g = G()
    g.ref, g.x, g.y, g.rot, g.uuid = f.ref, f.x, f.y, norm_ang(180 - f.rot), f.uuid
    return g


def place_text(s, f, x, y, rot):
    """a footprint block re-placed at x,y,rot. Pad and text (at) angles are absolute in
    a .kicad_pcb, so a rotation turns each of them by the same delta."""
    if '\n\t\t(transform\n' in s:
        s, k = re.subn(r'(\n\t\t\(transform\n\t\t\t\(translate )\S+ \S+\)',
                       lambda m: f"{m[1]}{num(x)} {num(y)})", s, count=1)
        if rot is not None:
            s, k2 = re.subn(r'(\n\t\t\(transform\n\t\t\t\(translate [^)]*\)\n\t\t\t\(rotate )\S+\)',
                            lambda m: f"{m[1]}{num(norm_ang(rot))})", s, count=1)
            k = k and k2
    else:
        s, k = re.subn(r'\n\t\t\(at \S+ \S+(?: \S+)?\)',
                       f"\n\t\t(at {num(x)} {num(y)}" + (f" {num(norm_ang(rot if rot is not None else f.rot))})"
                                                       if (rot if rot is not None else f.rot) else ')'), s, count=1)
    if not k:
        raise SystemExit(f"{f.ref}: no placement line found; nothing written")
    dr = norm_ang(rot - f.rot) if rot is not None else 0.0
    if dr:
        def turn(m):                                     # pads omit a 0 angle, fields never do
            ang = norm_ang((float(m[5]) if m[5] else 0.0) + dr) % 360
            return f"{m[1]}{m[3]} {m[4]}" + (f" {num(ang)})" if ang or m[2] != 'pad' else ')')
        s = re.sub(r'(\n\t\t\((pad|property|fp_text) [^\n]*\n\t\t\t\(at )(\S+) (\S+?)(?: (\S+))?\)', turn, s)
    return s


NUM = r'-?\d+(?:\.\d+)?(?:e-?\d+)?'
SIDED = re.compile(r'"([FB])\.(Cu|SilkS|Fab|CrtYd|Mask|Paste|Adhes|Silkscreen|Courtyard)"')


def flip_text(s, f):
    """a footprint block flipped to the other side in place, as KiCad's left-right flip
    does it: every child's local y negated, rotation 180 - r, each pad/text absolute
    angle 180 - a, F/B layers swapped, text mirroring toggled (custom-pad primitives
    are pad-local, so negating y mirrors them too). On the board that is a mirror in x
    about the footprint position."""
    s = SIDED.sub(lambda m: f'"{"B" if m[1] == "F" else "F"}.{m[2]}"', s)
    s = re.sub(rf'\((start|end|mid|center|xy) ({NUM}) ({NUM})\)',
               lambda m: f"({m[1]} {m[2]} {num(-float(m[3]))})", s)
    def at(m):                                           # pads omit a 0 angle, fields never do
        ang = norm_ang(180 - float(m[5] or 0)) % 360        # items 0..360, footprints -180..180
        return f"{m[1]}{m[3]} {num(-float(m[4]))}" + (f" {num(ang)})" if ang or m[2] != 'pad' else ')')
    s = re.sub(rf'(\n\t\t\((\w+)[^\n]*\n\t\t\t\(at )({NUM}) ({NUM})(?: ({NUM}))?\)', at, s)
    if '\n\t\t(transform\n' in s:
        s = re.sub(r'(\n\t\t\t\(rotate )(\S+)\)', lambda m: f"{m[1]}{num(norm_ang(180 - float(m[2])))})", s, count=1)
    else:                                                # 10.0: (at X Y R) on the footprint itself
        s = re.sub(rf'(\n\t\t\(at )({NUM}) ({NUM})(?: ({NUM}))?\)',
                   lambda m: f"{m[1]}{m[2]} {m[3]} {num(norm_ang(180 - float(m[4] or 0)))})", s, count=1)
    def mirror(m):
        ind, e = m[1], m[0]
        j = re.search(r'\(justify([^)]*)\)', e)
        if j:
            w = j[1].split()
            w = [x for x in w if x != 'mirror'] if 'mirror' in w else w + ['mirror']
            if w:
                return e[:j.start()] + f"(justify {' '.join(w)})" + e[j.end():]
            ls, le = e.rindex('\n', 0, j.start()), e.index('\n', j.end())
            return e[:ls] + e[le:]
        return e[:-len(ind) - 1] + f"{ind}\t(justify mirror)\n{ind})"
    return re.sub(r'(?<=\n)(\t+)\(effects\n(?:\1\t.*\n)*\1\)', mirror, s)


def c_move(b, a):
    try:
        ref, x, y = a.args[0], float(a.args[1]), float(a.args[2])
        rot = float(a.args[3]) if len(a.args) > 3 else None
    except (IndexError, ValueError):
        print("move REF X Y [ROT] [--flip] -o OUT   (board mm, page origin)"); return 1
    f = b.fps.get(ref)
    if not f:
        print(f"{ref}: NOT FOUND"); return 1
    if _need_out(a, 'move'):
        return 1
    t = PcbText(b.path)
    s = t.text(f.uuid)
    if a.flip:
        s = flip_text(s, f)
    t.sub(f.uuid, place_text(s, _flipped(f) if a.flip else f, x, y, rot))
    t.save(a.out)
    tied = sum(1 for tr in b.tracks if tr['net'] and any(
        tr['net'] == p['net'] and math.dist(e, (p['x'], p['y'])) <= max(p['sx'], p['sy'])
        for p in f.pads for e in (tr['a'], tr['b'])))
    print(f"move {ref} {f.x:.3f},{f.y:.3f} rot {f.rot:g} -> {x:.3f},{y:.3f}" + (' FLIPPED' if a.flip else '')
          + (f" rot {norm_ang(rot):g}" if rot is not None else '') + f"   -> {a.out}"
          + (f"\n  {tied} track end(s) sat on its pads and stay behind: re-route them" if tied else ''))
    return 0
