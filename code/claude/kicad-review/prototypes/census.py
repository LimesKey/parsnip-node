#!/usr/bin/env python3
"""census.py BOARD [--list N] [--kind acute|90|odd|neck|stub]: whole-board track aesthetics census.
Corners at 2-end nodes (same net/layer), odd-angle segments, width steps mid-run, tiny segments."""
import sys, math, argparse
from collections import defaultdict, Counter
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/prototypes')
from kpcb_board import Board, PolyIndex, pt_seg_dist
from smooth import ang, turn, octi, q, in_pad, SKIP_CLASS
from kpcb_route import _legs

ap = argparse.ArgumentParser(); ap.add_argument('board'); ap.add_argument('--list', type=int, default=0)
ap.add_argument('--kind', default=''); ap.add_argument('--all', action='store_true')
a = ap.parse_args()
b = Board(a.board)
skip = {n for pair in _legs(b) for n in pair} | {n for n in {t['net'] for t in b.tracks} if (b.netclass(n) or 'Default') in SKIP_CLASS}
ends = defaultdict(list)
for i, t in enumerate(b.tracks):
    if not t['net']: continue
    ends[(t['net'], t['layer'], q(t['a']))].append(i); ends[(t['net'], t['layer'], q(t['b']))].append(i)
pads = defaultdict(list)
for f in b.fps.values():
    if f.placed:
        for p in f.pads:
            if p['net']: pads[p['net']].append(p)
vias = defaultdict(list)
for v in b.vias: vias[v['net']].append(v)
fills = defaultdict(list)
for fl in b.fills:
    fills[(fl['net'], fl['layer'])].append((fl['bbox'], PolyIndex(fl['pts'])))
def covered(net, ly, pt, w, mine):
    """pt's w/2 disk is inside other same-net copper: a pad, via, wider track or fill"""
    ring = [pt] + [(pt[0] + w / 2 * math.cos(k * math.pi / 4), pt[1] + w / 2 * math.sin(k * math.pi / 4)) for k in range(8)]
    for p in pads[net]:
        if (p['drill'] or ly in p['layers'] or '*.Cu' in p['layers']) and in_pad(p, pt, -w / 2):
            return True
    if any(math.dist(pt, (v['x'], v['y'])) <= v['size'] / 2 - w / 2 for v in vias[net]):
        return True
    for i in near[(net, ly)]:
        t = b.tracks[i]
        if i not in mine and t['w'] > w and pt_seg_dist(pt, t['a'], t['b']) <= (t['w'] - w) / 2:
            return True
    for bb, pi in fills[(net, ly)]:
        if bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3] and all(x in pi for x in ring):
            return True
    return False
near = defaultdict(list)
for i, t in enumerate(b.tracks):
    near[(t['net'], t['layer'])].append(i)
far = lambda t, n: t['b'] if q(t['a']) == n else t['a']
c = Counter(); items = defaultdict(list)
for (net, ly, n), lst in ends.items():
    if not a.all and net in skip: continue
    onvia = any(math.dist(n, (v['x'], v['y'])) <= v['size']/2 for v in vias[net])
    onpad = any(in_pad(p, n) for p in pads[net] if p['drill'] or ly in p['layers'])
    if len(lst) == 1 and not onvia and not onpad:
        pass
    if len(lst) == 2:
        t1, t2 = b.tracks[lst[0]], b.tracks[lst[1]]
        if t1['len'] < 1e-4 or t2['len'] < 1e-4: continue
        th = turn(ang(far(t1, n), n), ang(n, far(t2, n)))
        k = None if th < 0.5 else '45' if th < 46 else '90' if th < 91 else 'acute'
        if k and covered(net, ly, n, max(t1['w'], t2['w']), set(lst)):
            c['hid' + k] += 1
            k = None
        if k: c[k] += 1
        if k in ('90', 'acute'):
            items[k].append((net, ly, n, round(th), 'via' if onvia else 'pad' if onpad else '-'))
        if abs(t1['w'] - t2['w']) > 1e-3 and not onvia and not onpad:
            c['neck'] += 1; items['neck'].append((net, ly, n, t1['w'], t2['w']))
for i, t in enumerate(b.tracks):
    if not t['net'] or (not a.all and t['net'] in skip): continue
    if t['len'] < 1e-4: continue
    if math.dist(t['mid'], ((t['a'][0]+t['b'][0])/2, (t['a'][1]+t['b'][1])/2)) > 1e-4: c['arc'] += 1; continue
    c['segs'] += 1; c['mm'] += t['len']
    mid = ((t['a'][0]+t['b'][0])/2, (t['a'][1]+t['b'][1])/2)
    if not octi(t['a'], t['b']) and covered(t['net'], t['layer'], mid, t['w'], {i}):
        c['hidodd'] += 1
    elif not octi(t['a'], t['b']):
        c['odd'] += 1; items['odd'].append((t['net'], t['layer'], q(t['a']), q(t['b']), round(t['len'], 2)))
    if t['len'] < 0.05 and covered(t['net'], t['layer'], mid, t['w'], {i}): c['hidstub'] += 1
    elif t['len'] < 0.05: c['stub'] += 1; items['stub'].append((t['net'], t['layer'], q(t['a']), round(t['len'], 3)))
c['mm'] = round(c['mm'], 1)
print(dict(c))
byly = Counter((x[1], k) for k in ('90', 'acute', 'odd') for x in items[k])
print(dict(byly))
if a.list:
    for k in ([a.kind] if a.kind else ['acute', '90', 'odd', 'neck', 'stub']):
        print(f'--- {k} ({len(items[k])})')
        for x in items[k][:a.list]: print('  ', x)
