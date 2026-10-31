#!/usr/bin/env python3
"""
kpcb.py v2 - placement review for a KiCad board (.kicad_pcb, KiCad 7-10).

The placement-stage companion to knet.py. knet answers "what is wired to what";
kpcb answers "where is it, and is that a sane place to put it". Nothing here
needs routing to exist - every check runs on footprint geometry alone.

Indexes built from the board file:
  fps      ref -> footprint, layer, x/y/rot, sheet, attrs, dnp, courtyard bbox
  pads     ref -> [(num, net, board xy, size, pinfunction, pintype)]
  nets     net name -> [(ref, pad)]        taken from the pads, no .net needed
  outline  Edge.Cuts segments + closed rings, for inside/outside and clearance

Commands:
  kpcb.py FILE summary                board size, stackup, what is placed, per sheet
  kpcb.py FILE check                  rule-based placement review (see RULES)
  kpcb.py FILE where U8 J7            position, courtyard, edge distance, neighbours
  kpcb.py FILE where 130,60 -r 8      ...same, around a coordinate
  kpcb.py FILE map                    ASCII occupancy map of the board
  kpcb.py FILE sheet                  per-sheet placement cohesion + bounding box
  kpcb.py FILE unplaced               what is still parked off the outline
  kpcb.py FILE span                   nets ranked by how far apart their pads sit
  kpcb.py FILE ic                     which parts `ic REF` can advise on
  kpcb.py FILE ic U13                 WHERE to put a regulator's passives, with
                                      an ASCII picture of the recommendation
  kpcb.py FILE viapad [--signal] [--min N]
                                      SMD pads a via drill reaches into, small first
                                      (advice from the board's via protection)
  kpcb.py FILE where F5.1 BT1.1       a PAD's absolute centre, size, net; two specs
                                      also print the distance between them
  kpcb.py FILE net NET                every pad on a net (absolute xy), copper per
                                      layer, vias, zones, extent
  kpcb.py FILE height [REF...]        3D model height per part + the Z stack (cell +
                                      board + tallest part over it), via KiCad's GLB
                                      export of the STEP models; no model = UNKNOWN
  kpcb.py FILE view REF.. [-r 5]      cropped PNG of the board layers around parts (or
                                      X,Y / --box X0,Y0,X1,Y1), from kicad-cli's plot
  kpcb.py FILE silk                   silkscreen review: designators/texts over pads, under
                                      bodies, overlapping, off the edge, too small, ambiguous,
                                      mirrored or upside down; logos (art layer, body hiding it)
  kpcb.py FILE refdes [REF..] -o OUT  re-place designators beside their parts, clear of all
                                      that (--all, --show hidden ones, --size H --thick T)
  kpcb.py FILE pinlabels REF [NAME..] -o OUT   an aligned column of pin labels beside REF's
                                      pads (names: NAMEs, else the nets through fuses/0R)
  kpcb.py FILE label TEXT X,Y|--near REF -o OUT   TEXT at the nearest clear silk spot
  kpcb.py FILE movecheck REF X Y [ROT] what a move would newly break: foreign copper inside
                                      the clearance, copper to edge, courtyard overlap;
                                      `REF --scan x=X y=A..B`, `via X,Y NX,NY`, `--no-drag`. Read-only.
  kpcb.py FILE freebox SIDE W H       where a W x H silk box fits on side f|b (clear of
                                      courtyards, through pads, silk/art; 1 mm from edge)
  kpcb.py FILE tidy                   near-miss alignment (rows/columns, pitch, hole insets,
                                      rotations, centring), each run through movecheck
  kpcb.py FILE rf [NET...]            50-ohm trace review: width necks, microstrip Zo
                                      from the stackup, same-layer GND gap, reference
                                      plane under it, GND via fence vs lambda/20
  kpcb.py FILE refslots [NET...]      outer-layer signal tracks over a gap in the plane below:
                                      mm over GND / another plane / gap, and what cuts each gap

Writers (always to a new file, -o OUT; verify the copy with kdrc.py):
  kpcb.py FILE fixlines -o OUT        undo the 10.99 fp_line frame bug (KiCad draws the
                                      silk/courtyard/fab lines near the page origin)
  kpcb.py FILE renet -o OUT           netless tracks/vias take the one net their ends touch
  kpcb.py FILE move REF X Y [ROT] -o OUT   re-place a footprint (--flip: to the other side;
                                      --no-drag: KiCad's Move, tracks and in-pad vias stay)
  kpcb.py FILE ripup NET.. | --drc R.json -o OUT   delete tracks/vias (KiCad-flagged or by net,
                                      --box), prune what dangles, drop their teardrops; `[GND]
                                      --inpour`: plane tracks wholly inside their own fill;
                                      `[NET] --deadend`: copper hanging off one point, no pad
  kpcb.py FILE airwires [NET...]      what is left to route: per net, each missing link
                                      between copper pieces, nearest copper, gap
  kpcb.py FILE route NET... [-o OUT]  grid A* router (8 directions, through vias) through
                                      clearance-inflated foreign copper; dry run without -o
  kpcb.py FILE place REF [--near REF] [-o OUT]  where a part fits (both sides, 4 rotations),
                                      ranked by ratsnest length, copper-checked
  kpcb.py FILE pairs [NET...]         diff pairs (X+/X-, XP/XN): leg lengths, skew, vias,
                                      coupled length and edge gap vs the netclass
  kpcb.py FILE unhook REF... -o OUT   rip only the copper that served these parts (prune
                                      with them absent; never a whole net)
  kpcb.py FILE vscan NET X0,Y0,X1,Y1  the router's model of a box as ASCII: legal via spots,
                                      free layers per cell (--width, --via, --layers)
  kpcb.py FILE smooth [-o OUT]        pull track chains tight and octilinear (--overlaps,
                                      --corners, --square, --pads, --jogs [--nudge], --twins: one
                                      other pass; --all: every pass but jogs/twins until nothing changes)
  kpcb.py FILE tracks [--view P.png]  trace-quality census: visible corners, off-45, width
                                      steps, tiny segments, ends at a pad edge, runs joined only
                                      through a pad, twin runs, lane jogs, detours, GND tracks
                                      inside their pour, and why smooth left each (--box: a region)
  kpcb.py FILE reroute [NET..] -o OUT rip a detour net and route it again; kept when better
  kpcb.py FILE congest [--all]        routing demand vs free copper per cell: where it jams
  kpcb.py FILE fanout REF             per pin of a dense part: escape, dogbone via, or boxed
  kpcb.py FILE widen [NET..] -o OUT   grow under-width tracks to their netclass width
                                      (--uniform-power: a power run at its widest where clear;
                                      --uniform: a sliver narrower than its neighbours to theirs)
  kpcb.py FILE loops [NET..] [-o OUT] redundant track chains on signal nets (-o deletes)
  kpcb.py FILE diff OLD.kicad_pcb     what changed since OLD: footprints, silk text, vias,
                                      zones, teardrops, tracks (+ length, per-net series R);
                                      --box a region, -v each net's -/+ segments

`ic` is the one command that suggests rather than judges. It reads the IC's own
pad coordinates - there is no per-part template - classifies its pins by name,
finds its passives, and places each one by the rule that actually governs it:
input caps straddling the tightest VIN/PGND pad pair on the outside face of the
package (that is the high-di/dt loop, made small), the inductor hard against the
SW pads, output caps as a bank at the inductor's far pad, the feedback divider
run out the far side away from SW, boot and bias caps at their own pins. With
the IC still in the parked pile it anchors on whichever passive is already
placed and reports where the IC itself then goes.

Thresholds (mm) - defaults are conservative; override per board in kpcb.json:
  --edge 0.5      courtyard-to-board-edge minimum
  --hole 1.5      keepout margin beyond a mounting hole's own pad/drill radius
  --conn 10.0     how far a connector may sit from the nearest edge
  --rf 8.0        RF part / antenna net to switching node
  --therm 8.0     heat source to heat-sensitive part
  --bypass 3.0    supply pin to its nearest bypass cap
  --clear 0.0     extra margin added to every courtyard-vs-courtyard test
  --gap 0.25      courtyard-to-courtyard gap inside an `ic` suggestion
  --fb 4.0        `ic`: feedback part to switching node
  --tol 1.0       `ic`: slop before a placed part reads as OK in its slot
  --span 0        NETSPAN threshold (0 = half the board diagonal)
  --fanout 8      a net with more nodes than this is a rail, not a signal

Project config: kpcb.json next to the board persists board defaults and mutes
confirmed non-bugs, exactly like knet.json does for the netlist, e.g.
  {"edge": 0.3, "bypass": 4.0, "suppress": ["CONNACC:J8", "UNPLACED"]}
Precedence: built-in defaults < kpcb.json < command line.

Output is capped on purpose. A board has hundreds of footprints and an
uncapped pairwise check would bury the answer: findings that share a shape
fold into one `tail [N]: refs` line, each rule stops after --max lines with a
`+N more` tail, and neighbour lists are capped too. Counts in the tails stay
accurate even when not every item is printed.

Exit codes: 0 clean, 1 not found, 2 `check` found an ERROR, 3 bad file.
"""
import sys, os, re, json, math, glob, argparse, signal, time
from collections import Counter, defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kcommon import refrange, natkey, trunc, prefix, unesc_disp, print_findings, parse_args
except ImportError:                                     # pragma: no cover
    print("kpcb.py needs kcommon.py beside it (shared parser + finding formatter)",
          file=sys.stderr)
    raise
# The heavy commands live in kpcb_<cmd>.py beside this; the Board model in kpcb_board.py.
from kpcb_board import (_find_pad, _resolve_net, bbox, Board, box_dist, ctr, hit, inside, poly_dist,
                        pad_box, pt_seg_dist, sheet_of)
from kpcb_check import c_check, gen_findings, net_spans
from kpcb_copper import Copper
from kpcb_amp import c_ampacity
from kpcb_zones import c_zones
from kpcb_viapad import c_viapad
from kpcb_rf import c_rf, c_refslots
from kpcb_height import c_height
from kpcb_ic import c_ic, role_pads
from kpcb_view import c_view
from kpcb_silk import c_silk, c_refdes
from kpcb_label import c_pinlabels, c_label
from kpcb_move import c_movecheck
from kpcb_freebox import c_freebox
from kpcb_tidy import c_tidy
from kpcb_edit import c_fixlines, c_renet, c_move, c_ripup, c_edit, c_unhook
from kpcb_route import c_airwires, c_pairs, c_route, _segd
from kpcb_place import c_place
from kpcb_power import c_fatten, c_stitch, c_vscan
from kpcb_smooth import c_smooth
from kpcb_tracks import c_tracks, c_widen, c_loops
from kpcb_diff import c_diff
from kpcb_dense import c_congest, c_fanout, c_reroute


def c_summary(b, a):
    P = b.placed()
    un = [f for f in b.fps.values() if not f.placed]
    o = b.outline
    w, h = (o[2] - o[0], o[3] - o[1]) if o else (0, 0)
    used = {s: sum(f.area for f in P if f.back == s) for s in (False, True)}
    if a.json:
        print(json.dumps({'path': b.path, 'outline': o, 'w': w, 'h': h,
                          'copper': b.copper, 'footprints': len(b.fps),
                          'placed': len(P), 'unplaced': len(un),
                          'courtyard_mm2': {'front': round(used[False], 1),
                                            'back': round(used[True], 1)},
                          'zones': [{'net': n, 'layers': l} for n, l in b.zones]}, indent=1))
        return
    print(f"board  : {b.path}")
    print(f"gen    : {b.gen.strip()}")
    if o:
        print(f"outline: {w:.2f} x {h:.2f} mm   x {o[0]:.2f}..{o[2]:.2f}  y {o[1]:.2f}..{o[3]:.2f}"
              f"   {len(b.rings)} ring(s), {len(b.edge_segs)} segs")
    else:
        print("outline: NONE on Edge.Cuts - every placement check is disabled")
    print(f"copper : {len(b.copper)} layers  {' '.join(b.copper)}")
    print("origins: " + '  '.join(f"{k} {x:g},{y:g}" for k, (x, y) in b.origins.items() if k != 'page')
          + "   (`where --origin grid|aux` quotes what KiCad's Properties dialog shows)")
    for n, l in b.zones:
        print(f"zone   : {n:<10} {' '.join(l)}")
    if b.teardrops:
        print(f"teardrops: {b.teardrops} (zone-type fillets, left out of every pour figure)")
    front = sum(1 for f in P if not f.back)
    print(f"\nfootprints: {len(b.fps)}   placed {len(P)} ({front} front / {len(P)-front} back)"
          f"   unplaced {len(un)}")
    if o and w * h:
        print(f"courtyard used: front {used[False]:.0f} mm2 ({100*used[False]/(w*h):.0f}%), "
              f"back {used[True]:.0f} mm2 ({100*used[True]/(w*h):.0f}%) "
              f"of the {w*h:.0f} mm2 outline bbox   [sum of courtyards, so >100% "
              f"means they overlap]")
    if o and w * h:
        left = sum(f.area for f in un)
        free = 2 * w * h - used[False] - used[True]      # two sides, not one
        print(f"still to place: {left:.0f} mm2 of courtyard across {len(un)} part(s) "
              f"vs {free:.0f} mm2 free over two sides "
              f"[{'fits with room' if free > left * 2 else 'tight' if free > left else 'DOES NOT FIT as-is'};"
              f" crude - ignores routing space, keepouts and part height]")
    bys = defaultdict(lambda: [0, 0])
    for f in b.fps.values():
        bys[sheet_of(f)][0 if f.placed else 1] += 1
    print(f"\n{'sheet':<20} {'placed':>6} {'left':>6}")
    for s in sorted(bys, key=natkey):
        p, u = bys[s]
        print(f"{trunc(s,20):<20} {p:>6} {u:>6}")
    dnp = sorted([f.ref for f in b.fps.values() if f.dnp], key=natkey)
    print(f"\nDNP ({len(dnp)}): {refrange(dnp) or 'none'}")
    holes = sorted([f.ref for f in b.fps.values() if b.is_hole(f)], key=natkey)
    print(f"mounting holes ({len(holes)}): {refrange(holes) or 'none'}")
    print("\nlargest placed parts:")
    for f in sorted(P, key=lambda f: -f.area)[:8]:
        print(f"  {f.area:>7.1f} mm2  {f.ref:<6} {trunc(f.value,22):<22} "
              f"{f.x:>7.2f},{f.y:<7.2f} {'B' if f.back else 'F'}")

def c_unplaced(b, a):
    un = [f for f in b.fps.values() if not f.placed]
    if not un:
        print("everything is inside the outline"); return
    bys = defaultdict(list)
    for f in un:
        bys[sheet_of(f)].append(f.ref)
    print(f"{len(un)} footprint(s) still parked off the board:\n")
    for s in sorted(bys, key=natkey):
        print(f"  {trunc(s,20):<20} {len(bys[s]):>3}  {refrange(bys[s])}")
    big = sorted([f for f in un if f.area > 8], key=lambda f: -f.area)[:10]
    if big:
        print("\nlargest still unplaced (place these first - they set the floorplan):")
        for f in big:
            print(f"  {f.area:>7.1f} mm2  {f.ref:<6} {trunc(f.value,26):<26} {trunc(f.fp,34)}")

def c_sheet(b, a):
    """Placement cohesion per schematic sheet. A sheet whose parts are scattered
    across the board is usually a floorplan mistake, not a routing one."""
    want = a.args[0] if a.args else None
    bys = defaultdict(list)
    for f in b.fps.values():
        if want and not sheet_of(f).lower().startswith(want.lower()):
            continue
        bys[sheet_of(f)].append(f)
    if not bys:
        print(f"no sheet matches {want!r}"); return 1
    print(f"{'sheet':<18} {'plc':>4} {'left':>4} {'bbox (x0,y0 x1,y1)':<30} {'spread':>7}  outliers")
    for s in sorted(bys, key=natkey):
        fl = bys[s]
        P = [f for f in fl if f.placed]
        if not P:
            print(f"{trunc(s,18):<18} {0:>4} {len(fl):>4} {'-':<30} {'-':>7}")
            continue
        bb = bbox([(f.x, f.y) for f in P])
        c = ctr(bb)
        # keyed: two parts at the same distance used to fall through to
        # comparing the FP objects themselves and crash the whole command
        d = sorted(((math.hypot(f.x - c[0], f.y - c[1]), f) for f in P),
                   key=lambda t: (t[0], natkey(t[1].ref)), reverse=True)
        med = d[len(d) // 2][0]
        out = [f.ref for dist, f in d[:3] if dist > max(3 * med, 15)]
        print(f"{trunc(s,18):<18} {len(P):>4} {len(fl)-len(P):>4} "
              f"{f'{bb[0]:.0f},{bb[1]:.0f} {bb[2]:.0f},{bb[3]:.0f}':<30} "
              f"{max(bb[2]-bb[0], bb[3]-bb[1]):>6.0f}m  {' '.join(out) or '-'}")
    print("\nspread = longer side of the placed bbox; outliers are >3x the median "
          "distance from\nthat sheet's centroid - a part that drifted away from its block.")

def c_where(b, a):
    if not a.args:
        print("where needs a ref or an x,y coordinate", file=sys.stderr); return 1
    miss, pts = False, []
    ox, oy = b.origins.get(a.origin, (0.0, 0.0))
    if a.origin != 'page':
        print(f"coordinates relative to the {a.origin} origin {ox:g},{oy:g} (KiCad: Display origin "
              f"= {'Grid' if a.origin == 'grid' else 'Drill/place file'}, Y down), in and out")
    O = lambda x, y, n=3: f"{x - ox:.{n}f}, {y - oy:.{n}f}"         # noqa: E731
    for spec in a.args:
        m = re.match(r'^(-?[\d.]+)\s*,\s*(-?[\d.]+)$', spec)
        if m:
            p = (float(m.group(1)) + ox, float(m.group(2)) + oy)
            pts.append((spec, p))
            box = (p[0], p[1], p[0], p[1])
            if b.edge_segs:
                where = 'inside' if inside(p, b.rings) else 'OUTSIDE'
                d = f"   edge {min(pt_seg_dist(p, u, v) for u, v in b.edge_segs):.2f} mm"
            else:
                where, d = 'no Edge.Cuts, so inside/outside is', ''
            print(f"\n=== {O(*p, 2).replace(' ', '')}   {where} the outline{d}")
            _neigh(b, box, None, a)
            continue
        pad = _find_pad(b, spec)
        if pad:
            f, p = pad
            pts.append((spec, (p['x'], p['y'])))
            ly = ' '.join(l for l in p['layers'] if l.endswith('.Cu')) or ' '.join(p['layers'])
            print(f"\n=== {spec}  {p['fn'] or ''}  on {unesc_disp(p['net']) or '(no net)'}")
            print(f"  at        : {O(p['x'], p['y'])}  ({'absolute' if a.origin == 'page' else a.origin + ' origin'})"
                  f"  pad rot {p['prot']:g}")
            bb = pad_box(p)                          # the pad's own size is before its rotation
            print(f"  pad       : {p['kind']} {p['shape']} {p['sx']:.2f} x {p['sy']:.2f} mm"
                  + (f"  drill {p['drill']:.2f}" if p['drill'] else '') + f"  {ly}"
                  + (f"  (on the board: {bb[2] - bb[0]:.2f} x {bb[3] - bb[1]:.2f}, x {bb[0]:.2f}..{bb[2]:.2f}"
                     f" y {bb[1]:.2f}..{bb[3]:.2f})" if p['prot'] % 180 else ''))
            if b.edge_segs:
                print(f"  edge dist : {min(pt_seg_dist((p['x'], p['y']), u, v) for u, v in b.edge_segs):.2f} mm")
            others = sorted(((math.hypot(q['x'] - p['x'], q['y'] - p['y']), g.ref, q)
                             for g in b.fps.values() for q in g.pads
                             if q is not p and q['net'] and q['net'] == p['net']), key=lambda t: t[0])
            if others:
                print("  same net  : " + ', '.join(f"{r}.{q['num']} {d:.1f} mm" for d, r, q in others[:6])
                      + (f" ... +{len(others)-6}" if len(others) > 6 else ''))
            continue
        f = b.fps.get(spec)
        if not f:
            near = [r for r in b.fps if r.upper().startswith(spec.upper())][:6]
            print(f"{spec}: NOT FOUND" + (f"; did you mean {' '.join(near)}" if near else ""))
            miss = True; continue
        c = f.crtyd
        print(f"\n=== {f.ref}  {f.value}{'   [DNP]' if f.dnp else ''}"
              f"{'' if f.placed else '   [UNPLACED - parked off the outline]'}")
        print(f"  footprint : {f.fp}")
        print(f"  sheet     : {f.sheet}")
        print(f"  at        : {O(f.x, f.y)}  rot {f.rot:g}  layer {f.layer}"
              + (f"  scale {f.scale[0]:g} x {f.scale[1]:g}" if f.scale != (1.0, 1.0) else '')
              + (f"  (padless, art on {'B' if f.back else 'F'})" if not f.pads and f.art else ''))
        print(f"  courtyard : {O(c[0], c[1], 2).replace(' ', '')} .. {O(c[2], c[3], 2).replace(' ', '')}  "
              f"({c[2]-c[0]:.2f} x {c[3]-c[1]:.2f} mm){'' if f.crtyd_real else '  [NO CrtYd - pads+fab bbox]'}")
        if f.fab:
            fb = f.fab
            print(f"  fab body  : {O(fb[0], fb[1], 2).replace(' ', '')} .. {O(fb[2], fb[3], 2).replace(' ', '')}  "
                  f"({fb[2]-fb[0]:.2f} x {fb[3]-fb[1]:.2f} mm)  [Fab layer only: the housing]")
        if f.edge is not None:
            print(f"  edge dist : {f.edge:.2f} mm" + ("   <-- ON/ACROSS THE EDGE" if f.edge <= 0.001 else ""))
        tags = [t for t, ok in (('RF', b.is_rf(f)), ('hot', b.is_hot(f)),
                                ('heat-sensitive', b.is_sens(f)), ('connector', b.is_conn(f)),
                                ('mounting hole', b.is_hole(f))) if ok]
        if tags:
            print(f"  class     : {', '.join(tags)}")
        nets = sorted({p['net'] for p in f.pads if p['net']}, key=natkey)
        print(f"  nets ({len(nets)}) : {trunc(' '.join(unesc_disp(n) for n in nets), 300)}")
        _neigh(b, c, f, a)
        pts.append((spec, (f.x, f.y)))
    if len(pts) == 2:
        (n1, p1), (n2, p2) = pts
        print(f"\n{n1} -> {n2}: {math.dist(p1, p2):.2f} mm "
              f"(dx {p2[0]-p1[0]:+.2f}, dy {p2[1]-p1[1]:+.2f})")
    return 1 if miss else 0

def _neigh(b, box, self_fp, a):
    """Neighbour list, capped. The whole point of this tool is to answer a
    placement question in a few lines; dumping every part within a radius on a
    440-footprint board would defeat that."""
    r = a.radius
    rows = []
    for g in b.fps.values():
        if g is self_fp or not g.placed:
            continue
        d = box_dist(box, g.crtyd)
        ov = d <= 0 and hit(box, g.crtyd)
        if d <= r and self_fp is not None and (self_fp.cpoly or g.cpoly):
            d, ov = poly_dist(self_fp.outline, g.outline)   # a cross/L courtyard is not its bbox
        if d <= r:
            rows.append((d, g, ov))
    rows.sort(key=lambda t: (t[0], natkey(t[1].ref)))
    print(f"  neighbours within {r:g} mm ({len(rows)}):")
    for d, g, ov in rows[:a.max]:
        side = 'B' if g.back else 'F'
        # same-side contact is what `check` calls OVERLAP; a cross-side one is
        # just a projection, so label it differently and do not cry wolf
        flag = ''
        if g.art and not g.pads:
            flag = '  (art)'                  # a logo has no body to clash with; `silk` covers it
        elif ov:
            flag = '  OVERLAP' if (self_fp is None or g.back == self_fp.back) \
                   else '  (overlaps, opposite side)'
        print(f"    {d:>6.2f} mm  {g.ref:<6} {side} {trunc(g.value,20):<20} {trunc(g.fp,30)}{flag}")
    if len(rows) > a.max:
        print(f"    ... +{len(rows)-a.max} more within {r:g} mm ({len(rows)} total) "
              f"- raise --max or lower -r")

def c_map(b, a):
    """ASCII occupancy. Cheap floorplan overview: which block owns which corner
    and where the free space is, in ~40 lines instead of a 400-row table.

    One side at a time on purpose. Painting both onto one grid turned most of
    this board into `*` - the back-side cells sit under the front-side module -
    which is exactly the kind of output that looks informative and says
    nothing."""
    if not b.outline:
        print("no board outline"); return 1
    if a.layers:
        return _cu_map(b, a)
    o = b.outline
    cols = max(20, a.cols)
    cw = (o[2] - o[0]) / cols
    ch = cw * 2.0                      # terminal cells are about twice as tall
    nrows = max(4, int(math.ceil((o[3] - o[1]) / ch)))
    want = (a.side or 'f').lower()[0]
    if want not in ('f', 'b'):
        print("--side takes f or b", file=sys.stderr); return 1
    P = [f for f in b.placed() if f.back == (want == 'b')]
    other = len(b.placed()) - len(P)
    keys, used = {}, set()
    for sh in sorted({sheet_of(f) for f in P}, key=natkey):
        stem = sh.split('/')[-2] if sh.count('/') > 1 else sh
        for c in re.sub(r'[^A-Za-z]', '', stem) + 'abcdefghijklmnop':
            if c.upper() not in used:
                keys[sh] = c.upper(); used.add(c.upper()); break
    grid = [[' '] * cols for _ in range(nrows)]
    for f in P:
        k = keys.get(sheet_of(f), '?')
        c = f.crtyd
        for gy in range(max(0, int((c[1] - o[1]) / ch)),
                        min(nrows, int((c[3] - o[1]) / ch) + 1)):
            for gx in range(max(0, int((c[0] - o[0]) / cw)),
                            min(cols, int((c[2] - o[0]) / cw) + 1)):
                cur = grid[gy][gx]
                grid[gy][gx] = k if cur == ' ' else ('*' if cur != k else k)
    for gy in range(nrows):
        for gx in range(cols):
            if grid[gy][gx] == ' ':
                p = (o[0] + (gx + .5) * cw, o[1] + (gy + .5) * ch)
                grid[gy][gx] = '.' if inside(p, b.rings) else ' '
    print(f"{b.path}  {'FRONT' if want == 'f' else 'BACK'} side  "
          f"{o[2]-o[0]:.1f} x {o[3]-o[1]:.1f} mm   1 cell = {cw:.2f} x {ch:.2f} mm   "
          f"x+ right, y+ down")
    print("      +" + "-" * cols + "+")
    for gy, row in enumerate(grid):
        print(f"{o[1]+gy*ch:>6.0f}|" + ''.join(row) + "|")
    print("      +" + "-" * cols + "+")
    print(f"      {o[0]:<.0f}" + " " * max(0, cols - 8) + f"{o[2]:.0f}")
    print("\nkey: " + '  '.join(f"{v}={trunc(k,18)}" for k, v in
                                sorted(keys.items(), key=lambda kv: kv[1])))
    print("     . = inside the outline, free    * = two sheets share the cell    "
          "(blank) = outside")
    print(f"     {other} placed part(s) on the other side are not shown "
          f"(`map --side {'b' if want == 'f' else 'f'}`); "
          f"{len(b.fps)-len(b.placed())} unplaced (`unplaced`)")

def _cu_map(b, a):
    """`map --layers L`: copper occupancy of one copper layer - where a route can still
    go. Tracks, pads and vias are hard copper; a pour is passable (a route cuts it)."""
    from kpcb_dense import cu_raster
    ly = a.layers.split(',')[0]
    if ly not in b.copper:
        print(f"--layers {ly}: not a copper layer ({' '.join(b.copper)})"); return 1
    o, res = b.outline, 0.2
    W, H, hard, soft = cu_raster(b, ly, res)
    cols = max(20, a.cols)
    cw = (o[2] - o[0]) / cols
    ch = cw * 2.0
    rows = max(4, int(math.ceil((o[3] - o[1]) / ch)))
    print(f"{b.path}  {ly} copper   {o[2]-o[0]:.1f} x {o[3]-o[1]:.1f} mm   1 cell = {cw:.2f} x {ch:.2f} mm")
    print("      +" + "-" * cols + "+")
    tot = [0, 0]
    for gy in range(rows):
        line = ''
        for gx in range(cols):
            i0, i1 = int(gx * cw / res), min(W, int((gx + 1) * cw / res))
            j0, j1 = int(gy * ch / res), min(H, int((gy + 1) * ch / res))
            n = max(1, (i1 - i0) * (j1 - j0))
            hc = sum(sum(hard[j * W + i0:j * W + i1]) for j in range(j0, j1)) / n
            sc = sum(sum(soft[j * W + i0:j * W + i1]) for j in range(j0, j1)) / n
            cx, cy = o[0] + (gx + .5) * cw, o[1] + (gy + .5) * ch
            if b.rings and not inside((cx, cy), b.rings):
                line += ' '
                continue
            tot[0] += hc
            tot[1] += 1
            line += '#' if hc >= .6 else '+' if hc >= .3 else ':' if hc >= .12 else \
                    ('~' if sc >= .5 else '.' if hc == 0 else ',')
        print(f"{o[1]+gy*ch:>6.0f}|{line}|")
    print("      +" + "-" * cols + "+")
    print(f"      {o[0]:<.0f}" + " " * max(0, cols - 8) + f"{o[2]:.0f}")
    print(f"\nkey: # >=60% of the cell is track/pad/via copper, + >=30%, : >=12%, , some,\n"
          f"     ~ pour only (passable: a route cuts it, the refill flows round), . free\n"
          f"     {100 * tot[0] / max(1, tot[1]):.0f}% of {ly} inside the outline is hard copper; "
          f"`map --layers L` for another layer, `airwires` for what still needs a path")
    return 0

def c_span(b, a):
    rows = net_spans(b, a)
    if a.json:
        print(json.dumps(rows[:a.max], indent=1)); return 0
    if not rows:
        print("no net has two or more placed pads yet"); return 0
    diag = math.hypot(b.outline[2] - b.outline[0], b.outline[3] - b.outline[1]) \
        if b.outline else 0.0
    print(f"{b.path}: {len(rows)} net(s) with 2+ placed pads, longest first"
          + (f"   board diagonal {diag:.0f} mm" if diag else ""))
    print(f"\n{'span':>7}  {'net':<34} {'plc/tot':>8}  parts")
    for r in rows[:a.max]:
        mark = ' ' if r['placed'] == r['nodes'] else '+'
        print(f"{r['span']:>6.1f}m  {trunc(unesc_disp(r['net']),34):<34} "
              f"{r['placed']:>3}/{r['nodes']:<4}{mark} {trunc(' '.join(r['refs']), 40)}")
    if len(rows) > a.max:
        print(f"  ... +{len(rows)-a.max} shorter net(s) ({len(rows)} total)")
    print(f"\nNets above --fanout {a.fanout:g} nodes (GND, +3V3, ...) are left out on "
          f"purpose - a rail's\nspan is the board, and listing its nodes would bury "
          f"everything else. '+' = not every\nnode is placed yet, so the span will "
          f"only grow. Span is pad bbox diagonal, not trace\nlength; a long span is "
          f"a floorplan question, not yet a routing error.")
    return 0

# ---------------- board vs netlist ----------------

def _canon_net(n):
    """KiCad appends _1, _2 to `unconnected-*` pseudo-nets on the board side but
    not in the netlist export. That is not a difference; treating it as one puts
    a false finding in front of every real one."""
    return re.sub(r'_\d+$', '', n) if n.startswith('unconnected-') else n

def find_netlist(board_path, given):
    """The .net that goes with this board. Same stem first, then any .net in the
    directory - and it says which one it took when there was a choice, because
    silently reviewing against the wrong export is worse than not finding one."""
    if given:
        return (rel(given), 1) if os.path.exists(given) else (None, 0)
    stem = os.path.splitext(os.path.abspath(board_path))[0]
    d = os.path.dirname(stem)
    # a *-merged.net (multi-root export) is authoritative; a bare stem.net can be
    # a single-root export that silently omits whole sheets, so prefer merged.
    for c in (stem + '-merged.net', os.path.join(d, '*-merged.net'),
              stem + '.net', os.path.join(d, '*.net')):
        hits = sorted(glob.glob(c))
        if hits:
            return rel(hits[0]), len(hits)
    return None, 0

def rel(p):
    r = os.path.relpath(p)
    return r if not r.startswith('..') else p

def sync_findings(b, netpath, a):
    """Every difference between what the schematic says and what the board has.

    This runs before anything else is worth reading. A placement review of a
    board that was never re-synced is a review of the wrong circuit, and the
    board file gives no hint that it is stale - its pads carry net names that
    look perfectly valid."""
    from kcommon import Netlist
    n = Netlist(netpath)
    F = []
    def add(sev, rule, msg, refs=()):
        F.append({'severity': sev, 'rule': rule, 'msg': msg, 'refs': list(refs)})

    bf, nf = set(b.fps), set(n.comps)
    # padless and never synced (no sheet): a logo or pasted art, not a component
    art = {r for r in bf - nf if not b.fps[r].pads and not b.fps[r].sheet}
    if art:
        add('INFO', 'SYNCART', f"padless graphic footprints, not components ({len(art)}): "
            f"{trunc(' '.join(sorted((r or '(blank)' for r in art), key=natkey)), 90)}")
    # art with no symbol and no board_only: Update PCB with "Delete footprints with
    # no symbols" deletes it silently (parsnip lost five logos that way)
    for r in sorted((r for r in art if 'board_only' not in b.fps[r].attr), key=natkey):
        f = b.fps[r]
        add('WARN', 'SYNCART', f"{r or '(blank)'} ({f.fp.split(':')[-1]} at {f.x:.1f},{f.y:.1f} "
            f"{f.layer[0]}) lacks board_only: Update PCB deletes it; tick 'Not in schematic'", [r])
    # 'Not in schematic' (board_only): KiCad's own parity check skips these
    bonly = {r for r in bf - nf - art if 'board_only' in b.fps[r].attr}
    if bonly:
        add('INFO', 'SYNCBOARD', f"board-only footprints (attr board_only, not in the "
            f"schematic by design) ({len(bonly)}): {trunc(refrange(sorted(bonly, key=natkey)), 90)}")
    art |= bonly
    if bf - nf - art:
        add('ERROR', 'SYNCPART', f"on the board but not in the netlist "
            f"({len(bf-nf-art)}): {trunc(refrange(sorted(bf-nf-art, key=natkey)), 90)}")
    if nf - bf:
        add('ERROR', 'SYNCPART', f"in the netlist but not on the board "
            f"({len(nf-bf)}): {trunc(refrange(sorted(nf-bf, key=natkey)), 90)}")

    for r in sorted(bf & nf, key=natkey):
        f, c = b.fps[r], n.comps[r]
        # a refdes match is not a link: Update PCB pairs by symbol path, so an
        # unlinked footprint gets deleted and re-added from the schematic
        if c.get('path') and f.sympath != c['path']:
            add('ERROR', 'SYNCLINK', f"{r} is not linked to its symbol: board path "
                f"{f.sympath or '(none)'} / netlist {c['path']}", [r])
        if 'board_only' in f.attr:
            add('WARN', 'SYNCLINK', f"{r} has a schematic symbol but is marked board_only "
                "('Not in schematic'); clear the flag", [r])
        if c['footprint'] and f.fp != c['footprint']:
            add('ERROR', 'SYNCFP', f"{r} footprint differs: board {trunc(f.fp,34)} / "
                                   f"netlist {trunc(c['footprint'],34)}", [r])
        if f.value != c['value']:
            add('WARN', 'SYNCVAL', f"{r} value differs: board {trunc(f.value,20)} / "
                                   f"netlist {trunc(c['value'],20)}", [r])
        if f.dnp != c['dnp']:
            add('WARN', 'SYNCDNP', f"{r} is DNP {'on the board' if f.dnp else 'in the '
                                   'netlist'} only", [r])

    for r in sorted(bf & nf, key=natkey):
        for p in b.fps[r].pads:
            want = n.pinnet.get((r, p['num']))
            if want is None:
                if p['net']:
                    add('WARN', 'SYNCNET', f"{r} pad {p['num']} carries "
                        f"{unesc_disp(p['net'])} but the netlist has no such pin", [r])
                continue
            if _canon_net(p['net'] or '') != _canon_net(want):
                add('ERROR', 'SYNCNET', f"{r}.{p['num']} is on "
                    f"{unesc_disp(p['net']) or '(no net)'} on the board but "
                    f"{unesc_disp(want)} in the netlist", [r])
    return F, n

def c_sync(b, a):
    netpath, cands = find_netlist(b.path, a.args[0] if a.args else None)
    if not netpath:
        print("sync needs the .net export: `kpcb.py board.kicad_pcb sync board.net`\n"
              "(a *.net beside the board is found automatically)", file=sys.stderr)
        return 1
    try:
        F, n = sync_findings(b, netpath, a)
    except Exception as e:
        print(f"could not read {netpath}: {e}", file=sys.stderr); return 3
    if a.json:
        print(json.dumps(F, indent=1))
        return 2 if any(x['severity'] == 'ERROR' for x in F) else 0
    ne = sum(1 for x in F if x['severity'] == 'ERROR')
    ni = sum(1 for x in F if x['severity'] == 'INFO')
    print(f"board   : {b.path}   {len(b.fps)} footprints")
    print(f"netlist : {netpath}   {len(n.comps)} components   exported {n.date}"
          + (f"   [{cands} .net files here; name one to be sure]" if cands > 1 else ""))
    # a SYNCART WARN is a risk to the next Update PCB, not a stale board
    if len(F) == ni + sum(1 for x in F if x['rule'] == 'SYNCART' and x['severity'] == 'WARN'):
        print("\nIN SYNC - same parts, same footprints, same values, same net on every "
              "pad.\nPlacement findings can be trusted to be about the current circuit.")
        if F:
            print_findings(F, '', rules=SYNC_RULES, cap=a.max)
        return 0
    print_findings(F, f"\n{ne} error, {len(F)-ne-ni} warn" + (f", {ni} info" if ni else '') + "\n",
                   rules=SYNC_RULES, cap=a.max)
    if ne:
        print("\nThe board has not been re-synced from the schematic. Run KiCad's "
              "'Update PCB from\nSchematic' first - until then every other finding "
              "here may be about a stale circuit.")
    else:
        print("\nConnectivity matches; only the annotations above differ. Worth "
              "reconciling, but\nplacement findings are still about the right "
              "circuit.")
    return 2 if ne else 0

SYNC_RULES = {
    'SYNCPART': 'component in one file and not the other',
    'SYNCFP':   'footprint assignment differs between board and netlist',
    'SYNCVAL':  'value differs between board and netlist',
    'SYNCDNP':  'DNP flag differs between board and netlist',
    'SYNCNET':  'a pad sits on a different net than the netlist says',
    'SYNCART':  'on the board only, but padless and unsynced: art, not a missing part',
    'SYNCBOARD': 'on the board only, marked board_only (Not in schematic): by design',
    'SYNCLINK': 'refdes matches but the footprint is not linked to that symbol (path)',
}

# ---------------- one-call review ----------------

def c_review(b, a):
    """Everything a fresh session needs before it can say anything useful, in one
    call, ending with the specific next calls worth making.

    The point is not to save printing - it is to save the caller working out
    what to run next from a summary it has not seen yet."""
    b._dead_memo = {}                                # read-only: check and the census share dead_all
    netpath, _ = find_netlist(b.path, a.args[0] if a.args else None)
    if netpath:
        try:
            F, n = sync_findings(b, netpath, a)
            ne = sum(1 for x in F if x['severity'] == 'ERROR')
            F = [x for x in F if x['severity'] != 'INFO']
            if ne:
                print(f"### STOP: board vs {os.path.basename(netpath)} - {ne} error(s)\n")
                print_findings(F, '', rules=SYNC_RULES, cap=5)
                print("\nThe board is stale. `sync` for the full list, then re-sync in "
                      "KiCad.\nEverything below is about the circuit the BOARD has, "
                      "which is not the one you drew.\n")
            else:
                print(f"### in sync with {os.path.basename(netpath)}"
                      f"{f' ({len(F)} non-blocking difference(s), `sync` for detail)' if F else ''}\n")
        except Exception as e:
            print(f"### could not read {netpath}: {e}\n")
    else:
        print("### no .net beside the board - cannot tell whether the board is stale. "
              "Pass one:\n### `kpcb.py board.kicad_pcb review board.net`\n")

    print("### placement"); c_summary(b, a)
    found = gen_findings(b, a)
    print("\n### findings"); rc = c_check(b, a, found)
    print("\n### longest nets"); old, a.max = a.max, min(a.max, 6); c_span(b, a); a.max = old
    from kpcb_route import links
    todo = {}
    for net in sorted({p['net'] for f in b.placed() for p in f.pads if p['net'] and not p['net'].startswith('unconnected-')}):
        L = links(b, net)[1]
        if L:
            todo[net] = L
    print(f"\n### routing\n{sum(len(v) for v in todo.values())} unrouted link(s) on {len(todo)} net(s)"
          + (f", e.g. {', '.join(unesc_disp(n) for n in sorted(todo, key=lambda n: -len(todo[n]))[:5])}"
             if todo else ' - every net is one piece of copper (from the saved fill)'))
    from kpcb_tracks import donothing, nothing_line
    dn = donothing(b) if b.tracks else None
    if dn:
        print(f"do-nothing copper: {nothing_line(dn)}")

    # what to do next, worked out from the findings rather than left to the reader
    F, _ = found
    hot = defaultdict(int)
    for x in F:
        if x['severity'] == 'ERROR':
            for r in x['refs']:
                hot[r] += 1
    nxt = []
    regs = [f.ref for f in b.fps.values() if prefix(f.ref) == 'U' and not f.placed
            and (role_pads(f).get('SW') or role_pads(f).get('OUT')) and len(f.pads) >= 5]
    if regs:
        nxt.append(f"kpcb.py {b.path} ic {' '.join(sorted(regs, key=natkey)[:4])}"
                   f"   # unplaced regulator(s): where their passives go")
    busy = [r for r, c in sorted(hot.items(), key=lambda kv: (-kv[1], natkey(kv[0])))[:4]]
    if busy:
        nxt.append(f"kpcb.py {b.path} where {' '.join(busy)}"
                   f"   # in the most ERROR findings")
    if sum(1 for f in b.fps.values() if not f.placed) > 20:
        nxt.append(f"kpcb.py {b.path} map   # where the free space is, before placing more")
    un = sorted((f for f in b.fps.values() if not f.placed and f.pads), key=lambda f: -f.area)
    if un:
        nxt.append(f"kpcb.py {b.path} place {un[0].ref}   # where the biggest unplaced part fits (scratch copy: -o)")
    if dn and any(dn[k] for k in ('dead', 'passvia', 'micro', 'inpour')):
        nxt.append(f"kpcb.py {b.path} tracks --kind deadend,stub,inpour   # do-nothing copper; the clean-up order is "
                   f"recipes.md 'Clean up the routing' (ripup --deadend, smooth --micro, ripup --inpour)")
    if todo:
        nxt.append(f"kpcb.py {b.path} airwires   # each unrouted link; on a scratch copy `route --pending -o OUT`")
        nxt.append(f"kpcb.py {b.path} map --layers In2.Cu   # where a layer still has room for routes")
    if netpath:
        nxt.append(f"knet.py {netpath} check   # the electrical half; placement cannot see it")
    if b.zones:
        nxt.append(f"kpcb.py {b.path} zones   # pour coverage per layer (cached fill state)")
    if any(re.search(r'RF|50', b.netclass(n), re.I) for n in b.nets):
        nxt.append(f"kpcb.py {b.path} rf   # 50-ohm nets: width necks, Zo, reference plane, via fence")
    nxt.append(f"kpcb.py {b.path} height   # Z stack from the 3D models (cell + board + tallest part)")
    nxt.append(f"kdrc.py {b.path}   # KiCad's own DRC + ERC - real clearance/rules, not heuristics")
    nxt.append(f"kpcb.py {b.path} sync   # re-run after any schematic change")
    print("\n### next\n" + '\n'.join('  ' + x for x in nxt))
    return rc

def _xy(p):
    """x,y at >= 3 decimals and every one KiCad stored (an `edit seg` laid from a rounded
    102.661 ended 0.25 um off a via at 102.66125, and KiCad flagged it not centred)"""
    f = lambda v: (lambda s: s + '0' * (3 - len(s.split('.')[1])))(f"{v:.6f}".rstrip('0'))   # noqa: E731
    return f"{f(p[0])},{f(p[1])}"


def c_copper(b, a):
    """Every track, arc, via, pad and zone touching a box (X0,Y0,X1,Y1, or X,Y with -r),
    with the uuid prefix `edit` takes: the inventory a hand edit is written from."""
    try:
        v = [float(x) for x in a.args[0].split(',')]
        box = tuple(v) if len(v) == 4 else (v[0] - a.radius, v[1] - a.radius, v[0] + a.radius, v[1] + a.radius)
        assert len(v) in (2, 4)
    except (IndexError, ValueError, AssertionError):
        print("copper X0,Y0,X1,Y1 | X,Y [-r R]  [--layers L,..] [--net NET]"); return 1
    lys = set(a.layers.split(',')) if a.layers else set(b.copper)
    nets = set(a.net or [])
    want = lambda n: not nets or n in nets                     # noqa: E731
    def hit(bb):
        return bb[2] >= box[0] and bb[0] <= box[2] and bb[3] >= box[1] and bb[1] <= box[3]
    rows = []
    for t in b.tracks:
        hw = t['w'] / 2
        bb = (min(t['a'][0], t['b'][0]) - hw, min(t['a'][1], t['b'][1]) - hw,
              max(t['a'][0], t['b'][0]) + hw, max(t['a'][1], t['b'][1]) + hw)
        inb = lambda q: box[0] <= q[0] <= box[2] and box[1] <= q[1] <= box[3]    # noqa: E731
        edges = (((box[0], box[1]), (box[2], box[1])), ((box[2], box[1]), (box[2], box[3])),
                 ((box[2], box[3]), (box[0], box[3])), ((box[0], box[3]), (box[0], box[1])))
        if t['layer'] in lys and want(t['net']) and hit(bb) and (
                inb(t['a']) or inb(t['b']) or min(_segd(t['a'], t['b'], e0, e1) for e0, e1 in edges) <= hw):
            rows.append((t['layer'], 'T', f"{trunc(unesc_disp(t['net']) or '(no net)', 22):<22} w{t['w']:.2f}  "
                         f"{_xy(t['a'])} -> {_xy(t['b'])}", t['uuid']))
    for v in b.vias:
        r0 = v['size'] / 2
        if want(v['net']) and hit((v['x'] - r0, v['y'] - r0, v['x'] + r0, v['y'] + r0)) and lys & set(b.copper):
            rows.append(('via', 'V', f"{trunc(unesc_disp(v['net']) or '(no net)', 22):<22} {v['size']:.2f}/{v['drill']:.2f}"
                         f"  {_xy((v['x'], v['y']))}" + ('  free' if v.get('free') else ''), v['uuid']))
    for f in b.placed():
        for p in f.pads:
            cl = [l for l in b.copper if l in p['layers'] or '*.Cu' in p['layers']]
            if want(p['net']) and lys & set(cl) and hit(pad_box(p)):
                rows.append(('/'.join(l.split('.')[0] for l in cl), 'P',
                             f"{f.ref + '.' + p['num']:<10} {trunc(unesc_disp(p['net']) or '(no net)', 22):<22} "
                             f"{_xy((p['x'], p['y']))}  {p['sx']:.2f}x{p['sy']:.2f}", ''))
    for z in b.zones:
        net, zl = z
        if z.outline and want(net) and lys & set(zl if '*.Cu' not in zl else b.copper) and hit(bbox(z.outline)):
            rows.append(('/'.join(l.split('.')[0] for l in zl), 'Z',
                         f"{trunc(unesc_disp(net) or ('(rule area)' if z.keepout else '(no net)'), 22):<22} prio {z.priority:<3}"
                         + (f" {z.name!r}" if z.name else ''), z.uuid or ''))
    order = {ly: i for i, ly in enumerate(b.copper)}
    rows.sort(key=lambda r: (r[1] != 'Z', order.get(r[0], -1), r[1], r[2]))
    print(f"copper in {box[0]:.2f},{box[1]:.2f} .. {box[2]:.2f},{box[3]:.2f}: {len(rows)} item(s)"
          + (f"  (nets: {', '.join(sorted(nets))})" if nets else ''))
    for ly, k, txt, u in rows[:a.max * 20]:
        print(f"  {k} {ly:<8} {txt}" + (f"  {u[:8]}" if u else ''))
    if len(rows) > a.max * 20:
        print(f"  ... +{len(rows) - a.max * 20} more - narrow the box, --layers or --net")
    return 0


def c_net(b, a):
    """Every pad on a net with its absolute position, plus the routed copper
    per layer, vias, zones and the physical extent - pad-level geometry that
    `where REF` (footprint level) can't answer."""
    names = list(a.args) + list(a.net or [])
    if not names:
        print("net needs a net name (use --net=-BATT for a leading dash)", file=sys.stderr); return 1
    rc = 0
    for name in names:
        net, cand = _resolve_net(b, name)
        if not net:
            print(f"{name}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else ''))
            rc = 1; continue
        pads = sorted(((f, p) for f in b.fps.values() for p in f.pads if p['net'] == net),
                      key=lambda fp: (natkey(fp[0].ref), natkey(fp[1]['num'])))
        segs = [t for t in b.tracks if t['net'] == net]
        vias = [v for v in b.vias if v['net'] == net]
        fills = [fl for fl in b.fills if fl['net'] == net]
        print(f"\n=== {unesc_disp(net)}   netclass {b.netclass(net) or 'Default'}   "
              f"{len(pads)} pad(s), {len(segs)} track seg(s), {len(vias)} via(s)")
        for f, p in pads[:a.max * 3]:
            ly = '/'.join(l.split('.')[0] for l in p['layers'] if l.endswith('.Cu'))
            print(f"  {f.ref + '.' + p['num']:<10} {p['x']:8.3f} {p['y']:8.3f}  {ly:<6} "
                  f"{p['sx']:.2f}x{p['sy']:.2f} {trunc(p['fn'], 16)}")
        if len(pads) > a.max * 3:
            print(f"  ... +{len(pads) - a.max * 3} more pad(s)")
        for ly in b.copper:
            ss = [t for t in segs if t['layer'] == ly]
            if ss:
                ws = sorted({t['w'] for t in ss})
                print(f"  {ly:<7} {len(ss):>3} seg  {sum(t['len'] for t in ss):7.2f} mm  width "
                      + (f"{ws[0]:.3f}" if len(ws) == 1 else f"{ws[0]:.3f}..{ws[-1]:.3f}"))
        if vias:
            dr = sorted({v['drill'] for v in vias})
            print(f"  vias    {len(vias):>3}      drill {' '.join(f'{d:g}' for d in dr)} mm")
        by = defaultdict(list)
        for fl in fills:
            by[fl['layer']].append(fl['area'])
        for ly, ar in sorted(by.items()):
            print(f"  zone    {ly:<7} {sum(ar):7.1f} mm2 in {len(ar)} fragment(s)")
        xy = [(p['x'], p['y']) for _, p in pads] + [q for t in segs for q in (t['a'], t['b'])] \
            + [(v['x'], v['y']) for v in vias]
        if xy:
            e = bbox(xy)
            print(f"  extent  {e[2]-e[0]:.1f} x {e[3]-e[1]:.1f} mm  "
                  f"({e[0]:.1f},{e[1]:.1f} .. {e[2]:.1f},{e[3]:.1f})")
        C = Copper(b, net)
        P = C.pieces()
        pp, stray = [q for q in P if q['pads']], [q for q in P if not q['pads']]
        def what(q):
            f = ', '.join(f"{ly} fill {ar:.1f} mm2" for ly, ar in sorted(q['fills'], key=lambda z: -z[1])[:2])
            return ', '.join(x for x in (f, f"{q['tracks']} trk" if q['tracks'] else '',
                                          f"{q['vias']} via" if q['vias'] else '',
                                          f"@{q['at'][0]:.1f},{q['at'][1]:.1f}") if x)
        if len(pp) <= 1:
            print("  copper  one piece" + (f" joins all {len(pads)} pad(s)" if pads else ""))
        else:
            print(f"  copper  {len(pp)} PIECES - not one conductor. Main: {len(pp[0]['pads'])} pad(s). Cut off:")
            for q in pp[1:1 + a.max]:
                print(f"    {' '.join(q['pads'][:8])}{' ...' if len(q['pads']) > 8 else ''}  ({what(q)})")
        for t, f, p, dep in C.clip_only():          # joined only by a body grazing the pad's shape
            print(f"  note    {t['layer']} track {t['a'][0]:.2f},{t['a'][1]:.2f} -> {t['b'][0]:.2f},{t['b'][1]:.2f} "
                  f"clips a corner of {f.ref}.{p['num']} ({dep * 1000:.0f} um overlap, no end on it), its only join: "
                  "KiCad counts it, a marginal one; end the track on the pad")
        if stray:
            big = sorted(stray, key=lambda q: -sum(ar for _l, ar in q['fills']))
            print(f"  islands {len(stray)} padless piece(s) (isolated copper), largest: "
                  + '; '.join(what(q) for q in big[:3]))
        from kpcb_route import _planes
        from kpcb_copper import dead_copper, dead_desc
        for d in dead_copper(b, net) if all(n != net for n, _l in _planes(b)) else ():
            print(f"  dead    branch {dead_desc(d)}: serves no pad (`ripup --deadend {unesc_disp(net.split('/')[-1])}`)")
    return rc

def c_vias(b, a):
    """`vias`: every via by size / drill / layer span, the nets on each, and the
    worst drill aspect ratio - the via question scripts kept re-deriving by hand."""
    if not b.vias:
        print("no vias"); return 0
    full = {b.copper[0], b.copper[-1]} if b.copper else set()
    groups = defaultdict(list)
    for v in b.vias:
        span = 'through' if not v['layers'] or set(v['layers']) >= full else '-'.join(v['layers'])
        groups[(v['size'], v['drill'], span)].append(v['net'])
    thick = sum(t for _n, ty, t, _e in b.stack if ty and t)
    print(f"{len(b.vias)} via(s), {len({v['net'] for v in b.vias})} net(s)"
          + (f"; board {thick:.2f} mm, worst aspect {thick / min(v['drill'] for v in b.vias if v['drill']):.1f}:1"
             if thick and any(v['drill'] for v in b.vias) else ''))
    print(f"  {'size':>5} {'drill':>5} {'count':>6}  {'span':<8} nets")
    for (sz, dr, span), nets in sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        top = Counter(nets).most_common(4)
        more = len(set(nets)) - len(top)
        print(f"  {sz:5.2f} {dr:5.2f} {len(nets):6d}  {span:<8} "
              + ', '.join(f"{n or '(no net)'} {c}" for n, c in top) + (f", +{more} more net(s)" if more > 0 else ''))
    return 0

CMDS = {'summary': c_summary, 'check': c_check, 'where': c_where, 'map': c_map,
        'sheet': c_sheet, 'unplaced': c_unplaced, 'ic': c_ic, 'span': c_span,
        'zones': c_zones, 'sync': c_sync, 'review': c_review, 'ampacity': c_ampacity,
        'viapad': c_viapad, 'net': c_net, 'rf': c_rf, 'height': c_height, 'view': c_view,
        'silk': c_silk, 'movecheck': c_movecheck, 'freebox': c_freebox,
        'tidy': c_tidy, 'vias': c_vias, 'fixlines': c_fixlines, 'renet': c_renet, 'move': c_move, 'ripup': c_ripup,
        'airwires': c_airwires, 'route': c_route, 'place': c_place, 'pairs': c_pairs,
        'edit': c_edit, 'copper': c_copper, 'fatten': c_fatten, 'stitch': c_stitch, 'unhook': c_unhook,
        'vscan': c_vscan, 'smooth': c_smooth, 'tracks': c_tracks, 'widen': c_widen, 'loops': c_loops,
        'diff': c_diff, 'refdes': c_refdes, 'pinlabels': c_pinlabels, 'label': c_label,
        'congest': c_congest, 'fanout': c_fanout, 'reroute': c_reroute, 'refslots': c_refslots}

LIMITS = (('edge', 0.5, 'courtyard-to-board-edge minimum, mm'),
          ('hole', 1.5, 'keepout beyond a mounting hole pad radius, mm'),
          ('conn', 10.0, 'max connector distance from an edge, mm'),
          ('rf', 8.0, 'RF part to switching node, mm'),
          ('therm', 8.0, 'heat source to heat-sensitive part, mm'),
          ('bypass', 3.0, 'supply pin to its bypass cap, mm'),
          ('clear', 0.0, 'extra margin on every courtyard test, mm'),
          ('gap', 0.25, 'for `ic`: courtyard gap in a suggestion, mm'),
          ('fb', 4.0, 'for `ic`: feedback part to switching node, mm'),
          ('tol', 1.0, 'for `ic`: slop allowed before a placed part stops reading OK, mm'),
          ('span', 0.0, 'NETSPAN threshold, mm (0 = half the board diagonal)'),
          ('fanout', 8.0, 'a net with more nodes than this is a rail'))

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('file')
    ap.add_argument('cmd', choices=list(CMDS))
    ap.add_argument('args', nargs='*')
    ap.add_argument('-r', '--radius', type=float, default=5.0,
                    help='for `where`: neighbour search radius in mm (5)')
    ap.add_argument('--max', type=int, default=12,
                    help='max lines per rule / per neighbour list before a +N tail (12)')
    ap.add_argument('--cols', type=int, default=48, help='for `map`: grid width (48)')
    ap.add_argument('--side', default=None, help='for `map` and `view --layers silk`: f (front, default) or b; for `place`: f, b or both (default)')
    ap.add_argument('--near', default='', help='for `place`: the part (or X,Y) this one serves, e.g. the IC a cap decouples; '
                    'for `label`: the part whose designator spot the label takes')
    ap.add_argument('--rots', default='', help='for `place`: rotations to try (0,90,180,270)')
    ap.add_argument('--pick', type=int, default=1, help='for `place -o`: write the Nth clear spot (1)')
    ap.add_argument('--shove', action='store_true',
                    help='for `place`: also find spots that open up when ONE small placed part (<= 3 pads) moves '
                         '<= 1.5 mm; -o writes shove #1 when no spot is clear as it stands')
    ap.add_argument('--origin', choices=('page', 'grid', 'aux'), default='page',
                    help="for `where`: read and print x,y relative to the board's grid or aux "
                         "(drill/place) origin, as KiCad's Properties dialog does (page)")
    for name, dflt, hlp in LIMITS:
        ap.add_argument('--' + name, type=float, default=None, help=hlp + f' ({dflt:g})')
    ap.add_argument('--anchor', default='',
                    help="for `ic`: hang the layout off this already-placed part "
                         "instead of the IC ('none' to disable the automatic choice)")
    ap.add_argument('--cin', default='', help="for `ic`: force the input caps, e.g. --cin C122,C124")
    ap.add_argument('--cout', default='', help="for `ic`: force the output caps")
    ap.add_argument('--ncin', type=int, default=3, help='for `ic`: how many input caps to place off a rail (3)')
    ap.add_argument('--ncout', type=int, default=3, help='for `ic`: how many output caps to place off a rail (3)')
    ap.add_argument('--assoc', type=int, default=6, help="for `ic`: a net with more nodes than this is a rail, not the IC's own net (6)")
    ap.add_argument('--amps', type=float, default=None,
                    help='for `ampacity`: required current on the named net(s), A')
    ap.add_argument('--dt', type=float, default=10.0,
                    help='for `ampacity`: allowed temperature rise, C (10)')
    ap.add_argument('--plating', type=float, default=20.0,
                    help='for `ampacity`: via barrel plating thickness, um (20)')
    ap.add_argument('--vdrop', type=float, default=0.25,
                    help='for `ampacity`: flag if series-bound Vdrop exceeds this, V (0.25)')
    ap.add_argument('--voids', action='store_true',
                    help='for `zones`: copper-free regions in the plane pour + a via spot to rescue each')
    ap.add_argument('--area', type=float, default=2.0, help='for `zones --voids`: smallest void core, mm2 (2)')
    ap.add_argument('--from', dest='src', default='',
                    help='for `ampacity`: source pad REF.PIN; with --to, solve how the current '
                         'splits between the two pads (tracks + via barrels resistive, pours ideal)')
    ap.add_argument('--to', dest='dst', default='', help='for `ampacity`: sink pad REF.PIN')
    ap.add_argument('--fills', action='store_true',
                    help='for `ampacity --from/--to`: also solve R through the REAL saved fills (pour necks '
                         'included) on a grid; numpy speeds it up when installed')
    ap.add_argument('--cell', type=float, default=0.0,
                    help='for `ampacity --fills`: grid cell, mm (0.1 with numpy, else 0.2); for `congest`: '
                         'the map cell, mm (2)')
    ap.add_argument('--net', action='append', default=[],
                    help='for `ampacity`: net name, repeatable. For a name starting with '
                         '"-", use =, e.g. --net=-BATT (a space before the dash still '
                         'confuses argparse, as does the positional arg)')
    ap.add_argument('--freq', type=float, default=None,
                    help='for `rf`: signal frequency in MHz, for the lambda/20 fence check '
                         '(default: inferred from the net/sheet name)')
    ap.add_argument('--fence', type=float, default=1.5,
                    help='for `rf`: a GND via this close to the trace edge counts as fence, mm (1.5)')
    ap.add_argument('--signal', action='store_true',
                    help='for `viapad`: hide GND/power pads (routine drops), keep signal pads')
    ap.add_argument('--min', dest='min_vias', type=int, default=1,
                    help='for `viapad`: only pads holding >= N vias, e.g. 4 for thermal pads (1)')
    ap.add_argument('--layers', default='',
                    help="for `view`: comma list of layers (default: the part's side Cu/SilkS/Fab/CrtYd + Edge.Cuts; "
                         "'silk': that side's silkscreen drawn from the model, pads, bodies, outlines, texts, flagged ones red); "
                         "for `map`: one copper layer -> its copper occupancy (where routes can go); "
                         "for `route`: the layers it may use")
    ap.add_argument('--box', default='', help='for `view`: X0,Y0,X1,Y1 board mm instead of refs; '
                                              'for `fatten`/`stitch`/`ripup`: only tracks / vias in this box; '
                                              'for `tracks`: the census of this region; for `diff`: vias and tracks here')
    ap.add_argument('--px', type=float, default=20, help='for `view`: pixels per mm (plot: ~1200 px across the box; 60 for --tracks, 120 for --sites)')
    ap.add_argument('--by-net', action='store_true', help='for `view`: the copper on --layers (default all), fills '
                    'included, one hue per net, GND grey, a legend of the largest nets in the box')
    ap.add_argument('--tracks', action='store_true', help='for `view`: draw tracks/pads/vias from the board model, '
                                                         'no pour fills, one colour per copper layer (each --net green)')
    ap.add_argument('--mark', default='', help='for `view --tracks`: X,Y;X,Y points to ring')
    ap.add_argument('--sites', default='', help='for `view`: LAYER:X,Y;... one panel per site (-r = half size, 0.8)')
    ap.add_argument('-o', '--out', default='',
                    help='for `view`: output .png (or .svg); for the writers: the new board file')
    ap.add_argument('--scan', nargs='+', default=None, metavar='AXIS=V',
                    help='for `movecheck`: slide REF along x=X y=Y0..Y1 (or y=Y x=X0..X1), print clear stretches')
    ap.add_argument('--step', type=float, default=0.05, help='for `movecheck --scan`: step, mm (0.05)')
    ap.add_argument('--grid', type=float, default=0.05, help='for `route`: grid pitch, mm (0.05)')
    ap.add_argument('--margin', type=float, default=3.0,
                    help='for `route`: window around the gap, mm (3; widened x2.5 then x6 on failure)')
    ap.add_argument('--width', type=float, default=None,
                    help='for `route`: track width, mm (default: netclass, then 0.2, then 0.15); '
                         'for `fatten`: the band width (default: 2x the netclass width, >= 2 mm); '
                         'for `vscan`: the track width a cell must clear (0.15); '
                         'for `widen`: the target for signal nets (0.2)')
    ap.add_argument('--via', default='', help='for `route`, `stitch` and `vscan`: via SIZE/DRILL, mm (default: netclass, or 0.5/0.2)')
    ap.add_argument('--via-cost', type=float, default=1.5, help='for `route`: a via costs this many mm (1.5)')
    ap.add_argument('--turn', type=float, default=0.3, help='for `route`: a bend costs this many grid cells (0.3)')
    ap.add_argument('--pour-cost', type=float, default=0.6,
                    help='for `route`: extra cost per mm through a foreign pour (0.6 = 1.6x)')
    ap.add_argument('--inner-cost', type=float, default=1.25, help='for `route`: inner-layer cost factor (1.25)')
    ap.add_argument('--ref-cost', type=float, default=2.0,
                    help="for `route`: x the inner cost on the layer under an outer layer's RF traces "
                         "(their reference plane, In1 on parsnip) (2)")
    ap.add_argument('--rips', type=int, default=10,
                    help="for `route`: rip-up budget - a failed link may cross this run's own routes of "
                         "other nets, which are ripped and routed again at once (all undone if one cannot), "
                         "this many times per run (10; 0 = off)")
    ap.add_argument('--avoid', default='', metavar='X0,Y0,X1,Y1[;..]',
                    help="for `route`: boxes no new track or via may touch, every layer, for this run only (a "
                         "footprint keepout that allows tracks, e.g. U1's antenna); nothing is written for them")
    ap.add_argument('--rip-board', type=int, default=0, metavar='N',
                    help="for `route`: also rip unlocked signal nets ALREADY on the board with <= N pads (not GND, "
                         "power, RF, USB or poured nets), a whole net at a time, re-routing them at once (0 = off)")
    ap.add_argument('--rip-cost', type=float, default=10.0,
                    help='for `route`: cost per mm through a route it would rip (10: a 10 mm detour wins)')
    ap.add_argument('--pour', type=float, default=0.0, metavar='W',
                    help='for `route`: a link no track width fits is found at this probe width (0.3) and laid '
                         'as same-net zones, a netclass-width band round it that the refill fits (0 = off)')
    ap.add_argument('--neck', type=float, default=0.0, metavar='W',
                    help='for `route`: neck-down width out of a fine-pitch pad no class-width track can leave '
                         '(default: the widest of 0.4/0.3/0.25/0.2 that fits)')
    ap.add_argument('--neck-len', type=float, default=1.5,
                    help='for `route`: the longest a neck may run before the class width, mm (1.5; 0 = off)')
    ap.add_argument('--minw', type=float, default=None,
                    help='for `fatten`: only tracks at least this wide get a band (0.5: sense legs stay thin)')
    ap.add_argument('--keep', default='', help='for `fatten`: X0,Y0,X1,Y1[;...] boxes the bands stay out of; '
                    'for `stitch --sparse`: boxes no via goes in')
    ap.add_argument('--n', type=int, default=0, help='for `stitch`: vias per anchor (3 near a point, 2 beside each via)')
    ap.add_argument('--sparse', type=float, default=0.0, metavar='T',
                    help='for `stitch`: greedy stitching of the outer pour, worst point first, until every point is '
                         'within T mm of a via of the net (--keep boxes stay free)')
    ap.add_argument('--in-pad', action='store_true',
                    help='for `stitch`: a via may sit wholly inside a same-net SMD pad of >= 3 mm2 (FET, fuse, inductor)')
    ap.add_argument('--pair', action='store_true',
                    help='for `route`: a diff-pair leg whose mate is already routed runs alongside it at the '
                         'netclass pitch (diff_pair_width + diff_pair_gap): `route X_P X_N --pair`')
    ap.add_argument('--force', action='store_true', help='for `route`: route an RF-class net anyway')
    ap.add_argument('--pending', action='store_true', help='for `route`: every net `airwires` lists, shortest first')
    ap.add_argument('--since', default='', help='for `airwires`: an earlier refilled copy: which nets a later pass split, and the tracks that did it')
    ap.add_argument('--report', default='', help='for `airwires --since`: write the culprit uuids as JSON for `ripup --drc`')
    ap.add_argument('--cut-islands', action='store_true',
                    help='for `route`: let a route cross a power-island pour at 4x the plane cost (check the '
                         'refill: it may split the island) instead of treating it as copper')
    ap.add_argument('--flip', action='store_true', help='for `move`: flip REF to the other side first')
    ap.add_argument('--stub', type=float, default=None, metavar='W',
                    help="for `edit dragvia`: the pad-to-via stub's width (default: the pad-layer track on the via, "
                         "capped at the pad's narrow side)")
    ap.add_argument('--no-drag', action='store_true', help="for `move`/`movecheck`: KiCad's Move (M), not Drag (D): "
                    "its tracks and in-pad vias stay put (moving a part off its via-in-pad)")
    ap.add_argument('--overlaps', action='store_true', help='for `smooth`: drop doubled same-net copper, trim hairpin tips')
    ap.add_argument('--corners', action='store_true', help='for `smooth`: cut every visible corner >= 46 deg at a two-track node')
    ap.add_argument('--square', action='store_true', help='for `smooth`: slide an off-45 segment\'s pad end onto a 45 inside the pad')
    ap.add_argument('--pads', action='store_true', help='for `smooth`: run a dead-end trace that stops at a pad edge onto the pad centre')
    ap.add_argument('--micro', action='store_true', help='for `smooth`: merge segments under 0.05 mm into a neighbour '
                    '(a join stays a join), delete one only when wholly under other same-net copper')
    ap.add_argument('--jogs', action='store_true', help='for `smooth`: collapse lane jogs, moving one blocking free GND via')
    ap.add_argument('--twins', action='store_true', help='for `smooth`: two same-net runs side by side into one pad '
                    're-laid as a daisy chain through the nearer start (not in --all)')
    ap.add_argument('--nudge', action='store_true', help='for `smooth --jogs`: also drag one blocking signal via <= 0.3 mm')
    ap.add_argument('--all', action='store_true', help='for `smooth -o`: chains, overlaps (+ merge), corners, square, pads, repeated until a round '
                                                      'changes nothing; for `tracks`: include RF/USB/diff-pair nets; '
                                                      'for `refdes`: every visible designator, not only flagged ones; '
                                                      'for `congest`: every net\'s pad MST, no tracks (the placement '
                                                      'view); for `fanout`: list routed pads too')
    ap.add_argument('--hidden', action='store_true', help='for `smooth --corners`: also corners hidden under same-net copper')
    ap.add_argument('--uniform', action='store_true', help='for `widen`: a non-power segment narrower than its '
                    'neighbours (each end a two-track node or a via, never a pad) takes their width where clear')
    ap.add_argument('--uniform-power', action='store_true', help='for `widen`: per power run, also raise every '
                    'segment to the run\'s widest where clear (never narrows); reports the width steps left')
    ap.add_argument('--kind', default='', help='for `tracks`: only these rows: acute,90,off45,neck,stub,pad,overlap,twin,'
                                               'jog,detour,inpour,inpourpwr,deadend')
    ap.add_argument('--show', action='store_true', help='for `refdes`: also place (and unhide) hidden designators')
    ap.add_argument('--size', type=float, default=None, help='for `refdes`: designator text height, mm (default: kept, >= the fab minimum); '
                    'for `pinlabels`/`label`: text height (default 1, >= the fab minimum)')
    ap.add_argument('--thick', type=float, default=None, help='for `refdes`: stroke thickness, mm (default: kept; 0.15 x --size)')
    ap.add_argument('--view', default='', help='for `tracks`: also a PNG montage of the listed rows, one panel each')
    ap.add_argument('--drc', default='', help='for `ripup`: a kicad-cli DRC JSON (`kdrc.py --report`): rip up the items it flags')
    ap.add_argument('--inpour', action='store_true', help='for `ripup`: tracks wholly inside the saved fill of their own '
                    'net - every plane net\'s (GND), or a NAMED net\'s of any kind (opt-in) - minus any a thermal-relief '
                    'pad needs as a spoke (`tracks --kind inpour,inpourpwr` lists them)')
    ap.add_argument('--drop-track', action='store_true', help='for `fatten`: refill a temp copy and delete the banded '
                    'tracks the band then wholly holds (`ripup NET --inpour` on them; OUT stays unfilled)')
    ap.add_argument('--deadend', action='store_true', help='for `ripup`: copper that serves no pad (`tracks --kind deadend`): '
                    'its tracks and vias, a dead end cut back, a zone whose every fill fragment is dead')
    ap.add_argument('--types', default='', help='for `ripup --drc`: violation types to act on (shorts, clearances, dangling)')
    ap.add_argument('--drop', action='store_true',
                    help='for `renet`: delete netless clusters that touch 2+ nets or none (stale routes)')
    ap.add_argument('-v', '--verbose', action='store_true', help='for `route`: print every segment and via; for `diff`: '
                    'each net\'s removed (-), added (+) and re-widened (~) segments with coordinates')
    ap.add_argument('--only', default='')
    ap.add_argument('--skip', default='')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--rules', action='store_true', help='print the check rule legend')
    ap.add_argument('--no-suppress', action='store_true',
                    help="for `check`: ignore kpcb.json's suppress list")
    ap.add_argument('-h', '--help', action='store_true')
    a = parse_args(ap)
    if a.help:
        print(__doc__); return
    cfg = {}
    try:
        cp = os.path.join(os.path.dirname(os.path.abspath(a.file)), 'kpcb.json')
        if os.path.exists(cp):
            cfg = json.load(open(cp))
    except Exception as e:
        print(f"(ignoring malformed kpcb.json: {e})", file=sys.stderr)
    for name, dflt, _ in LIMITS:                # the command line wins, then kpcb.json
        if getattr(a, name) is None:
            try:
                setattr(a, name, float(cfg.get(name, dflt)))
            except (TypeError, ValueError):
                setattr(a, name, dflt)
    a.current = cfg.get('current') if isinstance(cfg.get('current'), dict) else {}
    a.height_cfg = cfg.get('height') if isinstance(cfg.get('height'), dict) else {}
    a.suppress = {}
    if not a.no_suppress:
        for entry in cfg.get('suppress') or []:
            rule, _, tok = str(entry).partition(':')
            a.suppress.setdefault(rule.strip().upper(), set()).add(tok.strip())
    try:
        b = Board(a.file)
    except FileNotFoundError:
        print(f"no such board: {a.file}", file=sys.stderr); return 3
    except ValueError as e:
        print(str(e), file=sys.stderr); return 3
    if not a.span and b.outline:
        a.span = 0.5 * math.hypot(b.outline[2] - b.outline[0], b.outline[3] - b.outline[1])
    t0 = time.time()
    rc = CMDS[a.cmd](b, a) or 0
    out = getattr(a, 'out', None)
    if out and a.cmd != 'diff' and os.path.exists(out) and os.path.getmtime(out) >= t0 - 1 \
            and out.endswith('.kicad_pcb'):           # every writer: the do-nothing copper it added
        from kpcb_tracks import gate
        gate(b, out, a.cmd)
    return rc

if __name__ == '__main__':
    if hasattr(signal, 'SIGPIPE'):                  # piping to `head`: no traceback
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    sys.exit(main() or 0)
