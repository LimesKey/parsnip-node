"""kpcb.py `pinlabels` and `label`: new silk texts, each at a spot `silk` would pass (clear
of pads + silk clearance, part bodies, outlines, texts, logos, board art and the edge).

`pinlabels REF [NAME...] [--size H] [-r MAX] -o OUT`: one label per pin, all at one offset
from the pin row (an aligned column), at the first clear offset on either side, up to MAX mm
(default 3). NAMEs go to pins 1..N; without them every netted pad is labelled with its net,
followed through a fuse or 0R to the named net beyond. When no column fits it names what
blocks it.

`label TEXT X,Y | --near REF [--size H] [-r R] [--side b] -o OUT`: TEXT at the clear spot
nearest X,Y (or REF's designator, which is then hidden), within R mm (default 3)."""
import math
from kcommon import natkey, unesc_disp
from kpcb_board import pad_box, hit
from kpcb_edit import PcbText, num, qs
from kpcb_silk import Silk, _box, limits

H, T = 1.0, 0.15                                     # parsnip's fab minimum, stroke font


def gr_text(s, x, y, ang, back, h, t):
    import uuid
    return (f'\t(gr_text {qs(s)}\n\t\t(at {num(x)} {num(y)} {num(ang)})\n\t\t(layer "{"B" if back else "F"}.SilkS")\n'
            f'\t\t(uuid "{uuid.uuid4()}")\n\t\t(effects\n\t\t\t(font\n\t\t\t\t(size {num(h)} {num(h)})\n'
            f'\t\t\t\t(thickness {num(t)})\n\t\t\t)\n' + ('\t\t\t(justify mirror)\n' if back else '') + '\t\t)\n\t)\n')


def why(S, box, side):
    """what blocks a box: the refs it hits, EDGE"""
    return {r[2] or r[1] for r in S.hits(box, side)} | ({'EDGE'} if S.edge_gap(box) < S.clr + 0.1 else set())


def pin_names(b, f):
    """[(pad, name)]: every netted pad, its net followed through a fuse or 0R (2-pad F*, or R
    valued 0) while the net is unnamed"""
    by = {}
    for g in b.fps.values():
        for p in g.pads:
            if p['net']:
                by.setdefault(p['net'], []).append((g, p))
    def name(net, seen=()):
        if net.startswith('Net-(') and len(seen) < 3:
            for g, p in by.get(net, ()):
                if g is f or g.ref in seen or len(g.pads) != 2:
                    continue
                if g.ref.startswith('F') or g.ref.startswith('R') and g.value.upper().rstrip('RΩ') in ('0', '0.0'):
                    other = next(q['net'] for q in g.pads if q is not p)
                    if other:
                        return name(other, seen + (g.ref,))
        return unesc_disp(net).split('/')[-1]
    pads = sorted((p for p in f.pads if p['net'] and not p['net'].startswith('unconnected-')),
                  key=lambda p: natkey(str(p['num'])))
    return [(p, name(p['net'])) for p in pads]


def column(S, pp, names, side, h, maxoff):
    """(offset, dir, [(name, cx, cy, box)], ang) of the nearest clear aligned column, or the blockers"""
    bx = [pad_box(p) for p in pp]
    vert = (max(p['y'] for p in pp) - min(p['y'] for p in pp)) >= (max(p['x'] for p in pp) - min(p['x'] for p in pp))
    ang = 0 if vert else 90
    best, blk = None, set()
    for d in (-1, 1):
        edge = (min(q[0] for q in bx) if d < 0 else max(q[2] for q in bx)) if vert else \
            (min(q[1] for q in bx) if d < 0 else max(q[3] for q in bx))
        for k in range(int(maxoff / 0.05)):
            off, put = S.clr + 0.05 + 0.05 * k, []
            for p, n in zip(pp, names):
                L = 0.9 * h * len(n)
                cx, cy = (edge + d * (off + L / 2), p['y']) if vert else (p['x'], edge + d * (off + L / 2))
                box = _box(cx, cy, ang, n, h)
                if S.edge_gap(box) < S.clr + 0.1 or S.hits(box, side) or any(hit(box, o[3]) for o in put):
                    if k in (0, int(maxoff / 0.05) - 1):
                        blk |= why(S, box, side)
                    break
                put.append((n, cx, cy, box))
            else:
                if best is None or off < best[0]:
                    best = (off, d, put, ang)
                break
    return best, blk


def c_pinlabels(b, a):
    if not a.args or a.args[0] not in b.fps:
        print("pinlabels REF [NAME...] [--size H] [-r MAX] -o OUT" + (f"\n{a.args[0]}: NOT FOUND" if a.args else ''))
        return 1
    f = b.fps[a.args[0]]
    if len(a.args) > 1:
        pads = {str(p['num']): p for p in f.pads}
        miss = [str(i + 1) for i in range(len(a.args) - 1) if str(i + 1) not in pads]
        if miss:
            print(f"{f.ref} has no pad {' '.join(miss)}"); return 1
        rows = [(pads[str(i + 1)], n) for i, n in enumerate(a.args[1:])]
    else:
        rows = pin_names(b, f)
    if not rows:
        print(f"{f.ref}: no netted pad to label"); return 1
    h = max(a.size or H, limits(b)[0])
    S = Silk(b)
    best, blk = column(S, [p for p, _n in rows], [n for _p, n in rows], f.back, h,
                       a.radius if a.radius != 5.0 else 3.0)
    print(f"pinlabels {f.ref}: " + ' '.join(f"{p['num']}={n}" for p, n in rows))
    if not best:
        print(f"  no clear aligned column within {a.radius if a.radius != 5.0 else 3.0:g} mm on either side; "
              f"blocked by {' '.join(sorted(blk)) or '?'}")
        return 2
    off, d, put, ang = best
    print(f"  {'B' if f.back else 'F'} side, {'-' if d < 0 else '+'}{'x' if ang == 0 else 'y'} of the pads at "
          f"{off:.2f} mm, {h:g} mm text")
    return _write(b, a, [(n, cx, cy, ang) for n, cx, cy, _bx in put], f.back, h)


def c_label(b, a):
    if not a.args or not (a.near or ',' in a.args[-1]):
        print("label TEXT X,Y | label TEXT --near REF  [--size H] [-r R] [--side b] -o OUT"); return 1
    txt = a.args[0]
    hide = None
    if a.near:
        f = b.fps.get(a.near)
        if not f:
            print(f"{a.near}: NOT FOUND"); return 1
        x = next((x for x in f.fields if x['kind'] == 'Reference'), None)
        anchor, back = ((x['x'], x['y']) if x else (f.x, f.y)), f.back
        hide = (f, x) if x and not x['hidden'] else None
    else:
        anchor, back = tuple(float(v) for v in a.args[1].split(',')), (a.side or 'f')[:1].lower() == 'b'
    h = max(a.size or H, limits(b)[0])
    S = Silk(b)
    if hide:
        S.drop_text(hide[1])
    r, step, best = a.radius if a.radius != 5.0 else 3.0, 0.05, None
    for ang in (0, 90):
        n = int(r / step)
        for d, cx, cy in sorted((math.hypot(i * step, j * step), anchor[0] + i * step, anchor[1] + j * step)
                                for i in range(-n, n + 1) for j in range(-n, n + 1)):
            if d > r or best and d >= best[0] - 0.5:
                break
            box = _box(cx, cy, ang, txt, h)
            if S.edge_gap(box) >= S.clr + 0.1 and not S.hits(box, back):
                best = (d, cx, cy, ang)
                break
    if not best:
        print(f"label '{txt}': no clear spot within {r:g} mm of {anchor[0]:.2f},{anchor[1]:.2f}; blocked by "
              f"{' '.join(sorted(why(S, _box(*anchor, 0, txt, h), back)))}")
        return 2
    d, cx, cy, ang = best
    print(f"label '{txt}' {'B' if back else 'F'} at {cx:.2f},{cy:.2f} {ang} deg, {d:.2f} mm from the anchor"
          + (f"; {hide[0].ref}'s designator hidden" if hide else ''))
    return _write(b, a, [(txt, cx, cy, ang)], back, h, hide[0] if hide else None)


def _write(b, a, texts, back, h, hide=None):
    if not a.out:
        for n, cx, cy, ang in texts:
            print(f"    {n:<12} {cx:8.3f},{cy:<8.3f} {ang:>3}")
        print("(dry run: -o OUT writes it; then `silk` on OUT)")
        return 0
    t = PcbText(b.path)
    for n, cx, cy, ang in texts:
        t.add(gr_text(n, cx, cy, ang, back, h, max(T, round(0.15 * h, 3))))
    if hide:
        from kpcb_silk import ref_block, edit_text
        s = t.text(hide.uuid)
        m = ref_block(s)
        t.sub(hide.uuid, s[:m.start()] + edit_text(m.group(0), hide=True) + s[m.end():])
    t.save(a.out)
    print(f"wrote {len(texts)} text(s) -> {a.out}; check: kpcb.py {a.out} silk")
    return 0
