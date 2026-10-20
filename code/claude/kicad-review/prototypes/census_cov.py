import math
from collections import defaultdict
from kpcb_board import PolyIndex, pt_seg_dist
from smooth2 import in_pad
_cache = {}


def _idx(b):
    if id(b) not in _cache:
        pads, vias, fills = defaultdict(list), defaultdict(list), defaultdict(list)
        for f in b.fps.values():
            if f.placed:
                for p in f.pads:
                    if p['net']:
                        pads[p['net']].append(p)
        for v in b.vias:
            vias[v['net']].append(v)
        for fl in b.fills:
            fills[(fl['net'], fl['layer'])].append((fl['bbox'], PolyIndex(fl['pts'])))
        _cache[id(b)] = (pads, vias, fills)
    return _cache[id(b)]


def covered(b, net, ly, pt, w, mine):
    """pt's w/2 disk is inside other same-net copper: a pad, via, wider track or fill"""
    pads, vias, fills = _idx(b)
    ring = [pt] + [(pt[0] + w / 2 * math.cos(k * math.pi / 4), pt[1] + w / 2 * math.sin(k * math.pi / 4)) for k in range(8)]
    for p in pads[net]:
        if (p['drill'] or ly in p['layers'] or '*.Cu' in p['layers']) and in_pad(p, pt, -w / 2):
            return True
    if any(math.dist(pt, (v['x'], v['y'])) <= v['size'] / 2 - w / 2 for v in vias[net]):
        return True
    for i, t in enumerate(b.tracks):
        if t['net'] == net and t['layer'] == ly and i not in mine and t['w'] > w and pt_seg_dist(pt, t['a'], t['b']) <= (t['w'] - w) / 2:
            return True
    for bb, pi in fills[(net, ly)]:
        if bb[0] <= pt[0] <= bb[2] and bb[1] <= pt[1] <= bb[3] and all(x in pi for x in ring):
            return True
    return False
