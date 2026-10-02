"""kpcb.py `tidy`: near-miss alignment, each suggestion pre-checked with movecheck.
Read-only: it prints REF now -> new and never writes the board."""
from collections import Counter, defaultdict
from kcommon import natkey, prefix
from kpcb_move import World, placed_as

NEAR = (0.005, 0.15)        # a near miss: off by more than this lo, at most this hi (mm)
REACH = 6.0                 # same-package neighbours this close count as one row/column


def _target(vals, refs):
    """the value most of a group sits on; a tie goes to the natkey-first part's"""
    c = Counter(round(v, 3) for v in vals)
    top = max(c.values())
    if top > 1 and list(c.values()).count(top) == 1:
        return max(c, key=c.get)
    return round(vals[min(range(len(refs)), key=lambda i: natkey(refs[i]))], 3)


def suggestions(b):
    """[(kind, fp, new x, new y, new rot, why)]"""
    P = [f for f in b.placed() if f.pads and not b.is_hole(f)]
    out, seen = [], set()
    def add(kind, f, x, y, rot, why):
        k = (f.ref, kind, round(x, 3), round(y, 3), rot)
        if k not in seen:
            seen.add(k)
            out.append((kind, f, x, y, rot, why))
    # rows and columns with one outlier: same package nearby, or siblings anywhere when the
    # family is small (SW1/SW2, J4/J8, BT1/BT2 - not every 0402 cap on the board)
    fam = Counter((prefix(f.ref), f.fp, f.back) for f in P)
    for ax, name in ((1, 'row'), (0, 'column')):
        for f in P:
            fv = (f.x, f.y)[ax]
            grp = [g for g in P if g.back == f.back and g.fp == f.fp and abs((g.x, g.y)[ax] - fv) <= NEAR[1]
                   and (abs(g.x - f.x) + abs(g.y - f.y) <= REACH
                        or prefix(g.ref) == prefix(f.ref) and fam[(prefix(f.ref), f.fp, f.back)] <= 4)]
            if len(grp) < 2:
                continue
            t = _target([(g.x, g.y)[ax] for g in grp], [g.ref for g in grp])
            if NEAR[0] < abs(fv - t) <= NEAR[1]:
                others = ' '.join(sorted((g.ref for g in grp if g is not f), key=natkey)[:4])
                add('near-miss', f, t if ax == 0 else f.x, t if ax == 1 else f.y, None,
                    f"{'xy'[ax]} to the {name} of {others} ({abs(fv - t):.3f} off)")
    # rows/columns of >= 3 with a slightly uneven pitch: even it out between the ends
    for ax in (0, 1):
        lines = defaultdict(list)
        for f in P:
            lines[(f.fp, f.back, round((f.x, f.y)[1 - ax], 2))].append(f)
        for fs in lines.values():
            fs.sort(key=lambda f: (f.x, f.y)[ax])
            chain = [fs[:1]]
            for f in fs[1:]:
                if (f.x, f.y)[ax] - (chain[-1][-1].x, chain[-1][-1].y)[ax] <= REACH:
                    chain[-1].append(f)
                else:
                    chain.append([f])
            for c in chain:
                if len(c) < 3:
                    continue
                v = [(f.x, f.y)[ax] for f in c]
                p = [v[i + 1] - v[i] for i in range(len(v) - 1)]
                mean = (v[-1] - v[0]) / (len(v) - 1)
                if not (0.02 < max(p) - min(p) < 0.25 * mean):
                    continue
                for i, f in enumerate(c[1:-1], 1):
                    want = v[0] + i * mean
                    if abs(want - v[i]) > NEAR[0]:
                        add('pitch', f, want if ax == 0 else f.x, want if ax == 1 else f.y, None,
                            f"even {mean:.3f} pitch along {'xy'[ax]} between {c[0].ref} and {c[-1].ref} "
                            f"(pitches {', '.join(f'{q:.3f}' for q in p)})")
    # mounting-hole insets from the outline that nearly agree
    holes, o = [f for f in b.placed() if b.is_hole(f)], b.outline
    if len(holes) >= 2 and o:
        for ax in (0, 1):
            ins = [min((h.x, h.y)[ax] - o[ax], o[ax + 2] - (h.x, h.y)[ax]) for h in holes]
            t = _target(ins, [h.ref for h in holes])
            for h, i in zip(holes, ins):
                if NEAR[0] < abs(i - t) <= 0.5:
                    v = (h.x, h.y)[ax]
                    nv = o[ax] + t if v - o[ax] < o[ax + 2] - v else o[ax + 2] - t
                    add('hole inset', h, nv if ax == 0 else h.x, nv if ax == 1 else h.y, None,
                        f"{'xy'[ax]} inset {i:.3f} -> {t:.3f} mm like {' '.join(sorted((g.ref for g in holes if g is not h), key=natkey))}")
    # rotations that are not a multiple of 90
    for f in P:
        r = f.rot % 90
        if min(r, 90 - r) > 0.01:
            nr = round(f.rot / 90) * 90
            add('rotation', f, f.x, f.y, nr, f"rot {f.rot:g} is not a multiple of 90")
    # a small part inside a much bigger same-side part: off its long centre line
    for f in P:
        for g in P:
            if g is f or g.back != f.back or g.area < 20 * f.area:
                continue
            bx = g.fab or g.crtyd
            if not (bx[0] < f.x < bx[2] and bx[1] < f.y < bx[3]):
                continue
            cx, cy = (bx[0] + bx[2]) / 2, (bx[1] + bx[3]) / 2
            long_x = bx[2] - bx[0] >= bx[3] - bx[1]
            off = (f.y - cy) if long_x else (f.x - cx)
            if NEAR[0] < abs(off) <= 2.0:
                add('centring', f, f.x if long_x else cx, cy if long_x else f.y, None,
                    f"{abs(off):.3f} off {g.ref}'s long centre line {'y' if long_x else 'x'}="
                    f"{cy if long_x else cx:.3f} (along it: {(f.x - cx) if long_x else (f.y - cy):+.2f} from centre)")
    return out


def c_tidy(b, a):
    S = suggestions(b)
    if not S:
        print(f"{b.path}: nothing near-miss to tidy"); return 0
    print(f"{b.path}: {len(S)} tidy suggestion(s), each pre-checked with movecheck (nothing is written)")
    worlds, clear = {}, 0
    for kind in ('near-miss', 'pitch', 'hole inset', 'rotation', 'centring'):
        rows = [s for s in S if s[0] == kind]
        if not rows:
            continue
        print(f"\n{kind}:")
        for _k, f, x, y, rot, why in sorted(rows, key=lambda s: natkey(s[1].ref))[:a.max]:
            w = worlds.get(f.ref) or worlds.setdefault(f.ref, World(b, ref=f.ref))
            cur = w.hits(*placed_as(f, f.x, f.y, f.rot, b.copper), f.back)
            items, rings = placed_as(f, x, y, rot, b.copper)
            new = [m for k, m in w.hits(items, rings, f.back).items() if k not in cur]
            clear += not new
            print(f"  {f.ref:<6} {f.x:.3f},{f.y:.3f}" + (f" rot {f.rot:g}" if rot is not None else '')
                  + f" -> {x:.3f},{y:.3f}" + (f" rot {rot:g}" if rot is not None else '') + f"   {why}")
            print("         movecheck: " + ('clear' if not new else f"{len(new)} new hit(s): {new[0]}"))
        if len(rows) > a.max:
            print(f"  ... +{len(rows) - a.max} more {kind} - raise --max")
    print(f"\n{clear} of the shown suggestions are clear. To apply one: select the part in KiCad, "
          f"Drag (D) or\nedit its position in Properties, then refill zones (B) and re-run `kpcb check`.")
    return 0
