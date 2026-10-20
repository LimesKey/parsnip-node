"""square.py BOARD [-o OUT]: an off-octilinear segment with an end inside a same-net pad gets that
end slid within the pad (nearest octilinear direction from the other end, the end kept >= w/4
inside the pad and at most 0.5 mm from where it was), checked with exact.Exact + the same-net
contact rule. The other tracks ending at the old point move with it (they join through the pad
anyway). Skips RF/USB/diff pairs and segments hidden under same-net copper."""
import sys, os, math, re, argparse
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smooth2 as S
S.RES = 0.01
from exact import Exact
from census_cov import covered
from kpcb_board import Board
from kpcb_route import _legs
from kpcb_edit import PcbText, tear_edits, num
from collections import defaultdict

ap = argparse.ArgumentParser(); ap.add_argument('board'); ap.add_argument('-o', '--out')
a = ap.parse_args()
b = Board(a.board)
skip = {n for pair in _legs(b) for n in pair} | {n for n in {t['net'] for t in b.tracks} if (b.netclass(n) or 'Default') in S.SKIP_CLASS}
pads = defaultdict(list)
for f in b.fps.values():
    if f.placed:
        for p in f.pads:
            if p['net']:
                pads[p['net']].append((f.ref, p))
q = S.q
moved = {}
for i, t in enumerate(b.tracks):
    if not t['net'] or t['net'] in skip or t['len'] < 0.05 or t['locked'] or S.octi(t['a'], t['b']):
        continue
    if math.dist(t['mid'], ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)) > 1e-4:
        continue
    if covered(b, t['net'], t['layer'], t['mid'], t['w'], {i}):
        continue
    best = None
    for E, F in ((t['a'], t['b']), (t['b'], t['a'])):
        hosts = [(r, p) for r, p in pads[t['net']] if (p['drill'] or t['layer'] in p['layers']) and S.in_pad(p, E)]
        if not hosts:
            continue
        # F must not itself move: skip when F is in a pad we'd also slide (fine) - F stays put here
        L = math.dist(E, F)
        th = S.ang(F, E)
        for k in range(8):
            d = math.radians(k * 45)
            if S.turn(th, k * 45) > 20:
                continue
            u = (math.cos(d), math.sin(d))
            s = (E[0] - F[0]) * u[0] + (E[1] - F[1]) * u[1]       # projection of E onto the ray
            if s <= 0.05:
                continue
            E2 = (F[0] + u[0] * s, F[1] + u[1] * s)
            if math.dist(E, E2) > 0.5:
                continue
            if not any(S.in_pad(p, E2, -t['w'] / 4) for _r, p in hosts):
                continue
            score = math.dist(E, E2)
            if best is None or score < best[0]:
                best = (score, E, E2, F)
    if not best:
        continue
    _, E, E2, F = best
    xs, ys = [E[0], E2[0], F[0]], [E[1], E2[1], F[1]]
    ex = Exact(b, t['net'], t['layer'], t['w'], (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2))
    if not ex.seg(F, E2):
        continue
    ck = S.Clear(b, t['net'], t['layer'], t['w'], [F, E], [i])
    n = max(1, math.ceil(math.dist(F, E2) / 0.01))
    if any(ck.own[kk] and not ck.allow[kk] for kk in (ck._k((F[0] + (E2[0] - F[0]) * j / n, F[1] + (E2[1] - F[1]) * j / n)) for j in range(n + 1))):
        continue
    # every same-net track ending at E on this layer moves its end to E2 (must stay clear too)
    co = [j for j, u in enumerate(b.tracks) if j != i and u['net'] == t['net'] and u['layer'] == t['layer'] and q(E) in (q(u['a']), q(u['b']))]
    if any(not Exact(b, t['net'], t['layer'], b.tracks[j]['w'], (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2)).seg(
            E2, b.tracks[j]['b'] if q(b.tracks[j]['a']) == q(E) else b.tracks[j]['a']) for j in co):
        continue
    for j in [i] + co:
        u = b.tracks[j]
        if q(u['a']) == q(E):
            u['a'] = E2
        else:
            u['b'] = E2
        u['len'] = math.dist(u['a'], u['b'])
        u['mid'] = ((u['a'][0] + u['b'][0]) / 2, (u['a'][1] + u['b'][1]) / 2)
        moved[j] = u
    print(f"  {t['net']:26s} {t['layer']:6s} end {E[0]:.3f},{E[1]:.3f} -> {E2[0]:.3f},{E2[1]:.3f} ({math.dist(E, E2) * 1000:.0f} um)")
print(f"squared, {len(moved)} track end(s) moved")
if a.out:
    old = {t['uuid']: t for t in Board(a.board).tracks}
    txt = PcbText(a.board)
    for u in moved.values():
        s = txt.text(u['uuid'])
        for tag, P in (('start', u['a']), ('end', u['b'])):
            s, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(P[0])} {num(P[1])})", s, count=1)
            assert c == 1
        txt.sub(u['uuid'], s)
    n = len(tear_edits(txt, [old[u['uuid']] for u in moved.values()]))
    txt.save(a.out)
    print(f"wrote {a.out}: {n} teardrop(s) dropped")
