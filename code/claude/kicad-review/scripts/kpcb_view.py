"""kpcb.py `view`: a cropped PNG of chosen board layers, from kicad-cli's own plot; or,
with --tracks / --sites, drawn from the Board model: tracks, pads and vias only, one
colour per copper layer, no pour fills to hide the routing; or, with --by-net, the copper
on the chosen layers fills included, one hue per net (GND grey) and a legend."""
import os, re, math, hashlib, subprocess
from kcommon import kicad_cli, cache_prune, CACHE, unesc_disp
from kpcb_board import bbox, xf

LAYERS = {False: 'F.Cu,F.SilkS,F.Fab,F.CrtYd,Edge.Cuts', True: 'B.Cu,B.SilkS,B.Fab,B.CrtYd,Edge.Cuts'}


def plot(b, layers):
    """kicad-cli's single-file SVG of `layers`, cached per board save. Its viewBox is
    the page in mm with the board at its own coordinates, so a crop is a viewBox."""
    tag = hashlib.sha1(f"{os.path.abspath(b.path)}:{os.path.getmtime(b.path)}:{layers}".encode()).hexdigest()[:12]
    out = os.path.join(CACHE, 'pcbsvg', tag + '.svg')
    if not os.path.exists(out):
        os.makedirs(os.path.dirname(out), exist_ok=True)
        r = subprocess.run([kicad_cli(b.path), 'pcb', 'export', 'svg', '--layers', layers, '--mode-single',
                            '--exclude-drawing-sheet', '-o', out, b.path], capture_output=True, text=True)
        if not os.path.exists(out):
            raise RuntimeError((r.stderr or r.stdout).strip()[-400:])
        cache_prune()
    return out


def crop(txt, box):
    """the SVG text with its size and viewBox set to box (x0, y0, x1, y1) in mm"""
    w, h = box[2] - box[0], box[3] - box[1]
    return re.sub(r'width="[^"]*" height="[^"]*" viewBox="[^"]*"',
                  f'width="{w:g}mm" height="{h:g}mm" viewBox="{box[0]:g} {box[1]:g} {w:g} {h:g}"',
                  txt, count=1)


COL = ['#d23c3c', '#d2a01e', '#1ea0d2', '#5050e6', '#3cb43c', '#b43cb4']     # F, In1, In2, ... B


def _col(b, ly):
    i = b.copper.index(ly) if ly in b.copper else 0
    return COL[-3] if i == len(b.copper) - 1 and i > 0 else COL[min(i, len(COL) - 1)]


def model_svg(b, box, layers, nets=(), marks=(), label=''):
    """an SVG group of the box from the Board model (board mm): pads, tracks (arcs as a
    quadratic through their mid), vias, the edge; `nets` drawn bright green, marks ringed"""
    x0, y0, x1, y1 = box
    inb = lambda p, m=1.0: x0 - m <= p[0] <= x1 + m and y0 - m <= p[1] <= y1 + m      # noqa: E731
    o = [f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="#141414"/>']
    for ly in reversed(layers):
        c = _col(b, ly)
        for f in b.fps.values():
            for p in f.pads if f.placed else ():
                if not (p['drill'] or ly in p['layers'] or '*.Cu' in p['layers']) or not inb((p['x'], p['y']), 3):
                    continue
                fill = '#3c3' if p['net'] in nets else c
                if p['prims']:
                    for poly in p['prims']:
                        pts = ' '.join(f"{u:.4f},{v:.4f}" for u, v in (xf(x, y, p['x'], p['y'], p['prot']) for x, y in poly))
                        o.append(f'<polygon points="{pts}" fill="{fill}" fill-opacity="0.5"/>')
                elif p['shape'] == 'circle':
                    o.append(f'<circle cx="{p["x"]}" cy="{p["y"]}" r="{p["sx"] / 2}" fill="{fill}" fill-opacity="0.5"/>')
                else:
                    rx = min(p['sx'], p['sy']) / 2 if p['shape'] == 'oval' else min(p['sx'], p['sy']) * 0.25 if p['shape'] == 'roundrect' else 0
                    o.append(f'<rect x="{-p["sx"] / 2}" y="{-p["sy"] / 2}" width="{p["sx"]}" height="{p["sy"]}" rx="{rx}" '
                             f'fill="{fill}" fill-opacity="0.5" transform="translate({p["x"]} {p["y"]}) rotate({-p["prot"]})"/>')
        for t in b.tracks:
            if t['layer'] != ly or not (inb(t['a']) or inb(t['b']) or inb(t['mid'])):
                continue
            col = '#5f5' if t['net'] in nets else c
            mx, my = (t['a'][0] + t['b'][0]) / 2, (t['a'][1] + t['b'][1]) / 2
            if math.dist(t['mid'], (mx, my)) > 1e-4:
                d = f"M{t['a'][0]} {t['a'][1]} Q{2 * t['mid'][0] - mx} {2 * t['mid'][1] - my} {t['b'][0]} {t['b'][1]}"
            else:
                d = f"M{t['a'][0]} {t['a'][1]} L{t['b'][0]} {t['b'][1]}"
            o.append(f'<path d="{d}" stroke="{col}" stroke-width="{t["w"]}" fill="none" stroke-linecap="round" opacity="0.9"/>')
    for v in b.vias:
        if inb((v['x'], v['y'])):
            o.append(f'<circle cx="{v["x"]}" cy="{v["y"]}" r="{v["size"] / 2}" fill="{"#5f5" if v["net"] in nets else "#ddd"}"/>'
                     f'<circle cx="{v["x"]}" cy="{v["y"]}" r="{v["drill"] / 2}" fill="#141414"/>')
    for u, v in b.edge_segs:
        if inb(u, 5) or inb(v, 5):
            o.append(f'<line x1="{u[0]}" y1="{u[1]}" x2="{v[0]}" y2="{v[1]}" stroke="#e8e050" stroke-width="0.1"/>')
    r = max(0.15, (x1 - x0) / 25)
    for mx, my in marks:
        o.append(f'<circle cx="{mx}" cy="{my}" r="{r}" fill="none" stroke="#0f0" stroke-width="{r / 6}"/>')
    if label:
        o.append(f'<text x="{x0 + r / 2}" y="{y0 + 1.3 * r}" font-size="{0.8 * r}" font-family="sans-serif" fill="#fff">{label}</text>')
    return '\n'.join(o)


def bynet_svg(b, box, layers, top=12):
    """(SVG group, legend rows): the box's copper on `layers` from the model, fills included,
    one hue per net (by copper area in the box, largest first, golden-angle spaced so
    neighbours differ), GND dim grey: a foreign net's band in a plane, a dead-end pour or a
    fill island stands out where a layer colour hides it"""
    x0, y0, x1, y1 = box
    hit = lambda bb, m=0.0: bb[2] >= x0 - m and bb[0] <= x1 + m and bb[3] >= y0 - m and bb[1] <= y1 + m   # noqa: E731
    area = {}
    fls = [fl for fl in b.fills if fl['layer'] in layers and hit(fl['bbox'])]
    for fl in fls:
        bb = fl['bbox']
        ov = max(0, min(bb[2], x1) - max(bb[0], x0)) * max(0, min(bb[3], y1) - max(bb[1], y0))
        area[fl['net']] = area.get(fl['net'], 0) + fl['area'] * ov / max((bb[2] - bb[0]) * (bb[3] - bb[1]), 1e-9)
    trk = [t for t in b.tracks if t['layer'] in layers and t['net'] and hit(bbox([t['a'], t['b']]), t['w'])]
    for t in trk:
        area[t['net']] = area.get(t['net'], 0) + t['len'] * t['w']
    rank = sorted((n for n in area if n), key=lambda n: -area[n])
    hue = {n: (k * 137.508) % 360 for k, n in enumerate(n for n in rank if n != 'GND')}
    col = lambda n: '#666' if n == 'GND' else f"hsl({hue[n]:.0f},75%,55%)" if n in hue else '#888'   # noqa: E731
    o = [f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="#141414"/>']
    for ly in reversed(layers):
        for fl in fls:
            if fl['layer'] == ly:
                pts = ' '.join(f"{x:.3f},{y:.3f}" for x, y in fl['pts'])
                o.append(f'<polygon points="{pts}" fill="{col(fl["net"])}" fill-opacity="{0.25 if fl["net"] == "GND" else 0.55}"/>')
        for f in b.placed():
            for p in f.pads:
                if p['net'] and (p['drill'] or ly in p['layers'] or '*.Cu' in p['layers']) and hit((p['x'], p['y'], p['x'], p['y']), 3):
                    o.append(f'<rect x="{-p["sx"] / 2}" y="{-p["sy"] / 2}" width="{p["sx"]}" height="{p["sy"]}" fill="{col(p["net"])}" '
                             f'fill-opacity="0.9" stroke="#fff" stroke-width="0.03" transform="translate({p["x"]} {p["y"]}) rotate({-p["prot"]})"/>')
        for t in trk:
            if t['layer'] == ly:
                o.append(f'<line x1="{t["a"][0]}" y1="{t["a"][1]}" x2="{t["b"][0]}" y2="{t["b"][1]}" stroke="{col(t["net"])}" '
                         f'stroke-width="{t["w"]}" stroke-linecap="round" opacity="{0.5 if t["net"] == "GND" else 0.95}"/>')
    for v in b.vias:
        if hit((v['x'], v['y'], v['x'], v['y'])):
            o.append(f'<circle cx="{v["x"]}" cy="{v["y"]}" r="{v["size"] / 2}" fill="{col(v["net"])}" stroke="#fff" stroke-width="0.03"/>'
                     f'<circle cx="{v["x"]}" cy="{v["y"]}" r="{v["drill"] / 2}" fill="#141414"/>')
    for u, v in b.edge_segs:
        if hit((min(u[0], v[0]), min(u[1], v[1]), max(u[0], v[0]), max(u[1], v[1])), 5):
            o.append(f'<line x1="{u[0]}" y1="{u[1]}" x2="{v[0]}" y2="{v[1]}" stroke="#e8e050" stroke-width="0.1"/>')
    leg = rank[:top]
    fs = max(0.3, (x1 - x0) / 45)
    o.append(f'<rect x="{x0}" y="{y0}" width="{fs * 14}" height="{fs * (1.3 * len(leg) + 0.6)}" fill="#000" fill-opacity="0.7"/>')
    for k, n in enumerate(leg):
        yy = y0 + fs * (1.3 * k + 1.1)
        o.append(f'<rect x="{x0 + fs * 0.3}" y="{yy - fs * 0.8}" width="{fs}" height="{fs}" fill="{col(n)}"/>'
                 f'<text x="{x0 + fs * 1.6}" y="{yy}" font-size="{fs}" font-family="sans-serif" fill="#fff">'
                 f'{unesc_disp(n.split("/")[-1]).replace("&", "&amp;").replace("<", "&lt;")}</text>')
    return '\n'.join(o), [(n, area[n]) for n in leg]


def silk_svg(b, box, back, flagged=frozenset()):
    """one side's silkscreen from the model, seen from that side (the back is mirrored so it
    reads): exposed pads, Fab bodies, silk outlines, logos, every visible silk text drawn as
    text at its size and angle; flagged texts (ids) red with their estimated box"""
    from kpcb_silk import exposed, art_rows
    x0, y0, x1, y1 = box
    inb = lambda bb: bb[2] >= x0 - 1 and bb[0] <= x1 + 1 and bb[3] >= y0 - 1 and bb[1] <= y1 + 1     # noqa: E731
    from kpcb_board import pad_box
    o = [f'<rect x="{x0}" y="{y0}" width="{x1 - x0}" height="{y1 - y0}" fill="#10301c"/>',
         f'<g transform="matrix(-1 0 0 1 {x0 + x1} 0)">' if back else '<g>']
    side = 'B' if back else 'F'
    for f in b.placed():
        for p in f.pads:
            if exposed(p, back) and inb(pad_box(p)):
                if p['shape'] == 'circle' and not p['prims']:
                    o.append(f'<circle cx="{p["x"]}" cy="{p["y"]}" r="{p["sx"] / 2}" fill="#c8a050"/>')
                else:
                    bb = pad_box(p) if p['prims'] or p['prot'] % 90 else None
                    o.append(f'<rect x="{bb[0]}" y="{bb[1]}" width="{bb[2] - bb[0]}" height="{bb[3] - bb[1]}" fill="#c8a050"/>' if bb else
                             f'<rect x="{-p["sx"] / 2}" y="{-p["sy"] / 2}" width="{p["sx"]}" height="{p["sy"]}" fill="#c8a050" '
                             f'transform="translate({p["x"]} {p["y"]}) rotate({-p["prot"]})"/>')
        if f.back == back and f.fab and inb(f.fab):
            fb = f.fab
            o.append(f'<rect x="{fb[0]}" y="{fb[1]}" width="{fb[2] - fb[0]}" height="{fb[3] - fb[1]}" fill="none" '
                     f'stroke="#7a8a80" stroke-width="0.05"/>')
        for ly, a, c, w in f.silklines:
            if ly[0] == side and inb((min(a[0], c[0]), min(a[1], c[1]), max(a[0], c[0]), max(a[1], c[1]))):
                o.append(f'<line x1="{a[0]}" y1="{a[1]}" x2="{c[0]}" y2="{c[1]}" stroke="#f0f0f0" stroke-width="{w}" stroke-linecap="round"/>')
    for f, ext, _c, _p in art_rows(b):
        if f.back == back and inb(ext):
            o.append(f'<rect x="{ext[0]}" y="{ext[1]}" width="{ext[2] - ext[0]}" height="{ext[3] - ext[1]}" fill="#f0f0f0" '
                     f'fill-opacity="0.25" stroke="#f0f0f0" stroke-width="0.08" stroke-dasharray="0.3 0.2"/>')
    texts = [x for f in b.placed() for x in f.fields] + b.silktexts
    for x in texts:
        if x['hidden'] or not x['layer'].startswith(side + '.SilkS') or not inb(x['box']):
            continue
        bad = id(x) in flagged
        col = '#ff5050' if bad else '#f0f0f0'
        if bad:
            bb = x['box']
            o.append(f'<rect x="{bb[0]}" y="{bb[1]}" width="{bb[2] - bb[0]}" height="{bb[3] - bb[1]}" fill="none" '
                     f'stroke="#ff5050" stroke-width="0.06"/>')
        o += _text_lines(x, col)
    for u, v in b.edge_segs:
        if inb((min(u[0], v[0]), min(u[1], v[1]), max(u[0], v[0]), max(u[1], v[1]))):
            o.append(f'<line x1="{u[0]}" y1="{u[1]}" x2="{v[0]}" y2="{v[1]}" stroke="#e8e050" stroke-width="0.1"/>')
    return '\n'.join(o + ['</g>'])


def _text_lines(x, col):
    """<text> per line of a silk text, each laid on the ink its own line covers (x['parts']:
    glyph boxes from KiCad's render_cache, else word boxes placed by the justification), so a
    left-justified mirrored title block sits where KiCad plots it. The view's font is
    monospace (DejaVu Sans Mono advances 0.602 em) and scaled in x to that width: rsvg-convert
    ignores textLength. Falls back to the whole box when lines and parts disagree."""
    t = math.radians(x['ang'])
    c, s = math.cos(t), math.sin(t)
    loc = lambda px, py: ((px - x['x']) * c - (py - x['y']) * s, (px - x['x']) * s + (py - x['y']) * c)  # noqa: E731
    lines = x['text'].split('\\n')
    while len(lines) > 1 and not lines[-1].strip():
        lines.pop()
    spans = []                                  # per part: (u0, u1, v0, v1) in the text's own frame
    for pb in x.get('parts') or [x['box']]:
        q = [loc(px, py) for px in (pb[0], pb[2]) for py in (pb[1], pb[3])]
        spans.append((min(u for u, _ in q), max(u for u, _ in q), min(v for _, v in q), max(v for _, v in q)))
    rows = []
    for sp in sorted(spans, key=lambda r: (r[2] + r[3]) / 2):
        if rows and (sp[2] + sp[3]) / 2 - rows[-1][-1] < 0.8 * x['h']:
            rows[-1][0].append(sp)
        else:
            rows.append(([sp], (sp[2] + sp[3]) / 2))
        rows[-1] = (rows[-1][0], (sp[2] + sp[3]) / 2)
    out, esc = [], lambda ln: ln.replace('&', '&amp;').replace('<', '&lt;')    # noqa: E731
    em = x['h'] * 1.3
    fs = f'font-size="{em:.3f}" font-family="DejaVu Sans Mono, monospace" fill="{col}" dominant-baseline="central"'
    if len(rows) != len(lines):                # a knockout block, or an odd cache: centre the lines in the box
        cx, cy = (x['box'][0] + x['box'][2]) / 2, (x['box'][1] + x['box'][3]) / 2
        for k, ln in enumerate(lines):
            dy = (k - (len(lines) - 1) / 2) * 1.6 * x['h']
            out.append(f'<text x="0" y="{dy:.3f}" {fs} text-anchor="middle" transform="translate({cx} {cy}) '
                       f'rotate({-x["ang"]}){" scale(-1 1)" if x["mirror"] else ""}">{esc(ln)}</text>')
        return out
    for ln, (sp, _v) in zip(lines, rows):
        u0, u1 = min(r[0] for r in sp), max(r[1] for r in sp)
        vc = (min(r[2] for r in sp) + max(r[3] for r in sp)) / 2
        sx = (u1 - u0) / max(0.602 * em * len(ln.strip()), 1e-6)
        at = f"translate({u1:.3f} {vc:.3f}) scale({-sx:.4f} 1)" if x['mirror'] else f"translate({u0:.3f} {vc:.3f}) scale({sx:.4f} 1)"
        out.append(f'<text x="0" y="0" {fs} text-anchor="start" '
                   f'transform="translate({x["x"]} {x["y"]}) rotate({-x["ang"]}) {at}">{esc(ln.strip())}</text>')
    return out


def _png(svg_txt, out, wpx):
    tmp = out.rsplit('.', 1)[0] + '.svg'
    open(tmp, 'w', encoding='utf-8').write(svg_txt)
    if out.endswith('.svg'):
        return out, ''
    r = subprocess.run(['rsvg-convert', '-w', str(int(wpx)), '-o', out, tmp], capture_output=True, text=True)
    return (out, '') if not r.returncode else (tmp, r.stderr.strip() or 'rsvg-convert not installed?')


def sites_png(b, sites, out, half=0.8, px=120, cols=4, page=16, first=1):
    """one panel per site [(layer, (x, y), label)], each 2*half mm square on its layer,
    in a grid of <= `page` panels per PNG (one tall strip is unreadable once downscaled);
    over a page, OUT.png becomes OUT-1.png, OUT-2.png..., panels numbered on. Returns the
    paths, space-joined"""
    if len(sites) > page:
        stem, ext = os.path.splitext(out)
        return ' '.join(sites_png(b, sites[i:i + page], f"{stem}-{i // page + 1}{ext}", half, px, cols, page, first + i)
                        for i in range(0, len(sites), page))
    n, side = len(sites), 2 * half
    rows = math.ceil(n / cols)
    W, H = min(n, cols) * (side + 0.1), rows * (side + 0.1)
    o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {H}" width="{W * px:.0f}" height="{H * px:.0f}">',
         f'<rect width="{W}" height="{H}" fill="#444"/>']
    for k, (ly, (x, y), lab) in enumerate(sites):
        cx, cy = (k % cols) * (side + 0.1), (k // cols) * (side + 0.1)
        o.append(f'<svg x="{cx}" y="{cy}" width="{side}" height="{side}" viewBox="{x - half} {y - half} {side} {side}">')
        o.append(model_svg(b, (x - half, y - half, x + half, y + half), [ly], marks=[(x, y)], label=f"{first + k} {ly} {lab}"))
        o.append('</svg>')
    path, err = _png('\n'.join(o + ['</svg>']), out, W * px)
    if err:
        print(f"rsvg-convert failed ({err}); the SVG is {path}")
    return path


def c_view(b, a):
    """`view REF... | X,Y | --box X0,Y0,X1,Y1`: PNG of the region, -r mm around the
    parts' courtyards, on their side's layers unless --layers says otherwise. --tracks draws
    it from the model (no fills); --sites LAYER:X,Y;... one panel per site"""
    out_dir = CACHE
    if a.sites:
        sites = []
        for spec in a.sites.split(';'):
            ly, _, xy = spec.partition(':')
            try:
                x, y = (float(v) for v in xy.split(','))
            except ValueError:
                print(f"--sites wants LAYER:X,Y;LAYER:X,Y (got {spec!r})"); return 1
            sites.append((ly, (x, y), ''))
        out = a.out or os.path.join(out_dir, 'pcbview_sites.png')
        print(f"{sites_png(b, sites, out, a.radius if a.radius != 5.0 else 0.8, a.px if a.px != 20 else 120)}  "
              f"({len(sites)} panel(s), tracks/pads/vias only)")
        return 0
    back = False
    if a.box:
        box = tuple(float(v) for v in a.box.split(','))
    else:
        pts = []
        for spec in a.args:
            m = re.match(r'^(-?[\d.]+)\s*,\s*(-?[\d.]+)$', spec)
            if m:
                pts.append((float(m[1]), float(m[2])))
            elif spec in b.fps:
                f = b.fps[spec]
                pts += [f.crtyd[:2], f.crtyd[2:]]
                back |= f.back
            else:
                print(f"{spec}: NOT FOUND"); return 1
        if not pts:
            print("view needs REF..., X,Y or --box X0,Y0,X1,Y1"); return 1
        bb = bbox(pts)
        box = (bb[0] - a.radius, bb[1] - a.radius, bb[2] + a.radius, bb[3] + a.radius)
    if a.layers == 'silk':
        import argparse
        back = back or getattr(a, 'side', '') == 'b'      # a bare box or point: --side b for the back
        from kpcb_silk import findings
        _F, per = findings(b, argparse.Namespace(only=''))
        w, h = box[2] - box[0], box[3] - box[1]
        px = a.px if a.px != 20 else 60
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box[0]} {box[1]} {w} {h}" '
               f'width="{w * px:.0f}" height="{h * px:.0f}">\n' + silk_svg(b, box, back, set(per)) + '\n</svg>')
        name = re.sub(r'[^A-Za-z0-9._-]', '_', '_'.join(a.args) or 'box')
        path, err = _png(svg, a.out or os.path.join(CACHE, f"pcbsilk_{name}.png"), w * px)
        if err:
            print(f"rsvg-convert failed ({err}); the SVG is {path}"); return 3
        print(f"{path}  ({'B' if back else 'F'} silk from the model: pads gold, Fab bodies grey, flagged text red with its "
              f"box; {'seen from the bottom (mirrored)' if back else 'seen from the top'}; {box[0]:g},{box[1]:g} - "
              f"{box[2]:g},{box[3]:g} mm)")
        return 0
    layers = a.layers or LAYERS[back]
    if getattr(a, 'by_net', False):
        cu = [l for l in (a.layers.split(',') if a.layers else b.copper) if l in b.copper]
        w, h = box[2] - box[0], box[3] - box[1]
        px = a.px if a.px != 20 else 60
        g, leg = bynet_svg(b, box, cu)
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box[0]} {box[1]} {w} {h}" '
               f'width="{w * px:.0f}" height="{h * px:.0f}">\n' + g + '\n</svg>')
        name = re.sub(r'[^A-Za-z0-9._-]', '_', '_'.join(a.args) or 'box')
        path, err = _png(svg, a.out or os.path.join(out_dir, f"pcbnets_{name}.png"), w * px)
        if err:
            print(f"rsvg-convert failed ({err}); the SVG is {path}"); return 3
        print(f"{path}  ({'/'.join(cu)} by net, fills included, GND grey; {box[0]:g},{box[1]:g} - {box[2]:g},{box[3]:g} mm, "
              f"seen from the top)\n  largest: " + ', '.join(f"{unesc_disp(n.split('/')[-1])} {ar:.0f} mm2" for n, ar in leg[:8]))
        return 0
    if a.tracks:
        cu = [l for l in (a.layers.split(',') if a.layers else b.copper) if l in b.copper]
        from kpcb_board import _resolve_net
        nets = [n if n in b.nets or any(t['net'] == n for t in b.tracks) else _resolve_net(b, n)[0] or n
                for n in a.net]
        marks = [tuple(float(v) for v in m.split(',')) for m in a.mark.split(';') if m] if a.mark else []
        w, h = box[2] - box[0], box[3] - box[1]
        px = a.px if a.px != 20 else 60
        svg = (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{box[0]} {box[1]} {w} {h}" '
               f'width="{w * px:.0f}" height="{h * px:.0f}">\n' + model_svg(b, box, cu, set(nets), marks) + '\n</svg>')
        name = re.sub(r'[^A-Za-z0-9._-]', '_', '_'.join(a.args) or 'box')
        path, err = _png(svg, a.out or os.path.join(out_dir, f"pcbtracks_{name}.png"), w * px)
        if err:
            print(f"rsvg-convert failed ({err}); the SVG is {path}"); return 3
        print(f"{path}  ({'/'.join(cu)} tracks/pads/vias, no fills" + (f", {' '.join(map(unesc_disp, nets))} green" if nets else '')
              + f"; {box[0]:g},{box[1]:g} - {box[2]:g},{box[3]:g} mm, seen from the top)")
        return 0
    try:
        svg = plot(b, layers)
    except RuntimeError as e:
        print(f"kicad-cli pcb export svg failed: {e}"); return 3
    name = re.sub(r'[^A-Za-z0-9._-]', '_', '_'.join(a.args) or 'box')
    out = a.out or os.path.join(CACHE, f"pcbview_{name}.png")
    tmp = out.rsplit('.', 1)[0] + '.svg'
    open(tmp, 'w', encoding='utf-8').write(crop(open(svg, encoding='utf-8').read(), box))
    where = f"{layers}; {box[0]:g},{box[1]:g} - {box[2]:g},{box[3]:g} mm, seen from the top"
    if out.endswith('.svg'):
        print(f"{out}  ({where})"); return 0
    w = box[2] - box[0]                          # the default 20 px/mm left a 6 mm box 130 px wide
    wpx = w * a.px if a.px != 20 else min(max(1200, w * 20), 4000)
    r = subprocess.run(['rsvg-convert', '-b', 'white', '-w', str(int(wpx)), '-o', out, tmp],
                       capture_output=True, text=True)
    if r.returncode:
        print(f"rsvg-convert failed ({r.stderr.strip() or 'not installed?'}); the SVG is {tmp}"); return 3
    print(f"{out}  ({where})")
    return 0
