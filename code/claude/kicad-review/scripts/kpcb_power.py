"""kpcb.py `fatten` and `stitch`: widen a power path that is already routed, and add the
vias its layer changes need. Both write a copy (-o OUT); KiCad's DRC on it is the verdict
(`kdrc.py OUT drc --refill-save --baseline BEFORE.json`), `ampacity --fills` the measure.

`fatten NET... [--layers L,..] [--width W] [--minw M] [--box X0,Y0,X1,Y1] [--keep BOX]` wraps
each net's tracks on each layer (those >= --minw wide, 0.5: a sense or thermistor leg stays
thin) in a same-net zone band W wide (default: 2x the netclass width, at least 2 mm),
rastered at 0.05 mm (the traced staircases fitted with straight edges), minus: every
other net's zone outline (+0.3 mm), rule areas that
forbid pours (footprint ones too), the inner layer under outer RF-netclass traces (their
reference plane, 1.5 mm each side), a 1.0 mm halo round the GND pads of every part on the
net (room for the GND pour's thermal spokes; a shunt's or a decoupling cap's ground must stay reachable: the parsnip -BATT band
cut R41.4 off without it), bands made earlier in the same run, and --keep boxes. Holes
are slit open (a KiCad zone outline is one ring) and each piece gets its own priority above
every zone on the board, so no two tie. The refill then fits the band round every foreign
item, necking only where something is in the way.

`stitch NET --sparse T [--keep BOX;..] [--box ...]` stitches the net's outer-layer pour: the
point farthest from any NET via or plated hole in its own F/B fill fragment first, a legal
spot near it (the search below), repeated until every point is within T mm (--keep: boxes
no via goes in, e.g. an antenna keepout or a pocket left for routing).

`stitch NET [X,Y] [-r R] [--box ...] [--n N] [--via S/D] [--in-pad]` finds through-via spots
legal in the router's own model (route's Grid: netclass and .kicad_dru net-pair clearances,
hole to hole, copper to edge, rule areas incl. footprint ones), on NET copper (track, pad
or saved fill) on >= 2 layers and in no other net's power pour: the N nearest X,Y within R,
or N beside each NET via in --box (0.6-1.6 mm out). Dry run lists them; -o OUT writes them
`(free yes)`, since KiCad re-nets a trackless via from the copper it touches otherwise.
--in-pad lets a via sit wholly inside a same-net SMD pad of >= 3 mm2 (FET, fuse, inductor,
holder tab - tented or filled), never in a small pad (paste wicks)."""
import math
from collections import defaultdict
from kcommon import unesc_disp
from kpcb_board import PolyIndex, bbox, grow, ipc_width, pad_box, pt_seg_dist, thick_of, xf
from kpcb_copper import _local
from kpcb_edit import PcbText, _need_out
from kpcb_move import cu_layers
from kpcb_route import ONES, Grid, _iv, _kos, _netclass, _trace, _area, class_via

RES = 0.05


class Raster:
    """a bytearray over a box, cell centres at x0 + i*res, painted by the router's
    row-interval shapes (`fatten` at 0.05 mm, `ampacity --fills` at its --cell)"""

    def __init__(self, box, res=RES):
        self.x0, self.y0, self.res = box[0], box[1], res
        self.W = int((box[2] - box[0]) / res) + 3
        self.H = int((box[3] - box[1]) / res) + 3
        self.d = bytearray(self.W * self.H)

    def paint(self, shape, R, val=1):
        y_lo, y_hi, f = _iv(shape, R)
        W, zero, res = self.W, bytes(self.W), self.res
        for j in range(max(0, math.ceil((y_lo - self.y0) / res)), min(self.H - 1, math.floor((y_hi - self.y0) / res)) + 1):
            iv = f(self.y0 + j * res)
            if iv:
                i0 = max(0, math.ceil((iv[0] - self.x0) / res - 1e-9))
                i1 = min(W - 1, math.floor((iv[1] - self.x0) / res + 1e-9))
                if i0 <= i1:
                    self.d[j * W + i0:j * W + i1 + 1] = (ONES if val else zero)[:i1 - i0 + 1]

    def poly(self, pts, grow_by=0.0, val=1):
        """a polygon's inside (even-odd), grown by painting its edges as octagon capsules (`octo`)"""
        idx = PolyIndex(pts)
        fb, W, res = idx.bbox, self.W, self.res
        for j in range(max(0, int((fb[1] - self.y0) / res)), min(self.H, int((fb[3] - self.y0) / res) + 2)):
            y = self.y0 + j * res
            xs = sorted(x1 + (y - y1) * (x2 - x1) / (y2 - y1)
                        for x1, y1, x2, y2 in idx.rows.get(math.floor(y / idx.cell), ()) if (y1 > y) != (y2 > y))
            for a0, a1 in zip(xs[::2], xs[1::2]):
                i0, i1 = max(0, math.ceil((a0 - self.x0) / res)), min(W - 1, int((a1 - self.x0) / res))
                if i0 <= i1:
                    self.d[j * W + i0:j * W + i1 + 1] = (ONES if val else bytes(W))[:i1 - i0 + 1]
        if grow_by > 0:
            for u, v in zip(pts, pts[1:] + pts[:1]):
                self.poly(octo([u, v], grow_by), 0.0, val)


C8 = math.cos(math.pi / 8)
OCT = [(math.cos((2 * k + 1) * math.pi / 8) / C8, math.sin((2 * k + 1) * math.pi / 8) / C8) for k in range(8)]


def hull(pts):
    """convex hull (monotone chain), counter-clockwise"""
    P = sorted(set(pts))
    if len(P) < 3:
        return P
    def half(seq):
        h = []
        for p in seq:
            while len(h) > 1 and (h[-1][0] - h[-2][0]) * (p[1] - h[-2][1]) - (h[-1][1] - h[-2][1]) * (p[0] - h[-2][0]) <= 0:
                h.pop()
            h.append(p)
        return h[:-1]
    return half(P) + half(P[::-1])


def octo(pts, r):
    """pts (a segment's two ends, a pad's corners) grown by an octagon of inradius r: a convex
    polygon whose every grown edge runs at 0/45/90 deg. A round grow left each band end and
    GND-pad bite as an arc of chords (20-115 odd-angle corners per zone, Ryan 2026-10-28)"""
    return hull([(x + r * u, y + r * v) for x, y in pts for u, v in OCT])


def pad_halo(p, r):
    """pad p's copper grown by r with square corners (a custom pad: its box; a round one: its
    square): a band's bite round it is a plain rectangular notch, never a chamfered nub"""
    if p['prims']:
        bb = pad_box(p)
        pts = [(bb[0], bb[1]), (bb[2], bb[3])]
    elif p['shape'] == 'circle':
        pts = [(p['x'] - p['sx'] / 2, p['y'] - p['sx'] / 2), (p['x'] + p['sx'] / 2, p['y'] + p['sx'] / 2)]
    else:
        pts = [xf(u, v, p['x'], p['y'], p['prot']) for u in (-p['sx'] / 2, p['sx'] / 2) for v in (-p['sy'] / 2, p['sy'] / 2)]
    return hull([(x + dx, y + dy) for x, y in pts for dx in (-r, r) for dy in (-r, r)])


def ongrid(box, res=RES):
    """box moved out so the outline corners `_outlines` traces (cell corners) land on the res
    grid, as KiCad's own grid places a hand-drawn zone's"""
    return ((math.floor(box[0] / res) - 0.5) * res, (math.floor(box[1] / res) - 0.5) * res, box[2] + res, box[3] + res)


def band(segs, hw):
    """the on-grid Raster of segs [(p0, p1)] each octagon-grown by hw (`route --pour`'s band)"""
    r = Raster(ongrid(grow(bbox([q for s in segs for q in s]), hw + 0.5)))
    for p0, p1 in segs:
        r.poly(octo([p0, p1], hw))
    return r


def _outlines(r, slit=True):
    """zone outlines [(x, y)] for the set cells: diagonal-only touches closed, every hole
    slit open to the outside (one ring per piece, as a KiCad zone outline is; slit=False:
    a hole comes back as a loop of its own), each put on 0/45/90 lines (kpcb_fillet.octilinear)"""
    from kpcb_fillet import octilinear
    W, H, d = r.W, r.H, r.d
    for j in range(H - 1):                           # a checkerboard 2x2 is two loops meeting
        for i in range(W - 1):                       # at a point: fill it in
            a_, b_, c_, e_ = d[j * W + i], d[j * W + i + 1], d[(j + 1) * W + i], d[(j + 1) * W + i + 1]
            if a_ and e_ and not b_ and not c_ or b_ and c_ and not a_ and not e_:
                d[j * W + i] = d[j * W + i + 1] = d[(j + 1) * W + i] = d[(j + 1) * W + i + 1] = 1
    ext = bytearray(W * H)                           # empty cells reachable from the border
    stack = [k for k in range(W * H) if not d[k] and (k < W or k >= W * (H - 1) or k % W in (0, W - 1))]
    for k in stack:
        ext[k] = 1
    while stack:
        k = stack.pop()
        i, j = k % W, k // W
        for ni, nj in ((i + 1, j), (i - 1, j), (i, j + 1), (i, j - 1)):
            if 0 <= ni < W and 0 <= nj < H:
                nk = nj * W + ni
                if not d[nk] and not ext[nk]:
                    ext[nk] = 1
                    stack.append(nk)
    for k in range(W * H) if slit else ():           # a hole: slit it up to the outside
        if not d[k] and not ext[k]:
            i, j = k % W, k // W                     # (row-major: the first cell met is its top)
            jj = j - 1
            while jj >= 0 and not ext[jj * W + i]:
                d[jj * W + i] = 0
                ext[jj * W + i] = 1
                jj -= 1
            seen = [k]                               # the hole itself now belongs outside
            ext[k] = 1
            while seen:
                q = seen.pop()
                qi, qj = q % W, q // W
                for ni, nj in ((qi + 1, qj), (qi - 1, qj), (qi, qj + 1), (qi, qj - 1)):
                    if 0 <= ni < W and 0 <= nj < H:
                        nk = nj * W + ni
                        if not d[nk] and not ext[nk]:
                            ext[nk] = 1
                            seen.append(nk)
    out, res = [], r.res
    for L in _trace(d, W, H):
        if _area(L) * res * res >= 0.3:
            out.append(octilinear([(round(r.x0 + (ci - 0.5) * res, 4), round(r.y0 + (cj - 0.5) * res, 4)) for ci, cj in L]))
    return out


def _boxes(s):
    return [tuple(float(v) for v in x.split(',')) for x in (s or '').split(';') if x.strip()]


def c_fatten(b, a):
    from kpcb_board import _resolve_net
    nets = []
    for name in list(a.args) + list(a.net or []):
        n, cand = _resolve_net(b, name)
        if not n:
            print(f"{name}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); return 1
        nets.append(n)
    if not nets:
        print("fatten NET... --amps A [--dt 10] | --width W [--layers L,..] [--minw 0.5] [--box X0,Y0,X1,Y1] "
              "[--keep X0,Y0,X1,Y1;...] -o OUT")
        return 1
    amps = getattr(a, 'amps', None)
    if not a.width and not amps:
        print("fatten needs a purpose: --amps A (the current this path carries; only tracks too thin for it at --dt "
              "get a band, as wide as A needs) or --width W. A band that only shaves a few mohm is copper with no "
              "job: `widen` the track instead")
        return 1
    if _need_out(a, 'fatten'):
        return 1
    layers = [l for l in (a.layers.split(',') if a.layers else b.copper) if l in b.copper]
    box = _boxes(a.box)[0] if a.box else None
    keep = _boxes(a.keep)
    minw = a.minw if a.minw is not None else 0.5
    kos = _kos(b)
    rfl = {t['layer'] for t in b.tracks if t['layer'] in b.outer and 'RF' in (b.netclass(t['net'] or '') or '').upper()}
    ref = {b.copper[1] if ly == b.copper[0] else b.copper[-2] for ly in rfl} - b.outer if len(b.copper) > 2 else set()
    rf = [t for t in b.tracks if t['layer'] in rfl and 'RF' in (b.netclass(t['net'] or '') or '').upper()]
    t = PcbText(b.path)
    prio = 1 + max((z.priority for z in b.zones), default=0)
    made, rc, banded = [], 0, set()
    from kpcb_copper import dead_copper
    for net in nets:
        cls = _netclass(b, net)
        w = a.width or max(2 * (cls.get('track_width') or 0.5), 2.0)
        c = cls.get('clearance') or 0.2
        gnd = [(p, f) for f in b.placed() if any(q['net'] == net for q in f.pads) for p in f.pads if p['net'] == 'GND']
        dead = {id(t) for d in dead_copper(b, net) for t, _L, k in d['tracks'] if k is None}   # never band copper serving no pad
        if dead:
            print(f"fatten {unesc_disp(net)}: {len(dead)} track(s) serve no pad, left out (`ripup --deadend` them)")
        for ly in layers:
            segs = [s for s in b.tracks if s['net'] == net and s['layer'] == ly and s['w'] >= minw - 1e-9 and id(s) not in dead and
                    (not box or any(box[0] <= q[0] <= box[2] and box[1] <= q[1] <= box[3] for q in (s['a'], s['b'])))]
            wl = w
            if amps and not a.width:                 # the purpose: carry amps at --dt, as wide as that needs
                need = math.ceil(ipc_width(amps, thick_of(b, ly), ly in b.outer, a.dt) / RES - 1e-6) * RES
                ok = [s for s in segs if s['w'] >= need - 1e-9]
                segs = [s for s in segs if s['w'] < need - 1e-9]
                if ok:
                    print(f"fatten {unesc_disp(net)} {ly}: {len(ok)} track(s) already carry {amps:g} A at {a.dt:g} C "
                          f"(>= {need:.2f} mm), no band")
                wl = need
            own = [(z.name or z.uuid[:8], PolyIndex(z.outline)) for z in b.zones if z[0] == net and z.outline and
                   not z.keepout and (ly in z[1] or '*.Cu' in z[1])]
            inz = defaultdict(int)                   # a same-net zone pours them already: a band = 2 zones, 1 copper
            for s in segs:
                zn = next((n for n, ix in own if s['a'] in ix and s['b'] in ix and s['mid'] in ix), None)
                if zn:
                    inz[zn] += 1
            if inz:
                segs = [s for s in segs if not any(s['a'] in ix and s['b'] in ix and s['mid'] in ix for _n, ix in own)]
                print(f"fatten {unesc_disp(net)} {ly}: " + ', '.join(f"{k} track(s) inside zone '{n}'" for n, k in inz.items())
                      + " left out (that zone already pours them)")
            if not segs:
                continue
            banded |= {s['uuid'] for s in segs}
            r = Raster(ongrid(grow(bbox([q for s in segs for q in (s['a'], s['b'])]), wl / 2 + 0.5)))
            for s in segs:
                r.poly(octo([s['a'], s['b']], wl / 2))
            for z in b.zones:                        # other nets' zones and pour keepouts stay theirs
                zn, zl = z
                if z.outline and (ly in zl or '*.Cu' in zl) and (z.keepout or zn not in (net, 'GND', '')):
                    r.poly(z.outline, 0.3, 0)
            for lys, idx, _nv, nopour, *_x in kos:
                if nopour and ly in lys:
                    r.poly(idx.pts, 0.3, 0)
            if ly in ref:
                for s in rf:
                    r.poly(octo([s['a'], s['b']], s['w'] / 2 + 1.5), 0.0, 0)
            for p, f in gnd:                         # its own parts' ground stays reachable: room for
                if p['drill'] or ly in cu_layers(p['layers'], b.copper) or ly not in b.outer:   # the GND pour's
                    r.poly(pad_halo(p, 1.0), 0.0, 0)   # 0.5 thermal gap + a spoke + clearance
            for kb in keep:
                r.poly([(kb[0], kb[1]), (kb[2], kb[1]), (kb[2], kb[3]), (kb[0], kb[3])], 0.0, 0)
            for n2, l2, ring in made:
                if l2 == ly and n2 != net:
                    r.poly(ring, 0.3, 0)
            rings, thin = [], 0
            for ring in _outlines(r):                # under 2 mm2, or squeezed to about its tracks' own copper:
                ix = PolyIndex(ring)                 # a wider track does that job without a zone
                if _area(ring) >= max(2.0, 1.25 * sum(s['w'] * s['len'] for s in segs if s['mid'] in ix)):
                    rings.append(ring)
                else:
                    thin += 1
            if thin:
                print(f"fatten {unesc_disp(net)} {ly}: {thin} piece(s) under 2 mm2 or < 1.25x their tracks' copper "
                      "left out (`widen` those tracks instead)")
            area = 0.0
            for ring in rings:
                t.zone(net, ly, ring, prio, c, 0.25, name=f"fatten {net}")
                made.append((net, ly, ring))
                area += _area(ring)
                prio += 1
            print(f"fatten {unesc_disp(net)} {ly}: {len(segs)} track(s) >= {minw:g} mm -> "
                  f"{len(rings)} zone(s), a {wl:g} mm band, {area:.1f} mm2 outline, {sum(map(len, rings))} corners")
    if not made:
        print("fatten: no tracks to widen (check --minw, --layers, --box)"); return 1
    t.save(a.out)
    if getattr(a, 'drop_track', False):
        _drop_banded(b, t, a.out, nets, banded)
    print(f"wrote {len(made)} zone(s) (priority {prio - len(made)}..{prio - 1}) -> {a.out}\n"
          f"  verify: kdrc.py {a.out} drc --refill-save --unconnected --baseline BEFORE.json, then\n"
          f"  `airwires --since BEFORE` (a band can cut a plane region off) and `ampacity --from A --to B --fills`")
    return rc


def _drop_banded(b, t, out, nets, banded):
    """fatten --drop-track: refill a temp copy of OUT (kicad-cli, in the project's rules; OUT
    itself is never a kicad-cli save), then delete, in fatten's own edit `t`, the banded tracks
    the refilled band wholly holds - `ripup NET --inpour` on just those, thermal-spoke guard and
    prune included - and write OUT again"""
    import os, subprocess, tempfile
    from kcommon import kicad_cli
    from kdrc import copy_project
    from kpcb_board import Board
    from kpcb_edit import inpour_kill, tear_edits
    with tempfile.TemporaryDirectory() as d:
        stem = copy_project(b.path, d, pcb=out)[0]
        tb = os.path.join(d, stem + '.kicad_pcb')
        rep = os.path.join(d, 'r.json')
        r = subprocess.run([kicad_cli(tb), 'pcb', 'drc', '--refill-zones', '--save-board', '--format', 'json', '-o', rep, tb],
                           capture_output=True, text=True)
        if not os.path.exists(rep):
            print(f"  --drop-track: the kicad-cli refill failed, tracks kept: {(r.stderr or r.stdout).strip()[:200]}")
            return
        fb = Board(tb)
        kill, _cand, kept = inpour_kill(fb, set(nets), None, only=banded)
        gone = {o['uuid'] for o in fb.tracks + fb.vias if id(o) in kill}
    for u in gone:
        if u in t.span:
            t.sub(u, '')
    tear = len(tear_edits(t, [x for x in b.tracks if x['uuid'] in gone]))
    t.save(out)
    print(f"  --drop-track: {len(gone)} banded track(s) and via(s) the refilled band wholly holds deleted ({len(kept)} "
          f"kept: a thermal pad's spoke), {tear} teardrop(s); OUT is still unfilled - refill it")


def _on_net(b, trk, pds, x, y, vsz, fills):
    """copper layers where a via disk at x,y lands on the net: a track's body, a pad (trk,
    pds: the net's tracks and pads near the window), a saved fill"""
    cu, out = b.copper, set()
    for s in trk:
        if pt_seg_dist((x, y), s['a'], s['b']) <= s['w'] / 2 - vsz / 4:
            out.add(s['layer'])
    for p in pds:
        u, v = _local(p, (x, y))
        if abs(u) <= p['sx'] / 2 and abs(v) <= p['sy'] / 2:
            out |= set(cu) if p['drill'] else cu_layers(p['layers'], cu)
    for ly, idxs in fills.items():
        if ly not in out and any(all((x + dx, y + dy) in ix for dx, dy in
                                     ((0, 0), (vsz / 2, 0), (-vsz / 2, 0), (0, vsz / 2), (0, -vsz / 2))) for ix in idxs):
            out.add(ly)
    return out


def _own_pad_ok(pds, x, y, vsz, in_pad):
    """a via disk overlapping a same-net SMD pad: only wholly inside a big one, with --in-pad;
    never within 0.1 mm of a same-net plated-hole pad"""
    for p in pds:
        u, v = _local(p, (x, y))
        hx, hy = p['sx'] / 2, p['sy'] / 2
        gap = math.hypot(max(abs(u) - hx, 0), max(abs(v) - hy, 0))
        if p['drill']:                               # never in (or kissing) a soldered annular ring: it wicks
            if gap < vsz / 2 + 0.1:                  # solder, and the plated hole already ties every layer
                return False
            continue
        if gap >= vsz / 2:
            continue                                 # clear of this pad
        if not in_pad or p['sx'] * p['sy'] < 3.0 or abs(u) > hx - vsz / 2 or abs(v) > hy - vsz / 2:
            return False
    return True


def net_fills(b, net, dead=frozenset()):
    fills = defaultdict(list)
    for fl in b.fills:
        if fl['net'] == net and id(fl) not in dead:
            fills[fl['layer']].append(PolyIndex(fl['pts']))
    return fills


def legal_spots(b, net, ax, ay, r0, r1, vsz, vdr, n, in_pad, fills, have, dead=frozenset()):
    """up to n via spots r0..r1 from ax,ay, nearest first: legal in the router's model, clear
    of every drill in `have` [(x, y, drill)] (appended to), and on the net's copper on >= 2
    layers, not counting `dead` tracks (ids: copper that serves no pad). [(x, y, distance)]"""
    win = (ax - r1 - 1, ay - r1 - 1, ax + r1 + 1, ay + r1 + 1)
    g = Grid(b, net, win, RES, 0.2, vsz, vdr, [], ownpads=not in_pad)    # no layers: only the via rasters are read
    inw = lambda bb: bb[2] >= win[0] and bb[0] <= win[2] and bb[3] >= win[1] and bb[1] <= win[3]   # noqa: E731
    trk = [s for s in b.tracks if s['net'] == net and id(s) not in dead and inw(bbox([s['a'], s['b']]))]
    pds = [p for f in b.placed() for p in f.pads if p['net'] == net and inw(pad_box(p))]
    got, cells = [], []
    for j in range(g.H):
        for i in range(g.W):
            x, y = g.x0 + i * RES, g.y0 + j * RES
            d = math.hypot(x - ax, y - ay)
            if r0 - 1e-9 <= d <= r1 and not g.vblk[j * g.W + i] and not g.vpour[j * g.W + i]:
                cells.append((d, x, y))
    sep = lambda hd: max(vsz + 0.05, (vdr + hd) / 2 + 0.25)      # noqa: E731  drill to drill
    near = [h for h in have if abs(h[0] - ax) <= r1 + sep(h[2]) and abs(h[1] - ay) <= r1 + sep(h[2])]
    for d, x, y in sorted(cells):
        if len(got) >= n:
            break
        if any(math.dist((x, y), (hx, hy)) < sep(hd) for hx, hy, hd in near):
            continue
        if len(_on_net(b, trk, pds, x, y, vsz, fills)) < 2 or not _own_pad_ok(pds, x, y, vsz, in_pad):
            continue
        got.append((round(x, 3), round(y, 3), d))
        have.append((x, y, vdr))
        near.append((x, y, vdr))
    return got


def sparse(b, net, target, keep, box, vias, step=0.25, dead=frozenset()):
    """greedy stitching of NET's outer-layer pour: the sample farthest from any NET via or
    plated hole in its own F/B fill fragment first, a legal spot near it (legal_spots, the
    `stitch` search, each via size in turn), repeat until every sample is within `target`
    mm or has no legal spot within 2.5 mm. Returns ([(x, y, size, drill, worst d, at)],
    distances before, after, samples given up on)"""
    out = lambda x, y: any(k[0] <= x <= k[2] and k[1] <= y <= k[3] for k in keep) or \
        (box and not (box[0] <= x <= box[2] and box[1] <= y <= box[3]))           # noqa: E731
    tg = max((z.thermal_gap for z in b.zones if z[0] == net), default=0.5)
    anch = [(v['x'], v['y'], 0.0) for v in b.vias if v['net'] == net and id(v) not in dead] + \
        [(p['x'], p['y'], max(p['sx'], p['sy']) / 2 + tg) for f in b.placed() for p in f.pads
         if p['drill'] and p['net'] == net]          # a plated hole: its relief keeps its centre out of the fill
    frags = []                                       # [(fill index, [sample pts])]
    for fl in b.fills:
        if fl['layer'] in b.outer and fl['net'] == net and fl['area'] >= 4 and id(fl) not in dead:
            x0, y0, x1, y1 = fl['bbox']
            idx = PolyIndex(fl['pts'])
            pts = [p for p in ((x0 + i * step, y0 + j * step) for i in range(int((x1 - x0) / step) + 1)
                               for j in range(int((y1 - y0) / step) + 1)) if not out(*p) and p in idx]
            if pts:
                frags.append((idx, pts))
    mine = lambda idx, c: (c[0], c[1]) in idx       # noqa: E731
    dist = {}                                        # (fragment, sample) -> nearest own anchor, mm
    for k, (idx, pts) in enumerate(frags):
        G = defaultdict(list)                        # its anchors in 1 mm buckets: ring search
        for x, y, tol in anch:
            if idx.touches((x, y), tol) if tol else (x, y) in idx:
                G[(math.floor(x), math.floor(y))].append((x, y))
        for p in pts:
            gx, gy, best = math.floor(p[0]), math.floor(p[1]), 99.0
            for r in range(0, 40):
                if best <= r - 1:
                    break
                for cx in range(gx - r, gx + r + 1):
                    for cy in (range(gy - r, gy + r + 1) if cx in (gx - r, gx + r) else (gy - r, gy + r)):
                        for c in G.get((cx, cy), ()):
                            best = min(best, math.dist(p, c))
            dist[k, p] = best
    before = sorted(dist.values(), reverse=True)
    fills, have, gone, made = net_fills(b, net, dead), [(v['x'], v['y'], v['drill']) for v in b.vias], set(), []
    while True:
        w = max(((d, k, p) for (k, p), d in dist.items() if d > target and (k, p) not in gone), default=None)
        if not w:
            break
        d, k, p = w
        got = None
        for vsz, vdr in vias:
            cand = [c for c in legal_spots(b, net, p[0], p[1], 0.0, min(2.5, d - target / 2), vsz, vdr, 6, False,
                                           fills, list(have), dead) if not out(c[0], c[1]) and mine(frags[k][0], c)]
            if cand:
                got = (cand[0][0], cand[0][1], vsz, vdr)
                break
        if not got:                                  # nothing legal near: give up on this neighbourhood
            gone |= {(kk, qq) for (kk, qq) in dist if kk == k and math.dist(qq, p) <= 0.75}
            continue
        have.append((got[0], got[1], got[3]))
        made.append(got + (d, p))
        for kk, (idx, pts) in enumerate(frags):
            if mine(idx, got[:2]):
                for qq in pts:
                    dist[kk, qq] = min(dist[kk, qq], math.dist(qq, got[:2]))
    return made, before, sorted(dist.values(), reverse=True), gone


def c_stitch(b, a):
    from kpcb_board import _resolve_net
    if not a.args:
        print("stitch NET [X,Y] [-r R] [--box X0,Y0,X1,Y1] [--n N] [--via S/D] [--in-pad] [-o OUT]"); return 1
    net, cand = _resolve_net(b, a.args[0])
    if not net:
        print(f"{a.args[0]}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); return 1
    cls = _netclass(b, net)
    vsz, vdr = class_via(cls)
    if a.via:
        vsz, vdr = (float(x) for x in a.via.split('/'))
    from kpcb_route import _planes
    from kpcb_copper import dead_copper
    dead = {id(o) for d in (dead_copper(b, net) if all(n_ != net for n_, _l in _planes(b)) else ())
            for o in [t for t, _L, k in d['tracks'] if k is None] + d['vias'] + d['fills']}   # never stitch dead copper
    n = a.n or (3 if len(a.args) > 1 else 2)
    if len(a.args) > 1:
        try:
            ax, ay = (float(v) for v in a.args[1].split(','))
        except ValueError:
            print(f"{a.args[1]}: want X,Y"); return 1
        R = a.radius if a.radius else 1.5
        anchors = [(ax, ay, 0.0, R, 'near')]
    else:
        box = _boxes(a.box)[0] if a.box else None
        anchors = [(v['x'], v['y'], 0.6, 1.6, 'via') for v in b.vias if v['net'] == net and id(v) not in dead and
                   (not box or box[0] <= v['x'] <= box[2] and box[1] <= v['y'] <= box[3])]
        if not anchors:
            print(f"{unesc_disp(net)}: no via of the net{' in --box' if a.box else ''} to stitch beside "
                  f"(give X,Y for a spot near a point)"); return 1
    if a.sparse:
        sizes = [(vsz, vdr)] + ([(0.5, 0.2)] if (vsz, vdr) != (0.5, 0.2) and not a.via else [])
        made, before, after, gone = sparse(b, net, a.sparse, _boxes(a.keep), _boxes(a.box)[0] if a.box else None, sizes,
                                           dead=dead)
        if not before:
            print(f"stitch --sparse: no saved outer-layer fill of {unesc_disp(net)} >= 4 mm2 (refill first)"); return 1
        print(f"stitch {unesc_disp(net)} --sparse {a.sparse:g}: outer pour sampled every 0.25 mm, worst point first, "
              f"vias {' then '.join(f'{x:g}/{y:g}' for x, y in sizes)}" + (f", --keep {a.keep}" if a.keep else ''))
        for x, y, s_, d_, w, p in made[:a.max * 4]:
            print(f"  via {s_:g}/{d_:g} at {x:.3f},{y:.3f}  for {w:.1f} mm at {p[0]:.2f},{p[1]:.2f}")
        if len(made) > a.max * 4:
            print(f"  ... +{len(made) - a.max * 4} more")
        area = lambda ds, t: sum(1 for v in ds if v > t) * 0.0625   # noqa: E731
        for t in sorted({a.sparse, 4.0, 5.0, 8.0}):
            print(f"  pour > {t:g} mm from a via: {area(before, t):6.1f} -> {area(after, t):6.1f} mm2")
        far = lambda ds: max((d for d in ds if d < 99), default=0.0)       # noqa: E731
        lone = sum(1 for d in after if d >= 99) * 0.0625
        print(f"  worst {far(before):.1f} -> {far(after):.1f} mm; {len(made)} via(s); {len(gone)} sample(s) with no legal "
              f"spot within 2.5 mm" + (f"; {lone:.1f} mm2 in fragments with no via at all" if lone else ''))
        if not made or not a.out:
            print("  (dry run: -o OUT writes them, free)" if made else ''); return 0 if made else 2
        t = PcbText(b.path)
        for x, y, s_, d_, *_r in made:
            t.via(net, x, y, s_, d_, [b.copper[0], b.copper[-1]], free=True)
        t.save(a.out)
        print(f"wrote {len(made)} via(s) -> {a.out}\n  verify: kdrc.py {a.out} drc --refill-save --baseline BEFORE.json")
        return 0
    fills = net_fills(b, net, dead)
    if not fills:
        print(f"(no saved fill of {unesc_disp(net)}: only tracks and pads count as its copper; refill first "
              f"if it has zones)")
    chosen, rows = [], []
    have = [(v['x'], v['y'], v['drill']) for v in b.vias]
    for ax, ay, r0, r1, kind in anchors:
        got = legal_spots(b, net, ax, ay, r0, r1, vsz, vdr, n, a.in_pad, fills, have, dead)
        chosen += got
        rows.append((ax, ay, kind, got))
    print(f"stitch {unesc_disp(net)}: via {vsz:g}/{vdr:g} mm, legal in the router's model and on the net's copper "
          f"on >= 2 layers" + (", in big same-net pads allowed" if a.in_pad else ''))
    for ax, ay, kind, got in rows[:a.max * 4]:
        print(f"  {'near' if kind == 'near' else 'beside via'} {ax:.2f},{ay:.2f}: "
              + (', '.join(f"{x:.2f},{y:.2f} ({d:.2f} mm)" for x, y, d in got) if got else 'no legal spot'))
    if len(rows) > a.max * 4:
        print(f"  ... +{len(rows) - a.max * 4} more anchor(s)")
    if not chosen:
        return 2
    if not a.out:
        print(f"  (dry run: {len(chosen)} via(s); -o OUT writes them, free)"); return 0
    t = PcbText(b.path)
    for x, y, _d in chosen:
        t.via(net, x, y, vsz, vdr, [b.copper[0], b.copper[-1]], free=True)
    t.save(a.out)
    print(f"wrote {len(chosen)} via(s) -> {a.out}\n  verify: kdrc.py {a.out} drc --refill-save --baseline BEFORE.json")
    return 0


def c_vscan(b, a):
    """`vscan NET X0,Y0,X1,Y1 [--via S/D] [--width W] [--layers L]`: the router's own model of
    a box, as ASCII at 0.1 mm: where a through via of NET is legal and which layers a track
    of width W may cross each cell on. What `route` sees when it reports a pin boxed in."""
    from kpcb_board import _resolve_net
    if len(a.args) < 2:
        print("vscan NET X0,Y0,X1,Y1 [--via S/D] [--width W] [--layers L]"); return 1
    net, cand = _resolve_net(b, a.args[0])
    if not net:
        print(f"{a.args[0]}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); return 1
    box = _boxes(a.args[1])[0]
    vsz, vdr = class_via(_netclass(b, net))
    if a.via:
        vsz, vdr = (float(x) for x in a.via.split('/'))
    w = a.width or 0.15
    ly = a.layers.split(',')[0] if a.layers else ''
    if ly and ly not in b.copper:
        print(f"{ly}: not a copper layer ({', '.join(b.copper)})"); return 1
    res = 0.1
    g = Grid(b, net, box, res, w, vsz, vdr, b.copper)
    tag = {l: (l[0] if l[0] in 'FB' else l[2]) for l in b.copper}           # F, 1, 2, B
    print(f"vscan {unesc_disp(net)}  {box[0]:g},{box[1]:g} .. {box[2]:g},{box[3]:g}  via {vsz:g}/{vdr:g}  "
          f"track w {w:g}  cell {res:g} mm (route's Grid: netclass, pair and type rules, edge, keepouts)")
    print("  " + (f"{ly}: # blocked, . free for the track, V a legal via" if ly else
                  "V legal via, v legal via in a power-island pour (a cost), # no layer free, "
                  f"+ 2+ layers free, else the one free layer ({' '.join(f'{c}={l}' for l, c in tag.items())})"))
    print(' ' * 8 + ''.join('|' if abs((g.x0 + i * res) - round(g.x0 + i * res)) < 1e-6 else ' ' for i in range(g.W))
          + f"   (| every 1 mm from x {math.ceil(g.x0):g})")
    for j in range(g.H):
        row = ''
        for i in range(g.W):
            k = j * g.W + i
            if ly:
                row += '#' if g.blk[ly][k] else 'V' if not g.vblk[k] else '.'
            elif not g.vblk[k]:
                row += 'v' if g.vpour[k] else 'V'
            else:
                free = [l for l in b.copper if not g.blk[l][k]]
                row += '#' if not free else '+' if len(free) > 1 else tag[free[0]]
        print(f"{g.y0 + j * res:7.2f} {row}")
    return 0
