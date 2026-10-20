#!/usr/bin/env python3
"""vscan.py BOARD NET X0,Y0,X1,Y1 [VSZ/VDR] [W]: legal through-via spots for NET in the
box (router Grid rules), plus per-layer free-for-track cells at width W, as ASCII."""
import sys
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board

bf, net, box = sys.argv[1], sys.argv[2], tuple(map(float, sys.argv[3].split(',')))
vsz, vdr = map(float, (sys.argv[4] if len(sys.argv) > 4 else '0.5/0.2').split('/'))
w = float(sys.argv[5]) if len(sys.argv) > 5 else 0.15
LY = sys.argv[6] if len(sys.argv) > 6 else ''
from kpcb_route import Grid
b = Board(bf)
from kpcb_board import _resolve_net
net = _resolve_net(b, net) if not any(p['net'] == net for f in b.fps.values() for p in f.pads) else net
res = 0.1
g = Grid(b, net, box, res, w, vsz, vdr, b.copper)
print(f"net {net}  box {box}  via {vsz}/{vdr}  track w {w}   cols x0={box[0]} step {res}")
rows = []
for j in range(g.H):
    y = g.y0 + j * res
    line = ''
    for i in range(g.W):
        k = j * g.W + i
        if LY:
            ch = '#' if g.blk[LY][k] else ('V' if not g.vblk[k] else '.')
        elif not g.vblk[k]:
            ch = 'V' if not g.vpour[k] else 'v'
        else:
            free = [ly for ly in b.copper if not g.blk[ly][k]]
            ch = ''.join(ly[0] if ly[0] != 'I' else ly[2] for ly in free)[:1] if free else '#'
            ch = {'F': 'f', 'B': 'b', '1': '1', '2': '2'}.get(ch, ch) if len(free) == 1 else ('+' if free else '#')
        line += ch
    print(f"{y:7.2f} {line}")
