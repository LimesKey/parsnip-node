"""kpcb.py `silk` and `refdes`: review and organize the silkscreen.

`silk REF... | --box X0,Y0,X1,Y1` lists the silk texts there instead (position, angle,
size, uuid, findings): what `edit ref/hide/show/textsize/text` takes.

`silk [--only RULE]` judges every visible silk text (designators, values, user and board
texts) and the padless graphic footprints (logos):
  SILK-PAD      over exposed pad copper (within the silk clearance): the fab clips it
  SILK-UNDER    under a placed part's body (Fab, else courtyard) on that side: hidden once built
  SILK-OVERLAP  over another silk text or a footprint's silk outline
  SILK-EDGE     off the board or inside the silk clearance of the edge
  SILK-SIZE     under the fab minimum height / thickness (.kicad_dru text rules, .kicad_pro);
                a TrueType text by its glyph stroke (TTF_STROKE x height)
  SILK-AMBIG    a designator nearer another part than its own, or > 2.5 mm from it
  SILK-MIRROR   back-side text not mirrored, or front-side text mirrored: unreadable
  SILK-ORIENT   a front designator upside down (180/270) or at an odd angle
  SILK-HIDDEN   a designator hidden or not on silk (info)
  SILK-ART      a logo under a part body or over pads
KiCad's own silk_over_copper / silk_overlap may be IGNORED in the .kicad_pro (parsnip's
are), so these are the checks that see them. Text boxes are estimated (0.9 x size per
character, 1.2 x size tall: within ~0.1 mm of KiCad's stroke font for designators).

`refdes [REF...] [--all] [--show] [--size H] [--thick T] -o OUT` re-places designators: by
default the visible ones with a SILK-PAD/UNDER/OVERLAP/EDGE/AMBIG/ORIENT finding, REF...
named ones, --all every visible one, --show the hidden ones too (made visible). Each goes
to the best clear spot beside what a reader sees of its part (pads + Fab body): 4 sides,
slid along each, three rings out, upright (0 or 90 deg), centred, clear of pads (+ silk
clearance), part bodies, other silk text, footprint silk outlines, logos and the edge,
and never ambiguous the way `silk` judges it; nearer its current spot wins ties. Parts of
the same footprint and rotation in a row take the same side where they can.
--size/--thick also normalise the text (default: kept, raised to the fab minimum)."""
import argparse, glob, json, math, os, re
from collections import Counter, defaultdict
from kcommon import natkey, trunc, print_findings, prefix
from kpcb_board import bbox, box_dist, grow, hit, inside, overlap_area, pad_box, seg_box_dist

RULES = {'SILK-PAD': 'silk over exposed pad copper: clipped at the mask opening',
         'SILK-UNDER': "silk under a placed part's body: hidden once assembled",
         'SILK-OVERLAP': 'silk text over another text or a silk outline',
         'SILK-EDGE': 'silk off the board or inside the edge clearance',
         'SILK-SIZE': 'text under the fab minimum height / thickness',
         'SILK-AMBIG': 'designator nearer another part than its own, or far from it',
         'SILK-MIRROR': 'back text not mirrored / front text mirrored',
         'SILK-ORIENT': 'front designator upside down or at an odd angle',
         'SILK-HIDDEN': 'designator hidden or off the silkscreen',
         'SILK-ART': 'logo under a part body or over pads'}
# KiCad judges a TrueType text's thickness by its glyph stroke, not (thickness): DIN Mittelschrift's
# stroke is ~0.116 x height, so the 0.15 mm rule fails it under 1.3 mm (1.2 fails, 1.3 passes on
# parsnip; bold does not help). ponytail: one ratio for every face; measure per face if another differs
TTF_STROKE = 0.116


def size_short(x, mh, mt):
    """'' or why a text is under the fab minimum: height/thickness, or a TTF's glyph stroke"""
    if x.get('ttf'):
        need = max(mh, mt / TTF_STROKE)
        return f"TrueType at {x['h']:g} mm: glyph stroke under the {mt:g} mm minimum (needs >= ~{need:.2g} mm)" \
            if x['h'] < need - 1e-6 else ''
    if x['h'] < mh - 1e-6 or x['thick'] < mt - 1e-6:
        return f"{x['h']:g}/{x['thick']:g} mm under the {mh:g}/{mt:g} minimum"
    return ''


MOVABLE = {'SILK-PAD', 'SILK-UNDER', 'SILK-OVERLAP', 'SILK-EDGE', 'SILK-AMBIG', 'SILK-ORIENT'}


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


def limits(b):
    """(min text height, min thickness, silk clearance) from the .kicad_dru and .kicad_pro"""
    d = os.path.dirname(os.path.abspath(b.path))
    h, t, c = [0.0], [0.0], [0.0]
    for dru in glob.glob(os.path.join(d, '*.kicad_dru')):
        txt = re.sub(r'(?m)^\s*#.*$', '', open(dru, encoding='utf-8', errors='replace').read())
        for lst, k in ((h, 'text_height'), (t, 'text_thickness'), (c, 'silk_clearance')):
            lst += [float(x) for x in re.findall(r'\(constraint\s+%s\s+\(min\s+([\d.]+)\s*mm\)' % k, txt)]
    for pro in glob.glob(os.path.join(d, '*.kicad_pro')):
        try:
            r = json.load(open(pro))['board']['design_settings']['rules']
            h.append(float(r.get('min_text_height', 0)))
            t.append(float(r.get('min_text_thickness', 0)))
            c.append(float(r.get('min_silk_clearance', 0)))
        except (OSError, ValueError, KeyError, TypeError):
            pass
    return max(h), max(t), max(c)


class Silk:
    """the silkscreen of one side as obstacles, bucketed in 2 mm cells: exposed pads,
    part bodies (Fab, else courtyard), silk texts, footprint silk outlines, logos, board art"""

    def __init__(self, b):
        self.b, self.clr = b, limits(b)[2]
        self.grid = defaultdict(list)
        P = [g for g in b.placed() if g.pads]
        for g in P:
            for p in g.pads:
                for side in (False, True):
                    if on_side(p, side) or p['drill']:
                        self.put(('pad', side, g, p), pad_box(p))
            if body(g) and not b.is_hole(g):      # no Fab body (E22P, cell holders, easyeda headers): the courtyard
                self.put(('body', g.back, g, None), body(g))
            for ly, a, c, w in g.silklines:
                self.put(('line', ly[0] == 'B', g, (a, c, w)), grow(bbox([a, c]), w / 2))
        for f, ext, _cov, _pads in art_rows(b):
            self.put(('art', f.back, f, None), ext)
        for ly, bb, segs in b.silkart:              # board-level art (gr_poly, gr_circle...)
            side = ly[0] == 'B'
            if segs is None:
                self.put(('art', side, None, None), bb)
            for a, c, w in segs or ():
                self.put(('line', side, None, (a, c, w)), grow(bbox([a, c]), w / 2))
        self.texts = {}                           # id(field) -> (side, box, owner, field)
        for g in b.fps.values():
            for x in g.fields if g.placed else ():
                if not x['hidden'] and x['layer'].endswith('.SilkS'):
                    self.add_text(x, g)
        for x in b.silktexts:
            self.add_text(x, None)

    def put(self, item, bb):
        for cx in range(math.floor(bb[0] / 2), math.floor(bb[2] / 2) + 1):
            for cy in range(math.floor(bb[1] / 2), math.floor(bb[3] / 2) + 1):
                self.grid[(cx, cy)].append((item, bb))

    def add_text(self, x, owner):
        side = x['layer'][0] == 'B'
        self.texts[id(x)] = (side, x['box'], owner, x)
        self.put(('text', side, owner, x), x['box'])

    def drop_text(self, x):
        self.texts.pop(id(x), None)
        for cell in self.grid.values():
            cell[:] = [it for it in cell if not (it[0][0] == 'text' and it[0][3] is x)]

    def near(self, bb):
        seen = set()
        for cx in range(math.floor(bb[0] / 2), math.floor(bb[2] / 2) + 1):
            for cy in range(math.floor(bb[1] / 2), math.floor(bb[3] / 2) + 1):
                for item, ib in self.grid.get((cx, cy), ()):
                    if id(item) not in seen:
                        seen.add(id(item))
                        yield item, ib

    def hits(self, box, side, me=None, owner=None):
        """[(rule, message, other ref)] for a text box on a side: what it runs over"""
        out = []
        for (kind, s, g, extra), ib in self.near(grow(box, self.clr + 0.5)):
            if s != side:
                continue
            if kind == 'pad' and hit(grow(box, self.clr), ib):
                out.append(('SILK-PAD', f"over {g.ref}.{extra['num']}", g.ref))
            elif kind == 'body' and hit(box, ib) and \
                    not (g is owner and not g.fab and me is not None and me['kind'] == 'user'):  # its own marks, courtyard body
                out.append(('SILK-UNDER', 'under its own body' if g is owner else f"under {g.ref}'s body", g.ref))
            elif kind == 'line' and seg_box_dist(extra[0], extra[1], box) < extra[2] / 2 and \
                    not (g is owner and me is not None and me['kind'] == 'user'):     # its own +/- marks: by design
                out.append(('SILK-OVERLAP', 'over board art' if g is None else 'over its own silk outline' if g is owner
                            else f"over {g.ref}'s silk outline", g.ref if g else ''))
            elif kind == 'art' and hit(box, ib):
                out.append(('SILK-OVERLAP', f"over logo {g.ref or '(blank)'}" if g else 'over board art', g.ref if g else ''))
            elif kind == 'text' and extra is not me and hit(box, ib):
                who = (f"{g.ref}'s {extra['kind'].lower()}" if extra['kind'] != 'Reference' else f"{g.ref}'s designator") \
                    if g else f"board text '{trunc(extra['text'], 16)}'"
                out.append(('SILK-OVERLAP', f"over {who}", g.ref if g else ''))
        dedup, seen = [], set()
        for r in out:
            if (r[0], r[1]) not in seen:
                seen.add((r[0], r[1]))
                dedup.append(r)
        return dedup

    def edge_gap(self, box):
        """how far the box sits inside the outline (< 0: off it)"""
        b = self.b
        if not b.edge_segs:
            return 9.0
        c = ((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
        if b.rings and not inside(c, b.rings):
            return -1.0
        return min(seg_box_dist(u, v, box) for u, v in b.edge_segs)


_VIS = {}


PADONLY = {'TP', 'JP', 'NT', 'H', 'MH', 'FID'}       # nothing stands on these but their pads


def body(f):
    """a part's body: its Fab outline, else (no Fab layer: E22P, cell holders, easyeda parts) its
    courtyard when something physical stands there (a 3D model, or not a test point, jumper,
    net tie, hole or fiducial), else None"""
    if f.fab:
        return f.fab
    return f.crtyd if f.crtyd_real and (f.models or prefix(f.ref) not in PADONLY) else None


def visual(f):
    """what a reader sees of a part: its pads and its body (Fab, else courtyard)"""
    if id(f) not in _VIS:
        bd = body(f)
        pts = [q for p in f.pads for bb in (pad_box(p),) for q in (bb[:2], bb[2:])] + ([bd[:2], bd[2:]] if bd else [])
        _VIS[id(f)] = bbox(pts) if pts else f.crtyd
    return _VIS[id(f)]


def aligned(box, v):
    """does the box sit beside v, overlapping it along one axis (a label in line with its part)"""
    return min(box[2], v[2]) - max(box[0], v[0]) > 0 or min(box[3], v[3]) - max(box[1], v[1]) > 0


def ambiguity(b, box, f):
    """(gap to its own part, nearest other same-side part, its gap), on what a reader sees"""
    own = box_dist(box, visual(f))
    best = (9e9, None)
    for g in b.placed():
        if g is not f and g.pads and g.back == f.back and not b.is_hole(g) and hit(grow(box, own + 0.5), g.crtyd):
            d = box_dist(box, visual(g))
            if d < best[0]:
                best = (d, g)
    return own, best[1], best[0]


def findings(b, a, S=None):
    """(findings, {id(field): set(rules)}) over every visible silk text and logo"""
    mh, mt, clr = limits(b)
    S = S or Silk(b)
    only = {x.strip().upper() for x in a.only.split(',')} if a.only else None
    F, per, hidden = [], defaultdict(set), []
    def add(sev, rule, msg, refs, x=None):
        if not only or rule in only:
            F.append({'severity': sev, 'rule': rule, 'msg': msg, 'refs': [r for r in refs if r]})
        if x is not None:
            per[id(x)].add(rule)
    for f in sorted(b.placed(), key=lambda f: natkey(f.ref)):
        for x in f.fields:
            name = f"{f.ref}'s designator" if x['kind'] == 'Reference' else f"{f.ref}'s {x['kind'].lower()} '{trunc(x['text'], 12)}'"
            if x['kind'] == 'Reference' and (x['hidden'] or not x['layer'].endswith('.SilkS')):
                if f.pads:
                    hidden.append(f.ref)
                continue
            if x['hidden'] or not x['layer'].endswith('.SilkS'):
                continue
            side = x['layer'][0] == 'B'
            for rule, msg, other in S.hits(x['box'], side, x, f):
                add('WARN', rule, f"{name} {msg}", [f.ref] if rule != 'SILK-OVERLAP' or not other else [f.ref, other], x)
            if S.edge_gap(x['box']) < clr:
                add('WARN', 'SILK-EDGE', f"{name} {'off the board' if S.edge_gap(x['box']) < 0 else 'inside the edge clearance'}",
                    [f.ref], x)
            if size_short(x, mh, mt):
                add('WARN', 'SILK-SIZE', f"{name} {size_short(x, mh, mt)}", [f.ref], x)
            if x['mirror'] != side:
                add('WARN', 'SILK-MIRROR', f"{name} on {x['layer']} is {'not ' if side else ''}mirrored", [f.ref], x)
            if x['kind'] == 'Reference' and f.pads:
                own, g, d = ambiguity(b, x['box'], f)
                if g is not None and d + 0.15 < own and not (own <= 0.6 and aligned(x['box'], visual(f))):
                    add('WARN', 'SILK-AMBIG', f"{f.ref}'s designator sits nearer {g.ref} ({d:.2f} mm) than {f.ref} ({own:.2f} mm)",
                        [f.ref, g.ref], x)
                elif own > 2.5:
                    add('WARN', 'SILK-AMBIG', f"{f.ref}'s designator is {own:.1f} mm from its part", [f.ref], x)
                r = round(x['ang']) % 360
                if not side and r not in (0, 90):
                    add('INFO', 'SILK-ORIENT', f"{f.ref}'s designator at {x['ang']:g} deg " +
                        ('reads upside down (0/90 read from the bottom/right edge)' if r in (180, 270) else 'is not a multiple of 90'),
                        [f.ref], x)
    if hidden:
        add('INFO', 'SILK-HIDDEN', f"{len(hidden)} designator(s) hidden or off the silk: {' '.join(hidden[:24])}"
            + (f" +{len(hidden) - 24} more" if len(hidden) > 24 else '') + " (`refdes --show` places them)", [])
    for x in b.silktexts:
        name = f"board text '{trunc(x['text'], 20)}'"
        side = x['layer'][0] == 'B'
        for rule, msg, other in S.hits(x['box'], side, x, None):
            add('WARN', rule, f"{name} {msg}", [other])
        if S.edge_gap(x['box']) < clr:
            add('WARN', 'SILK-EDGE', f"{name} {'off the board' if S.edge_gap(x['box']) < 0 else 'inside the edge clearance'}", [])
        if size_short(x, mh, mt):
            add('WARN', 'SILK-SIZE', f"{name} {size_short(x, mh, mt)}", [])
        if x['mirror'] != side:
            add('WARN', 'SILK-MIRROR', f"{name} on {x['layer']} is {'not ' if side else ''}mirrored", [])
    for f, ext, cov, pads in art_rows(b):
        if cov and cov[0][0] >= .5:
            add('WARN', 'SILK-ART', f"logo {f.ref or '(blank)'} {100 * min(1, cov[0][0]):.0f}% under {cov[0][1].ref}'s body",
                [f.ref, cov[0][1].ref])
        if pads:
            add('WARN', 'SILK-ART', f"logo {f.ref or '(blank)'} crosses {len(pads)} pad(s): "
                + ' '.join(f'{r}.{n}' for r, n in pads[:6]), [f.ref])
    return F, per


def c_silk(b, a):
    if a.args or a.box:
        return silk_list(b, a)
    mh, mt, clr = limits(b)
    F, _per = findings(b, a)
    R = [x for f in b.placed() if f.pads for x in f.fields if x['kind'] == 'Reference']
    vis = [x for x in R if not x['hidden'] and x['layer'].endswith('.SilkS')]
    n = Counter(x['severity'] for x in F)
    print_findings(F, f"{b.path}: silk {n['ERROR']} error, {n['WARN']} warn, {n['INFO']} info\n"
                      f"designators: {len(vis)} visible of {len(R)}; minimum text {mh:g}/{mt:g} mm, silk clearance "
                      f"{clr:g} mm; {len(b.silktexts)} board text(s), {len(art_rows(b))} logo(s)",
                   RULES, a.max)
    rows = art_rows(b)
    if rows:
        print(f"\nlogos ({len(rows)} padless graphic footprint(s); move one with `move REF X Y`, find room with `freebox`):")
    for f, ext, cov, pads in rows:
        lays = Counter(l for l, _ in f.arts)
        side = 'B' if f.back else 'F'
        print(f"  {f.ref or '(blank)':<10} {trunc(f.fp, 40):<40} {', '.join(f'{l} ({k})' for l, k in lays.most_common())}"
              + (f"  [footprint layer {f.layer}, art on {side}]" if f.layer[:1] != side else ''))
        print(f"  {'':<10} at {f.x:.2f},{f.y:.2f} rot {f.rot:g}" + (f" scale {f.scale[0]:g}" if f.scale != (1.0, 1.0) else '')
              + f"  extent {ext[2] - ext[0]:.2f} x {ext[3] - ext[1]:.2f} mm  "
              + (f"HIDDEN under {cov[0][1].ref} ({100 * min(1, cov[0][0]):.0f}%)" if cov and cov[0][0] >= .5 else
                 'visible' + (f" (partly under {', '.join(f'{g.ref} {100 * c:.0f}%' for c, g in cov[:3])})" if cov else ''))
              + (f"; crosses {len(pads)} {side}-side pad(s): {' '.join(f'{r}.{n}' for r, n in pads[:6])}" if pads else ''))
    sizes = Counter((x['h'], x['thick']) for x in vis)
    if sizes:
        print("\ndesignator sizes (height/thickness mm x count): "
              + ', '.join(f"{h:g}/{t:g} x{c}" for (h, t), c in sizes.most_common(8)))
    print("next: `refdes -o OUT` re-places the flagged designators (dry run without -o); `view REF --layers "
          "F.SilkS,F.Fab,F.Cu` to look")
    return 2 if n['ERROR'] else 0


def silk_list(b, a):
    """`silk REF... [-r R] | --box X0,Y0,X1,Y1`: every silk text in the region with what
    `edit` needs (position, angle, size, uuid) and its findings"""
    if a.box:
        box = tuple(float(v) for v in a.box.split(','))
    else:
        miss = [r for r in a.args if r not in b.fps]
        if miss:
            print(f"{' '.join(miss)}: NOT FOUND"); return 1
        r = a.radius if a.radius != 5.0 else 1.0
        box = grow(bbox([q for ref in a.args for q in (b.fps[ref].crtyd[:2], b.fps[ref].crtyd[2:])]), r)
    _F, per = findings(b, argparse.Namespace(only=''))
    rows = [(f, x) for f in b.placed() for x in f.fields] + [(None, x) for x in b.silktexts]
    rows = [(f, x) for f, x in rows if x['layer'].endswith('.SilkS') and hit(x['box'], box)]
    print(f"silk in {box[0]:.2f},{box[1]:.2f} .. {box[2]:.2f},{box[3]:.2f}: {len(rows)} text(s) "
          f"(move one: `edit ref REF X,Y ANG` / `edit text UUID X,Y ANG`)")
    for f, x in sorted(rows, key=lambda r: (r[1]['layer'], r[1]['y'], r[1]['x'])):
        who = f"{f.ref} {'designator' if x['kind'] == 'Reference' else x['kind'].lower()}" if f else 'board text'
        flags = ', '.join(sorted(r.replace('SILK-', '').lower() for r in per.get(id(x), ())))
        print(f"  {x['layer'][0]}  {who:<20} {trunc(x['text'], 18)!r:<20} {x['x']:8.3f},{x['y']:<8.3f} {x['ang']:>4g} deg  "
              f"{x['h']:g}/{x['thick']:g}" + (' TTF' if x.get('ttf') else '') + (' hidden' if x['hidden'] else '')
              + (f"  [{x['uuid'][:8]}]" if not f else '') + (f"  ({flags})" if flags else ''))
    return 0


# ---------------------------------------------------------------- refdes (writer)

def _box(cx, cy, ang, txt, h):
    """the text box text_box() would give a centred text of height h at cx,cy"""
    w, hh = 0.9 * h * len(txt), 1.2 * h
    return (cx - w / 2, cy - hh / 2, cx + w / 2, cy + hh / 2) if round(ang) % 180 == 0 else \
        (cx - hh / 2, cy - w / 2, cx + hh / 2, cy + w / 2)


def candidates(f, txt, h, clr):
    """[(cx, cy, ang, slot, preference)]: beside what a reader sees of f (pads + Fab body),
    just over the silk clearance and two rings further out, slid along each side (centre
    first), upright"""
    c, out = visual(f), []
    for ring, gap in enumerate((clr + 0.02, clr + 0.35, clr + 0.8)):
        for ang in (0, 90):
            bw, bh = (0.9 * h * len(txt), 1.2 * h) if ang == 0 else (1.2 * h, 0.9 * h * len(txt))
            top, bot = c[1] - gap - bh / 2, c[3] + gap + bh / 2
            lft, rgt = c[0] - gap - bw / 2, c[2] + gap + bw / 2
            for t in (0.5, 0.25, 0.75, 0.0, 1.0):
                x, y = c[0] + t * (c[2] - c[0]), c[1] + t * (c[3] - c[1])
                for cx, cy, slot, pref in ((x, top, 'top', 0.0), (rgt, y, 'right', 0.1),
                                           (x, bot, 'bottom', 0.2), (lft, y, 'left', 0.3)):
                    out.append((cx, cy, ang, slot if t == 0.5 else f"{slot}{'+' if t > .5 else '-'}",
                                pref + 0.4 * ring + 0.3 * abs(t - 0.5) + (0.25 if ang == 90 else 0)))
    return out


def plan_refdes(b, a):
    """[(f, field, (cx, cy, ang), h, t, slot, why) | (f, field, None, ..., reason)]"""
    mh, mt, clr = limits(b)
    S = Silk(b)
    _F, per = findings(b, argparse.Namespace(only=''), S)
    want = set(a.args)
    todo = []
    for f in b.placed():
        if not f.pads or b.is_hole(f):
            continue
        for x in f.fields:
            if x['kind'] != 'Reference' or not x['layer'].endswith('.SilkS'):
                continue
            if want and f.ref not in want:
                continue
            why = per.get(id(x), set()) & MOVABLE
            if x['hidden'] and not a.show and f.ref not in want:
                continue
            if not (want or a.all or why or (x['hidden'] and a.show)):
                continue
            todo.append((f, x, why or ({'SILK-HIDDEN'} if x['hidden'] else {'asked'})))
    todo.sort(key=lambda t: (round(t[0].y, 1), t[0].x))       # rows top to bottom, left to right
    for f, x, _w in todo:
        S.drop_text(x)                             # the ones being moved are not obstacles
    out, rowpick = [], {}
    for f, x, why in todo:
        h = max(a.size or x['h'], mh)
        t = max(a.thick or (x['thick'] if not a.size else round(0.15 * h, 3)), mt)
        side = x['layer'][0] == 'B'
        key = (f.fp, round(f.rot) % 360, f.back)
        prev = rowpick.get(key)
        best = None
        for cx, cy, ang, slot, pref in candidates(f, f.ref, h, clr):
            box = _box(cx, cy, ang, f.ref, h)
            if S.edge_gap(box) < clr or S.hits(box, side, None, f):
                continue
            own, g, d = ambiguity(b, box, f)
            if g is not None and d + 0.15 < own and not (own <= 0.6 and aligned(box, visual(f))):
                continue                           # it must read as f's, not a neighbour's (as `silk` judges)
            score = pref + own + 0.15 * math.dist((cx, cy), (x['x'], x['y'])) - \
                (0.4 if prev and prev[0] == slot and prev[1] == ang and math.dist(prev[2], (f.x, f.y)) <= 6 else 0)
            if best is None or score < best[0]:
                best = (score, (cx, cy, ang), slot)
        if best is None:
            out.append((f, x, None, h, t, '', why))
            S.add_text(x, f)                       # it stays where it is: still an obstacle
            continue
        rowpick[key] = (best[2], best[1][2], (f.x, f.y))
        moved = dict(x, x=best[1][0], y=best[1][1], ang=best[1][2], h=h, thick=t, hidden=False,
                     box=_box(*best[1], f.ref, h))
        S.add_text(moved, f)
        out.append((f, x, best[1], h, t, best[2], why))
    return out


def ref_block(s, field='Reference'):
    """a footprint field's span in its text (10.x property, or older fp_text): Reference or Value"""
    return re.search(r'\n\t\t\(property "%s" "(?:[^"\\]|\\.)*"\n.*?\n\t\t\)' % field, s, re.S) or \
        re.search(r'\n\t\t\(fp_text %s "(?:[^"\\]|\\.)*"\n.*?\n\t\t\)' % field.lower(), s, re.S)


def edit_text(blk, at=None, ang=None, h=None, t=None, hide=None, centre=False):
    """a text block (footprint field at 3 tabs, board gr_text at 2) with its (at), size,
    thickness, hide flag changed; centre drops left/right/top/bottom justification (mirror
    kept). Every regex count asserted."""
    from kpcb_edit import num
    ind = re.match(r'\n?(\t+)\(', blk)[1] + '\t'
    if at is not None or ang is not None:
        m = re.search(r'\(at (\S+) (\S+)(?: (\S+))?\)', blk)
        x, y = at if at is not None else (m[1], m[2])
        a = ang if ang is not None else (float(m[3]) if m[3] else 0.0)
        blk = blk[:m.start()] + f"(at {num(float(x))} {num(float(y))} {num(a)})" + blk[m.end():]
    if h is not None:
        blk, k = re.subn(r'\(size [^)]*\)', f"(size {num(h)} {num(h)})", blk, count=1)
        assert k == 1
    if t is not None:
        blk, k = re.subn(r'\(thickness [^)]*\)', f"(thickness {num(t)})", blk, count=1)
        if not k:
            blk = re.sub(r'(\(size [^)]*\))', lambda q: f"{q[1]}\n{ind}\t\t(thickness {num(t)})", blk, count=1)
    if hide is False:
        blk = re.sub(r'\n\t+\(hide yes\)', '', blk)
    elif hide is True and '(hide yes)' not in blk:
        blk, k = re.subn(r'(\n\t+\(layer [^\n]*\))', r'\1' + f"\n{ind}(hide yes)", blk, count=1)
        assert k == 1
    if centre:
        blk = re.sub(r'\n\t+\(justify([^)]*)\)', lambda q: f"\n{ind}\t(justify mirror)" if 'mirror' in q[1] else '', blk)
    return blk


def c_refdes(b, a):
    from kpcb_edit import PcbText, _need_out
    from kpcb_move import fp_local
    plan = plan_refdes(b, a)
    if not plan:
        print("refdes: no designator to re-place (every visible one is clear; name REFs, or --all / --show)")
        return 0
    ok = [p for p in plan if p[2]]
    print(f"refdes: {len(ok)} of {len(plan)} designator(s) get a clear spot beside their part")
    for f, x, pos, h, t, slot, why in plan[:a.max * 3]:
        if pos:
            print(f"  {f.ref:<6} {x['x']:8.2f},{x['y']:<8.2f} {x['ang']:>4g} -> {pos[0]:8.2f},{pos[1]:<8.2f} {pos[2]:>3g}  "
                  f"{slot:<12} {h:g}/{t:g} mm  ({', '.join(sorted(w.replace('SILK-', '').lower() for w in why))})")
        else:
            print(f"  {f.ref:<6} no clear spot: leave it, hide it, or shrink it (--size)  "
                  f"({', '.join(sorted(w.replace('SILK-', '').lower() for w in why))})")
    if len(plan) > a.max * 3:
        print(f"  ... +{len(plan) - a.max * 3} more")
    if not a.out:
        print("(dry run: -o OUT writes it; then `silk` on OUT, `view` to look)")
        return 0
    if _need_out(a, 'refdes'):
        return 1
    tx = PcbText(b.path)
    edits = defaultdict(list)
    for f, x, pos, h, t, *_r in ok:
        edits[f.uuid].append((x, pos, h, t, f))
    for fu, items in edits.items():
        s = tx.text(fu)
        for x, pos, h, t, f in items:
            m = ref_block(s)
            if not m:
                raise SystemExit(f"refdes: {f.ref} has no Reference field in the tab layout; nothing written")
            s = s[:m.start()] + edit_text(m.group(0), fp_local(f, pos[:2]), pos[2], h, t, hide=False, centre=True) + s[m.end():]
        tx.sub(fu, s)
    tx.save(a.out)
    print(f"wrote {a.out}: {len(ok)} designator(s) re-placed. Check: kpcb.py {a.out} silk, then kdrc.py {a.out} drc")
    return 0
