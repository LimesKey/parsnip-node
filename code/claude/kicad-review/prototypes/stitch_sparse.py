"""Greedy GND stitching of sparse outer-layer pour: worst point (farthest from any GND via /
plated GND hole in its F.Cu or B.Cu fill fragment) first, a legal via spot near it via kpcb's
own `legal_spots` (the `stitch` search), repeat until every point is within TARGET mm or no
spot helps.  usage: stitch_sparse.py BOARD TARGET [-o OUT]"""
import sys, math, re
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board, point_in_poly
from kpcb_power import legal_spots, net_fills
from kpcb_edit import PcbText

GND = re.compile(r'(^|/)GND$')
EXCL = [(105, 134, 150, 166)]          # U1 + its antenna + the GPIO_38 pocket (R4/J1/U4): keep free
STEP = 0.25

b = Board(sys.argv[1]); target = float(sys.argv[2])
out = sys.argv[sys.argv.index('-o') + 1] if '-o' in sys.argv else None
anch = [(v['x'], v['y']) for v in b.vias if GND.search(v['net'] or '')]
anch += [(p['x'], p['y']) for f in b.placed() for p in f.pads if p['drill'] and GND.search(p['net'] or '')]
excl = lambda x, y: any(x0 <= x <= x1 and y0 <= y <= y1 for x0, y0, x1, y1 in EXCL)   # noqa: E731

frags = []                                       # [(layer, fill, [sample pts])]
for fl in b.fills:
    if fl['layer'] in ('F.Cu', 'B.Cu') and GND.search(fl['net'] or '') and fl['area'] >= 4:
        x0, y0, x1, y1 = fl['bbox']
        pts = [(x0 + i * STEP, y0 + j * STEP) for i in range(int((x1 - x0) / STEP) + 1)
               for j in range(int((y1 - y0) / STEP) + 1)]
        pts = [p for p in pts if not excl(*p) and point_in_poly(p, fl['pts'])]
        if pts:
            frags.append((fl['layer'], fl, pts))

def mine(fl, a):
    x0, y0, x1, y1 = fl['bbox']
    return x0 - 0.5 <= a[0] <= x1 + 0.5 and y0 - 0.5 <= a[1] <= y1 + 0.5 and point_in_poly(a, fl['pts'])

dist = {}                                        # (frag idx, pt) -> distance to nearest own anchor
for k, (ly, fl, pts) in enumerate(frags):
    own = [a for a in anch if mine(fl, a)]
    for p in pts:
        dist[k, p] = min((math.dist(p, a) for a in own), default=99.0)

def worst_of():
    return max(((d, k, p) for (k, p), d in dist.items() if d > target and (k, p) not in dead), default=None)

fills = net_fills(b, 'GND')
have = [(v['x'], v['y'], v['drill']) for v in b.vias]
dead, made = set(), []
before = sorted((d for d in dist.values()), reverse=True)
while True:
    w = worst_of()
    if not w:
        break
    d, k, p = w
    got = None
    for vsz, vdr in ((0.6, 0.3), (0.5, 0.2)):
        r1 = min(2.5, d - target / 2)
        cand = legal_spots(b, 'GND', p[0], p[1], 0.0, r1, vsz, vdr, 6, False, fills, list(have))
        cand = [c for c in cand if not excl(c[0], c[1]) and mine(frags[k][1], (c[0], c[1]))]
        if cand:
            got = (cand[0][0], cand[0][1], vsz, vdr); break
    if not got:
        for (kk, q) in list(dist):                # nothing legal near: drop this point's neighbourhood
            if kk == k and math.dist(q, p) <= 0.75:
                dead.add((kk, q))
        continue
    x, y, vsz, vdr = got
    have.append((x, y, vdr)); made.append(got)
    gain = 0
    for kk, (ly, fl, pts) in enumerate(frags):
        if mine(fl, (x, y)):
            for q in pts:
                nd = math.dist(q, (x, y))
                if nd < dist[kk, q]:
                    dist[kk, q] = nd; gain += 1
    print(f"via {vsz}/{vdr} at {x:.3f},{y:.3f}  for worst {d:.1f} mm at {p[0]:.2f},{p[1]:.2f} ({frags[k][0]})")

after = sorted(dist.values(), reverse=True)
area = lambda ds, t: sum(1 for v in ds if v > t) * STEP * STEP   # noqa: E731
for t in (4, 5, 6, 8):
    print(f"outer GND pour > {t} mm from a via: {area(before, t):6.1f} -> {area(after, t):6.1f} mm2")
print(f"worst {before[0]:.1f} -> {after[0]:.1f} mm; {len(made)} via(s); {len({(k, p) for k, p in dead})} sample(s) with no legal spot")
if out and made:
    t = PcbText(b.path)
    for x, y, vsz, vdr in made:
        t.via('GND', x, y, vsz, vdr, [b.copper[0], b.copper[-1]], free=True)
    t.save(out)
    print(f"wrote {len(made)} via(s) -> {out}")
