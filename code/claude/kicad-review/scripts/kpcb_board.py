"""Board model for kpcb.py: part classification, geometry helpers, IPC-2221 math, FP and Board."""
import os, re, sys, json, math, glob
from collections import defaultdict
from functools import cached_property
from kcommon import (load_sexp, kids, kid, val, has, prefix, parse_value, unesc_disp,
                     rail_voltage, rules_warning)

# ---------------- part classification ----------------
# Small and deliberately greppable: these are the knobs to turn when a check
# misfires on a board whose parts this tool has never seen.

RF_NET   = re.compile(r'(^|[/_])(ANT|RF|LNA|RFIN|RFOUT|VCC_RF)(_|$|\d)', re.I)
RF_FP    = re.compile(r'Connector_Coaxial|RF_Module|RF_GPS|Antenna|U\.?FL|SMA_', re.I)
RF_VAL   = re.compile(r'\b(NEO-M9|NEO-M8|SX12\d\d|E22|ESP32|BALUN|SAW)', re.I)
SW_PIN   = re.compile(r'^(SW|LX|PH|VSW|SWITCH|VLX)\d*$', re.I)
# E22P / E22-900M30S style: an RF PA module at >= 27 dBm dissipates ~2 W on TX
HOT_VAL  = re.compile(r'\b(BQ25\d|LM6146|TPS2594|TPS6\d|TPS7A|AP63\d|MP\d{4}|TPS55'
                      r'|E\d{2,3}P?-\d+M(2[7-9]|3\d))', re.I)
SENS_FP  = re.compile(r'Crystal|Oscillator|BatteryHolder|BAT-SMD', re.I)
SENS_VAL = re.compile(r'\b(NEO-M9|NEO-M8|NEO-F10|32\.768|TCXO)', re.I)
THERM_PFX = ('TH', 'NTC', 'RT')   # thermistors: THERMAL checks them as a measurement bias
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
    return math.hypot(max(0.0, a[0] - b[2], b[0] - a[2]), max(0.0, a[1] - b[3], b[1] - a[3]))

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

def seg_seg_dist(p, q, r, s):
    """distance between segments pq and rs (0 when they cross)"""
    o = lambda a, b, c: (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])   # noqa: E731
    if o(r, s, p) * o(r, s, q) < 0 and o(p, q, r) * o(p, q, s) < 0:
        return 0.0
    return min(pt_seg_dist(p, r, s), pt_seg_dist(q, r, s), pt_seg_dist(r, p, q), pt_seg_dist(s, p, q))

def seg_closest(p, a, b):
    """the point of segment a-b nearest p"""
    dx, dy = b[0] - a[0], b[1] - a[1]
    L2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / L2)) if L2 else 0.0
    return (a[0] + t * dx, a[1] + t * dy)

def ray_hit(p, d, segs):
    """distance along unit direction d from p to the first segment it crosses, or None"""
    best = None
    for a, b in segs:
        ex, ey = b[0] - a[0], b[1] - a[1]
        den = d[0] * ey - d[1] * ex
        if abs(den) < 1e-12:
            continue
        t = ((a[0] - p[0]) * ey - (a[1] - p[1]) * ex) / den
        u = ((a[0] - p[0]) * d[1] - (a[1] - p[1]) * d[0]) / den
        if t > -1e-9 and -1e-9 <= u <= 1 + 1e-9 and (best is None or t < best):
            best = t
    return best

def pad_box(p):
    """a pad's copper as an axis-aligned box in board mm (anchor + custom primitives)"""
    t = math.radians(p['prot'])
    hx = abs(p['sx'] / 2 * math.cos(t)) + abs(p['sy'] / 2 * math.sin(t))
    hy = abs(p['sx'] / 2 * math.sin(t)) + abs(p['sy'] / 2 * math.cos(t))
    pts = [(p['x'] - hx, p['y'] - hy), (p['x'] + hx, p['y'] + hy)]
    pts += [xf(u, v, p['x'], p['y'], p['prot']) for poly in p['prims'] for u, v in poly]
    return bbox(pts)

def text_box(x, y, ang, txt, node, k=1.0):
    """estimated board box of a KiCad text at x,y (its anchor), absolute angle"""
    return text_parts(x, y, ang, txt, node, k)[0]

def text_parts(x, y, ang, txt, node, k=1.0):
    """(box, parts) of a KiCad text at x,y (its anchor), absolute angle. box: 0.9 x font
    width per character (KiCad's own text box, the plot's textLength), 1.6 x height per line,
    placed by its (justify). parts: the boxes ink covers, so a pad in a word gap is clear. A
    TrueType text's render_cache is KiCad's own outline in board coordinates: one box per
    glyph polygon, exact unless a writer moved the text and left it stale (then it misses
    the estimate). Otherwise one box per word, at the ink KiCad's plot draws: 0.82 x width
    per character for the stroke font, with 0.28 / 0.31 x width bearing at a left / right
    anchor (68 parsnip texts: residual -0.22..+0.15 x width per word, p10..p90); 0.75 for a
    TrueType face (its caches: 0.53-0.89, median 0.75); a lone '-' is a bar at mid-height. A
    knockout text is one solid box: the ink grown by KiCad's knockout margin, height / 9
    (measured on parsnip's plot: 0.122 at 1.1 mm, 0.167 at 1.5)."""
    if 'knockout' in (kid(node, 'layer') or []):
        sz = kid(kid(kid(node, 'effects') or [], 'font') or [], 'size') or ['size', '1', '1']
        bb = bbox([q for pb in _ink(x, y, ang, txt, node, k)[1] for q in (pb[:2], pb[2:])])
        bb = grow(bb, k * float(sz[1]) / 9)
        return bb, [bb]
    return _ink(x, y, ang, txt, node, k)

def _ink(x, y, ang, txt, node, k):
    eff = kid(node, 'effects') or []
    fnt = kid(eff, 'font') or []
    sz = kid(fnt, 'size') or ['size', '1', '1']
    js = set((kid(eff, 'justify') or [])[1:])
    lines = txt.split('\\n')
    fw, ch, ttf = k * float(sz[2]), k * float(sz[1]), kid(fnt, 'face') is not None
    w, h = .9 * fw * max(len(ln) for ln in lines), ch * (1.6 * len(lines) - .4)
    left, right = ('right', 'left') if 'mirror' in js else ('left', 'right')
    x0 = 0.0 if left in js else -w if right in js else -w / 2
    y0 = 0.0 if 'top' in js else -h if 'bottom' in js else -h / 2
    est = bbox([xf(u, v, x, y, ang) for u in (x0, x0 + w) for v in (y0, y0 + h)])
    rc = kid(node, 'render_cache')
    polys = [[(float(q[1]), float(q[2])) for q in kids(kid(pg, 'pts') or [], 'xy')] for pg in kids(rc or [], 'polygon')]
    polys = [pp for pp in polys if pp]
    if polys:
        cb = bbox([q for pp in polys for q in pp])
        if cb[0] < est[2] + 1 and cb[2] > est[0] - 1 and cb[1] < est[3] + 1 and cb[3] > est[1] - 1:
            return cb, [bbox(pp) for pp in polys]
    iw, bl, br = (.75 * fw, 0.0, 0.0) if ttf else (.82 * fw, .28 * fw, .31 * fw)
    parts = []
    for i, ln in enumerate(lines):          # reading order from the anchor, then mirrored about it
        lw, ly = iw * len(ln), y0 + i * 1.6 * ch
        r0 = bl if 'left' in js else -br - lw if 'right' in js else -lw / 2
        for m in re.finditer(r'\S+', ln):
            ra, rb = r0 + iw * m.start(), r0 + iw * m.end()
            ua, ub = (-rb, -ra) if 'mirror' in js else (ra, rb)
            va, vb = (ly + .4 * ch, ly + .7 * ch) if m.group(0) == '-' else (ly, ly + ch * 1.2)
            parts.append(bbox([xf(u, v, x, y, ang) for u in (ua, ub) for v in (va, vb)]))
    return est, parts or [est]

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

def _edges(R):
    return [(r[i], r[i + 1]) for r in R for i in range(len(r) - 1)]

def poly_dist(A, B):
    """Gap between two courtyards given as closed rings (lists of points), and
    whether they truly overlap: edges crossing, or one inside the other. Two
    outlines that only touch are 0.0 apart but do not overlap."""
    ea, eb = _edges(A), _edges(B)
    if any(seg_cross(a0, a1, b0, b1) for a0, a1 in ea for b0, b1 in eb):
        return 0.0, True
    d = min([pt_seg_dist(p, b0, b1) for r in A for p in r for b0, b1 in eb] +
            [pt_seg_dist(p, a0, a1) for r in B for p in r for a0, a1 in ea])
    for X, Y, ey in ((A, B, eb), (B, A, ea)):          # containment: an off-edge vertex inside
        p = next((q for r in X for q in r if min(pt_seg_dist(q, c, e) for c, e in ey) > 1e-4), None)
        if p is None or inside(p, Y):
            return 0.0, True
    return d, False

class PolyIndex:
    """Even-odd point-in-polygon on a big ring (a zone fill has 10k+ points) in
    O(edges in one 0.5 mm row): an edge a horizontal ray at y can cross must
    span y, so bucketing edges by y is exact."""
    def __init__(self, pts, cell=0.5):
        self.pts, self.cell, self.rows = pts, cell, defaultdict(list)
        self.bbox = bbox(pts)
        for (x1, y1), (x2, y2) in zip(pts, pts[1:] + pts[:1]):
            if y1 != y2:
                for k in range(math.floor(min(y1, y2) / cell), math.floor(max(y1, y2) / cell) + 1):
                    self.rows[k].append((x1, y1, x2, y2))

    @cached_property
    def grid(self):
        """edges by (x, y) cell, for touches(): only fill-to-fill merging needs it"""
        g, c = defaultdict(list), self.cell
        for (x1, y1), (x2, y2) in zip(self.pts, self.pts[1:] + self.pts[:1]):
            for cx in range(math.floor(min(x1, x2) / c), math.floor(max(x1, x2) / c) + 1):
                for k in range(math.floor(min(y1, y2) / c), math.floor(max(y1, y2) / c) + 1):
                    g[(cx, k)].append(((x1, y1), (x2, y2)))
        return g

    def touches(self, p, tol=0.01):
        """inside, or within tol of the outline (a shared boundary reads either way)"""
        if p in self:
            return True
        c = self.cell
        return any(pt_seg_dist(p, a, b) <= tol
                   for gx in (math.floor((p[0] - tol) / c), math.floor((p[0] + tol) / c))
                   for gy in (math.floor((p[1] - tol) / c), math.floor((p[1] + tol) / c))
                   for a, b in self.grid.get((gx, gy), ()))

    def __contains__(self, p):
        x, y = p
        b = self.bbox
        if not (b[0] <= x <= b[2] and b[1] <= y <= b[3]):
            return False
        c = False
        for x1, y1, x2, y2 in self.rows.get(math.floor(y / self.cell), ()):
            if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
                c = not c
        return c

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
                 'dnp', 'pads', 'crtyd', 'crtyd_real', 'cpoly', 'body', 'placed', '_edge', 'models', 'art',
                 'fab', 'scale', 'edgeref', 'arts', 'texts', 'uuid', 'badlines', 'lcsc', 'mpn', 'fields', 'silklines',
                 'sympath')

    @property
    def outline(self):
        """courtyard as closed rings: the real CrtYd shape when it chains, else
        the bbox rectangle"""
        if self.cpoly:
            return self.cpoly
        x0, y0, x1, y1 = self.crtyd
        return [[(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]]

    @property
    def edge(self):
        """Courtyard-to-outline distance (None: no outline), computed on first use:
        ~0.15 s for every footprint on a real board, and most commands never ask."""
        if callable(self._edge):
            self._edge = self._edge()
        return self._edge

    @property
    def back(self):
        # a padless graphic's (layer) is meaningless (a B.Cu logo can carry F.SilkS
        # art): its side is where the art is
        if not self.pads and self.art:
            return self.art == 'B'
        return self.layer.startswith('B.')

    @property
    def area(self):
        b = self.crtyd
        return (b[2] - b[0]) * (b[3] - b[1])


class Zone(tuple):
    """a board-level zone: unpacks as (net, layers) like it always did, and carries
    .outline [(x, y)], .priority, .keepout (a rule area), .uuid, .name"""

    def __new__(cls, net, layers, **kw):
        z = super().__new__(cls, (net, layers))
        z.__dict__.update(kw)
        return z


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
        self._fill_nodes = []
        edge = []

        self.teardrops = 0
        for z in kids(root, 'zone'):
            net = val(z, 'net')
            if kid(kid(z, 'attr') or [], 'teardrop'):     # pad/track fillet, not a pour
                self.teardrops += 1
                continue
            lays = kid(z, 'layers') or kid(z, 'layer') or []
            pts = [(_f(q[1]), _f(q[2])) for q in (kid(kid(z, 'polygon') or [], 'pts') or [])[1:]
                   if isinstance(q, list) and q and q[0] == 'xy']
            pr = val(z, 'priority')
            self.zones.append(Zone(net, [l for l in lays[1:] if isinstance(l, str)], outline=pts,
                                   priority=int(_f(pr)) if pr else 0, keepout=kid(z, 'keepout') is not None,
                                   uuid=val(z, 'uuid'), name=val(z, 'name')))
            self._fill_nodes += [(net, fp) for fp in kids(z, 'filled_polygon')]

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
        # what KiCad's Properties dialog quotes coordinates from, per Display origin
        st = kid(root, 'setup') or []
        self.origins = {'page': (0.0, 0.0), 'grid': self._xy(st, 'grid_origin'),
                        'aux': self._xy(st, 'aux_axis_origin')}

        # routed tracks (segments + arcs) and vias, carrying their own net names
        self.tracks, self.vias = [], []
        for s in kids(root, 'segment'):
            p0, p1 = self._xy(s, 'start'), self._xy(s, 'end')
            self.tracks.append({'net': val(s, 'net'), 'layer': val(s, 'layer'),
                                'w': _f(val(s, 'width')), 'len': math.dist(p0, p1),
                                'a': p0, 'b': p1, 'uuid': val(s, 'uuid'), 'locked': val(s, 'locked') == 'yes',
                                'mid': ((p0[0] + p1[0]) / 2, (p0[1] + p1[1]) / 2)})
        for s in kids(root, 'arc'):
            p0, pm, p1 = self._xy(s, 'start'), self._xy(s, 'mid'), self._xy(s, 'end')
            pts = arc_pts(p0, pm, p1, 24)
            self.tracks.append({'net': val(s, 'net'), 'layer': val(s, 'layer'), 'uuid': val(s, 'uuid'),
                                'locked': val(s, 'locked') == 'yes', 'w': _f(val(s, 'width')), 'mid': pm, 'a': p0, 'b': p1,
                                'len': sum(math.dist(pts[i], pts[i + 1])
                                           for i in range(len(pts) - 1))})
        for v in kids(root, 'via'):
            vx, vy = self._xy(v, 'at')
            lys = kid(v, 'layers') or []
            self.vias.append({'net': val(v, 'net'), 'drill': _f(val(v, 'drill')), 'uuid': val(v, 'uuid'),
                              'size': _f(val(v, 'size')), 'x': vx, 'y': vy, 'free': val(v, 'free') == 'yes',
                              'locked': val(v, 'locked') == 'yes',
                              'layers': [l for l in lys[1:] if isinstance(l, str)]})

        edge += self._graphics(root, None, 'gr_')
        # copper drawn as graphics (a GND rectangle, a SW poly): [(net, layer, ring, stroke, filled)]
        self.cushapes = []
        for g in root[1:]:
            if isinstance(g, list) and g and str(g[0]).startswith('gr_') and val(g, 'layer').endswith('.Cu'):
                self.cushapes += self._cushape(g, val(g, 'net'), lambda q: q)
        # board-level silk (pasted art, text): [(layer, bbox)]; text boxes are estimated
        self.silk, self.silktexts = [], []      # silktexts: board gr_text with the detail `silk` judges
        self.silkart = []                       # board silk graphics: (layer, bbox, None | [(a, b, w)] outline)
        for g in root[1:]:
            if not (isinstance(g, list) and g and str(g[0]).startswith('gr_') and val(g, 'layer').endswith('.SilkS')):
                continue
            if g[0] == 'gr_text':
                at = kid(g, 'at') or ['at', '0', '0']
                ang = _f(at[3]) if len(at) > 3 else 0.0
                bx, parts = text_parts(float(at[1]), float(at[2]), ang, str(g[1]), g)
                self.silk.append((val(g, 'layer'), bx))
                eff = kid(g, 'effects') or []
                fnt = kid(eff, 'font') or []
                sz, th = kid(fnt, 'size') or ['size', '1', '1'], kid(fnt, 'thickness')
                self.silktexts.append({'kind': 'board', 'text': str(g[1]), 'layer': val(g, 'layer'), 'x': float(at[1]),
                                       'y': float(at[2]), 'ang': ang, 'h': _f(sz[1], 1.0), 'thick': _f(th[1], 0.15) if th else 0.15,
                                       'hidden': False, 'mirror': 'mirror' in (kid(eff, 'justify') or []),
                                       'knockout': 'knockout' in (kid(g, 'layer') or []),
                                       'uuid': val(g, 'uuid'), 'box': bx, 'parts': parts, 'ttf': kid(fnt, 'face') is not None})
            elif self._gpts(g, g[0]):
                self.silk.append((val(g, 'layer'), bbox(self._gpts(g, g[0]))))
                self.silkart.append(self._art(g))
        for node in kids(root, 'footprint'):
            f = self._footprint(node)
            self.fps[f.ref] = f
            edge += self._graphics(node, f, 'fp_')
            for g in node[1:]:                       # footprint copper: the net of a pad it lies on
                if isinstance(g, list) and g and str(g[0]).startswith('fp_') and val(g, 'layer').endswith('.Cu'):
                    for net, ly, ring, w, fill in self._cushape(g, '', lambda q, f=f: xf(q[0] * f.scale[0], q[1] * f.scale[1], f.x, f.y, f.rot)):
                        bb = bbox(ring)
                        net = next((p['net'] for p in f.pads if p['net'] and
                                    pad_box(p)[0] <= (bb[0] + bb[2]) / 2 <= pad_box(p)[2] and
                                    pad_box(p)[1] <= (bb[1] + bb[3]) / 2 <= pad_box(p)[3]), '')
                        self.cushapes.append((net, ly, ring, w, fill))
        bad = [f.ref for f in self.fps.values() if f.badlines]
        if bad:
            print(f"WARNING: {len(bad)} footprint(s) have fp_line graphics stored in the wrong frame "
                  f"(a KiCad 10.99 nightly save bug: KiCad draws them near the page origin). Read "
                  f"corrected here; repair a copy with `kpcb.py {os.path.basename(path)} fixlines -o OUT`.",
                  file=sys.stderr)

        self.edge_segs = [(s[i], s[i + 1]) for s in edge for i in range(len(s) - 1)]
        self.rings = rings(edge)
        self.outline = bbox([p for s in edge for p in s]) if edge else None
        for f in self.fps.values():
            f.placed = self._placed(f)
            f._edge = (lambda f=f: self._edge_dist(f)) if self.edge_segs else None

    @cached_property
    def fills(self):
        """the saved zone fills, per layer: most of the file, parsed on first use
        because most commands never look at them"""
        out = []
        for net, fp in self._fill_nodes:
            pts = [(float(p[1]), float(p[2])) for p in (kid(fp, 'pts') or [])[1:]
                   if isinstance(p, list) and p and p[0] == 'xy']
            if len(pts) >= 3:
                out.append({'net': net, 'layer': val(fp, 'layer'), 'pts': pts,
                            'area': poly_area(pts), 'bbox': bbox(pts)})
        return out

    # -- parsing helpers -------------------------------------------------
    def _graphics(self, node, f, pfx):
        """Edge.Cuts polylines from a container, in board coordinates. Footprint
        graphics count too - a milled slot often lives inside a footprint."""
        ox, oy, rot = (f.x, f.y, f.rot) if f else (0.0, 0.0, 0.0)
        kx, ky = f.scale if f else (1.0, 1.0)
        out = []
        def T(p):
            return xf(p[0] * kx, p[1] * ky, ox, oy, rot)
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

    @classmethod
    def _cushape(cls, g, net, T):
        """one copper graphic as [(net, layer, closed ring, stroke width, filled)]"""
        t = str(g[0])[3:]
        w = _f(val(kid(g, 'stroke') or [], 'width'))
        fill = (kid(g, 'fill') or ['', 'no'])[1] in ('yes', 'solid')
        if t == 'rect':
            (x0, y0), (x1, y1) = cls._xy(g, 'start'), cls._xy(g, 'end')
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
        elif t == 'poly':
            pts = cls._pts(g)
        elif t == 'circle':
            c, e = cls._xy(g, 'center'), cls._xy(g, 'end')
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            pts = [(c[0] + r * math.cos(k * math.pi / 8), c[1] + r * math.sin(k * math.pi / 8)) for k in range(16)]
        elif t == 'arc':
            pts, fill = arc_pts(cls._xy(g, 'start'), cls._xy(g, 'mid'), cls._xy(g, 'end')), False
        elif t == 'line':
            pts, fill = [cls._xy(g, 'start'), cls._xy(g, 'end')], False
        else:
            return []
        pts = [T(q) for q in pts]
        if t in ('rect', 'poly', 'circle') and len(pts) >= 3:
            pts.append(pts[0])                      # a closed outline: its last side is copper too
        return [(net, val(g, 'layer'), pts, w, fill and len(pts) >= 3)] if len(pts) >= 2 else []

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
        # (scale sx sy) multiplies the footprint-local geometry (scaled logos)
        f.scale = kx, ky = tuple(_f(x, 1) for x in sc[1:3]) if tr and sc and len(sc) > 2 else (1.0, 1.0)
        f.x, f.y = (float(at[1]), float(at[2])) if at else (0.0, 0.0)
        f.rot = float(at[3]) if at and len(at) > 3 else 0.0
        f.layer = val(node, 'layer', 'F.Cu')
        f.uuid = val(node, 'uuid')
        f.badlines = self._fix_lines(node, f)
        f.models = [m[1] for m in kids(node, 'model') if len(m) > 1 and not has(m, 'hide')]
        f.sheet = val(node, 'sheetname', '')
        f.sympath = val(node, 'path', '')   # sheet tstamps + symbol uuid; '' = unlinked
        a = kid(node, 'attr') or []
        f.attr = set(x for x in a[1:] if isinstance(x, str))
        f.dnp = 'dnp' in f.attr
        f.ref, f.value, f.lcsc, f.mpn = '?', '', '', ''
        for p in kids(node, 'property'):
            if len(p) > 2 and p[1] == 'Reference':
                f.ref = p[2]
            elif len(p) > 2 and p[1] == 'Value':
                f.value = p[2]
            elif len(p) > 2 and p[1] in ('LCSC Part', 'LCSC') and isinstance(p[2], str):
                f.lcsc = p[2].strip()
            elif len(p) > 2 and p[1] == 'MPN' and isinstance(p[2], str):
                f.mpn = p[2].strip()
        # visible silk text (refdes, values, fp_text): (at) is footprint-local, its angle absolute
        f.texts = []
        for t in kids(node, 'property') + kids(node, 'fp_text'):
            lay = val(t, 'layer')
            hid = 'hide' in t or (kid(t, 'hide') or ['', 'no'])[1:2] == ['yes'] or \
                'hide' in (kid(t, 'effects') or [])
            if not lay.endswith('.SilkS') or hid or len(t) < 3:
                continue
            txt = str(t[2]).replace('${REFERENCE}', f.ref).replace('${VALUE}', f.value)
            at = kid(t, 'at') or ['at', '0', '0']
            tx, ty = xf(float(at[1]) * kx, float(at[2]) * ky, f.x, f.y, f.rot)
            f.texts.append((lay, text_box(tx, ty, _f(at[3]) if len(at) > 3 else 0.0, txt, t, (kx + ky) / 2)))
        # every designator, value and user text, hidden ones too: what `silk`/`refdes` judge and move
        f.fields = []
        for t in kids(node, 'property') + kids(node, 'fp_text'):
            if len(t) < 3 or (t[0] == 'property' and t[1] not in ('Reference', 'Value')):
                continue
            kind = t[1] if t[0] == 'property' else {'reference': 'Reference', 'value': 'Value'}.get(t[1], 'user')
            txt = str(t[2]).replace('${REFERENCE}', f.ref).replace('${VALUE}', f.value)
            at = kid(t, 'at') or ['at', '0', '0']
            eff = kid(t, 'effects') or []
            fnt = kid(eff, 'font') or []
            sz, th = kid(fnt, 'size') or ['size', '1', '1'], kid(fnt, 'thickness')
            ang = _f(at[3]) if len(at) > 3 else 0.0
            tx, ty = xf(float(at[1]) * kx, float(at[2]) * ky, f.x, f.y, f.rot)
            f.fields.append({'kind': kind, 'text': txt, 'layer': val(t, 'layer'), 'x': tx, 'y': ty, 'ang': ang,
                             'h': _f(sz[1], 1.0), 'w': _f(sz[2] if len(sz) > 2 else sz[1], 1.0),
                             'thick': _f(th[1], 0.15) if th else 0.15 * _f(sz[1], 1.0),
                             'hidden': 'hide' in t or (kid(t, 'hide') or ['', 'no'])[1:2] == ['yes'] or 'hide' in eff,
                             'mirror': 'mirror' in (kid(eff, 'justify') or []), 'uuid': val(t, 'uuid'),
                             'knockout': 'knockout' in (kid(t, 'layer') or []),
                             'ttf': kid(fnt, 'face') is not None})
            f.fields[-1]['box'], f.fields[-1]['parts'] = text_parts(tx, ty, ang, txt, t, (kx + ky) / 2)
        if f.ref in self.fps:                     # KiCad allows it; keep every one visible
            n = 2
            while f"{f.ref}~dup{n}" in self.fps:
                n += 1
            f.ref = f"{f.ref}~dup{n}"
        sides = [val(g, 'layer')[:2] for g in node[1:] if isinstance(g, list) and g
                 and str(g[0]).startswith('fp_') and val(g, 'layer')[:2] in ('F.', 'B.')]
        f.art = max('FB', key=lambda c: sides.count(c + '.')) if sides else None

        f.pads = []
        pad_pts, all_pts, crt_pts, crt_segs, fab_pts, dwg = [], [], [], [], [], []
        for p in kids(node, 'pad'):
            num = p[1] if len(p) > 1 else '?'
            pat = kid(p, 'at')
            lx, ly = (float(pat[1]), float(pat[2])) if pat else (0.0, 0.0)
            prot = float(pat[3]) if pat and len(pat) > 3 else 0.0
            sz = kid(p, 'size')
            sx, sy = (float(sz[1]) * abs(kx), float(sz[2]) * abs(ky)) if sz and len(sz) > 2 else (0.0, 0.0)
            bx, by = xf(lx * kx, ly * ky, f.x, f.y, f.rot)
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
            pad_pts += [xf(u * kx, v * ky, bx, by, prot) for poly in prims for u, v in poly]
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
        f.arts = []                             # a padless graphic's art: [(layer, item bbox)]
        for tag in ('fp_line', 'fp_rect', 'fp_arc', 'fp_poly', 'fp_circle'):
            for g in kids(node, tag):
                lay = val(g, 'layer')
                if tag == 'fp_line' and lay == 'Dwgs.User':
                    dwg.append(tuple(xf(q[0] * kx, q[1] * ky, f.x, f.y, f.rot)
                                     for q in (self._xy(g, 'start'), self._xy(g, 'end'))))
                cf = lay.endswith('.CrtYd') or lay.endswith('.Fab')
                art = not f.pads and not cf and lay[:2] in ('F.', 'B.')
                if not (cf or art):
                    continue
                pts = [xf(p[0] * kx, p[1] * ky, f.x, f.y, f.rot) for p in self._gpts(g, tag)]
                if art:
                    if pts:
                        f.arts.append((lay, bbox(pts)))
                    continue
                all_pts += pts
                if lay.endswith('.Fab'):
                    fab_pts += pts
                if lay.endswith('.CrtYd'):
                    crt_pts += pts
                    crt_segs.append(self._crt_seg(g, tag, f))
        f.silklines = []                        # its silkscreen outline: [(layer, a, b, stroke)]
        if f.pads:
            T = lambda q: xf(q[0] * kx, q[1] * ky, f.x, f.y, f.rot)       # noqa: E731
            for tag in ('fp_line', 'fp_rect', 'fp_arc', 'fp_poly', 'fp_circle'):
                for g in kids(node, tag):
                    lay = val(g, 'layer')
                    if not lay.endswith('.SilkS'):
                        continue
                    w = _f(val(kid(g, 'stroke') or [], 'width'), 0.12)
                    seg = self._crt_seg(g, tag, f) if tag != 'fp_line' else [T(self._xy(g, 'start')), T(self._xy(g, 'end'))]
                    f.silklines += [(lay, seg[i], seg[i + 1], w) for i in range(len(seg or []) - 1)]
        f.body = bbox(pad_pts + all_pts) if (pad_pts or all_pts) else (f.x, f.y, f.x, f.y)
        f.crtyd = bbox(crt_pts) if crt_pts else f.body
        f.crtyd_real = bool(crt_pts)
        f.fab = bbox(fab_pts) if fab_pts else None       # the housing outline alone
        # an edge-launch part's own "PCB Edge" line (Dwgs.User): where its flange expects the edge
        mark = any(len(t) > 2 and re.search(r'PCB\s*Edge', str(t[2]), re.I) and val(t, 'layer') == 'Dwgs.User'
                   for t in kids(node, 'fp_text'))
        f.edgeref = dwg if mark and dwg else None
        f.cpoly = rings([s for s in crt_segs if s]) or None
        return f

    @classmethod
    def _fix_lines(cls, node, f):
        """KiCad 10.99 nightly (build ~4975, the 2026-10-08 parsnip save) wrote every
        fp_line through the board->footprint transform a second time: stored =
        T^-1(local), so KiCad and a plain reader draw them near the page origin while
        the pads and other graphics are right. Sound local lines are small and land
        far out (at the footprint) once transformed; corrupt ones are the reverse.
        A footprint near the page origin reads ~1:1 and is left alone. The node's
        start/end are rewritten in place to the true local values. Returns how many
        fp_lines were fixed (0 for a sound footprint)."""
        L = kids(node, 'fp_line')
        if not L:
            return 0
        kx, ky = f.scale
        T = lambda p: xf(p[0] * kx, p[1] * ky, f.x, f.y, f.rot)          # noqa: E731
        raw = [cls._xy(g, e) for g in L for e in ('start', 'end')]
        med = lambda P: sorted(math.hypot(*p) for p in P)[len(P) // 2]   # noqa: E731
        if med(raw) < 10 or med(raw) < 1.5 * med([T(p) for p in raw]):
            return 0
        for g in L:
            for e in ('start', 'end'):
                k = kid(g, e)
                k[1], k[2] = (f"{v:.6f}" for v in T((float(k[1]), float(k[2]))))
        return len(L)

    @classmethod
    def _gpts(cls, g, tag):
        """a graphic's defining points in its own frame (a circle: its bbox corners)"""
        t = tag[3:]
        if t in ('line', 'rect'):
            return [cls._xy(g, 'start'), cls._xy(g, 'end')]
        if t == 'arc':
            return [cls._xy(g, 'start'), cls._xy(g, 'mid'), cls._xy(g, 'end')]
        if t == 'poly':
            return cls._pts(g)
        if t == 'circle':
            c, e = cls._xy(g, 'center'), cls._xy(g, 'end')
            r = math.hypot(e[0] - c[0], e[1] - c[1])
            return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
        return []

    @classmethod
    def _art(cls, g):
        """(layer, bbox, segs): a filled shape is its bbox (segs None); an unfilled one or a line
        is its stroke, so text inside a frame or ring is not under it"""
        pts, t = cls._gpts(g, g[0]), g[0][3:]
        f = kid(g, 'fill')
        w = _f((kid(kid(g, 'stroke') or [], 'width') or ['', '0.1'])[1], 0.1)
        if t in ('poly', 'rect', 'circle') and f and f[1] in ('yes', 'solid'):
            return val(g, 'layer'), bbox(pts), None
        if t == 'rect':
            (x0, y0), (x1, y1) = pts
            pts = [(x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0)]
        elif t == 'circle':
            (x0, y0), (x1, y1) = pts
            c, r = ((x0 + x1) / 2, (y0 + y1) / 2), (x1 - x0) / 2
            pts = [(c[0] + r * math.cos(k * math.pi / 12), c[1] + r * math.sin(k * math.pi / 12)) for k in range(25)]
        elif t == 'poly':
            pts = pts + pts[:1]
        return val(g, 'layer'), grow(bbox(pts), w / 2), [(a, c, w) for a, c in zip(pts, pts[1:])]

    def _crt_seg(self, g, tag, f):
        """one courtyard primitive as a board-frame polyline, for rings()"""
        T = lambda p: xf(p[0] * f.scale[0], p[1] * f.scale[1], f.x, f.y, f.rot)      # noqa: E731
        if tag == 'fp_line':
            return [T(self._xy(g, 'start')), T(self._xy(g, 'end'))]
        if tag == 'fp_rect':
            (x0, y0), (x1, y1) = self._xy(g, 'start'), self._xy(g, 'end')
            return [T(p) for p in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
        if tag == 'fp_arc':
            return [T(p) for p in arc_pts(self._xy(g, 'start'), self._xy(g, 'mid'), self._xy(g, 'end'))]
        if tag == 'fp_poly':
            pts = self._pts(g)
            return [T(p) for p in pts + pts[:1]] if pts else None
        c, e = self._xy(g, 'center'), self._xy(g, 'end')
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [T((c[0] + r * math.cos(k * math.pi / 8), c[1] + r * math.sin(k * math.pi / 8)))
                for k in range(17)]

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

    def edge_clearance(self):
        """copper-to-edge minimum: the larger of the .kicad_pro floor and any .kicad_dru
        edge_clearance rule beside the board (0.3 mm when neither says)"""
        if not hasattr(self, '_ec'):
            d, v = os.path.dirname(os.path.abspath(self.path)), []
            for pro in glob.glob(os.path.join(d, '*.kicad_pro')):
                try:
                    v.append(float(json.load(open(pro))['board']['design_settings']['rules']
                                   ['min_copper_edge_clearance']))
                except (OSError, ValueError, KeyError, TypeError):
                    pass
            for dru in glob.glob(os.path.join(d, '*.kicad_dru')):
                v += [float(x) for x in re.findall(r'\(constraint edge_clearance \(min ([\d.]+)mm\)\)',
                                                   open(dru, encoding='utf-8', errors='replace').read())]
            self._ec = max(v) if v else 0.3
        return self._ec

    def warn_rules(self):
        """once per board, on stderr: with no project rules beside it, netclasses and
        clearances silently fall back to defaults (every clearance 0.2, RF unprotected)"""
        if '_warned' not in self.__dict__:
            self._warned = rules_warning(os.path.dirname(os.path.abspath(self.path)) or '.',
                                         "kpcb's netclasses and clearances use")
            if self._warned:
                print(self._warned, file=sys.stderr)

    def netclass(self, net):
        """Netclass of a net from the .kicad_pro beside the board (explicit
        assignment, else the first matching wildcard pattern), '' if none."""
        if not hasattr(self, '_nc'):
            self.warn_rules()
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
        memo = self.__dict__.setdefault('_ncmemo', {})
        if net in memo:
            return memo[net]
        direct, pats = self._nc
        if net in direct:
            c = direct[net]
            memo[net] = c[0] if isinstance(c, list) and c else str(c)
            return memo[net]
        import fnmatch
        memo[net] = next((c for p, c in pats if fnmatch.fnmatchcase(net, p)), '')
        return memo[net]

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
        # a thermistor near a hot thing is a bias, not damage: THERMAL says so apart
        if prefix(f.ref) in THERM_PFX:
            return False
        return bool(SENS_FP.search(f.fp) or SENS_VAL.search(f.value))


# ---------------- commands ----------------

def sheet_of(f):
    return f.sheet or '/'

def thick_of(b, layer):
    return b.thick.get(layer) or 0.035          # 1oz fallback when no stackup

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
