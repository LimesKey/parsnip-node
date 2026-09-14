#!/usr/bin/env python3
"""
ksch.py - render a KiCad-convention schematic to SVG from a short text spec.

Write ~15 lines of spec, get a schematic that looks like KiCad: IEC symbol
bodies, pin numbers outside / pin names inside, green orthogonal wires,
junction dots, GND triangles, power bars, hierarchical labels.
Never hand-write SVG for a circuit again.

  ksch.py render -o out.svg -            # spec on stdin (heredoc: cheapest)
  ksch.py render spec.ksch -o out.svg
  ksch.py syms                           # symbol table: types, pins, anchors
  ksch.py help                           # full spec language

Grid: 1 unit = 2.54 mm = 100 mil, +x right, +y DOWN (screen order, like KiCad).
Every coordinate in a spec is an integer grid unit. Two-pin parts are 2 units
pin to pin, so `r R1 4,2 v` puts pin 1 at (4,2) and pin 2 at (4,4).

SPEC LANGUAGE (one statement per line, '#' starts a comment)

  title  <text>                       diagram title, top left
  ic   REF x,y NAME [w=N] [h=N] L:1=EN,2=OVLO R:6=OUT T:5=IN B:8=GND
  conn REF x,y NAME R:1=VCC,2=GND     connector: box, pins one side
  r|c|cp|l|fb|d|ds|dz|led|tvs|sw|fuse|xtal|bat|jp|tp|ant  REF x,y [ORIENT] [value]
  nmos|pmos|npn|pnp  REF x,y [ORIENT] [value]
  wire  A.1 B.2 [8,4 ...]             route between pins/points, in order
  net   NAME [@x=N|@y=N] A.1 B.2 ...  named net, optional trunk column/row
  gnd   A.2 [gnd|gnda|earth]          ground symbol hung off a pin
  pwr   +3V3 A.1                      power rail symbol hung off a pin
  label NAME A.1                      local net label at a pin
  hlabel NAME A.1 [in|out|bidi]       hierarchical label (sheet pin)
  glabel NAME A.1                     global label
  nc    U1.7                          no-connect X
  note  x,y <text>                    free annotation text
  group x,y w,h <text>                dashed annotation box

ORIENT for 2-pin parts: v (pin1 top, default) h (pin1 left) vr (pin1 bottom)
hr (pin1 right).  For transistors: r (default, gate/base left) l (mirrored).
Pins are addressed by number (U8.6) or by name (U8.OUT, R1.2, Q1.g, D1.k).

FLAGS
  --net board.net   pull IC value + pin names from a netlist, so `L:1,2,5`
                    needs numbers only. Implies --verify.
  --verify          check every drawn connection against that netlist and
                    report any wire the real board does not have.
  --px N            pixels per grid unit (22). --theme kicad|mono|dark
  --us              zigzag resistors instead of the KiCad/IEC box
  --grid            draw the dot grid.  --frame  draw a border
  --quiet           suppress the check report
"""
import sys, os, re, math, argparse

VERSION = "1.0"

# ---------------- themes ----------------

THEMES = {
    'kicad': dict(dnp='#a05050', bg='#ffffff', body='#840000', fill='#ffffc2', pin='#840000',
                  pnum='#840000', pname='#840000', wire='#008484', junc='#008484',
                  label='#008484', glabel='#ce0000', hlabel='#840000',
                  ref='#840000', val='#840000', note='#4c4c4c', grid='#dcdcdc',
                  nc='#0000c8'),
    'mono':  dict(dnp='#777777', bg='#ffffff', body='#111111', fill='#ffffff', pin='#111111',
                  pnum='#555555', pname='#111111', wire='#111111', junc='#111111',
                  label='#111111', glabel='#111111', hlabel='#111111',
                  ref='#111111', val='#444444', note='#666666', grid='#e6e6e6',
                  nc='#111111'),
    'dark':  dict(dnp='#9a7f7f', bg='#1e2229', body='#f2b8b8', fill='#2b2f38', pin='#f2b8b8',
                  pnum='#c9a0a0', pname='#f2b8b8', wire='#4ec9b0', junc='#4ec9b0',
                  label='#4ec9b0', glabel='#ff8a80', hlabel='#f2b8b8',
                  ref='#f2b8b8', val='#b8c4d0', note='#8a94a0', grid='#2a2f38',
                  nc='#82aaff'),
}

# ---------------- KiCad name escaping ----------------

_UNESC = [('{slash}', '/'), ('{backslash}', '\\'), ('{dblquote}', '"'),
          ('{quote}', "'"), ('{lt}', '<'), ('{gt}', '>'), ('{colon}', ':'),
          ('{dot}', '.'), ('{tab}', ' '), ('{space}', ' ')]


def unesc(s):
    """KiCad escapes netlist/field text; turn it back into readable characters.
    ~{FLT} (overbar) is kept as a marker and handled by the text renderer."""
    s = str(s)
    for a, b in _UNESC:
        s = s.replace(a, b)
    return s


def split_overbar(s):
    """'PG/~{FLT}' -> [('PG/',0), ('FLT',1)] so SVG can overline the right run."""
    out, i = [], 0
    for m in re.finditer(r'~\{([^}]*)\}', s):
        if m.start() > i:
            out.append((s[i:m.start()], 0))
        out.append((m.group(1), 1))
        i = m.end()
    if i < len(s):
        out.append((s[i:], 0))
    return out or [(s, 0)]


def plain(s):
    return ''.join(t for t, _ in split_overbar(unesc(s)))


# ---------------- geometry ----------------

def rot_pt(x, y, deg):
    if deg == 90:
        return (-y, x)
    if deg == 180:
        return (-x, -y)
    if deg == 270:
        return (y, -x)
    return (x, y)


DIRV = {'U': (0, -1), 'D': (0, 1), 'L': (-1, 0), 'R': (1, 0)}


def rot_dir(d, deg, mir=False):
    dx, dy = DIRV[d]
    if mir:
        dx = -dx
    dx, dy = rot_pt(dx, dy, deg)
    for k, v in DIRV.items():
        if abs(v[0] - dx) < 1e-6 and abs(v[1] - dy) < 1e-6:
            return k
    return d


ORIENT = {'v': 0, 'h': 270, 'vr': 180, 'hr': 90,
          'r0': 0, 'r90': 90, 'r180': 180, 'r270': 270,
          'up': 180, 'down': 0, 'left': 90, 'right': 270}


# ---------------- primitive constructors (local grid coords) ----------------
# prim tuples: ('l',x1,y1,x2,y2,w) ('r',x,y,w,h,filled) ('c',x,y,rad,filled)
#              ('p',[(x,y)...],closed,filled) ('a',x1,y1,x2,y2,rad,sweep)
#              ('t',x,y,text,size,anchor,rot)

def L(x1, y1, x2, y2, w=1.0):
    return ('l', x1, y1, x2, y2, w)


def POLY(pts, closed=True, filled=False):
    return ('p', pts, closed, filled)


# Each builder returns (prims, pins, bbox) where
#   pins = {name: (x, y, dir)}   dir = direction a wire leaves the pin
#   bbox = (x0, y0, x1, y1) of the drawn body (for overlap checks / text)

def _leads(a, b, body0, body1):
    """vertical leads for a 2-pin part: pin1 (0,0) .. body .. pin2 (0,2)"""
    return [L(0, a, 0, body0), L(0, body1, 0, b)]


def s_r(us=False):
    if us:
        pts = [(0, .55)]
        y = .55
        for i in range(6):
            y += .15
            pts.append((.32 if i % 2 == 0 else -.32, y))
            y += .0
        pts.append((0, 1.45))
        pr = [L(0, 0, 0, .55), L(0, 1.45, 0, 2)]
        zz = [(0, .55), (.32, .70), (-.32, .95), (.32, 1.20), (-.32, 1.35), (0, 1.45)]
        pr.append(POLY(zz, closed=False))
        bb = (-.32, .55, .32, 1.45)
    else:
        pr = _leads(0, 2, .6, 1.4) + [('r', -.35, .6, .7, .8, 1)]
        bb = (-.35, .6, .35, 1.4)
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, bb


def s_c(pol=False):
    pr = [L(0, 0, 0, .85), L(0, 1.15, 0, 2), L(-.62, .85, .62, .85, 1.2)]
    if pol:
        pr.append(('a', -.62, 1.25, .62, 1.25, 1.6, 0))
        pr.append(('t', -.85, .55, '+', .45, 'middle', 0))
    else:
        pr.append(L(-.62, 1.15, .62, 1.15, 1.2))
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.62, .85, .62, 1.25)


def s_l(bead=False):
    pr = [L(0, 0, 0, .4), L(0, 1.6, 0, 2)]
    for i in range(4):
        y = .4 + i * .3
        pr.append(('a', 0, y, 0, y + .3, .17, 1))
    if bead:
        pr.append(('r', -.3, .45, .6, 1.1, 0))
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.3, .4, .3, 1.6)


def _diode_body(kind):
    pr = [L(0, 0, 0, .68), L(0, 1.32, 0, 2),
          POLY([(-.5, .68), (.5, .68), (0, 1.32)], True, 1)]
    bar = [L(-.5, 1.32, .5, 1.32, 1.2)]
    if kind == 'ds':
        bar += [L(-.5, 1.32, -.5, 1.12), L(-.5, 1.12, -.32, 1.12),
                L(.5, 1.32, .5, 1.52), L(.5, 1.52, .32, 1.52)]
    elif kind == 'dz':
        bar += [L(-.5, 1.32, -.32, 1.12), L(.5, 1.32, .32, 1.52)]
    pr += bar
    if kind == 'led':
        pr += [L(.55, .85, .95, .45), POLY([(.95, .45), (.78, .5), (.87, .62)], True, 2),
               L(.55, 1.1, .95, .7), POLY([(.95, .7), (.78, .75), (.87, .87)], True, 2)]
    return pr


def s_d(kind='d'):
    pr = _diode_body(kind)
    bb = (-.5, .68, 1.0 if kind == 'led' else .5, 1.32)
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D'), 'a': (0, 0, 'U'), 'k': (0, 2, 'D')}, bb


def s_tvs():
    pr = [L(0, 0, 0, .5), L(0, 1.5, 0, 2),
          POLY([(-.5, .5), (.5, .5), (0, 1.0)], True, 1),
          POLY([(-.5, 1.5), (.5, 1.5), (0, 1.0)], True, 1),
          L(-.5, 1.0, .5, 1.0, 1.2)]
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.5, .5, .5, 1.5)


def s_fuse():
    pr = _leads(0, 2, .6, 1.4) + [('r', -.32, .6, .64, .8, 1), L(0, .6, 0, 1.4)]
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.32, .6, .32, 1.4)


def s_sw():
    pr = [L(0, 0, 0, .5), L(0, 2, 0, 1.5),
          ('c', 0, .58, .09, 2), ('c', 0, 1.42, .09, 2),
          L(.06, 1.38, .62, .62), L(-.45, .8, .45, .8, 1.0), L(0, .8, 0, .55)]
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.45, .5, .62, 1.5)


def s_xtal():
    pr = [L(0, 0, 0, .6), L(0, 2, 0, 1.4), L(-.55, .6, .55, .6, 1.2),
          L(-.55, 1.4, .55, 1.4, 1.2), ('r', -.3, .75, .6, .5, 1)]
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.55, .6, .55, 1.4)


def s_bat():
    pr = [L(0, 0, 0, .7), L(0, 2, 0, 1.3),
          L(-.6, .7, .6, .7, 1.3), L(-.3, .95, .3, .95, 1.3),
          L(-.6, 1.05, .6, 1.05, 1.3), L(-.3, 1.3, .3, 1.3, 1.3),
          ('t', -.9, .62, '+', .45, 'middle', 0)]
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D'),
                '+': (0, 0, 'U'), '-': (0, 2, 'D')}, (-.6, .7, .6, 1.3)


def s_jp():
    pr = [L(0, 0, 0, .55), L(0, 2, 0, 1.45), ('c', 0, .6, .1, 2), ('c', 0, 1.4, .1, 2),
          ('a', 0, .6, 0, 1.4, .55, 0)]
    return pr, {'1': (0, 0, 'U'), '2': (0, 2, 'D')}, (-.55, .5, .1, 1.5)


def s_tp():
    return ([L(0, 0, 0, .5), ('c', 0, .68, .18, 0)],
            {'1': (0, 0, 'U')}, (-.18, .5, .18, .86))


def s_ant():
    pr = [L(0, 0, 0, -.9), L(-.7, -1.6, 0, -.9), L(.7, -1.6, 0, -.9)]
    return pr, {'1': (0, 0, 'D')}, (-.7, -1.6, .7, 0)


def s_fet(p=False):
    """gate is the anchor at (0,0), drain top right, source bottom right."""
    pr = [L(0, 0, .75, 0), L(.75, -.8, .75, .8, 1.1),
          L(1.15, -.8, 1.15, -.3, 1.1), L(1.15, -.28, 1.15, .28, 1.1),
          L(1.15, .3, 1.15, .8, 1.1),
          L(1.15, -.55, 2, -.55), L(2, -.55, 2, -1.6),
          L(1.15, .55, 2, .55), L(2, .55, 2, 1.6),
          L(1.15, 0, 2, 0), L(2, 0, 2, .55)]
    if p:  # bulk arrow points away from the channel
        pr.append(POLY([(1.5, 0), (1.22, -.16), (1.22, .16)], True, 2))
    else:
        pr.append(POLY([(1.22, 0), (1.5, -.16), (1.5, .16)], True, 2))
    up, dn = ('s', 'd') if p else ('d', 's')
    pins = {'1': (0, 0, 'L'), '2': (2, -1.6, 'U'), '3': (2, 1.6, 'D'),
            'g': (0, 0, 'L'), up: (2, -1.6, 'U'), dn: (2, 1.6, 'D')}
    if p:
        pins['2'], pins['3'] = (2, 1.6, 'D'), (2, -1.6, 'U')
    return pr, pins, (.75, -1.6, 2, 1.6)


def s_bjt(p=False):
    pr = [('c', 1.5, 0, 1.15, 0), L(0, 0, .9, 0), L(.9, -.7, .9, .7, 1.2),
          L(.9, -.35, 1.8, -.9), L(1.8, -.9, 1.8, -1.8),
          L(.9, .35, 1.8, .9), L(1.8, .9, 1.8, 1.8)]
    if p:
        pr.append(POLY([(.98, .4), (1.35, .45), (1.13, .78)], True, 2))
    else:
        pr.append(POLY([(1.72, .8), (1.32, .74), (1.5, .44)], True, 2))
    up, dn = ('e', 'c') if p else ('c', 'e')
    pins = {'1': (0, 0, 'L'), '2': (1.8, -1.8, 'U'), '3': (1.8, 1.8, 'D'),
            'b': (0, 0, 'L'), up: (1.8, -1.8, 'U'), dn: (1.8, 1.8, 'D')}
    if p:
        pins['2'], pins['3'] = (1.8, 1.8, 'D'), (1.8, -1.8, 'U')
    return pr, pins, (.35, -1.8, 2.65, 1.8)


TWO_PIN = {
    'r': lambda o: s_r(o.get('us')), 'c': lambda o: s_c(False),
    'cp': lambda o: s_c(True), 'l': lambda o: s_l(False),
    'fb': lambda o: s_l(True), 'd': lambda o: s_d('d'),
    'ds': lambda o: s_d('ds'), 'dz': lambda o: s_d('dz'),
    'led': lambda o: s_d('led'), 'tvs': lambda o: s_tvs(),
    'fuse': lambda o: s_fuse(), 'sw': lambda o: s_sw(),
    'xtal': lambda o: s_xtal(), 'bat': lambda o: s_bat(),
    'jp': lambda o: s_jp(), 'tp': lambda o: s_tp(), 'ant': lambda o: s_ant(),
    'nmos': lambda o: s_fet(False), 'pmos': lambda o: s_fet(True),
    'npn': lambda o: s_bjt(False), 'pnp': lambda o: s_bjt(True),
}

ANCHOR_NOTE = {
    'ic': 'top-left of body', 'conn': 'top-left of body',
    'nmos': 'gate pin', 'pmos': 'gate pin', 'npn': 'base pin', 'pnp': 'base pin',
    'ant': 'feed pin', 'tp': 'pin 1',
}


# ---------------- IC / connector body ----------------

TXT = 0.5          # KiCad default text height: 50 mil
LEAD = 2.0         # pin lead length in grid units
PITCH = 2.0        # pin pitch


def s_ic(sides, w=None, h=None, name='', pitch=PITCH):
    """sides: {'L':[(num,name),...], 'R':.., 'T':.., 'B':..}"""
    nL, nR = len(sides.get('L', [])), len(sides.get('R', []))
    nT, nB = len(sides.get('T', [])), len(sides.get('B', []))
    if h is None:
        h = max(2, int(pitch * max(nL, nR, 1)))
    if w is None:
        wl = max([len(plain(n)) for _, n in sides.get('L', [])] or [0])
        wr = max([len(plain(n)) for _, n in sides.get('R', [])] or [0])
        need = 1.0 + 0.34 * (wl + wr) + (1.0 if (wl and wr) else 0.0)
        w = max(3, int(math.ceil(need)), int(pitch * max(nT, nB, 1)),
                int(math.ceil(0.34 * len(plain(name)) + 1)))
    pr = [('r', 0, 0, w, h, 1)]
    pins = {}
    for side, lst in sides.items():
        for i, (num, nm) in enumerate(lst):
            if not nm and num.startswith('\x00'):
                continue
            if side == 'L':
                py = pitch * i + 1
                pr.append(L(-LEAD, py, 0, py))
                pins[num] = (-LEAD, py, 'L')
                pr.append(('t', 0.35, py + .17, nm, TXT, 'start', 0))
                pr.append(('t', -0.3, py - .28, num, TXT * .8, 'end', 0))
            elif side == 'R':
                py = pitch * i + 1
                pr.append(L(w, py, w + LEAD, py))
                pins[num] = (w + LEAD, py, 'R')
                pr.append(('t', w - 0.35, py + .17, nm, TXT, 'end', 0))
                pr.append(('t', w + 0.3, py - .28, num, TXT * .8, 'start', 0))
            elif side == 'T':
                px = pitch * i + 1
                pr.append(L(px, -LEAD, px, 0))
                pins[num] = (px, -LEAD, 'U')
                pr.append(('t', px + .17, 0.35, nm, TXT, 'start', -90))
                pr.append(('t', px - .28, -0.3, num, TXT * .8, 'end', -90))
            else:
                px = pitch * i + 1
                pr.append(L(px, h, px, h + LEAD))
                pins[num] = (px, h + LEAD, 'D')
                pr.append(('t', px + .17, h - 0.35, nm, TXT, 'end', -90))
                pr.append(('t', px - .28, h + 0.3, num, TXT * .8, 'start', -90))
    return pr, pins, (0, 0, w, h)


# ---------------- symbol instance ----------------

class Sym:
    def __init__(self, typ, ref, x, y, orient='v', value='', name='', opts=None,
                 sides=None, w=None, h=None, pitch=PITCH, dnp=False, flat=False):
        self.typ, self.ref, self.x, self.y = typ, ref, x, y
        self.value, self.name, self.dnp, self.flat = value, name, dnp, flat
        o = (orient or 'v').lower()
        self.mir = o in ('l', 'mir', 'flip')
        if o in ('l', 'r', 'mir', 'flip'):
            o = 'v'
        self.deg = ORIENT.get(o, 0)
        opts = opts or {}
        if typ in ('ic', 'conn'):
            pr, pins, bb = s_ic(sides or {}, w, h, name or value, pitch)
            self.deg, self.mir = 0, False       # bodies stay upright
        else:
            pr, pins, bb = TWO_PIN[typ](opts)
        self.prims, self.lpins, self.lbb = pr, pins, bb

    def _t(self, x, y):
        if self.mir:
            x = -x
        x, y = rot_pt(x, y, self.deg)
        return (self.x + x, self.y + y)

    def world_prims(self):
        out = []
        for p in self.prims:
            k = p[0]
            if k == 'l':
                x1, y1 = self._t(p[1], p[2]); x2, y2 = self._t(p[3], p[4])
                out.append(('l', x1, y1, x2, y2, p[5]))
            elif k == 'r':
                c = [self._t(p[1], p[2]), self._t(p[1] + p[3], p[2] + p[4])]
                x0 = min(c[0][0], c[1][0]); y0 = min(c[0][1], c[1][1])
                out.append(('r', x0, y0, abs(c[1][0] - c[0][0]),
                            abs(c[1][1] - c[0][1]), p[5]))
            elif k == 'c':
                x, y = self._t(p[1], p[2]); out.append(('c', x, y, p[3], p[4]))
            elif k == 'p':
                out.append(('p', [self._t(*q) for q in p[1]], p[2], p[3]))
            elif k == 'a':
                x1, y1 = self._t(p[1], p[2]); x2, y2 = self._t(p[3], p[4])
                sw = p[6] ^ (1 if self.mir else 0)
                if self.deg in (90, 270):
                    sw ^= 0
                out.append(('a', x1, y1, x2, y2, p[5], sw))
            elif k == 't':
                x, y = self._t(p[1], p[2])
                anc = p[5]
                if self.mir and anc in ('start', 'end'):
                    anc = 'end' if anc == 'start' else 'start'
                out.append(('t', x, y, p[3], p[4], anc, p[6]))
        return out

    def pins(self):
        out = {}
        for k, (px, py, d) in self.lpins.items():
            x, y = self._t(px, py)
            out[k] = (x, y, rot_dir(d, self.deg, self.mir))
        return out

    def bbox(self):
        c = [self._t(self.lbb[0], self.lbb[1]), self._t(self.lbb[2], self.lbb[3]),
             self._t(self.lbb[0], self.lbb[3]), self._t(self.lbb[2], self.lbb[1])]
        xs = [p[0] for p in c]; ys = [p[1] for p in c]
        return (min(xs), min(ys), max(xs), max(ys))

    def fields(self):
        """ref + value text, placed the way KiCad does: beside a rotated part,
        above/below an upright one."""
        x0, y0, x1, y1 = self.bbox()
        out = []
        if self.typ in ('ic', 'conn'):
            if self.flat:
                v = ('%s  %s' % (self.ref, self.value or self.name)).strip()
                return [('t', x0, y0 - .55, v, TXT, 'start', 0, 'ref')]
            if self.ref:
                out.append(('t', (x0 + x1) / 2, y0 - .85, self.ref, TXT * 1.1, 'middle', 0, 'ref'))
            v = self.value or self.name
            if v:
                dy = 2.9 if any(p[2] == 'D' for p in self.lpins.values()) else 1.25
                out.append(('t', (x0 + x1) / 2, y1 + dy, v, TXT, 'middle', 0, 'val'))
            return out
        horiz = self.deg in (90, 270)
        if horiz:
            if self.ref:
                out.append(('t', (x0 + x1) / 2, y0 - .6, self.ref, TXT, 'middle', 0, 'ref'))
            if self.value:
                out.append(('t', (x0 + x1) / 2, y1 + .85, self.value, TXT, 'middle', 0, 'val'))
        else:
            if self.ref:
                out.append(('t', x1 + .35, (y0 + y1) / 2 - .05, self.ref, TXT, 'start', 0, 'ref'))
            if self.value:
                out.append(('t', x1 + .35, (y0 + y1) / 2 + .65, self.value, TXT, 'start', 0, 'val'))
        return out


# ---------------- power / label glyphs ----------------

def glyph_gnd(x, y, kind='gnd', name=''):
    pr = [('l', x, y, x, y + .8, 1.0)]
    if kind == 'earth':
        pr += [('l', x - .6, y + .8, x + .6, y + .8, 1.1),
               ('l', x - .38, y + 1.1, x + .38, y + 1.1, 1.1),
               ('l', x - .16, y + 1.4, x + .16, y + 1.4, 1.1)]
        bb = (x - .6, y, x + .6, y + 1.4)
    else:
        pr += [('p', [(x - .62, y + .8), (x + .62, y + .8), (x, y + 1.5)], True, False)]
        bb = (x - .62, y, x + .62, y + 1.5)
    if name and name.upper() not in ('GND', ''):
        pr.append(('t', x, y + 2.15, name, TXT, 'middle', 0))
        bb = (bb[0], bb[1], bb[2], y + 2.3)
    return pr, bb


def glyph_pwr(x, y, name):
    pr = [('l', x, y, x, y - .8, 1.0), ('l', x - .62, y - .8, x + .62, y - .8, 1.1),
          ('t', x, y - 1.15, name, TXT, 'middle', 0)]
    return pr, (x - .62, y - 1.6, x + .62, y)


def glyph_label(x, y, d, name, kind='label', flow='in'):
    """local / hierarchical / global net label sitting on a wire end. Built
    facing right, then mirrored or rotated onto the pin's direction the way
    KiCad orients a label."""
    n = plain(name)
    tw = 0.34 * len(n) + 0.5
    if kind == 'label':
        pts, tx = [], (.3, .18)
    elif kind == 'glabel':
        pts = [(0, 0), (.45, -.45), (tw + .45, -.45), (tw + .9, 0),
               (tw + .45, .45), (.45, .45)]
        tx = (.7, .18)
    elif flow == 'out':
        pts = [(0, -.45), (tw, -.45), (tw + .45, 0), (tw, .45), (0, .45)]
        tx = (.7, .18)
    else:
        pts = [(0, 0), (.45, -.45), (tw + .45, -.45), (tw + .45, .45), (.45, .45)]
        tx = (.7, .18)
    deg, mir = 0, d == 'L'
    if d == 'U':
        deg = 270
    elif d == 'D':
        deg = 90

    def m(px, py):
        if mir:
            px = -px
        px, py = rot_pt(px, py, deg)
        return (x + px, y + py)
    anchor = 'end' if mir else 'start'
    trot = {90: -90, 270: -90}.get(deg, 0)
    tp = m(*tx)
    if deg == 90:
        tp = (tp[0] - .36, tp[1])
        anchor = 'end'
    pr = []
    if pts:
        pr.append(('p', [m(*q) for q in pts], True, False))
    pr.append(('t', tp[0], tp[1], name, TXT, anchor, trot))
    xs = [m(*q)[0] for q in pts] or [tp[0], tp[0] + (tw if not mir else -tw)]
    ys = [m(*q)[1] for q in pts] or [tp[1] - .5, tp[1] + .5]
    if deg:
        ys += [y - tw - 1 if deg == 270 else y, y if deg == 270 else y + tw + 1]
    else:
        xs += [tp[0] + (tw if not mir else -tw)]
    return pr, (min(xs), min(ys), max(xs), max(ys))


def glyph_nc(x, y):
    s = .3
    return [('l', x - s, y - s, x + s, y + s, 1.1), ('l', x - s, y + s, x + s, y - s, 1.1)], \
           (x - s, y - s, x + s, y + s)


# ---------------- router ----------------

def _behind(d, vx, vy):
    """target sits on the wrong side of a pin that faces direction d"""
    dx, dy = DIRV[d]
    return (vx * dx + vy * dy) < -1e-9


def route(a, b):
    """orthogonal 2- or 3-segment route. A pin is always left along the way it
    faces, so a wire never dives back through the body it came from."""
    pre, post = [], []
    ax, ay, ad = a
    bx, by, bd = b
    if ad and _behind(ad, bx - ax, by - ay):
        dx, dy = DIRV[ad]
        pre = [(ax, ay)]
        ax, ay = ax + dx, ay + dy
        ad = 'R' if dx == 0 else 'U'
    if bd and _behind(bd, ax - bx, ay - by):
        dx, dy = DIRV[bd]
        post = [(bx, by)]
        bx, by = bx + dx, by + dy
        bd = 'R' if dx == 0 else 'U'
    core = _route(ax, ay, ad, bx, by, bd)
    return pre + core + post


def _route(ax, ay, ad, bx, by, bd):
    if abs(ax - bx) < 1e-6 or abs(ay - by) < 1e-6:
        return [(ax, ay), (bx, by)]
    ah = ad in ('L', 'R')
    bh = bd in ('L', 'R')
    if ad is None and bd is None:
        return [(ax, ay), (bx, ay), (bx, by)]
    if ad is None:
        ah = not bh
    if bd is None:
        bh = not ah
    if ah and not bh:
        return [(ax, ay), (bx, ay), (bx, by)]
    if bh and not ah:
        return [(ax, ay), (ax, by), (bx, by)]
    if ah and bh:
        mx = (ax + bx) / 2.0
        return [(ax, ay), (mx, ay), (mx, by), (bx, by)]
    my = (ay + by) / 2.0
    return [(ax, ay), (ax, my), (bx, my), (bx, by)]


def _k(x, y):
    return (round(x, 3), round(y, 3))


def on_seg(p, s):
    """point strictly inside axis-aligned segment s"""
    (x1, y1), (x2, y2) = s
    x, y = p
    if abs(x1 - x2) < 1e-6 and abs(x - x1) < 1e-6:
        return min(y1, y2) + 1e-6 < y < max(y1, y2) - 1e-6
    if abs(y1 - y2) < 1e-6 and abs(y - y1) < 1e-6:
        return min(x1, x2) + 1e-6 < x < max(x1, x2) - 1e-6
    return False


def junctions(segs):
    ends = {}
    for s in segs:
        for p in s:
            ends[_k(*p)] = ends.get(_k(*p), 0) + 1
    out = []
    for p, n in ends.items():
        deg = n + 2 * sum(1 for s in segs if on_seg(p, s))
        if deg >= 3:
            out.append(p)
    return out


class UF:
    def __init__(self):
        self.p = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.p[ra] = rb


# ---------------- spec document ----------------

class SpecError(Exception):
    pass


def _norm(s):
    return re.sub(r'[^a-z0-9]', '', plain(s).lower())


def twid(s, size):
    return 0.56 * size * len(plain(s))


class Doc:
    def __init__(self, opts=None):
        self.o = opts or {}
        self.syms, self.order = {}, []
        self.alias = {}
        self.segs, self.ops = [], []
        self.title = ''
        self.pinuse = {}                 # (ref,pin) -> count of wire ends
        self.netdecl = []                # (name, [(ref,pin)], kind)
        self.errors, self.warns, self.info = [], [], []
        self.nl = self.o.get('nl')

    # --- helpers -------------------------------------------------
    def add(self, *op):
        self.ops.append(op)

    def toks(self, raw):
        raw = raw.strip()
        if not raw or raw.startswith('#'):
            return []
        out, cur, q = [], '', ''
        for ch in raw:
            if q:
                if ch == q:
                    q = ''
                else:
                    cur += ch
            elif ch in '"\'':
                q = ch
            elif ch.isspace():
                if cur:
                    out.append(cur); cur = ''
            elif ch == '#' and not cur:
                break
            else:
                cur += ch
        if cur:
            out.append(cur)
        return out

    def xy(self, tok):
        m = re.match(r'^(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)$', tok)
        if not m:
            raise SpecError(f"'{tok}' is not an x,y coordinate")
        return (float(m.group(1)), float(m.group(2)))

    def ep(self, tok):
        """endpoint -> (x, y, dir, (ref,pin) or None)"""
        if ',' in tok:
            x, y = self.xy(tok)
            return (x, y, None, None)
        ref, _, pin = tok.partition('.')
        s = self.syms.get(ref)
        if not s:
            raise SpecError(f"no symbol '{ref}' (declare it before using it)")
        pins = s.pins()
        if not pin:
            if len(pins) == 1:
                pin = list(pins)[0]
            else:
                raise SpecError(f"'{ref}' has {len(pins)} pins: say {ref}.PIN "
                                f"(one of {', '.join(sorted(pins))})")
        key = pin if pin in pins else self.alias.get(ref, {}).get(_norm(pin))
        if key is None:
            av = ', '.join(sorted(pins)) or '(none)'
            an = ', '.join(sorted(self.alias.get(ref, {}))) if self.alias.get(ref) else ''
            raise SpecError(f"'{ref}' has no pin '{pin}'. numbers: {av}"
                            + (f" names: {an}" if an else ''))
        x, y, d = pins[key]
        self.pinuse[(ref, key)] = self.pinuse.get((ref, key), 0) + 1
        return (x, y, d, (ref, key))

    def wire(self, a, b, pts=()):
        path = [a]
        for p in pts:
            path.append(p)
        path.append(b)
        segs = []
        for i in range(len(path) - 1):
            r = route(path[i], path[i + 1])
            for k in range(len(r) - 1):
                if _k(*r[k]) != _k(*r[k + 1]):
                    segs.append((r[k], r[k + 1]))
        self.segs.extend(segs)
        return segs

    # --- statements ----------------------------------------------
    def parse(self, text):
        for n, raw in enumerate(text.splitlines(), 1):
            t = self.toks(raw)
            if not t:
                continue
            try:
                self.stmt(t[0].lower(), t[1:], n)
            except SpecError as e:
                self.errors.append(f"line {n}: {e}")
            except Exception as e:
                self.errors.append(f"line {n}: {type(e).__name__}: {e}")
        return self

    def stmt(self, kw, a, ln):
        if kw == 'title':
            self.title = ' '.join(a); return
        if kw == 'note':
            x, y = self.xy(a[0])
            self.add('t', x, y, ' '.join(a[1:]), TXT, 'start', 0, 'note'); return
        if kw == 'group':
            x, y = self.xy(a[0]); w, h = self.xy(a[1])
            self.add('r', x, y, w, h, 0, 'group')
            if len(a) > 2:
                self.add('t', x + .2, y - .35, ' '.join(a[2:]), TXT, 'start', 0, 'note')
            return
        if kw in ('ic', 'conn'):
            return self.st_ic(kw, a)
        if kw in TWO_PIN:
            return self.st_two(kw, a)
        if kw == 'wire':
            if len(a) < 2:
                raise SpecError('wire needs at least two endpoints')
            eps = [self.ep(t) for t in a]
            for i in range(len(eps) - 1):
                self.wire(eps[i][:3], eps[i + 1][:3])
            pins = [e[3] for e in eps if e[3]]
            if len(pins) > 1:
                self.netdecl.append((None, pins, 'wire'))
            return
        if kw == 'net':
            return self.st_net(a)
        if kw == 'gnd':
            e = self.ep(a[0])
            kind = (a[1].lower() if len(a) > 1 else 'gnd')
            name = kind.upper() if kind not in ('gnd', 'earth') else ('GND' if kind == 'gnd' else 'EARTH')
            x, y, d = e[:3]
            if d == 'D':
                p = (x, y + 2)
            elif d == 'U':
                p = (x, y - 2)
            else:
                p = (x + (2 if d == 'R' else -2), y + 1)
            self.wire((x, y, d), (p[0], p[1], 'U'))
            pr, bb = glyph_gnd(p[0], p[1], 'earth' if kind == 'earth' else 'gnd', name)
            for q in pr:
                self.add(*q, 'pwr')
            if e[3]:
                self.netdecl.append((name, [e[3]], 'pwr'))
            return
        if kw == 'pwr':
            e = self.ep(a[1]) if len(a) > 1 else None
            if e is None:
                raise SpecError('pwr NAME REF.PIN')
            name = a[0]
            x, y, d = e[:3]
            if d == 'U':
                p = (x, y - 2)
            elif d == 'D':
                p = (x, y + 2)
            else:
                p = (x + (2 if d == 'R' else -2), y - 1)
            self.wire((x, y, d), (p[0], p[1], 'D'))
            pr, bb = glyph_pwr(p[0], p[1], name)
            for q in pr:
                self.add(*q, 'pwr')
            if e[3]:
                self.netdecl.append((name, [e[3]], 'pwr'))
            return
        if kw in ('label', 'glabel', 'hlabel'):
            name = a[0]
            e = self.ep(a[1])
            flow = a[2].lower() if len(a) > 2 else 'in'
            x, y, d = e[:3]
            pr, bb = glyph_label(x, y, d or 'R', name, kw, flow)
            for q in pr:
                self.add(*q, kw)
            if e[3]:
                self.netdecl.append((name, [e[3]], kw))
            return
        if kw == 'nc':
            e = self.ep(a[0])
            pr, bb = glyph_nc(e[0], e[1])
            for q in pr:
                self.add(*q, 'nc')
            return
        raise SpecError(f"unknown statement '{kw}' (try: ksch.py help)")

    def st_two(self, kw, a):
        if len(a) < 2:
            raise SpecError(f'{kw} REF x,y [orient] [value]')
        ref = a[0]
        x, y = self.xy(a[1])
        rest = a[2:]
        orient = 'v'
        if rest and rest[0].lower() in set(list(ORIENT) + ['l', 'r', 'mir', 'flip']):
            if not (kw in ('r',) and len(rest) == 1 and rest[0].lower() == 'r'):
                orient = rest[0].lower(); rest = rest[1:]
        dnp = any(t.lower() == 'dnp' for t in rest)
        rest = [t for t in rest if t.lower() != 'dnp']
        val = ' '.join(rest)
        if ref in self.syms:
            raise SpecError(f"duplicate ref '{ref}'")
        if val == '@' and self.nl:
            val = unesc(self.nl.value(ref))
        self.syms[ref] = Sym(kw, ref, x, y, orient, val, dnp=dnp,
                             opts={'us': self.o.get('us')})
        self.order.append(ref)

    def st_ic(self, kw, a):
        if len(a) < 2:
            raise SpecError('ic REF x,y NAME [w=N] [h=N] L:1=EN,2=IN R:6=OUT B:8=GND')
        ref = a[0]
        x, y = self.xy(a[1])
        name, w, h = '', None, None
        pitch, dnp, flat = PITCH, False, False
        sides = {}
        for t in a[2:]:
            m = re.match(r'^([LRTB]):(.*)$', t, re.I)
            if m:
                side = m.group(1).upper()
                lst = []
                for i, e in enumerate(m.group(2).split(',')):
                    e = e.strip()
                    if not e:
                        lst.append(None); continue
                    num, _, nm = e.partition('=')
                    if not nm:
                        nm = unesc(self.nl.pinname(ref, num)) if self.nl else num
                    lst.append((num, unesc(nm)))
                sides[side] = lst
            elif t.lower().startswith('w='):
                w = float(t[2:])
            elif t.lower().startswith('h='):
                h = float(t[2:])
            elif t.lower().startswith('p='):
                pitch = float(t[2:])
            elif t.lower() == 'dnp':
                dnp = True
            elif t.lower() == 'flat':
                flat = True
            elif not name:
                name = t
        if name == '@':
            name = unesc(self.nl.value(ref)) if self.nl else ref
        if not sides:
            if not self.nl:
                raise SpecError('no pins given (need L:/R:/T:/B:, or --net for auto)')
            sides = self.auto_sides(ref)
        # blank slots keep alignment: replace None with a spacer
        clean = {}
        for s, lst in sides.items():
            out = []
            for i, e in enumerate(lst):
                if e:
                    out.append(e)
                else:
                    out.append(('\x00%d%s' % (i, s), ''))
            clean[s] = out
        if ref in self.syms:
            raise SpecError(f"duplicate ref '{ref}'")
        sym = Sym(kw, ref, x, y, 'v', '', name, sides=clean, w=w, h=h, pitch=pitch,
                  dnp=dnp, flat=flat)
        for s, lst in clean.items():
            for num, nm in lst:
                if num.startswith('\x00'):
                    sym.lpins.pop(num, None)
        self.syms[ref] = sym
        self.order.append(ref)
        self.alias[ref] = {}
        for s, lst in clean.items():
            for num, nm in lst:
                if nm:
                    self.alias[ref].setdefault(_norm(nm), num)

    def auto_sides(self, ref):
        """netlist-driven placement: power up, ground down, inputs left,
        outputs right - the way a KiCad symbol is normally drawn."""
        sp = self.nl.sympins(ref)
        if not sp:
            raise SpecError(f"{ref} not in the netlist; give pins explicitly")
        Lp, Rp, Tp, Bp = [], [], [], []
        for num in sorted(sp, key=lambda z: (len(z), z)):
            nm, ty = sp[num]
            n, t = plain(nm).upper(), (ty or '').lower()
            net = (self.nl.cpins.get(ref, {}) or {}).get(num, '')
            if 'GND' in n or 'VSS' in n or (net and self.nl.is_gnd(net)):
                Bp.append((num, nm))
            elif re.match(r'^(VCC|VDD|VIN|V\+|VBAT|VBUS|AVDD|DVDD|VDDA)', n) or \
                    (t == 'power_in' and 'GND' not in n):
                Tp.append((num, nm))
            elif t in ('output', 'open_collector', 'open_emitter', 'tri_state'):
                Rp.append((num, nm))
            else:
                Lp.append((num, nm))
        if len(Lp) > 2 * max(1, len(Rp)) and len(Lp) > 6:
            half = (len(Lp) + 1) // 2
            Rp = Lp[half:] + Rp
            Lp = Lp[:half]
        out = {}
        for k, v in (('L', Lp), ('R', Rp), ('T', Tp), ('B', Bp)):
            if v:
                out[k] = v
        return out

    def st_net(self, a):
        name = a[0]
        rest = list(a[1:])
        trunk = None
        if rest and rest[0].startswith('@'):
            m = re.match(r'^@([xy])=(-?\d+(?:\.\d+)?)$', rest[0])
            if not m:
                raise SpecError("trunk must look like @x=12 or @y=7")
            trunk = (m.group(1), float(m.group(2)))
            rest = rest[1:]
        eps = [self.ep(t) for t in rest]
        if len(eps) < 2:
            raise SpecError('net NAME needs at least two endpoints')
        if trunk:
            ax, tv = trunk
            touch = []
            for e in eps:
                tgt = (tv, e[1], None) if ax == 'x' else (e[0], tv, None)
                self.wire(e[:3], tgt)
                touch.append(tgt[1] if ax == 'x' else tgt[0])
            lo, hi = min(touch), max(touch)
            if hi > lo:
                p1 = (tv, lo) if ax == 'x' else (lo, tv)
                p2 = (tv, hi) if ax == 'x' else (hi, tv)
                self.segs.append((p1, p2))
            if ax == 'x':
                self.add('t', tv + .3, lo - .7, name, TXT, 'start', 0, 'label')
            else:
                self.add('t', lo - .6, tv - .5, name, TXT, 'start', 0, 'label')
        else:
            for e in eps[1:]:
                self.wire(eps[0][:3], e[:3])
            x, y, d = eps[0][:3]
            self.add('t', x + (.4 if d != 'L' else -.4), y - .4, name, TXT,
                     'end' if d == 'L' else 'start', 0, 'label')
        pins = [e[3] for e in eps if e[3]]
        if pins:
            self.netdecl.append((name, pins, 'net'))

    # --- build + emit --------------------------------------------
    def build(self):
        ops = []
        for ref in self.order:
            s = self.syms[ref]
            for p in s.world_prims():
                if p[0] == 't':
                    role = 'pnum' if p[4] < TXT * .9 else 'pname'
                    ops.append(p + (role,))
                else:
                    ops.append(p + ('dnp' if s.dnp else 'body',))
            for f in s.fields():
                ops.append(f)
        ops.extend(self.ops)
        return ops

    def bbox(self):
        xs, ys = [], []

        def pt(x, y):
            xs.append(x); ys.append(y)
        for a, b in self.segs:
            pt(*a); pt(*b)
        for op in self.build():
            k = op[0]
            if k == 'l':
                pt(op[1], op[2]); pt(op[3], op[4])
            elif k == 'r':
                pt(op[1], op[2]); pt(op[1] + op[3], op[2] + op[4])
            elif k == 'c':
                pt(op[1] - op[3], op[2] - op[3]); pt(op[1] + op[3], op[2] + op[3])
            elif k == 'p':
                for q in op[1]:
                    pt(*q)
            elif k == 'a':
                pt(op[1], op[2]); pt(op[3], op[4])
            elif k == 't':
                w = twid(op[3], op[4]); anc = op[5]; rot = op[6]
                if rot:
                    pt(op[1] - op[4], op[2]); pt(op[1] + op[4], op[2] - w if anc == 'start' else op[2] + w)
                else:
                    x0 = op[1] - (w if anc == 'end' else w / 2 if anc == 'middle' else 0)
                    pt(x0, op[2] - op[4]); pt(x0 + w, op[2] + op[4] * .4)
        if not xs:
            xs, ys = [0, 1], [0, 1]
        return min(xs), min(ys), max(xs), max(ys)

    def svg(self):
        T = THEMES[self.o.get('theme', 'kicad')]
        PX = float(self.o.get('px', 22))
        M = 1.2
        x0, y0, x1, y1 = self.bbox()
        if self.title:
            y0 -= 1.6
            x1 = max(x1, x0 + twid(self.title, TXT * 1.3))
        W = (x1 - x0 + 2 * M) * PX
        H = (y1 - y0 + 2 * M) * PX

        def X(v):
            return round((v - x0 + M) * PX, 2)

        def Y(v):
            return round((v - y0 + M) * PX, 2)

        def S(v):
            return round(v * PX, 2)
        e = ['<svg xmlns="http://www.w3.org/2000/svg" width="%.0f" height="%.0f" '
             'viewBox="0 0 %.0f %.0f" font-family="DejaVu Sans,Verdana,Arial,sans-serif">'
             % (W, H, W, H),
             '<rect width="%.0f" height="%.0f" fill="%s"/>' % (W, H, T['bg'])]
        if self.o.get('grid'):
            gx = math.ceil(x0)
            while gx <= x1:
                gy = math.ceil(y0)
                while gy <= y1:
                    e.append('<circle cx="%s" cy="%s" r="0.7" fill="%s"/>' % (X(gx), Y(gy), T['grid']))
                    gy += 1
                gx += 1
        if self.o.get('frame'):
            e.append('<rect x="4" y="4" width="%.0f" height="%.0f" fill="none" stroke="%s"/>'
                     % (W - 8, H - 8, T['note']))
        if self.title:
            e.append(self._text(X(x0), Y(y0 + 1.0), self.title, S(TXT * 1.3), 'start',
                                T['body'], 0, bold=True))
        for a, b in self.segs:
            e.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s" '
                     'stroke-linecap="round"/>'
                     % (X(a[0]), Y(a[1]), X(b[0]), Y(b[1]), T['wire'], S(.055)))
        for jx, jy in junctions(self.segs):
            e.append('<circle cx="%s" cy="%s" r="%s" fill="%s"/>'
                     % (X(jx), Y(jy), S(.14), T['junc']))
        for op in self.build():
            k, role = op[0], op[-1]
            col = T.get(role, T['body'])
            dsh = ' stroke-dasharray="%s,%s"' % (S(.18), S(.14)) if role == 'dnp' else ''
            if k == 'l':
                e.append('<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" stroke-width="%s"%s/>'
                         % (X(op[1]), Y(op[2]), X(op[3]), Y(op[4]), col, S(.05 * op[5]), dsh))
            elif k == 'r':
                fill = T['fill'] if op[5] == 1 else ('none' if op[5] == 0 else col)
                dash = ' stroke-dasharray="%s,%s"' % (S(.2), S(.15)) if role in ('group', 'dnp') else ''
                e.append('<rect x="%s" y="%s" width="%s" height="%s" fill="%s" stroke="%s" '
                         'stroke-width="%s"%s/>'
                         % (X(op[1]), Y(op[2]), S(op[3]), S(op[4]), fill,
                            T['note'] if role == 'group' else col, S(.055), dash))
            elif k == 'c':
                fill = T['fill'] if op[4] == 1 else ('none' if op[4] == 0 else col)
                e.append('<circle cx="%s" cy="%s" r="%s" fill="%s" stroke="%s" stroke-width="%s"/>'
                         % (X(op[1]), Y(op[2]), S(op[3]), fill, col, S(.055)))
            elif k == 'p':
                d = 'M ' + ' L '.join('%s %s' % (X(q[0]), Y(q[1])) for q in op[1])
                if op[2]:
                    d += ' Z'
                fill = T['fill'] if op[3] == 1 else ('none' if not op[3] else col)
                e.append('<path d="%s" fill="%s" stroke="%s" stroke-width="%s"%s '
                         'stroke-linejoin="round"/>' % (d, fill, col, S(.055), dsh))
            elif k == 'a':
                e.append('<path d="M %s %s A %s %s 0 0 %d %s %s" fill="none" stroke="%s" '
                         'stroke-width="%s"/>'
                         % (X(op[1]), Y(op[2]), S(op[5]), S(op[5]), op[6],
                            X(op[3]), Y(op[4]), col, S(.055)))
            elif k == 't':
                e.append(self._text(X(op[1]), Y(op[2]), op[3], S(op[4]), op[5], col, op[6],
                                    bold=(role == 'ref')))
        e.append('</svg>')
        return ''.join(e)

    @staticmethod
    def _text(x, y, s, size, anchor, fill, rot=0, bold=False):
        parts = split_overbar(unesc(str(s)))
        body = ''.join(
            (''.join(c + '\u0305' for c in t) if ob else t) for t, ob in parts)
        body = _x(body)
        tr = ' transform="rotate(%d %s %s)"' % (rot, x, y) if rot else ''
        return ('<text x="%s" y="%s" font-size="%s" fill="%s" text-anchor="%s"%s%s>%s</text>'
                % (x, y, size, fill, anchor, ' font-weight="bold"' if bold else '', tr, body))


def _x(s):
    return (str(s).replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;')
            .replace('"', '&quot;'))


# ---------------- checks ----------------

def _ovl(a, b, pad=0.0):
    return not (a[2] <= b[0] + pad or b[2] <= a[0] + pad or
                a[3] <= b[1] + pad or b[3] <= a[1] + pad)


def _seg_hits_box(s, bb):
    (x1, y1), (x2, y2) = s
    lo = (min(x1, x2), min(y1, y2), max(x1, x2), max(y1, y2))
    return _ovl(lo, bb, 0.05)


def check_doc(d):
    out, unwired = [], []
    refs = d.order
    for i in range(len(refs)):
        for j in range(i + 1, len(refs)):
            a, b = d.syms[refs[i]], d.syms[refs[j]]
            if _ovl(a.bbox(), b.bbox()):
                out.append(('WARN', f"{refs[i]} and {refs[j]} bodies overlap - move one"))
    for ref in refs:
        s = d.syms[ref]
        bb = s.bbox()
        for seg in d.segs:
            if _seg_hits_box(seg, bb):
                ends = {_k(*seg[0]), _k(*seg[1])}
                pinpts = {_k(p[0], p[1]) for p in s.pins().values()}
                if ends & pinpts:
                    continue
                out.append(('WARN', f"a wire crosses the body of {ref} at "
                                    f"({seg[0][0]:g},{seg[0][1]:g})-({seg[1][0]:g},{seg[1][1]:g})"))
                break
    for ref in refs:
        s = d.syms[ref]
        bypos = {}
        for p, (x, y, _dd) in s.pins().items():
            bypos.setdefault(_k(x, y), []).append(p)
        loose = []
        for _pos, names in bypos.items():
            if any((ref, n) in d.pinuse for n in names):
                continue
            loose.append(sorted(names, key=lambda z: (not z.isdigit(), z))[0])
        if loose:
            unwired.append(f"{ref}.{'/'.join(sorted(loose))}")
    if unwired:
        out.append(('INFO', "drawn but not wired: " + ' '.join(unwired)
                    + "   (use `nc REF.PIN` where that is deliberate)"))
    return out


def drawn_nets(d):
    """union-find over wire geometry -> [(set of (ref,pin), set of net names)]"""
    uf = UF()
    for a, b in d.segs:
        uf.union(_k(*a), _k(*b))
    pts = set()
    for a, b in d.segs:
        pts.add(_k(*a)); pts.add(_k(*b))
    for p in pts:
        for s in d.segs:
            if on_seg(p, s):
                uf.union(p, _k(*s[0]))
    groups = {}
    for ref in d.order:
        for pin, (x, y, _dir) in d.syms[ref].pins().items():
            if (ref, pin) not in d.pinuse:
                continue   # a symbol's convenience aliases (a diode's 'a'/'k' for
                           # its '1'/'2', a FET's 'g'/'d'/'s', a battery's '+'/'-')
                           # sit at the same point as a numbered pin; only check
                           # the pin the spec actually referenced, not every alias
            k = _k(x, y)
            if k in uf.p:
                groups.setdefault(uf.find(k), [set(), set()])[0].add((ref, pin))
    for name, pins, kind in d.netdecl:
        if not name or not pins:
            continue
        x, y, _dir = d.syms[pins[0][0]].pins()[pins[0][1]]
        k = _k(x, y)
        if k in uf.p:
            groups.setdefault(uf.find(k), [set(), set()])[1].add(name)
    return list(groups.values())


def verify_doc(d, nl):
    out = []
    for ref in d.order:
        if ref not in nl.comps:
            out.append(('WARN', f"{ref} is not in the netlist"))
    # Only pins the spec actually wired/labelled - checking every alias a symbol
    # type defines ('a'/'k' on a diode, 'g'/'d'/'s' on a FET, '+'/'-' on a battery)
    # would flag nearly every diode/FET/battery, since real parts number pins
    # numerically and those aliases are never themselves real netlist pins.
    for ref, pin in sorted(d.pinuse):
        c = nl.comps.get(ref)
        if not c:
            continue
        sp = nl.sympins(ref)
        if sp and pin not in sp:
            out.append(('ERROR', f"{ref} has no pin {pin} on the real part "
                                 f"(it has {' '.join(sorted(sp))})"))
    for pins, names in drawn_nets(d):
        real = {}
        for ref, pin in pins:
            n = nl.cpins.get(ref, {}).get(pin)
            real.setdefault(n, []).append(f"{ref}.{pin}")
        if len(real) > 1:
            bits = '; '.join(f"{k or 'unconnected'}: {' '.join(v)}" for k, v in real.items())
            out.append(('ERROR', f"drawn as one node but the board disagrees -> {bits}"))
        elif real:
            rn = list(real)[0]
            for nm in names:
                if rn and _norm(nm.split('/')[-1]) != _norm(rn.split('/')[-1]) \
                        and not (nl.is_gnd(rn) and 'GND' in nm.upper()):
                    out.append(('WARN', f"label '{nm}' sits on net {rn}"))
    return out


# ---------------- symbol reference ----------------

def cmd_syms():
    rows = [
        ('r', '1 2', 'pin1', 'resistor (IEC box; --us for zigzag)'),
        ('c cp', '1 2', 'pin1', 'capacitor / polarised capacitor'),
        ('l fb', '1 2', 'pin1', 'inductor / ferrite bead'),
        ('d ds dz led', '1 2 a k', 'anode', 'diode / schottky / zener / LED'),
        ('tvs', '1 2', 'pin1', 'bidirectional TVS'),
        ('fuse', '1 2', 'pin1', 'fuse'),
        ('sw', '1 2', 'pin1', 'momentary switch'),
        ('xtal', '1 2', 'pin1', 'crystal'),
        ('bat', '1 2 + -', '+ pin', 'battery / cell'),
        ('jp', '1 2', 'pin1', 'solder jumper'),
        ('tp', '1', 'pin1', 'test point'),
        ('ant', '1', 'feed', 'antenna'),
        ('nmos pmos', '1 2 3 g d s', 'gate', 'MOSFET, gate left, drain up, source down'),
        ('npn pnp', '1 2 3 b c e', 'base', 'BJT, base left, collector up, emitter down'),
        ('ic conn', 'as declared', 'top-left of body', 'IC / connector body'),
    ]
    print("type            pins            anchor at        symbol")
    for t, p, a, desc in rows:
        print(f"{t:<15} {p:<15} {a:<16} {desc}")
    print("""
Anchor = the coordinate you write in the spec. Two-pin parts are 2 units long,
so `c C1 6,4 v` puts pin 1 at 6,4 and pin 2 at 6,6.
ORIENT: v (default, pin1 top) h (pin1 left) vr (pin1 bottom) hr (pin1 right).
Transistors: default gate/base on the left; `l` mirrors it.
Connection glyphs: gnd  pwr  label  hlabel  glabel  nc.""")


# ---------------- CLI ----------------

def load_netlist(path):
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import kcommon
    return kcommon.Netlist(path)


def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('-o', '--out', default='')
    ap.add_argument('--net', default='')
    ap.add_argument('--verify', action='store_true')
    ap.add_argument('--px', type=float, default=22)
    ap.add_argument('--theme', default='kicad', choices=list(THEMES))
    ap.add_argument('--us', action='store_true')
    ap.add_argument('--grid', action='store_true')
    ap.add_argument('--frame', action='store_true')
    ap.add_argument('--quiet', action='store_true')
    ap.add_argument('-h', '--help', action='store_true')
    a, pos = ap.parse_known_args(sys.argv[1:])
    bad = [p for p in pos if p.startswith('--')]
    if bad:
        print(f"unknown flag {bad[0]}", file=sys.stderr); return 3
    a.cmd = pos[0] if pos else 'help'
    a.spec = pos[1] if len(pos) > 1 else '-'
    if a.help or a.cmd in ('help', '-h', '--help'):
        print(__doc__); return 0
    if a.cmd == 'syms':
        cmd_syms(); return 0
    if a.cmd not in ('render', 'check'):
        print(f"unknown command '{a.cmd}' (render | check | syms | help)", file=sys.stderr)
        return 3
    text = sys.stdin.read() if a.spec == '-' else open(a.spec, encoding='utf-8').read()
    nl = None
    if a.net:
        try:
            nl = load_netlist(a.net)
        except FileNotFoundError:
            print(f"no such netlist: {a.net}", file=sys.stderr); return 3
    d = Doc(dict(px=a.px, theme=a.theme, us=a.us, grid=a.grid, frame=a.frame, nl=nl))
    d.parse(text)
    if d.errors:
        for m in d.errors:
            print(f"ERROR  {m}", file=sys.stderr)
        return 3
    msgs = check_doc(d)
    if nl and (a.verify or a.net):
        msgs += verify_doc(d, nl)
    hard = [m for m in msgs if m[0] == 'ERROR']
    if a.cmd == 'check':
        for s, m in msgs or [('INFO', 'no findings')]:
            print(f"{s:<6} {m}")
        return 2 if hard else 0
    out = a.out or 'ksch.svg'
    open(out, 'w', encoding='utf-8').write(d.svg())
    x0, y0, x1, y1 = d.bbox()
    print(f"wrote {out}  ({len(d.order)} symbols, {len(d.segs)} wire segments, "
          f"{(x1 - x0):.0f}x{(y1 - y0):.0f} units)")
    if not a.quiet:
        for s, m in msgs:
            print(f"{s:<6} {m}")
    return 2 if hard else 0


try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
