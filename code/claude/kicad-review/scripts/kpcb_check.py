"""kpcb.py `check`: the placement rules (RULES) and their findings."""
import json, math
from collections import defaultdict
from kcommon import (refrange, natkey, trunc, prefix, unesc_disp, GND_RE, rail_voltage,
                     print_findings, suppressed)
from kpcb_board import (bbox, box_dist, EDGEMNT, grow, hit, overlap_area, pt_box_dist,
                        RF_FP, RF_VAL, sheet_of, SUP_PIN, THERM_PFX)

# ---------------- net span ----------------

def net_spans(b, a):
    """Bounding-box diagonal of every net's placed pads.

    The one routing-quality number that exists before any routing does: a
    three-node signal net whose parts are 100 mm apart is a floorplan problem,
    and it is visible now, not after the autorouter fails. Rails are excluded
    by node count rather than listed - dumping GND's 245 nodes is exactly the
    failure this tool exists to avoid."""
    rows = []
    for n, nodes in b.nets.items():
        base = n.split('/')[-1]
        # by node count AND by name: a two-node board can still have a +3V3 net,
        # and a rail's span is the board no matter how few things sit on it
        if len(nodes) > a.fanout or n.startswith('unconnected-') \
           or GND_RE.match(base) or rail_voltage(base) is not None:
            continue
        pts, refs = [], set()
        for r, num in nodes:
            f = b.fps.get(r)
            if not f or not f.placed:
                continue
            refs.add(r)
            pts += [(p['x'], p['y']) for p in f.pads if p['num'] == num]
        if len(pts) < 2:
            continue
        bb = bbox(pts)
        rows.append({'net': n, 'span': math.hypot(bb[2] - bb[0], bb[3] - bb[1]),
                     'placed': len(refs), 'nodes': len({r for r, _ in nodes}),
                     'refs': sorted(refs, key=natkey)})
    rows.sort(key=lambda r: -r['span'])
    return rows

# ---------------- rules ----------------

RULES = {
    'OVERLAP':  'courtyards of two placed parts intersect',
    'EDGECLR':  'courtyard crosses the board edge or sits closer than --edge',
    'HOLECLR':  'part inside a mounting hole keepout, or a hole off the board',
    'CONNACC':  'connector buried away from an edge, or its cable exit blocked',
    'RFNOISE':  'RF part / antenna net within --rf of a switching node',
    'THERMAL':  'heat source within --therm of a heat-sensitive part',
    'BYPASS':   'supply pin further than --bypass from its nearest bypass cap',
    'NOCRTYD':  'footprint has no courtyard - overlap was checked on pads+fab instead',
    'NETSPAN':  'a fully placed non-rail net whose pads are more than --span apart',
    'UNPLACED': 'footprint still parked off the board outline',
}

def _drill_in(f, g):
    """Does either part's through-hole barrel land inside the other's courtyard?"""
    for a, b in ((f, g), (g, f)):
        for p in a.pads:
            if p['drill'] and pt_box_dist((p['x'], p['y']), b.crtyd) < p['drill'] / 2:
                return True
    return False

def gen_findings(b, a):
    only = {s.strip().upper() for s in a.only.split(',')} if a.only else None
    skip = {s.strip().upper() for s in a.skip.split(',')} if a.skip else set()
    F = []
    def add(sev, rule, msg, refs=()):
        if (only and rule not in only) or rule in skip:
            return
        F.append({'severity': sev, 'rule': rule, 'msg': msg, 'refs': list(refs)})

    P = b.placed()
    holes = [f for f in b.fps.values() if b.is_hole(f)]

    # --- UNPLACED: one folded line per sheet, never one per part -------
    bys = defaultdict(list)
    for f in b.fps.values():
        if not f.placed:
            bys[sheet_of(f)].append(f.ref)
    for s in sorted(bys, key=natkey):
        add('INFO', 'UNPLACED', f"{trunc(s,24)}: {len(bys[s])} off the board - "
                                f"{trunc(refrange(bys[s]), 90)}")

    # --- NOCRTYD -------------------------------------------------------
    for f in P:
        if not f.crtyd_real:
            add('INFO', 'NOCRTYD', f"{f.ref} has no F/B.CrtYd geometry; "
                                   f"overlap tested on its pad+fab extent instead", [f.ref])

    # --- OVERLAP: pairwise, but only among placed parts ----------------
    # O(n^2) on the placed set only. 400 unplaced parts stacked in a pile would
    # otherwise produce thousands of meaningless pair findings.
    if not b.rings:
        add('WARN', 'EDGECLR', "no closed Edge.Cuts ring: cannot tell inside from "
                               "outside, so placement checks fell back to the outline bbox")
    S = sorted(P, key=lambda f: f.crtyd[0])
    for i, f in enumerate(S):
        for g in S[i + 1:]:
            if g.crtyd[0] > f.crtyd[2] + a.clear:      # sweep line: no further overlap possible
                break
            # Opposite sides do not clash just by projecting onto each other -
            # a back-side battery holder over front-side 0402s is fine. The only
            # real cross-side clash is a drilled barrel landing in the other
            # part's area, so test the drills, not the bounding boxes.
            if f.back != g.back and not _drill_in(f, g):
                continue
            if b.is_hole(f) or b.is_hole(g):
                continue                               # HOLECLR owns this pair
            A = overlap_area(grow(f.crtyd, a.clear / 2), grow(g.crtyd, a.clear / 2))
            if A > 1e-6:
                add('ERROR', 'OVERLAP', f"{f.ref} and {g.ref} courtyards overlap by "
                                        f"{A:.2f} mm2 ({'same' if f.back == g.back else 'opposite'} side)",
                    [f.ref, g.ref])

    # --- EDGECLR -------------------------------------------------------
    for f in P:
        if f.edge is None:
            continue
        if f.edge <= 0.001:
            # a coax/USB/edge-mount part is SUPPOSED to sit on the edge
            why = ''
            if EDGEMNT.search(f.fp):
                sev, why = 'INFO', ' (edge-mount part, expected)'
            elif RF_VAL.search(f.value) or RF_FP.search(f.fp):
                # a WROOM/E22-style module is placed with its antenna over the
                # edge on purpose; the thing to verify is the keepout, not this
                sev, why = 'INFO', (' (RF module - the antenna end is meant to overhang; '
                                    'confirm the keepout under it)')
            else:
                sev = 'ERROR'
            add(sev, 'EDGECLR', f"{f.ref} courtyard touches or crosses the board edge{why}",
                [f.ref])
        elif f.edge < a.edge:
            add('WARN', 'EDGECLR', f"{f.ref} courtyard is {f.edge:.2f} mm from the edge "
                                   f"(want >= {a.edge:g})", [f.ref])

    # --- HOLECLR -------------------------------------------------------
    for h in holes:
        r = max([max(p['w'], p['drill']) / 2 for p in h.pads] or [1.1]) + a.hole
        if not h.placed:
            # Mid-placement the whole BOM sits in a pile beside the board and a
            # hole parked in that pile is simply not placed yet. Only call it an
            # error when it is out there on its own, which is the real bug.
            pile = sum(1 for g in b.fps.values() if not g.placed and g is not h
                       and math.hypot(g.x - h.x, g.y - h.y) < 25.0)
            add('ERROR' if pile < 3 else 'INFO', 'HOLECLR',
                f"{h.ref} mounting hole is outside the board outline at "
                f"{h.x:.1f},{h.y:.1f}" + (f" (in the parked pile with {pile} other "
                f"unplaced parts - not placed yet, rather than misplaced)" if pile >= 3 else ""),
                [h.ref])
            continue
        keep = (h.x - r, h.y - r, h.x + r, h.y + r)
        for f in P:
            if f is h or b.is_hole(f):
                continue
            if hit(keep, f.crtyd):
                add('ERROR', 'HOLECLR', f"{f.ref} is inside {h.ref}'s {r:.1f} mm screw "
                                        f"keepout", [f.ref, h.ref])

    # --- CONNACC -------------------------------------------------------
    for f in P:
        if not b.is_conn(f) or prefix(f.ref) == 'TP' or f.edge is None:
            continue
        if EDGEMNT.search(f.fp) and f.edge > 1.0:
            add('WARN', 'CONNACC', f"{f.ref} ({trunc(f.fp.split(':')[-1],28)}) is an "
                                   f"edge/side-entry part but sits {f.edge:.1f} mm in from "
                                   f"the edge", [f.ref])
        elif f.edge > a.conn:
            add('WARN', 'CONNACC', f"{f.ref} is {f.edge:.1f} mm from the nearest edge "
                                   f"(want <= {a.conn:g} so a cable can reach it)", [f.ref])
            continue
        # cable exit: sweep the courtyard straight out to the nearest edge and
        # see what is standing in the corridor. Axis-aligned only, which is the
        # honest limit - a diagonal exit is not modelled.
        for blk in _corridor(b, f):
            add('WARN', 'CONNACC', f"{f.ref}'s cable exit corridor is blocked by "
                                   f"{blk.ref} ({trunc(blk.value,18)})", [f.ref, blk.ref])

    # --- RFNOISE -------------------------------------------------------
    sw = b.sw_nets()
    noisy = [f for f in P if any(p['net'] in sw for p in f.pads)]
    for f in P:
        if not b.is_rf(f):
            continue
        for g in noisy:
            if g is f:
                continue
            d = box_dist(f.crtyd, g.crtyd)
            if d < a.rf:
                add('WARN', 'RFNOISE', f"{f.ref} ({trunc(f.value,18)}) is {d:.1f} mm from "
                                       f"switching node part {g.ref}"
                                       f"{'' if f.back == g.back else ' (opposite side)'} "
                                       f"(want >= {a.rf:g})",
                    [f.ref, g.ref])

    # --- THERMAL -------------------------------------------------------
    hot = [f for f in P if b.is_hot(f)]
    for f in P:
        therm = prefix(f.ref) in THERM_PFX
        if not (therm or b.is_sens(f)):
            continue
        for g in hot:
            if g is f:
                continue
            d = box_dist(f.crtyd, g.crtyd)
            if d < a.therm:
                side = '' if f.back == g.back else ' (opposite side, coupled through the board)'
                if therm:       # a bias, not damage: it reads g instead of what it watches
                    msg = (f"{f.ref} thermistor is {d:.1f} mm from {g.ref} ({trunc(g.value,18)}){side}"
                           f" - reads its heat; a bias unless {g.ref} is what it watches")
                else:
                    msg = (f"{f.ref} ({trunc(f.value,18)}) is heat-sensitive and "
                           f"{d:.1f} mm from {g.ref} ({trunc(g.value,18)}){side}")
                add('WARN', 'THERMAL', msg, [f.ref, g.ref])

    # --- NETSPAN -------------------------------------------------------
    # Fully placed only: a net still waiting on parts will move, and flagging it
    # now would just be noise that clears itself.
    for r in net_spans(b, a):
        if r['placed'] == r['nodes'] and r['span'] > a.span:
            add('WARN', 'NETSPAN', f"{unesc_disp(r['net'])} spans {r['span']:.0f} mm "
                                   f"between {len(r['refs'])} placed parts "
                                   f"({trunc(' '.join(r['refs']), 40)}) - want <= {a.span:.0f}",
                r['refs'])

    # --- BYPASS --------------------------------------------------------
    caps = defaultdict(list)
    for f in P:
        if prefix(f.ref) == 'C':
            for p in f.pads:
                if p['net']:
                    caps[p['net']].append(f)
    for f in P:
        if prefix(f.ref) != 'U' or len(f.pads) < 4:
            continue
        seen = set()
        for p in f.pads:
            if not p['net'] or not SUP_PIN.match(p['fn'] or ''):
                continue
            if GND_RE.match(p['net'].split('/')[-1]):
                continue      # e.g. NEO-M9N VDD_USB strapped to GND when USB is unused
            cl = [c for c in caps.get(p['net'], []) if c is not f]
            if not cl or p['net'] in seen:
                continue                    # no cap placed yet: not a placement fault
            seen.add(p['net'])
            d = min(pt_box_dist((p['x'], p['y']), c.crtyd) for c in cl)
            best = min(cl, key=lambda c: pt_box_dist((p['x'], p['y']), c.crtyd))
            if d > a.bypass:
                add('WARN', 'BYPASS', f"{f.ref}.{p['num']} ({p['fn']}, {unesc_disp(p['net'])}) "
                                      f"nearest placed bypass cap is {best.ref} at {d:.1f} mm "
                                      f"(want <= {a.bypass:g})", [f.ref, best.ref])

    n = len(F)
    F = [f for f in F if not suppressed(f, a.suppress)]
    return F, n - len(F)

def _corridor(b, f):
    """Parts standing between a connector and the nearest board edge."""
    c, o = f.crtyd, b.outline
    if not o:
        return []
    gaps = {'-x': c[0] - o[0], '+x': o[2] - c[2], '-y': c[1] - o[1], '+y': o[3] - c[3]}
    d = min(gaps, key=lambda k: gaps[k])
    if gaps[d] <= 0.5:
        return []
    lane = {'-x': (o[0], c[1], c[0], c[3]), '+x': (c[2], c[1], o[2], c[3]),
            '-y': (c[0], o[1], c[2], c[1]), '+y': (c[0], c[3], c[2], o[3])}[d]
    return [g for g in b.placed()
            if g is not f and g.back == f.back and not b.is_hole(g) and hit(lane, g.crtyd)]

def c_check(b, a):
    F, supp = gen_findings(b, a)
    if a.json:
        print(json.dumps(F, indent=1)); return 2 if any(x['severity'] == 'ERROR' for x in F) else 0
    n = defaultdict(int)
    for f in F:
        n[f['severity']] += 1
    print_findings(F, f"{b.path}: {n['ERROR']} error, {n['WARN']} warn, {n['INFO']} info "
                      f"({len(b.placed())} of {len(b.fps)} footprints placed)\n",
                   rules=RULES, cap=a.max)
    if not F:
        print("no findings")
    if supp:
        print(f"\n({supp} finding(s) suppressed via kpcb.json `suppress` list - "
              f"`--no-suppress` to see them)")
    if a.rules or not F:
        print("\nrules: " + ', '.join(f"{k}={v}" for k, v in sorted(RULES.items())))
    print("\nThese are geometric heuristics on placement only - no routing, no DRC, no "
          "3D bodies.\nConfirm anything that matters against the mechanical drawing or "
          "KiCad's own DRC.")
    return 2 if n['ERROR'] else 0
