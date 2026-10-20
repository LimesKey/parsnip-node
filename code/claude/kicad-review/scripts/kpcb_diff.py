"""kpcb.py `diff OLD.kicad_pcb`: what an edit changed, OLD -> this board - the fingerprint
every board edit needs. Items are matched by uuid (the writers keep them, KiCad keeps
them): footprints (moved, rotated, flipped, added, removed), their visible silk text
(refdes moved, resized, hidden), board silk items, vias, zones (teardrops counted apart),
and tracks - removed, added, modified (width only, or geometry), net or layer changes on
kept ones, length per layer, and the series resistance of each net's changed tracks
(stackup copper thickness per layer)."""
import math
from collections import defaultdict, Counter
from kcommon import natkey, unesc_disp
from kpcb_board import Board, RHO_CU, thick_of


def _r(b, t):
    return RHO_CU * t['len'] / max(t['w'] * thick_of(b, t['layer']), 1e-9)


def c_diff(b, a):
    if not a.args:
        print("diff OLD.kicad_pcb   (what changed from OLD to this board)"); return 1
    try:
        o = Board(a.args[0])
    except (FileNotFoundError, ValueError) as e:
        print(f"diff: {e}"); return 3
    print(f"diff {a.args[0]} -> {b.path}")
    lines, same = [], []
    # footprints
    mv, rot, flip, txt, placed = [], [], [], [], set()
    for ref in sorted(set(b.fps) & set(o.fps), key=natkey):
        f, g = b.fps[ref], o.fps[ref]
        if f.layer != g.layer:
            flip.append(ref)
        if math.dist((f.x, f.y), (g.x, g.y)) > 1e-4:
            mv.append(f"{ref} {f.x - g.x:+.3f},{f.y - g.y:+.3f}")
        if abs((f.rot - g.rot + 180) % 360 - 180) > 1e-6:
            rot.append(f"{ref} {g.rot:g}->{f.rot:g}")
        if f.layer != g.layer or math.dist((f.x, f.y), (g.x, g.y)) > 1e-4 or f.rot != g.rot:
            placed.add(ref)
        if sorted(f.texts) != sorted(g.texts):
            txt.append(ref)
    add, rem = sorted(set(b.fps) - set(o.fps), key=natkey), sorted(set(o.fps) - set(b.fps), key=natkey)
    fp_n = len(set(b.fps) & set(o.fps)) - len(placed)
    if mv or rot or flip or add or rem:
        lines.append(f"footprints: {len(mv)} moved, {len(rot)} rotated, {len(flip)} flipped, {len(add)} added, "
                     f"{len(rem)} removed")
        for tag, xs in (('moved', mv), ('rotated', rot), ('flipped', flip), ('added', add), ('removed', rem)):
            if xs:
                lines.append(f"  {tag:<8} " + ', '.join(xs[:a.max]) + (f" +{len(xs) - a.max}" if len(xs) > a.max else ''))
    else:
        same.append(f"footprints ({fp_n})")
    # silk: footprint text and board graphics on the silkscreen
    still = [r for r in txt if r not in placed]
    sb, so = Counter((ly, tuple(round(v, 3) for v in bb)) for ly, bb in b.silk), \
        Counter((ly, tuple(round(v, 3) for v in bb)) for ly, bb in o.silk)
    if txt or sb != so:
        lines.append(f"silk: visible text changed on {len(txt)} footprint(s) ({len(still)} of them did not move)"
                     + (f"; board silk items {sum(so.values())} -> {sum(sb.values())}, "
                        f"{sum((sb - so).values())} new/changed" if sb != so else ''))
        if still:
            lines.append("  text only: " + ' '.join(still[:a.max * 2]) + (f" +{len(still) - 2 * a.max}" if len(still) > 2 * a.max else ''))
    else:
        same.append("silk")
    # vias
    vb, vo = {v['uuid']: v for v in b.vias}, {v['uuid']: v for v in o.vias}
    key = lambda v: (round(v['x'], 4), round(v['y'], 4), v['size'], v['drill'], v['net'], tuple(v['layers']), v['free'])  # noqa: E731
    vch = [u for u in set(vb) & set(vo) if key(vb[u]) != key(vo[u])]
    va, vr = set(vb) - set(vo), set(vo) - set(vb)
    if vch or va or vr:
        lines.append(f"vias: {len(vch)} changed, {len(va)} added, {len(vr)} removed ({len(vo)} -> {len(vb)})")
        for u in sorted(vch)[:a.max]:
            v, w = vb[u], vo[u]
            lines.append(f"  {unesc_disp(v['net'])} {w['x']:.3f},{w['y']:.3f} -> {v['x']:.3f},{v['y']:.3f}"
                         + (f" net {unesc_disp(w['net'])} -> {unesc_disp(v['net'])}" if v['net'] != w['net'] else ''))
    else:
        same.append(f"vias ({len(vb)})")
    # zones (not teardrops)
    zb, zo = {z.uuid: z for z in b.zones}, {z.uuid: z for z in o.zones}
    zk = lambda z: (z[0], tuple(z[1]), tuple(z.outline), z.priority, z.keepout)  # noqa: E731
    zch = [u for u in set(zb) & set(zo) if zk(zb[u]) != zk(zo[u])]
    za, zr = set(zb) - set(zo), set(zo) - set(zb)
    if zch or za or zr:
        lines.append(f"zones: {len(zch)} changed, {len(za)} added, {len(zr)} removed (not counting teardrops)")
        for u in sorted(zch)[:a.max]:
            lines.append(f"  changed {unesc_disp(zb[u][0]) or 'rule area'} {'/'.join(zb[u][1])} '{zb[u].name}'")
        for u in sorted(za)[:a.max]:
            lines.append(f"  added   {unesc_disp(zb[u][0]) or 'rule area'} {'/'.join(zb[u][1])} '{zb[u].name}'")
    else:
        same.append(f"zones ({len(zb)})")
    if b.teardrops != o.teardrops:
        lines.append(f"teardrops: {o.teardrops} -> {b.teardrops}")
    else:
        same.append(f"teardrops ({b.teardrops})")
    # tracks
    tb, to = {t['uuid']: t for t in b.tracks}, {t['uuid']: t for t in o.tracks}
    geo = lambda t: (tuple(round(v, 4) for v in t['a']), tuple(round(v, 4) for v in t['b']))  # noqa: E731
    wonly, moved, renet, relay = [], [], [], []
    for u in set(tb) & set(to):
        t, w = tb[u], to[u]
        if t['net'] != w['net']:
            renet.append(u)
        if t['layer'] != w['layer']:
            relay.append(u)
        if geo(t) != geo(w):
            moved.append(u)
        elif abs(t['w'] - w['w']) > 1e-6:
            wonly.append(u)
    ta, tr = set(tb) - set(to), set(to) - set(tb)
    if wonly or moved or renet or relay or ta or tr:
        lines.append(f"tracks: {len(tr)} removed, {len(ta)} added, {len(moved)} re-laid (same uuid, new ends), "
                     f"{len(wonly)} width-only; {len(to)} -> {len(tb)}")
        if renet or relay:
            lines.append(f"  !! {len(renet)} kept track(s) changed net, {len(relay)} changed layer")
        L = defaultdict(float)
        for t in b.tracks:
            L[t['layer']] += t['len']
        for t in o.tracks:
            L[t['layer']] -= t['len']
        lines.append("  length: " + ', '.join(f"{ly} {L[ly]:+.2f} mm" for ly in b.copper if abs(L.get(ly, 0)) > 5e-3)
                     + f"  (total {sum(L.values()):+.2f} mm)")
        nets = defaultdict(lambda: [0.0, 0.0, 0])                    # net -> [R old, R new, tracks]
        for u in tr | set(moved) | set(wonly):
            nets[to[u]['net']][0] += _r(o, to[u])
            nets[to[u]['net']][2] += 1
        for u in ta | set(moved) | set(wonly):
            nets[tb[u]['net']][1] += _r(b, tb[u])
        ws = Counter(round(tb[u]['w'] - to[u]['w'], 3) for u in wonly)
        if ws:
            lines.append("  widths: " + ', '.join(f"{d:+g} mm x{n}" for d, n in ws.most_common(6)))
        top = sorted(nets.items(), key=lambda kv: -abs(kv[1][1] - kv[1][0]))
        lines.append(f"  series R of the changed tracks, per net ({len(nets)} net(s), biggest change first):")
        for net, (r0, r1, n) in top[:a.max]:
            lines.append(f"    {unesc_disp(net) or '(no net)':<30} {r0 * 1e3:8.2f} -> {r1 * 1e3:8.2f} mohm ({r1 * 1e3 - r0 * 1e3:+.2f})")
        if len(top) > a.max:
            lines.append(f"    ... +{len(top) - a.max} more net(s)")
    else:
        same.append(f"tracks ({len(tb)})")
    print('\n'.join(lines) if lines else "no change")
    if same:
        print(f"identical: {', '.join(same)}")
    return 0
