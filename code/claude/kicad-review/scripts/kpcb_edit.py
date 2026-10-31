"""kpcb.py board writers. Each writes a NEW file (-o OUT, never the input) by text
surgery on KiCad's own formatting - only the edited spans change, every match count
is asserted - and leaves the verdict to KiCad: `kdrc.py OUT --unconnected` on the copy
(it refills the zones first).

  fixlines -o OUT              undo the 10.99 fp_line frame bug (Board._fix_lines)
  renet -o OUT                 netless tracks/vias whose ends touch one net take it
  move REF X Y [ROT] -o OUT    re-place a footprint (pads/text turn with it, segment ends
                               on its pads are dragged along; --flip for the other side)
  edit OP... -o OUT            scripted hand edits: width, del, seg, via (free), mvvia, vcenter,
                               solid, simplify, unround, and the silk ops (ref, value, text, sizes)
  unhook REF... -o OUT         rip the copper that only served these parts

`route` and `place` (kpcb_route.py, kpcb_place.py) write through PcbText too."""
import math, os, re, uuid
from collections import defaultdict
from kcommon import unesc_disp
from kpcb_board import PolyIndex, _pidx, xf, pad_box, pt_seg_dist, seg_seg_dist
from kpcb_copper import UF, _local, _reach, _around, pad_dist
from kpcb_move import cu_layers, via_layers, drag_plan, drag_set


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
    tab), so footprints, tracks, vias and zones are found by uuid without re-serialising
    anything that was not edited. New tracks/vias go in before the first zone."""

    def __init__(self, path):
        self.path = path
        with open(path, encoding='utf-8') as fh:
            self.txt = fh.read()
        self.span, self.edits = {}, []
        for m in re.finditer(r'\n\t\((footprint|segment|arc|via|gr_text|zone)\b', self.txt):
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
        """replace (or with '' delete) an item; returns the edit, so it can be withdrawn"""
        s, e = self.span[uid]
        if not new and self.txt[e:e + 1] == '\n':            # a deletion takes its line break too
            e += 1
        self.edits.append((s, e, new))
        return self.edits[-1]

    def add(self, item):
        """queue a new item; returns the edit, so `edits.remove` can withdraw it (a rip-up)"""
        self.edits.append((self.tail, self.tail, item))
        return self.edits[-1]

    def segment(self, net, layer, w, a, b):
        return self.add(f"\t(segment\n\t\t(start {num(a[0])} {num(a[1])})\n\t\t(end {num(b[0])} {num(b[1])})\n"
                 f"\t\t(width {num(w)})\n\t\t(layer {qs(layer)})\n\t\t(net {qs(net)})\n"
                 f"\t\t(uuid \"{uuid.uuid4()}\")\n\t)\n")

    def via(self, net, x, y, size, drill, layers, free=False):
        """free: a stitching via with no track. KiCad re-nets a trackless via from the copper it
        touches on load/save unless it is marked free (a GND via came back -BATT, 2026-10-19)"""
        return self.add(f"\t(via\n\t\t(at {num(x)} {num(y)})\n\t\t(size {num(size)})\n\t\t(drill {num(drill)})\n"
                 f"\t\t(layers {' '.join(qs(l) for l in layers)})\n" + ("\t\t(free yes)\n" if free else '')
                 + f"\t\t(net {qs(net)})\n\t\t(uuid \"{uuid.uuid4()}\")\n\t)\n")

    def zone(self, net, layer, pts, priority, clearance=0.2, min_w=0.25, name=None):
        """an unfilled solid-connect zone (the refill draws its copper): sharp corners (faceted
        arcs unrounded, kpcb_fillet) + KiCad's 0.2 fillet, as Ryan draws his own power zones"""
        from kpcb_fillet import fillet_outline, FILLET
        pts, r, _note = fillet_outline(pts)
        r = r or FILLET
        sm = f"\t\t\t(smoothing fillet)\n\t\t\t(radius {num(r)})\n" if r else ''
        xy = ''.join(f"\t\t\t\t(xy {num(x)} {num(y)})\n" for x, y in pts)
        return self.add(f"\t(zone\n\t\t(net {qs(net)})\n\t\t(layer {qs(layer)})\n\t\t(uuid \"{uuid.uuid4()}\")\n"
                        f"\t\t(name {qs(name or 'route ' + net)})\n\t\t(hatch edge 0.5)\n\t\t(priority {priority})\n"
                        f"\t\t(connect_pads yes\n\t\t\t(clearance {num(clearance)})\n\t\t)\n\t\t(min_thickness {num(min_w)})\n"
                        f"\t\t(fill yes\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n{sm}"
                        f"\t\t\t(island_removal_mode 0)\n\t\t)\n\t\t(polygon\n\t\t\t(pts\n{xy}\t\t\t)\n\t\t)\n\t)\n")

    def save(self, out, inplace=False):
        """inplace: OUT is this run's own output being amended (the post-write gate), never the input"""
        if not inplace and os.path.abspath(out) == os.path.abspath(self.path):
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
            if (pad_dist(p, e) <= hw) if p['prims'] else (abs(u) <= hx + hw and abs(v) <= hy + hw):
                return True                                  # a custom pad: its real polygons
        return pt_seg_dist((p['x'], p['y']), x['a'], x['b']) <= min(hx, hy) + hw
    if p['prims']:                                           # a via in a pad
        return pad_dist(p, (x['x'], x['y'])) <= 0
    u, v = _local(p, (x['x'], x['y']))
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
    and via of the named nets (inside --box). Then prune what that leaves dangling - a
    track end touching nothing of its net, a via joining fewer than two layers - spreading
    only from removed copper, repeated until stable, and the teardrops on removed copper."""
    import json
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
    elif not a.drc and not a.inpour and not getattr(a, 'deadend', False):
        print("ripup NET... [--box X0,Y0,X1,Y1] | --drc REPORT.json [--types T,T] | [NET] --inpour | [NET] --deadend  "
              "[-o OUT]"); return 1
    if a.inpour:
        return _inpour(b, a, names, box)
    if getattr(a, 'deadend', False):
        return _deadend(b, a, names, box)
    kill = {id(t) for t in b.tracks if (t['uuid'] in sel or t['net'] in names) and inbox([t['a'], t['b']])}
    kill |= {id(v) for v in b.vias if (v['uuid'] in sel or v['net'] in names) and inbox([(v['x'], v['y'])])}
    return _rip_out(b, a, kill, 'ripup')


def _inpour(b, a, names, box):
    """`ripup [NET] --inpour`: delete tracks lying wholly inside their own net's saved fill
    (`tracks --kind inpour,inpourpwr`): every plane net's, or a NAMED net's of any kind
    (opt-in: whether a power path may rely on its fill alone is the designer's call), each with
    what its removal leaves dangling, except one whose removal takes a track off a thermal-relief
    pad: KiCad counts that track as a spoke, and without it the pad can be starved
    (STARVED_THERMAL: C18.1, 2026-10-27). Then, once, the stubs the rip leaves hanging in the
    pour (a plane net's too), a thermal pad's included: a stub is no spoke Ryan keeps, and
    TODO.md accepts the 1-spoke 0402 it can leave. Also kept: copper joining two pads (a decoupling
    return, an IC GND pin to its EP) and a leg whose removal leaves a remaining end loose (on a
    pad edge, in the pour) - every bdbbaac candidate was one Ryan kept, 2026-10-28. Refill, then DRC."""
    kill, cand, kept = inpour_kill(b, names, box)
    from kpcb_route import _planes
    pl = {n for n, _l in _planes(b)}
    print(f"inpour: {len(cand)} {'plane-net ' if names <= pl else ''}track(s) wholly inside their own saved fill, "
          f"{len(kept)} run(s) kept (a thermal pad's spoke, a pad-to-pad tie, or a leg whose removal leaves the rest "
          f"ending loose)")
    for t, lab in kept[:a.max]:
        print(f"  kept {unesc_disp(t['net'])} {t['layer']} w{t['w']:g} {t['a'][0]:.3f},{t['a'][1]:.3f} -> "
              f"{t['b'][0]:.3f},{t['b'][1]:.3f}  ({lab})")
    if not a.out:
        n = sum(1 for t in b.tracks if id(t) in kill)
        print(f"  (dry run: {n} track(s) with what they leave dangling; -o OUT deletes them, then "
              f"kdrc.py OUT drc --refill-save --baseline BEFORE.json)")
        return 0
    return _rip_out(b, a, kill, 'ripup --inpour')


def inpour_kill(b, names, box, only=None):
    """(kill ids, candidate track idx, kept [(track, why)]) for `ripup --inpour` on b's saved fill,
    candidates joined end to end taken as one run (kept: its first track);
    only: the track uuids that may go (fatten --drop-track: the tracks it banded, one at a time
    and none of the 2026-10-28 guards - the band is their replacement)"""
    from kpcb_tracks import in_pour
    from kpcb_board import solid, PolyIndex
    from kpcb_smooth import seg_pad_dist, on_layer
    cand = [i for i in in_pour(b, names or None, box, every=bool(names)) if only is None or b.tracks[i]['uuid'] in only]
    zones = [(z, PolyIndex(z.outline)) for z in b.zones if z[0] and not z.keepout and z.outline]
    def thermal(p, ly):
        zs = [z for z, idx in zones if z[0] == p['net'] and (ly in z[1] or '*.Cu' in z[1]) and (p['x'], p['y']) in idx]
        return bool(zs) and not solid(p, max(zs, key=lambda z: z.priority))
    pads = defaultdict(list)
    for f in b.placed():
        for p in f.pads:
            pads[p['net']].append((f, p))
    from kpcb_copper import pad_dist
    from kpcb_board import pt_seg_dist

    def ends_on(u, E, gone):
        """what u's end E lands on: live tracks, vias at their centre, and pads it sits in by w/2
        (or at their centre) - a pad's edge or a via's rim is not one (U12.11's feed grazes one)"""
        return [('t', id(o)) for o in b.tracks if id(o) not in gone and o is not u and o['net'] == u['net'] and
                o['layer'] == u['layer'] and pt_seg_dist(E, o['a'], o['b']) <= (o['w'] + u['w']) / 2] + \
            [('v', id(v)) for v in b.vias if v['net'] == u['net'] and id(v) not in gone and u['layer'] in via_layers(v, b.copper)
             and math.dist(E, (v['x'], v['y'])) < 0.05] + \
            [('p', id(p)) for _f, p in pads[u['net']] if on_layer(p, u['layer']) and
             (pad_dist(p, E) <= -u['w'] / 2 or math.dist(E, (p['x'], p['y'])) < 0.05)]

    def loose(u, E, gone):
        """E lands on nothing, in a via off its centre (KiCad's TRACK_NOT_CENTERED_ON_VIA once the
        sliver that was centred there goes: GND 105.82,82.58), or the run it is on now reaches
        < 2 pads/vias (a dead end in the pour)"""
        if not ends_on(u, E, gone) or any(
                v['net'] == u['net'] and id(v) not in gone and 1e-3 < math.dist(E, (v['x'], v['y'])) <= v['size'] / 2
                and u['layer'] in via_layers(v, b.copper) for v in b.vias):
            return True
        tid = {id(o): o for o in b.tracks}
        seen, todo, anchors = {id(u)}, [u], set()
        while todo and len(anchors) < 2:
            o = todo.pop()
            for x in (o['a'], o['b']):
                for k, j in ends_on(o, x, gone):
                    if k != 't':
                        anchors.add(j)
                    elif j not in seen:
                        seen.add(j)
                        todo.append(tid[j])
        return len(anchors) < 2
    runs = UF()                                      # candidates joined end to end go as one run (a
    for i in cand:                                   # via-to-via track pair in the pour: one at a time,
        runs.find(i)                                 # each would leave the other loose)
        for j in cand:
            ti, tj = b.tracks[i], b.tracks[j]
            if only is None and i < j and ti['net'] == tj['net'] and ti['layer'] == tj['layer'] and \
                    any(pt_seg_dist(e, tj['a'], tj['b']) <= (ti['w'] + tj['w']) / 2 for e in (ti['a'], ti['b'])):
                runs.union(i, j)
    grp = defaultdict(list)
    for i in cand:
        grp[runs.find(i)].append(b.tracks[i])
    kill, kept = set(), []
    for run in grp.values():
        t = run[0]
        if id(t) in kill:
            continue
        k = prune(b, kill | {id(u) for u in run}, planes=False) - kill
        gone = [u for u in b.tracks if id(u) in k]
        hit = [f"{f.ref}.{p['num']}" for u in gone for f, p in pads[u['net']]
               if on_layer(p, u['layer']) and seg_pad_dist(u['a'], u['b'], p) <= u['w'] / 2 and thermal(p, u['layer'])]
        if hit:
            kept.append((t, f"feeds {hit[0]}"))
            continue
        if only is None:                             # Ryan kept every bdbbaac candidate (2026-10-28):
            ties = {(f.ref, p['num']) for u in gone for f, p in pads[u['net']]   # a pad-to-pad tie (C183.2 ->
                    if on_layer(p, u['layer']) and seg_pad_dist(u['a'], u['b'], p) <= u['w'] / 2}   # U22, U17.9 -> EP)
            hang = next((f"{E[0]:.2f},{E[1]:.2f}" for u in b.tracks if id(u) not in kill | k and u['net'] == t['net']
                         and u['layer'] == t['layer'] for E in (u['a'], u['b'])
                         if any(pt_seg_dist(E, g['a'], g['b']) <= g['w'] / 2 + 1e-3 for g in gone)
                         and loose(u, E, kill | k)), None)   # or a leg whose rest then ends on a pad edge / in the pour
            if len(ties) >= 2 or hang:
                kept.append((t, f"ties {len(ties)} pads" if len(ties) >= 2 else f"the rest would end loose @{hang}"))
                continue
        kill |= k
    kill = prune(b, kill) if kill else kill          # the stubs it all leaves, a kept spoke's included
    return kill, cand, [(t, lab) for t, lab in kept if id(t) not in kill]


def _deadend(b, a, names, box):
    """`ripup [NET] --deadend`: delete copper that serves no pad (kpcb_copper.dead_copper): its
    wholly dead tracks and vias, a track dead only past its last tee cut back to it, and a zone
    whose every fill fragment is dead (a band of a live zone is reported, never touched). RF
    netclass branches (matching stubs?) only when named. Then prune, as every ripup."""
    from kpcb_copper import dead_all, dead_desc, drop_layer
    inb = lambda p: not box or box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]   # noqa: E731
    ds = [(net, d) for net, lst in sorted(dead_all(b, sorted(names) if names else None).items()) for d in lst
          if (names or 'RF' not in (b.netclass(net) or '').upper()) and (not d['at'] or inb(d['at']))]
    dead_fl = {id(fl) for _n, d in ds for fl in d['fills']}
    zones = {fl['zone'] for fl in b.fills if id(fl) in dead_fl}
    zones = {z for z in zones if all(id(fl) in dead_fl for fl in b.fills if fl.get('zone') == z)}
    from kpcb_smooth import Exact
    kill, cuts, mid, stubs = set(), [], 0, []
    for _n, d in ds:
        kill |= {id(v) for v in d['vias']}
        blocked = set()                              # a cut whose stub onto an off-line via won't clear
        for t, P, c in d.get('stubs', ()):
            ok = Exact(b, t['net'], t['layer'], t['w'], (min(P[0], c[0]), min(P[1], c[1]),
                                                         max(P[0], c[0]), max(P[1], c[1]))).seg(P, c)
            (stubs.append((t, P, c)) if ok else blocked.add(id(t)))
        stubs = [x for x in stubs if id(x[0]) not in blocked]
        for t, _L, keep in d['tracks']:
            if keep is None:
                kill.add(id(t))
            elif keep and id(t) not in blocked:
                cuts.append((t, keep))
            else:
                mid += 1
    print(f"deadend: {len(ds)} branch(es) serving no pad, {sum(len(d['tracks']) for _n, d in ds)} track(s), "
          f"{sum(len(d['vias']) for _n, d in ds)} via(s), {len(zones)} whole zone(s)")
    for net, d in ds[:a.max]:
        print(f"  {unesc_disp(net):<28} {dead_desc(d)}")
    left = sorted({(fl['zone'], fl['layer']) for fl in b.fills if id(fl) in dead_fl and fl['zone'] not in zones})
    for z, ly in left:                               # its zone has live fragments too
        keep = drop_layer(b, z, ly)
        print(f"  pour left {z[:8]} {ly}: " + (f"drop {ly} from the zone (keep {', '.join(keep)}): its {ly} fill joins "
              "no pad, only vias joined on another layer; check `ampacity --from --to --fills`" if keep
              else "its zone has live fragments too - trim the zone outline by hand"))
    for t, P, c in stubs:
        print(f"  cut {t['uuid'][:8]} ends in a via off its line: to {P[0]:g},{P[1]:g} + a {t['w']:g} mm stub onto "
              f"the centre {c[0]:g},{c[1]:g} (clear)")
    if mid:
        print(f"  {mid} track(s) reported, not cut: dead only between two live pieces, or the stub onto a via "
              "off the track's line would not clear")
    if not a.out:
        print("  (dry run: -o OUT deletes them, then kdrc.py OUT drc --refill-save --baseline BEFORE.json)")
        return 0
    first = len(kill)
    orig = {id(tr): (tr['a'], tr['b']) for tr, _k in cuts}
    for tr, (a2, b2) in cuts:                        # the prune must see the cut ends, not the dead ones
        tr.update(a=a2, b=b2, len=math.dist(a2, b2), mid=((a2[0] + b2[0]) / 2, (a2[1] + b2[1]) / 2))
    kill = prune(b, kill)
    t = PcbText(b.path)
    gone = [o for o in b.tracks + b.vias if id(o) in kill]
    for o in gone:
        if o['uuid'] in t.span:
            t.sub(o['uuid'], '')
    for z in zones:
        if z in t.span:
            t.sub(z, '')
    for tr, (a2, b2) in cuts:
        if id(tr) in kill:
            continue
        txt = t.text(tr['uuid'])
        for tag, P in (('start', a2), ('end', b2)):
            txt, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(P[0])} {num(P[1])})", txt, count=1)
            assert c == 1
        t.sub(tr['uuid'], txt)
    for tr, P, c in stubs:
        if id(tr) not in kill:
            t.segment(tr['net'], tr['layer'], tr['w'], P, c)
    cut_off = [dict(tr, a=p, b=q, len=math.dist(p, q)) for tr, (a2, b2) in cuts if id(tr) not in kill
               for p, q in ((orig[id(tr)][0], a2), (b2, orig[id(tr)][1])) if math.dist(p, q) > 1e-6]   # the part that goes
    tear = len(tear_edits(t, [o for o in gone if 'a' in o] + cut_off))
    if _need_out(a, 'ripup --deadend'):
        return 1
    t.save(a.out)
    print(f"  -> {a.out}: {len(gone)} item(s) deleted ({len(gone) - first} more pruned as dangling), {len(cuts)} cut "
          f"back" + (f" ({len(stubs)} with a stub onto its via)" if stubs else '') + f", {len(zones)} zone(s), {tear} "
          "teardrop(s); then kdrc.py --refill-save --baseline")
    return 0


def c_unhook(b, a):
    """`unhook REF... -o OUT`: rip only the copper that served these parts - the track ends
    on their pads (on a pad's layers) that sit on nothing else of the net (no via, no other
    part's pad), then the prune with the parts treated as absent: a stub that only fed them
    goes, a via in their pads goes only if it then joins fewer than two layers, and a shared
    trunk stays. Unlike `ripup --box` it never takes a whole net."""
    refs = [r for r in a.args if r in b.fps]
    if not refs or len(refs) != len(a.args):
        print(f"unhook REF... -o OUT   (not on the board: {' '.join(r for r in a.args if r not in b.fps) or '-'})")
        return 1
    cu, absent = b.copper, set(refs)
    mine = [p for r in refs for p in b.fps[r].pads if p['net']]
    def on(p, t, st):
        return p['net'] == t['net'] and (p['drill'] or t['layer'] in cu_layers(p['layers'], cu)) and _touch(st, ('p', p))
    kill = set()
    for t in b.tracks:
        for e in (t['a'], t['b']) if t['net'] else ():
            st = ('t', {'layer': t['layer'], 'w': t['w'], 'a': e, 'b': e})
            if any(on(p, t, st) for p in mine) and \
                    not any(v['net'] == t['net'] and t['layer'] in via_layers(v, cu) and _touch(st, ('v', v)) for v in b.vias) and \
                    not any(on(p, t, st) for f in b.fps.values() if f.ref not in absent for p in f.pads):
                kill.add(id(t))
                break
    return _rip_out(b, a, kill, 'unhook', absent=absent)


def _rip_out(b, a, kill, cmd, absent=frozenset()):
    """delete `kill` (ids of tracks/vias), prune what that leaves dangling (pads of `absent`
    parts anchor nothing), drop their teardrops, report, write -o"""
    first = len(kill)
    kill = prune(b, kill, absent)
    gone = [o for o in b.tracks + b.vias if id(o) in kill]
    t = PcbText(b.path)
    for o in gone:
        if o['uuid'] in t.span:
            t.sub(o['uuid'], '')
    tear = len(tear_edits(t, [o for o in gone if 'layer' in o and 'a' in o]))
    per = defaultdict(lambda: [0, 0])
    for o in gone:
        per[o['net']][0 if 'layer' in o and 'a' in o else 1] += 1
    print(f"{cmd}: {first} item(s) selected, {len(gone) - first} more pruned as dangling, "
          f"{tear} teardrop(s) with them: {sum(x[0] for x in per.values())} track(s) + "
          f"{sum(x[1] for x in per.values())} via(s) on {len(per)} net(s)")
    for net, (k, v) in sorted(per.items(), key=lambda kv: -sum(kv[1]))[:a.max]:
        print(f"  {unesc_disp(net) or '(no net)':<34} {k:>3} trk {v:>2} via")
    if len(per) > a.max:
        print(f"  ... +{len(per) - a.max} more net(s)")
    if not gone:
        return 0
    for net, cut, kept in cut_off(b, kill, absent) if absent else ():
        print(f"  CUT  {' '.join(cut)} now off {unesc_disp(net)}'s other {len(kept)} pad(s): its only link ran "
              f"through {' '.join(sorted(absent))} - re-route it")
    if _need_out(a, cmd):
        return 1
    t.save(a.out)
    print(f"  -> {a.out}; then airwires / route the nets, and kdrc.py --refill-save --baseline")
    return 0


def cut_off(b, kill, absent):
    """pads of parts not in `absent` that shared a copper piece before the rip and are cut
    from it after (the deleted `kill` gone, the `absent` parts' pads anchoring nothing):
    [(net, cut pads, pads they were joined to)]"""
    from types import SimpleNamespace
    from kpcb_copper import Copper
    after = SimpleNamespace(fps={r: f for r, f in b.fps.items() if r not in absent},
                            tracks=[x for x in b.tracks if id(x) not in kill],
                            vias=[x for x in b.vias if id(x) not in kill], fills=b.fills, copper=b.copper)
    out = []
    for net in sorted({o['net'] for o in b.tracks + b.vias if id(o) in kill and o['net']}):
        def groups(bd):
            C = Copper(bd, net)
            g = defaultdict(set)
            for i, (f, p) in enumerate(C.pads):
                if f.ref not in absent:
                    g[C.piece.find(('pad', i))].add(f"{f.ref}.{p['num']}")
            return list(g.values())
        post = groups(after)
        for g in groups(b):
            parts = sorted((g & h for h in post if g & h), key=lambda h: (-len(h), sorted(h)))
            for cut in parts[1:]:
                out.append((net, sorted(cut), sorted(parts[0])))
    return out


def prune(b, kill, absent=frozenset(), planes=True):
    """kill (ids of b.tracks/b.vias to delete) grown by what that leaves dangling: a track
    end touching nothing of its net (no pad, via, track junction or fill), a via joining
    fewer than two layers - spreading only from removed copper (and the pads of `absent`
    parts, which anchor nothing), until stable - then by what it leaves serving no pad
    (_newly_dead: a cycle hanging off one point, a plane stub ending in its fill; planes=False
    leaves the plane stubs to a later call). Returns the grown set."""
    from kpcb_board import PolyIndex
    kill = set(kill)
    nets = {o['net'] for o in b.tracks + b.vias if id(o) in kill} | \
        {p['net'] for f in b.fps.values() if f.ref in absent for p in f.pads if p['net']}
    # prune: per affected net, drop dangling copper until nothing changes
    fills = defaultdict(list)
    for fl in b.fills:
        if fl['net'] in nets:
            fills[(fl['net'], fl['layer'])].append(PolyIndex(fl['pts']))
    pads = defaultdict(list)
    for f in b.fps.values():
        for p in f.pads if f.ref not in absent else ():
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
    # only copper touching what went can newly dangle: an old stub elsewhere on GND is not ours
    frontier = [('t', t) for t in b.tracks if id(t) in kill] + [('v', v) for v in b.vias if id(v) in kill] + \
        [('p', p) for f in b.fps.values() if f.ref in absent for p in f.pads if p['net'] in nets]
    while frontier:
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
        cand = {id(x[1]): x for f0 in frontier for x in near(f0[1]['net'], box_of(f0))     # touching it, not
                if x[0] != 'p' and (on(x) & on(f0)) and _touch(x, f0)}                    # merely its box
        dead = []
        for it in cand.values():
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
                        dead.append(it); break
            else:                                    # KiCad: a via must join two or more LAYERS
                lys = via_layers(o, cu)
                got = set()
                for x in near(net, box_of(it)):
                    if x[1] is not o and (on(x) & lys) and _touch(('v', o), x):
                        got |= on(x) & lys
                got |= {ly for ly in lys for idx in fills.get((net, ly), ())
                        if any(q in idx for q in _around(o['x'], o['y'], o['size'] / 2))}
                if len(got) < 2:
                    dead.append(it)
        kill |= {id(x[1]) for x in dead}
        frontier = dead
    return kill | _newly_dead(b, kill, absent, nets, planes)


def _newly_dead(b, kill, absent, nets, planes=True):
    """what the leaf prune cannot see: copper the rip leaves serving no pad though every end
    still touches something - a two-layer run through two vias, a cycle (kpcb_copper.dead_copper),
    a plane-net stub whose free end lies in its own fill (the leaf prune counts fill as a join:
    `ripup GND --inpour` left both ends of a half-ripped chain, 2026-10-27) - and not dead before
    the rip (an old dead branch elsewhere on the net is not ours). A plane net gives only its
    track stubs (kpcb_copper.plane_stubs: its islands are the pour's); planes=False skips them
    (inpour_kill prices each candidate without the ~2 s GND graph, then runs it once)"""
    from types import SimpleNamespace
    from kpcb_copper import dead_copper, plane_stubs
    from kpcb_route import _planes
    pl = {n for n, _l in _planes(b)}
    todo = sorted(n for n in nets if n and (planes or n not in pl))
    if not todo:
        return set()
    after = SimpleNamespace(fps={r: f for r, f in b.fps.items() if r not in absent}, fills=b.fills, copper=b.copper,
                            tracks=[x for x in b.tracks if id(x) not in kill], vias=[x for x in b.vias if id(x) not in kill])
    def ids(bd, n):
        ds = dead_copper(bd, n)
        return {id(o) for d in (plane_stubs(ds) if n in pl else ds) for o in [t for t, _L, k in d['tracks'] if k is None] + d['vias']}
    return {i for n in todo for i in ids(after, n) - ids(b, n)}


def tear_edits(t, trk):
    """delete the teardrop zones that fillet any of the tracks `trk` (removed copper must
    not leave its fillets behind); returns the edits, so a rip-up can withdraw them"""
    out = []
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
                out.append(t.edits[-1])
                break
    return out


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


def drag_tracks(b, t, f, x, y, rot, flip=False):
    """KiCad's Drag (D) for a part re-placed at x,y,rot (kpcb_move.drag_plan): each segment
    end on one of its pads, on that pad's layers, follows the pad; a via inside a pad moves
    with it, and every end on that via with it; an end on any other via stays. A flip moves
    the pads to the other layer and an arc cannot bend, so those ends stay (counted).
    Returns (ends dragged, ends left, vias moved)."""
    if flip:
        ends, _vias, left = drag_set(b, f)
        return 0, left + len(ends), 0
    segs, vias, left = drag_plan(b, f, x, y, rot)
    dragged = 0
    for tr, a, c in segs.values():
        u, seg = tr['uuid'], t.text(tr['uuid'])
        for k, e, old in (('start', a, tr['a']), ('end', c, tr['b'])):
            if e != old:
                seg, n = re.subn(rf'\({k} \S+ \S+\)', f"({k} {num(e[0])} {num(e[1])})", seg, count=1)
                assert n == 1, (u, k)
                dragged += 1
        t.sub(u, seg)
    for v, vx, vy in vias:
        s, n = re.subn(r'\(at \S+ \S+\)', f"(at {num(vx)} {num(vy)})", t.text(v['uuid']), count=1)
        assert n == 1, v['uuid']
        t.sub(v['uuid'], s)
    return dragged, left, len(vias)


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
    tear = 0
    if a.no_drag:                                    # KiCad's Move (M): its tracks and vias stay put
        from kpcb_move import pad_ends
        ends, _vias, stay = pad_ends(b, f)
        dragged, left, vin = 0, len(ends) + stay, 0
        tip = lambda tr, k: (tr[k], tr['b' if k == 'a' else 'a'])            # noqa: E731
        near = [dict(tr, a=e, b=(e[0] + (o[0] - e[0]) * s_, e[1] + (o[1] - e[1]) * s_), len=min(tr['len'], .5))
                for tr, k in ends for e, o in [tip(tr, k)] for s_ in [min(1.0, .5 / (tr['len'] or 1))]]
        tear = len(tear_edits(t, near))              # the teardrops of ends left on the moved pads
    else:
        dragged, left, vin = drag_tracks(b, t, f, x, y, rot, a.flip)
    t.save(a.out)
    print(f"move {ref} {f.x:.3f},{f.y:.3f} rot {f.rot:g} -> {x:.3f},{y:.3f}" + (' FLIPPED' if a.flip else '')
          + (f" rot {norm_ang(rot):g}" if rot is not None else '') + f"   -> {a.out}"
          + (f"\n  dragged {dragged} track end(s) that sat on its pads (on their layers) or on its vias" if dragged else '')
          + (f"\n  {left} track end(s) on its pads stay behind ({'--no-drag' if a.no_drag else 'flipped: other layer' if a.flip else 'arcs'}): "
             f"re-route them" if left else '')
          + (f"\n  dropped {tear} teardrop(s) of the ends left behind" if tear else '')
          + (f"\n  moved {vin} via(s) inside its pads with it" if vin else ''))
    return 0


# ---------------------------------------------------------------- edit

EDIT_OPS = {'width': 2, 'del': 1, 'seg': 5, 'via': 3, 'solid': 1, 'simplify': 2,
            'ref': 3, 'hide': 1, 'show': 1, 'textsize': 3, 'text': 3, 'vcenter': 1,
            'value': 3, 'hide-value': 1, 'show-value': 1, 'valuesize': 3, 'mvvia': 2,
            'knockout': 2, 'retext': 2, 'addtext': 5, 'unround': 1, 'dragvia': 2}
EDIT_USE = ("edit OP... -o OUT   (one or more, in order; UUID = a unique prefix, from `copper`)\n"
            "  width UUID W                  a segment's or arc's width\n"
            "  del UUID                      delete a segment, arc, via or zone\n"
            "  seg NET LAYER W X1,Y1 X2,Y2   add a segment\n"
            "  via NET X,Y SIZE/DRILL        add a through via, written (free yes)\n"
            "  solid REF.PAD                 the pad joins zones solid (zone_connect 2), not by spokes\n"
            "  simplify UUID EPS             fit a zone outline's staircases with straight edges (corners\n"
            "                                within EPS mm dropped; its stale fill goes, refill after)\n"
            "  unround UUID|all              a faceted zone outline (arcs as chord runs or staircases) as\n"
            "                                sharp corners + (smoothing fillet) (radius 0.2), editable in\n"
            "                                KiCad; left faceted if KiCad's fillet would lose copper. all =\n"
            "                                the bands route --pour / fatten wrote (named route* / fatten*)\n"
            "  ref REF X,Y ANG|keep          move REF's designator to board X,Y (centred; angle absolute)\n"
            "  hide REF | show REF           hide / show REF's designator\n"
            "  textsize REF|UUID H T         REF's designator (or a board text's) height and stroke, mm\n"
            "  value REF X,Y ANG|keep        move REF's Value field, as ref (parts that use it as a label)\n"
            "  hide-value REF | show-value REF | valuesize REF H T   the Value field, as hide/show/textsize\n"
            "  text UUID X,Y ANG|keep        move a board silk text or a footprint's user text (a jumper's\n"
            "                                '1'/'3'), uuid from `silk REF` or `silk --box ..`; `del UUID` and\n"
            "                                `textsize UUID H T` take either too\n"
            "  knockout UUID|REF on|off      knockout (inverted) text: a board/user text, or REF's designator\n"
            "  retext UUID TEXT              a board or user text's content\n"
            "  addtext TEXT X,Y ANG f|b H    a new board silk text centred at X,Y, H mm (stroke 0.15 H, >= 0.15)\n"
            "  mvvia UUID R                  re-place a trackless (stitching) via at the nearest legal spot\n"
            "                                within R mm, same size/drill/free flag; reports the move and\n"
            "                                the net's via count within 3 mm before/after\n"
            "  dragvia UUID X,Y|auto         move a via WITH its tracks (KiCad's Drag) off the pad its drill\n"
            "                                sits in: ends still in that pad stay, a pad-to-via stub (--stub\n"
            "                                W) where nothing else joins them; auto = the nearest clear spot\n"
            "                                within 1.2 mm keeping every segment 0/45/90, drill clear of pads\n"
            "  vcenter all|DRC.json          snap track ends inside a same-net via onto its centre (a\n"
            "                                segment left zero-length goes); DRC.json (kdrc --report) limits\n"
            "                                it to its track_not_centered_on_via tracks. A snap that\n"
            "                                would break clearance is skipped and listed")


def mvvia(b, s, u, R):
    """the via's text with (at) moved to the nearest legal spot within R (stitch's search:
    the router's model, drill spacing, on the net's copper on >= 2 layers); the via itself
    is out of the model while it looks"""
    from kpcb_power import legal_spots, net_fills
    v = next((x for x in b.vias if x['uuid'] == u), None)
    if v is None:
        raise SystemExit(f"edit: {u[:8]} is not a via; nothing written")
    c = (v['x'], v['y'])
    if any(tr['net'] == v['net'] and min(math.dist(tr['a'], c), math.dist(tr['b'], c)) <= v['size'] / 2
           for tr in b.tracks):
        raise SystemExit(f"edit: via {u[:8]} has a track on it (mvvia moves stitching vias only); nothing written")
    near = lambda p: 1 + sum(1 for x in b.vias if x is not v and x['net'] == v['net']  # noqa: E731
                             and math.dist(p, (x['x'], x['y'])) <= 3)    # itself included, where it stands
    before = near(c)
    b.vias.remove(v)
    try:
        got = legal_spots(b, v['net'], *c, 0.0, R, v['size'], v['drill'], 1, False, net_fills(b, v['net']),
                          [(x['x'], x['y'], x['drill']) for x in b.vias])
    finally:
        b.vias.append(v)
    if not got:
        raise SystemExit(f"edit: no legal spot for via {u[:8]} within {R:g} mm; nothing written")
    x, y, d = got[0]
    if d < 1e-6:
        print(f"  mvvia {u[:8]} already sits on a legal spot: not moved")
        return s
    s, k = re.subn(r'\(at [^)]*\)', f"(at {num(x)} {num(y)})", s, count=1)
    assert k == 1
    after = near((x, y))
    print(f"  mvvia {u[:8]} {unesc_disp(v['net'])} {v['size']:g}/{v['drill']:g}" + (" free" if v['free'] else '')
          + f" {c[0]:.3f},{c[1]:.3f} -> {x:.3f},{y:.3f} ({d:.2f} mm); {unesc_disp(v['net'])} vias within 3 mm "
          f"{before} -> {after}")
    return s


DRAG_R, DRAG_STEP = 1.2, 0.05     # ponytail: dragvia auto's square grid; a ring search if 1.2 mm is short


def dragvia(b, t, cur, u, spec, stubw=None):
    """`edit dragvia UUID X,Y|auto`: move a via that has tracks on it (via_plan's Drag) off the
    pad its drill sits in. Tracks whose end still lands in that pad stay (the pad joins them); a
    stub joins pad and via on the pad's layer when nothing else does (the via's ring 50 um into
    the pad, a same-net fill holding both); the dragged ends' teardrops go. auto: the spots within DRAG_R where the drill clears
    every pad's mask opening and drills keep legal_spots' spacing, ranked by every moved or new
    segment staying 0/45/90, then distance."""
    from kpcb_move import World, via_plan, via_drag
    from kpcb_viapad import drill_depth
    from kpcb_smooth import Exact, octi
    v = next((x for x in b.vias if x['uuid'] == u), None)
    if v is None:
        raise SystemExit(f"edit: {u[:8]} is not a via; nothing written")
    c0, net, r = (v['x'], v['y']), v['net'], v['size'] / 2
    L = via_layers(v, b.copper)
    pads = [(f, p) for f in b.placed() for p in f.pads if p['kind'] == 'smd' and p['num']
            and math.dist((p['x'], p['y']), c0) < 4 and any(l in L for l in p['layers'])]
    dep = lambda p, xy: drill_depth(p, {'x': xy[0], 'y': xy[1], 'drill': v['drill']})     # noqa: E731
    vic = max(((dep(p, c0), f, p) for f, p in pads if p['net'] == net and dep(p, c0) > 0), default=None,
              key=lambda x: x[0])
    vf, vp = (vic[1], vic[2]) if vic else (None, None)
    pl = next((l for l in vp['layers'] if l in b.copper), None) if vp else None
    keep = {id(tr) for tr in b.tracks if vp and tr['net'] == net and tr['layer'] == pl
            and any(math.dist(e, c0) < r - 1e-3 and pad_dist(vp, e, True) <= 0 for e in (tr['a'], tr['b']))}
    on = [tr for tr in b.tracks if tr['net'] == net and min(math.dist(tr['a'], c0), math.dist(tr['b'], c0)) < r - 1e-3]
    w0 = stubw or min([max([tr['w'] for tr in on if tr['layer'] == pl] or [tr['w'] for tr in on] or [0.2])]
                      + ([min(vp['sx'], vp['sy'])] if vp else []))
    P = (vp['x'], vp['y']) if vp else None
    fl = [_pidx(b, f) for f in b.fills if pl and f['net'] == net and f['layer'] == pl]
    ex = Exact(b, net, pl, w0, (c0[0] - DRAG_R - 1, c0[1] - DRAG_R - 1, c0[0] + DRAG_R + 1, c0[1] + DRAG_R + 1)) if pl else None
    holes = [(x['x'], x['y'], x['drill']) for x in b.vias if x is not v] + \
        [(p['x'], p['y'], p['drill']) for f in b.placed() for p in f.pads if p['drill']]
    sep = lambda hd: max(v['size'] + 0.05, (v['drill'] + hd) / 2 + 0.25)                   # noqa: E731  as legal_spots
    w = World(b, via=v)

    def plan(to):
        """(cost, moved, stub, why-not) for the via at `to`"""
        if any(dep(p, to) > 0 for _f, p in pads):
            return None, None, None, 'its drill still reaches into a pad\'s mask opening'
        if any(math.dist(to, h[:2]) < sep(h[2]) for h in holes):
            return None, None, None, 'too close to another drill'
        stub = (P, to) if vp and not any(to in i and P in i for i in fl) \
            and pad_dist(vp, to, True) > r - 0.05 else None        # its ring still 50 um into the pad: joined
        moved = via_plan(b, v, to, keep, on)
        segs = [(a2, b2) for _t, a2, b2 in moved] + ([stub] if stub else [])
        return (not all(octi(a2, b2) for a2, b2 in segs if math.dist(a2, b2) > 1e-6), math.dist(to, c0)), moved, stub, ''

    def ok(to):
        cost, moved, stub, why = plan(to)
        if cost is None:
            return None, why
        new, moved = via_drag(b, v, to, w, keep)
        if new:
            return None, '; '.join(sorted(new.values())[:3])
        if stub and not ex.seg(*stub):
            return None, f"the {w0:g} mm stub from {vf.ref}.{vp['num']} would not clear"
        return (cost, to, moved, stub), ''

    if spec == 'auto':
        n = int(DRAG_R / DRAG_STEP)
        grid = [(round(c0[0] + i * DRAG_STEP, 4), round(c0[1] + j * DRAG_STEP, 4)) for i in range(-n, n + 1)
                for j in range(-n, n + 1) if 0 < math.hypot(i, j) * DRAG_STEP <= DRAG_R]
        cand = sorted((c_, to) for to in grid for c_ in [plan(to)[0]] if c_ is not None)
        best, whys = None, []
        for _c, to in cand:
            best, why = ok(to)
            if best:
                break
            whys.append(f"{to[0]:g},{to[1]:g}: {why}")
        if best is None:
            raise SystemExit(f"edit: dragvia {u[:8]}: no clear spot within {DRAG_R:g} mm ({len(cand)} with the drill off "
                             "every pad tried; nearest:\n    " + '\n    '.join(whys[:3]) + ")\n  nothing written; "
                             "`move REF X Y --no-drag` moves the part off the via instead")
    else:
        to = tuple(float(x) for x in spec.split(','))
        best, why = ok(to)
        if best is None:
            raise SystemExit(f"edit: dragvia {u[:8]} -> {spec}: {why}; nothing written")
    (skew, d), to, moved, stub = best
    cur[u] = re.sub(r'\(at \S+ \S+\)', f"(at {num(to[0])} {num(to[1])})", cur.get(u, t.text(u)), count=1)
    fold = 0
    for tr, a2, b2 in moved:
        if math.dist(a2, b2) < 1e-6:                 # an in-via sliver folded to nothing
            cur[tr['uuid']], fold = '', fold + 1
            continue
        seg = cur.get(tr['uuid'], t.text(tr['uuid']))
        for k, e, old in (('start', a2, tr['a']), ('end', b2, tr['b'])):
            if e != old:
                seg, k2 = re.subn(rf'\({k} \S+ \S+\)', f"({k} {num(e[0])} {num(e[1])})", seg, count=1)
                assert k2 == 1, (tr['uuid'], k)
        cur[tr['uuid']] = seg
    if stub:
        t.segment(net, pl, w0, *stub)
    near = [dict(tr, a=e, b=(e[0] + (o[0] - e[0]) * s_, e[1] + (o[1] - e[1]) * s_))
            for tr in on for e, o in ((tr['a'], tr['b']), (tr['b'], tr['a'])) if math.dist(e, c0) < r - 1e-3
            for s_ in [min(1.0, .5 / (tr['len'] or 1))]]
    tear = len(tear_edits(t, near))
    print(f"  dragvia {u[:8]} {unesc_disp(net)} {c0[0]:g},{c0[1]:g} -> {to[0]:g},{to[1]:g} ({d:.2f} mm"
          + (', off 0/45/90' if skew else '') + f"): {len(moved) - fold} track(s) dragged"
          + (f", {fold} in-via sliver(s) folded away" if fold else '')
          + (f", {len(keep)} left in {vf.ref}.{vp['num']}" if keep else '')
          + (f", stub {w0:g} mm {pl} from {vf.ref}.{vp['num']}" if stub else '')
          + (f", {tear} teardrop(s) dropped (regenerate in KiCad)" if tear else ''))


def _closer(ex, old, new):
    """does the moved segment `new` break clearance to any item it did not already break
    it to as `old`: per item, not Exact.seg's one verdict (an old track already too close
    to a fill would otherwise pass every snap)"""
    from kpcb_smooth import seg_pad_dist
    hw = ex.w / 2
    tests = ([lambda p, c, a=a, e=e, n=n: seg_seg_dist(p, c, a, e) - n for a, e, n in ex.trk]
             + [lambda p, c, o=o, n=n: pt_seg_dist(o, p, c) - n for o, n in ex.disks]
             + [lambda p, c, d=d, n=n: seg_pad_dist(p, c, d) - n for d, n in ex.pads]
             + [lambda p, c, u=u, v=v: seg_seg_dist(p, c, u, v) - ex.ec for u, v in ex.edge])
    for f in tests:
        m = f(*new) - hw
        if m < -1e-4 and m < f(*old) - hw - 1e-4:
            return True
    samp = lambda p, c: [(p[0] + (c[0] - p[0]) * k / 20, p[1] + (c[1] - p[1]) * k / 20) for k in range(21)]  # noqa: E731
    for idx, n in ex.fills:
        if any(idx.touches(x, n + hw) for x in samp(*new)) and not any(idx.touches(x, n + hw) for x in samp(*old)):
            return True
    return False


def vcenter(b, t, cur, src):
    """track ends inside a same-net via but off its centre (KiCad 10.99 has no CLI clean-up
    for track_not_centered_on_via) snap onto the centre; a segment both of whose ends land
    on one centre is deleted. `src` 'all', or a kdrc --report JSON naming the tracks"""
    import json
    from kpcb_move import is_arc
    from kpcb_smooth import Exact
    only = None
    if src != 'all':
        only = {it.get('uuid') for v in json.load(open(src)).get('violations', [])
                if v.get('type') == 'track_not_centered_on_via' for it in v.get('items', [])}
    vias = defaultdict(list)
    for v in b.vias:
        vias[v['net']].append(v)
    snapped, dropped, kept = 0, set(), []
    for tr in b.tracks:
        u = tr.get('uuid')
        if is_arc(tr) or not u or (only is not None and u not in only) or u not in t.span or cur.get(u) == '':
            continue
        new = [tr['a'], tr['b']]
        for i, e in enumerate(new):
            for v in vias[tr['net']]:
                if 0.0005 < math.dist(e, (v['x'], v['y'])) <= v['size'] / 2 - 1e-6:
                    new[i] = (v['x'], v['y'])
                    break
        if new == [tr['a'], tr['b']]:
            continue
        if math.dist(*new) < 1e-6:
            cur[u] = ''
            dropped.add(u)
            continue
        box = (min(new[0][0], new[1][0]) - 1, min(new[0][1], new[1][1]) - 1,
               max(new[0][0], new[1][0]) + 1, max(new[0][1], new[1][1]) + 1)
        if _closer(Exact(b, tr['net'], tr['layer'], tr['w'], box), (tr['a'], tr['b']), new):
            kept.append(u)
            continue
        s = cur.get(u, t.text(u))
        s, n1 = re.subn(r'\(start [^)]*\)', f"(start {num(new[0][0])} {num(new[0][1])})", s, count=1)
        s, n2 = re.subn(r'\(end [^)]*\)', f"(end {num(new[1][0])} {num(new[1][1])})", s, count=1)
        assert n1 == n2 == 1, u
        cur[u] = s
        snapped += 1
    print(f"  vcenter: {snapped} track end(s) snapped to the via centre, {len(dropped)} zero-length segment(s) deleted"
          + (f", {len(kept)} skipped (the snap breaks clearance): {' '.join(x[:8] for x in kept)}" if kept else '')
          + (f"  [{len(only)} track(s) named in {src}]" if only is not None else ''))
    return dropped


def _drop_cache(s, _old=None):
    """a moved or resized TrueType text's cached outline is stale: KiCad redraws it from the font"""
    if '(render_cache' in s:
        s = re.sub(r'\n(\t+)\(render_cache .*?\n\1\)', '', s, flags=re.S)
        print("  (TrueType text: its render_cache dropped; KiCad redraws it from the font)")
    return s


def _shift_cache(s, dx, dy):
    """a TrueType text moved without turning: its cached outline moves with it, so `silk`
    keeps judging the real glyphs (a dropped cache reads as a wider estimated box)"""
    sh = lambda m: re.sub(r'\(xy (\S+) (\S+)\)', lambda q: f"(xy {num(float(q[1]) + dx)} {num(float(q[2]) + dy)})", m[0])  # noqa: E731
    return re.sub(r'\n(\t+)\(render_cache .*?\n\1\)', sh, s, flags=re.S)


def fp_field(s, fu):
    """the span of the text (property or fp_text) with uuid fu inside a footprint's text"""
    for m in re.finditer(r'\n\t\t\((?:property|fp_text)(?: (?:"(?:[^"\\]|\\.)*"|\w+))+\n.*?\n\t\t\)', s, re.S):
        if f'(uuid "{fu}")' in m.group(0):
            return m
    raise SystemExit(f"edit: text {fu[:8]} not found in its footprint's tab layout; nothing written")


def _knockout(blk, on):
    blk, c = re.subn(r'\(layer ("[^"]+")(?: knockout)?\)', lambda m: f"(layer {m[1]}{' knockout' if on else ''})",
                     blk, count=1)
    assert c == 1
    return blk


def c_edit(b, a):
    """scripted hand edits on a copy, every match asserted: what a power pass or a
    clean-up needs between the routers (a width, a stray track, a stitching via)"""
    ops, i, args = [], 0, list(a.args)
    while i < len(args):
        k = args[i]
        n = EDIT_OPS.get(k)
        if n is None or i + n >= len(args):
            print(f"edit: bad or short op {k!r}\n{EDIT_USE}"); return 1
        ops.append((k, args[i + 1:i + 1 + n]))
        i += 1 + n
    if not ops or any(len(v) != EDIT_OPS[k] for k, v in ops):
        print(EDIT_USE); return 1
    if _need_out(a, 'edit'):
        return 1
    t = PcbText(b.path)
    cur = {}                                             # uuid -> its text as edited so far
    instub = set()                                       # in-via stubs: the via keeps its teardrops

    def one(pfx):
        m = [u for u in t.span if u.startswith(pfx)]
        if len(m) != 1:
            raise SystemExit(f"edit: {pfx!r} matches {len(m)} item(s); nothing written")
        return m[0]
    sub = {x['uuid']: (f, x) for f in b.fps.values() for x in f.fields if x['uuid']}

    def text_at(pfx, ops):
        """a board gr_text (its uuid, None) or a footprint text (the footprint's uuid, the
        field): what `text`/`textsize`/`del`/`knockout`/`retext` act on"""
        m = [(u, None) for u in t.span if u.startswith(pfx) and t.text(u).lstrip().startswith('(gr_text')] \
            + [(f.uuid, x) for u, (f, x) in sub.items() if u.startswith(pfx)]
        if len(m) != 1:
            raise SystemExit(f"edit: {pfx!r} matches {len(m)} board/footprint text(s); nothing written")
        if m[0][1] and m[0][1]['kind'] != 'user' and ops != 'any':
            raise SystemExit(f"edit: {pfx!r} is {sub[m[0][1]['uuid']][0].ref}'s {m[0][1]['kind']} field: "
                             f"use ref/value/hide/textsize REF; nothing written")
        return m[0]

    def text_edit(pfx, fn, ops='', cache=_drop_cache):
        """apply fn to a text block, in its footprint when it is a footprint text; `cache`
        then fixes its render_cache (default: dropped). cache(blk, old xy) for a move"""
        u, x = text_at(pfx, ops)
        s = cur.get(u, t.text(u))
        if x is None:
            at = re.search(r'\(at (\S+) (\S+)', s)
            cur[u] = cache(fn(s, None), (float(at[1]), float(at[2])))
            return u
        m = fp_field(s, x['uuid'])
        cur[u] = s[:m.start()] + cache(fn(m.group(0), sub[x['uuid']][0]), (x['x'], x['y'])) + s[m.end():]
        return x['uuid']
    xy = lambda s: tuple(float(v) for v in s.split(','))      # noqa: E731
    for k, v in ops:
        if k == 'width':
            u = one(v[0])
            s = cur.get(u, t.text(u))
            if not s.lstrip().startswith(('(segment', '(arc')):
                raise SystemExit(f"edit: {v[0]} is not a segment or arc; nothing written")
            s, c = re.subn(r'\(width [^)]*\)', f"(width {num(float(v[1]))})", s, count=1)
            assert c == 1
            cur[u] = s
            print(f"  width {u[:8]} -> {float(v[1]):g}")
        elif k == 'del' and any(u.startswith(v[0]) for u in sub):     # a footprint user text
            print(f"  del   {text_edit(v[0], lambda blk, f: '')[:8]} (footprint user text)")
        elif k == 'del':
            u = one(v[0])
            if u in cur and not cur[u]:
                raise SystemExit(f"edit: {u[:8]} deleted twice; nothing written")
            cur[u] = ''
            print(f"  del   {u[:8]} ({t.text(u).split()[0].lstrip('(')})")
        elif k == 'seg':
            net, ly, w, p0, p1 = v
            ends = []
            for e in (xy(p0), xy(p1)):                   # within 1 um of a same-net via/pad centre: onto it
                c = min([(v2['x'], v2['y']) for v2 in b.vias if v2['net'] == net] +
                        [(p['x'], p['y']) for f in b.placed() for p in f.pads if p['net'] == net],
                        key=lambda c: math.dist(c, e), default=None)
                ends.append(c if c and 0 < math.dist(c, e) <= 1e-3 else e)
            t.segment(net, ly, float(w), *ends)
            snap = [f"{e[0]:g},{e[1]:g}" for e, s0 in zip(ends, (p0, p1)) if e != xy(s0)]
            print(f"  seg   {net} {ly} w {float(w):g} {p0} -> {p1}" + (f"  (snapped onto the centre {' '.join(snap)})" if snap else ''))
        elif k == 'via':
            net, p, sd = v
            sz, dr = (float(x) for x in sd.split('/'))
            t.via(net, *xy(p), sz, dr, [b.copper[0], b.copper[-1]], free=True)
            print(f"  via   {net} {p} {sz:g}/{dr:g} (free)")
        elif k == 'simplify':
            from kpcb_route import simplify
            u = one(v[0])
            s = cur.get(u, t.text(u))
            pols = list(re.finditer(r'(\n\t\t\(polygon\n\t\t\t\(pts\n)(.*?)(\n\t\t\t\)\n\t\t\))', s, re.S))
            if not s.lstrip().startswith('(zone') or not pols or any('(arc' in m[2] for m in pols):
                raise SystemExit(f"edit: {v[0]} is not a zone with a straight-edged outline; nothing written")
            for m in reversed(pols):
                pts = [(float(x), float(y)) for x, y in re.findall(r'\(xy (\S+) (\S+)\)', m[2])]
                P = simplify(pts, float(v[1]))
                s = s[:m.start(2)] + ''.join(f"\t\t\t\t(xy {num(x)} {num(y)})\n" for x, y in P).rstrip('\n') + s[m.end(2):]
                print(f"  simplify {u[:8]} {len(pts)} -> {len(P)} corner(s)")
            s, nf = re.subn(r'\n\t\t\(filled_polygon\n.*?\n\t\t\)', '', s, flags=re.S)
            if nf:
                print(f"  ({nf} stale fill polygon(s) dropped: refill)")
            cur[u] = s
        elif k == 'unround':
            from kpcb_fillet import fillet_outline, smooth_fill
            every = v[0] == 'all'
            done = 0
            for u in [u for u in t.span if t.text(u).lstrip().startswith('(zone')] if every else [one(v[0])]:
                s = cur.get(u, t.text(u))
                pols = list(re.finditer(r'(\n\t\t\(polygon\n\t\t\t\(pts\n)(.*?)(\n\t\t\t\)\n\t\t\))', s, re.S))
                nm = re.search(r'\n\t\t\(name "([^"]*)"\)', s)
                if not s.lstrip().startswith('(zone') or '(teardrop' in s or '(keepout' in s or len(pols) != 1 \
                        or '(arc' in pols[0][2] or every and not (nm and nm[1].startswith(('route', 'fatten'))):
                    if every:
                        continue
                    raise SystemExit(f"edit: {v[0]} is not a copper zone with one straight-edged outline; nothing written")
                m = pols[0]
                lab = f"{u[:8]} {(nm[1] if nm else '')[:28]}"
                if '(smoothing fillet)' in s:            # already sharp + fillet: its corners are meant
                    if not every:
                        print(f"  unround {lab}: already (smoothing fillet), left as is")
                    continue
                Q, r, note = fillet_outline([(float(x), float(y)) for x, y in re.findall(r'\(xy (\S+) (\S+)\)', m[2])])
                s2 = smooth_fill(s[:m.start(2)] + ''.join(f"\t\t\t\t(xy {num(x)} {num(y)})\n" for x, y in Q).rstrip('\n')
                                 + s[m.end(2):], r) if r else None
                if s2 is None:
                    if not every or 'faceted' in note:
                        print(f"  unround {lab}: {note if r is None else 'no (fill yes) block'}")
                    continue
                cur[u] = re.sub(r'\n\t\t\(filled_polygon\n.*?\n\t\t\)', '', s2, flags=re.S)
                done += 1
                print(f"  unround {lab}: {note}")
            if every:
                print(f"  unround all: {done} zone(s) now sharp + fillet {'(stale fills dropped: refill)' if done else ''}")
        elif k == 'textsize' and v[0] not in b.fps:     # a board or footprint user text, by uuid
            from kpcb_silk import edit_text
            u = text_edit(v[0], lambda blk, f: edit_text(blk, h=float(v[1]), t=float(v[2])))
            print(f"  textsize {u[:8]} {v[1]} {v[2]}")
        elif k == 'knockout':
            if v[1] not in ('on', 'off'):
                raise SystemExit(f"edit: knockout wants on|off, not {v[1]!r}; nothing written")
            kn = lambda blk, f: _knockout(blk, v[1] == 'on')      # noqa: E731
            if v[0] in b.fps:                                    # REF: its designator
                fx = next((x for x in b.fps[v[0]].fields if x['kind'] == 'Reference' and x['uuid']), None)
                if not fx:
                    raise SystemExit(f"edit: {v[0]} has no Reference field with a uuid; nothing written")
                text_edit(fx['uuid'], kn, 'any')
            else:
                text_edit(v[0], kn)
            print(f"  knockout {v[0]} {v[1]}")
        elif k == 'retext':
            def rt(blk, f):
                blk, c = re.subn(r'^(\n?\t+\((?:gr_text|fp_text user|property "[^"]*") )"(?:[^"\\]|\\.)*"',
                                 lambda m: m[1] + qs(v[1]), blk, count=1)
                assert c == 1
                return blk
            u = text_edit(v[0], rt)
            print(f"  retext {u[:8]} -> {v[1]!r}")
        elif k == 'addtext':
            from kpcb_label import gr_text
            if v[3] not in ('f', 'b'):
                raise SystemExit(f"edit: addtext side is f or b, not {v[3]!r}; nothing written")
            h = float(v[4])
            t.add(gr_text(v[0], *xy(v[1]), float(v[2]), v[3] == 'b', h, max(0.15, round(0.15 * h, 3))))
            print(f"  addtext {v[0]!r} {v[1]} {v[2]} deg {v[3].upper()}.SilkS {h:g} mm")
        elif k in ('ref', 'hide', 'show', 'textsize', 'value', 'hide-value', 'show-value', 'valuesize'):
            from kpcb_silk import ref_block, edit_text
            from kpcb_move import fp_local
            f = b.fps.get(v[0])
            if not f:
                raise SystemExit(f"edit: no footprint {v[0]!r}; nothing written")
            fld = 'Value' if k in ('value', 'hide-value', 'show-value', 'valuesize') else 'Reference'
            s = cur.get(f.uuid, t.text(f.uuid))
            m = ref_block(s, fld)
            if not m:
                raise SystemExit(f"edit: {f.ref} has no {fld} field; nothing written")
            if k in ('ref', 'value'):
                nb = edit_text(m.group(0), fp_local(f, xy(v[1])), None if v[2] == 'keep' else float(v[2]), centre=True)
            elif k in ('textsize', 'valuesize'):
                nb = edit_text(m.group(0), h=float(v[1]), t=float(v[2]))
            else:
                nb = edit_text(m.group(0), hide=k.startswith('hide'))
            cur[f.uuid] = s[:m.start()] + nb + s[m.end():]
            print(f"  {k:<5} {f.ref} " + ' '.join(v[1:]))
        elif k == 'text':
            from kpcb_silk import edit_text
            from kpcb_move import fp_local
            ang, to = None if v[2] == 'keep' else float(v[2]), xy(v[1])
            keep = lambda blk, old: _shift_cache(blk, to[0] - old[0], to[1] - old[1])   # noqa: E731
            u = text_edit(v[0], lambda blk, f: edit_text(blk, fp_local(f, to) if f else to, ang),
                          cache=keep if ang is None else _drop_cache)
            print(f"  text  {u[:8]} -> {v[1]} {v[2]}")
        elif k == 'mvvia':
            u = one(v[0])
            cur[u] = mvvia(b, cur.get(u, t.text(u)), u, float(v[1]))
        elif k == 'vcenter':
            instub |= vcenter(b, t, cur, v[0])
        elif k == 'dragvia':
            dragvia(b, t, cur, one(v[0]), v[1], a.stub)
        else:                                            # solid REF.PAD
            ref, _, pad = v[0].partition('.')
            f = b.fps.get(ref)
            if not f or not pad:
                raise SystemExit(f"edit: no footprint {ref!r} (solid wants REF.PAD); nothing written")
            s = cur.get(f.uuid, t.text(f.uuid))
            ms = list(re.finditer(r'\n\t\t\(pad "%s" .*?\n\t\t\)' % re.escape(pad), s, re.S))
            if len(ms) != 1:
                raise SystemExit(f"edit: {v[0]} matches {len(ms)} pad(s); nothing written")
            blk = ms[0].group(0)
            was = re.search(r'\n\t\t\t\(zone_connect (\d)\)', blk)
            if was:
                nb = re.sub(r'(\n\t\t\t)\(zone_connect \d\)', r'\1(zone_connect 2)', blk, count=1)
            else:                                        # KiCad's order: net, pinfunction, pintype, zone_connect
                for tag in ('pintype', 'pinfunction', 'net'):
                    m2 = re.search(r'\n\t\t\t\(%s [^\n]*\)' % tag, blk)
                    if m2:
                        nb = blk[:m2.end()] + '\n\t\t\t(zone_connect 2)' + blk[m2.end():]
                        break
                else:
                    raise SystemExit(f"edit: {v[0]} has no (net) line to anchor on; nothing written")
            cur[f.uuid] = s[:ms[0].start()] + nb + s[ms[0].end():]
            print(f"  solid {v[0]}" + (' (was already solid)' if was and was[1] == '2' else ''))
    for u, s in cur.items():
        t.sub(u, s)
    gone = [tr for tr in b.tracks if cur.get(tr['uuid'], None) == '' and tr['uuid'] not in instub]
    tear = len(tear_edits(t, gone))                      # a deleted track's teardrop would orphan
    t.save(a.out)
    print(f"edit: {len(ops)} op(s)" + (f", {tear} teardrop(s) of deleted tracks dropped" if tear else '')
          + f" -> {a.out}\n  verify: kdrc.py {a.out} --unconnected --baseline BEFORE.json")
    return 0
