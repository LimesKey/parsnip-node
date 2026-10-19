"""PROTOTYPE (2026-10-18, needs shapely, see SKILL-BACKLOG "fatten"). Power-path pour bands and vias:
  uv run --no-project --with shapely python power_bands.py IN OUT SPEC.json
  jobs: fatten (zone band round a net's tracks), vias (parallel free vias), widthcheck (fill cross-section)
SPEC: list of jobs
  {"fatten": NET, "layer": L, "w": W, "box": [x0,y0,x1,y1]?, "prio": P?}
      wrap NET's tracks on L (inside box) in a same-net zone band W wide
  {"vias": NET, "box": [...]?, "n": N, "size": 0.6, "drill": 0.3, "ring": [0.8, 1.2]}
      next to each existing NET via in box, add up to N vias that clear every
      foreign pad/track/via/hole on all layers (0.25 mm) and sit on NET copper
      (track, pad, band or zone outline) on at least two layers
  {"widthcheck": NET, "layer": L, "box": [...]?}   (read-only, needs a refilled IN)
      narrowest cross-section of NET's fill along its tracks on L
Prints what it did; KiCad's DRC on OUT is the verdict."""
import sys, json, math
import os; sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'scripts'))
from kpcb_board import Board, pad_box
from kcommon import load_sexp, kids, kid, val
from kpcb_edit import PcbText
from shapely.geometry import LineString, Polygon, Point, box as sbox
from shapely.ops import unary_union
from shapely import affinity

CU = ['F.Cu', 'In1.Cu', 'In2.Cu', 'B.Cu']
CLR = 0.25
NEW = []
KEEP = []


def zones_of(path):
    """[(net, layers, prio, Polygon, keepout, fills{layer: [Polygon]})] board-level zones"""
    out = []
    for z in kids(load_sexp(path), 'zone'):
        if kid(kid(z, 'attr') or [], 'teardrop'):
            continue
        lays = kid(z, 'layers') or kid(z, 'layer') or []
        lays = [l for l in lays[1:] if isinstance(l, str)]
        if any(l == '*.Cu' for l in lays):
            lays = CU
        pts = [(float(p[1]), float(p[2])) for p in kids(kid(kid(z, 'polygon'), 'pts'), 'xy')]
        if len(pts) < 3:
            continue
        fills = {}
        for fp in kids(z, 'filled_polygon'):
            q = [(float(p[1]), float(p[2])) for p in kids(kid(fp, 'pts'), 'xy')]
            if len(q) >= 3:
                fills.setdefault(val(fp, 'layer'), []).append(Polygon(q).buffer(0))
        pr = val(z, 'priority')
        out.append((val(z, 'net') or '', lays, int(pr) if pr else 0, Polygon(pts).buffer(0),
                    kid(z, 'keepout') is not None, fills))
    return out


def inbox(p, bx):
    return not bx or (bx[0] <= p[0] <= bx[2] and bx[1] <= p[1] <= bx[3])


def pad_poly(p):
    """conservative: the pad's axis-aligned box incl. custom primitives"""
    return sbox(*pad_box(p))


def slit(poly):
    """KiCad zone outlines are simple rings: cut each hole open with a thin slit"""
    for h in list(poly.interiors):
        c = Polygon(h).representative_point()
        y0 = poly.bounds[1] - 1
        poly = poly.difference(sbox(c.x - 0.005, y0, c.x + 0.005, c.y))
        if poly.geom_type == 'MultiPolygon':
            return [q for g in poly.geoms for q in slit(g)]
    return [poly]


def fatten(b, t, zs, job, prio):
    net, ly, w = job['fatten'], job['layer'], job['w']
    bx = job.get('box')
    minw = job.get('minw', 0.5)
    segs = [s for s in b.tracks if s['net'] == net and s['layer'] == ly and s['w'] >= minw - 1e-6
            and (inbox(s['a'], bx) or inbox(s['b'], bx))]
    if not segs:
        print(f'fatten {net} {ly}: no tracks'); return []
    band = unary_union([LineString([s['a'], s['b']]).buffer(w / 2, quad_segs=6) for s in segs])
    for zn, lays, pr, poly, ko, _ in zs:
        if ly in lays and (ko or zn not in (net, 'GND')):
            band = band.difference(poly.buffer(0.3))
    # a band never takes the ground access of a part on its own net (shunts, decoupling caps, TVS)
    for f in b.fps.values():
        if any(q.get('net') == net for q in f.pads):
            for q in f.pads:
                lays = q.get('layers') or []
                if q.get('net') == 'GND' and (ly in lays or '*.Cu' in lays or ly in ('In1.Cu', 'In2.Cu')):
                    band = band.difference(pad_poly(q).buffer(0.6))
    for kb in KEEP + job.get('keep', []):
        band = band.difference(sbox(*kb))
    for zn, zl, poly in NEW:     # bands of other nets made earlier in this run
        if zl == ly and zn != net:
            band = band.difference(poly.buffer(0.3))
    if ly == 'In1.Cu':          # keep the RF reference plane whole under F.Cu RF traces
        rf = [s for s in b.tracks if s['layer'] == 'F.Cu' and b.netclass(s['net']).startswith('RF')]
        if rf:
            band = band.difference(unary_union([LineString([s['a'], s['b']]).buffer(1.5) for s in rf]))
    geoms = band.geoms if band.geom_type == 'MultiPolygon' else [band]
    made = []
    for g in geoms:
        for q in slit(g.simplify(0.01)):
            if q.area < 0.3:
                continue
            pts = list(q.exterior.coords)[:-1]
            t.zone(net, ly, pts, prio)
            made.append(q); NEW.append((net, ly, q))
    print(f'fatten {net} {ly} w{w}: {len(segs)} track(s) -> {len(made)} zone(s), {sum(q.area for q in made):.1f} mm2 outline, prio {prio}')
    return made


def via_ok(b, x, y, size, drill, net, zs, bands):
    p = Point(x, y)
    o = b.outline
    if o and not (o[0] + size / 2 + 0.5 <= x <= o[2] - size / 2 - 0.5 and o[1] + size / 2 + 0.5 <= y <= o[3] - size / 2 - 0.5):
        return 'edge'
    for f in b.fps.values():
        for q in f.pads:
            d = pad_poly(q).distance(p) - size / 2
            if (q.get('drill') or 0) > 0 and math.dist((x, y), (q['x'], q['y'])) - drill / 2 - (q['drill']) / 2 < 0.3:
                return f'hole {f.ref}.{q["num"]}'
            if not any('Cu' in l for l in (q.get('layers') or [])):
                continue
            if q.get('net') == net:
                x0, y0, x1, y1 = pad_box(q)
                big = (x1 - x0) * (y1 - y0) >= 3.0 and f.ref[0] in 'QFLBUJ'   # FET/fuse/inductor/holder/IC power pads
                if d < 0.05 and not (q.get('drill') or 0) and not (big and pad_poly(q).buffer(-0.05).contains(p.buffer(size / 2))):
                    return f'in pad {f.ref}.{q["num"]}'       # no via-in-pad in small pads: paste wicks (TODO.md)
                continue
            if d < CLR:
                return f'pad {f.ref}.{q["num"]}'
    for s in b.tracks:
        if s['net'] == net:
            continue
        if LineString([s['a'], s['b']]).distance(p) - s['w'] / 2 - size / 2 < CLR:
            return f'track {s["net"]} {s["layer"]}'
    for v in b.vias + bands.get('_newvias', []):
        dd = math.dist((x, y), (v['x'], v['y']))
        if dd - drill / 2 - v['drill'] / 2 < 0.3:
            return 'hole-hole'
        if v['net'] != net and dd - size / 2 - v['size'] / 2 < CLR:
            return f'via {v["net"]}'
    for zn, lays, pr, poly, ko, _ in zs:
        if ko and poly.distance(p) < size / 2 + 0.2:
            return 'keepout'
        if zn not in (net, 'GND', '') and poly.distance(p) < size / 2 + 0.3:
            return f'zone {zn}'
    # must land on NET copper on >= 2 layers: a track it sits on, or the refilled pour of the net
    hit = 0
    disk = p.buffer(size / 2 + 0.05)
    for ly in CU:
        on = any(s['net'] == net and s['layer'] == ly and LineString([s['a'], s['b']]).distance(p) <= s['w'] / 2 - size / 4
                 for s in b.tracks)
        on = on or any(zn == net and g.contains(disk) for zn, lays, pr, poly, ko, f in zs for g in f.get(ly, []))
        hit += on
    return None if hit >= 2 else f'on {hit} layer(s)'


def vias(b, t, zs, job, bands):
    net, bx = job['vias'], job.get('box')
    size, drill, n = job.get('size', 0.6), job.get('drill', 0.3), job.get('n', 2)
    added = []
    for v in [v for v in b.vias if v['net'] == net and inbox((v['x'], v['y']), bx)]:
        k = 0
        for r in job.get('ring', [0.85, 1.25]):
            for a in range(0, 360, 30):
                if k >= n:
                    break
                x, y = v['x'] + r * math.cos(math.radians(a)), v['y'] + r * math.sin(math.radians(a))
                x, y = round(x, 3), round(y, 3)
                if via_ok(b, x, y, size, drill, net, zs, bands) is None:
                    e = t.via(net, x, y, size, drill, ['F.Cu', 'B.Cu'])
                    t.edits[t.edits.index(e)] = (e[0], e[1], e[2].replace('\t\t(net ', '\t\t(free yes)\n\t\t(net ', 1))
                    nv = {'net': net, 'x': x, 'y': y, 'size': size, 'drill': drill}
                    bands.setdefault('_newvias', []).append(nv)
                    b.vias.append(nv)
                    added.append((x, y)); k += 1
    print(f'vias {net}: +{len(added)} ({size}/{drill})', ' '.join(f'{x:.2f},{y:.2f}' for x, y in added))


def widthcheck(b, zs, job):
    net, ly, bx = job['widthcheck'], job['layer'], job.get('box')
    fill = unary_union([g for zn, lays, pr, poly, ko, f in zs if zn == net for g in f.get(ly, [])] or [Polygon()])
    segs = [s for s in b.tracks if s['net'] == net and s['layer'] == ly and (inbox(s['a'], bx) or inbox(s['b'], bx))]
    worst = []
    for s in segs:
        L = math.dist(s['a'], s['b'])
        if L < 1e-6:
            continue
        ux, uy = (s['b'][0] - s['a'][0]) / L, (s['b'][1] - s['a'][1]) / L
        for i in range(int(L / 0.25) + 1):
            x, y = s['a'][0] + ux * i * 0.25, s['a'][1] + uy * i * 0.25
            cut = LineString([(x - uy * 4, y + ux * 4), (x + uy * 4, y - ux * 4)])
            piece = cut.intersection(fill.union(LineString([s['a'], s['b']]).buffer(s['w'] / 2)))
            parts = piece.geoms if hasattr(piece, 'geoms') else [piece]
            wpt = max((g.length for g in parts if g.distance(Point(x, y)) < 1e-6), default=0)
            worst.append((wpt, x, y))
    worst.sort()
    import collections
    h = collections.Counter(min(int(w * 2) / 2, 3.0) for w, _, _ in worst)
    print('  width histogram (mm: samples of 0.25 mm):', dict(sorted(h.items())))
    # contiguous narrow stretches (< 1.5 mm), by location
    nar = sorted((round(x, 1), round(y, 1), round(w, 2)) for w, x, y in worst if w < 1.5)
    if nar:
        print('  < 1.5 mm at:', ' '.join(f'{x},{y}({w})' for x, y, w in nar[:40]))
    print(f'widthcheck {net} {ly}: {len(worst)} samples; narrowest', ', '.join(f'{w:.2f}mm@{x:.1f},{y:.1f}' for w, x, y in worst[:6]))


def main():
    src, out, spec = sys.argv[1], sys.argv[2], json.load(open(sys.argv[3]))
    b = Board(src)
    zs = zones_of(src)
    t = PcbText(src)
    prio = max(pr for _, _, pr, *_ in zs) + 1
    bands, edited = {}, False
    for job in spec:
        if 'keep' in job and len(job) == 1:
            KEEP.extend(job['keep']); continue
        if 'fatten' in job:
            made = fatten(b, t, zs, job, job.get('prio', prio))
            bands.setdefault((job['fatten'], job['layer']), []).extend(made)
            prio += 1; edited = edited or bool(made)
        elif 'vias' in job:
            vias(b, t, zs, job, bands); edited = True
        elif 'widthcheck' in job:
            widthcheck(b, zs, job)
    if edited and out != '-':
        t.save(out); print('->', out)


main()
