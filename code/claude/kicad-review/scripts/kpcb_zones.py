"""kpcb.py `zones`: per-layer pour coverage, or coverage under one footprint."""
import json
from collections import defaultdict
from kcommon import GND_RE, rail_voltage
from kpcb_board import (_sample_grid, bbox, ctr, hit, inside, overlap_area, point_in_poly,
                        poly_area)

def _zones_under(b, a):
    """`zones REF...`: point-sample whether a filled net actually covers a
    footprint's courtyard, instead of just eyeballing the whole-board table.
    Closes the RF-reference-plane question ("is GND continuous under U9")
    that the bbox-margin view can only infer from dominant-island + all-edges-
    reached. 11x11 samples per footprint; each sample tests every fill whose
    own bbox overlaps the courtyard, so a plane broken into several polygons
    still counts as covered wherever any piece of it reaches."""
    fail = 0
    for ref in a.args:
        f = b.fps.get(ref)
        if not f:
            print(f"{ref}: no such footprint"); fail = 1; continue
        rect = f.crtyd
        # an edge-mount SMA/USB-C courtyard overhangs the outline; copper can't
        # exist there, so sampling it would read a good launch as MOSTLY MISSING.
        # Grid only the on-board part (then drop any point a notch still excludes).
        o = b.outline or rect
        on = (max(rect[0], o[0]), max(rect[1], o[1]), min(rect[2], o[2]), min(rect[3], o[3]))
        # ponytail: fixed 0.5 mm inset on clipped sides for the pour's edge pullback;
        # read the zone/edge clearances if a board pulls back further
        e = 0.5
        g = (on[0] + e * (on[0] > rect[0]), on[1] + e * (on[1] > rect[1]),
             on[2] - e * (on[2] < rect[2]), on[3] - e * (on[3] < rect[3]))
        pts = _sample_grid(g, 11) if g[0] < g[2] and g[1] < g[3] else []
        if b.rings:
            pts = [p for p in pts if inside(p, b.rings)]
        off = 1 - overlap_area(on, rect) / max(f.area, 1e-9) if pts else 1.0
        print(f"{ref}: courtyard {rect[2]-rect[0]:.1f} x {rect[3]-rect[1]:.1f} mm "
              f"@ {ctr(rect)[0]:.1f},{ctr(rect)[1]:.1f}"
              + (f"  ({100*off:.0f}% hangs off the board edge, not sampled)" if off > 0.005 else ''))
        if not pts:
            continue
        for ly in b.copper:
            # bbox overlap is only a coarse prefilter: a net's OWN pour can be
            # substantial yet still never actually reach this courtyard (its
            # island bbox just happens to graze it), so score every candidate
            # net and only report ones that land at least one real sample -
            # otherwise "the plane exists somewhere nearby" reads as "broken".
            fills_here = [fl for fl in b.fills if fl['layer'] == ly and hit(fl['bbox'], rect)]
            cand = sorted({fl['net'] for fl in fills_here})
            scored = []
            for net in cand:
                fs = [fl for fl in fills_here if fl['net'] == net]
                hit_n, holes = 0, []
                for p in pts:
                    if any(point_in_poly(p, fl['pts']) for fl in fs):
                        hit_n += 1
                    else:
                        holes.append(p)
                if hit_n:
                    scored.append((net, hit_n, holes))
            if not scored:
                print(f"    {ly:<8} no fill actually reaches this footprint's courtyard")
                fail = 1
                continue
            for net, hit_n, holes in scored:
                pct = 100 * hit_n / len(pts)
                # a handful of misses is normal: every non-plane pad/via under
                # the part carries its own clearance moat. Only a MAJORITY
                # miss (a genuinely broken or absent reference) fails the exit
                # code; a few holes just get listed for a look, not a verdict.
                if hit_n == len(pts):
                    tag = 'continuous'
                elif not (GND_RE.match(net.split('/')[-1]) or rail_voltage(net.split('/')[-1]) is not None):
                    tag = 'local signal pour, not a reference plane'   # no verdict
                elif pct >= 50:
                    tag = 'has gaps (normal near non-plane pads/vias unless clustered)'
                else:
                    tag = 'MOSTLY MISSING'
                    fail = 1
                print(f"    {ly:<8} {net:<10} {pct:5.0f}% of samples covered  ({tag})")
                if holes:
                    shown = ' '.join(f'{x:.1f},{y:.1f}' for x, y in holes[:4])
                    more = f' ...+{len(holes)-4}' if len(holes) > 4 else ''
                    print(f"        uncovered near: {shown}{more}")
    print("\nSamples are an 11x11 grid inside the courtyard, not the whole fill - a scattered few\n"
          "misses are the normal clearance moat around any non-plane pad or via under the part,\n"
          "not a defect; only a large contiguous run of misses or <50% total is flagged. 100% is\n"
          "on this footprint only, not a guarantee elsewhere on the plane. Fills are the LAST\n"
          "SAVED state (see `zones` with no args).")
    return 2 if fail else 0

def c_zones(b, a):
    """Zone-fill coverage per copper layer, from the fills cached in the board.

    Reports gross copper area over board area, island count, largest-island
    share, and the fill's bounding-box margins to each board edge - enough to
    catch a pour that is fragmented, missing a layer, or nowhere near an edge.
    It is geometry on the LAST SAVED fill, not a live refill: a zone shows 0
    if it was never filled or a part moved after the last Fill All Zones.

    `zones REF...` instead point-samples whether a net's fill actually covers
    that footprint's courtyard - see `_zones_under`."""
    if a.args:
        return _zones_under(b, a)
    o = b.outline
    board_area = max((poly_area(r) for r in b.rings), default=0.0) or \
        ((o[2] - o[0]) * (o[3] - o[1]) if o else 0.0)
    by_layer = defaultdict(lambda: defaultdict(list))     # layer -> net -> [fill]
    for fl in b.fills:
        by_layer[fl['layer']][fl['net']].append(fl)
    declared = {(net, ly) for net, lays in b.zones for ly in lays}
    filled = set()
    rows = []
    for ly in b.copper:
        nets = by_layer.get(ly)
        if not nets:
            rows.append((ly, '-', 0.0, 0, 0.0, None))
            continue
        for net, fs in sorted(nets.items()):
            filled.add((net, ly))
            area = sum(f['area'] for f in fs)
            fb = bbox([p for f in fs for p in f['pts']])
            largest = max((f['area'] for f in fs), default=0.0)
            rows.append((ly, net, area, len(fs), largest, fb))
    unfilled = sorted(declared - filled)

    if a.json:
        print(json.dumps({'board_area_mm2': round(board_area, 1), 'outline': o,
                          'layers': [{'layer': ly, 'net': net,
                                      'area_mm2': round(ar, 1),
                                      'coverage_pct': round(100 * ar / board_area, 1) if board_area else 0,
                                      'islands': isl,
                                      'largest_island_pct': round(100 * lg / ar, 1) if ar else 0,
                                      'fill_bbox': [round(v, 2) for v in fb] if fb else None}
                                     for ly, net, ar, isl, lg, fb in rows if net != '-'],
                          'declared_unfilled': [{'net': n, 'layer': l} for n, l in unfilled]},
                         indent=1))
        return 0

    print(f"{b.path}: zone fill coverage")
    if o:
        print(f"board {board_area:.0f} mm2 (outline ring)   bbox "
              f"x {o[0]:.1f}..{o[2]:.1f}  y {o[1]:.1f}..{o[3]:.1f}")
    print(f"\n{'layer':<8} {'net':<8} {'cover':>6} {'isl':>4} {'top1':>5}  "
          f"uncovered margins (mm, of fill bbox)")
    for ly, net, area, isl, largest, fb in rows:
        if net == '-':
            print(f"{ly:<8} {'-':<8} {'0%':>6} {'0':>4} {'-':>5}  (no fill on this layer)")
            continue
        cov = 100 * area / board_area if board_area else 0
        top1 = 100 * largest / area if area else 0
        marg = (f"top {fb[1]-o[1]:.1f}  bot {o[3]-fb[3]:.1f}  "
                f"L {fb[0]-o[0]:.1f}  R {o[2]-fb[2]:.1f}") if (fb and o) else ''
        print(f"{ly:<8} {net:<8} {cov:5.0f}% {isl:>4} {top1:4.0f}%  {marg}")
    if unfilled:
        print("\nDECLARED BUT NOT FILLED (never filled, or emptied on last save):")
        for net, ly in unfilled:
            print(f"  {net} on {ly}")
    print("\nCoverage is gross copper area / board area; a low % or a small top-island "
          "share means a\nfragmented pour. Margins are of the fill's bounding box, so they "
          "only flag when NOTHING\nreaches that edge (one sliver hides the gap) - eyeball a "
          "render for interior voids.\nFills are the LAST SAVED state: refill in KiCad "
          "(Edit > Fill All Zones, save) or run\n`kdrc.py FILE drc` (refills in memory) if a "
          "zone reads 0 or parts moved since the fill.")
    return 0
