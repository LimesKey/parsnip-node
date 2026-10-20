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
from kpcb_board import PolyIndex, bbox, grow, pad_box, pt_seg_dist
from kpcb_copper import _local
from kpcb_edit import PcbText, _need_out
from kpcb_move import cu_layers
from kpcb_route import ONES, Grid, _iv, _kos, _netclass, _pad_shape, _trace, _area

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
        """a polygon's inside (even-odd), grown by painting its edges as capsules"""
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
                self.paint(('cap', u, v, 0.0), grow_by, val)


def _outlines(r):
    """zone outlines [(x, y)] for the set cells: diagonal-only touches closed, every hole
    slit open to the outside (one ring per piece, as a KiCad zone outline is)"""
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
    for k in range(W * H):                           # a hole: slit it up to the outside
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
            out.append([(round(r.x0 + (ci - 0.5) * res, 4), round(r.y0 + (cj - 0.5) * res, 4)) for ci, cj in L])
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
        print("fatten NET... [--layers L,..] [--width W] [--minw 0.5] [--box X0,Y0,X1,Y1] [--keep X0,Y0,X1,Y1;...] -o OUT")
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
    made, rc = [], 0
    for net in nets:
        cls = _netclass(b, net)
        w = a.width or max(2 * (cls.get('track_width') or 0.5), 2.0)
        c = cls.get('clearance') or 0.2
        gnd = [(p, f) for f in b.placed() if any(q['net'] == net for q in f.pads) for p in f.pads if p['net'] == 'GND']
        for ly in layers:
            segs = [s for s in b.tracks if s['net'] == net and s['layer'] == ly and s['w'] >= minw - 1e-9 and
                    (not box or any(box[0] <= q[0] <= box[2] and box[1] <= q[1] <= box[3] for q in (s['a'], s['b'])))]
            if not segs:
                continue
            r = Raster(grow(bbox([q for s in segs for q in (s['a'], s['b'])]), w / 2 + 0.5))
            for s in segs:
                r.paint(('cap', s['a'], s['b'], 0.0), w / 2)
            for z in b.zones:                        # other nets' zones and pour keepouts stay theirs
                zn, zl = z
                if z.outline and (ly in zl or '*.Cu' in zl) and (z.keepout or zn not in (net, 'GND', '')):
                    r.poly(z.outline, 0.3, 0)
            for lys, idx, _nv, nopour, *_x in kos:
                if nopour and ly in lys:
                    r.poly(idx.pts, 0.3, 0)
            if ly in ref:
                for s in rf:
                    r.paint(('cap', s['a'], s['b'], s['w'] / 2), 1.5, 0)
            for p, f in gnd:                         # its own parts' ground stays reachable: room for
                if p['drill'] or ly in cu_layers(p['layers'], b.copper) or ly not in b.outer:   # the GND pour's
                    r.paint(_pad_shape(p, pad_box(p)), 1.0, 0)  # 0.5 thermal gap + a spoke + clearance
            for kb in keep:
                r.poly([(kb[0], kb[1]), (kb[2], kb[1]), (kb[2], kb[3]), (kb[0], kb[3])], 0.0, 0)
            for n2, l2, ring in made:
                if l2 == ly and n2 != net:
                    r.poly(ring, 0.3, 0)
            rings = _outlines(r)
            area = 0.0
            for ring in rings:
                t.zone(net, ly, ring, prio, c, 0.25, name=f"fatten {net}")
                made.append((net, ly, ring))
                area += _area(ring)
                prio += 1
            print(f"fatten {unesc_disp(net)} {ly}: {len(segs)} track(s) >= {minw:g} mm -> "
                  f"{len(rings)} zone(s), a {w:g} mm band, {area:.1f} mm2 outline, {sum(map(len, rings))} corners")
    if not made:
        print("fatten: no tracks to widen (check --minw, --layers, --box)"); return 1
    t.save(a.out)
    print(f"wrote {len(made)} zone(s) (priority {prio - len(made)}..{prio - 1}) -> {a.out}\n"
          f"  verify: kdrc.py {a.out} drc --refill-save --unconnected --baseline BEFORE.json, then\n"
          f"  `airwires --since BEFORE` (a band can cut a plane region off) and `ampacity --from A --to B --fills`")
    return rc


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
    """a via disk overlapping a same-net SMD pad: only wholly inside a big one, with --in-pad"""
    for p in pds:
        if p['drill']:
            continue
        u, v = _local(p, (x, y))
        hx, hy = p['sx'] / 2, p['sy'] / 2
        if math.hypot(max(abs(u) - hx, 0), max(abs(v) - hy, 0)) >= vsz / 2:
            continue                                 # clear of this pad
        if not in_pad or p['sx'] * p['sy'] < 3.0 or abs(u) > hx - vsz / 2 or abs(v) > hy - vsz / 2:
            return False
    return True


def net_fills(b, net):
    fills = defaultdict(list)
    for fl in b.fills:
        if fl['net'] == net:
            fills[fl['layer']].append(PolyIndex(fl['pts']))
    return fills


def legal_spots(b, net, ax, ay, r0, r1, vsz, vdr, n, in_pad, fills, have):
    """up to n via spots r0..r1 from ax,ay, nearest first: legal in the router's model, clear
    of every drill in `have` [(x, y, drill)] (appended to), and on the net's copper on >= 2
    layers. [(x, y, distance)]"""
    win = (ax - r1 - 1, ay - r1 - 1, ax + r1 + 1, ay + r1 + 1)
    g = Grid(b, net, win, RES, 0.2, vsz, vdr, b.copper, ownpads=not in_pad)
    inw = lambda bb: bb[2] >= win[0] and bb[0] <= win[2] and bb[3] >= win[1] and bb[1] <= win[3]   # noqa: E731
    trk = [s for s in b.tracks if s['net'] == net and inw(bbox([s['a'], s['b']]))]
    pds = [p for f in b.placed() for p in f.pads if p['net'] == net and inw(pad_box(p))]
    got, cells = [], []
    for j in range(g.H):
        for i in range(g.W):
            x, y = g.x0 + i * RES, g.y0 + j * RES
            d = math.hypot(x - ax, y - ay)
            if r0 - 1e-9 <= d <= r1 and not g.vblk[j * g.W + i] and not g.vpour[j * g.W + i]:
                cells.append((d, x, y))
    for d, x, y in sorted(cells):
        if len(got) >= n:
            break
        if any(math.dist((x, y), (hx, hy)) < max(vsz + 0.05, (vdr + hd) / 2 + 0.25) for hx, hy, hd in have):
            continue
        if len(_on_net(b, trk, pds, x, y, vsz, fills)) < 2 or not _own_pad_ok(pds, x, y, vsz, in_pad):
            continue
        got.append((round(x, 3), round(y, 3), d))
        have.append((x, y, vdr))
    return got


def c_stitch(b, a):
    from kpcb_board import _resolve_net
    if not a.args:
        print("stitch NET [X,Y] [-r R] [--box X0,Y0,X1,Y1] [--n N] [--via S/D] [--in-pad] [-o OUT]"); return 1
    net, cand = _resolve_net(b, a.args[0])
    if not net:
        print(f"{a.args[0]}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else '')); return 1
    cls = _netclass(b, net)
    vsz, vdr = (cls.get('via_diameter') or 0.6), (cls.get('via_drill') or 0.3)
    if (vsz - vdr) / 2 < 0.15 - 1e-6:
        vsz, vdr = 0.5, 0.2
    if a.via:
        vsz, vdr = (float(x) for x in a.via.split('/'))
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
        anchors = [(v['x'], v['y'], 0.6, 1.6, 'via') for v in b.vias if v['net'] == net and
                   (not box or box[0] <= v['x'] <= box[2] and box[1] <= v['y'] <= box[3])]
        if not anchors:
            print(f"{unesc_disp(net)}: no via of the net{' in --box' if a.box else ''} to stitch beside "
                  f"(give X,Y for a spot near a point)"); return 1
    fills = net_fills(b, net)
    if not fills:
        print(f"(no saved fill of {unesc_disp(net)}: only tracks and pads count as its copper; refill first "
              f"if it has zones)")
    chosen, rows = [], []
    have = [(v['x'], v['y'], v['drill']) for v in b.vias]
    for ax, ay, r0, r1, kind in anchors:
        got = legal_spots(b, net, ax, ay, r0, r1, vsz, vdr, n, a.in_pad, fills, have)
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
    cls = _netclass(b, net)
    vsz, vdr = (cls.get('via_diameter') or 0.6), (cls.get('via_drill') or 0.3)
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
