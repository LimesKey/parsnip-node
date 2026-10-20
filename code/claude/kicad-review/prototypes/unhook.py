#!/usr/bin/env python3
"""unhook.py BOARD OUT REF...: rip the copper that only served REF... - seed with every
track/via touching their pads, then ripup's prune with those parts treated as absent."""
import sys, json, argparse, tempfile
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board, pad_box
from kpcb_edit import c_ripup

bf, out, refs = sys.argv[1], sys.argv[2], sys.argv[3:]
b = Board(bf)
boxes = [(p['net'], pad_box(p)) for r in refs for p in b.fps[r].pads if p['net']]
def hit(net, x, y, r):
    return any(n == net and bb[0] - r <= x <= bb[2] + r and bb[1] - r <= y <= bb[3] + r for n, bb in boxes)
seed = [t['uuid'] for t in b.tracks if hit(t['net'], *t['a'], 0) or hit(t['net'], *t['b'], 0)]
seed += [v['uuid'] for v in b.vias if hit(v['net'], v['x'], v['y'], 0)]
for r in refs:
    del b.fps[r]
rep = tempfile.NamedTemporaryFile('w', suffix='.json', delete=False)
json.dump({'violations': [{'type': 'shorting_items', 'items': [{'uuid': u} for u in seed]}]}, rep)
rep.close()
a = argparse.Namespace(drc=rep.name, types='shorting_items', box='', args=[], max=20, out=out)
c_ripup(b, a)
