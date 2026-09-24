#!/usr/bin/env python3
"""
kpcb.py v2 - placement review for a KiCad board (.kicad_pcb, KiCad 7-10).

The placement-stage companion to knet.py. knet answers "what is wired to what";
kpcb answers "where is it, and is that a sane place to put it". Nothing here
needs routing to exist - every check runs on footprint geometry alone.

Indexes built from the board file:
  fps      ref -> footprint, layer, x/y/rot, sheet, attrs, dnp, courtyard bbox
  pads     ref -> [(num, net, board xy, size, pinfunction, pintype)]
  nets     net name -> [(ref, pad)]        taken from the pads, no .net needed
  outline  Edge.Cuts segments + closed rings, for inside/outside and clearance

Commands:
  kpcb.py FILE summary                board size, stackup, what is placed, per sheet
  kpcb.py FILE check                  rule-based placement review (see RULES)
  kpcb.py FILE where U8 J7            position, courtyard, edge distance, neighbours
  kpcb.py FILE where 130,60 -r 8      ...same, around a coordinate
  kpcb.py FILE map                    ASCII occupancy map of the board
  kpcb.py FILE sheet                  per-sheet placement cohesion + bounding box
  kpcb.py FILE unplaced               what is still parked off the outline
  kpcb.py FILE span                   nets ranked by how far apart their pads sit
  kpcb.py FILE ic                     which parts `ic REF` can advise on
  kpcb.py FILE ic U13                 WHERE to put a regulator's passives, with
                                      an ASCII picture of the recommendation
  kpcb.py FILE viapad [--signal] [--min N]
                                      components with a via centred in an SMD pad
                                      (via-in-pad -> needs filled+capped vias)
  kpcb.py FILE where F5.1 BT1.1       a PAD's absolute centre, size, net; two specs
                                      also print the distance between them
  kpcb.py FILE net NET                every pad on a net (absolute xy), copper per
                                      layer, vias, zones, extent
  kpcb.py FILE height [REF...]        3D model height per part + the Z stack (cell +
                                      board + tallest part over it), via KiCad's GLB
                                      export of the STEP models; no model = UNKNOWN
  kpcb.py FILE rf [NET...]            50-ohm trace review: width necks, microstrip Zo
                                      from the stackup, same-layer GND gap, reference
                                      plane under it, GND via fence vs lambda/20

`ic` is the one command that suggests rather than judges. It reads the IC's own
pad coordinates - there is no per-part template - classifies its pins by name,
finds its passives, and places each one by the rule that actually governs it:
input caps straddling the tightest VIN/PGND pad pair on the outside face of the
package (that is the high-di/dt loop, made small), the inductor hard against the
SW pads, output caps as a bank at the inductor's far pad, the feedback divider
run out the far side away from SW, boot and bias caps at their own pins. With
the IC still in the parked pile it anchors on whichever passive is already
placed and reports where the IC itself then goes.

Thresholds (mm) - defaults are conservative; override per board in kpcb.json:
  --edge 0.5      courtyard-to-board-edge minimum
  --hole 1.5      keepout margin beyond a mounting hole's own pad/drill radius
  --conn 10.0     how far a connector may sit from the nearest edge
  --rf 8.0        RF part / antenna net to switching node
  --therm 8.0     heat source to heat-sensitive part
  --bypass 3.0    supply pin to its nearest bypass cap
  --clear 0.0     extra margin added to every courtyard-vs-courtyard test
  --gap 0.25      courtyard-to-courtyard gap inside an `ic` suggestion
  --fb 4.0        `ic`: feedback part to switching node
  --tol 1.0       `ic`: slop before a placed part reads as OK in its slot
  --span 0        NETSPAN threshold (0 = half the board diagonal)
  --fanout 8      a net with more nodes than this is a rail, not a signal

Project config: kpcb.json next to the board persists board defaults and mutes
confirmed non-bugs, exactly like knet.json does for the netlist, e.g.
  {"edge": 0.3, "bypass": 4.0, "suppress": ["CONNACC:J8", "UNPLACED"]}
Precedence: built-in defaults < kpcb.json < command line.

Output is capped on purpose. A board has hundreds of footprints and an
uncapped pairwise check would bury the answer: findings that share a shape
fold into one `tail [N]: refs` line, each rule stops after --max lines with a
`+N more` tail, and neighbour lists are capped too. Counts in the tails stay
accurate even when not every item is printed.

Exit codes: 0 clean, 1 not found, 2 `check` found an ERROR, 3 bad file.
"""
import sys, os, re, json, math, glob, argparse
from collections import defaultdict

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
try:
    from kzo import microstrip, field_zo              # trace impedance (kzo.py beside this)
    from kcommon import (load_sexp, kids, kid, val, has, refrange, natkey, trunc,
                      prefix, parse_value, unesc_disp, GND_RE, rail_voltage,
                      print_findings, suppressed)
except ImportError:                                     # pragma: no cover
    print("kpcb.py needs kcommon.py beside it (shared parser + finding formatter)",
          file=sys.stderr)
    raise

# ---------------- part classification ----------------
# Small and deliberately greppable: these are the knobs to turn when a check
# misfires on a board whose parts this tool has never seen.

RF_NET   = re.compile(r'(^|[/_])(ANT|RF|LNA|RFIN|RFOUT|VCC_RF)(_|$|\d)', re.I)
RF_FP    = re.compile(r'Connector_Coaxial|RF_Module|RF_GPS|Antenna|U\.?FL|SMA_', re.I)
RF_VAL   = re.compile(r'\b(NEO-M9|NEO-M8|SX12\d\d|E22|ESP32|BALUN|SAW)', re.I)
SW_PIN   = re.compile(r'^(SW|LX|PH|VSW|SWITCH|VLX)\d*$', re.I)
HOT_VAL  = re.compile(r'\b(BQ25\d|LM6146|TPS2594|TPS6\d|TPS7A|AP63\d|MP\d{4}|TPS55)', re.I)
SENS_FP  = re.compile(r'Crystal|Oscillator|BatteryHolder|BAT-SMD', re.I)
SENS_VAL = re.compile(r'\b(NEO-M9|NEO-M8|32\.768|TCXO)', re.I)
CONN_FP  = re.compile(r'^Connector|PinHeader|PinSocket|JST|Molex|USB|TestPoint', re.I)
EDGEMNT  = re.compile(r'EdgeMount|Coaxial|SMA|U\.?FL|USB_C_Receptacle|Horizontal', re.I)
HOLE_FP  = re.compile(r'MountingHole|Mounting_Hole', re.I)
# supply pin NAMES. Deliberately not `pintype == power_in`: easyeda2kicad types
# almost every pin `passive`, so a type-based test would check nothing at all.
# `VSS` must not sneak in via a `VS` prefix, hence the explicit list.
SUP_PIN  = re.compile(r'^(VDD|VCC|AVDD|AVCC|VDDA|VDDIO|VDDL|VBAT|VBUS|VIN|VSYS|VPP?'
                      r'|\d+V\d*)\w*$', re.I)

# ---------------- geometry ----------------

def xf(lx, ly, ox, oy, rot):
    """Footprint-local (lx,ly) -> board coords. KiCad rotates CCW on a screen
    whose +y points down, which is this sign pattern and not the textbook one."""
    if not rot:
        return (ox + lx, oy + ly)
    t = math.radians(rot)
    c, s = math.cos(t), math.sin(t)
    return (ox + lx * c + ly * s, oy - lx * s + ly * c)

def bbox(pts):
    xs = [p[0] for p in pts]; ys = [p[1] for p in pts]
    return (min(xs), min(ys), max(xs), max(ys))

def grow(b, m):
    return (b[0] - m, b[1] - m, b[2] + m, b[3] + m)

def hit(a, b):
    """Do two bboxes overlap? Touching exactly is not an overlap."""
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]

def overlap_area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return w * h if w > 0 and h > 0 else 0.0

def poly_area(pts):
    """Shoelace area of a ring of (x,y). Absolute, so winding direction and
    island/hole orientation never make it negative."""
    n = len(pts)
    if n < 3:
        return 0.0
    s = sum(pts[i][0] * pts[(i + 1) % n][1] - pts[(i + 1) % n][0] * pts[i][1]
            for i in range(n))
    return abs(s) / 2.0

def point_in_poly(pt, pts):
    """Ray-casting point-in-polygon test; pts is a ring of (x, y). Used to
    point-sample whether a zone fill actually covers a rectangle, since a
    fill's bbox can be misleadingly solid-looking (see `zones REF`)."""
    x, y = pt
    inside = False
    n = len(pts)
    for i in range(n):
        x1, y1 = pts[i]
        x2, y2 = pts[(i + 1) % n]
        if (y1 > y) != (y2 > y):
            xint = x1 + (y - y1) * (x2 - x1) / (y2 - y1)
            if x < xint:
                inside = not inside
    return inside

def _sample_grid(rect, n=11):
    """n x n sample points spread evenly inside a rectangle (never on its
    edge, so a fill boundary doesn't produce a coin-flip result)."""
    x0, y0, x1, y1 = rect
    xs = [x0 + (x1 - x0) * (i + 0.5) / n for i in range(n)]
    ys = [y0 + (y1 - y0) * (j + 0.5) / n for j in range(n)]
    return [(x, y) for y in ys for x in xs]

def ctr(b):
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)

def box_dist(a, b):
    """Gap between two bboxes; 0 if they touch or overlap."""
    dx = max(0.0, max(a[0] - b[2], b[0] - a[2]))
    dy = max(0.0, max(a[1] - b[3], b[1] - a[3]))
    return math.hypot(dx, dy)

def pt_box_dist(p, b):
    dx = max(b[0] - p[0], 0.0, p[0] - b[2])
    dy = max(b[1] - p[1], 0.0, p[1] - b[3])
    return math.hypot(dx, dy)

def pt_seg_dist(p, a, b):
    vx, vy = b[0] - a[0], b[1] - a[1]
    L = vx * vx + vy * vy
    if L <= 0:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * vx + (p[1] - a[1]) * vy) / L))
    return math.hypot(p[0] - a[0] - t * vx, p[1] - a[1] - t * vy)

def _ccw(a, b, c):
    return (c[1] - a[1]) * (b[0] - a[0]) > (b[1] - a[1]) * (c[0] - a[0])

def seg_cross(a, b, c, d):
    return _ccw(a, c, d) != _ccw(b, c, d) and _ccw(a, b, c) != _ccw(a, b, d)

def seg_box_dist(a, b, box):
    """Distance from segment a-b to a bbox; 0 if the segment touches or enters it."""
    x0, y0, x1, y1 = box
    for p in (a, b):
        if x0 <= p[0] <= x1 and y0 <= p[1] <= y1:
            return 0.0
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    for i in range(4):
        if seg_cross(a, b, corners[i], corners[(i + 1) % 4]):
            return 0.0
    return min([pt_box_dist(a, box), pt_box_dist(b, box)] +
               [pt_seg_dist(c, a, b) for c in corners])

def arc_pts(start, mid, end, n=8):
    """Flatten a KiCad 3-point arc into n chords. Falls back to the three given
    points if they are collinear (a degenerate arc KiCad still accepts)."""
    (x1, y1), (x2, y2), (x3, y3) = start, mid, end
    d = 2 * (x1 * (y2 - y3) + x2 * (y3 - y1) + x3 * (y1 - y2))
    if abs(d) < 1e-9:
        return [start, mid, end]
    ux = ((x1**2 + y1**2) * (y2 - y3) + (x2**2 + y2**2) * (y3 - y1) +
          (x3**2 + y3**2) * (y1 - y2)) / d
    uy = ((x1**2 + y1**2) * (x3 - x2) + (x2**2 + y2**2) * (x1 - x3) +
          (x3**2 + y3**2) * (x2 - x1)) / d
    r = math.hypot(x1 - ux, y1 - uy)
    a1 = math.atan2(y1 - uy, x1 - ux)
    a2 = math.atan2(y2 - uy, x2 - ux)
    a3 = math.atan2(y3 - uy, x3 - ux)
    sweep = (a3 - a1) % (2 * math.pi)
    if not ((a2 - a1) % (2 * math.pi)) <= sweep:
        sweep -= 2 * math.pi
    return [(ux + r * math.cos(a1 + sweep * i / n),
             uy + r * math.sin(a1 + sweep * i / n)) for i in range(n + 1)]

def rings(segs, tol=0.02):
    """Chain segments into closed rings. Board outlines are drawn as loose
    lines and arcs in no particular order, so they get walked end-to-end here;
    anything that will not close is dropped rather than guessed at."""
    pool = [list(s) for s in segs if s and len(s) >= 2]
    out = []
    while pool:
        cur = pool.pop(0)
        moved = True
        while moved and (abs(cur[0][0] - cur[-1][0]) > tol or abs(cur[0][1] - cur[-1][1]) > tol):
            moved = False
            for i, s in enumerate(pool):
                for a, b in ((s[0], s[-1]), (s[-1], s[0])):
                    if abs(a[0] - cur[-1][0]) <= tol and abs(a[1] - cur[-1][1]) <= tol:
                        cur += (s if a is s[0] else s[::-1])[1:]
                        pool.pop(i); moved = True; break
                if moved:
                    break
        if len(cur) >= 4 and abs(cur[0][0] - cur[-1][0]) <= tol and abs(cur[0][1] - cur[-1][1]) <= tol:
            out.append(cur)
    return out

def inside(p, rgs):
    """Even-odd point-in-polygon over every ring, so a milled cutout (its own
    ring) correctly reads as outside the board."""
    c = False
    for ring in rgs:
        for i in range(len(ring) - 1):
            (xa, ya), (xb, yb) = ring[i], ring[i + 1]
            if (ya > p[1]) != (yb > p[1]) and \
               p[0] < (xb - xa) * (p[1] - ya) / (yb - ya + 1e-12) + xa:
                c = not c
    return c

# ---------------- ampacity (IPC-2221) ----------------

MIL = 0.0254        # mm per mil
RHO_CU = 1.72e-5    # copper resistivity, ohm*mm (R = RHO*L_mm / A_mm2)
POUR_MIN = 50.0     # a net's filled area (mm2) above this on a layer = real plane

def _f(s, d=0.0):
    try:
        return float(s)
    except (TypeError, ValueError):
        return d

def ipc_current(width_mm, thick_mm, external, dt):
    """IPC-2221 steady-state current limit (A) for a bare copper trace at a
    temperature rise of dt degC: I = k*dt^0.44*A^0.725, A in mils^2,
    k=0.048 outer layer / 0.024 inner (inner buries the heat, so ~half)."""
    if width_mm <= 0 or thick_mm <= 0:
        return 0.0
    a_mils2 = (width_mm / MIL) * (thick_mm / MIL)
    return (0.048 if external else 0.024) * (dt ** 0.44) * (a_mils2 ** 0.725)

def via_current(drill_mm, plating_mm, dt):
    """Plated barrel as an internal strip: width = pi*drill, thickness = plating."""
    return ipc_current(math.pi * drill_mm, plating_mm, False, dt)

def ipc_width(need_a, thick_mm, external, dt):
    """Invert IPC-2221: the trace width (mm) needed to carry need_a on a layer
    of this thickness, so a TRACE-THIN warning can say how wide to make it."""
    if need_a <= 0 or thick_mm <= 0:
        return 0.0
    a_mils2 = (need_a / ((0.048 if external else 0.024) * dt ** 0.44)) ** (1 / 0.725)
    return (a_mils2 / (thick_mm / MIL)) * MIL

# ---------------- index build ----------------

class FP:
    __slots__ = ('ref', 'value', 'fp', 'layer', 'x', 'y', 'rot', 'sheet', 'attr',
                 'dnp', 'pads', 'crtyd', 'crtyd_real', 'body', 'placed', '_edge', 'models')

    @property
    def edge(self):
        """Courtyard-to-outline distance (None: no outline), computed on first use:
        ~0.15 s for every footprint on a real board, and most commands never ask."""
        if callable(self._edge):
            self._edge = self._edge()
        return self._edge

    @property
    def back(self):
        return self.layer.startswith('B.')

    @property
    def area(self):
        b = self.crtyd
        return (b[2] - b[0]) * (b[3] - b[1])


class Board:
    def __init__(self, path):
        if not os.path.exists(path):
            raise FileNotFoundError(path)
        root = load_sexp(path)
        if not (isinstance(root, list) and root and root[0] == 'kicad_pcb'):
            raise ValueError(f"{path} is not a .kicad_pcb (got {root[0] if root else 'nothing'})")
        self.path = path
        self.gen = val(root, 'generator') + ' ' + val(root, 'generator_version')
        lay = kids(root, 'layers')
        self.copper = [k[1] for k in (lay[0][1:] if lay else [])
                       if isinstance(k, list) and len(k) > 1 and str(k[1]).endswith('.Cu')]
        self.fps, self.nets = {}, defaultdict(list)
        self.zones = []
        self.fills = []                         # cached zone fills, per layer
        edge = []

        self.teardrops = 0
        for z in kids(root, 'zone'):
            net = val(z, 'net')
            if kid(kid(z, 'attr') or [], 'teardrop'):     # pad/track fillet, not a pour
                self.teardrops += 1
                continue
            lays = kid(z, 'layers') or kid(z, 'layer') or []
            self.zones.append((net, [l for l in lays[1:] if isinstance(l, str)]))
            for fp in kids(z, 'filled_polygon'):
                pts = [(float(p[1]), float(p[2])) for p in (kid(fp, 'pts') or [])[1:]
                       if isinstance(p, list) and p and p[0] == 'xy']
                if len(pts) >= 3:
                    self.fills.append({'net': net, 'layer': val(fp, 'layer'),
                                       'pts': pts, 'area': poly_area(pts),
                                       'bbox': bbox(pts)})

        # copper thickness per layer from the stackup (mm); outer = first+last Cu
        self.thick = {}
        self.stack = []                         # [(name, type, thickness mm, er)] top->bottom
        stk = kid(kid(root, 'setup') or [], 'stackup')
        for ly in kids(stk or [], 'layer'):
            if len(ly) > 1 and isinstance(ly[1], str):
                self.stack.append((ly[1], val(ly, 'type'), _f(val(ly, 'thickness')),
                                   _f(val(ly, 'epsilon_r'))))
            if val(ly, 'type') == 'copper' and len(ly) > 1 and isinstance(ly[1], str):
                self.thick[ly[1]] = _f(val(ly, 'thickness'))
        self.outer = {self.copper[0], self.copper[-1]} if self.copper else set()

        # routed tracks (segments + arcs) and vias, carrying their own net names
        self.tracks, self.vias = [], []
        for s in kids(root, 'segment'):
            p0, p1 = self._xy(s, 'start'), self._xy(s, 'end')
            self.tracks.append({'net': val(s, 'net'), 'layer': val(s, 'layer'),
                                'w': _f(val(s, 'width')), 'len': math.dist(p0, p1),
                                'a': p0, 'b': p1,
                                'mid': ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)})
        for s in kids(root, 'arc'):
            p0, pm, p1 = self._xy(s, 'start'), self._xy(s, 'mid'), self._xy(s, 'end')
            pts = arc_pts(p0, pm, p1, 24)
            self.tracks.append({'net': val(s, 'net'), 'layer': val(s, 'layer'),
                                'w': _f(val(s, 'width')), 'mid': pm, 'a': p0, 'b': p1,
                                'len': sum(math.dist(pts[i], pts[i + 1])
                                           for i in range(len(pts) - 1))})
        for v in kids(root, 'via'):
            vx, vy = self._xy(v, 'at')
            lys = kid(v, 'layers') or []
            self.vias.append({'net': val(v, 'net'), 'drill': _f(val(v, 'drill')),
                              'x': vx, 'y': vy,
                              'layers': [l for l in lys[1:] if isinstance(l, str)]})

        edge += self._graphics(root, None, 'gr_')
        for node in kids(root, 'footprint'):
            f = self._footprint(node)
            if f.ref in self.fps:                 # KiCad allows it; make it visible
                f.ref = f"{f.ref}~dup"
            self.fps[f.ref] = f
            edge += self._graphics(node, f, 'fp_')

        self.edge_segs = [(s[i], s[i + 1]) for s in edge for i in range(len(s) - 1)]
        self.rings = rings(edge)
        self.outline = bbox([p for s in edge for p in s]) if edge else None
        for f in self.fps.values():
            f.placed = self._placed(f)
            f._edge = (lambda f=f: self._edge_dist(f)) if self.edge_segs else None

    # -- parsing helpers -------------------------------------------------
    def _graphics(self, node, f, pfx):
        """Edge.Cuts polylines from a container, in board coordinates. Footprint
        graphics count too - a milled slot often lives inside a footprint."""
        ox, oy, rot = (f.x, f.y, f.rot) if f else (0.0, 0.0, 0.0)
        out = []
        def T(p):
            return xf(p[0], p[1], ox, oy, rot)
        for tag in ('line', 'arc', 'rect', 'poly', 'circle'):
            for g in kids(node, pfx + tag):
                if val(g, 'layer') != 'Edge.Cuts':
                    continue
                if tag == 'line':
                    out.append([T(self._xy(g, 'start')), T(self._xy(g, 'end'))])
                elif tag == 'rect':
                    (x0, y0), (x1, y1) = self._xy(g, 'start'), self._xy(g, 'end')
                    r = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
                    out.append([T(p) for p in r])
                elif tag == 'arc':
                    out.append([T(p) for p in arc_pts(self._xy(g, 'start'),
                                                      self._xy(g, 'mid'),
                                                      self._xy(g, 'end'))])
                elif tag == 'circle':
                    c, e = self._xy(g, 'center'), self._xy(g, 'end')
                    r = math.hypot(e[0] - c[0], e[1] - c[1])
                    out.append([T((c[0] + r * math.cos(a * math.pi / 8),
                                   c[1] + r * math.sin(a * math.pi / 8))) for a in range(17)])
                elif tag == 'poly':
                    pts = self._pts(g)
                    if pts:
                        out.append([T(p) for p in pts + [pts[0]]])
        return out

    @staticmethod
    def _pad_prims(p):
        """A custom pad's (primitives ...) as polygons in the pad-local frame
        (before the pad's own rotation). Its `size` is only the anchor, often far
        smaller than the copper (BQ25798 pads: 0.15 mm anchor, 0.65 mm poly)."""
        out = []
        for g in (kid(p, 'primitives') or [])[1:]:
            if not isinstance(g, list) or not g:
                continue
            w = _f(val(g, 'width')) / 2
            if g[0] == 'gr_poly':
                out.append(Board._pts(g))
            elif g[0] == 'gr_rect':
                (x0, y0), (x1, y1) = Board._xy(g, 'start'), Board._xy(g, 'end')
                out.append([(x0 - w, y0 - w), (x1 + w, y0 - w), (x1 + w, y1 + w), (x0 - w, y1 + w)])
            elif g[0] == 'gr_circle':               # circumscribed 16-gon: never under-states
                (cx, cy), e = Board._xy(g, 'center'), Board._xy(g, 'end')
                r = (math.hypot(e[0] - cx, e[1] - cy) + w) / math.cos(math.pi / 16)
                out.append([(cx + r * math.cos(k * math.pi / 8), cy + r * math.sin(k * math.pi / 8))
                            for k in range(16)])
            elif g[0] == 'gr_line':                 # stroke as its bounding box
                (x0, y0), (x1, y1) = Board._xy(g, 'start'), Board._xy(g, 'end')
                out.append([(min(x0, x1) - w, min(y0, y1) - w), (max(x0, x1) + w, min(y0, y1) - w),
                            (max(x0, x1) + w, max(y0, y1) + w), (min(x0, x1) - w, max(y0, y1) + w)])
        return [q for q in out if len(q) >= 3]

    @staticmethod
    def _xy(node, tag):
        k = kid(node, tag)
        return (float(k[1]), float(k[2])) if k and len(k) >= 3 else (0.0, 0.0)

    @staticmethod
    def _pts(node):
        p = kid(node, 'pts')
        return [(float(c[1]), float(c[2])) for c in kids(p, 'xy')] if p else []

    def _footprint(self, node):
        f = FP()
        f.fp = node[1] if len(node) > 1 and isinstance(node[1], str) else '?'
        # KiCad <= 10.0 writes (at X Y R); 10.99 nightly writes
        # (transform (translate X Y) (rotate R) (scale 1 1)). Reading only (at)
        # parks every nightly footprint at 0,0, i.e. "UNPLACED".
        tr = kid(node, 'transform')
        at = kid(node, 'at')
        if tr:
            t, r, sc = kid(tr, 'translate') or [], kid(tr, 'rotate') or [], kid(tr, 'scale')
            at = ['at', *(t[1:3] or ['0', '0']), *(r[1:2] or ['0'])]
            if sc and [_f(x, 1) for x in sc[1:3]] != [1.0, 1.0]:
                print(f"kpcb: footprint scale {sc[1:3]} is not modelled, geometry of "
                      f"this part is wrong", file=sys.stderr)
        f.x, f.y = (float(at[1]), float(at[2])) if at else (0.0, 0.0)
        f.rot = float(at[3]) if at and len(at) > 3 else 0.0
        f.layer = val(node, 'layer', 'F.Cu')
        f.models = [m[1] for m in kids(node, 'model') if len(m) > 1 and not has(m, 'hide')]
        f.sheet = val(node, 'sheetname', '')
        a = kid(node, 'attr') or []
        f.attr = set(x for x in a[1:] if isinstance(x, str))
        f.dnp = 'dnp' in f.attr
        f.ref, f.value = '?', ''
        for p in kids(node, 'property'):
            if len(p) > 2 and p[1] == 'Reference':
                f.ref = p[2]
            elif len(p) > 2 and p[1] == 'Value':
                f.value = p[2]

        f.pads = []
        pad_pts, all_pts, crt_pts = [], [], []
        for p in kids(node, 'pad'):
            num = p[1] if len(p) > 1 else '?'
            pat = kid(p, 'at')
            lx, ly = (float(pat[1]), float(pat[2])) if pat else (0.0, 0.0)
            prot = float(pat[3]) if pat and len(pat) > 3 else 0.0
            sz = kid(p, 'size')
            sx, sy = (float(sz[1]), float(sz[2])) if sz and len(sz) > 2 else (0.0, 0.0)
            bx, by = xf(lx, ly, f.x, f.y, f.rot)
            prims = self._pad_prims(p)          # custom pad copper, pad-local frame
            # pad AABB straight in board coords: a .kicad_pcb stores `prot` as the
            # ABSOLUTE board angle (f.rot already baked in), so envelope around the
            # board-frame centre with prot alone. Rotating a local AABB through xf
            # (adding f.rot again) double-counts and swaps the axes on a rotated,
            # non-square pad.
            t = math.radians(prot)
            hx = abs(sx / 2 * math.cos(t)) + abs(sy / 2 * math.sin(t))
            hy = abs(sx / 2 * math.sin(t)) + abs(sy / 2 * math.cos(t))
            for c in ((bx - hx, by - hy), (bx + hx, by - hy),
                      (bx + hx, by + hy), (bx - hx, by + hy)):
                pad_pts.append(c)
            pad_pts += [xf(u, v, bx, by, prot) for poly in prims for u, v in poly]
            fn = re.sub(r'_\d+$', '', val(p, 'pinfunction'))
            net = val(p, 'net')
            lays = kid(p, 'layers') or []
            d = kid(p, 'drill') or []
            # (drill 0.8) or (drill oval 1.0 2.0) - take the widest number given
            dn = [float(t) for t in d[1:] if isinstance(t, str)
                  and re.match(r'^[\d.]+$', t)]
            f.pads.append({'num': num, 'net': net, 'x': bx, 'y': by, 'fn': fn,
                           'type': val(p, 'pintype'), 'w': max(sx, sy),
                           'sx': sx, 'sy': sy, 'prot': prot,
                           'shape': p[3] if len(p) > 3 and isinstance(p[3], str) else '',
                           'kind': p[2] if len(p) > 2 else '',
                           'drill': max(dn) if dn else 0.0, 'prims': prims,
                           'layers': [l for l in lays[1:] if isinstance(l, str)]})
            if net:
                self.nets[net].append((f.ref, num))
        # courtyard: the only outline KiCad guarantees is a keepout envelope
        for tag in ('fp_line', 'fp_rect', 'fp_arc', 'fp_poly', 'fp_circle'):
            for g in kids(node, tag):
                lay = val(g, 'layer')
                pts = []
                if lay.endswith('.CrtYd') or lay.endswith('.Fab'):
                    if tag in ('fp_line', 'fp_rect'):
                        pts = [self._xy(g, 'start'), self._xy(g, 'end')]
                    elif tag == 'fp_arc':
                        pts = [self._xy(g, 'start'), self._xy(g, 'mid'), self._xy(g, 'end')]
                    elif tag == 'fp_poly':
                        pts = self._pts(g)
                    elif tag == 'fp_circle':
                        c, e = self._xy(g, 'center'), self._xy(g, 'end')
                        r = math.hypot(e[0] - c[0], e[1] - c[1])
                        pts = [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
                pts = [xf(p[0], p[1], f.x, f.y, f.rot) for p in pts]
                all_pts += pts
                if lay.endswith('.CrtYd'):
                    crt_pts += pts
        f.body = bbox(pad_pts + all_pts) if (pad_pts or all_pts) else (f.x, f.y, f.x, f.y)
        f.crtyd = bbox(crt_pts) if crt_pts else f.body
        f.crtyd_real = bool(crt_pts)
        return f

    # -- derived ---------------------------------------------------------
    def _placed(self, f):
        """A part is placed if its centre is inside the outline. At the placement
        stage most of the BOM is still parked in a pile beside the board, and
        every other check has to ignore that pile or it drowns the real
        findings."""
        if not self.rings:
            return True if not self.outline else \
                self.outline[0] <= f.x <= self.outline[2] and self.outline[1] <= f.y <= self.outline[3]
        return inside((f.x, f.y), self.rings)

    def _edge_dist(self, f):
        """Signed-ish: distance from the courtyard to the outline. 0.0 means the
        courtyard is sitting on the edge or straddling it."""
        return min(seg_box_dist(a, b, f.crtyd) for a, b in self.edge_segs)

    def placed(self):
        return [f for f in self.fps.values() if f.placed]

    def netclass(self, net):
        """Netclass of a net from the .kicad_pro beside the board (explicit
        assignment, else the first matching wildcard pattern), '' if none."""
        if not hasattr(self, '_nc'):
            self._nc = ({}, [])
            for pro in glob.glob(os.path.join(os.path.dirname(os.path.abspath(self.path)), '*.kicad_pro')):
                try:
                    ns = json.load(open(pro)).get('net_settings', {})
                except (OSError, ValueError):
                    continue
                self._nc = (ns.get('netclass_assignments') or {},
                            [(p.get('pattern', ''), p.get('netclass', ''))
                             for p in ns.get('netclass_patterns') or []])
                break
        direct, pats = self._nc
        if net in direct:
            c = direct[net]
            return c[0] if isinstance(c, list) and c else str(c)
        import fnmatch
        return next((c for p, c in pats if fnmatch.fnmatchcase(net, p)), '')

    def net_fps(self, net):
        return [self.fps[r] for r, _ in self.nets.get(net, []) if r in self.fps]

    def is_hole(self, f):
        return bool(HOLE_FP.search(f.fp)) or \
               (f.pads and all(p['kind'] == 'np_thru_hole' for p in f.pads))

    def is_conn(self, f):
        return prefix(f.ref) in ('J', 'P', 'CN', 'X') or bool(CONN_FP.match(f.fp.split(':')[0]))

    def is_rf(self, f):
        if RF_FP.search(f.fp) or RF_VAL.search(f.value):
            return True
        return any(RF_NET.search(p['net'].split('/')[-1]) for p in f.pads if p['net'])

    @staticmethod
    def is_power_l(f):
        """A buck inductor, not an RF matching inductor. Both are `L`, so value
        alone is not enough: a 470nH 0603 in an antenna match is not a switching
        node. Size is what actually separates them."""
        return (prefix(f.ref) == 'L' and (parse_value(f.value, 'L') or 0) >= 1e-6
                and f.area >= 6.0)

    def sw_nets(self):
        """Switching nodes: any pad whose function names one, plus both ends of
        a power inductor. Name-matched, not type-matched - most of this board's
        symbols type every pin `passive`. Named power rails are excluded: the
        quiet side of a buck inductor is the output rail, not a switching node,
        and leaving it in makes every part on +3V3 look noisy."""
        out = set()
        for f in self.fps.values():
            for p in f.pads:
                if SW_PIN.match(p['fn'] or '') and p['net']:
                    out.add(p['net'])
        for f in self.fps.values():
            # Fallback for a switcher whose SW pin is not named: take the power
            # inductor's own nets. Skipped when one end is already a known SW
            # node, because then the other end is the quiet output rail and
            # adding it makes every load on that rail look noisy.
            if self.is_power_l(f) and not any(p['net'] in out for p in f.pads):
                out.update(p['net'] for p in f.pads if p['net'])
        return {n for n in out if rail_voltage(n.split('/')[-1]) is None}

    def is_hot(self, f):
        if HOT_VAL.search(f.value):
            return True
        if self.is_power_l(f):
            return True
        return any(SW_PIN.match(p['fn'] or '') for p in f.pads)

    def is_sens(self, f):
        # a thermistor near a hot thing is the point of the thermistor
        if prefix(f.ref) in ('TH', 'NTC', 'RT'):
            return False
        return bool(SENS_FP.search(f.fp) or SENS_VAL.search(f.value))


# ---------------- commands ----------------

def sheet_of(f):
    return f.sheet or '/'

def c_summary(b, a):
    P = b.placed()
    un = [f for f in b.fps.values() if not f.placed]
    o = b.outline
    w, h = (o[2] - o[0], o[3] - o[1]) if o else (0, 0)
    used = {s: sum(f.area for f in P if f.back == s) for s in (False, True)}
    if a.json:
        print(json.dumps({'path': b.path, 'outline': o, 'w': w, 'h': h,
                          'copper': b.copper, 'footprints': len(b.fps),
                          'placed': len(P), 'unplaced': len(un),
                          'courtyard_mm2': {'front': round(used[False], 1),
                                            'back': round(used[True], 1)},
                          'zones': [{'net': n, 'layers': l} for n, l in b.zones]}, indent=1))
        return
    print(f"board  : {b.path}")
    print(f"gen    : {b.gen.strip()}")
    if o:
        print(f"outline: {w:.2f} x {h:.2f} mm   x {o[0]:.2f}..{o[2]:.2f}  y {o[1]:.2f}..{o[3]:.2f}"
              f"   {len(b.rings)} ring(s), {len(b.edge_segs)} segs")
    else:
        print("outline: NONE on Edge.Cuts - every placement check is disabled")
    print(f"copper : {len(b.copper)} layers  {' '.join(b.copper)}")
    for n, l in b.zones:
        print(f"zone   : {n:<10} {' '.join(l)}")
    if b.teardrops:
        print(f"teardrops: {b.teardrops} (zone-type fillets, left out of every pour figure)")
    front = sum(1 for f in P if not f.back)
    print(f"\nfootprints: {len(b.fps)}   placed {len(P)} ({front} front / {len(P)-front} back)"
          f"   unplaced {len(un)}")
    if o and w * h:
        print(f"courtyard used: front {used[False]:.0f} mm2 ({100*used[False]/(w*h):.0f}%), "
              f"back {used[True]:.0f} mm2 ({100*used[True]/(w*h):.0f}%) "
              f"of the {w*h:.0f} mm2 outline bbox   [sum of courtyards, so >100% "
              f"means they overlap]")
    if o and w * h:
        left = sum(f.area for f in un)
        free = 2 * w * h - used[False] - used[True]      # two sides, not one
        print(f"still to place: {left:.0f} mm2 of courtyard across {len(un)} part(s) "
              f"vs {free:.0f} mm2 free over two sides "
              f"[{'fits with room' if free > left * 2 else 'tight' if free > left else 'DOES NOT FIT as-is'};"
              f" crude - ignores routing space, keepouts and part height]")
    bys = defaultdict(lambda: [0, 0])
    for f in b.fps.values():
        bys[sheet_of(f)][0 if f.placed else 1] += 1
    print(f"\n{'sheet':<20} {'placed':>6} {'left':>6}")
    for s in sorted(bys, key=natkey):
        p, u = bys[s]
        print(f"{trunc(s,20):<20} {p:>6} {u:>6}")
    dnp = sorted([f.ref for f in b.fps.values() if f.dnp], key=natkey)
    print(f"\nDNP ({len(dnp)}): {refrange(dnp) or 'none'}")
    holes = sorted([f.ref for f in b.fps.values() if b.is_hole(f)], key=natkey)
    print(f"mounting holes ({len(holes)}): {refrange(holes) or 'none'}")
    print("\nlargest placed parts:")
    for f in sorted(P, key=lambda f: -f.area)[:8]:
        print(f"  {f.area:>7.1f} mm2  {f.ref:<6} {trunc(f.value,22):<22} "
              f"{f.x:>7.2f},{f.y:<7.2f} {'B' if f.back else 'F'}")

def c_unplaced(b, a):
    un = [f for f in b.fps.values() if not f.placed]
    if not un:
        print("everything is inside the outline"); return
    bys = defaultdict(list)
    for f in un:
        bys[sheet_of(f)].append(f.ref)
    print(f"{len(un)} footprint(s) still parked off the board:\n")
    for s in sorted(bys, key=natkey):
        print(f"  {trunc(s,20):<20} {len(bys[s]):>3}  {refrange(bys[s])}")
    big = sorted([f for f in un if f.area > 8], key=lambda f: -f.area)[:10]
    if big:
        print("\nlargest still unplaced (place these first - they set the floorplan):")
        for f in big:
            print(f"  {f.area:>7.1f} mm2  {f.ref:<6} {trunc(f.value,26):<26} {trunc(f.fp,34)}")

def c_sheet(b, a):
    """Placement cohesion per schematic sheet. A sheet whose parts are scattered
    across the board is usually a floorplan mistake, not a routing one."""
    want = a.args[0] if a.args else None
    bys = defaultdict(list)
    for f in b.fps.values():
        if want and not sheet_of(f).lower().startswith(want.lower()):
            continue
        bys[sheet_of(f)].append(f)
    if not bys:
        print(f"no sheet matches {want!r}"); return 1
    print(f"{'sheet':<18} {'plc':>4} {'left':>4} {'bbox (x0,y0 x1,y1)':<30} {'spread':>7}  outliers")
    for s in sorted(bys, key=natkey):
        fl = bys[s]
        P = [f for f in fl if f.placed]
        if not P:
            print(f"{trunc(s,18):<18} {0:>4} {len(fl):>4} {'-':<30} {'-':>7}")
            continue
        bb = bbox([(f.x, f.y) for f in P])
        c = ctr(bb)
        # keyed: two parts at the same distance used to fall through to
        # comparing the FP objects themselves and crash the whole command
        d = sorted(((math.hypot(f.x - c[0], f.y - c[1]), f) for f in P),
                   key=lambda t: (t[0], natkey(t[1].ref)), reverse=True)
        med = d[len(d) // 2][0]
        out = [f.ref for dist, f in d[:3] if dist > max(3 * med, 15)]
        print(f"{trunc(s,18):<18} {len(P):>4} {len(fl)-len(P):>4} "
              f"{f'{bb[0]:.0f},{bb[1]:.0f} {bb[2]:.0f},{bb[3]:.0f}':<30} "
              f"{max(bb[2]-bb[0], bb[3]-bb[1]):>6.0f}m  {' '.join(out) or '-'}")
    print("\nspread = longer side of the placed bbox; outliers are >3x the median "
          "distance from\nthat sheet's centroid - a part that drifted away from its block.")

def c_where(b, a):
    if not a.args:
        print("where needs a ref or an x,y coordinate", file=sys.stderr); return 1
    miss, pts = False, []
    for spec in a.args:
        m = re.match(r'^(-?[\d.]+)\s*,\s*(-?[\d.]+)$', spec)
        if m:
            p = (float(m.group(1)), float(m.group(2)))
            pts.append((spec, p))
            box = (p[0], p[1], p[0], p[1])
            if b.edge_segs:
                where = 'inside' if inside(p, b.rings) else 'OUTSIDE'
                d = f"   edge {min(pt_seg_dist(p, u, v) for u, v in b.edge_segs):.2f} mm"
            else:
                where, d = 'no Edge.Cuts, so inside/outside is', ''
            print(f"\n=== {p[0]:.2f},{p[1]:.2f}   {where} the outline{d}")
            _neigh(b, box, None, a)
            continue
        pad = _find_pad(b, spec)
        if pad:
            f, p = pad
            pts.append((spec, (p['x'], p['y'])))
            ly = ' '.join(l for l in p['layers'] if l.endswith('.Cu')) or ' '.join(p['layers'])
            print(f"\n=== {spec}  {p['fn'] or ''}  on {unesc_disp(p['net']) or '(no net)'}")
            print(f"  at        : {p['x']:.3f}, {p['y']:.3f}  (absolute)  pad rot {p['prot']:g}")
            print(f"  pad       : {p['kind']} {p['shape']} {p['sx']:.2f} x {p['sy']:.2f} mm"
                  + (f"  drill {p['drill']:.2f}" if p['drill'] else '') + f"  {ly}")
            if b.edge_segs:
                print(f"  edge dist : {min(pt_seg_dist((p['x'], p['y']), u, v) for u, v in b.edge_segs):.2f} mm")
            others = sorted(((math.hypot(q['x'] - p['x'], q['y'] - p['y']), g.ref, q)
                             for g in b.fps.values() for q in g.pads
                             if q is not p and q['net'] and q['net'] == p['net']), key=lambda t: t[0])
            if others:
                print("  same net  : " + ', '.join(f"{r}.{q['num']} {d:.1f} mm" for d, r, q in others[:6])
                      + (f" ... +{len(others)-6}" if len(others) > 6 else ''))
            continue
        f = b.fps.get(spec)
        if not f:
            near = [r for r in b.fps if r.upper().startswith(spec.upper())][:6]
            print(f"{spec}: NOT FOUND" + (f"; did you mean {' '.join(near)}" if near else ""))
            miss = True; continue
        c = f.crtyd
        print(f"\n=== {f.ref}  {f.value}{'   [DNP]' if f.dnp else ''}"
              f"{'' if f.placed else '   [UNPLACED - parked off the outline]'}")
        print(f"  footprint : {f.fp}")
        print(f"  sheet     : {f.sheet}")
        print(f"  at        : {f.x:.3f}, {f.y:.3f}  rot {f.rot:g}  layer {f.layer}")
        print(f"  courtyard : {c[0]:.2f},{c[1]:.2f} .. {c[2]:.2f},{c[3]:.2f}  "
              f"({c[2]-c[0]:.2f} x {c[3]-c[1]:.2f} mm){'' if f.crtyd_real else '  [NO CrtYd - pads+fab bbox]'}")
        if f.edge is not None:
            print(f"  edge dist : {f.edge:.2f} mm" + ("   <-- ON/ACROSS THE EDGE" if f.edge <= 0.001 else ""))
        tags = [t for t, ok in (('RF', b.is_rf(f)), ('hot', b.is_hot(f)),
                                ('heat-sensitive', b.is_sens(f)), ('connector', b.is_conn(f)),
                                ('mounting hole', b.is_hole(f))) if ok]
        if tags:
            print(f"  class     : {', '.join(tags)}")
        nets = sorted({p['net'] for p in f.pads if p['net']}, key=natkey)
        print(f"  nets ({len(nets)}) : {trunc(' '.join(unesc_disp(n) for n in nets), 300)}")
        _neigh(b, c, f, a)
        pts.append((spec, (f.x, f.y)))
    if len(pts) == 2:
        (n1, p1), (n2, p2) = pts
        print(f"\n{n1} -> {n2}: {math.dist(p1, p2):.2f} mm "
              f"(dx {p2[0]-p1[0]:+.2f}, dy {p2[1]-p1[1]:+.2f})")
    return 1 if miss else 0

def _neigh(b, box, self_fp, a):
    """Neighbour list, capped. The whole point of this tool is to answer a
    placement question in a few lines; dumping every part within a radius on a
    440-footprint board would defeat that."""
    r = a.radius
    rows = []
    for g in b.fps.values():
        if g is self_fp or not g.placed:
            continue
        d = box_dist(box, g.crtyd)
        if d <= r:
            rows.append((d, g))
    rows.sort(key=lambda t: (t[0], natkey(t[1].ref)))
    print(f"  neighbours within {r:g} mm ({len(rows)}):")
    for d, g in rows[:a.max]:
        side = 'B' if g.back else 'F'
        # same-side contact is what `check` calls OVERLAP; a cross-side one is
        # just a projection, so label it differently and do not cry wolf
        flag = ''
        if d <= 0 and hit(box, g.crtyd):
            flag = '  OVERLAP' if (self_fp is None or g.back == self_fp.back) \
                   else '  (overlaps, opposite side)'
        print(f"    {d:>6.2f} mm  {g.ref:<6} {side} {trunc(g.value,20):<20} {trunc(g.fp,30)}{flag}")
    if len(rows) > a.max:
        print(f"    ... +{len(rows)-a.max} more within {r:g} mm ({len(rows)} total) "
              f"- raise --max or lower -r")

def c_map(b, a):
    """ASCII occupancy. Cheap floorplan overview: which block owns which corner
    and where the free space is, in ~40 lines instead of a 400-row table.

    One side at a time on purpose. Painting both onto one grid turned most of
    this board into `*` - the back-side cells sit under the front-side module -
    which is exactly the kind of output that looks informative and says
    nothing."""
    if not b.outline:
        print("no board outline"); return 1
    o = b.outline
    cols = max(20, a.cols)
    cw = (o[2] - o[0]) / cols
    ch = cw * 2.0                      # terminal cells are about twice as tall
    nrows = max(4, int(math.ceil((o[3] - o[1]) / ch)))
    want = (a.side or 'f').lower()[0]
    if want not in ('f', 'b'):
        print("--side takes f or b", file=sys.stderr); return 1
    P = [f for f in b.placed() if f.back == (want == 'b')]
    other = len(b.placed()) - len(P)
    keys, used = {}, set()
    for sh in sorted({sheet_of(f) for f in P}, key=natkey):
        stem = sh.split('/')[-2] if sh.count('/') > 1 else sh
        for c in re.sub(r'[^A-Za-z]', '', stem) + 'abcdefghijklmnop':
            if c.upper() not in used:
                keys[sh] = c.upper(); used.add(c.upper()); break
    grid = [[' '] * cols for _ in range(nrows)]
    for f in P:
        k = keys.get(sheet_of(f), '?')
        c = f.crtyd
        for gy in range(max(0, int((c[1] - o[1]) / ch)),
                        min(nrows, int((c[3] - o[1]) / ch) + 1)):
            for gx in range(max(0, int((c[0] - o[0]) / cw)),
                            min(cols, int((c[2] - o[0]) / cw) + 1)):
                cur = grid[gy][gx]
                grid[gy][gx] = k if cur == ' ' else ('*' if cur != k else k)
    for gy in range(nrows):
        for gx in range(cols):
            if grid[gy][gx] == ' ':
                p = (o[0] + (gx + .5) * cw, o[1] + (gy + .5) * ch)
                grid[gy][gx] = '.' if inside(p, b.rings) else ' '
    print(f"{b.path}  {'FRONT' if want == 'f' else 'BACK'} side  "
          f"{o[2]-o[0]:.1f} x {o[3]-o[1]:.1f} mm   1 cell = {cw:.2f} x {ch:.2f} mm   "
          f"x+ right, y+ down")
    print("      +" + "-" * cols + "+")
    for gy, row in enumerate(grid):
        print(f"{o[1]+gy*ch:>6.0f}|" + ''.join(row) + "|")
    print("      +" + "-" * cols + "+")
    print(f"      {o[0]:<.0f}" + " " * max(0, cols - 8) + f"{o[2]:.0f}")
    print("\nkey: " + '  '.join(f"{v}={trunc(k,18)}" for k, v in
                                sorted(keys.items(), key=lambda kv: kv[1])))
    print("     . = inside the outline, free    * = two sheets share the cell    "
          "(blank) = outside")
    print(f"     {other} placed part(s) on the other side are not shown "
          f"(`map --side {'b' if want == 'f' else 'f'}`); "
          f"{len(b.fps)-len(b.placed())} unplaced (`unplaced`)")

# ---------------- IC placement helper ----------------
# `ic REF` answers "where do this part's passives go", not just "what is wrong".
# Everything below is computed from the real pad coordinates in the board file -
# there is no per-part template, so a part this tool has never seen still works
# as long as its pins are named.

ROLE_PAT = (
    ('SW',   r'SW|LX|PH|VSW|SWITCH|VLX'),        # before VIN: VSW is not an input
    ('BOOT', r'C?BOOT|BST|BTST|VBOOT|RBOOT'),
    ('VIN',  r'VIN|PVIN|VBUS|VCCIN|IN|AVIN|VDDIN'),
    ('OUT',  r'VOUT|OUT|SYS|VSYS'),
    ('FB',   r'FB|VFB|FBK|VSENSE|ADJ'),
    ('GND',  r'PGND|GND|AGND|DGND|VSS|EP|EPAD|PAD|THERMAL'),
    ('BIAS', r'VCC|BIAS|VDD|VREG|REGN|VDDA|AVDD'),
)
# anchored, so `~{INT}` never matches IN and PGOOD never matches GND
ROLE_RE = [(r, re.compile(r'^(' + p + r')\d*$', re.I)) for r, p in ROLE_PAT]
PASSIVE_PFX = ('C', 'R', 'L', 'FB', 'D')

def unit(v):
    n = math.hypot(v[0], v[1])
    return (v[0] / n, v[1] / n) if n > 1e-9 else (1.0, 0.0)

def ray_exit(p, d, box):
    """How far along +d from p until we leave `box`. 0 if p is already out."""
    t = 0.0
    for i in (0, 1):
        if abs(d[i]) < 1e-9:
            continue
        lim = box[i + 2] if d[i] > 0 else box[i]
        t = max(t, (lim - p[i]) / d[i])
    return max(0.0, t)

def axis_snap(v):
    """Snap a direction to the nearest axis. Placement is orthogonal in practice
    and a 3-degree tilt in a recommendation is noise, not information."""
    return (math.copysign(1.0, v[0]), 0.0) if abs(v[0]) >= abs(v[1]) else (0.0, math.copysign(1.0, v[1]))

def fp_axis(f):
    """(pad1->pad2 board angle, length, width) for a two-pad part, else None.
    The angle is what a recommended rotation is computed against, so it comes
    from the pads themselves and never from an assumed footprint convention."""
    ps = [p for p in f.pads if p['num'] in ('1', '2')] or f.pads[:2]
    if len(ps) < 2:
        return None
    a, bp = ps[0], ps[1]
    ang = math.degrees(math.atan2(-(bp['y'] - a['y']), bp['x'] - a['x'])) % 360
    c = f.crtyd
    w, h = c[2] - c[0], c[3] - c[1]
    return (ang, max(w, h), min(w, h))

def want_rot(f, target_deg):
    """Rotation that turns f's pad1->pad2 axis to `target_deg` (board frame,
    +y down). Derived from where the pads actually sit now plus the footprint's
    current rotation, so it is right for any footprint orientation convention."""
    ax = fp_axis(f)
    if not ax:
        return round(f.rot, 1)
    # Snapped to 90 deg: a pin pair on a diagonal would otherwise ask for a
    # 165.3 deg part. The loop cost of the few degrees is nil, and nobody
    # hand-places at 165.3 deg.
    return round((ax[0] + f.rot - target_deg) / 90.0) % 4 * 90.0

def role_pads(f):
    by = defaultdict(list)
    for p in f.pads:
        fn = p['fn'] or ''
        for role, rx in ROLE_RE:
            if rx.match(fn):
                by[role].append(p); break
        else:
            # easyeda2kicad leaves pinfunction blank on plenty of parts; a blank
            # pin sitting on GND is still a ground pin
            if not fn and p['net'] and GND_RE.match(p['net'].split('/')[-1]):
                by['GND'].append(p)
    return by

def two_pin_on(b, net, other=None, pfx=PASSIVE_PFX):
    """Two-pin parts bridging `net` and (optionally) a net matching `other`."""
    out = []
    for r, _ in b.nets.get(net, []):
        f = b.fps.get(r)
        if not f or f in out or prefix(f.ref) not in pfx:
            continue
        nets = [p['net'] for p in f.pads if p['net']]
        if len(set(nets)) != 2:
            continue
        far = [n for n in nets if n != net]
        if not far:
            continue
        if other == 'GND' and not GND_RE.match(far[0].split('/')[-1]):
            continue
        if other not in (None, 'GND') and far[0] != other:
            continue
        out.append(f)
    return out

def rail_pick(b, ic, cands, want, kind, net):
    """Pick which of a rail's many caps belong to THIS regulator.

    A rail net carries every bypass cap on the board - VSYS here has 20 - so
    taking them all would be the `walk`-dumps-the-whole-rail mistake. The board
    file does carry one hard fact: where each cap physically sits. Rank by
    distance from the cap to the pad this cap serves on THIS regulator (VIN for
    CIN, the inductor/output node for COUT), so a same-value cap that really
    belongs to another regulator on the shared rail - sitting centimetres away -
    can't steal the slot. Sheet and refdes only break ties between caps that are
    equally close. Still say the pick is inferred: nothing in a .kicad_pcb proves
    which cap the schematic drew next to which pin."""
    if not cands:
        return [], ''
    # anchor = pad(s) on this net belonging to the regulator itself, else the
    # inductor (a buck's output net lives at the inductor, not on an IC pin).
    anc = [p for p in ic['fp'].pads if p.get('net') == net]
    if not anc and ic.get('L'):
        anc = [p for p in ic['L'].pads if p.get('net') == net]
    ax = sum(p['x'] for p in anc) / len(anc) if anc else ic['fp'].x
    ay = sum(p['y'] for p in anc) / len(anc) if anc else ic['fp'].y
    hint = [int(re.sub(r'\D', '', r) or 0) for r in ic['own_refs'] if prefix(r) == 'C']
    mid = sorted(hint)[len(hint) // 2] if hint else None
    def key(f):
        d = round(math.hypot(f.x - ax, f.y - ay), 1)   # 0.1 mm buckets
        n = int(re.sub(r'\D', '', f.ref) or 0)
        return (d, f.sheet != ic['fp'].sheet, abs(n - mid) if mid else 0, natkey(f.ref))
    ranked = sorted(cands, key=key)
    # one of each value first. "Smallest cap closest to the pin" only means
    # anything if the set actually holds a 100n, a 1u and a 10u rather than
    # three 1u that happened to sit next to each other in the refdes run.
    first, extra = {}, []
    for g in ranked:
        v = parse_value(g.value, 'C')
        extra.append(g) if v in first else first.setdefault(v, g)
    ranked = list(first.values()) + extra
    note = ''
    if len(cands) > want:
        pin = 'VIN' if kind == 'CIN' else 'output'
        note = (f"{len(cands)} caps sit on this rail; the {min(want,len(ranked))} physically "
                f"closest to {ic['fp'].ref}'s {pin} pad were taken as {kind}. "
                f"Override with --{kind.lower()} REF,REF if the schematic says otherwise.")
    return ranked[:want], note

def ic_context(b, f, a):
    """Everything the recommendation needs, gathered once."""
    ic = {'fp': f, 'pads': role_pads(f), 'warn': [], 'own_refs': [], 'assoc': {}}
    R = ic['pads']
    nets_of = lambda role: [p['net'] for p in R.get(role, []) if p['net']]

    # parts on this IC's own private nets: unambiguous, no rail guessing needed
    for p in f.pads:
        n = p['net']
        if not n or len(b.nets.get(n, [])) > a.assoc:
            continue
        if GND_RE.match(n.split('/')[-1]) or rail_voltage(n.split('/')[-1]) is not None:
            continue
        for g in two_pin_on(b, n):
            if g.ref != f.ref:
                ic['own_refs'].append(g.ref)
    ic['own_refs'] = sorted(set(ic['own_refs']), key=natkey)

    sw = set(nets_of('SW')) or {p['net'] for p in f.pads if p['net'] in b.sw_nets()}
    ic['sw_nets'] = sorted(sw)
    if not R.get('VIN'):
        # a plain IC names its supply +3V3 or VDD_IO, not VIN. Falling back to
        # the same SUP_PIN table the BYPASS rule uses turns `ic` into a bypass
        # placer for any part, which is the same loop rule at a smaller scale.
        R['VIN'] = [p for p in f.pads if p['net'] and SUP_PIN.match(p['fn'] or '')
                    and not GND_RE.match(p['net'].split('/')[-1])]
    ic['vin_nets'] = sorted(set(nets_of('VIN')))
    ic['out_nets'] = sorted(set(nets_of('OUT')))

    # the inductor decides the topology, so find it before naming anything
    ind = [g for n in sw for g in two_pin_on(b, n, pfx=('L',))]
    ind = sorted({g.ref: g for g in ind}.values(), key=lambda g: natkey(g.ref))
    ic['L'] = ind[0] if ind else None
    vout = None
    if ic['L']:
        far = [p['net'] for p in ic['L'].pads if p['net'] and p['net'] not in sw]
        if far:
            vout = far[0]
        else:
            # both ends switch: a 4-switch buck-boost. The output is the OUT pin.
            ic['topo'] = 'buck-boost (inductor between two switching nodes)'
            vout = ic['out_nets'][0] if ic['out_nets'] else None
    ic['vout'] = vout or (ic['out_nets'][0] if ic['out_nets'] else None)

    vin_v = next((rail_voltage(n.split('/')[-1]) for n in ic['vin_nets']), None)
    out_v = rail_voltage(ic['vout'].split('/')[-1]) if ic['vout'] else None
    if 'topo' not in ic:
        if not sw:
            ic['topo'] = 'linear / load switch (no switching node)' if ic['out_nets'] \
                         else 'not a regulator shape - generic bypass placement only'
        elif vin_v and out_v:
            ic['topo'] = 'buck' if out_v < vin_v else 'boost'
        else:
            ic['topo'] = 'switching (buck assumed; rail voltages unknown)'

    ov = {k: [x.strip() for x in v.split(',') if x.strip()]
          for k, v in (('CIN', a.cin), ('COUT', a.cout)) if v}
    def take(kind, net, want):
        if kind in ov:
            got = [b.fps[r] for r in ov[kind] if r in b.fps]
            miss = [r for r in ov[kind] if r not in b.fps]
            if miss:
                ic['warn'].append(f"--{kind.lower()}: no such footprint: {' '.join(miss)}")
            return got, ''
        if not net:
            return [], ''
        c = two_pin_on(b, net, 'GND', pfx=('C',))
        if len(b.nets.get(net, [])) <= a.assoc:
            return c, ''
        return rail_pick(b, ic, c, want, kind, net)

    ic['CIN'], n1 = take('CIN', ic['vin_nets'][0] if ic['vin_nets'] else None, a.ncin)
    ic['COUT'], n2 = take('COUT', ic['vout'], a.ncout)
    ic['notes'] = [n for n in (n1, n2) if n]
    ic['CBOOT'] = [g for n in nets_of('BOOT') for g in two_pin_on(b, n, pfx=('C',))]
    ic['CBIAS'] = [g for n in nets_of('BIAS') for g in two_pin_on(b, n, 'GND', pfx=('C',))]
    ic['FBparts'] = [g for n in nets_of('FB') for g in two_pin_on(b, n)]
    named = {g.ref for k in ('CIN', 'COUT', 'CBOOT', 'CBIAS', 'FBparts') for g in ic[k]}
    if ic['L']:
        named.add(ic['L'].ref)
    ic['misc'] = [r for r in ic['own_refs'] if r not in named]
    return ic

def _slot(ref, role, pos, rot, why, n=(0.0, 0.0)):
    return {'ref': ref, 'role': role, 'x': pos[0], 'y': pos[1], 'rot': rot,
            'why': why, 'n': n}

# who gets to keep its ideal spot when two slots collide. The loop parts come
# first because their whole reason for being there is the loop; a bias cap
# 2 mm further out costs nothing.
# The inductor keeps its spot ahead of everything: SW-pad-to-inductor is the
# shortest and most critical edge of the loop, and it is also the part with no
# room to spare. A third stacked input cap is what should move instead.
SLOT_PRIO = {'L': 0, 'CIN': 1, 'COUT': 2, 'CBOOT': 3, 'CBIAS': 4, 'FB': 5}

def resolve_slots(b, slots):
    """Push lower-priority slots outward until they stop overlapping.

    On a package that puts VIN, PGND, SW and BOOT on one corner - which is most
    of them, because that is what makes the loop small - the ideal spots
    genuinely collide. Reporting a pile of overlaps would be true and useless;
    the answer a person wants is the next-best spot, so take it."""
    done = []
    for s in sorted(slots, key=lambda s: (SLOT_PRIO.get(s['role'], 9), natkey(s['ref']))):
        n, moved = s['n'], 0.0
        while n != (0.0, 0.0) and moved < 12.0:
            box = slot_box(b, s)
            if not any(overlap_area(box, slot_box(b, d)) > 0.01 for d in done):
                break
            s['x'] += n[0] * 0.2; s['y'] += n[1] * 0.2
            moved += 0.2
        if moved:
            s['why'] += f" [pushed {moved:.1f} mm further out to clear another slot]"
        done.append(s)
    return slots

def ic_plan(b, ic, a):
    """Recommended position + rotation for every passive we could name.

    The one rule underneath all of it: a high-di/dt loop is made small by
    putting the cap across the pin pair that carries the loop, on the outside
    face of the package, with nothing between them."""
    f, R, out = ic['fp'], ic['pads'], []
    C = ctr(f.crtyd)
    gnd = R.get('GND', [])

    def straddle(hot, caps, role, label):
        """Caps placed across a (supply pin, return pin) pair, on the outside
        face of the package, smallest innermost. This is the whole loop-area
        rule, and it is the same rule for a buck's VIN/PGND pair and for a
        plain IC's VDD/GND bypass - so there is one implementation."""
        pairs = []
        for vp in hot:
            if not gnd:
                break
            gp = min(gnd, key=lambda g: math.hypot(g['x'] - vp['x'], g['y'] - vp['y']))
            pairs.append((math.hypot(gp['x'] - vp['x'], gp['y'] - vp['y']), vp, gp))
        pairs.sort(key=lambda t: t[0])
        seen, uniq = set(), []
        for d, vp, gp in pairs:
            k = (round(vp['x'], 2), round(vp['y'], 2))
            if k not in seen:
                seen.add(k); uniq.append((d, vp, gp))
        rows = defaultdict(float)
        for i, g in enumerate(sorted(caps, key=lambda g: parse_value(g.value, 'C') or 9e9)):
            if not uniq:
                break
            d, vp, gp = uniq[i % len(uniq)]
            ax = fp_axis(g)
            if not ax:
                continue
            u = unit((gp['x'] - vp['x'], gp['y'] - vp['y']))
            m = ((vp['x'] + gp['x']) / 2, (vp['y'] + gp['y']) / 2)
            n = axis_snap((-u[1], u[0]))
            if (m[0] - C[0]) * n[0] + (m[1] - C[1]) * n[1] < 0:
                n = (-n[0], -n[1])
            k = (round(n[0], 1), round(n[1], 1))
            off = ray_exit(m, n, f.crtyd) + a.gap + ax[2] / 2 + rows[k]
            rows[k] += ax[2] + a.gap
            p1 = next((p['net'] for p in g.pads if p['num'] == '1'), '')
            tgt = math.degrees(math.atan2(-u[1], u[0])) % 360
            if p1 and GND_RE.match(p1.split('/')[-1]):
                tgt = (tgt + 180) % 360
            out.append(_slot(g.ref, role, (m[0] + off * n[0], m[1] + off * n[1]),
                             want_rot(g, tgt),
                             f"across {label}.{vp['num']}/GND.{gp['num']} "
                             f"({d:.2f} mm apart), smallest value innermost", n))

    straddle(R.get('VIN', []), ic['CIN'], 'CIN', 'VIN')
    # -- inductor: outboard of the switch pads, body pointing away ---------
    swp = [p for p in f.pads if p['net'] in ic['sw_nets']]
    L = ic['L']
    Ldir = Lout = None
    if L and swp and fp_axis(L):
        ax = fp_axis(L)
        groups = defaultdict(list)
        for p in swp:
            groups[p['net']].append(p)
        cs = [((sum(q['x'] for q in v) / len(v)), (sum(q['y'] for q in v) / len(v)))
              for v in groups.values()]
        if len(cs) >= 2:                       # buck-boost: bridge the two nodes
            axis = unit((cs[1][0] - cs[0][0], cs[1][1] - cs[0][1]))
            m = ((cs[0][0] + cs[1][0]) / 2, (cs[0][1] + cs[1][1]) / 2)
            n = axis_snap((-axis[1], axis[0]))
            why = "bridges both switch nodes, just outboard of the SW pads"
        else:
            m = cs[0]
            n = axis_snap((m[0] - C[0], m[1] - C[1])) if (m != C) else (1.0, 0.0)
            axis = n
            why = "SW pad to inductor is the shortest edge of the switching loop"
        if (m[0] - C[0]) * n[0] + (m[1] - C[1]) * n[1] < 0:
            n = (-n[0], -n[1])
        off = ray_exit(m, n, f.crtyd) + a.gap + (ax[2] if len(cs) >= 2 else ax[1]) / 2
        pos = (m[0] + off * n[0], m[1] + off * n[1])
        tgt = math.degrees(math.atan2(-axis[1], axis[0])) % 360
        p1 = next((p['net'] for p in L.pads if p['num'] == '1'), '')
        if len(cs) < 2 and p1 and p1 not in ic['sw_nets']:
            tgt = (tgt + 180) % 360
        out.append(_slot(L.ref, 'L', pos, want_rot(L, tgt), why, n))
        Ldir, Lout = n, (pos[0] + n[0] * ax[1] / 2, pos[1] + n[1] * ax[1] / 2)

    # -- output caps: immediately after the inductor, in the current path ---
    if Lout:
        # side by side across the output node, not end to end: three caps in a
        # line would put the last one 25 mm downstream of the first
        side = (-Ldir[1], Ldir[0])
        cs = sorted(ic['COUT'], key=lambda g: -(parse_value(g.value, 'C') or 0))
        wid = [fp_axis(g)[2] + a.gap for g in cs if fp_axis(g)]
        run = -sum(wid) / 2
        for g in cs:
            ax = fp_axis(g)
            if not ax:
                continue
            off = a.gap + ax[1] / 2
            lat = run + ax[2] / 2
            run += ax[2] + a.gap
            tgt = math.degrees(math.atan2(-Ldir[1], Ldir[0])) % 360
            p1 = next((p['net'] for p in g.pads if p['num'] == '1'), '')
            if p1 and GND_RE.match(p1.split('/')[-1]):
                tgt = (tgt + 180) % 360
            out.append(_slot(g.ref, 'COUT',
                             (Lout[0] + off * Ldir[0] + lat * side[0],
                              Lout[1] + off * Ldir[1] + lat * side[1]),
                             want_rot(g, tgt),
                             "a bank across the output node right at the "
                             "inductor's output pad, largest first", Ldir))

    if not Lout and ic['COUT']:
        # linear regulator or load switch: no inductor, so the output cap sits
        # across OUT/GND exactly the way the input cap sits across IN/GND
        straddle(R.get('OUT', []), ic['COUT'], 'COUT', 'OUT')

    # -- boot / bias caps: at their own pins, they are small loops too ------
    used = defaultdict(float)          # one stack per face, shared by both kinds
    for kind, role in (('CBOOT', 'BOOT'), ('CBIAS', 'BIAS')):
        for g in ic[kind]:
            ps = [p for p in f.pads if p['net'] in {q['net'] for q in g.pads}]
            ax = fp_axis(g)
            if not ps or not ax:
                continue
            m = (sum(p['x'] for p in ps) / len(ps), sum(p['y'] for p in ps) / len(ps))
            n = axis_snap((m[0] - C[0], m[1] - C[1]))
            k = (round(n[0], 1), round(n[1], 1))
            off = ray_exit(m, n, f.crtyd) + a.gap + ax[2] / 2 + used[k]
            used[k] += ax[2] + a.gap
            out.append(_slot(g.ref, kind, (m[0] + off * n[0], m[1] + off * n[1]),
                             want_rot(g, 90 if abs(n[0]) < 0.5 else 0),
                             f"hard against the {role} pin(s) it serves", n))

    # -- feedback divider: outboard of FB, away from SW ---------------------
    fbp = R.get('FB', [])
    if fbp and ic['FBparts']:
        m = (sum(p['x'] for p in fbp) / len(fbp), sum(p['y'] for p in fbp) / len(fbp))
        n = axis_snap((m[0] - C[0], m[1] - C[1]))
        along = (-n[1], n[0])
        run = 0.0
        for g in sorted(ic['FBparts'], key=lambda g: natkey(g.ref)):
            ax = fp_axis(g)
            if not ax:
                continue
            off = ray_exit(m, n, f.crtyd) + a.gap + ax[1] / 2
            lat = run + ax[2] / 2
            run += ax[2] + a.gap
            pos = (m[0] + off * n[0] + along[0] * lat, m[1] + off * n[1] + along[1] * lat)
            tgt = math.degrees(math.atan2(-n[1], n[0])) % 360
            out.append(_slot(g.ref, 'FB', pos, want_rot(g, tgt),
                             "FB node kept short and pointed away from SW; "
                             "route FB on a layer with GND under it", n))
    return resolve_slots(b, out)

# uppercase = an IC pin, lowercase = a recommended slot; they must not collide
ROLE_MARK = {'VIN': 'V', 'GND': 'G', 'SW': 'S', 'FB': 'F', 'BOOT': 'B',
             'OUT': 'O', 'BIAS': 'X'}

def slot_box(b, s):
    ax = fp_axis(b.fps[s['ref']])
    if not ax:
        return (s['x'] - .5, s['y'] - .5, s['x'] + .5, s['y'] + .5)
    horiz = min(s['rot'] % 180, 180 - s['rot'] % 180) < 45
    w, h = (ax[1], ax[2]) if horiz else (ax[2], ax[1])
    return (s['x'] - w / 2, s['y'] - h / 2, s['x'] + w / 2, s['y'] + h / 2)

def ic_diagram(b, ic, slots, cols):
    """A picture of the recommendation, to glance at. Same ASCII-grid trick as
    `map`, but scoped to one IC so a cell is a few tenths of a millimetre.

    ic['off'] is the anchor shift: the slots are already in board coordinates
    but the IC's own pads are still where it is parked, so its geometry moves
    here too or the frame stretches across the whole pile."""
    f = ic['fp']
    ox, oy = ic.get('off', (0.0, 0.0))
    crtyd = (f.crtyd[0] + ox, f.crtyd[1] + oy, f.crtyd[2] + ox, f.crtyd[3] + oy)
    boxes = [slot_box(b, s) for s in slots]
    r = bbox([(v[i], v[j]) for v in [crtyd] + boxes for i, j in ((0, 1), (2, 3))])
    r = grow(r, 0.6)
    cw = max((r[2] - r[0]) / cols, 0.05)
    ch = cw * 2.0
    nr = max(3, int(math.ceil((r[3] - r[1]) / ch)))
    g = [[' '] * cols for _ in range(nr)]

    def stamp(box, ch_, over=True):
        for gy in range(max(0, int((box[1] - r[1]) / ch)),
                        min(nr, int((box[3] - r[1]) / ch) + 1)):
            for gx in range(max(0, int((box[0] - r[0]) / cw)),
                            min(cols, int((box[2] - r[0]) / cw) + 1)):
                # first writer wins: a cell straddling two boxes that merely
                # sit next to each other is not a clash, and painting it '*'
                # made a correct layout look broken
                if g[gy][gx] in (' ', '.') or (over and g[gy][gx] == '.'):
                    g[gy][gx] = ch_

    stamp(crtyd, '.', over=False)
    keys = {}
    for i, (s, box) in enumerate(zip(slots, boxes)):
        k = chr(ord('a') + i) if i < 26 else '?'
        keys[k] = s
        stamp(box, k)
    # a placed part standing in a slot, clipped to the clash itself: stamping
    # its whole courtyard once painted the entire frame '!' and said nothing
    for gname, gfp in b.fps.items():
        if gfp is f or not gfp.placed or gfp.back != f.back or b.is_hole(gfp) \
           or gname in {s['ref'] for s in slots}:
            continue
        for box in boxes:
            if hit(box, gfp.crtyd):
                stamp((max(box[0], gfp.crtyd[0]), max(box[1], gfp.crtyd[1]),
                       min(box[2], gfp.crtyd[2]), min(box[3], gfp.crtyd[3])), '!')
    # pins last and one cell each, so they always survive and never fight
    R = role_pads(f)
    for role, ps in R.items():
        if role not in ROLE_MARK:
            continue
        for p in ps:
            gx, gy = int((p['x'] + ox - r[0]) / cw), int((p['y'] + oy - r[1]) / ch)
            if 0 <= gx < cols and 0 <= gy < nr:
                g[gy][gx] = ROLE_MARK[role]
    out = [f"  recommended layout, 1 cell = {cw:.2f} x {ch:.2f} mm, x+ right / y+ down",
           "  +" + "-" * cols + "+"]
    out += ["  |" + ''.join(row) + "|" for row in g]
    out.append("  +" + "-" * cols + "+")
    out.append("  IC body '.'   pins " + ' '.join(f"{v}={k}" for k, v in ROLE_MARK.items())
               + "   '!' = a placed part is standing in that slot")
    out.append(f"  cells are {cw:.2f} mm wide, so two slots can share one - the table "
               f"above has the real numbers")
    out.append("  " + '   '.join(f"{k}={keys[k]['ref']}" for k in sorted(keys)))
    return out

ANCHOR_ORDER = {'L': 0, 'COUT': 1, 'CIN': 2, 'CBOOT': 3, 'CBIAS': 4, 'FB': 5}

def ic_anchor(b, ic, slots, a):
    """Re-hang the whole recommendation off a part that is already placed.

    Mid-placement the big parts land first: on this board the inductors are
    down and the regulators are still in the parked pile. Without this the
    tool reports 'L4 is 90 mm from its slot', which is true and useless - the
    inductor is not the thing that should move. Anchoring instead answers the
    question actually being asked: given L4 where it is, where does U13 go.

    Translation only. If the anchor also needs turning, that is said out loud
    rather than guessed at, because rotating the IC changes every pad position
    and the honest fix is to rotate it in KiCad and re-run."""
    if a.anchor.lower() in ('none', '-'):
        return None
    cands = [s for s in slots if b.fps[s['ref']].placed]
    if a.anchor:
        cands = [s for s in cands if s['ref'].upper() == a.anchor.upper()]
        if not cands:
            ic['warn'].append(f"--anchor {a.anchor}: not one of this IC's placed "
                              f"passives, ignored")
            return None
    elif ic['fp'].placed:
        return None                       # the IC itself is the anchor already
    if not cands:
        return None
    s = min(cands, key=lambda s: (ANCHOR_ORDER.get(s['role'], 9), natkey(s['ref'])))
    g = b.fps[s['ref']]
    dx, dy = g.x - s['x'], g.y - s['y']
    dr = (g.rot - s['rot']) % 360
    for t in slots:
        t['x'] += dx; t['y'] += dy
    ic['off'] = (dx, dy)
    return {'ref': s['ref'], 'dx': dx, 'dy': dy, 'dr': dr if dr <= 180 else dr - 360}

def c_ic(b, a):
    """Recommended placement for a regulator's supporting passives."""
    if not a.args:
        cands = [f for f in b.fps.values()
                 if prefix(f.ref) == 'U' and (role_pads(f).get('SW') or
                    role_pads(f).get('OUT')) and len(f.pads) >= 5]
        if not cands:
            print("no regulator-shaped part found (needs a pin named SW/LX/OUT). "
                  "`ic REF` works on any IC.")
            return 1
        print("`ic REF` gives a placement recommendation for an IC's passives. "
              "Candidates on this board:\n")
        for f in sorted(cands, key=lambda f: natkey(f.ref)):
            R = role_pads(f)
            print(f"  {f.ref:<5} {trunc(f.value,22):<22} "
                  f"{'placed' if f.placed else 'UNPLACED':<8} "
                  f"{'switching' if R.get('SW') else 'linear'}")
        return 0
    rc = 0
    for ref in a.args:
        f = b.fps.get(ref)
        if not f:
            print(f"{ref}: NOT FOUND"); rc = 1; continue
        ic = ic_context(b, f, a)
        slots = ic_plan(b, ic, a)
        anc = ic_anchor(b, ic, slots, a)
        print(f"\n=== {f.ref}  {f.value}   {ic['topo']}")
        if anc:
            print(f"  {f.ref} is NOT PLACED. Anchored on {anc['ref']}, which is: "
                  f"put {f.ref} at {f.x+anc['dx']:.2f},{f.y+anc['dy']:.2f} "
                  f"rot {f.rot:g} {f.layer} and the rows below follow.")
            if abs(anc['dr']) > 5:
                print(f"  {anc['ref']} is rotated {anc['dr']:+.0f} deg from the slot it "
                      f"wants. Turn {f.ref} by {anc['dr']:+.0f} deg in KiCad and re-run - "
                      f"rotating it moves every pad, so these numbers assume you have.")
        else:
            print(f"  {'placed at' if f.placed else 'NOT PLACED, parked at'} "
                  f"{f.x:.2f},{f.y:.2f} rot {f.rot:g} {f.layer}"
                  + ("" if f.placed else "  - the coordinates below are relative to "
                                         "the parked IC, so place it (or `--anchor` "
                                         "one of its placed passives) and re-run"))
        print(f"  VIN {', '.join(unesc_disp(n) for n in ic['vin_nets']) or '?'}"
              f"   SW {', '.join(unesc_disp(n) for n in ic['sw_nets']) or 'none'}"
              f"   VOUT {unesc_disp(ic['vout']) if ic['vout'] else '?'}")
        got = {s['ref'] for s in slots}
        named = [(k, [g.ref for g in ic[k]]) for k in ('CIN', 'COUT', 'CBOOT', 'CBIAS')]
        named.append(('L', [ic['L'].ref] if ic['L'] else []))
        named.append(('FB', [g.ref for g in ic['FBparts']]))
        if any(v for _, v in named):
            print("  parts     : " + '  '.join(f"{k}={' '.join(v)}" for k, v in named if v))
        if ic['misc']:
            print(f"  also on its own nets (not positioned): {' '.join(ic['misc'])}")

        if not slots:
            print("  nothing positionable found - no VIN/GND pin pair, no inductor on "
                  "the SW net, and no caps on its private nets.")
            for w in ic['warn'] + ic['notes']:
                print(f"  note: {w}")
            continue

        print(f"\n  {'ref':<6} {'role':<6} {'suggest x,y':<18} {'rot':>5}  "
              f"{'now':<20} why")
        for s in sorted(slots, key=lambda s: (s['role'], natkey(s['ref']))):
            g = b.fps[s['ref']]
            d = math.hypot(g.x - s['x'], g.y - s['y'])
            dr = min((g.rot - s['rot']) % 180, (s['rot'] - g.rot) % 180)
            now = 'unplaced' if not g.placed else (
                'OK' if d <= a.tol and dr <= 5 else f"{d:.1f} mm / {dr:.0f} deg off")
            print(f"  {s['ref']:<6} {s['role']:<6} "
                  f"{f'{s['x']:.2f},{s['y']:.2f}':<18} {s['rot']:>5.1f}  "
                  f"{now:<20} {s['why']}")

        print()
        for line in ic_diagram(b, ic, slots, min(a.cols, 64)):
            print(line)

        # -- the checks that only make sense once a target exists ----------
        msgs = list(ic['warn'])
        # two slots wanting the same space is a real outcome, not a bug: on a
        # package whose VIN, PGND and SW pins all sit on one corner the input
        # caps and the inductor both want to go there. Say so - the diagram
        # paints first-writer-wins and would hide it.
        boxes = [(t, slot_box(b, t)) for t in slots]
        for i, (t, bx) in enumerate(boxes):
            for u, by in boxes[i + 1:]:
                A = overlap_area(bx, by)
                if A > 0.01:
                    msgs.append(f"the slots for {t['ref']} ({t['role']}) and "
                                f"{u['ref']} ({u['role']}) overlap by {A:.2f} mm2 - "
                                f"both want the same face of the package; move one "
                                f"out and accept the longer loop on that one")
        blockers = defaultdict(list)
        for s in slots:
            box = slot_box(b, s)
            for g in b.fps.values():
                if g is f or g.ref in got or not g.placed or b.is_hole(g):
                    continue
                if g.back == f.back and hit(box, g.crtyd):
                    blockers[g.ref].append(s['ref'])
        for r, who in sorted(blockers.items(), key=lambda kv: natkey(kv[0]))[:a.max]:
            msgs.append(f"{r} is already placed inside the slot suggested for "
                        f"{' '.join(who)} - move one of them")
        swset = set(ic['sw_nets'])
        noisy = [g for g in b.fps.values()
                 if g.placed and any(p['net'] in swset for p in g.pads)]
        for g in ic['FBparts']:
            if not g.placed:
                continue
            for h in noisy:
                d = box_dist(g.crtyd, h.crtyd)
                if d < a.fb:
                    msgs.append(f"{g.ref} (feedback) is {d:.1f} mm from switching-node "
                                f"part {h.ref} - want >= {a.fb:g} mm, and never under "
                                f"the inductor")
        if ic['CIN'] and ic['pads'].get('VIN') and f.placed:
            # same measure on both sides - cap centre to the VIN pad - or the
            # comparison flatters whichever one is measured pad-to-pad
            vp = ic['pads']['VIN'][0]
            for g in ic['CIN'][:1]:
                t = next((s for s in slots if s['ref'] == g.ref), None)
                if g.placed and t:
                    now = math.hypot(g.x - vp['x'], g.y - vp['y'])
                    new = math.hypot(t['x'] - vp['x'], t['y'] - vp['y'])
                    verdict = (f"{now-new:.1f} mm shorter" if new < now - 0.2
                               else "no better - this package's VIN and GND pins are "
                                    "too far apart for the cap that has to bridge them")
                    msgs.append(f"input loop, {g.ref} centre to VIN.{vp['num']}: "
                                f"{now:.1f} mm now, {new:.1f} mm in the slot above "
                                f"({verdict})")
        if ic['pads'].get('FB') and ic['pads'].get('SW'):
            fx = ctr(f.crtyd)
            same = all((p['x'] - fx[0]) * (q['x'] - fx[0]) +
                       (p['y'] - fx[1]) * (q['y'] - fx[1]) > 0
                       for p in ic['pads']['FB'] for q in ic['pads']['SW'])
            if same:
                msgs.append("FB and SW pins are on the same face of the package - "
                            "run the FB trace out and around, never past the SW pad")
        for m in msgs[:a.max] + ic['notes']:
            print(f"  note: {m}")
        if len(msgs) > a.max:
            print(f"  ... +{len(msgs)-a.max} more note(s)")
        print("  Positions are geometric suggestions from pad coordinates and the "
              "loop rules, not\n  a datasheet layout. Check them against the "
              "datasheet's own layout example.")
    return rc

# ---------------- net span ----------------

def net_spans(b, a):
    """Bounding-box diagonal of every net's placed pads.

    The one routing-quality number that exists before any routing does: a
    three-node signal net whose parts are 100 mm apart is a floorplan problem,
    and it is visible now, not after the autorouter fails. Rails are excluded
    by node count rather than listed - dumping GND's 245 nodes is exactly the
    failure this tool exists to avoid."""
    rows = []
    for n, nodes in b.nets.items():
        base = n.split('/')[-1]
        # by node count AND by name: a two-node board can still have a +3V3 net,
        # and a rail's span is the board no matter how few things sit on it
        if len(nodes) > a.fanout or n.startswith('unconnected-') \
           or GND_RE.match(base) or rail_voltage(base) is not None:
            continue
        pts, refs = [], set()
        for r, num in nodes:
            f = b.fps.get(r)
            if not f or not f.placed:
                continue
            refs.add(r)
            pts += [(p['x'], p['y']) for p in f.pads if p['num'] == num]
        if len(pts) < 2:
            continue
        bb = bbox(pts)
        rows.append({'net': n, 'span': math.hypot(bb[2] - bb[0], bb[3] - bb[1]),
                     'placed': len(refs), 'nodes': len({r for r, _ in nodes}),
                     'refs': sorted(refs, key=natkey)})
    rows.sort(key=lambda r: -r['span'])
    return rows

def c_span(b, a):
    rows = net_spans(b, a)
    if a.json:
        print(json.dumps(rows[:a.max], indent=1)); return 0
    if not rows:
        print("no net has two or more placed pads yet"); return 0
    diag = math.hypot(b.outline[2] - b.outline[0], b.outline[3] - b.outline[1]) \
        if b.outline else 0.0
    print(f"{b.path}: {len(rows)} net(s) with 2+ placed pads, longest first"
          + (f"   board diagonal {diag:.0f} mm" if diag else ""))
    print(f"\n{'span':>7}  {'net':<34} {'plc/tot':>8}  parts")
    for r in rows[:a.max]:
        mark = ' ' if r['placed'] == r['nodes'] else '+'
        print(f"{r['span']:>6.1f}m  {trunc(unesc_disp(r['net']),34):<34} "
              f"{r['placed']:>3}/{r['nodes']:<4}{mark} {trunc(' '.join(r['refs']), 40)}")
    if len(rows) > a.max:
        print(f"  ... +{len(rows)-a.max} shorter net(s) ({len(rows)} total)")
    print(f"\nNets above --fanout {a.fanout:g} nodes (GND, +3V3, ...) are left out on "
          f"purpose - a rail's\nspan is the board, and listing its nodes would bury "
          f"everything else. '+' = not every\nnode is placed yet, so the span will "
          f"only grow. Span is pad bbox diagonal, not trace\nlength; a long span is "
          f"a floorplan question, not yet a routing error.")
    return 0

# ---------------- rules ----------------

RULES = {
    'OVERLAP':  'courtyards of two placed parts intersect',
    'EDGECLR':  'courtyard crosses the board edge or sits closer than --edge',
    'HOLECLR':  'part inside a mounting hole keepout, or a hole off the board',
    'CONNACC':  'connector buried away from an edge, or its cable exit blocked',
    'RFNOISE':  'RF part / antenna net within --rf of a switching node',
    'THERMAL':  'heat source within --therm of a heat-sensitive part',
    'BYPASS':   'supply pin further than --bypass from its nearest bypass cap',
    'NOCRTYD':  'footprint has no courtyard - overlap was checked on pads+fab instead',
    'NETSPAN':  'a fully placed non-rail net whose pads are more than --span apart',
    'UNPLACED': 'footprint still parked off the board outline',
}

def _drill_in(f, g):
    """Does either part's through-hole barrel land inside the other's courtyard?"""
    for a, b in ((f, g), (g, f)):
        for p in a.pads:
            if p['drill'] and pt_box_dist((p['x'], p['y']), b.crtyd) < p['drill'] / 2:
                return True
    return False

def gen_findings(b, a):
    only = {s.strip().upper() for s in a.only.split(',')} if a.only else None
    skip = {s.strip().upper() for s in a.skip.split(',')} if a.skip else set()
    F = []
    def add(sev, rule, msg, refs=()):
        if (only and rule not in only) or rule in skip:
            return
        F.append({'severity': sev, 'rule': rule, 'msg': msg, 'refs': list(refs)})

    P = b.placed()
    holes = [f for f in b.fps.values() if b.is_hole(f)]

    # --- UNPLACED: one folded line per sheet, never one per part -------
    bys = defaultdict(list)
    for f in b.fps.values():
        if not f.placed:
            bys[sheet_of(f)].append(f.ref)
    for s in sorted(bys, key=natkey):
        add('INFO', 'UNPLACED', f"{trunc(s,24)}: {len(bys[s])} off the board - "
                                f"{trunc(refrange(bys[s]), 90)}")

    # --- NOCRTYD -------------------------------------------------------
    for f in P:
        if not f.crtyd_real:
            add('INFO', 'NOCRTYD', f"{f.ref} has no F/B.CrtYd geometry; "
                                   f"overlap tested on its pad+fab extent instead", [f.ref])

    # --- OVERLAP: pairwise, but only among placed parts ----------------
    # O(n^2) on the placed set only. 400 unplaced parts stacked in a pile would
    # otherwise produce thousands of meaningless pair findings.
    if not b.rings:
        add('WARN', 'EDGECLR', "no closed Edge.Cuts ring: cannot tell inside from "
                               "outside, so placement checks fell back to the outline bbox")
    S = sorted(P, key=lambda f: f.crtyd[0])
    for i, f in enumerate(S):
        for g in S[i + 1:]:
            if g.crtyd[0] > f.crtyd[2] + a.clear:      # sweep line: no further overlap possible
                break
            # Opposite sides do not clash just by projecting onto each other -
            # a back-side battery holder over front-side 0402s is fine. The only
            # real cross-side clash is a drilled barrel landing in the other
            # part's area, so test the drills, not the bounding boxes.
            if f.back != g.back and not _drill_in(f, g):
                continue
            if b.is_hole(f) or b.is_hole(g):
                continue                               # HOLECLR owns this pair
            A = overlap_area(grow(f.crtyd, a.clear / 2), grow(g.crtyd, a.clear / 2))
            if A > 1e-6:
                add('ERROR', 'OVERLAP', f"{f.ref} and {g.ref} courtyards overlap by "
                                        f"{A:.2f} mm2 ({'same' if f.back == g.back else 'opposite'} side)",
                    [f.ref, g.ref])

    # --- EDGECLR -------------------------------------------------------
    for f in P:
        if f.edge is None:
            continue
        if f.edge <= 0.001:
            # a coax/USB/edge-mount part is SUPPOSED to sit on the edge
            why = ''
            if EDGEMNT.search(f.fp):
                sev, why = 'INFO', ' (edge-mount part, expected)'
            elif RF_VAL.search(f.value) or RF_FP.search(f.fp):
                # a WROOM/E22-style module is placed with its antenna over the
                # edge on purpose; the thing to verify is the keepout, not this
                sev, why = 'INFO', (' (RF module - the antenna end is meant to overhang; '
                                    'confirm the keepout under it)')
            else:
                sev = 'ERROR'
            add(sev, 'EDGECLR', f"{f.ref} courtyard touches or crosses the board edge{why}",
                [f.ref])
        elif f.edge < a.edge:
            add('WARN', 'EDGECLR', f"{f.ref} courtyard is {f.edge:.2f} mm from the edge "
                                   f"(want >= {a.edge:g})", [f.ref])

    # --- HOLECLR -------------------------------------------------------
    for h in holes:
        r = max([max(p['w'], p['drill']) / 2 for p in h.pads] or [1.1]) + a.hole
        if not h.placed:
            # Mid-placement the whole BOM sits in a pile beside the board and a
            # hole parked in that pile is simply not placed yet. Only call it an
            # error when it is out there on its own, which is the real bug.
            pile = sum(1 for g in b.fps.values() if not g.placed and g is not h
                       and math.hypot(g.x - h.x, g.y - h.y) < 25.0)
            add('ERROR' if pile < 3 else 'INFO', 'HOLECLR',
                f"{h.ref} mounting hole is outside the board outline at "
                f"{h.x:.1f},{h.y:.1f}" + (f" (in the parked pile with {pile} other "
                f"unplaced parts - not placed yet, rather than misplaced)" if pile >= 3 else ""),
                [h.ref])
            continue
        keep = (h.x - r, h.y - r, h.x + r, h.y + r)
        for f in P:
            if f is h or b.is_hole(f):
                continue
            if hit(keep, f.crtyd):
                add('ERROR', 'HOLECLR', f"{f.ref} is inside {h.ref}'s {r:.1f} mm screw "
                                        f"keepout", [f.ref, h.ref])

    # --- CONNACC -------------------------------------------------------
    for f in P:
        if not b.is_conn(f) or prefix(f.ref) == 'TP' or f.edge is None:
            continue
        if EDGEMNT.search(f.fp) and f.edge > 1.0:
            add('WARN', 'CONNACC', f"{f.ref} ({trunc(f.fp.split(':')[-1],28)}) is an "
                                   f"edge/side-entry part but sits {f.edge:.1f} mm in from "
                                   f"the edge", [f.ref])
        elif f.edge > a.conn:
            add('WARN', 'CONNACC', f"{f.ref} is {f.edge:.1f} mm from the nearest edge "
                                   f"(want <= {a.conn:g} so a cable can reach it)", [f.ref])
            continue
        # cable exit: sweep the courtyard straight out to the nearest edge and
        # see what is standing in the corridor. Axis-aligned only, which is the
        # honest limit - a diagonal exit is not modelled.
        for blk in _corridor(b, f):
            add('WARN', 'CONNACC', f"{f.ref}'s cable exit corridor is blocked by "
                                   f"{blk.ref} ({trunc(blk.value,18)})", [f.ref, blk.ref])

    # --- RFNOISE -------------------------------------------------------
    sw = b.sw_nets()
    noisy = [f for f in P if any(p['net'] in sw for p in f.pads)]
    for f in P:
        if not b.is_rf(f):
            continue
        for g in noisy:
            if g is f:
                continue
            d = box_dist(f.crtyd, g.crtyd)
            if d < a.rf:
                add('WARN', 'RFNOISE', f"{f.ref} ({trunc(f.value,18)}) is {d:.1f} mm from "
                                       f"switching node part {g.ref}"
                                       f"{'' if f.back == g.back else ' (opposite side)'} "
                                       f"(want >= {a.rf:g})",
                    [f.ref, g.ref])

    # --- THERMAL -------------------------------------------------------
    hot = [f for f in P if b.is_hot(f)]
    for f in P:
        if not b.is_sens(f):
            continue
        for g in hot:
            if g is f:
                continue
            d = box_dist(f.crtyd, g.crtyd)
            if d < a.therm:
                add('WARN', 'THERMAL', f"{f.ref} ({trunc(f.value,18)}) is heat-sensitive and "
                                       f"{d:.1f} mm from {g.ref} ({trunc(g.value,18)})"
                                       + ('' if f.back == g.back else
                                          ' (opposite side, coupled through the board)'),
                    [f.ref, g.ref])

    # --- NETSPAN -------------------------------------------------------
    # Fully placed only: a net still waiting on parts will move, and flagging it
    # now would just be noise that clears itself.
    for r in net_spans(b, a):
        if r['placed'] == r['nodes'] and r['span'] > a.span:
            add('WARN', 'NETSPAN', f"{unesc_disp(r['net'])} spans {r['span']:.0f} mm "
                                   f"between {len(r['refs'])} placed parts "
                                   f"({trunc(' '.join(r['refs']), 40)}) - want <= {a.span:.0f}",
                r['refs'])

    # --- BYPASS --------------------------------------------------------
    caps = defaultdict(list)
    for f in P:
        if prefix(f.ref) == 'C':
            for p in f.pads:
                if p['net']:
                    caps[p['net']].append(f)
    for f in P:
        if prefix(f.ref) != 'U' or len(f.pads) < 4:
            continue
        seen = set()
        for p in f.pads:
            if not p['net'] or not SUP_PIN.match(p['fn'] or ''):
                continue
            if GND_RE.match(p['net'].split('/')[-1]):
                continue      # e.g. NEO-M9N VDD_USB strapped to GND when USB is unused
            cl = [c for c in caps.get(p['net'], []) if c is not f]
            if not cl or p['net'] in seen:
                continue                    # no cap placed yet: not a placement fault
            seen.add(p['net'])
            d = min(pt_box_dist((p['x'], p['y']), c.crtyd) for c in cl)
            best = min(cl, key=lambda c: pt_box_dist((p['x'], p['y']), c.crtyd))
            if d > a.bypass:
                add('WARN', 'BYPASS', f"{f.ref}.{p['num']} ({p['fn']}, {unesc_disp(p['net'])}) "
                                      f"nearest placed bypass cap is {best.ref} at {d:.1f} mm "
                                      f"(want <= {a.bypass:g})", [f.ref, best.ref])

    n = len(F)
    F = [f for f in F if not suppressed(f, a.suppress)]
    return F, n - len(F)

def _corridor(b, f):
    """Parts standing between a connector and the nearest board edge."""
    c, o = f.crtyd, b.outline
    if not o:
        return []
    gaps = {'-x': c[0] - o[0], '+x': o[2] - c[2], '-y': c[1] - o[1], '+y': o[3] - c[3]}
    d = min(gaps, key=lambda k: gaps[k])
    if gaps[d] <= 0.5:
        return []
    lane = {'-x': (o[0], c[1], c[0], c[3]), '+x': (c[2], c[1], o[2], c[3]),
            '-y': (c[0], o[1], c[2], c[1]), '+y': (c[0], c[3], c[2], o[3])}[d]
    return [g for g in b.placed()
            if g is not f and g.back == f.back and not b.is_hole(g) and hit(lane, g.crtyd)]

def c_check(b, a):
    F, supp = gen_findings(b, a)
    if a.json:
        print(json.dumps(F, indent=1)); return 2 if any(x['severity'] == 'ERROR' for x in F) else 0
    n = defaultdict(int)
    for f in F:
        n[f['severity']] += 1
    print_findings(F, f"{b.path}: {n['ERROR']} error, {n['WARN']} warn, {n['INFO']} info "
                      f"({len(b.placed())} of {len(b.fps)} footprints placed)\n",
                   rules=RULES, cap=a.max)
    if not F:
        print("no findings")
    if supp:
        print(f"\n({supp} finding(s) suppressed via kpcb.json `suppress` list - "
              f"`--no-suppress` to see them)")
    if a.rules or not F:
        print("\nrules: " + ', '.join(f"{k}={v}" for k, v in sorted(RULES.items())))
    print("\nThese are geometric heuristics on placement only - no routing, no DRC, no "
          "3D bodies.\nConfirm anything that matters against the mechanical drawing or "
          "KiCad's own DRC.")
    return 2 if n['ERROR'] else 0

# ---------------- board vs netlist ----------------

def _canon_net(n):
    """KiCad appends _1, _2 to `unconnected-*` pseudo-nets on the board side but
    not in the netlist export. That is not a difference; treating it as one puts
    a false finding in front of every real one."""
    return re.sub(r'_\d+$', '', n) if n.startswith('unconnected-') else n

def find_netlist(board_path, given):
    """The .net that goes with this board. Same stem first, then any .net in the
    directory - and it says which one it took when there was a choice, because
    silently reviewing against the wrong export is worse than not finding one."""
    if given:
        return (rel(given), 1) if os.path.exists(given) else (None, 0)
    stem = os.path.splitext(os.path.abspath(board_path))[0]
    d = os.path.dirname(stem)
    # a *-merged.net (multi-root export) is authoritative; a bare stem.net can be
    # a single-root export that silently omits whole sheets, so prefer merged.
    for c in (stem + '-merged.net', os.path.join(d, '*-merged.net'),
              stem + '.net', os.path.join(d, '*.net')):
        hits = sorted(glob.glob(c))
        if hits:
            return rel(hits[0]), len(hits)
    return None, 0

def rel(p):
    r = os.path.relpath(p)
    return r if not r.startswith('..') else p

def sync_findings(b, netpath, a):
    """Every difference between what the schematic says and what the board has.

    This runs before anything else is worth reading. A placement review of a
    board that was never re-synced is a review of the wrong circuit, and the
    board file gives no hint that it is stale - its pads carry net names that
    look perfectly valid."""
    from kcommon import Netlist
    n = Netlist(netpath)
    F = []
    def add(sev, rule, msg, refs=()):
        F.append({'severity': sev, 'rule': rule, 'msg': msg, 'refs': list(refs)})

    bf, nf = set(b.fps), set(n.comps)
    if bf - nf:
        add('ERROR', 'SYNCPART', f"on the board but not in the netlist "
            f"({len(bf-nf)}): {trunc(refrange(sorted(bf-nf, key=natkey)), 90)}")
    if nf - bf:
        add('ERROR', 'SYNCPART', f"in the netlist but not on the board "
            f"({len(nf-bf)}): {trunc(refrange(sorted(nf-bf, key=natkey)), 90)}")

    for r in sorted(bf & nf, key=natkey):
        f, c = b.fps[r], n.comps[r]
        if c['footprint'] and f.fp != c['footprint']:
            add('ERROR', 'SYNCFP', f"{r} footprint differs: board {trunc(f.fp,34)} / "
                                   f"netlist {trunc(c['footprint'],34)}", [r])
        if f.value != c['value']:
            add('WARN', 'SYNCVAL', f"{r} value differs: board {trunc(f.value,20)} / "
                                   f"netlist {trunc(c['value'],20)}", [r])
        if f.dnp != c['dnp']:
            add('WARN', 'SYNCDNP', f"{r} is DNP {'on the board' if f.dnp else 'in the '
                                   'netlist'} only", [r])

    nmis = 0
    for r in sorted(bf & nf, key=natkey):
        for p in b.fps[r].pads:
            want = n.pinnet.get((r, p['num']))
            if want is None:
                if p['net']:
                    add('WARN', 'SYNCNET', f"{r} pad {p['num']} carries "
                        f"{unesc_disp(p['net'])} but the netlist has no such pin", [r])
                continue
            if _canon_net(p['net'] or '') != _canon_net(want):
                nmis += 1
                add('ERROR', 'SYNCNET', f"{r}.{p['num']} is on "
                    f"{unesc_disp(p['net']) or '(no net)'} on the board but "
                    f"{unesc_disp(want)} in the netlist", [r])
    return F, n

def c_sync(b, a):
    netpath, cands = find_netlist(b.path, a.args[0] if a.args else None)
    if not netpath:
        print("sync needs the .net export: `kpcb.py board.kicad_pcb sync board.net`\n"
              "(a *.net beside the board is found automatically)", file=sys.stderr)
        return 1
    try:
        F, n = sync_findings(b, netpath, a)
    except Exception as e:
        print(f"could not read {netpath}: {e}", file=sys.stderr); return 3
    if a.json:
        print(json.dumps(F, indent=1))
        return 2 if any(x['severity'] == 'ERROR' for x in F) else 0
    ne = sum(1 for x in F if x['severity'] == 'ERROR')
    print(f"board   : {b.path}   {len(b.fps)} footprints")
    print(f"netlist : {netpath}   {len(n.comps)} components   exported {n.date}"
          + (f"   [{cands} .net files here; name one to be sure]" if cands > 1 else ""))
    if not F:
        print("\nIN SYNC - same parts, same footprints, same values, same net on every "
              "pad.\nPlacement findings can be trusted to be about the current circuit.")
        return 0
    print_findings(F, f"\n{ne} error, {len(F)-ne} warn\n", rules=SYNC_RULES, cap=a.max)
    if ne:
        print("\nThe board has not been re-synced from the schematic. Run KiCad's "
              "'Update PCB from\nSchematic' first - until then every other finding "
              "here may be about a stale circuit.")
    else:
        print("\nConnectivity matches; only the annotations above differ. Worth "
              "reconciling, but\nplacement findings are still about the right "
              "circuit.")
    return 2 if ne else 0

SYNC_RULES = {
    'SYNCPART': 'component in one file and not the other',
    'SYNCFP':   'footprint assignment differs between board and netlist',
    'SYNCVAL':  'value differs between board and netlist',
    'SYNCDNP':  'DNP flag differs between board and netlist',
    'SYNCNET':  'a pad sits on a different net than the netlist says',
}

# ---------------- one-call review ----------------

def c_review(b, a):
    """Everything a fresh session needs before it can say anything useful, in one
    call, ending with the specific next calls worth making.

    The point is not to save printing - it is to save the caller working out
    what to run next from a summary it has not seen yet."""
    netpath, _ = find_netlist(b.path, a.args[0] if a.args else None)
    if netpath:
        try:
            F, n = sync_findings(b, netpath, a)
            ne = sum(1 for x in F if x['severity'] == 'ERROR')
            if ne:
                print(f"### STOP: board vs {os.path.basename(netpath)} - {ne} error(s)\n")
                print_findings(F, '', rules=SYNC_RULES, cap=5)
                print("\nThe board is stale. `sync` for the full list, then re-sync in "
                      "KiCad.\nEverything below is about the circuit the BOARD has, "
                      "which is not the one you drew.\n")
            else:
                print(f"### in sync with {os.path.basename(netpath)}"
                      f"{f' ({len(F)} non-blocking difference(s), `sync` for detail)' if F else ''}\n")
        except Exception as e:
            print(f"### could not read {netpath}: {e}\n")
    else:
        print("### no .net beside the board - cannot tell whether the board is stale. "
              "Pass one:\n### `kpcb.py board.kicad_pcb review board.net`\n")

    print("### placement"); c_summary(b, a)
    print("\n### findings"); rc = c_check(b, a)
    print("\n### longest nets"); old, a.max = a.max, min(a.max, 6); c_span(b, a); a.max = old

    # what to do next, worked out from the findings rather than left to the reader
    F, _ = gen_findings(b, a)
    hot = defaultdict(int)
    for x in F:
        if x['severity'] == 'ERROR':
            for r in x['refs']:
                hot[r] += 1
    nxt = []
    regs = [f.ref for f in b.fps.values() if prefix(f.ref) == 'U' and not f.placed
            and (role_pads(f).get('SW') or role_pads(f).get('OUT')) and len(f.pads) >= 5]
    if regs:
        nxt.append(f"kpcb.py {b.path} ic {' '.join(sorted(regs, key=natkey)[:4])}"
                   f"   # unplaced regulator(s): where their passives go")
    busy = [r for r, c in sorted(hot.items(), key=lambda kv: (-kv[1], natkey(kv[0])))[:4]]
    if busy:
        nxt.append(f"kpcb.py {b.path} where {' '.join(busy)}"
                   f"   # in the most ERROR findings")
    if sum(1 for f in b.fps.values() if not f.placed) > 20:
        nxt.append(f"kpcb.py {b.path} map   # where the free space is, before placing more")
    if netpath:
        nxt.append(f"knet.py {netpath} check   # the electrical half; placement cannot see it")
    if b.zones:
        nxt.append(f"kpcb.py {b.path} zones   # pour coverage per layer (cached fill state)")
    if any(re.search(r'RF|50', b.netclass(n), re.I) for n in b.nets):
        nxt.append(f"kpcb.py {b.path} rf   # 50-ohm nets: width necks, Zo, reference plane, via fence")
    nxt.append(f"kpcb.py {b.path} height   # Z stack from the 3D models (cell + board + tallest part)")
    nxt.append(f"kdrc.py {b.path}   # KiCad's own DRC + ERC - real clearance/rules, not heuristics")
    nxt.append(f"kpcb.py {b.path} sync   # re-run after any schematic change")
    print("\n### next\n" + '\n'.join('  ' + x for x in nxt))
    return rc

def _zones_under(b, a):
    """`zones REF...`: point-sample whether a filled net actually covers a
    footprint's courtyard, instead of just eyeballing the whole-board table.
    Closes the RF-reference-plane question ("is GND continuous under U9")
    that the bbox-margin view can only infer from dominant-island + all-edges-
    reached. 11x11 samples per footprint; each sample tests every fill whose
    own bbox overlaps the courtyard, so a plane broken into several polygons
    still counts as covered wherever any piece of it reaches."""
    fail = 0
    for ref in a.args:
        f = b.fps.get(ref)
        if not f:
            print(f"{ref}: no such footprint"); fail = 1; continue
        rect = f.crtyd
        # an edge-mount SMA/USB-C courtyard overhangs the outline; copper can't
        # exist there, so sampling it would read a good launch as MOSTLY MISSING.
        # Grid only the on-board part (then drop any point a notch still excludes).
        o = b.outline or rect
        on = (max(rect[0], o[0]), max(rect[1], o[1]), min(rect[2], o[2]), min(rect[3], o[3]))
        # ponytail: fixed 0.5 mm inset on clipped sides for the pour's edge pullback;
        # read the zone/edge clearances if a board pulls back further
        e = 0.5
        g = (on[0] + e * (on[0] > rect[0]), on[1] + e * (on[1] > rect[1]),
             on[2] - e * (on[2] < rect[2]), on[3] - e * (on[3] < rect[3]))
        pts = _sample_grid(g, 11) if g[0] < g[2] and g[1] < g[3] else []
        if b.rings:
            pts = [p for p in pts if inside(p, b.rings)]
        off = 1 - overlap_area(on, rect) / max(f.area, 1e-9) if pts else 1.0
        print(f"{ref}: courtyard {rect[2]-rect[0]:.1f} x {rect[3]-rect[1]:.1f} mm "
              f"@ {ctr(rect)[0]:.1f},{ctr(rect)[1]:.1f}"
              + (f"  ({100*off:.0f}% hangs off the board edge, not sampled)" if off > 0.005 else ''))
        if not pts:
            continue
        for ly in b.copper:
            # bbox overlap is only a coarse prefilter: a net's OWN pour can be
            # substantial yet still never actually reach this courtyard (its
            # island bbox just happens to graze it), so score every candidate
            # net and only report ones that land at least one real sample -
            # otherwise "the plane exists somewhere nearby" reads as "broken".
            fills_here = [fl for fl in b.fills if fl['layer'] == ly and hit(fl['bbox'], rect)]
            cand = sorted({fl['net'] for fl in fills_here})
            scored = []
            for net in cand:
                fs = [fl for fl in fills_here if fl['net'] == net]
                hit_n, holes = 0, []
                for p in pts:
                    if any(point_in_poly(p, fl['pts']) for fl in fs):
                        hit_n += 1
                    else:
                        holes.append(p)
                if hit_n:
                    scored.append((net, hit_n, holes))
            if not scored:
                print(f"    {ly:<8} no fill actually reaches this footprint's courtyard")
                fail = 1
                continue
            for net, hit_n, holes in scored:
                pct = 100 * hit_n / len(pts)
                # a handful of misses is normal: every non-plane pad/via under
                # the part carries its own clearance moat. Only a MAJORITY
                # miss (a genuinely broken or absent reference) fails the exit
                # code; a few holes just get listed for a look, not a verdict.
                if hit_n == len(pts):
                    tag = 'continuous'
                elif not (GND_RE.match(net.split('/')[-1]) or rail_voltage(net.split('/')[-1]) is not None):
                    tag = 'local signal pour, not a reference plane'   # no verdict
                elif pct >= 50:
                    tag = 'has gaps (normal near non-plane pads/vias unless clustered)'
                else:
                    tag = 'MOSTLY MISSING'
                    fail = 1
                print(f"    {ly:<8} {net:<10} {pct:5.0f}% of samples covered  ({tag})")
                if holes:
                    shown = ' '.join(f'{x:.1f},{y:.1f}' for x, y in holes[:4])
                    more = f' ...+{len(holes)-4}' if len(holes) > 4 else ''
                    print(f"        uncovered near: {shown}{more}")
    print("\nSamples are an 11x11 grid inside the courtyard, not the whole fill - a scattered few\n"
          "misses are the normal clearance moat around any non-plane pad or via under the part,\n"
          "not a defect; only a large contiguous run of misses or <50% total is flagged. 100% is\n"
          "on this footprint only, not a guarantee elsewhere on the plane. Fills are the LAST\n"
          "SAVED state (see `zones` with no args).")
    return 2 if fail else 0

def c_zones(b, a):
    """Zone-fill coverage per copper layer, from the fills cached in the board.

    Reports gross copper area over board area, island count, largest-island
    share, and the fill's bounding-box margins to each board edge - enough to
    catch a pour that is fragmented, missing a layer, or nowhere near an edge.
    It is geometry on the LAST SAVED fill, not a live refill: a zone shows 0
    if it was never filled or a part moved after the last Fill All Zones.

    `zones REF...` instead point-samples whether a net's fill actually covers
    that footprint's courtyard - see `_zones_under`."""
    if a.args:
        return _zones_under(b, a)
    o = b.outline
    board_area = max((poly_area(r) for r in b.rings), default=0.0) or \
        ((o[2] - o[0]) * (o[3] - o[1]) if o else 0.0)
    by_layer = defaultdict(lambda: defaultdict(list))     # layer -> net -> [fill]
    for fl in b.fills:
        by_layer[fl['layer']][fl['net']].append(fl)
    declared = {(net, ly) for net, lays in b.zones for ly in lays}
    filled = set()
    rows = []
    for ly in b.copper:
        nets = by_layer.get(ly)
        if not nets:
            rows.append((ly, '-', 0.0, 0, 0.0, None))
            continue
        for net, fs in sorted(nets.items()):
            filled.add((net, ly))
            area = sum(f['area'] for f in fs)
            fb = bbox([p for f in fs for p in f['pts']])
            largest = max((f['area'] for f in fs), default=0.0)
            rows.append((ly, net, area, len(fs), largest, fb))
    unfilled = sorted(declared - filled)

    if a.json:
        print(json.dumps({'board_area_mm2': round(board_area, 1), 'outline': o,
                          'layers': [{'layer': ly, 'net': net,
                                      'area_mm2': round(ar, 1),
                                      'coverage_pct': round(100 * ar / board_area, 1) if board_area else 0,
                                      'islands': isl,
                                      'largest_island_pct': round(100 * lg / ar, 1) if ar else 0,
                                      'fill_bbox': [round(v, 2) for v in fb] if fb else None}
                                     for ly, net, ar, isl, lg, fb in rows if net != '-'],
                          'declared_unfilled': [{'net': n, 'layer': l} for n, l in unfilled]},
                         indent=1))
        return 0

    print(f"{b.path}: zone fill coverage")
    if o:
        print(f"board {board_area:.0f} mm2 (outline ring)   bbox "
              f"x {o[0]:.1f}..{o[2]:.1f}  y {o[1]:.1f}..{o[3]:.1f}")
    print(f"\n{'layer':<8} {'net':<8} {'cover':>6} {'isl':>4} {'top1':>5}  "
          f"uncovered margins (mm, of fill bbox)")
    for ly, net, area, isl, largest, fb in rows:
        if net == '-':
            print(f"{ly:<8} {'-':<8} {'0%':>6} {'0':>4} {'-':>5}  (no fill on this layer)")
            continue
        cov = 100 * area / board_area if board_area else 0
        top1 = 100 * largest / area if area else 0
        marg = (f"top {fb[1]-o[1]:.1f}  bot {o[3]-fb[3]:.1f}  "
                f"L {fb[0]-o[0]:.1f}  R {o[2]-fb[2]:.1f}") if (fb and o) else ''
        print(f"{ly:<8} {net:<8} {cov:5.0f}% {isl:>4} {top1:4.0f}%  {marg}")
    if unfilled:
        print("\nDECLARED BUT NOT FILLED (never filled, or emptied on last save):")
        for net, ly in unfilled:
            print(f"  {net} on {ly}")
    print("\nCoverage is gross copper area / board area; a low % or a small top-island "
          "share means a\nfragmented pour. Margins are of the fill's bounding box, so they "
          "only flag when NOTHING\nreaches that edge (one sliver hides the gap) - eyeball a "
          "render for interior voids.\nFills are the LAST SAVED state: refill in KiCad "
          "(Edit > Fill All Zones, save) or run\n`kdrc.py FILE drc` (refills in memory) if a "
          "zone reads 0 or parts moved since the fill.")
    return 0

def thick_of(b, layer):
    return b.thick.get(layer) or 0.035          # 1oz fallback when no stackup

def _net_pads(b, net):
    """(ref, pin, x, y) for every pad on a net - the named landmarks you can
    Ctrl+F / click in KiCad to find the trace."""
    return [(f.ref, p['num'], p['x'], p['y'])
            for f in b.fps.values() for p in f.pads if p['net'] == net]

def _nearest_pad_dist(pads, pt):
    """(ref, pin, distance mm) for the pad closest to a point, or None."""
    if not pt or not pads:
        return None
    return min(((r, n, math.hypot(x - pt[0], y - pt[1])) for r, n, x, y in pads),
               key=lambda t: t[2])

def _nearest_pad(pads, pt):
    """The pad closest to a point, as 'REF.PIN (D.D mm)', or '' if none."""
    t = _nearest_pad_dist(pads, pt)
    return f"{t[0]}.{t[1]} ({t[2]:.1f} mm away)" if t else ''

def _net_graph(b, net, tol=0.05):
    """Connectivity graph of one net's routed copper, so width is read in context
    instead of segment-by-segment. Nodes = track endpoints merged within `tol` mm,
    stitched across layers where a via sits and bridged where tracks land on a
    shared pad. Edges = the segments. A BRIDGE edge is one whose removal splits the
    graph: all current between the two sides must cross it, so the narrowest bridge
    is the real series bottleneck. A segment inside a parallel loop is not a bridge,
    so two traces that split and reconverge no longer read as one thin strand.
    Endpoint-based: two traces that only cross mid-span with no shared end/via/pad
    are still separate (KiCad would merge that copper; this does not)."""
    segs = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
    if not segs:
        return None

    def q(p):
        return (round(p[0] / tol), round(p[1] / tol))
    parent = {}

    def find(k):
        parent.setdefault(k, k)
        root = k
        while parent[root] != root:
            root = parent[root]
        while parent[k] != root:
            parent[k], k = root, parent[k]
        return root

    def union(x, y):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[rx] = ry

    ekeys = []
    for t in segs:
        ka, kb = (t['layer'],) + q(t['a']), (t['layer'],) + q(t['b'])
        find(ka); find(kb)
        ekeys.append((ka, kb))
    bygrid = defaultdict(list)
    for k in list(parent):
        bygrid[k[1:]].append(k)

    def near(gx, gy, r=1):
        return [k for dx in range(-r, r + 1) for dy in range(-r, r + 1)
                for k in bygrid.get((gx + dx, gy + dy), [])]
    for v in b.vias:                                  # a via stitches its layers
        if v['net'] == net:
            ks = near(*q((v['x'], v['y'])))
            for k in ks[1:]:
                union(ks[0], k)
    pad_key = {}                                       # ref -> a grid key at one of its pads
    for f in b.fps.values():                          # a pad bridges tracks on it
        for p in f.pads:
            if p['net'] != net:
                continue
            th = p['drill'] > 0 or any('*.Cu' in l for l in p['layers'])
            ks = near(*q((p['x'], p['y'])), r=max(1, int(p['w'] / 2 / tol)))
            if not th:
                ks = [k for k in ks if k[0] in p['layers']]
            if ks:
                pad_key[f.ref] = ks[0]
            for k in ks[1:]:
                union(ks[0], k)

    node = {}

    def nid(k):
        return node.setdefault(find(k), len(node))
    adj, enode = defaultdict(list), []
    for i, (ka, kb) in enumerate(ekeys):
        u, w = nid(ka), nid(kb)
        enode.append((u, w))
        adj[u].append((w, i)); adj[w].append((u, i))
    n = len(node)
    cid, comp = {}, 0                                 # label components over the edge graph
    for s in range(n):
        if s in cid:
            continue
        cid[s] = comp; stack = [s]
        while stack:
            u = stack.pop()
            for w, _ in adj[u]:
                if w not in cid:
                    cid[w] = comp; stack.append(w)
        comp += 1
    # the main current-carrying copper is the component with the most track length;
    # a stray fragment or a one-pad spur is its own (tiny) component and its lone
    # edge would otherwise count as a false bridge.
    clen = defaultdict(float)
    for i, (u, _w) in enumerate(enode):
        clen[cid[u]] += segs[i]['len']
    main = max(clen, key=clen.get) if clen else 0
    # bridges: iterative DFS low-link, skipping only the edge we entered on (so a
    # second parallel edge between the same nodes correctly prevents a bridge).
    bridges, disc, low, timer = set(), {}, {}, [0]
    for s in range(n):
        if s in disc:
            continue
        disc[s] = low[s] = timer[0]; timer[0] += 1
        stack = [(s, -1, iter(adj[s]))]
        while stack:
            u, pe, it = stack[-1]
            for (w, ei) in it:
                if ei == pe:
                    continue
                if w not in disc:
                    disc[w] = low[w] = timer[0]; timer[0] += 1
                    stack.append((w, ei, iter(adj[w])))
                    break
                low[u] = min(low[u], disc[w])
            else:
                stack.pop()
                if stack:
                    pu = stack[-1][0]
                    low[pu] = min(low[pu], low[u])
                    if low[u] > disc[pu]:
                        bridges.add(pe)
    main_bridges = {i for i in bridges if cid[enode[i][0]] == main}
    pad_node = {ref: nid(find(k)) for ref, k in pad_key.items()}
    return {'segs': segs, 'nodes': n, 'comp': comp, 'bridges': main_bridges,
            'adj': adj, 'enode': enode, 'cid': cid, 'pad_node': pad_node}

def _net_geo(b, net):
    """One net's routed copper in a single pass: {layer: [minw, len]}, total
    length, list of via drills, and series-sum resistance (ohm, pessimistic)."""
    layers, total, r = {}, 0.0, 0.0
    for t in b.tracks:
        if t['net'] != net or t['w'] <= 0:
            continue
        total += t['len']
        r += RHO_CU * t['len'] / (t['w'] * thick_of(b, t['layer']))
        L = layers.setdefault(t['layer'], [math.inf, 0.0, (0.0, 0.0), 0.0])
        L[1] += t['len']
        if t['w'] < L[0]:                        # remember the narrowest seg + where it is
            L[0], L[2], L[3] = t['w'], t['mid'], t['len']
    vd = [v['drill'] for v in b.vias if v['net'] == net and v['drill'] > 0]
    return layers, total, vd, r

def _seg_amp(b, t, dt):
    return ipc_current(t['w'], thick_of(b, t['layer']), t['layer'] in b.outer, dt)

def _is_tap_ref(b, ref):
    """A part that can only be a current SENSE tap, never a series power path:
    a thermistor/test point outright, or a resistor whose value is too high to
    be a power-path element (a shunt/current-sense R is <<1 ohm; a divider/pull
    tap is typically >=1k)."""
    p = prefix(ref)
    if p in ('TH', 'TP'):
        return True
    if p == 'R':
        fp = b.fps.get(ref)
        val = parse_value(fp.value, 'R') if fp else None
        return val is not None and val >= 1000.0
    return False

def _bridge_is_tap(b, g, i):
    """True if bridge edge `i` isolates a pendant sub-branch, on EITHER side,
    whose sole pads belong to sense-tap parts (see `_is_tap_ref`). Such a
    bridge is a false series bottleneck: the net's full budgeted current has
    no reason to detour down a thermistor or pull-up leg, so it should not be
    picked as the mandatory bridge for a TRACE-THIN verdict. Checking both
    sides (not just the smaller one) sidesteps a tie when a 2-node net splits
    1-vs-1."""
    u, w = g['enode'][i]
    seen = {u}
    stack = [u]
    while stack:
        x = stack.pop()
        for y, ei in g['adj'][x]:
            if ei == i or y in seen:
                continue
            seen.add(y)
            stack.append(y)
    comp_nodes = {n for n, c in g['cid'].items() if c == g['cid'][u]}
    other = comp_nodes - seen
    for side in (seen, other):
        refs = {ref for ref, nd in g['pad_node'].items() if nd in side}
        if refs and all(_is_tap_ref(b, ref) for ref in refs):
            return True
    return False

def _bott_fields(b, t, dt):
    """Bottleneck fields from a single track dict (the constraining segment)."""
    return {'i': _seg_amp(b, t, dt), 'ly': t['layer'], 'w': t['w'],
            'mid': t['mid'], 'seg': t['len'], 'ext': t['layer'] in b.outer}

def _amp_row(b, net, need, a, graph=False):
    layers, total, vd, r = _net_geo(b, net)
    segs = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
    if not layers and not vd:
        return None
    rows = []                                    # per-layer table, informational
    for ly, (minw, ln, mid, seglen) in sorted(layers.items()):
        ext = ly in b.outer
        rows.append((ly, minw, ln, ipc_current(minw, thick_of(b, ly), ext, a.dt),
                     ext, mid, seglen))
    # narrowest single segment anywhere (the old, geometry-blind number)
    naive = min(segs, key=lambda t: _seg_amp(b, t, a.dt)) if segs else None
    # narrowest MANDATORY segment: a bridge in the connectivity graph, i.e. one all
    # the current must cross. A segment in a parallel loop is skipped, so split-and-
    # reconverge no longer reads as one thin strand. Falls back to naive if no graph.
    meshed, comp, bott, bott_is_tap = False, None, naive, False
    if graph and segs:
        g = _net_graph(b, net)
        comp = g['comp'] if g else None
        bridge_idx = list(g['bridges']) if g else []
        # a bridge that only isolates a thermistor/test-point/pull-R leg is a
        # sense tap, not a series power path - the net's budgeted current has
        # no reason to run down it, so it's excluded before picking the
        # narrowest MANDATORY bottleneck (see _bridge_is_tap).
        real_idx = [i for i in bridge_idx if not _bridge_is_tap(b, g, i)]
        if real_idx:
            bott = min((g['segs'][i] for i in real_idx), key=lambda t: _seg_amp(b, t, a.dt))
        elif bridge_idx:
            bott = min((g['segs'][i] for i in bridge_idx), key=lambda t: _seg_amp(b, t, a.dt))
            bott_is_tap = True                  # every bridge left is a sense tap
        elif g:
            meshed = True                        # a full mesh: no single mandatory seg
    per_via = [via_current(d, a.plating / 1000.0, a.dt) for d in vd]
    farea = defaultdict(list)                   # real filled copper per layer (mm2)
    for fl in b.fills:
        if fl['net'] == net:
            farea[fl['layer']].append(fl['area'])
    poured = sorted(ly for ly, ar in farea.items() if sum(ar) > POUR_MIN)
    pour = {ly: (sum(farea[ly]), len(farea[ly]), max(farea[ly])) for ly in poured}
    bf = _bott_fields(b, bott, a.dt) if bott else {'i': 0.0, 'ly': '-', 'w': 0.0,
                                                   'mid': (0.0, 0.0), 'seg': 0.0, 'ext': False}
    nf = _bott_fields(b, naive, a.dt) if naive else bf
    pads = _net_pads(b, net)
    near = _nearest_pad_dist(pads, bf['mid'])
    # a short, wide stub landing right on a pad is a pad neck: IPC-2221's
    # long-trace steady-state formula overstates its thermal risk because the
    # pad copper (and, for a fine-pitch part, the part's own die/thermal pad)
    # sinks heat that a real long trace of this width could not.
    pad_neck = bool(bott) and bool(near) and 0 < bf['seg'] < 2 * bf['w'] and near[2] <= 1.0
    return {'net': net, 'need': need, 'layers': rows, 'total': total,
            'bott_i': bf['i'], 'bott_ly': bf['ly'], 'bott_mid': bf['mid'], 'bott_seg': bf['seg'],
            'bott_w': bf['w'], 'bott_is_tap': bott_is_tap, 'pad_neck': pad_neck,
            'naive_i': nf['i'], 'naive_ly': nf['ly'], 'naive_w': nf['w'], 'naive_mid': nf['mid'],
            'meshed': meshed, 'comp': comp, 'graphed': graph and bool(segs),
            'ends': sorted({r for r, _n, _x, _y in pads},
                           key=lambda r: (prefix(r) in ('C', 'R', 'TP', 'TH', 'FB'), natkey(r))),
            'bott_near': _nearest_pad(pads, bf['mid']),
            'need_w': ipc_width(need or 0, thick_of(b, bf['ly']), bf['ext'], a.dt) if bf['ly'] != '-' else 0.0,
            'bott_ext': bf['ext'],
            'need_w_outer': ipc_width(need or 0, thick_of(b, b.copper[0]), True, a.dt)
                            if b.copper and not bf['ext'] else 0.0,
            'vias': len(vd), 'via_bound': sum(per_via),
            'via_min': min(per_via) if per_via else 0.0, 'r': r, 'poured': poured,
            'pour': pour}

def _amp_verdicts(row, a):
    need = row['need']
    if need is None:
        return []
    if row['poured']:                    # a plane net: the pour carries it, not these stubs
        # area > POUR_MIN says a pour EXISTS, not that it is wide enough: a
        # fragmented fill or one thin neck can still be the real limiter
        shape = '; '.join(f"{ly} {ar:.0f} mm2 in {n} fragment(s), largest {100*mx/ar:.0f}%"
                          for ly, (ar, n, mx) in sorted(row['pour'].items()))
        return [('POURED', f"a pour carries it ({shape}). UNVERIFIED, not a pass: the "
                          f"pour's narrowest neck is not measured - check the path between "
                          f"the end pads in KiCad, and `zones` for fill state")]
    out = []
    if row['meshed']:                    # a full mesh: no single segment is mandatory
        if row['naive_i'] < need:
            out.append(('MESH-CHECK', f"no series bottleneck (fully meshed); narrowest single "
                                     f"seg is {row['naive_i']:.2f} A but current splits - "
                                     f"confirm the parallel copper sums >= {need:.2f} A"))
        return out
    if row['bott_i'] < need:
        mx, my = row['bott_mid']
        near = row['bott_near'] or f'{mx:.1f},{my:.1f}'
        if row['pad_neck']:
            out.append(('PAD-NECK', f"narrowest copper ({row['bott_seg']:.2f} mm long, "
                                    f"{row['bott_w']:.2f} mm wide) is a stub landing right on "
                                    f"{near} - IPC-2221's long-trace formula ({row['bott_i']:.2f} A) "
                                    f"overstates the risk here since the pad sinks heat locally. "
                                    f"Not a real TRACE-THIN unless the copper stays this narrow "
                                    f"past the pad."))
        elif row['bott_is_tap']:
            out.append(('MIXED-NET', f"every series bottleneck left after excluding thermistor/"
                                     f"test-point/pull-R taps is itself one, narrowest "
                                     f"{row['bott_i']:.2f} A near {near} - this net mixes a power "
                                     f"path with sense taps; the {need:.2f} A budget likely runs "
                                     f"through different copper than this tap. Verify visually."))
        else:
            msg = (f"bottleneck {row['bott_i']:.2f} A < {need:.2f} A on "
                   f"{row['bott_ly']}; widen to >= {row['need_w']:.2f} mm. "
                   f"Narrowest bridge {row['bott_seg']:.1f} mm seg near {near} "
                   f"(cursor to {mx:.1f},{my:.1f})")
            if not row['bott_ext'] and row['need_w'] > 2.0:
                msg += (f". {row['need_w']:.2f} mm on an inner layer is impractical - "
                        f"move this bridge to F.Cu/B.Cu instead (needs only "
                        f">= {row['need_w_outer']:.2f} mm there)")
            out.append(('TRACE-THIN', msg))
    # a thinner segment exists but is paralleled (not on the mandatory path)
    if row['naive_i'] < need and row['naive_i'] < row['bott_i'] - 1e-6:
        nx, ny = row['naive_mid']
        out.append(('PARALLEL-CHECK', f"a thinner {row['naive_w']:.2f} mm seg ({row['naive_i']:.2f} A) "
                                     f"at {nx:.1f},{ny:.1f} is paralleled, not mandatory - OK only "
                                     f"if its parallel group sums >= {need:.2f} A"))
    if row['vias'] and row['via_bound'] < need:
        want = math.ceil(need / row['via_min']) if row['via_min'] > 0 else 0
        out.append(('VIA-FEW', f"{row['vias']} via(s) ~{row['via_bound']:.2f} A parallel "
                               f"< {need:.2f} A; want ~{want} of this size"))
    if a.vdrop and need * row['r'] > a.vdrop:
        out.append(('LONG-DROP', f"Vdrop <= {need * row['r'] * 1000:.0f} mV @ {need:.2f} A "
                                 f"over {row['total']:.0f} mm (series upper bound)"))
    return out

def _print_amp(row, a):
    need, v = row['need'], _amp_verdicts(row, a)
    head = f"{row['net']}: "
    if need is not None:
        head += f"need {need:.2f} A   "
    if row['meshed']:
        kind = 'meshed, no series bottleneck'
    elif row['poured']:
        kind = (f"POURED - thinnest TRACK {row['bott_i']:.2f} A on {row['bott_ly']} is not "
                f"the net's capacity")
    else:
        tag = ' (narrowest bridge)' if row['graphed'] else ''
        near = f" near {row['bott_near']}" if row['bott_near'] else ''
        kind = f"bottleneck {row['bott_i']:.2f} A on {row['bott_ly']}{tag}{near}"
    head += f"routed {row['total']:.1f} mm   {kind}"
    if need is not None and not v:
        head += "   OK"
    print(head)
    if row['graphed'] and row['comp'] is not None:
        note = f" - copper is in {row['comp']} island(s); only the pour/pads join them" \
            if row['comp'] > 1 else ''
        naive_note = f"; narrowest single seg {row['naive_i']:.2f} A (paralleled)" \
            if row['naive_i'] < row['bott_i'] - 1e-6 else ''
        print(f"    graph: {len(row['ends'])} pad(s){note}{naive_note}")
    if row['ends']:
        landmarks = ' '.join(row['ends'][:10]) + (' ...' if len(row['ends']) > 10 else '')
        print(f"    find it: click any of these in KiCad to highlight the net -> {landmarks}")
    for ly, minw, ln, i, ext, mid, seg in row['layers']:
        mark = ('' if row['poured'] else '  <- bottleneck layer') if ly == row['bott_ly'] else ''
        print(f"    {ly:<8} len {ln:6.1f}  minw {minw:.3f}  ->  {i:5.2f} A  "
              f"({'external' if ext else 'internal'}){mark}")
    if row['vias']:
        print(f"    {'vias':<8} {row['vias']:>3} x        ->  {row['via_bound']:5.2f} A "
              f"parallel bound ({row['via_min']:.2f} A each)")
    if need is not None:
        print(f"    R<={row['r'] * 1000:.1f} mohm  Vdrop<={need * row['r'] * 1000:.0f} mV  "
              f"P<={need * need * row['r'] * 1000:.0f} mW  (series upper bound"
              + (", tracks only - the pour is ignored)" if row['poured'] else ")"))
    for tag, msg in v:
        print(f"    !! {tag}: {msg}")
    return v

def _amp_json(row):
    if not row:
        return None
    return {'net': row['net'], 'need_A': row['need'], 'routed_mm': round(row['total'], 2),
            'bottleneck_A': round(row['bott_i'], 3), 'bottleneck_layer': row['bott_ly'],
            'need_width_mm': round(row['need_w'], 3) if row['need'] else None,
            'bottleneck_at': [round(v, 2) for v in row['bott_mid']],
            'bottleneck_near': row['bott_near'], 'on_refs': row['ends'],
            'bottleneck_is_bridge': row['graphed'] and not row['meshed'],
            'bottleneck_is_pad_neck': row['pad_neck'], 'bottleneck_is_tap': row['bott_is_tap'],
            'narrowest_single_A': round(row['naive_i'], 3), 'meshed': row['meshed'],
            'components': row['comp'],
            'layers': [{'layer': l, 'minw_mm': w, 'len_mm': round(ln, 2), 'amp_A': round(i, 3),
                        'external': e} for l, w, ln, i, e, _m, _s in row['layers']],
            'vias': row['vias'], 'via_bound_A': round(row['via_bound'], 3),
            'r_mohm': round(row['r'] * 1000, 2),
            # when present, bottleneck_A is track-only and NOT the net's capacity
            'pour': {ly: {'area_mm2': round(ar, 1), 'fragments': n, 'largest_mm2': round(mx, 1)}
                     for ly, (ar, n, mx) in row['pour'].items()}}

def _amp_footer():
    print("\nIPC-2221: I = k*dT^0.44*A^0.725 (k=0.048 outer, 0.024 inner). Outer-layer\n"
          "numbers match the published charts and are trustworthy; the INNER-layer 0.024 k\n"
          "is very conservative - with adjacent GND planes (this board pours GND on all 4)\n"
          "real inner ampacity (IPC-2152) runs ~2-3x higher, so an internal TRACE-THIN\n"
          "overstates how thin it is. Thickness is read from the stackup, so fix the foil\n"
          "weight there if it is wrong.\n"
          "Bottleneck is the narrowest BRIDGE in the copper graph (endpoints merged, vias\n"
          "and shared pads stitched): a segment all the current must cross. A segment inside\n"
          "a parallel loop is skipped, so a split-and-reconverge no longer reads as one thin\n"
          "strand. Remaining blind spots: two traces that only cross mid-span with no shared\n"
          "end/via/pad are still separate copper here (KiCad would merge them); a parallel\n"
          "group whose widths individually pass but SUM short is flagged PARALLEL-CHECK for\n"
          "you to add up. Via bound is optimistic (all vias parallel). R/Vdrop/P are a\n"
          "SERIES UPPER BOUND, so a small bound is definitely fine and a large one just\n"
          "means trace the real source-to-load path by hand.")

def c_ampacity(b, a):
    """Current-carrying check on the ROUTED copper (not the schematic).

    Per net: IPC-2221 ampacity of the narrowest segment on each layer (the
    series bottleneck), the parallel current bound of its vias, and a
    series-upper-bound resistance / voltage drop for the length. Give a
    required current with `--amps X` on named nets, or a kpcb.json
    "current":{"NET":amps} budget, and it warns TRACE-THIN / VIA-FEW /
    LONG-DROP. With no budget it just reports capacity."""
    if not b.tracks:
        print(f"{b.path}: no routed tracks yet (nothing routed, or a pre-route board)")
        return 0
    jbud = {}
    for k, vv in (getattr(a, 'current', None) or {}).items():
        jbud[k] = _f(vv)
    named, fail = list(a.args) + list(a.net or []), 0

    if named:
        rows = []
        for net in named:
            need = a.amps if a.amps is not None else jbud.get(net)
            row = _amp_row(b, net, need, a, graph=True)
            if row is None:
                print(f"{net}: no routed copper on this net")
                continue
            rows.append(row)
        if a.json:
            print(json.dumps([_amp_json(r) for r in rows], indent=1))
            return 0
        print(f"{b.path}: trace ampacity  (IPC-2221, dT={a.dt:g} C, via plating {a.plating:g} um)\n")
        for row in rows:
            fail += any(t in ('TRACE-THIN', 'VIA-FEW') for t, _ in _print_amp(row, a))
            print()
        _amp_footer()
        return 2 if fail else 0

    # scan mode: verdict any budgeted net, then list the heaviest routed nets
    allnets = sorted({t['net'] for t in b.tracks if t['net']})
    budg = [n for n in allnets if n in jbud]
    if a.json:
        src = budg or allnets
        print(json.dumps([o for o in (_amp_json(_amp_row(b, n, jbud.get(n), a, graph=True))
                                      for n in src) if o], indent=1))
        return 0
    print(f"{b.path}: trace ampacity scan  (IPC-2221, dT={a.dt:g} C)\n")
    if budg:
        print("Budgeted nets (kpcb.json current{}):")
        for n in budg:
            fail += any(t in ('TRACE-THIN', 'VIA-FEW')
                        for t, _ in _print_amp(_amp_row(b, n, jbud[n], a, graph=True), a))
            print()
    geos = sorted((r for r in (_amp_row(b, n, None, a) for n in allnets) if r),
                  key=lambda r: -r['total'])
    print(f"Heaviest routed nets (capacity only, top {a.max}):")
    print(f"  {'net':<18} {'routed':>7}  {'bott':>6}  {'layer(minw)':<15} vias")
    for row in geos[:a.max]:
        minw = next((m for l, m, *_ in row['layers'] if l == row['bott_ly']), 0.0)
        tag = '  poured (plane carries it)' if row['poured'] else ''
        print(f"  {row['net']:<18} {row['total']:6.1f}  {row['bott_i']:5.2f} A  "
              f"{row['bott_ly'] + f'({minw:.2f})':<15} {row['vias']}{tag}")
    if not budg:
        print('\nNo current budget set -> capacity only, no warnings. Add per-net amps to\n'
              'kpcb.json "current":{"VSYS":2.7}, or run `ampacity VSYS --amps 2.7`, to get\n'
              'too-thin / too-few-vias / voltage-drop warnings.')
    _amp_footer()
    return 2 if fail else 0

def _via_in_pad(pad, vx, vy):
    """True if (vx,vy) lands inside the pad's copper rectangle. The pad's `at`
    angle in a .kicad_pcb is ABSOLUTE (the footprint rotation is already baked
    in - unlike a .kicad_mod, where it's relative), so undo just `prot`, not
    f.rot+prot, and test the via against the pad half-extents. A roundrect/oval/
    circle pad is treated as its bounding box - a hair generous at the corners,
    which is the safe direction for a manufacturing flag."""
    dx, dy = vx - pad['x'], vy - pad['y']
    th = math.radians(pad['prot'])
    c, s = math.cos(th), math.sin(th)
    u, v = c * dx - s * dy, s * dx + c * dy
    if abs(u) <= pad['sx'] / 2 and abs(v) <= pad['sy'] / 2:
        return True
    # a custom pad's copper is its anchor PLUS its primitives (pad-local frame)
    return any(point_in_poly((u, v), poly) for poly in pad.get('prims') or ())

def c_viapad(b, a):
    """Every component with a via centred inside one of its SMD pads (via-in-pad).
    Same-net = intentional via-in-pad (needs filled+capped/type-VII vias at the
    fab). Different-net = the via sits in a foreign pad -> possible short."""
    order = {n: i for i, n in enumerate(b.copper)}

    def spans(via, layer):                          # does the via reach the pad's layer?
        vi = [order[l] for l in via['layers'] if l in order]
        if not vi or layer not in order:
            return True                             # unknown -> assume yes (conservative)
        return min(vi) <= order[layer] <= max(vi)

    # bin vias into 1 mm cells so this isn't pads x vias
    grid = defaultdict(list)
    for vi, v in enumerate(b.vias):
        grid[(int(v['x']), int(v['y']))].append(vi)

    # ref -> pad_num -> {'pad': pad, 'vias': {via_index: mismatch}}. Keying vias by
    # index dedupes a via that a split sub-pad (same number) covers twice.
    hits = defaultdict(lambda: defaultdict(lambda: {'pad': None, 'vias': {}}))
    for f in b.fps.values():
        for pad in f.pads:
            if pad['kind'] != 'smd' or pad['sx'] <= 0 or pad['sy'] <= 0 or not pad['num']:
                continue  # numberless SMD slivers (paste/mechanical) aren't via-in-pad targets
            play = next((l for l in pad['layers'] if l.endswith('.Cu')), None)
            reach = int(pad['w'] / 2 + 1)
            seen = set()
            for cx in range(int(pad['x']) - reach, int(pad['x']) + reach + 1):
                for cy in range(int(pad['y']) - reach, int(pad['y']) + reach + 1):
                    for vi in grid.get((cx, cy), ()):
                        if vi in seen:
                            continue
                        seen.add(vi)
                        v = b.vias[vi]
                        if not spans(v, play) or not _via_in_pad(pad, v['x'], v['y']):
                            continue
                        mism = bool(pad['net'] and v['net'] and pad['net'] != v['net'])
                        slot = hits[f.ref][pad['num']]
                        slot['pad'] = pad
                        slot['vias'][vi] = mism

    sw = b.sw_nets()
    def role(net):                          # the netclass catches Net-(BT1-+) etc.
        n = (net or '').split('/')[-1]
        if GND_RE.match(n):
            return 'GND'
        return 'PWR' if (rail_voltage(n) is not None or len(b.nets.get(net, [])) > a.fanout
                         or net in sw or re.search(r'PWR|POWER', b.netclass(net), re.I)) else 'SIG'
    roles = defaultdict(int)
    for pads in hits.values():
        for s_ in pads.values():
            s_['role'] = role(s_['pad']['net'])
            roles[s_['role']] += 1
            # a foreign-net via (possible short) is never filtered out
            s_['show'] = any(s_['vias'].values()) or (
                (not a.signal or s_['role'] == 'SIG') and len(s_['vias']) >= a.min_vias)
    n_pads = sum(len(pads) for pads in hits.values())
    n_via = sum(len(s['vias']) for pads in hits.values() for s in pads.values())
    n_mism = sum(1 for pads in hits.values() for s in pads.values()
                 for m in s['vias'].values() if m)
    if a.json:
        out = {r: [{'pad': num, 'pad_net': s['pad']['net'], 'role': s['role'],
                    'vias': len(s['vias']),
                    'via_nets': sorted({b.vias[vi]['net'] for vi in s['vias']}),
                    'mismatch': any(s['vias'].values())}
                   for num, s in pads.items() if s['show']]
               for r, pads in hits.items()}
        print(json.dumps({'components': len(hits), 'pads': n_pads, 'vias': n_via,
                          'net_mismatches': n_mism, 'hits': out}, indent=2))
        return 2 if n_mism else 0
    if not hits:
        print("no via-in-pad: no via centre lands inside any SMD pad."); return 0
    shown = sum(1 for pads in hits.values() for s in pads.values() if s['show'])
    print(f"via-in-pad: {n_via} via(s) in {n_pads} SMD pad(s) across {len(hits)} "
          f"component(s)  [pads: {roles['GND']} GND / {roles['PWR']} PWR / {roles['SIG']} SIG]"
          + (f"\nshowing {shown} (--signal/--min filter; net mismatches always shown)"
             if shown < n_pads else '')
          + "\nSame-net via-in-pad needs filled+capped (or type-VII) vias - flag it in the "
            "fab quote.\n")
    for ref in sorted(hits, key=natkey):
        if not any(s['show'] for s in hits[ref].values()):
            continue
        f = b.fps[ref]
        print(f"  {ref:<6} {trunc(f.value, 22):<22} {'B.Cu' if f.back else 'F.Cu'}")
        for num in sorted(hits[ref], key=natkey):
            s = hits[ref][num]
            if not s['show']:
                continue
            p = s['pad']
            mnets = sorted({b.vias[vi]['net'] or '(none)' for vi, m in s['vias'].items() if m})
            note = (f"OK same net ({p['net']})" if not mnets else
                    f"!! via net {'/'.join(mnets)} != pad net {p['net']} - possible short")
            print(f"       pad {num:<4} {s['role']:<3} {len(s['vias']):>2} via(s)   {note}")
    if n_mism:
        print(f"\n{n_mism} via(s) sit in a pad of a DIFFERENT net - that is a short, "
              f"not via-in-pad. Confirm with `kdrc.py {os.path.basename(a.file)}`.")
    return 2 if n_mism else 0

# ---------------- pad / net geometry ----------------

def _find_pad(b, spec):
    """'F5.1' -> (footprint, pad), or None. Several pads may share a number
    (split thermal pads); the first is returned, the rest share its net."""
    m = re.match(r'^([A-Za-z]+\d+)\.(\S+)$', spec)
    f = b.fps.get(m.group(1)) if m else None
    return next(((f, p) for p in f.pads if p['num'] == m.group(2)), None) if f else None

def _resolve_net(b, name):
    """Exact net name, else a unique match on the last path component or the
    unescaped display form ('LORA_ANT' -> '/Root/LoRa/LORA_ANT'). Returns
    (net, candidates)."""
    allnets = set(b.nets) | {t['net'] for t in b.tracks if t['net']}
    if name in allnets:
        return name, []
    c = sorted(n for n in allnets if n.split('/')[-1] == name or unesc_disp(n) == name)
    if len(c) == 1:
        return c[0], []
    return None, c or sorted(n for n in allnets if name.lower() in n.lower())[:8]

def c_net(b, a):
    """Every pad on a net with its absolute position, plus the routed copper
    per layer, vias, zones and the physical extent - pad-level geometry that
    `where REF` (footprint level) can't answer."""
    names = list(a.args) + list(a.net or [])
    if not names:
        print("net needs a net name (use --net=-BATT for a leading dash)", file=sys.stderr); return 1
    rc = 0
    for name in names:
        net, cand = _resolve_net(b, name)
        if not net:
            print(f"{name}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else ''))
            rc = 1; continue
        pads = sorted(((f, p) for f in b.fps.values() for p in f.pads if p['net'] == net),
                      key=lambda fp: (natkey(fp[0].ref), natkey(fp[1]['num'])))
        segs = [t for t in b.tracks if t['net'] == net]
        vias = [v for v in b.vias if v['net'] == net]
        fills = [fl for fl in b.fills if fl['net'] == net]
        print(f"\n=== {unesc_disp(net)}   netclass {b.netclass(net) or 'Default'}   "
              f"{len(pads)} pad(s), {len(segs)} track seg(s), {len(vias)} via(s)")
        for f, p in pads[:a.max * 3]:
            ly = '/'.join(l.split('.')[0] for l in p['layers'] if l.endswith('.Cu'))
            print(f"  {f.ref + '.' + p['num']:<10} {p['x']:8.3f} {p['y']:8.3f}  {ly:<6} "
                  f"{p['sx']:.2f}x{p['sy']:.2f} {trunc(p['fn'], 16)}")
        if len(pads) > a.max * 3:
            print(f"  ... +{len(pads) - a.max * 3} more pad(s)")
        for ly in b.copper:
            ss = [t for t in segs if t['layer'] == ly]
            if ss:
                ws = sorted({t['w'] for t in ss})
                print(f"  {ly:<7} {len(ss):>3} seg  {sum(t['len'] for t in ss):7.2f} mm  width "
                      + (f"{ws[0]:.3f}" if len(ws) == 1 else f"{ws[0]:.3f}..{ws[-1]:.3f}"))
        if vias:
            dr = sorted({v['drill'] for v in vias})
            print(f"  vias    {len(vias):>3}      drill {' '.join(f'{d:g}' for d in dr)} mm")
        by = defaultdict(list)
        for fl in fills:
            by[fl['layer']].append(fl['area'])
        for ly, ar in sorted(by.items()):
            print(f"  zone    {ly:<7} {sum(ar):7.1f} mm2 in {len(ar)} fragment(s)")
        xy = [(p['x'], p['y']) for _, p in pads] + [q for t in segs for q in (t['a'], t['b'])] \
            + [(v['x'], v['y']) for v in vias]
        if xy:
            e = bbox(xy)
            print(f"  extent  {e[2]-e[0]:.1f} x {e[3]-e[1]:.1f} mm  "
                  f"({e[0]:.1f},{e[1]:.1f} .. {e[2]:.1f},{e[3]:.1f})")
    return rc

# ---------------- RF traces ----------------

RF_FREQ = ((re.compile(r'GNSS|GPS|L1|L5', re.I), 1575.42),     # sheet/net -> MHz, for lambda/20
           (re.compile(r'LORA|915|SX12', re.I), 915.0))

def _ref_below(b, layer):
    """(next copper layer, dielectric height mm, thickness-weighted er) toward
    the board centre from an OUTER layer, from the stackup."""
    names = [n for n, *_ in b.stack]
    if layer not in names:
        return None
    i = names.index(layer)
    step = 1 if layer == (b.copper[0] if b.copper else 'F.Cu') else -1
    h = her = 0.0
    j = i + step
    while 0 <= j < len(b.stack) and b.stack[j][1] != 'copper':
        _, _, th, er = b.stack[j]
        h += th; her += th * er
        j += step
    if not (0 <= j < len(b.stack)) or h <= 0:
        return None
    return b.stack[j][0], h, her / h

def _mask_on(b, layer):
    """(thickness mm, er) of the solder mask over an outer copper layer, from
    the stackup, or None."""
    names = [n for n, *_ in b.stack]
    if layer not in names:
        return None
    i = names.index(layer)
    for j in (i - 1, i + 1):
        if 0 <= j < len(b.stack) and b.stack[j][0].endswith('.Mask') and b.stack[j][2] > 0:
            return b.stack[j][2], b.stack[j][3] or 3.8
    return None

def _side_gap(t, fills, w):
    """Per-side gap (left, right) in mm from a trace segment's midpoint to the
    nearest edge of a same-layer GND fill: the CPWG slot width. None = no fill
    edge within 2 mm on that side."""
    (ax, ay), (bx, by) = t['a'], t['b']
    mx, my = t['mid']
    gaps = [None, None]
    for fl in fills:
        x0, y0, x1, y1 = fl['bbox']
        if mx < x0 - 2 or mx > x1 + 2 or my < y0 - 2 or my > y1 + 2:
            continue
        pts = fl['pts']
        for i in range(len(pts)):
            p, q = pts[i - 1], pts[i]
            if min(p[0], q[0]) > mx + 2 or max(p[0], q[0]) < mx - 2 or \
               min(p[1], q[1]) > my + 2 or max(p[1], q[1]) < my - 2:
                continue
            vx, vy = q[0] - p[0], q[1] - p[1]
            L = vx * vx + vy * vy
            u = 0.0 if L <= 0 else max(0.0, min(1.0, ((mx - p[0]) * vx + (my - p[1]) * vy) / L))
            cx, cy = p[0] + u * vx, p[1] + u * vy
            d = math.hypot(cx - mx, cy - my) - w / 2
            if d > 2 or d < -1e-6:
                continue
            side = 0 if (bx - ax) * (cy - ay) - (by - ay) * (cx - ax) < 0 else 1
            if gaps[side] is None or d < gaps[side]:
                gaps[side] = d
    return gaps

def c_rf(b, a):
    """`rf [NET...]`: one-call review of a 50-ohm trace. With no net, every net
    whose netclass names RF/50 ohm. Per net: routed width uniformity (necks),
    microstrip Zo from the stackup plus the same-layer GND gap (coplanar
    coupling), reference-plane coverage under the trace, and the GND via fence."""
    names = list(a.args) + list(a.net or [])
    if not names:
        names = sorted({n for n in set(b.nets) | {t['net'] for t in b.tracks}
                        if n and re.search(r'RF|50', b.netclass(n), re.I)}, key=natkey)
        if not names:
            print("no net in an RF/50-ohm netclass - name one: `rf NET`", file=sys.stderr); return 1
    fence = a.fence
    rc = 0
    for name in names:
        net, cand = _resolve_net(b, name)
        if not net:
            print(f"{name}: no such net" + (f"; did you mean {' | '.join(cand)}" if cand else ''))
            rc = 1; continue
        segs = [t for t in b.tracks if t['net'] == net and t['w'] > 0]
        refs = sorted({r for r, _ in b.nets.get(net, [])}, key=natkey)
        sheets = ' '.join(sorted({b.fps[r].sheet for r in refs if r in b.fps}))
        f_mhz = a.freq or next((mhz for rx, mhz in RF_FREQ if rx.search(net + ' ' + sheets)), None)
        print(f"\n=== {unesc_disp(net)}   netclass {b.netclass(net) or 'Default'}   pads "
              f"{' '.join(refs[:8])}   {sheets}"
              + (f"   f {f_mhz:g} MHz{'' if a.freq else ' (inferred; --freq to set)'}" if f_mhz else ''))
        if not segs:
            print("  no routed track (pads only, or joined by pour)"); continue
        eeff_net = None
        for ly in b.copper:
            ss = [t for t in segs if t['layer'] == ly]
            if not ss:
                continue
            bylen = defaultdict(float)
            for t in ss:
                bylen[round(t['w'], 4)] += t['len']
            dom = max(bylen, key=bylen.get)
            tot = sum(bylen.values())
            widths = ', '.join(f"{w:.3f} x {l:.1f} mm" for w, l in sorted(bylen.items()))
            print(f"  {ly:<7} {len(ss)} seg, {tot:.1f} mm   widths: {widths}")
            necks = sorted((t for t in ss if t['w'] < dom * 0.95), key=lambda t: t['w'])
            for t in necks[:4]:
                print(f"    !! NECK {t['w']:.3f} mm (dominant {dom:.3f}) for {t['len']:.2f} mm "
                      f"at {t['mid'][0]:.2f},{t['mid'][1]:.2f}")
            if ly not in b.outer:
                print("    Zo: inner-layer stripline, not modelled here"); continue
            ref = _ref_below(b, ly)
            if not ref:
                print("    Zo: no stackup in the board file"); continue
            rly, h, er = ref
            z, eeff = microstrip(dom, h, er, thick_of(b, ly))
            eeff_net = eeff_net or eeff
            gnd_same = [fl for fl in b.fills if fl['layer'] == ly and GND_RE.match(fl['net'].split('/')[-1] or '')]
            gl, gr = [], []
            for t in ss:
                if t['len'] >= 0.2:
                    l_, r_ = _side_gap(t, gnd_same, t['w'])
                    if l_ is not None: gl.append(l_)
                    if r_ is not None: gr.append(r_)
            med = lambda v: sorted(v)[len(v) // 2] if v else None
            fmt2 = lambda x: '-' if x is None else f"{x:.2f}"
            print(f"    Zo {z:.1f} ohm microstrip ({dom:.3f} mm over {rly}, h {h:.3f} er {er:.2f}, "
                  f"closed form, uncoated)")
            # Side grounds within a few h pull Zo down (CPWG). The closed forms
            # for that need h >> w+2s, the opposite of a 4-layer board, so it is
            # field-solved (kzo.py) with the median gap per side and the mask.
            if gl or gr:
                sl, sr = med(gl), med(gr)
                near = [x for x in (sl, sr) if x is not None and x < 5 * h]
                print(f"    same-layer GND gap L {fmt2(sl)} / R {fmt2(sr)} mm (min {min(gl + gr):.2f})"
                      + ("" if near else "  -> >= 5h away: the microstrip figure holds"))
                if near:
                    # 0.01 mm steps (~0.1 ohm): nets and sides then share solves
                    gaps = tuple(round(x, 2) if x is not None and x < 5 * h else None for x in (sl, sr))
                    mask = _mask_on(b, ly)
                    zc, ee = field_zo(dom, h, er, thick_of(b, ly), s=gaps, mask=mask)
                    zu = field_zo(dom, h, er, thick_of(b, ly), s=gaps)[0] if mask else None
                    eeff_net = ee
                    print(f"    Zo {zc:.1f} ohm CPWG, field-solved"
                          + (f" with {ly[0]}.Mask {mask[0] * 1000:.0f} um er {mask[1]:g}"
                             f" ({zu:.1f} uncoated)" if mask else ', uncoated'))
            # reference plane continuity under the trace
            rfills = [fl for fl in b.fills if fl['layer'] == rly]
            samp = [q for t in ss for q in (t['a'], t['mid'], t['b'])]
            under = defaultdict(int)
            for q in samp:
                hitn = next((fl['net'] for fl in rfills if fl['bbox'][0] <= q[0] <= fl['bbox'][2]
                             and fl['bbox'][1] <= q[1] <= fl['bbox'][3] and point_in_poly(q, fl['pts'])), None)
                under[hitn] += 1
            desc = ', '.join(f"{'NOTHING' if n is None else n} {100*c/len(samp):.0f}%"
                             for n, c in sorted(under.items(), key=lambda kv: -kv[1]))
            bad = under.get(None, 0) or any(n and not GND_RE.match(n.split('/')[-1]) for n in under)
            print(f"    reference {rly} under the trace: {desc}"
                  + ("   <-- BROKEN/NON-GND RETURN PATH" if bad else ''))
            rc = rc or (2 if bad else 0)
        # GND via fence: vias within `fence` mm of the trace edge, per side
        side_v = ([], [])
        for v in b.vias:
            if not GND_RE.match((v['net'] or '').split('/')[-1] or ''):
                continue
            best = min(((pt_seg_dist((v['x'], v['y']), t['a'], t['b']) - t['w'] / 2, t) for t in segs),
                       key=lambda x: x[0])
            if best[0] <= fence:
                t = best[1]
                (ax, ay), (bx, by) = t['a'], t['b']
                sd = 0 if (bx - ax) * (v['y'] - ay) - (by - ay) * (v['x'] - ax) < 0 else 1
                side_v[sd].append((v['x'], v['y']))
        def maxnn(vs):
            return max((min(math.dist(p, q) for q in vs if q is not p) for p in vs), default=None) \
                if len(vs) > 1 else None
        nl, nr = maxnn(side_v[0]), maxnn(side_v[1])
        lim = (299792.458 / f_mhz / math.sqrt(eeff_net or 1) / 20) if f_mhz else None
        fmt = lambda x: '-' if x is None else f"{x:.1f}"
        verdict = ''
        if lim:
            worst = max((x for x in (nl, nr) if x is not None), default=None)
            verdict = (f"   lambda/20 = {lim:.1f} mm: " +
                       ('NO FENCE' if not side_v[0] and not side_v[1] else
                        'one side unfenced' if not (side_v[0] and side_v[1]) else
                        'OK' if worst is not None and worst <= lim else 'GAPS WIDER THAN lambda/20'))
        print(f"  GND fence (<= {fence:g} mm from the trace edge): {len(side_v[0]) + len(side_v[1])} vias, "
              f"L {len(side_v[0])} / R {len(side_v[1])}, widest nearest-neighbour gap "
              f"L {fmt(nl)} / R {fmt(nr)} mm{verdict}")
    print("\nZo: microstrip is Hammerstad's closed form (uncoated, ~1%); CPWG is a 2D field solve\n"
          "(kzo.py, ~1% vs exact cases) on the board file's stackup, rectangular copper, median\n"
          "gap per side. The fab's stackup (and its etch trapezoid) is the arbiter: check h and er\n"
          "above match it. Fills are the LAST SAVED state. Fence gaps are nearest-neighbour\n"
          "spacing along each side, a proxy for pitch on a bent trace.")
    return rc

# ---------------- 3D height (Z) ----------------

def _mat(node):
    """glTF node -> 4x4 row-major local transform (matrix, or T*R*S)."""
    if 'matrix' in node:
        m = node['matrix']                              # column-major
        return [[m[c * 4 + r] for c in range(4)] for r in range(4)]
    x, y, z, w = node.get('rotation', [0, 0, 0, 1])
    sx, sy, sz = node.get('scale', [1, 1, 1])
    tx, ty, tz = node.get('translation', [0, 0, 0])
    R = [[1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)],
         [2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)],
         [2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)]]
    return [[R[0][0] * sx, R[0][1] * sy, R[0][2] * sz, tx],
            [R[1][0] * sx, R[1][1] * sy, R[1][2] * sz, ty],
            [R[2][0] * sx, R[2][1] * sy, R[2][2] * sz, tz], [0, 0, 0, 1]]

def _mul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]

def heights(b):
    """({ref: height mm above its own board face}, [missing model files]).

    From KiCad's own GLB export: OCCT meshes each STEP model and places it, so
    this is the real model geometry, not a package-name guess. glTF is Y-up in
    metres and each part's node origin sits on its board face. A part with no
    loadable model is simply absent from the dict - never read that as 0 mm."""
    import struct, subprocess, tempfile
    from kcommon import kicad_cli
    with tempfile.TemporaryDirectory() as td:
        out = os.path.join(td, 'b.glb')
        r = subprocess.run([kicad_cli(b.path), 'pcb', 'export', 'glb', '--no-board-body',
                            '--no-dnp', '-f', '-o', out, b.path], capture_output=True, text=True)
        if not os.path.exists(out):
            raise RuntimeError((r.stderr or r.stdout)[-400:])
        blob = open(out, 'rb').read()
    missing = sorted(set(re.findall(r'File not found: (\S+)', r.stdout + r.stderr)))
    j = json.loads(blob[20:20 + struct.unpack('<I', blob[12:16])[0]])
    nodes, meshes, acc = j['nodes'], j.get('meshes', []), j.get('accessors', [])
    def walk(i, W, ys):
        n = nodes[i]
        W = _mul(W, _mat(n))
        for pr in (meshes[n['mesh']]['primitives'] if 'mesh' in n else []):
            a_ = acc[pr['attributes']['POSITION']]
            lo, hi = a_.get('min'), a_.get('max')
            if lo and hi:
                for cx in (lo[0], hi[0]):
                    for cy in (lo[1], hi[1]):
                        for cz in (lo[2], hi[2]):
                            ys.append(W[1][0] * cx + W[1][1] * cy + W[1][2] * cz + W[1][3])
        for c in n.get('children', []):
            walk(c, W, ys)
    out = {}
    root = j['scenes'][j.get('scene', 0)]['nodes']
    for ri in root:
        W0 = _mat(nodes[ri])
        for ci in nodes[ri].get('children', []):
            ref = nodes[ci].get('name')
            f = b.fps.get(ref)
            if not f:
                continue
            ys = []
            walk(ci, W0, ys)
            if ys:
                oy = _mul(W0, _mat(nodes[ci]))[1][3]
                out[ref] = 1000 * ((oy - min(ys)) if f.back else (max(ys) - oy))
    return out, missing

def c_height(b, a):
    """`height [REF...]`: 3D model height per part, and the board's Z stack -
    at each tall back-side part (the cells): its height + board + the tallest
    front part over its courtyard. The pocket-thickness budget in one call."""
    try:
        H, missing = heights(b)
    except Exception as e:
        print(f"GLB export failed: {e}", file=sys.stderr); return 3
    real = [f for f in b.fps.values() if f.placed and not f.dnp and not b.is_hole(f)]
    cfgd = {k: _f(v) for k, v in (a.height_cfg or {}).items() if k in b.fps}
    H.update(cfgd)                                  # kpcb.json "height": measured > model
    # model-less footprints that are only copper (jumpers, net ties, test pads/holes)
    flat = sorted((f.ref for f in real if f.ref not in H and
                   re.search(r'SolderJumper|NetTie|TestPoint|Fiducial', f.fp, re.I)), key=natkey)
    H.update({r: 0.0 for r in flat})
    nomodel = sorted((f.ref for f in real if f.ref not in H), key=natkey)
    gone = {os.path.basename(m) for m in missing}
    partial = sorted((f.ref for f in real if f.ref in H and f.ref not in cfgd
                      and any(os.path.basename(m) in gone for m in f.models)), key=natkey)
    holder = lambda r: prefix(r) == 'BT' or re.search(r'BatteryHolder|BAT-SMD', b.fps[r].fp)
    thick = sum(t for _, ty, t, _ in b.stack if ty in ('copper', 'core', 'prepreg')) or 1.6
    fmt = lambda r: (f"{r} {H[r]:.2f}" + ('*' if r in cfgd else '')) if r in H else f"{r} ?"
    if a.args:
        for ref in a.args:
            f = b.fps.get(ref)
            if not f:
                print(f"{ref}: no such footprint"); continue
            miss = [os.path.basename(m) for m in f.models if os.path.basename(m) in gone]
            print(f"{ref:<6} {'B' if f.back else 'F'}  " +
                  (f"{H[ref]:.2f} mm above its face" + (' (kpcb.json)' if ref in cfgd else '')
                   if ref in H else "NO 3D MODEL loaded - height UNKNOWN") + f"   {trunc(f.fp, 50)}"
                  + (f"   !! not found: {' '.join(miss)}" if miss else ''))
        return 0
    print(f"{b.path}: 3D heights (KiCad GLB export of the STEP models; board {thick:.3f} mm)")
    for side, back in (('front', False), ('back', True)):
        top = sorted((r for r in H if b.fps[r].back == back), key=lambda r: -H[r])
        print(f"  {side:<5} tallest: " + ', '.join(fmt(r) for r in top[:a.max]))
    if cfgd:
        print(f"  * = kpcb.json \"height\" override: {' '.join(f'{k}={v:g}' for k, v in cfgd.items())}")
    if flat:
        print(f"\n  assumed flat copper, no model ({len(flat)}): {trunc(refrange(flat), 200)}"
              f"  - a header or probe pin fitted to a TH test point adds height")
    if nomodel:
        print(f"\n  NO 3D MODEL ({len(nomodel)}) - height UNKNOWN, not zero: "
              f"{trunc(' '.join(nomodel), 300)}")
    if missing:
        print(f"  model files not found: {trunc(' '.join(sorted(gone)), 300)}")
    if partial:
        print(f"  PARTIAL (one of several models missing, height may be low): {' '.join(partial)}")
    print("\nZ stack (back part + board + tallest front part over its courtyard):")
    worst = None
    for r in sorted((r for r in H if b.fps[r].back), key=lambda r: -H[r])[:4]:
        g = b.fps[r]
        over = [f for f in real if not f.back and hit(f.crtyd, g.crtyd)]
        known = [f for f in over if f.ref in H]
        tf = max(known, key=lambda f: H[f.ref]) if known else None
        z = H[r] + thick + (H[tf.ref] if tf else 0)
        unk = [f.ref for f in over if f.ref not in H]
        worst = max(worst or 0, z)
        print(f"  at {r:<5} {H[r]:6.2f} + {thick:.2f} + {(H[tf.ref] if tf else 0):5.2f}"
              f" ({tf.ref if tf else 'nothing over it'}) = {z:6.2f} mm"
              + (f"   + UNKNOWN from {' '.join(unk[:6])}" if unk else '')
              + ("\n        !! battery-holder MODEL height: it may not include the cell (a 21700"
                 " is 21.7 mm across).\n        Measure the seated cell top and set kpcb.json "
                 f"{{\"height\": {{\"{r}\": MM}}}}" if holder(r) and r not in cfgd else ''))
    fr = [H[r] for r in H if not b.fps[r].back]
    bk = [H[r] for r in H if b.fps[r].back]
    if fr and bk:
        print(f"  whole-board bound (tallest back + board + tallest front): "
              f"{max(bk) + thick + max(fr):.2f} mm")
    print("\nHeights are model geometry as placed (incl. the model's own offset), meshed by\n"
          "OCCT - accurate to its tessellation (~0.01 mm). A part with no model is UNKNOWN.\n"
          "Enclosure, gasket, display and standoffs are not on the board and not counted.")
    return 0

CMDS = {'summary': c_summary, 'check': c_check, 'where': c_where, 'map': c_map,
        'sheet': c_sheet, 'unplaced': c_unplaced, 'ic': c_ic, 'span': c_span,
        'zones': c_zones, 'sync': c_sync, 'review': c_review, 'ampacity': c_ampacity,
        'viapad': c_viapad, 'net': c_net, 'rf': c_rf, 'height': c_height}

def main():
    ap = argparse.ArgumentParser(add_help=False)
    ap.add_argument('file')
    ap.add_argument('cmd', choices=list(CMDS))
    ap.add_argument('args', nargs='*')
    ap.add_argument('-r', '--radius', type=float, default=5.0,
                    help='for `where`: neighbour search radius in mm (5)')
    ap.add_argument('--max', type=int, default=12,
                    help='max lines per rule / per neighbour list before a +N tail (12)')
    ap.add_argument('--cols', type=int, default=48, help='for `map`: grid width (48)')
    ap.add_argument('--side', default='f', help='for `map`: f (front, default) or b (back)')
    for name, dflt, hlp in (('edge', 0.5, 'courtyard-to-board-edge minimum, mm'),
                            ('hole', 1.5, 'keepout beyond a mounting hole pad radius, mm'),
                            ('conn', 10.0, 'max connector distance from an edge, mm'),
                            ('rf', 8.0, 'RF part to switching node, mm'),
                            ('therm', 8.0, 'heat source to heat-sensitive part, mm'),
                            ('bypass', 3.0, 'supply pin to its bypass cap, mm'),
                            ('clear', 0.0, 'extra margin on every courtyard test, mm'),
                            ('gap', 0.25, 'for `ic`: courtyard gap in a suggestion, mm'),
                            ('fb', 4.0, 'for `ic`: feedback part to switching node, mm'),
                            ('tol', 1.0, 'for `ic`: slop allowed before a placed part '
                                         'stops reading OK, mm'),
                            ('span', 0.0, 'NETSPAN threshold, mm (0 = half the board diagonal)'),
                            ('fanout', 8.0, 'a net with more nodes than this is a rail')):
        ap.add_argument('--' + name, type=float, default=None, help=hlp + f' ({dflt:g})')
    ap.add_argument('--anchor', default='',
                    help="for `ic`: hang the layout off this already-placed part "
                         "instead of the IC ('none' to disable the automatic choice)")
    ap.add_argument('--cin', default='', help="for `ic`: force the input caps, e.g. --cin C122,C124")
    ap.add_argument('--cout', default='', help="for `ic`: force the output caps")
    ap.add_argument('--ncin', type=int, default=3, help='for `ic`: how many input caps to place off a rail (3)')
    ap.add_argument('--ncout', type=int, default=3, help='for `ic`: how many output caps to place off a rail (3)')
    ap.add_argument('--assoc', type=int, default=6, help="for `ic`: a net with more nodes than this is a rail, not the IC's own net (6)")
    ap.add_argument('--amps', type=float, default=None,
                    help='for `ampacity`: required current on the named net(s), A')
    ap.add_argument('--dt', type=float, default=10.0,
                    help='for `ampacity`: allowed temperature rise, C (10)')
    ap.add_argument('--plating', type=float, default=20.0,
                    help='for `ampacity`: via barrel plating thickness, um (20)')
    ap.add_argument('--vdrop', type=float, default=0.25,
                    help='for `ampacity`: flag if series-bound Vdrop exceeds this, V (0.25)')
    ap.add_argument('--net', action='append', default=[],
                    help='for `ampacity`: net name, repeatable. For a name starting with '
                         '"-", use =, e.g. --net=-BATT (a space before the dash still '
                         'confuses argparse, as does the positional arg)')
    ap.add_argument('--freq', type=float, default=None,
                    help='for `rf`: signal frequency in MHz, for the lambda/20 fence check '
                         '(default: inferred from the net/sheet name)')
    ap.add_argument('--fence', type=float, default=1.5,
                    help='for `rf`: a GND via this close to the trace edge counts as fence, mm (1.5)')
    ap.add_argument('--signal', action='store_true',
                    help='for `viapad`: hide GND/power pads (routine drops), keep signal pads')
    ap.add_argument('--min', dest='min_vias', type=int, default=1,
                    help='for `viapad`: only pads holding >= N vias, e.g. 4 for thermal pads (1)')
    ap.add_argument('--only', default='')
    ap.add_argument('--skip', default='')
    ap.add_argument('--json', action='store_true')
    ap.add_argument('--rules', action='store_true', help='print the check rule legend')
    ap.add_argument('--no-suppress', action='store_true',
                    help="for `check`: ignore kpcb.json's suppress list")
    ap.add_argument('-h', '--help', action='store_true')
    a = ap.parse_args()
    if a.help:
        print(__doc__); return
    cfg = {}
    try:
        cp = os.path.join(os.path.dirname(os.path.abspath(a.file)), 'kpcb.json')
        if os.path.exists(cp):
            cfg = json.load(open(cp))
    except Exception as e:
        print(f"(ignoring malformed kpcb.json: {e})", file=sys.stderr)
    for name, dflt in (('edge', 0.5), ('hole', 1.5), ('conn', 10.0), ('rf', 8.0),
                       ('therm', 8.0), ('bypass', 3.0), ('clear', 0.0), ('gap', 0.25),
                       ('fb', 4.0), ('tol', 1.0), ('span', 0.0), ('fanout', 8.0)):
        if getattr(a, name) is None:
            try:
                setattr(a, name, float(cfg.get(name, dflt)))
            except (TypeError, ValueError):
                setattr(a, name, dflt)
    a.current = cfg.get('current') if isinstance(cfg.get('current'), dict) else {}
    a.height_cfg = cfg.get('height') if isinstance(cfg.get('height'), dict) else {}
    a.suppress = {}
    if not a.no_suppress:
        for entry in cfg.get('suppress') or []:
            rule, _, tok = str(entry).partition(':')
            a.suppress.setdefault(rule.strip().upper(), set()).add(tok.strip())
    try:
        b = Board(a.file)
    except FileNotFoundError:
        print(f"no such board: {a.file}", file=sys.stderr); return 3
    except ValueError as e:
        print(str(e), file=sys.stderr); return 3
    if not a.span and b.outline:
        a.span = 0.5 * math.hypot(b.outline[2] - b.outline[0], b.outline[3] - b.outline[1])
    return CMDS[a.cmd](b, a) or 0

try:
    import signal
    signal.signal(signal.SIGPIPE, signal.SIG_DFL)
except Exception:
    pass

if __name__ == '__main__':
    sys.exit(main() or 0)
