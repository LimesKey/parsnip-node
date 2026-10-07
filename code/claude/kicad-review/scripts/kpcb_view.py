"""kpcb.py `view`: a cropped PNG of chosen board layers, from kicad-cli's own plot."""
import os, re, hashlib, subprocess
from kcommon import kicad_cli, cache_prune, CACHE
from kpcb_board import bbox

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


def c_view(b, a):
    """`view REF... | X,Y | --box X0,Y0,X1,Y1`: PNG of the region, -r mm around the
    parts' courtyards, on their side's layers unless --layers says otherwise"""
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
    layers = a.layers or LAYERS[back]
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
    r = subprocess.run(['rsvg-convert', '-b', 'white', '-w', str(int((box[2] - box[0]) * a.px)), '-o', out, tmp],
                       capture_output=True, text=True)
    if r.returncode:
        print(f"rsvg-convert failed ({r.stderr.strip() or 'not installed?'}); the SVG is {tmp}"); return 3
    print(f"{out}  ({where})")
    return 0
