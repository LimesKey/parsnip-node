"""tview.py BOARD X0,Y0,X1,Y1 OUT.png [--layers F.Cu,..] [--px 40] [--mark x,y;x,y]: tracks/pads/vias only, no fills"""
import sys, math, argparse, subprocess
sys.path.insert(0, '/home/limeskey/Desktop/Projects/parsnip-node/code/claude/kicad-review/scripts')
from kpcb_board import Board, xf
ap = argparse.ArgumentParser(); ap.add_argument('board'); ap.add_argument('box'); ap.add_argument('out')
ap.add_argument('--layers', default='F.Cu'); ap.add_argument('--px', type=int, default=40); ap.add_argument('--mark', default='')
ap.add_argument('--net', default='')
ap.add_argument('--sites', default='', help='LAYER:x,y;... -> montage OUT, box = half-size w,h')
a = ap.parse_args()
b = Board(a.board)


def render(box, L, out, marks, px, label=''):
  x0, y0, x1, y1 = box
  col = {'F.Cu': '#c83434', 'In1.Cu': '#c8a020', 'In2.Cu': '#20a0c8', 'B.Cu': '#4040d0'}
  o = [f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="{x0} {y0} {x1-x0} {y1-y0}" width="{int((x1-x0)*px)}" height="{int((y1-y0)*px)}">',
       f'<rect x="{x0}" y="{y0}" width="{x1-x0}" height="{y1-y0}" fill="#111"/>']
  inb = lambda p, m=1: x0 - m <= p[0] <= x1 + m and y0 - m <= p[1] <= y1 + m
  for ly in reversed(L):
      for f in b.fps.values():
          if not f.placed: continue
          for p in f.pads:
              if not (p['drill'] or ly in p['layers'] or '*.Cu' in p['layers']): continue
              if not inb((p['x'], p['y']), 3): continue
              if p['shape'] == 'circle':
                  o.append(f'<circle cx="{p["x"]}" cy="{p["y"]}" r="{p["sx"]/2}" fill="{col[ly]}" fill-opacity="0.45"/>')
              else:
                  o.append(f'<rect x="{-p["sx"]/2}" y="{-p["sy"]/2}" width="{p["sx"]}" height="{p["sy"]}" fill="{col[ly]}" fill-opacity="0.45" '
                           f'transform="translate({p["x"]} {p["y"]}) rotate({-p["prot"]})"/>')
      for t in b.tracks:
          if t['layer'] != ly or not (inb(t['a']) or inb(t['b'])): continue
          if math.dist(t['mid'], ((t['a'][0]+t['b'][0])/2, (t['a'][1]+t['b'][1])/2)) > 1e-4:
              d = f'M{t["a"][0]} {t["a"][1]} Q{2*t["mid"][0]-(t["a"][0]+t["b"][0])/2} {2*t["mid"][1]-(t["a"][1]+t["b"][1])/2} {t["b"][0]} {t["b"][1]}'
              o.append(f'<path d="{d}" stroke="{col[ly]}" stroke-width="{t["w"]}" fill="none" stroke-linecap="round" opacity="0.9"/>')
          else:
              o.append(f'<line x1="{t["a"][0]}" y1="{t["a"][1]}" x2="{t["b"][0]}" y2="{t["b"][1]}" stroke="{"#7f7" if t["net"] == a.net else col[ly]}" stroke-width="{t["w"]}" stroke-linecap="round" opacity="0.9"/>')
  for v in b.vias:
      if inb((v['x'], v['y'])):
          o.append(f'<circle cx="{v["x"]}" cy="{v["y"]}" r="{v["size"]/2}" fill="#ddd"/><circle cx="{v["x"]}" cy="{v["y"]}" r="{v["drill"]/2}" fill="#111"/>')
  for m in marks:
      mx, my = map(float, m.split(','))
      o.append(f'<circle cx="{mx}" cy="{my}" r="0.25" fill="none" stroke="#0f0" stroke-width="0.04"/>')
  o.append(f'<text x="{x0+0.1}" y="{y0+0.4}" font-size="0.35" fill="#fff">{label}</text>')
  o.append('</svg>')
  svg = out.rsplit('.', 1)[0] + '.svg'
  open(svg, 'w').write('\n'.join(o))
  subprocess.run(['rsvg-convert', '-o', out, svg], check=True)

if a.sites:
    hw, hh = map(float, a.box.split(','))
    outs = []
    for k, site in enumerate(a.sites.split(';')):
        ly, xy = site.split(':')
        x, y = map(float, xy.split(','))
        o = f"{a.out.rsplit('.', 1)[0]}_{k}.png"
        render((x - hw, y - hh, x + hw, y + hh), ly.split('+'), o, [f'{x},{y}'], a.px, f'{k} {ly} {x},{y}')
        outs.append(o)
    subprocess.run(['montage', *outs, '-tile', '4x', '-geometry', '+4+4', '-background', '#444', a.out], check=True)
else:
    render(tuple(map(float, a.box.split(','))), a.layers.split(','), a.out, list(filter(None, a.mark.split(';'))), a.px)
print(a.out)
