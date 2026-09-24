"""kpcb.py `ic`: where a regulator's caps, inductor and feedback divider go."""
import re, math
from collections import defaultdict
from kcommon import natkey, trunc, prefix, parse_value, unesc_disp, GND_RE, rail_voltage
from kpcb_board import bbox, box_dist, ctr, grow, hit, overlap_area, SUP_PIN

# ---------------- IC placement helper ----------------
# `ic REF` answers "where do this part's passives go", not just "what is wrong".
# Everything below is computed from the real pad coordinates in the board file -
# there is no per-part template, so a part this tool has never seen still works
# as long as its pins are named.

ROLE_PAT = (
    ('SW',   r'SW|LX|PH|VSW|SWITCH|VLX'),        # before VIN: VSW is not an input
    ('BOOT', r'C?BOOT|BST|BTST|VBOOT|RBOOT'),
    ('VIN',  r'VIN|PVIN|VBUS|VCCIN|IN|AVIN|VDDIN'),
    ('OUT',  r'VOUT|OUT|SYS|VSYS'),
    ('FB',   r'FB|VFB|FBK|VSENSE|ADJ'),
    ('GND',  r'PGND|GND|AGND|DGND|VSS|EP|EPAD|PAD|THERMAL'),
    ('BIAS', r'VCC|BIAS|VDD|VREG|REGN|VDDA|AVDD'),
)
# anchored, so `~{INT}` never matches IN and PGOOD never matches GND
ROLE_RE = [(r, re.compile(r'^(' + p + r')\d*$', re.I)) for r, p in ROLE_PAT]
PASSIVE_PFX = ('C', 'R', 'L', 'FB', 'D')

def unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-9 else (1.0, 0.0)

def ray_exit(p, d, box):
    """How far along +d from p until we leave `box`. 0 if p is already out."""
    t = 0.0
    for i in (0, 1):
        if abs(d[i]) < 1e-9:
            continue
        lim = box[i + 2] if d[i] > 0 else box[i]
        t = max(t, (lim - p[i]) / d[i])
    return max(0.0, t)

def axis_snap(v):
    """Snap a direction to the nearest axis. Placement is orthogonal in practice
    and a 3-degree tilt in a recommendation is noise, not information."""
    return (math.copysign(1.0, v[0]), 0.0) if abs(v[0]) >= abs(v[1]) else (0.0, math.copysign(1.0, v[1]))

def fp_axis(f):
    """(pad1->pad2 board angle, length, width) for a two-pad part, else None.
    The angle is what a recommended rotation is computed against, so it comes
    from the pads themselves and never from an assumed footprint convention."""
    ps = [p for p in f.pads if p['num'] in ('1', '2')] or f.pads[:2]
    if len(ps) < 2:
        return None
    a, bp = ps[0], ps[1]
    ang = math.degrees(math.atan2(-(bp['y'] - a['y']), bp['x'] - a['x'])) % 360
    c = f.crtyd
    w, h = c[2] - c[0], c[3] - c[1]
    return (ang, max(w, h), min(w, h))

def want_rot(f, target_deg):
    """Rotation that turns f's pad1->pad2 axis to `target_deg` (board frame,
    +y down). Derived from where the pads actually sit now plus the footprint's
    current rotation, so it is right for any footprint orientation convention."""
    ax = fp_axis(f)
    if not ax:
        return round(f.rot, 1)
    # Snapped to 90 deg: a pin pair on a diagonal would otherwise ask for a
    # 165.3 deg part. The loop cost of the few degrees is nil, and nobody
    # hand-places at 165.3 deg.
    return round((ax[0] + f.rot - target_deg) / 90.0) % 4 * 90.0

def role_pads(f):
    by = defaultdict(list)
    for p in f.pads:
        fn = p['fn'] or ''
        for role, rx in ROLE_RE:
            if rx.match(fn):
                by[role].append(p); break
        else:
            # easyeda2kicad leaves pinfunction blank on plenty of parts; a blank
            # pin sitting on GND is still a ground pin
            if not fn and p['net'] and GND_RE.match(p['net'].split('/')[-1]):
                by['GND'].append(p)
    return by

def two_pin_on(b, net, other=None, pfx=PASSIVE_PFX):
    """Two-pin parts bridging `net` and (optionally) a net matching `other`."""
    out = []
    for r, _ in b.nets.get(net, []):
        f = b.fps.get(r)
        if not f or f in out or prefix(f.ref) not in pfx:
            continue
        nets = [p['net'] for p in f.pads if p['net']]
        if len(set(nets)) != 2:
            continue
        far = [n for n in nets if n != net]
        if not far:
            continue
        if other == 'GND' and not GND_RE.match(far[0].split('/')[-1]):
            continue
        if other not in (None, 'GND') and far[0] != other:
            continue
        out.append(f)
    return out

def rail_pick(b, ic, cands, want, kind, net):
    """Pick which of a rail's many caps belong to THIS regulator.

    A rail net carries every bypass cap on the board - VSYS here has 20 - so
    taking them all would be the `walk`-dumps-the-whole-rail mistake. The board
    file does carry one hard fact: where each cap physically sits. Rank by
    distance from the cap to the pad this cap serves on THIS regulator (VIN for
    CIN, the inductor/output node for COUT), so a same-value cap that really
    belongs to another regulator on the shared rail - sitting centimetres away -
    can't steal the slot. Sheet and refdes only break ties between caps that are
    equally close. Still say the pick is inferred: nothing in a .kicad_pcb proves
    which cap the schematic drew next to which pin."""
    if not cands:
        return [], ''
    # anchor = pad(s) on this net belonging to the regulator itself, else the
    # inductor (a buck's output net lives at the inductor, not on an IC pin).
    anc = [p for p in ic['fp'].pads if p.get('net') == net]
    if not anc and ic.get('L'):
        anc = [p for p in ic['L'].pads if p.get('net') == net]
    ax = sum(p['x'] for p in anc) / len(anc) if anc else ic['fp'].x
    ay = sum(p['y'] for p in anc) / len(anc) if anc else ic['fp'].y
    hint = [int(re.sub(r'\D', '', r) or 0) for r in ic['own_refs'] if prefix(r) == 'C']
    mid = sorted(hint)[len(hint) // 2] if hint else None
    def key(f):
        d = round(math.hypot(f.x - ax, f.y - ay), 1)   # 0.1 mm buckets
        n = int(re.sub(r'\D', '', f.ref) or 0)
        return (d, f.sheet != ic['fp'].sheet, abs(n - mid) if mid else 0, natkey(f.ref))
    ranked = sorted(cands, key=key)
    # one of each value first. "Smallest cap closest to the pin" only means
    # anything if the set actually holds a 100n, a 1u and a 10u rather than
    # three 1u that happened to sit next to each other in the refdes run.
    first, extra = {}, []
    for g in ranked:
        v = parse_value(g.value, 'C')
        extra.append(g) if v in first else first.setdefault(v, g)
    ranked = list(first.values()) + extra
    note = ''
    if len(cands) > want:
        pin = 'VIN' if kind == 'CIN' else 'output'
        note = (f"{len(cands)} caps sit on this rail; the {min(want,len(ranked))} physically "
                f"closest to {ic['fp'].ref}'s {pin} pad were taken as {kind}. "
                f"Override with --{kind.lower()} REF,REF if the schematic says otherwise.")
    return ranked[:want], note

def ic_context(b, f, a):
    """Everything the recommendation needs, gathered once."""
    ic = {'fp': f, 'pads': role_pads(f), 'warn': [], 'own_refs': [], 'assoc': {}}
    R = ic['pads']
    nets_of = lambda role: [p['net'] for p in R.get(role, []) if p['net']]

    # parts on this IC's own private nets: unambiguous, no rail guessing needed
    for p in f.pads:
        n = p['net']
        if not n or len(b.nets.get(n, [])) > a.assoc:
            continue
        if GND_RE.match(n.split('/')[-1]) or rail_voltage(n.split('/')[-1]) is not None:
            continue
        for g in two_pin_on(b, n):
            if g.ref != f.ref:
                ic['own_refs'].append(g.ref)
    ic['own_refs'] = sorted(set(ic['own_refs']), key=natkey)

    sw = set(nets_of('SW')) or {p['net'] for p in f.pads if p['net'] in b.sw_nets()}
    ic['sw_nets'] = sorted(sw)
    if not R.get('VIN'):
        # a plain IC names its supply +3V3 or VDD_IO, not VIN. Falling back to
        # the same SUP_PIN table the BYPASS rule uses turns `ic` into a bypass
        # placer for any part, which is the same loop rule at a smaller scale.
        R['VIN'] = [p for p in f.pads if p['net'] and SUP_PIN.match(p['fn'] or '')
                    and not GND_RE.match(p['net'].split('/')[-1])]
    ic['vin_nets'] = sorted(set(nets_of('VIN')))
    ic['out_nets'] = sorted(set(nets_of('OUT')))

    # the inductor decides the topology, so find it before naming anything
    ind = [g for n in sw for g in two_pin_on(b, n, pfx=('L',))]
    ind = sorted({g.ref: g for g in ind}.values(), key=lambda g: natkey(g.ref))
    ic['L'] = ind[0] if ind else None
    vout = None
    if ic['L']:
        far = [p['net'] for p in ic['L'].pads if p['net'] and p['net'] not in sw]
        if far:
            vout = far[0]
        else:
            # both ends switch: a 4-switch buck-boost. The output is the OUT pin.
            ic['topo'] = 'buck-boost (inductor between two switching nodes)'
            vout = ic['out_nets'][0] if ic['out_nets'] else None
    ic['vout'] = vout or (ic['out_nets'][0] if ic['out_nets'] else None)

    vin_v = next((rail_voltage(n.split('/')[-1]) for n in ic['vin_nets']), None)
    out_v = rail_voltage(ic['vout'].split('/')[-1]) if ic['vout'] else None
    if 'topo' not in ic:
        if not sw:
            ic['topo'] = 'linear / load switch (no switching node)' if ic['out_nets'] \
                         else 'not a regulator shape - generic bypass placement only'
        elif vin_v and out_v:
            ic['topo'] = 'buck' if out_v < vin_v else 'boost'
        else:
            ic['topo'] = 'switching (buck assumed; rail voltages unknown)'

    ov = {k: [x.strip() for x in v.split(',') if x.strip()]
          for k, v in (('CIN', a.cin), ('COUT', a.cout)) if v}
    def take(kind, net, want):
        if kind in ov:
            got = [b.fps[r] for r in ov[kind] if r in b.fps]
            miss = [r for r in ov[kind] if r not in b.fps]
            if miss:
                ic['warn'].append(f"--{kind.lower()}: no such footprint: {' '.join(miss)}")
            return got, ''
        if not net:
            return [], ''
        c = two_pin_on(b, net, 'GND', pfx=('C',))
        if len(b.nets.get(net, [])) <= a.assoc:
            return c, ''
        return rail_pick(b, ic, c, want, kind, net)

    ic['CIN'], n1 = take('CIN', ic['vin_nets'][0] if ic['vin_nets'] else None, a.ncin)
    ic['COUT'], n2 = take('COUT', ic['vout'], a.ncout)
    ic['notes'] = [n for n in (n1, n2) if n]
    ic['CBOOT'] = [g for n in nets_of('BOOT') for g in two_pin_on(b, n, pfx=('C',))]
    ic['CBIAS'] = [g for n in nets_of('BIAS') for g in two_pin_on(b, n, 'GND', pfx=('C',))]
    ic['FBparts'] = [g for n in nets_of('FB') for g in two_pin_on(b, n)]
    named = {g.ref for k in ('CIN', 'COUT', 'CBOOT', 'CBIAS', 'FBparts') for g in ic[k]}
    if ic['L']:
        named.add(ic['L'].ref)
    ic['misc'] = [r for r in ic['own_refs'] if r not in named]
    return ic

def _slot(ref, role, pos, rot, why, n=(0.0, 0.0)):
    return {'ref': ref, 'role': role, 'x': pos[0], 'y': pos[1], 'rot': rot,
            'why': why, 'n': n}

# who gets to keep its ideal spot when two slots collide. The loop parts come
# first because their whole reason for being there is the loop; a bias cap
# 2 mm further out costs nothing.
# The inductor keeps its spot ahead of everything: SW-pad-to-inductor is the
# shortest and most critical edge of the loop, and it is also the part with no
# room to spare. A third stacked input cap is what should move instead.
SLOT_PRIO = {'L': 0, 'CIN': 1, 'COUT': 2, 'CBOOT': 3, 'CBIAS': 4, 'FB': 5}

def resolve_slots(b, slots):
    """Push lower-priority slots outward until they stop overlapping.

    On a package that puts VIN, PGND, SW and BOOT on one corner - which is most
    of them, because that is what makes the loop small - the ideal spots
    genuinely collide. Reporting a pile of overlaps would be true and useless;
    the answer a person wants is the next-best spot, so take it."""
    done = []
    for s in sorted(slots, key=lambda s: (SLOT_PRIO.get(s['role'], 9), natkey(s['ref']))):
        n, moved = s['n'], 0.0
        while n != (0.0, 0.0) and moved < 12.0:
            box = slot_box(b, s)
            if not any(overlap_area(box, slot_box(b, d)) > 0.01 for d in done):
                break
            s['x'] += n[0] * 0.2; s['y'] += n[1] * 0.2
            moved += 0.2
        if moved:
            s['why'] += f" [pushed {moved:.1f} mm further out to clear another slot]"
        done.append(s)
    return slots

def ic_plan(b, ic, a):
    """Recommended position + rotation for every passive we could name.

    The one rule underneath all of it: a high-di/dt loop is made small by
    putting the cap across the pin pair that carries the loop, on the outside
    face of the package, with nothing between them."""
    f, R, out = ic['fp'], ic['pads'], []
    C = ctr(f.crtyd)
    gnd = R.get('GND', [])

    def straddle(hot, caps, role, label):
        """Caps placed across a (supply pin, return pin) pair, on the outside
        face of the package, smallest innermost. This is the whole loop-area
        rule, and it is the same rule for a buck's VIN/PGND pair and for a
        plain IC's VDD/GND bypass - so there is one implementation."""
        pairs = []
        for vp in hot:
            if not gnd:
                break
            gp = min(gnd, key=lambda g: math.hypot(g['x'] - vp['x'], g['y'] - vp['y']))
            pairs.append((math.hypot(gp['x'] - vp['x'], gp['y'] - vp['y']), vp, gp))
        pairs.sort(key=lambda t: t[0])
        seen, uniq = set(), []
        for d, vp, gp in pairs:
            k = (round(vp['x'], 2), round(vp['y'], 2))
            if k not in seen:
                seen.add(k); uniq.append((d, vp, gp))
        rows = defaultdict(float)
        for i, g in enumerate(sorted(caps, key=lambda g: parse_value(g.value, 'C') or 9e9)):
            if not uniq:
                break
            d, vp, gp = uniq[i % len(uniq)]
            ax = fp_axis(g)
            if not ax:
                continue
            u = unit((gp['x'] - vp['x'], gp['y'] - vp['y']))
            m = ((vp['x'] + gp['x']) / 2, (vp['y'] + gp['y']) / 2)
            n = axis_snap((-u[1], u[0]))
            if (m[0] - C[0]) * n[0] + (m[1] - C[1]) * n[1] < 0:
                n = (-n[0], -n[1])
            k = (round(n[0], 1), round(n[1], 1))
            off = ray_exit(m, n, f.crtyd) + a.gap + ax[2] / 2 + rows[k]
            rows[k] += ax[2] + a.gap
            p1 = next((p['net'] for p in g.pads if p['num'] == '1'), '')
            tgt = math.degrees(math.atan2(-u[1], u[0])) % 360
            if p1 and GND_RE.match(p1.split('/')[-1]):
                tgt = (tgt + 180) % 360
            out.append(_slot(g.ref, role, (m[0] + off * n[0], m[1] + off * n[1]),
                             want_rot(g, tgt),
                             f"across {label}.{vp['num']}/GND.{gp['num']} "
                             f"({d:.2f} mm apart), smallest value innermost", n))

    straddle(R.get('VIN', []), ic['CIN'], 'CIN', 'VIN')
    # -- inductor: outboard of the switch pads, body pointing away ---------
    swp = [p for p in f.pads if p['net'] in ic['sw_nets']]
    L = ic['L']
    Ldir = Lout = None
    if L and swp and fp_axis(L):
        ax = fp_axis(L)
        groups = defaultdict(list)
        for p in swp:
            groups[p['net']].append(p)
        cs = [((sum(q['x'] for q in v) / len(v)), (sum(q['y'] for q in v) / len(v)))
              for v in groups.values()]
        if len(cs) >= 2:                       # buck-boost: bridge the two nodes
            axis = unit((cs[1][0] - cs[0][0], cs[1][1] - cs[0][1]))
            m = ((cs[0][0] + cs[1][0]) / 2, (cs[0][1] + cs[1][1]) / 2)
            n = axis_snap((-axis[1], axis[0]))
            why = "bridges both switch nodes, just outboard of the SW pads"
        else:
            m = cs[0]
            n = axis_snap((m[0] - C[0], m[1] - C[1])) if (m != C) else (1.0, 0.0)
            axis = n
            why = "SW pad to inductor is the shortest edge of the switching loop"
        if (m[0] - C[0]) * n[0] + (m[1] - C[1]) * n[1] < 0:
            n = (-n[0], -n[1])
        off = ray_exit(m, n, f.crtyd) + a.gap + (ax[2] if len(cs) >= 2 else ax[1]) / 2
        pos = (m[0] + off * n[0], m[1] + off * n[1])
        tgt = math.degrees(math.atan2(-axis[1], axis[0])) % 360
        p1 = next((p['net'] for p in L.pads if p['num'] == '1'), '')
        if len(cs) < 2 and p1 and p1 not in ic['sw_nets']:
            tgt = (tgt + 180) % 360
        out.append(_slot(L.ref, 'L', pos, want_rot(L, tgt), why, n))
        Ldir, Lout = n, (pos[0] + n[0] * ax[1] / 2, pos[1] + n[1] * ax[1] / 2)

    # -- output caps: immediately after the inductor, in the current path ---
    if Lout:
        # side by side across the output node, not end to end: three caps in a
        # line would put the last one 25 mm downstream of the first
        side = (-Ldir[1], Ldir[0])
        cs = sorted(ic['COUT'], key=lambda g: -(parse_value(g.value, 'C') or 0))
        wid = [fp_axis(g)[2] + a.gap for g in cs if fp_axis(g)]
        run = -sum(wid) / 2
        for g in cs:
            ax = fp_axis(g)
            if not ax:
                continue
            off = a.gap + ax[1] / 2
            lat = run + ax[2] / 2
            run += ax[2] + a.gap
            tgt = math.degrees(math.atan2(-Ldir[1], Ldir[0])) % 360
            p1 = next((p['net'] for p in g.pads if p['num'] == '1'), '')
            if p1 and GND_RE.match(p1.split('/')[-1]):
                tgt = (tgt + 180) % 360
            out.append(_slot(g.ref, 'COUT',
                             (Lout[0] + off * Ldir[0] + lat * side[0],
                              Lout[1] + off * Ldir[1] + lat * side[1]),
                             want_rot(g, tgt),
                             "a bank across the output node right at the "
                             "inductor's output pad, largest first", Ldir))

    if not Lout and ic['COUT']:
        # linear regulator or load switch: no inductor, so the output cap sits
        # across OUT/GND exactly the way the input cap sits across IN/GND
        straddle(R.get('OUT', []), ic['COUT'], 'COUT', 'OUT')

    # -- boot / bias caps: at their own pins, they are small loops too ------
    used = defaultdict(float)          # one stack per face, shared by both kinds
    for kind, role in (('CBOOT', 'BOOT'), ('CBIAS', 'BIAS')):
        for g in ic[kind]:
            ps = [p for p in f.pads if p['net'] in {q['net'] for q in g.pads}]
            ax = fp_axis(g)
            if not ps or not ax:
                continue
            m = (sum(p['x'] for p in ps) / len(ps), sum(p['y'] for p in ps) / len(ps))
            n = axis_snap((m[0] - C[0], m[1] - C[1]))
            k = (round(n[0], 1), round(n[1], 1))
            off = ray_exit(m, n, f.crtyd) + a.gap + ax[2] / 2 + used[k]
            used[k] += ax[2] + a.gap
            out.append(_slot(g.ref, kind, (m[0] + off * n[0], m[1] + off * n[1]),
                             want_rot(g, 90 if abs(n[0]) < 0.5 else 0),
                             f"hard against the {role} pin(s) it serves", n))

    # -- feedback divider: outboard of FB, away from SW ---------------------
    fbp = R.get('FB', [])
    if fbp and ic['FBparts']:
        m = (sum(p['x'] for p in fbp) / len(fbp), sum(p['y'] for p in fbp) / len(fbp))
        n = axis_snap((m[0] - C[0], m[1] - C[1]))
        along = (-n[1], n[0])
        run = 0.0
        for g in sorted(ic['FBparts'], key=lambda g: natkey(g.ref)):
            ax = fp_axis(g)
            if not ax:
                continue
            off = ray_exit(m, n, f.crtyd) + a.gap + ax[1] / 2
            lat = run + ax[2] / 2
            run += ax[2] + a.gap
            pos = (m[0] + off * n[0] + along[0] * lat, m[1] + off * n[1] + along[1] * lat)
            tgt = math.degrees(math.atan2(-n[1], n[0])) % 360
            out.append(_slot(g.ref, 'FB', pos, want_rot(g, tgt),
                             "FB node kept short and pointed away from SW; "
                             "route FB on a layer with GND under it", n))
    return resolve_slots(b, out)

# uppercase = an IC pin, lowercase = a recommended slot; they must not collide
ROLE_MARK = {'VIN': 'V', 'GND': 'G', 'SW': 'S', 'FB': 'F', 'BOOT': 'B',
             'OUT': 'O', 'BIAS': 'X'}

def slot_box(b, s):
    ax = fp_axis(b.fps[s['ref']])
    if not ax:
        return (s['x'] - .5, s['y'] - .5, s['x'] + .5, s['y'] + .5)
    horiz = min(s['rot'] % 180, 180 - s['rot'] % 180) < 45
    w, h = (ax[1], ax[2]) if horiz else (ax[2], ax[1])
    return (s['x'] - w / 2, s['y'] - h / 2, s['x'] + w / 2, s['y'] + h / 2)

def ic_diagram(b, ic, slots, cols):
    """A picture of the recommendation, to glance at. Same ASCII-grid trick as
    `map`, but scoped to one IC so a cell is a few tenths of a millimetre.

    ic['off'] is the anchor shift: the slots are already in board coordinates
    but the IC's own pads are still where it is parked, so its geometry moves
    here too or the frame stretches across the whole pile."""
    f = ic['fp']
    ox, oy = ic.get('off', (0.0, 0.0))
    crtyd = (f.crtyd[0] + ox, f.crtyd[1] + oy, f.crtyd[2] + ox, f.crtyd[3] + oy)
    boxes = [slot_box(b, s) for s in slots]
    r = bbox([(v[i], v[j]) for v in [crtyd] + boxes for i, j in ((0, 1), (2, 3))])
    r = grow(r, 0.6)
    cw = max((r[2] - r[0]) / cols, 0.05)
    ch = cw * 2.0
    nr = max(3, int(math.ceil((r[3] - r[1]) / ch)))
    g = [[' '] * cols for _ in range(nr)]

    def stamp(box, ch_, over=True):
        for gy in range(max(0, int((box[1] - r[1]) / ch)),
                        min(nr, int((box[3] - r[1]) / ch) + 1)):
            for gx in range(max(0, int((box[0] - r[0]) / cw)),
                            min(cols, int((box[2] - r[0]) / cw) + 1)):
                # first writer wins: a cell straddling two boxes that merely
                # sit next to each other is not a clash, and painting it '*'
                # made a correct layout look broken
                if g[gy][gx] in (' ', '.') or (over and g[gy][gx] == '.'):
                    g[gy][gx] = ch_

    stamp(crtyd, '.', over=False)
    keys = {}
    for i, (s, box) in enumerate(zip(slots, boxes)):
        k = chr(ord('a') + i) if i < 26 else '?'
        keys[k] = s
        stamp(box, k)
    # a placed part standing in a slot, clipped to the clash itself: stamping
    # its whole courtyard once painted the entire frame '!' and said nothing
    for gname, gfp in b.fps.items():
        if gfp is f or not gfp.placed or gfp.back != f.back or b.is_hole(gfp) \
           or gname in {s['ref'] for s in slots}:
            continue
        for box in boxes:
            if hit(box, gfp.crtyd):
                stamp((max(box[0], gfp.crtyd[0]), max(box[1], gfp.crtyd[1]),
                       min(box[2], gfp.crtyd[2]), min(box[3], gfp.crtyd[3])), '!')
    # pins last and one cell each, so they always survive and never fight
    R = role_pads(f)
    for role, ps in R.items():
        if role not in ROLE_MARK:
            continue
        for p in ps:
            gx, gy = int((p['x'] + ox - r[0]) / cw), int((p['y'] + oy - r[1]) / ch)
            if 0 <= gx < cols and 0 <= gy < nr:
                g[gy][gx] = ROLE_MARK[role]
    out = [f"  recommended layout, 1 cell = {cw:.2f} x {ch:.2f} mm, x+ right / y+ down",
           "  +" + "-" * cols + "+"]
    out += ["  |" + ''.join(row) + "|" for row in g]
    out.append("  +" + "-" * cols + "+")
    out.append("  IC body '.'   pins " + ' '.join(f"{v}={k}" for k, v in ROLE_MARK.items())
               + "   '!' = a placed part is standing in that slot")
    out.append(f"  cells are {cw:.2f} mm wide, so two slots can share one - the table "
               f"above has the real numbers")
    out.append("  " + '   '.join(f"{k}={keys[k]['ref']}" for k in sorted(keys)))
    return out

ANCHOR_ORDER = {'L': 0, 'COUT': 1, 'CIN': 2, 'CBOOT': 3, 'CBIAS': 4, 'FB': 5}

def ic_anchor(b, ic, slots, a):
    """Re-hang the whole recommendation off a part that is already placed.

    Mid-placement the big parts land first: on this board the inductors are
    down and the regulators are still in the parked pile. Without this the
    tool reports 'L4 is 90 mm from its slot', which is true and useless - the
    inductor is not the thing that should move. Anchoring instead answers the
    question actually being asked: given L4 where it is, where does U13 go.

    Translation only. If the anchor also needs turning, that is said out loud
    rather than guessed at, because rotating the IC changes every pad position
    and the honest fix is to rotate it in KiCad and re-run."""
    if a.anchor.lower() in ('none', '-'):
        return None
    cands = [s for s in slots if b.fps[s['ref']].placed]
    if a.anchor:
        cands = [s for s in cands if s['ref'].upper() == a.anchor.upper()]
        if not cands:
            ic['warn'].append(f"--anchor {a.anchor}: not one of this IC's placed "
                              f"passives, ignored")
            return None
    elif ic['fp'].placed:
        return None                       # the IC itself is the anchor already
    if not cands:
        return None
    s = min(cands, key=lambda s: (ANCHOR_ORDER.get(s['role'], 9), natkey(s['ref'])))
    g = b.fps[s['ref']]
    dx, dy = g.x - s['x'], g.y - s['y']
    dr = (g.rot - s['rot']) % 360
    for t in slots:
        t['x'] += dx; t['y'] += dy
    ic['off'] = (dx, dy)
    return {'ref': s['ref'], 'dx': dx, 'dy': dy, 'dr': dr if dr <= 180 else dr - 360}

def c_ic(b, a):
    """Recommended placement for a regulator's supporting passives."""
    if not a.args:
        cands = [f for f in b.fps.values()
                 if prefix(f.ref) == 'U' and (role_pads(f).get('SW') or
                    role_pads(f).get('OUT')) and len(f.pads) >= 5]
        if not cands:
            print("no regulator-shaped part found (needs a pin named SW/LX/OUT). "
                  "`ic REF` works on any IC.")
            return 1
        print("`ic REF` gives a placement recommendation for an IC's passives. "
              "Candidates on this board:\n")
        for f in sorted(cands, key=lambda f: natkey(f.ref)):
            R = role_pads(f)
            print(f"  {f.ref:<5} {trunc(f.value,22):<22} "
                  f"{'placed' if f.placed else 'UNPLACED':<8} "
                  f"{'switching' if R.get('SW') else 'linear'}")
        return 0
    rc = 0
    for ref in a.args:
        f = b.fps.get(ref)
        if not f:
            print(f"{ref}: NOT FOUND"); rc = 1; continue
        ic = ic_context(b, f, a)
        slots = ic_plan(b, ic, a)
        anc = ic_anchor(b, ic, slots, a)
        print(f"\n=== {f.ref}  {f.value}   {ic['topo']}")
        if anc:
            print(f"  {f.ref} is NOT PLACED. Anchored on {anc['ref']}, which is: "
                  f"put {f.ref} at {f.x+anc['dx']:.2f},{f.y+anc['dy']:.2f} "
                  f"rot {f.rot:g} {f.layer} and the rows below follow.")
            if abs(anc['dr']) > 5:
                print(f"  {anc['ref']} is rotated {anc['dr']:+.0f} deg from the slot it "
                      f"wants. Turn {f.ref} by {anc['dr']:+.0f} deg in KiCad and re-run - "
                      f"rotating it moves every pad, so these numbers assume you have.")
        else:
            print(f"  {'placed at' if f.placed else 'NOT PLACED, parked at'} "
                  f"{f.x:.2f},{f.y:.2f} rot {f.rot:g} {f.layer}"
                  + ("" if f.placed else "  - the coordinates below are relative to "
                                         "the parked IC, so place it (or `--anchor` "
                                         "one of its placed passives) and re-run"))
        print(f"  VIN {', '.join(unesc_disp(n) for n in ic['vin_nets']) or '?'}"
              f"   SW {', '.join(unesc_disp(n) for n in ic['sw_nets']) or 'none'}"
              f"   VOUT {unesc_disp(ic['vout']) if ic['vout'] else '?'}")
        got = {s['ref'] for s in slots}
        named = [(k, [g.ref for g in ic[k]]) for k in ('CIN', 'COUT', 'CBOOT', 'CBIAS')]
        named.append(('L', [ic['L'].ref] if ic['L'] else []))
        named.append(('FB', [g.ref for g in ic['FBparts']]))
        if any(v for _, v in named):
            print("  parts     : " + '  '.join(f"{k}={' '.join(v)}" for k, v in named if v))
        if ic['misc']:
            print(f"  also on its own nets (not positioned): {' '.join(ic['misc'])}")

        if not slots:
            print("  nothing positionable found - no VIN/GND pin pair, no inductor on "
                  "the SW net, and no caps on its private nets.")
            for w in ic['warn'] + ic['notes']:
                print(f"  note: {w}")
            continue

        print(f"\n  {'ref':<6} {'role':<6} {'suggest x,y':<18} {'rot':>5}  "
              f"{'now':<20} why")
        for s in sorted(slots, key=lambda s: (s['role'], natkey(s['ref']))):
            g = b.fps[s['ref']]
            d = math.hypot(g.x - s['x'], g.y - s['y'])
            dr = min((g.rot - s['rot']) % 180, (s['rot'] - g.rot) % 180)
            now = 'unplaced' if not g.placed else (
                'OK' if d <= a.tol and dr <= 5 else f"{d:.1f} mm / {dr:.0f} deg off")
            print(f"  {s['ref']:<6} {s['role']:<6} "
                  f"{f'{s['x']:.2f},{s['y']:.2f}':<18} {s['rot']:>5.1f}  "
                  f"{now:<20} {s['why']}")

        print()
        for line in ic_diagram(b, ic, slots, min(a.cols, 64)):
            print(line)

        # -- the checks that only make sense once a target exists ----------
        msgs = list(ic['warn'])
        # two slots wanting the same space is a real outcome, not a bug: on a
        # package whose VIN, PGND and SW pins all sit on one corner the input
        # caps and the inductor both want to go there. Say so - the diagram
        # paints first-writer-wins and would hide it.
        boxes = [(t, slot_box(b, t)) for t in slots]
        for i, (t, bx) in enumerate(boxes):
            for u, by in boxes[i + 1:]:
                A = overlap_area(bx, by)
                if A > 0.01:
                    msgs.append(f"the slots for {t['ref']} ({t['role']}) and "
                                f"{u['ref']} ({u['role']}) overlap by {A:.2f} mm2 - "
                                f"both want the same face of the package; move one "
                                f"out and accept the longer loop on that one")
        blockers = defaultdict(list)
        for s in slots:
            box = slot_box(b, s)
            for g in b.fps.values():
                if g is f or g.ref in got or not g.placed or b.is_hole(g):
                    continue
                if g.back == f.back and hit(box, g.crtyd):
                    blockers[g.ref].append(s['ref'])
        for r, who in sorted(blockers.items(), key=lambda kv: natkey(kv[0]))[:a.max]:
            msgs.append(f"{r} is already placed inside the slot suggested for "
                        f"{' '.join(who)} - move one of them")
        swset = set(ic['sw_nets'])
        noisy = [g for g in b.fps.values()
                 if g.placed and any(p['net'] in swset for p in g.pads)]
        for g in ic['FBparts']:
            if not g.placed:
                continue
            for h in noisy:
                d = box_dist(g.crtyd, h.crtyd)
                if d < a.fb:
                    msgs.append(f"{g.ref} (feedback) is {d:.1f} mm from switching-node "
                                f"part {h.ref} - want >= {a.fb:g} mm, and never under "
                                f"the inductor")
        if ic['CIN'] and ic['pads'].get('VIN') and f.placed:
            # same measure on both sides - cap centre to the VIN pad - or the
            # comparison flatters whichever one is measured pad-to-pad
            vp = ic['pads']['VIN'][0]
            for g in ic['CIN'][:1]:
                t = next((s for s in slots if s['ref'] == g.ref), None)
                if g.placed and t:
                    now = math.hypot(g.x - vp['x'], g.y - vp['y'])
                    new = math.hypot(t['x'] - vp['x'], t['y'] - vp['y'])
                    verdict = (f"{now-new:.1f} mm shorter" if new < now - 0.2
                               else "no better - this package's VIN and GND pins are "
                                    "too far apart for the cap that has to bridge them")
                    msgs.append(f"input loop, {g.ref} centre to VIN.{vp['num']}: "
                                f"{now:.1f} mm now, {new:.1f} mm in the slot above "
                                f"({verdict})")
        if ic['pads'].get('FB') and ic['pads'].get('SW'):
            fx = ctr(f.crtyd)
            same = all((p['x'] - fx[0]) * (q['x'] - fx[0]) +
                       (p['y'] - fx[1]) * (q['y'] - fx[1]) > 0
                       for p in ic['pads']['FB'] for q in ic['pads']['SW'])
            if same:
                msgs.append("FB and SW pins are on the same face of the package - "
                            "run the FB trace out and around, never past the SW pad")
        for m in msgs[:a.max] + ic['notes']:
            print(f"  note: {m}")
        if len(msgs) > a.max:
            print(f"  ... +{len(msgs)-a.max} more note(s)")
        print("  Positions are geometric suggestions from pad coordinates and the "
              "loop rules, not\n  a datasheet layout. Check them against the "
              "datasheet's own layout example.")
    return rc
