"""per F.Cu signal net: mm over In1 GND / other In1 plane / gap; gaps split by the In1 track that cuts them"""
import sys, math, re
from collections import defaultdict
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board
from kpcb_power import Raster
GND = re.compile(r'(^|/)GND$')
b = Board(sys.argv[1]); rly = sys.argv[2]; sly = sys.argv[3]; only = sys.argv[4:] 
o = b.outline
ras = {}
for fl in b.fills:
    if fl['layer'] == rly:
        key = 'GND' if GND.search(fl['net'] or '') else 'PLANE'
        ras.setdefault(key, Raster(o, 0.05)).poly(fl['pts'])
def at(r, x, y):
    i, j = round((x - r.x0) / r.res), round((y - r.y0) / r.res)
    return 0 <= i < r.W and 0 <= j < r.H and r.d[j * r.W + i]
reft = [t for t in b.tracks if t['layer'] == rly]
def dseg(p, a, c):
    (ax, ay), (cx, cy) = a, c; dx, dy = cx-ax, cy-ay; L2 = dx*dx+dy*dy or 1e-12
    u = max(0, min(1, ((p[0]-ax)*dx + (p[1]-ay)*dy) / L2)); return math.dist(p, (ax+u*dx, ay+u*dy))
poured = {fl['net'] for fl in b.fills if not GND.search(fl['net'] or '')}
res = defaultdict(lambda: defaultdict(float))
for t in b.tracks:
    n = t['net']
    if t['layer'] != sly or GND.search(n or '') or n in poured or (only and n not in only): continue
    (ax, ay), (bx, by) = t['a'], t['b']; L = math.dist(t['a'], t['b']); k = max(1, int(L/0.05)); dl = L/(k+1)
    for s in range(k+1):
        x, y = ax+(bx-ax)*s/k, ay+(by-ay)*s/k
        if at(ras['GND'], x, y): res[n]['gnd'] += dl; continue
        if 'PLANE' in ras and at(ras['PLANE'], x, y): res[n]['plane'] += dl; continue
        cut = [u['net'] for u in reft if u['net'] != n and dseg((x, y), u['a'], u['b']) <= u['w']/2 + 0.3]
        res[n]['gap:' + (cut[0] if cut else 'void/pad/via')] += dl
for n, d in sorted(res.items(), key=lambda kv: -sum(v for k, v in kv[1].items() if k.startswith('gap'))):
    gap = sum(v for k, v in d.items() if k.startswith('gap'))
    if gap < 0.5: continue
    det = ', '.join(f"{k[4:]} {v:.1f}" for k, v in sorted(d.items(), key=lambda kv: -kv[1]) if k.startswith('gap'))
    print(f"{n:28s} gnd {d['gnd']:5.1f}  plane {d['plane']:5.1f}  GAP {gap:5.1f}: {det}")
