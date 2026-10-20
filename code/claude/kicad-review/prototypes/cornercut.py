"""cornercut.py BOARD [-o OUT] [--list N]: cut every visible corner sharper than 45 degrees at a
node of exactly two same-net, same-layer tracks (not on a pad or via, any widths): P1 on leg 1,
P2 on leg 2 (0.05 mm steps, <= 1.5 mm, never the far ends), joined straight or by a 45 dogleg,
every new turn <= 45; cheapest (length + bends) clear one wins. Clearance: smooth2.Clear (typed
DRU rules) at RES 0.01, connector at the wider leg's width. Sequential: each cut is in the model
before the next corner is priced."""
import sys, os, math, re, argparse, uuid as U
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import smooth2 as S
S.RES = 0.01
from kpcb_board import Board
from kpcb_route import _legs
from kpcb_edit import PcbText, tear_edits, num
from collections import defaultdict

ap = argparse.ArgumentParser()
ap.add_argument('board'); ap.add_argument('-o', '--out'); ap.add_argument('--list', type=int, default=30)
ap.add_argument('--hidden', action='store_true', help='also corners hidden under same-net copper')
a = ap.parse_args()
b = Board(a.board)
skip = {n for pair in _legs(b) for n in pair} | {n for n in {t['net'] for t in b.tracks} if (b.netclass(n) or 'Default') in S.SKIP_CLASS}
pads = defaultdict(list)
for f in b.fps.values():
    if f.placed:
        for p in f.pads:
            if p['net']:
                pads[p['net']].append(p)
q = S.q


def far(t, n):
    return t['b'] if q(t['a']) == n else t['a']


def is_arc(t):
    return math.dist(t['mid'], ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)) > 1e-4


def corners():
    ends = defaultdict(list)
    for i, t in enumerate(b.tracks):
        if t['net'] and t['net'] not in skip and t['len'] > 1e-4 and not is_arc(t) and not t['locked']:
            ends[(t['net'], t['layer'], q(t['a']))].append(i)
            ends[(t['net'], t['layer'], q(t['b']))].append(i)
    allends = defaultdict(int)
    for t in b.tracks:
        if t['net']:
            allends[(t['net'], t['layer'], q(t['a']))] += 1
            allends[(t['net'], t['layer'], q(t['b']))] += 1
    out = []
    for (net, ly, n), lst in ends.items():
        if len(lst) != 2 or allends[(net, ly, n)] != 2:
            continue
        if any(math.dist(n, (v['x'], v['y'])) <= v['size'] / 2 for v in b.vias if v['net'] == net):
            continue
        t1, t2 = b.tracks[lst[0]], b.tracks[lst[1]]
        # an end cap that merely overlaps a pad is KiCad's connection to it: not a free corner
        if any((p['drill'] or ly in p['layers']) and S.in_pad(p, n, max(t1['w'], t2['w']) / 2 + 0.01) for p in pads[net]):
            continue
        if any(math.dist(n, (v['x'], v['y'])) <= v['size'] / 2 + max(t1['w'], t2['w']) / 2 + 0.01 for v in b.vias if v['net'] == net):
            continue
        th = S.turn(S.ang(far(t1, n), n), S.ang(n, far(t2, n)))
        if th >= 46:
            out.append((th, net, ly, n, lst[0], lst[1]))
    return sorted(out, reverse=True)


def tracks_through(net, ly, pt, excl):
    """another same-net track body runs through pt: a T, not a plain corner"""
    return any(i not in excl and t['net'] == net and t['layer'] == ly and S.pt_seg_dist(pt, t['a'], t['b']) <= t['w'] / 2
               for i, t in enumerate(b.tracks))


def cut(th, net, ly, V, i1, i2):
    t1, t2 = b.tracks[i1], b.tracks[i2]
    A, C = far(t1, V), far(t2, V)
    L1, L2 = math.dist(A, V), math.dist(V, C)
    w = max(t1['w'], t2['w'])
    if tracks_through(net, ly, V, {i1, i2}):
        return None
    ck = S.Clear(b, net, ly, w, [A, V, C], [i1, i2])        # same-net contact rule only
    from exact import Exact
    xs, ys = [A[0], V[0], C[0]], [A[1], V[1], C[1]]
    ex = Exact(b, net, ly, w, (min(xs) - 2, min(ys) - 2, max(xs) + 2, max(ys) + 2))

    def okc(con):
        if not ex.poly(con):
            return False
        for u, v in zip(con, con[1:]):
            n = max(1, math.ceil(math.dist(u, v) / 0.01))
            for k in range(n + 1):
                kk = ck._k((u[0] + (v[0] - u[0]) * k / n, u[1] + (v[1] - u[1]) * k / n))
                if ck.own[kk] and not ck.allow[kk]:
                    return False
        return True
    d_in, d_out = S.ang(A, V), S.ang(V, C)
    best = None
    steps = lambda L: [k * 0.05 for k in range(1, int(min(L - 0.02, 1.5) / 0.05) + 1)]
    for d1 in steps(L1):
        P1 = (V[0] + (A[0] - V[0]) * d1 / L1, V[1] + (A[1] - V[1]) * d1 / L1)
        for d2 in steps(L2):
            P2 = (V[0] + (C[0] - V[0]) * d2 / L2, V[1] + (C[1] - V[1]) * d2 / L2)
            for con in S.connectors(P1, P2):
                con = S.tidy(con)
                if len(con) < 2:
                    continue
                poly = [A] + con + [C]
                turns = [S.turn(S.ang(poly[k - 1], poly[k]), S.ang(poly[k], poly[k + 1])) for k in range(1, len(poly) - 1)]
                if max(turns) >= 46:
                    continue
                c = S.cost(poly)
                if best and c >= best[0]:
                    continue
                if okc(con):
                    best = (c, con)
    return best


done = []
for th, net, ly, V, i1, i2 in corners():
    t1, t2 = b.tracks[i1], b.tracks[i2]
    if not a.hidden:
        # skip a corner wholly inside wider same-net copper (pour bands, fat trunks)
        from census_cov import covered
        if covered(b, net, ly, V, max(t1['w'], t2['w']), {i1, i2}):
            continue
    r = cut(th, net, ly, V, i1, i2)
    if not r:
        continue
    con = r[1]
    w = max(t1['w'], t2['w'])
    for t, P in ((t1, con[0]), (t2, con[-1])):          # pull each leg back to its cut point
        if q(t['a']) == V:
            t['a'] = P
        else:
            t['b'] = P
        t['len'] = math.dist(t['a'], t['b'])
        t['mid'] = ((t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2)
        t['_moved'] = True
    for u, v in zip(con, con[1:]):
        b.tracks.append({'net': net, 'layer': ly, 'w': w, 'a': u, 'b': v, 'len': math.dist(u, v),
                         'mid': ((u[0] + v[0]) / 2, (u[1] + v[1]) / 2), 'uuid': None, 'locked': False})
    done.append((th, net, ly, V, con))
print(f"cut {len(done)} corner(s)")
for th, net, ly, V, con in done[:a.list]:
    print(f"  {net:28s} {ly:6s} {th:5.0f} deg at {V[0]:.3f},{V[1]:.3f}  -> {len(con) - 1} connector seg(s)")
if a.out:
    txt = PcbText(a.board)
    moved = [t for t in b.tracks if t.get('_moved') and t['uuid']]
    old = {t['uuid']: t for t in Board(a.board).tracks}
    for t in moved:
        s = txt.text(t['uuid'])
        for tag, P in (('start', t['a']), ('end', t['b'])):
            s, c = re.subn(r'\(%s [^)]*\)' % tag, f"({tag} {num(P[0])} {num(P[1])})", s, count=1)
            assert c == 1
        txt.sub(t['uuid'], s)
    for t in b.tracks:
        if t['uuid'] is None:
            txt.segment(t['net'], t['layer'], t['w'], t['a'], t['b'])
    n = len(tear_edits(txt, [old[t['uuid']] for t in moved]))
    txt.save(a.out)
    print(f"wrote {a.out}: {len(moved)} leg(s) shortened, {sum(1 for t in b.tracks if t['uuid'] is None)} segment(s) added, {n} teardrop(s) dropped")
