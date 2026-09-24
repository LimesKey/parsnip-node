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
                                      components with a via centred in an SMD pad
                                      (via-in-pad -> needs filled+capped vias)
  kpcb.py FILE where F5.1 BT1.1       a PAD's absolute centre, size, net; two specs
                                      also print the distance between them
  kpcb.py FILE net NET                every pad on a net (absolute xy), copper per
                                      layer, vias, zones, extent
  kpcb.py FILE height [REF...]        3D model height per part + the Z stack (cell +
                                      board + tallest part over it), via KiCad's GLB
                                      export of the STEP models; no model = UNKNOWN
  kpcb.py FILE rf [NET...]            50-ohm trace review: width necks, microstrip Zo
                                      from the stackup, same-layer GND gap, reference
                                      plane under it, GND via fence vs lambda/20

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
import sys, os, re, json, math, glob, argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kcommon import refrange, natkey, trunc, prefix, unesc_disp, print_findings
except ImportError:                                     # pragma: no cover
    print("kpcb.py needs kcommon.py beside it (shared parser + finding formatter)",
          file=sys.stderr)
    raise
# The heavy commands live in kpcb_<cmd>.py beside this; the Board model in kpcb_board.py.
from kpcb_board import (_find_pad, _resolve_net, bbox, Board, box_dist, ctr, hit, inside,
                        pt_seg_dist, sheet_of)
from kpcb_check import c_check, gen_findings, net_spans
from kpcb_amp import c_ampacity
from kpcb_zones import c_zones
from kpcb_viapad import c_viapad
from kpcb_rf import c_rf
from kpcb_height import c_height
from kpcb_ic import c_ic, role_pads


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
    for spec in a.args:
        m = re.match(r'^(-?[\d.]+)\s*,\s*(-?[\d.]+)$', spec)
        if m:
            p = (float(m.group(1)), float(m.group(2)))
            pts.append((spec, p))
            box = (p[0], p[1], p[0], p[1])
            if b.edge_segs:
                where = 'inside' if inside(p, b.rings) else 'OUTSIDE'
                d = f"   edge {min(pt_seg_dist(p, u, v) for u, v in b.edge_segs):.2f} mm"
            else:
                where, d = 'no Edge.Cuts, so inside/outside is', ''
            print(f"\n=== {p[0]:.2f},{p[1]:.2f}   {where} the outline{d}")
            _neigh(b, box, None, a)
            continue
        pad = _find_pad(b, spec)
        if pad:
            f, p = pad
            pts.append((spec, (p['x'], p['y'])))
            ly = ' '.join(l for l in p['layers'] if l.endswith('.Cu')) or ' '.join(p['layers'])
            print(f"\n=== {spec}  {p['fn'] or ''}  on {unesc_disp(p['net']) or '(no net)'}")
            print(f"  at        : {p['x']:.3f}, {p['y']:.3f}  (absolute)  pad rot {p['prot']:g}")
            print(f"  pad       : {p['kind']} {p['shape']} {p['sx']:.2f} x {p['sy']:.2f} mm"
                  + (f"  drill {p['drill']:.2f}" if p['drill'] else '') + f"  {ly}")
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
        print(f"  at        : {f.x:.3f}, {f.y:.3f}  rot {f.rot:g}  layer {f.layer}")
        print(f"  courtyard : {c[0]:.2f},{c[1]:.2f} .. {c[2]:.2f},{c[3]:.2f}  "
              f"({c[2]-c[0]:.2f} x {c[3]-c[1]:.2f} mm){'' if f.crtyd_real else '  [NO CrtYd - pads+fab bbox]'}")
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
        if d <= r:
            rows.append((d, g))
    rows.sort(key=lambda t: (t[0], natkey(t[1].ref)))
    print(f"  neighbours within {r:g} mm ({len(rows)}):")
    for d, g in rows[:a.max]:
        side = 'B' if g.back else 'F'
        # same-side contact is what `check` calls OVERLAP; a cross-side one is
        # just a projection, so label it differently and do not cry wolf
        flag = ''
        if d <= 0 and hit(box, g.crtyd):
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
    if bf - nf:
        add('ERROR', 'SYNCPART', f"on the board but not in the netlist "
            f"({len(bf-nf)}): {trunc(refrange(sorted(bf-nf, key=natkey)), 90)}")
    if nf - bf:
        add('ERROR', 'SYNCPART', f"in the netlist but not on the board "
            f"({len(nf-bf)}): {trunc(refrange(sorted(nf-bf, key=natkey)), 90)}")

    for r in sorted(bf & nf, key=natkey):
        f, c = b.fps[r], n.comps[r]
        if c['footprint'] and f.fp != c['footprint']:
            add('ERROR', 'SYNCFP', f"{r} footprint differs: board {trunc(f.fp,34)} / "
                                   f"netlist {trunc(c['footprint'],34)}", [r])
        if f.value != c['value']:
            add('WARN', 'SYNCVAL', f"{r} value differs: board {trunc(f.value,20)} / "
                                   f"netlist {trunc(c['value'],20)}", [r])
        if f.dnp != c['dnp']:
            add('WARN', 'SYNCDNP', f"{r} is DNP {'on the board' if f.dnp else 'in the '
                                   'netlist'} only", [r])

    nmis = 0
    for r in sorted(bf & nf, key=natkey):
        for p in b.fps[r].pads:
            want = n.pinnet.get((r, p['num']))
            if want is None:
                if p['net']:
                    add('WARN', 'SYNCNET', f"{r} pad {p['num']} carries "
                        f"{unesc_disp(p['net'])} but the netlist has no such pin", [r])
                continue
            if _canon_net(p['net'] or '') != _canon_net(want):
                nmis += 1
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
    print(f"board   : {b.path}   {len(b.fps)} footprints")
    print(f"netlist : {netpath}   {len(n.comps)} components   exported {n.date}"
          + (f"   [{cands} .net files here; name one to be sure]" if cands > 1 else ""))
    if not F:
        print("\nIN SYNC - same parts, same footprints, same values, same net on every "
              "pad.\nPlacement findings can be trusted to be about the current circuit.")
        return 0
    print_findings(F, f"\n{ne} error, {len(F)-ne} warn\n", rules=SYNC_RULES, cap=a.max)
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
}

# ---------------- one-call review ----------------

def c_review(b, a):
    """Everything a fresh session needs before it can say anything useful, in one
    call, ending with the specific next calls worth making.

    The point is not to save printing - it is to save the caller working out
    what to run next from a summary it has not seen yet."""
    netpath, _ = find_netlist(b.path, a.args[0] if a.args else None)
    if netpath:
        try:
            F, n = sync_findings(b, netpath, a)
            ne = sum(1 for x in F if x['severity'] == 'ERROR')
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
    print("\n### findings"); rc = c_check(b, a)
    print("\n### longest nets"); old, a.max = a.max, min(a.max, 6); c_span(b, a); a.max = old

    # what to do next, worked out from the findings rather than left to the reader
    F, _ = gen_findings(b, a)
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
    return rc

CMDS = {'summary': c_summary, 'check': c_check, 'where': c_where, 'map': c_map,
        'sheet': c_sheet, 'unplaced': c_unplaced, 'ic': c_ic, 'span': c_span,
        'zones': c_zones, 'sync': c_sync, 'review': c_review, 'ampacity': c_ampacity,
        'viapad': c_viapad, 'net': c_net, 'rf': c_rf, 'height': c_height}

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
    ap.add_argument('--side', default='f', help='for `map`: f (front, default) or b (back)')
    for name, dflt, hlp in (('edge', 0.5, 'courtyard-to-board-edge minimum, mm'),
                            ('hole', 1.5, 'keepout beyond a mounting hole pad radius, mm'),
                            ('conn', 10.0, 'max connector distance from an edge, mm'),
                            ('rf', 8.0, 'RF part to switching node, mm'),
                            ('therm', 8.0, 'heat source to heat-sensitive part, mm'),
                            ('bypass', 3.0, 'supply pin to its bypass cap, mm'),
                            ('clear', 0.0, 'extra margin on every courtyard test, mm'),
                            ('gap', 0.25, 'for `ic`: courtyard gap in a suggestion, mm'),
                            ('fb', 4.0, 'for `ic`: feedback part to switching node, mm'),
                            ('tol', 1.0, 'for `ic`: slop allowed before a placed part '
                                         'stops reading OK, mm'),
                            ('span', 0.0, 'NETSPAN threshold, mm (0 = half the board diagonal)'),
                            ('fanout', 8.0, 'a net with more nodes than this is a rail')):
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
    ap.add_argument('--only', default='')
    ap.add_argument('--skip', default='')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--rules', action='store_true', help='print the check rule legend')
    ap.add_argument('--no-suppress', action='store_true',
                    help="for `check`: ignore kpcb.json's suppress list")
    ap.add_argument('-h', '--help', action='store_true')
    a = ap.parse_args()
    if a.help:
        print(__doc__); return
    cfg = {}
    try:
        cp = os.path.join(os.path.dirname(os.path.abspath(a.file)), 'kpcb.json')
        if os.path.exists(cp):
            cfg = json.load(open(cp))
    except Exception as e:
        print(f"(ignoring malformed kpcb.json: {e})", file=sys.stderr)
    for name, dflt in (('edge', 0.5), ('hole', 1.5), ('conn', 10.0), ('rf', 8.0),
                       ('therm', 8.0), ('bypass', 3.0), ('clear', 0.0), ('gap', 0.25),
                       ('fb', 4.0), ('tol', 1.0), ('span', 0.0), ('fanout', 8.0)):
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
    return CMDS[a.cmd](b, a) or 0

try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
