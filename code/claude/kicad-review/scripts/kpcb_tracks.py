"""kpcb.py `tracks`, `widen` and `loops`: the quality of routed copper, and two clean-ups.

`tracks [--kind K] [--view OUT.png]` is a census of what a reviewer sees: corners of 46-90
deg and acute ones at nodes of two same-net tracks, off-45 segments, width steps mid-run,
segments under 0.05 mm, dead-end traces that stop at a pad edge instead of its centre
(`smooth --pads`). Each is dropped when it is hidden under the UNION of other same-net
copper (pads, vias, wider tracks, fills). Listed rows say why `smooth` left them: on a pad
or via (an anchor), a width step (`smooth --corners` takes it), a T, a frozen leg (arc,
locked), boxed in (no clear cut at the real clearance), or `fix` (a pass would take it).

`widen [NET...] [--width W] -o OUT` grows under-width tracks to the widest legal step up to
their target: a power netclass's track width (PWR_*), the net's class width for GND, W
(0.2) for signals. Clearance as `smooth`: route's typed Grid, Exact where it refuses. Power
classes >= 1 mm go per segment (resistance first) and skip legs under 0.5 mm (sense); the
rest take one width per run so a run does not alternate. Capped at an SMD pad's narrow side;
never a new same-net contact (Kelvin legs keep one); skipped: tracks inside a same-net
fill, switch nodes (EMI area), inner tracks within 2 mm of RF traces (their plane), RF/USB/
diff nets. Sequential: each accepted width is in the model before the next is priced.

`loops [NET...] [-o OUT]` finds redundant track chains on signal nets: greedily, the
longest chain whose removal - with ripup's prune of what that leaves dangling and of vias
joining < 2 layers - leaves the net's pad partition unchanged, both as KiCad joins copper
and on STRICT joins (an end cap wholly inside the pad or via, a real T): a pad entry whose
neighbour's cap merely grazes the pad is not redundant, and a pad already hanging by a
graze is not a licence to remove its whole path. Power, GND and poured nets
are skipped (parallel copper is lower R). -o deletes them and their teardrops; verify with
`kdrc.py OUT drc --refill-save --baseline BEFORE.json` (0 new unconnected)."""
import math, re
from collections import defaultdict, Counter
from kcommon import unesc_disp, rail_voltage
from kpcb_board import PolyIndex, pt_seg_dist, seg_seg_dist
from kpcb_route import Grid, _netclass, piece_parts
from kpcb_move import via_layers, is_arc
from kpcb_edit import PcbText, tear_edits, num, prune, _rip_out
from kpcb_smooth import (Index, Exact, chains, covered, cut_corner, skipped, in_pad, on_layer, octi, ang, turn,
                         q, RES, pad_ends, no_project)


# ---------------------------------------------------------------- tracks (census)

def census_rows(b, skip):
    """(Counter, rows): rows (kind, net, layer, xy, detail, why, (node, i1, i2) | None)"""
    ix = Index(b)
    c, rows = Counter(), []
    far = lambda t, n: t['b'] if q(t['a']) == n else t['a']    # noqa: E731
    for (net, ly, n), lst in ix.ends.items():
        if net in skip or len(lst) != 2:
            continue
        t1, t2 = b.tracks[lst[0]], b.tracks[lst[1]]
        if t1['len'] < 1e-4 or t2['len'] < 1e-4:
            continue
        w = max(t1['w'], t2['w'])
        onp, onv = ix.on_pad(net, ly, n, w / 2), ix.on_via(net, ly, n, w / 2)
        th = turn(ang(far(t1, n), n), ang(n, far(t2, n)))
        k = None if th < 46 else '90' if th < 91 else 'acute'
        if k and covered(b, ix, net, ly, n, w, set(lst)):
            c['hidden ' + k] += 1
            k = None
        tee = any(i not in lst and t['net'] == net and t['layer'] == ly and pt_seg_dist(n, t['a'], t['b']) <= t['w'] / 2
                  for i, t in enumerate(b.tracks))
        why = 'pad' if onp else 'via' if onv else 'T' if tee else \
            'frozen' if any(is_arc(t) or t['locked'] for t in (t1, t2)) else \
            'width step' if abs(t1['w'] - t2['w']) > 1e-3 else ''
        if k:
            c[k] += 1
            rows.append((k, net, ly, n, f"{th:.0f} deg", why, (n, lst[0], lst[1])))
        if abs(t1['w'] - t2['w']) > 1e-3 and not onp and not onv:
            c['neck'] += 1
            rows.append(('neck', net, ly, n, f"{t1['w']:g} -> {t2['w']:g} mm", 'T' if tee else '', None))
    for i, t in enumerate(b.tracks):
        if not t['net'] or t['net'] in skip or t['len'] < 1e-4:
            continue
        if is_arc(t):
            c['arc'] += 1
            continue
        c['segs'] += 1
        c['mm'] += t['len']
        if not octi(t['a'], t['b']):
            if covered(b, ix, t['net'], t['layer'], t['mid'], t['w'], {i}):
                c['hidden off45'] += 1
            else:
                c['off45'] += 1
                pe = any(ix.on_pad(t['net'], t['layer'], e) for e in (t['a'], t['b']))
                ve = any(ix.on_via(t['net'], t['layer'], e, t['w'] / 2) for e in (t['a'], t['b']))
                rows.append(('off45', t['net'], t['layer'], t['mid'], f"{t['len']:.2f} mm at {ang(t['a'], t['b']) % 180:.0f} deg",
                             'pad end (smooth --square)' if pe else 'via end' if ve else '', None))
        if t['len'] < 0.05:
            if covered(b, ix, t['net'], t['layer'], t['mid'], t['w'], {i}):
                c['hidden stub'] += 1
            else:
                c['stub'] += 1
                rows.append(('stub', t['net'], t['layer'], t['mid'], f"{t['len'] * 1000:.0f} um", '', None))
    for i, E, lab, p in pad_ends(b, ix, skip):
        c['pad'] += 1
        rows.append(('pad', b.tracks[i]['net'], b.tracks[i]['layer'], E,
                     f"{math.dist(E, (p['x'], p['y'])):.2f} mm off {lab}", 'fix: smooth --pads', None))
    from kpcb_jogs import find
    for jog in find(b, skip, every=True):
        c['jog'] += 1
        rows.append(('jog', jog[0], jog[1], jog[4][jog[5]], f"{jog[6]:.2f} mm sideways", '', jog[:6]))
    from kpcb_dense import detours
    for _x, net, L, M, V, n, ly in detours(b, skip):
        c['detour'] += 1
        at = next(((p['x'], p['y']) for f in b.placed() for p in f.pads if p['net'] == net), (0.0, 0.0))
        rows.append(('detour', net, ly, at, f"{L:.1f} vs MST {M:.1f} mm, {V} via", 'reroute ' + unesc_disp(net.split('/')[-1]), None))
    return c, rows


KINDS = ('acute', '90', 'off45', 'neck', 'stub', 'pad', 'jog', 'detour')


def c_tracks(b, a):
    skip = set() if a.all else skipped(b)
    c, rows = census_rows(b, skip)
    kinds = [k for k in KINDS if not a.kind or k in a.kind.split(',')]
    print(f"tracks: {c['segs']} segments, {c['mm']:.1f} mm, {c['arc']} arc(s)"
          + (f"; {len(skip)} RF/USB/diff-pair net(s) skipped (--all includes them)" if skip else ''))
    label = {'acute': 'corners, acute', '90': 'corners 46-90 deg', 'off45': 'off-45 segments',
             'neck': 'width steps mid-run', 'stub': 'segments < 0.05 mm', 'pad': 'ends at a pad edge',
             'jog': 'lane jogs 0.05-1 mm', 'detour': 'detours (> 1.5x MST)'}
    for k in KINDS:
        print(f"  {label[k]:<22} {c[k]:>4} visible" + (f"  ({c['hidden ' + k]} hidden under same-net copper)"
                                                      if 'hidden ' + k in c else ''))
    per = Counter((r[2], r[0]) for r in rows if r[0] in ('acute', '90', 'off45'))
    lays = sorted({ly for ly, _k in per}, key=lambda ly: b.copper.index(ly) if ly in b.copper else 99)
    if lays:
        print("  per layer: " + ' | '.join(f"{ly} {per[(ly, 'acute')]} acute {per[(ly, '90')]} x90 "
                                           f"{per[(ly, 'off45')]} off45" for ly in lays))
    shown = []
    for k in kinds:
        order = {'': 0, 'width step': 1, 'T': 2, 'frozen': 3}
        rs = sorted((r for r in rows if r[0] == k), key=lambda r: order.get(r[5], 4))   # what a pass can take first
        if not rs:
            continue
        print(f"\n{label[k]} ({len(rs)}):")
        for r in rs[:a.max]:
            why = r[5]
            if k == 'jog':                           # priced only when shown: a Clear per jog
                from kpcb_jogs import verdict
                why = verdict(b, *r[6], skip=skip)
            elif not why and r[6]:                   # a free corner: can the corner pass cut it?
                why = 'fix: smooth --corners' if cut_corner(b, r[1], r[2], *r[6]) else 'boxed in at the real clearance'
            elif not why and k == 'off45':
                why = 'smooth (or boxed in)'
            print(f"  {unesc_disp(r[1].split('/')[-1]):<24} {r[2]:<7} {r[3][0]:8.3f},{r[3][1]:<8.3f} {r[4]:<20} {why}")
            shown.append(r)
        if len(rs) > a.max:
            print(f"  ... +{len(rs) - a.max} more - raise --max")
    if a.view and shown:
        from kpcb_view import sites_png
        out = sites_png(b, [(r[2], r[3], f"{r[0]} {unesc_disp(r[1].split('/')[-1])}") for r in shown], a.view,
                        a.radius if a.radius != 5.0 else 0.8, a.px if a.px != 20 else 120)
        print(f"\n{out}  ({len(shown)} panel(s), tracks/pads/vias only, the listed rows in order)")
    return 0


# ---------------------------------------------------------------- widen

def c_widen(b, a):
    if no_project(b, 'widen'):
        return 1
    skip = skipped(b) | b.sw_nets()
    want = set(a.args) or None
    ix = Index(b)
    rf = [t for t in b.tracks if t['net'] in skipped(b) and t['layer'] in b.outer and
          'RF' in (b.netclass(t['net']) or '').upper()]
    fills = defaultdict(list)
    for fl in b.fills:
        fills[(fl['net'], fl['layer'])].append(PolyIndex(fl['pts']))
    bylay = defaultdict(list)
    for i, t in enumerate(b.tracks):
        bylay[(t['net'], t['layer'])].append(i)
    sig = a.width or 0.2

    def cls_of(net):
        nc = b.netclass(net) or 'Default'
        return nc, _netclass(b, net)

    def per_segment(t):
        nc, cl = cls_of(t['net'])
        return 'PWR' in nc.upper() and (cl.get('track_width') or 0) >= 1.0

    def target(t):
        nc, cl = cls_of(t['net'])
        if 'PWR' in nc.upper():
            if per_segment(t) and t['w'] < 0.5:
                return None                          # a sense / Kelvin leg
            return cl.get('track_width')
        return (cl.get('track_width') or 0.3) if t['net'] == 'GND' else sig

    def samples(t, step):
        n = max(1, math.ceil(t['len'] / step))
        return [(t['a'][0] + (t['b'][0] - t['a'][0]) * k / n, t['a'][1] + (t['b'][1] - t['a'][1]) * k / n) for k in range(n + 1)]

    def cap(t):
        """the narrow side of any SMD pad an end sits in"""
        return min([min(p['sx'], p['sy']) for p in ix.pads[t['net']] if on_layer(p, t['layer']) and not p['drill']
                    and (in_pad(p, t['a']) or in_pad(p, t['b']))], default=99.0)

    def in_fill(t):
        pis = fills.get((t['net'], t['layer']), ())
        return bool(pis) and all(any(pi.touches(x, 0) for pi in pis) for x in samples(t, 0.1))

    def contacts(i, t, w):
        """same-net items (not at t's ends) the capsule of width w touches"""
        out = set()
        if t['net'] == 'GND':
            return out
        ends = (t['a'], t['b'])
        for p in ix.pads[t['net']]:
            if on_layer(p, t['layer']) and not any(in_pad(p, e, 0.01) for e in ends) and \
                    any(in_pad(p, x, w / 2) for x in samples(t, 0.05)):
                out.add(('pad', id(p)))
        for v in ix.vias[t['net']]:
            c = (v['x'], v['y'])
            if t['layer'] in via_layers(v, b.copper) and all(math.dist(c, e) > v['size'] / 2 + 0.01 for e in ends) \
                    and pt_seg_dist(c, t['a'], t['b']) < (w + v['size']) / 2:
                out.add(('via', id(v)))
        for j in bylay[(t['net'], t['layer'])]:
            u = b.tracks[j]
            if j == i or any(pt_seg_dist(e, u['a'], u['b']) <= u['w'] / 2 + 0.01 for e in ends) or \
                    any(pt_seg_dist(e, t['a'], t['b']) <= t['w'] / 2 + 0.01 for e in (u['a'], u['b'])):
                continue                             # joined at an end or a T: one copper already
            if seg_seg_dist(t['a'], t['b'], u['a'], u['b']) < (w + u['w']) / 2:
                out.add(('trk', j))
        for k, pi in enumerate(fills.get((t['net'], t['layer']), ())):
            if any(pi.touches(x, w / 2) for x in samples(t, 0.05)):
                out.add(('fill', k))
        return out

    def clear(t, w):
        box = (min(t['a'][0], t['b'][0]) - 0.6, min(t['a'][1], t['b'][1]) - 0.6,
               max(t['a'][0], t['b'][0]) + 0.6, max(t['a'][1], t['b'][1]) + 0.6)
        g = Grid(b, t['net'], box, RES, w, 0.6, 0.3, [t['layer']], extra_clr=RES * 0.8)
        blk = g.blk[t['layer']]
        for x in samples(t, RES / 2):
            i, j = round((x[0] - g.x0) / g.res), round((x[1] - g.y0) / g.res)
            if not (0 <= i < g.W and 0 <= j < g.H) or blk[j * g.W + i]:
                return Exact(b, t['net'], t['layer'], w, (box[0] - 1, box[1] - 1, box[2] + 1, box[3] + 1)).seg(t['a'], t['b'])
        return True

    def scope(t):
        """a track widen may touch at all: named (or all) nets, not skipped/locked/arc, not an
        inner track under an RF trace's plane"""
        return not (not t['net'] or t['net'] in skip or (want and t['net'] not in want and t['net'].split('/')[-1]
                                                         not in want) or t['len'] < 1e-3 or t['locked'] or is_arc(t)
                    or t['layer'] not in b.outer and any(seg_seg_dist(t['a'], t['b'], r['a'], r['b']) < 2.0 for r in rf))

    def plan(t):
        """widths to try, widest first, or None"""
        if not scope(t):
            return None
        tg = target(t)
        if not tg or t['w'] >= tg - 1e-3:
            return None
        tg = min(tg, cap(t))
        if tg < t['w'] + 0.049 or in_fill(t):
            return None
        step, out, w = (0.1 if tg >= 0.6 else 0.05), [], tg
        while w >= t['w'] + 0.049:
            out.append(round(w, 3))
            w -= step
        return out

    def ok(i, t, w):
        return not (contacts(i, t, w) - contacts(i, t, t['w'])) and clear(t, w)

    done, tally = [], Counter()
    def accept(i, t, w):
        done.append((i, t['w'], w))
        tally[b.netclass(t['net']) or 'Default'] += t['len'] * (w - t['w'])
        t['w'] = w

    for i, t in enumerate(b.tracks):                 # power classes >= 1 mm: per segment
        if t['net'] and per_segment(t):
            for w in plan(t) or ():
                if ok(i, t, w):
                    accept(i, t, w)
                    break
    def runs_of(power):
        """tracks joined at nodes of exactly two same-net tracks off pads/vias, per-segment
        power classes (power) or the rest"""
        par = {}
        def find(x):
            while par.setdefault(x, x) != x:
                x = par[x]
            return x
        for (net, ly, n), lst in ix.ends.items():
            if len(lst) == 2 and all(per_segment(b.tracks[k]) == power for k in lst) and \
                    not ix.on_via(net, ly, n) and not ix.on_pad(net, ly, n):
                par[find(lst[0])] = find(lst[1])
        runs = defaultdict(list)
        for i, t in enumerate(b.tracks):
            if t['net'] and per_segment(t) == power:
                runs[find(i)].append(i)
        return runs.values()

    steps = None
    if a.uniform_power:                              # a power run at its widest where that is clear
        for run in runs_of(True):
            mx = max(b.tracks[i]['w'] for i in run)
            for i in run:
                t = b.tracks[i]
                if t['w'] >= mx - 1e-3 or t['w'] < 0.5 or not scope(t) or in_fill(t):
                    continue                         # at max, a sense leg, out of scope, or inside its own fill
                w = min(mx, cap(t))
                if w > t['w'] + 0.049 and ok(i, t, w):
                    accept(i, t, w)
        steps = sum(1 for (net, ly, n), lst in ix.ends.items() if len(lst) == 2
                    and all(per_segment(b.tracks[k]) for k in lst) and not ix.on_via(net, ly, n)
                    and not ix.on_pad(net, ly, n) and abs(b.tracks[lst[0]]['w'] - b.tracks[lst[1]]['w']) > 1e-3)
    # the rest, one width per run
    pad_end = lambda t: t['len'] < 0.6 and (ix.on_pad(t['net'], t['layer'], t['a']) or ix.on_pad(t['net'], t['layer'], t['b']))  # noqa: E731
    for run in runs_of(False):
        ps = {i: plan(b.tracks[i]) for i in run}
        if not any(ps.values()):
            continue
        core = [i for i in run if not pad_end(b.tracks[i])] or run
        tops = [ps[i][0] for i in core if ps[i]]
        if not tops:
            continue
        lo = min(b.tracks[i]['w'] for i in core)
        for w in sorted({w for i in core if ps[i] for w in ps[i] if w > lo + 0.049}, reverse=True):
            if w > min(tops) + 1e-3:
                continue
            need = [i for i in core if b.tracks[i]['w'] < w - 1e-3 and not (in_fill(b.tracks[i]) and not ps[i])]
            if any(not ps[i] or w not in ps[i] for i in need):
                continue
            if all(ok(i, b.tracks[i], w) for i in need):
                for i in need:
                    accept(i, b.tracks[i], w)
                for i in run:                        # pad entries taper only if they must
                    if i not in core:
                        for w2 in [x for x in (ps[i] or ()) if x <= w + 1e-3]:
                            if ok(i, b.tracks[i], w2):
                                accept(i, b.tracks[i], w2)
                                break
                break
    print(f"widen: {len(done)} track(s); copper added (mm2) by class: "
          + (', '.join(f'{k} {v:.1f}' for k, v in sorted(tally.items())) or 'none')
          + (f"; {steps} width step(s) left mid-run in power classes (boxed in)" if steps is not None else ''))
    for i, w0, w1 in sorted(done, key=lambda d: -b.tracks[d[0]]['len'] * (d[2] - d[1]))[:a.max]:
        t = b.tracks[i]
        print(f"  {unesc_disp(t['net'].split('/')[-1]):<26} {t['layer']:<7} {w0:g} -> {w1:g}  {t['len']:.2f} mm  "
              f"at {t['a'][0]:.2f},{t['a'][1]:.2f}")
    if len(done) > a.max:
        print(f"  ... +{len(done) - a.max} more")
    if not done or not a.out:
        if done:
            print("(dry run: -o OUT writes it)")
        return 0
    t = PcbText(b.path)
    for i, _w0, w1 in done:
        s, n = re.subn(r'\(width [^)]*\)', f'(width {num(w1)})', t.text(b.tracks[i]['uuid']), count=1)
        assert n == 1
        t.sub(b.tracks[i]['uuid'], s)
    tear = len(tear_edits(t, [b.tracks[i] for i, _a, _b in done]))
    t.save(a.out)
    print(f"wrote {a.out}: {len(done)} width edit(s), {tear} teardrop(s) dropped (the refill re-creates those "
          f"whose track stays <= 0.9x the pad/via)")
    return 0


# ---------------------------------------------------------------- loops

def c_loops(b, a):
    if no_project(b, 'loops'):
        return 1
    poured = {fl['net'] for fl in b.fills}
    skip = skipped(b)
    nets = [n for n in (set(a.args) or {t['net'] for t in b.tracks if t['net']})
            if n not in poured and n not in skip and n != 'GND' and 'PWR' not in (b.netclass(n) or '').upper()
            and rail_voltage(n.split('/')[-1]) is None]
    allT, allV = b.tracks, b.vias
    def part(net):          # KiCad's joins AND the strict ones: neither may change
        ts = [t for t in b.tracks if t['net'] == net]
        vs = [v for v in b.vias if v['net'] == net]
        entered = frozenset(f"{f.ref}.{p['num']}" for f in b.fps.values() for p in f.pads if p['net'] == net and (
            any(on_layer(p, t['layer']) and (in_pad(p, t['a']) or in_pad(p, t['b'])) for t in ts)
            or any(in_pad(p, (v['x'], v['y'])) for v in vs)))
        # a pad whose only centreline entry goes is left on a graze, which the strict partition
        # misses when the pad was already strictly apart (LED_DO U20.3's centre stub, 2026-10-19)
        return tuple({frozenset(f"{f.ref}.{p['num']}" for f, p in pc['pads']) for pc in piece_parts(b, net, st)}
                     for st in (False, True)) + (entered,)
    found, kill = [], set()
    for net in sorted(nets):
        base = part(net)
        while True:
            b.tracks = [t for t in allT if id(t) not in kill]
            b.vias = [v for v in allV if id(v) not in kill]
            got = None
            pads = [p for f in b.fps.values() if f.placed for p in f.pads if p['net'] == net]
            vias = [(v['x'], v['y'], v['size'] / 2) for v in b.vias if v['net'] == net]
            chs = [c for c in chains(b, set(), only={net})     # an entry stub inside one pad or via is no loop
                   if not any(all(in_pad(p, x) for x in c[4]) for p in pads)
                   and not any(all(math.dist(x, (vx, vy)) <= r for x in c[4]) for vx, vy, r in vias)]
            for ch in sorted(chs, key=lambda c: -sum(b.tracks[i]['len'] for i in c[3])):
                k2 = prune(b, {id(b.tracks[i]) for i in ch[3]})
                b.tracks = [t for t in allT if id(t) not in kill | k2]
                b.vias = [v for v in allV if id(v) not in kill | k2]
                same = part(net) == base
                b.tracks = [t for t in allT if id(t) not in kill]
                b.vias = [v for v in allV if id(v) not in kill]
                if same:
                    got = (ch, k2)
                    break
            if not got:
                break
            ch, k2 = got
            kill |= k2
            gone = [t for t in allT if id(t) in k2]
            found.append((net, sum(t['len'] for t in gone), sorted({t['layer'] for t in gone}), len(gone),
                          sum(1 for v in allV if id(v) in k2), ch[4][0]))
    b.tracks, b.vias = allT, allV
    if not found:
        print(f"loops: no redundant chain on {len(nets)} signal net(s) (power, GND, poured, RF/USB nets skipped)")
        return 0
    print(f"loops: {len(found)} redundant chain(s) on {len({f[0] for f in found})} net(s), "
          f"{sum(f[1] for f in found):.1f} mm; removing each keeps its net's pads joined (prune included)")
    for net, L, lys, nt, nv, at in sorted(found, key=lambda f: -f[1])[:a.max]:
        print(f"  {unesc_disp(net.split('/')[-1]):<26} {L:6.2f} mm  {'/'.join(lys):<14} {nt} seg" + (f" {nv} via" if nv else '')
              + f"  from {at[0]:.2f},{at[1]:.2f}")
    if len(found) > a.max:
        print(f"  ... +{len(found) - a.max} more")
    if not a.out:
        print("(dry run: -o OUT deletes them; then kdrc.py OUT drc --refill-save --baseline BEFORE.json)")
        return 0
    return _rip_out(b, a, kill, 'loops')
