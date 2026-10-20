"""widen.py BOARD [-o OUT] [--nets A,B] [--list N]: grow under-width tracks to the widest legal
step up to their target (PWR_* netclass width, GND 0.3, signals 0.2), checked with the typed
Grid (track 0.127/0.1, pad 0.1, via/zone/RF/pair rules as the netclass). An end inside an SMD
pad caps the width at the pad's narrow side; a widening may not newly touch same-net copper
(Kelvin legs keep their single contact; GND exempt); PWR_HIGH legs < 0.5 mm are sense, skipped;
a track wholly inside a same-net fill gains nothing and is skipped. Sequential: each accepted
width is in the model before the next track is priced."""
import sys, os, math, re, argparse, json
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smooth2 as S
from kpcb_board import Board, PolyIndex, pt_seg_dist
from kpcb_route import Grid, _legs
from kpcb_move import via_layers
from kpcb_edit import PcbText, tear_edits, num

ap = argparse.ArgumentParser()
ap.add_argument('board'); ap.add_argument('-o', '--out'); ap.add_argument('--nets', default='')
ap.add_argument('--list', type=int, default=20); ap.add_argument('--sig', type=float, default=0.2)
a = ap.parse_args()
b = Board(a.board)
cls = {c['name']: c for c in json.load(open(os.path.splitext(a.board)[0] + '.kicad_pro'))['net_settings']['classes']}
skip = {n for pair in _legs(b) for n in pair} | {n for n in {t['net'] for t in b.tracks} if (b.netclass(n) or 'Default') in S.SKIP_CLASS}
want = set(a.nets.split(',')) if a.nets else None
# switch nodes keep their area (EMI); the inner layer under RF traces keeps its plane
skip |= {p['net'] for f in b.fps.values() for p in f.pads if p['net'] and (p.get('fn') or '').upper().startswith(('SW', 'PH', 'LX'))}
rf = [t for t in b.tracks if 'RF' in (b.netclass(t['net']) or '')]
pads = {}
for f in b.fps.values():
    if f.placed:
        for p in f.pads:
            if p['net']:
                pads.setdefault(p['net'], []).append(p)
fills = {}
for fl in b.fills:
    fills.setdefault((fl['net'], fl['layer']), []).append(fl)


def target(t):
    nc = b.netclass(t['net']) or 'Default'
    if nc in ('PWR_HIGH', 'PWR_RAIL'):
        if nc == 'PWR_HIGH' and t['w'] < 0.5:
            return None
        return cls[nc]['track_width']
    if t['net'] == 'GND':
        return 0.3
    return a.sig


def on_layer(p, ly):
    return p['drill'] or ly in p['layers'] or (ly in ('F.Cu', 'B.Cu') and '*.Cu' in p['layers'])


def samples(t, step=0.02):
    n = max(1, math.ceil(t['len'] / step))
    return [(t['a'][0] + (t['b'][0] - t['a'][0]) * k / n, t['a'][1] + (t['b'][1] - t['a'][1]) * k / n) for k in range(n + 1)]


def cap(t):
    """narrow side of any SMD pad an end sits in"""
    c = 99.0
    for p in pads.get(t['net'], ()):
        if on_layer(p, t['layer']) and not p['drill'] and (S.in_pad(p, t['a']) or S.in_pad(p, t['b'])):
            c = min(c, p['sx'], p['sy'])
    return c


def in_fill(t):
    fs = fills.get((t['net'], t['layer']), ())
    pis = [PolyIndex(f['pts']) for f in fs]
    return bool(pis) and all(any(pi.touches(x, 0) for pi in pis) for x in samples(t, 0.1))


def contacts(t, w, i):
    """same-net items (not touching t's ends) the capsule of width w touches"""
    out = set()
    if t['net'] == 'GND':
        return out
    ends = (t['a'], t['b'])
    for p in pads.get(t['net'], ()):
        if on_layer(p, t['layer']) and not any(S.in_pad(p, e, 0.01) for e in ends) and \
                any(S.in_pad(p, x, w / 2) for x in samples(t, 0.05)):
            out.add(('pad', id(p)))
    for v in b.vias:
        if v['net'] == t['net'] and t['layer'] in via_layers(v, b.copper):
            c = (v['x'], v['y'])
            if all(math.dist(c, e) > v['size'] / 2 + 0.01 for e in ends) and pt_seg_dist(c, t['a'], t['b']) < (w + v['size']) / 2:
                out.add(('via', id(v)))
    for j, u in enumerate(b.tracks):
        if j == i or u['net'] != t['net'] or u['layer'] != t['layer']:
            continue
        if any(pt_seg_dist(e, u['a'], u['b']) <= u['w'] / 2 + 0.01 for e in ends) or \
                any(pt_seg_dist(e, t['a'], t['b']) <= t['w'] / 2 + 0.01 for e in (u['a'], u['b'])):
            continue                                        # joined at an end / T: already one copper
        if S.seg_dist(t['a'], t['b'], u['a'], u['b']) < (w + u['w']) / 2:
            out.add(('trk', j))
    for f in fills.get((t['net'], t['layer']), ()):
        pi = PolyIndex(f['pts'])
        if any(pi.touches(x, w / 2) for x in samples(t, 0.05)):
            out.add(('fill', id(f)))
    return out


def clear(t, w):
    xs, ys = [t['a'][0], t['b'][0]], [t['a'][1], t['b'][1]]
    box = (min(xs) - 0.6, min(ys) - 0.6, max(xs) + 0.6, max(ys) + 0.6)
    g = Grid(S.typed(b, t['layer']), t['net'], box, S.RES, w, 0.6, 0.3, [t['layer']], extra_clr=S.RES * 0.8)
    blk = g.blk[t['layer']]
    for x in samples(t, S.RES / 2):
        i, j = round((x[0] - g.x0) / g.res), round((x[1] - g.y0) / g.res)
        if not (0 <= i < g.W and 0 <= j < g.H) or blk[j * g.W + i]:
            return False
    return True


def plan(i, t):
    """[widths to try, best first] or None"""
    if not t['net'] or t['net'] in skip or (want and t['net'] not in want) or t['len'] < 1e-3 or t['locked']:
        return None
    if math.dist(t['mid'], ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)) > 1e-4:
        return None                                         # arc
    if t['layer'] not in ('F.Cu', 'B.Cu') and any(S.seg_dist(t['a'], t['b'], r['a'], r['b']) < 2.0 for r in rf):
        return None
    tg = target(t)
    if tg is None or t['w'] >= tg - 1e-3:
        return None
    tg = min(tg, cap(t))
    if tg < t['w'] + 0.049 or in_fill(t):
        return None
    step = 0.1 if tg >= 0.6 else 0.05
    cands, w = [], tg
    while w >= t['w'] + 0.049:
        cands.append(round(w, 3))
        w -= step
    return cands


def ok(i, t, w):
    return not (contacts(t, w, i) - contacts(t, t['w'], i)) and clear(t, w)


def per_segment(t):
    return (b.netclass(t['net']) or 'Default') == 'PWR_HIGH'


def runs():
    """non-PWR_HIGH tracks grouped into runs: joined end to end at nodes of exactly two
    same-net, same-layer tracks that are not on a pad or via (width changes do not break a run)"""
    from collections import defaultdict
    ends = defaultdict(list)
    for i, t in enumerate(b.tracks):
        if t['net'] and not per_segment(t):
            ends[(t['net'], t['layer'], S.q(t['a']))].append(i)
            ends[(t['net'], t['layer'], S.q(t['b']))].append(i)
    vias = {(v['net'], S.q((v['x'], v['y']))) for v in b.vias}
    par = {}
    def find(x):
        while par.setdefault(x, x) != x:
            x = par[x]
        return x
    for (net, ly, n), lst in ends.items():
        if len(lst) == 2 and (net, n) not in vias and \
                not any(on_layer(p, ly) and S.in_pad(p, n) for p in pads.get(net, ())):
            par[find(lst[0])] = find(lst[1])
    g = defaultdict(list)
    for i in {i for lst in ends.values() for i in lst}:
        g[find(i)].append(i)
    return list(g.values())


done = []
tally = {}
def accept(i, t, w):
    done.append((i, t['w'], w))
    k = b.netclass(t['net']) or 'Default'
    tally[k] = tally.get(k, 0) + t['len'] * (w - t['w'])
    t['w'] = w


for i, t in enumerate(b.tracks):                            # PWR_HIGH: per segment, R first
    if t['net'] and per_segment(t):
        for w in plan(i, t) or ():
            if ok(i, t, w):
                accept(i, t, w)
                break


def pad_end(t):
    return t['len'] < 0.6 and any(on_layer(p, t['layer']) and (S.in_pad(p, t['a']) or S.in_pad(p, t['b']))
                                  for p in pads.get(t['net'], ()))


for run in runs():                                          # the rest: one width per run
    ps = {i: plan(i, b.tracks[i]) for i in run}
    if not any(ps.values()):
        continue
    core = [i for i in run if not pad_end(b.tracks[i])] or run
    ends_ = [i for i in run if i not in core]
    tops = [ps[i][0] for i in core if ps[i]]
    if not tops:
        continue
    lo = min(b.tracks[i]['w'] for i in core)
    cands = sorted({w for i in core if ps[i] for w in ps[i] if w > lo + 0.049}, reverse=True)
    for w in cands:
        if w > min(tops) + 1e-3:
            continue
        need = [i for i in core if b.tracks[i]['w'] < w - 1e-3]
        if any(not ps[i] or w not in ps[i] for i in need):
            if all(ps[i] and w in ps[i] for i in need if not in_fill(b.tracks[i])):
                need = [i for i in need if not in_fill(b.tracks[i])]
            else:
                continue
        if all(ok(i, b.tracks[i], w) for i in need):
            for i in need:
                accept(i, b.tracks[i], w)
            for i in ends_:                                 # pad entries taper only if they must
                for w2 in [x for x in (ps[i] or ()) if x <= w + 1e-3]:
                    if ok(i, b.tracks[i], w2):
                        accept(i, b.tracks[i], w2)
                        break
            break
print(f"widened {len(done)} track(s); added copper (mm2) by class: " + ', '.join(f'{k} {v:.1f}' for k, v in sorted(tally.items())))
for i, w0, w1 in sorted(done, key=lambda d: -b.tracks[d[0]]['len'] * (d[2] - d[1]))[:a.list]:
    t = b.tracks[i]
    print(f"  {t['net']:28s} {t['layer']:6s} {w0:g} -> {w1:g}  {t['len']:.2f} mm  at {t['a'][0]:.2f},{t['a'][1]:.2f}")
if a.out:
    txt = PcbText(a.board)
    for i, w0, w1 in done:
        t = b.tracks[i]
        s, c = re.subn(r'\(width [^)]*\)', f'(width {num(w1)})', txt.text(t['uuid']), count=1)
        assert c == 1, t['uuid']
        txt.sub(t['uuid'], s)
    n = len(tear_edits(txt, [b.tracks[i] for i, _, _ in done]))
    txt.save(a.out)
    print(f"wrote {a.out}: {len(done)} width edit(s), {n} teardrop(s) dropped")
