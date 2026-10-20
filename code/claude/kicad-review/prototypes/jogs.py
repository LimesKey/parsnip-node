# prototype (2026-10-19 trace pass 3): census of lane jogs inside smooth chains, both collapse
# candidates, and what blocks each (foreign via / track / pad). Usage: jogs.py BOARD (in a scratch dir)
# for each chain jog: the two collapse candidates (extend the outer leg on one side to the other run's line);
# which clear, and what blocks the rest (foreign vias / tracks / pads)
import sys, math, json
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board
import kpcb_smooth as S
from kpcb_zones import _clearance_fn
from kcommon import *  # noqa
b = Board(sys.argv[1]); clr = _clearance_fn(b)[0]
def X(p1, p2, p3, p4):
    d1 = (p2[0]-p1[0], p2[1]-p1[1]); d2 = (p4[0]-p3[0], p4[1]-p3[1])
    den = d1[0]*d2[1] - d1[1]*d2[0]
    if abs(den) < 1e-9: return None
    t = ((p3[0]-p1[0])*d2[1] - (p3[1]-p1[1])*d2[0]) / den
    return (p1[0] + t*d1[0], p1[1] + t*d1[1])
def psd(p, a, c):
    dx, dy = c[0]-a[0], c[1]-a[1]; L = dx*dx+dy*dy
    t = 0 if L == 0 else max(0, min(1, ((p[0]-a[0])*dx + (p[1]-a[1])*dy) / L))
    return math.dist(p, (a[0]+t*dx, a[1]+t*dy))
res = []
for net, ly, w, order, pts in S.chains(b, S.skipped(b)):
    for k in range(1, len(pts) - 2):
        a, c = pts[k], pts[k+1]
        d1, d2 = S.ang(pts[k-1], a), S.ang(c, pts[k+2])
        if S.turn(d1, d2) > 1 or S.turn(d1, S.ang(a, c)) < 1: continue
        u = (math.cos(math.radians(d1)), math.sin(math.radians(d1)))
        off = abs((c[0]-a[0])*u[1] - (c[1]-a[1])*u[0])
        if not 0.05 <= off <= 1.0: continue
        cands = []
        if k >= 2:
            x = X(pts[k-2], pts[k-1], c, pts[k+2])
            if x: cands.append(('A', pts[:k-1] + [x] + pts[k+1:], [pts[k-2], x, c]))
        if k + 3 <= len(pts) - 1:
            y = X(pts[k+3], pts[k+2], pts[k-1], a)
            if y: cands.append(('B', pts[:k+1] + [y] + pts[k+3:], [a, y, pts[k+3]]))
        ck = None; got = None; why = []
        for tag, new, loc in cands:
            new = S.simplify(new)
            if S.length(new) > S.length(pts) * 1.09 + 0.02 or any(not S.octi(new[i], new[i+1]) for i in range(len(new)-1) if not S.octi(pts[min(i, len(pts)-2)], pts[min(i+1, len(pts)-1)]) is False):
                pass
            ck = ck or S.Clear(b, net, ly, w, pts, order)
            if ck.poly(loc):
                got = (tag, new); break
            bl = set()
            for v in b.vias:
                if v['net'] == net: continue
                g = clr(net, v['net'], 'track', 'via', ly) + v['size']/2 + w/2
                if any(psd((v['x'], v['y']), loc[i], loc[i+1]) < g for i in range(len(loc)-1)):
                    bl.add(('via', v['net'], round(v['x'],3), round(v['y'],3), bool(v.get('free')), v.get('uuid','')[:8]))
            for t in b.tracks:
                if t['net'] == net or t['layer'] != ly: continue
                g = clr(net, t['net'], 'track', 'track', ly) + (t['w'] + w)/2
                if any(S.seg_seg_dist(loc[i], loc[i+1], t['a'], t['b']) < g for i in range(len(loc)-1)):
                    bl.add(('trk', t['net'])); 
            for f in b.fps.values():
                for p in f.pads if f.placed else ():
                    if p['net'] != net and S.on_layer(p, ly) and any(S.seg_pad_dist(loc[i], loc[i+1], p) < w/2 + clr(net, p['net'], 'track', 'pad', ly) for i in range(len(loc)-1)):
                        bl.add(('pad', f.ref + '.' + str(p['num'])))
            why.append((tag, sorted(bl)))
        res.append(dict(net=net, ly=ly, w=w, at=[round(a[0],3), round(a[1],3)], off=round(off,3), ok=got and got[0], why=why))
ok = [r for r in res if r['ok']]
print(f'jogs {len(res)}: collapsible now {len(ok)}')
viaonly = [r for r in res if not r['ok'] and any(bl and all(x[0] == 'via' for x in bl) and len(bl) == 1 for _, bl in r['why'])]
print(f'blocked by exactly one via on some side: {len(viaonly)}')
for r in viaonly: print('  ', r['net'][:22], r['ly'], r['at'], r['off'], [ (t, bl) for t, bl in r['why'] if bl and len(bl)==1])
json.dump(res, open('jogs.json', 'w'), indent=0)
