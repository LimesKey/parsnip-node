#!/usr/bin/env python3
"""loops.py BOARD [NET...]: redundant track chains (loop members) per signal net.
Greedy: remove the longest chain whose removal leaves the net's pad partition
unchanged, repeat. Prints net, chain length, layer(s), uuids."""
import sys, json
from collections import defaultdict
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board
from kpcb_route import piece_parts

Q = lambda p: (round(p[0], 3), round(p[1], 3))


def partition(b, net):
    return {frozenset(f"{f.ref}.{p['num']}" for f, p in q['pads']) for q in piece_parts(b, net)}


def chains(b, net):
    T = [t for t in b.tracks if t['net'] == net]
    deg = defaultdict(int)
    for t in T:
        deg[(t['layer'], Q(t['a']))] += 1
        deg[(t['layer'], Q(t['b']))] += 1
    stops = set()
    for v in b.vias:
        if v['net'] == net:
            for ly in b.copper:
                stops.add((ly, Q((v['x'], v['y']))))
    for f in b.fps.values():
        for p in f.pads:
            if p['net'] == net:
                for ly in b.copper:
                    stops.add((ly, Q((p['x'], p['y']))))
    junction = lambda n: deg[n] != 2 or n in stops
    adj = defaultdict(list)
    for i, t in enumerate(T):
        adj[(t['layer'], Q(t['a']))].append(i)
        adj[(t['layer'], Q(t['b']))].append(i)
    seen, out = set(), []
    for i, t in enumerate(T):
        if i in seen:
            continue
        ch = {i}
        for end in ((t['layer'], Q(t['a'])), (t['layer'], Q(t['b']))):
            n, cur = end, i
            while not junction(n):
                nxt = [j for j in adj[n] if j != cur and j not in ch]
                if not nxt:
                    break
                cur = nxt[0]
                ch.add(cur)
                tt = T[cur]
                a, bb = (tt['layer'], Q(tt['a'])), (tt['layer'], Q(tt['b']))
                n = bb if a == n else a
        seen |= ch
        out.append([T[j] for j in ch])
    return out


def main():
    b = Board(sys.argv[1])
    nets = sys.argv[2:] or sorted({t['net'] for t in b.tracks if t['net']})
    filled = {fl['net'] for fl in b.fills}
    allT = b.tracks
    res = {}
    for net in nets:
        if net in filled or net == 'GND':
            continue
        base = partition(b, net)
        removed = []
        while True:
            best = None
            for ch in sorted(chains(b, net), key=lambda c: -sum(t['len'] for t in c)):
                ids = {id(t) for t in ch}
                b.tracks = [t for t in b.tracks if id(t) not in ids]
                ok = partition(b, net) == base
                b.tracks = allT if not removed else b.tracks + ch
                if not removed:
                    b.tracks = allT
                else:
                    b.tracks = [t for t in allT if id(t) not in {id(x) for c in removed for x in c}]
                if ok:
                    best = ch
                    break
            if not best:
                break
            removed.append(best)
            b.tracks = [t for t in allT if id(t) not in {id(x) for c in removed for x in c}]
        b.tracks = allT
        if removed:
            res[net] = [[t['uuid'] for t in c] for c in removed]
            for c in removed:
                L = sum(t['len'] for t in c)
                print(f"{net:32s} {L:6.2f} mm  {sorted({t['layer'] for t in c})}  {len(c)} seg  "
                      f"{c[0]['a'][0]:.2f},{c[0]['a'][1]:.2f}")
    json.dump(res, open(sys.argv[1] + '.loops.json', 'w'), indent=1)


main()
