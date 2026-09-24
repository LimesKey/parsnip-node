"""kpcb.py `rf`: controlled-impedance RF traces, reference plane and via fence."""
import sys, re, math
from collections import defaultdict
from kzo import microstrip, field_zo
from kcommon import natkey, unesc_disp, GND_RE
from kpcb_board import _resolve_net, point_in_poly, pt_seg_dist, thick_of

# ---------------- RF traces ----------------

RF_FREQ = ((re.compile(r'GNSS|GPS|L1|L5', re.I), 1575.42),     # sheet/net -> MHz, for lambda/20
           (re.compile(r'LORA|915|SX12', re.I), 915.0))

def _ref_below(b, layer):
    """(next copper layer, dielectric height mm, thickness-weighted er) toward
    the board centre from an OUTER layer, from the stackup."""
    names = [n for n, *_ in b.stack]
    if layer not in names:
        return None
    i = names.index(layer)
    step = 1 if layer == (b.copper[0] if b.copper else 'F.Cu') else -1
    h = her = 0.0
    j = i + step
    while 0 <= j < len(b.stack) and b.stack[j][1] != 'copper':
        _, _, th, er = b.stack[j]
        h += th; her += th * er
        j += step
    if not (0 <= j < len(b.stack)) or h <= 0:
        return None
    return b.stack[j][0], h, her / h

def _mask_on(b, layer):
    """(thickness mm, er) of the solder mask over an outer copper layer, from
    the stackup, or None."""
    names = [n for n, *_ in b.stack]
    if layer not in names:
        return None
    i = names.index(layer)
    for j in (i - 1, i + 1):
        if 0 <= j < len(b.stack) and b.stack[j][0].endswith('.Mask') and b.stack[j][2] > 0:
            return b.stack[j][2], b.stack[j][3] or 3.8
    return None

def _side_gap(t, fills, w):
    """Per-side gap (left, right) in mm from a trace segment's midpoint to the
    nearest edge of a same-layer GND fill: the CPWG slot width. None = no fill
    edge within 2 mm on that side."""
    (ax, ay), (bx, by) = t['a'], t['b']
    mx, my = t['mid']
    gaps = [None, None]
    for fl in fills:
        x0, y0, x1, y1 = fl['bbox']
        if mx < x0 - 2 or mx > x1 + 2 or my < y0 - 2 or my > y1 + 2:
            continue
        pts = fl['pts']
        for i in range(len(pts)):
            p, q = pts[i - 1], pts[i]
            if min(p[0], q[0]) > mx + 2 or max(p[0], q[0]) < mx - 2 or \
               min(p[1], q[1]) > my + 2 or max(p[1], q[1]) < my - 2:
                continue
            vx, vy = q[0] - p[0], q[1] - p[1]
            L = vx * vx + vy * vy
            u = 0.0 if L <= 0 else max(0.0, min(1.0, ((mx - p[0]) * vx + (my - p[1]) * vy) / L))
            cx, cy = p[0] + u * vx, p[1] + u * vy
            d = math.hypot(cx - mx, cy - my) - w / 2
            if d > 2 or d < -1e-6:
                continue
            side = 0 if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0 else 1
            if gaps[side] is None or d < gaps[side]:
                gaps[side] = d
    return gaps

def c_rf(b, a):
    """`rf [NET...]`: one-call review of a 50-ohm trace. With no net, every net
    whose netclass names RF/50 ohm. Per net: routed width uniformity (necks),
    microstrip Zo from the stackup plus the same-layer GND gap (coplanar
    coupling), reference-plane coverage under the trace, and the GND via fence."""
    names = list(a.args) + list(a.net or [])
    if not names:
        names = sorted({n for n in set(b.nets) | {t['net'] for t in b.tracks}
                        if n and re.search(r'RF|50', b.netclass(n), re.I)}, key=natkey)
        if not names:
            print("no net in an RF/50-ohm netclass - name one: `rf NET`", file=sys.stderr); return 1
    fence = a.fence
    rc = 0
    for name in names:
        net, cand = _resolve_net(b, name)
        if not net:
            print(f"{name}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else ''))
            rc = 1; continue
        segs = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
        refs = sorted({r for r, _ in b.nets.get(net, [])}, key=natkey)
        sheets = ' '.join(sorted({b.fps[r].sheet for r in refs if r in b.fps}))
        f_mhz = a.freq or next((mhz for rx, mhz in RF_FREQ if rx.search(net + ' ' + sheets)), None)
        print(f"\n=== {unesc_disp(net)}   netclass {b.netclass(net) or 'Default'}   pads "
              f"{' '.join(refs[:8])}   {sheets}"
              + (f"   f {f_mhz:g} MHz{'' if a.freq else ' (inferred; --freq to set)'}" if f_mhz else ''))
        if not segs:
            print("  no routed track (pads only, or joined by pour)"); continue
        eeff_net = None
        for ly in b.copper:
            ss = [t for t in segs if t['layer'] == ly]
            if not ss:
                continue
            bylen = defaultdict(float)
            for t in ss:
                bylen[round(t['w'], 4)] += t['len']
            dom = max(bylen, key=bylen.get)
            tot = sum(bylen.values())
            widths = ', '.join(f"{w:.3f} x {l:.1f} mm" for w, l in sorted(bylen.items()))
            print(f"  {ly:<7} {len(ss)} seg, {tot:.1f} mm   widths: {widths}")
            necks = sorted((t for t in ss if t['w'] < dom * 0.95), key=lambda t: t['w'])
            for t in necks[:4]:
                print(f"    !! NECK {t['w']:.3f} mm (dominant {dom:.3f}) for {t['len']:.2f} mm "
                      f"at {t['mid'][0]:.2f},{t['mid'][1]:.2f}")
            if ly not in b.outer:
                print("    Zo: inner-layer stripline, not modelled here"); continue
            ref = _ref_below(b, ly)
            if not ref:
                print("    Zo: no stackup in the board file"); continue
            rly, h, er = ref
            z, eeff = microstrip(dom, h, er, thick_of(b, ly))
            eeff_net = eeff_net or eeff
            gnd_same = [fl for fl in b.fills if fl['layer'] == ly and GND_RE.match(fl['net'].split('/')[-1] or '')]
            gl, gr = [], []
            for t in ss:
                if t['len'] >= 0.2:
                    l_, r_ = _side_gap(t, gnd_same, t['w'])
                    if l_ is not None: gl.append(l_)
                    if r_ is not None: gr.append(r_)
            med = lambda v: sorted(v)[len(v) // 2] if v else None
            fmt2 = lambda x: '-' if x is None else f"{x:.2f}"
            print(f"    Zo {z:.1f} ohm microstrip ({dom:.3f} mm over {rly}, h {h:.3f} er {er:.2f}, "
                  f"closed form, uncoated)")
            # Side grounds within a few h pull Zo down (CPWG). The closed forms
            # for that need h >> w+2s, the opposite of a 4-layer board, so it is
            # field-solved (kzo.py) with the median gap per side and the mask.
            if gl or gr:
                sl, sr = med(gl), med(gr)
                near = [x for x in (sl, sr) if x is not None and x < 5 * h]
                print(f"    same-layer GND gap L {fmt2(sl)} / R {fmt2(sr)} mm (min {min(gl + gr):.2f})"
                      + ("" if near else "  -> >= 5h away: the microstrip figure holds"))
                if near:
                    # 0.01 mm steps (~0.1 ohm): nets and sides then share solves
                    gaps = tuple(round(x, 2) if x is not None and x < 5 * h else None for x in (sl, sr))
                    mask = _mask_on(b, ly)
                    zc, ee = field_zo(dom, h, er, thick_of(b, ly), s=gaps, mask=mask)
                    zu = field_zo(dom, h, er, thick_of(b, ly), s=gaps)[0] if mask else None
                    eeff_net = ee
                    print(f"    Zo {zc:.1f} ohm CPWG, field-solved"
                          + (f" with {ly[0]}.Mask {mask[0] * 1000:.0f} um er {mask[1]:g}"
                             f" ({zu:.1f} uncoated)" if mask else ', uncoated'))
            # reference plane continuity under the trace
            rfills = [fl for fl in b.fills if fl['layer'] == rly]
            samp = [q for t in ss for q in (t['a'], t['mid'], t['b'])]
            under = defaultdict(int)
            for q in samp:
                hitn = next((fl['net'] for fl in rfills if fl['bbox'][0] <= q[0] <= fl['bbox'][2]
                             and fl['bbox'][1] <= q[1] <= fl['bbox'][3] and point_in_poly(q, fl['pts'])), None)
                under[hitn] += 1
            desc = ', '.join(f"{'NOTHING' if n is None else n} {100*c/len(samp):.0f}%"
                             for n, c in sorted(under.items(), key=lambda kv: -kv[1]))
            bad = under.get(None, 0) or any(n and not GND_RE.match(n.split('/')[-1]) for n in under)
            print(f"    reference {rly} under the trace: {desc}"
                  + ("   <-- BROKEN/NON-GND RETURN PATH" if bad else ''))
            rc = rc or (2 if bad else 0)
        # GND via fence: vias within `fence` mm of the trace edge, per side
        side_v = ([], [])
        for v in b.vias:
            if not GND_RE.match((v['net'] or '').split('/')[-1] or ''):
                continue
            best = min(((pt_seg_dist((v['x'], v['y']), t['a'], t['b']) - t['w'] / 2, t) for t in segs),
                       key=lambda x: x[0])
            if best[0] <= fence:
                t = best[1]
                (ax, ay), (bx, by) = t['a'], t['b']
                sd = 0 if (bx - ax) * (v['y'] - ay) - (by - ay) * (v['x'] - ax) < 0 else 1
                side_v[sd].append((v['x'], v['y']))
        def maxnn(vs):
            return max((min(math.dist(p, q) for q in vs if q is not p) for p in vs), default=None) \
                if len(vs) > 1 else None
        nl, nr = maxnn(side_v[0]), maxnn(side_v[1])
        lim = (299792.458 / f_mhz / math.sqrt(eeff_net or 1) / 20) if f_mhz else None
        fmt = lambda x: '-' if x is None else f"{x:.1f}"
        verdict = ''
        if lim:
            worst = max((x for x in (nl, nr) if x is not None), default=None)
            verdict = (f"   lambda/20 = {lim:.1f} mm: " +
                       ('NO FENCE' if not side_v[0] and not side_v[1] else
                        'one side unfenced' if not (side_v[0] and side_v[1]) else
                        'OK' if worst is not None and worst <= lim else 'GAPS WIDER THAN lambda/20'))
        print(f"  GND fence (<= {fence:g} mm from the trace edge): {len(side_v[0]) + len(side_v[1])} vias, "
              f"L {len(side_v[0])} / R {len(side_v[1])}, widest nearest-neighbour gap "
              f"L {fmt(nl)} / R {fmt(nr)} mm{verdict}")
    print("\nZo: microstrip is Hammerstad's closed form (uncoated, ~1%); CPWG is a 2D field solve\n"
          "(kzo.py, ~1% vs exact cases) on the board file's stackup, rectangular copper, median\n"
          "gap per side. The fab's stackup (and its etch trapezoid) is the arbiter: check h and er\n"
          "above match it. Fills are the LAST SAVED state. Fence gaps are nearest-neighbour\n"
          "spacing along each side, a proxy for pitch on a bent trace.")
    return rc
