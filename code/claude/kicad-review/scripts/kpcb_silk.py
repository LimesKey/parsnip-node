"""kpcb.py `silk`: padless graphic footprints (logos, pasted art): where the art
really is, what part body hides it, and which pads it runs over."""
from collections import Counter
from kcommon import natkey, trunc
from kpcb_board import bbox, hit, overlap_area, pad_box


def on_side(p, back):
    """does this pad have copper on that side (an SMD pad there, or a through pad)"""
    return any(l in ('B.Cu' if back else 'F.Cu', '*.Cu') for l in p['layers'])


def art_rows(b):
    """[(fp, extent, [(cover fraction, part)], [(ref, pad)])] per padless footprint
    with art, covers biggest first: same-side placed parts whose body (Fab, else
    courtyard) overlaps the art's extent"""
    parts = [g for g in b.placed() if g.pads]
    out = []
    for f in sorted((f for f in b.fps.values() if not f.pads and f.arts), key=lambda f: natkey(f.ref)):
        ext = bbox([q for _l, bb in f.arts for q in (bb[:2], bb[2:])])
        area = max(1e-9, (ext[2] - ext[0]) * (ext[3] - ext[1]))
        cov = sorted(((overlap_area(ext, g.fab or g.crtyd) / area, g) for g in parts
                      if g.back == f.back and hit(ext, g.fab or g.crtyd)), key=lambda t: (-t[0], natkey(t[1].ref)))
        pads = [(g.ref, p['num']) for g in parts for p in g.pads if on_side(p, f.back)
                and hit(ext, pad_box(p)) and any(hit(bb, pad_box(p)) for _l, bb in f.arts)]
        out.append((f, ext, cov, pads))
    return out


def c_silk(b, a):
    rows = art_rows(b)
    if not rows:
        print(f"{b.path}: no padless graphic footprints"); return 0
    hidden = 0
    print(f"{b.path}: {len(rows)} padless graphic footprint(s) (logos, pasted art)\n")
    for f, ext, cov, pads in rows:
        lays = Counter(l for l, _ in f.arts)
        side = 'B' if f.back else 'F'
        print(f"{f.ref or '(blank)':<11} {trunc(f.fp, 46)}")
        print(f"    art     : {', '.join(f'{l} ({n})' for l, n in lays.most_common())}"
              + (f"   [footprint layer {f.layer}, art on {side}]" if f.layer[:1] != side else ''))
        print(f"    at      : {f.x:.2f},{f.y:.2f}  rot {f.rot:g}"
              + (f"  scale {f.scale[0]:g}" if f.scale != (1.0, 1.0) else '')
              + f"   extent {ext[2] - ext[0]:.2f} x {ext[3] - ext[1]:.2f} mm "
                f"({ext[0]:.2f},{ext[1]:.2f} .. {ext[2]:.2f},{ext[3]:.2f})")
        if cov and cov[0][0] >= .5:
            hidden += 1
            print(f"    covered : HIDDEN under {cov[0][1].ref} ({trunc(cov[0][1].value, 20)}), "
                  f"{100 * min(1, cov[0][0]):.0f}% of the art's box"
                  + (f"; also {', '.join(f'{g.ref} {100 * c:.0f}%' for c, g in cov[1:4])}" if cov[1:] else ''))
        else:
            print("    covered : visible" + (f" (partly under {', '.join(f'{g.ref} {100 * c:.0f}%' for c, g in cov[:4])})"
                                             if cov else ''))
        print(f"    pads    : art crosses {len(pads)} {side}-side pad(s)"
              + (f": {' '.join(f'{r}.{n}' for r, n in pads[:8])}" + (' ...' if len(pads) > 8 else '') if pads else '')
              + ("  (silk on a pad is clipped at its mask opening)" if pads else ''))
    print(f"\n{hidden} of {len(rows)} hidden under a same-side part body. Art extents are the "
          f"items' boxes (lines\nwithout their stroke), so a crossed pad means a box touches it; "
          f"`view REF` shows it.")
    return 0
