"""Dress base.blend (KiCad GLB import) for the README turntable: PBR materials, render-only extras
(2x 21700 cells, M2 standoffs, acrylic plate, WeAct 3.7" e-paper),
studio lights, camera, 60 fps turntable with eased holds, lit LEDs, an e-paper minute tick and an exploded view,
Cycles on the HIP GPU. Run by render.sh.

blender -b base.blend -P dress.py -- OUT.blend SCREEN.png SECONDS [--test DIR frame,frame,...] [--res W H] [--spp N]
                                    [--dxf PLATE.dxf]
Also writes seq_{spin,plain,bare}.txt next to OUT.blend (render.sh's frame plan, see the end).
GLB frame: metres, X = pcb x, Y = -pcb y, Z = out of the front; board body z 0..1.5 mm.
Board coordinates below (holes, BT1/BT2) come from `kpcb.py parsnip.kicad_pcb where REF`:
re-check them after a layout change."""
import bpy, bmesh, math, os, re, sys, colorsys, random
import numpy as np
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
from PIL import Image, ImageDraw, ImageFont

argv = sys.argv[sys.argv.index('--') + 1:]
OUT, SCREEN, SECONDS = argv[0], argv[1], float(argv[2])
opt = lambda k, n=1: argv[argv.index(k) + 1:argv.index(k) + 1 + n] if k in argv else None
TEST = opt('--test', 2)
RES = [int(v) for v in (opt('--res', 2) or (1080, 1512))]
SPP = int((opt('--spp') or [256])[0])
DXF = (opt('--dxf') or [None])[0]
FPS = 60
ENV_GLOSSY = float(os.environ.get('ENV_GLOSSY', 1.0))   # room brightness seen by reflections (diffuse sees 0.12)

sc = bpy.context.scene
MM = 0.001
FRONT, BACK = 1.55, -0.05            # board surfaces, GLB z in mm (mask level)
CX, CY = 125.85, 100.26              # board centre, pcb mm
P = lambda x, y, z=0.0: Vector((x * MM, -y * MM, z * MM))   # pcb mm + GLB z mm -> GLB metres

# ---------------------------------------------------------------- timeline (seconds, frame 1 = t 0 = the poster)
# Front three-quarter hold across the loop seam (the e-paper ticks a minute in it), half turn to the back three-quarter,
# short hold (RGB LEDs, cells), half turn home. There, with the turntable stopped, the stack comes apart outermost
# layer first while the turntable eases on to AX (the parallax shows the depth), holds exploded, and closes innermost
# first on the way back to A0. That explode window TX0..TX1 starts and ends on the same still assembled frame, so
# cutting it out gives the assembled-only loop (render.sh: seq_plain / seq_bare below).
assert SECONDS == 15, 'the timeline below is laid out for a 15 s loop'
N = round(SECONDS * FPS)
A0 = math.radians(-35)                    # front three-quarter (back three-quarter is A0 + 180)
AX = math.radians(-45)                    # exploded hold, reached from A0: slowing into it from past -50 (the old
                                          # timeline) the e-paper and acrylic mirror the room white for ~0.7 s
T1, HOLD_B, SETTLE, TAIL = 1.2, 1.0, 0.4, 0.8     # front hold before move 1, back hold, before / after the explode
X_OUT, X_HOLD, X_IN, STAG = 1.0, 1.2, 0.9, 0.12   # each layer's move out / in, exploded hold, stagger between layers
XO, XI = X_OUT + 4 * STAG, X_IN + 4 * STAG          # first layer leaving to last one out / first one back to last
MOVE = (SECONDS - T1 - HOLD_B - SETTLE - XO - X_HOLD - XI - TAIL) / 2   # each half turn (3.77 s)
T2 = T1 + MOVE + HOLD_B                   # move 2 (back -> front) starts
TX0 = T2 + MOVE + SETTLE                  # explode window
TX1 = TX0 + XO + X_HOLD + XI
RAMP = 0.3                                # fraction of a move spent speeding up (and again slowing down)
T_TICK, T_FLASH, T_BACK = 0.25, 5 / FPS, T1 + MOVE + HOLD_B / 2   # minute tick, each flash phase, hidden tick back


def ease(u, r=RAMP):
    """0..1 over u 0..1: raised-cosine speed-up, cruise, mirrored slow-down (no jolt into or out of a hold)."""
    u = min(max(u, 0.0), 1.0)
    up = lambda x: x / 2 - r / (2 * math.pi) * math.sin(math.pi * x / r)
    return (up(u) if u < r else r / 2 + u - r if u <= 1 - r else 1 - r - up(1 - u)) / (1 - r)


def turn(t):                              # turntable angle, unwrapped across loops
    k, s = divmod(t, SECONDS)
    return (A0 + 2 * math.pi * k + math.pi * (ease((s - T1) / MOVE) + ease((s - T2) / MOVE))
            + (AX - A0) * (ease((s - TX0) / XO) - ease((s - TX1 + XI) / XI)))


sstep = lambda x: (lambda c: c * c * c * (c * (6 * c - 15) + 10))(min(max(x, 0.0), 1.0))   # smootherstep


def keys(owner, prop, fn, index=None, interp='LINEAR'):
    """Key fn(t) on every frame 0..N+1 (motion blur reads both ends of the loop)."""
    ed = bpy.context.preferences.edit
    old, ed.keyframe_new_interpolation_type = ed.keyframe_new_interpolation_type, interp
    for f in range(N + 2):
        v = fn((f - 1) / FPS)
        if index is None:
            setattr(owner, prop, v)
            owner.keyframe_insert(prop, frame=f)
        else:
            getattr(owner, prop)[index] = v
            owner.keyframe_insert(prop, index=index, frame=f)
    ed.keyframe_new_interpolation_type = old


# ---------------------------------------------------------------- materials
def mat(name, color, metal=0.0, rough=0.5, **kw):
    m = bpy.data.materials.new(name)
    if not m.node_tree:
        m.use_nodes = True
    b = m.node_tree.nodes['Principled BSDF']
    b.inputs['Base Color'].default_value = (*color, 1)
    b.inputs['Metallic'].default_value = metal
    b.inputs['Roughness'].default_value = rough
    for k, v in kw.items():
        b.inputs[k].default_value = v
    return m


def clear(name, tint=(1, 1, 1), ior=1.49, rough=0.0):
    """Refractive solid that still lets shadow rays through (no caustics needed under it)."""
    m = mat(name, tint, rough=rough, **{'Transmission Weight': 1.0, 'IOR': ior})
    nt = m.node_tree
    b, out = nt.nodes['Principled BSDF'], nt.nodes['Material Output']
    lp, tr, mix = (nt.nodes.new(t) for t in ('ShaderNodeLightPath', 'ShaderNodeBsdfTransparent', 'ShaderNodeMixShader'))
    nt.links.new(lp.outputs['Is Shadow Ray'], mix.inputs['Fac'])
    nt.links.new(b.outputs['BSDF'], mix.inputs[1])
    nt.links.new(tr.outputs['BSDF'], mix.inputs[2])
    nt.links.new(mix.outputs['Shader'], out.inputs['Surface'])
    return m


def node(nt, kind, *links, **props):
    """New node; links are (from_socket, input index or name) pairs into it."""
    n = nt.nodes.new(kind)
    for k, v in props.items():
        setattr(n, k, v)
    for src, dst in links:
        nt.links.new(src, n.inputs[dst])
    return n


def vary(m, amp, mm, socket='Roughness'):
    """Plated, stamped and coated surfaces never mirror evenly: modulate roughness by +-amp with ~mm-sized noise,
    fixed to each part (object space) and different on every part (W from the object's random)."""
    nt, b = m.node_tree, m.node_tree.nodes['Principled BSDF']
    r = b.inputs[socket].default_value
    tc, oi = node(nt, 'ShaderNodeTexCoord'), node(nt, 'ShaderNodeObjectInfo')
    w = node(nt, 'ShaderNodeMath', (oi.outputs['Random'], 0), operation='MULTIPLY')
    w.inputs[1].default_value = 100
    nz = node(nt, 'ShaderNodeTexNoise', (tc.outputs['Object'], 'Vector'), (w.outputs[0], 'W'), noise_dimensions='4D')
    nz.inputs['Scale'].default_value, nz.inputs['Detail'].default_value = 1 / (mm * MM), 0   # smooth: octaves read as pitting
    mr = node(nt, 'ShaderNodeMapRange', (nz.outputs['Fac'], 'Value'))
    for k, v in zip(('From Min', 'From Max', 'To Min', 'To Max'), (0.3, 0.7, r * (1 - amp), r * (1 + amp))):
        mr.inputs[k].default_value = v
    nt.links.new(mr.outputs['Result'], b.inputs[socket])
    return m


def jitter(m, val=0.06, rough=0.15):
    """Moulded and fired bodies differ a little from part to part (maker, lot): +-val on colour, +-rough on roughness."""
    nt, b = m.node_tree, m.node_tree.nodes['Principled BSDF']
    oi = node(nt, 'ShaderNodeObjectInfo')
    for socket, amp in (('Base Color', val), ('Roughness', rough)):
        k = node(nt, 'ShaderNodeMapRange', (oi.outputs['Random'], 'Value'))
        k.inputs['To Min'].default_value, k.inputs['To Max'].default_value = 1 - amp, 1 + amp
        if socket == 'Base Color':
            c = node(nt, 'ShaderNodeRGB')
            c.outputs[0].default_value = b.inputs[socket].default_value
            s = node(nt, 'ShaderNodeVectorMath', (c.outputs[0], 0), (k.outputs['Result'], 'Scale'), operation='SCALE')
        else:
            s = node(nt, 'ShaderNodeMath', (k.outputs['Result'], 0), operation='MULTIPLY')
            s.inputs[1].default_value = b.inputs[socket].default_value
        nt.links.new(s.outputs[0], b.inputs[socket])
    return m


# Dielectric IORs set how strongly each surface mirrors at normal incidence (F0 = ((n-1)/(n+1))^2: 3.4% for PA66 at
# 1.53, 4% for glass, 6.7% for polyimide at 1.70); metals mirror their base colour.
M = {
    'enig':   vary(mat('enig', (1.0, 0.74, 0.36), 1.0, 0.20), 0.15, 2),
    'solder': vary(mat('solder', (0.80, 0.80, 0.79), 1.0, 0.16), 0.2, 1),   # SAC305 reflows satin, not mirror
    'cu':     mat('cu_under_mask', (0.80, 0.46, 0.25), 1.0, 0.35),
    'mask':   vary(mat('mask', (0.010, 0.011, 0.012), 0.0, 0.42, **{'Coat Weight': 1.0, 'Coat Roughness': 0.10,
                                                                       'Coat IOR': 1.55, 'Alpha': 0.93}), 0.35, 6, 'Coat Roughness'),
    'silk':   mat('silk', (0.80, 0.80, 0.78), 0.0, 0.7, IOR=1.52),
    'fr4':    mat('fr4', (0.42, 0.40, 0.24), 0.0, 0.65, IOR=1.55),
    'nickel': vary(mat('nickel', (0.70, 0.68, 0.63), 1.0, 0.30), 0.15, 4),
    'insul':  mat('insul', (0.75, 0.72, 0.62), 0.0, 0.6),
    'brass':  vary(mat('brass', (0.86, 0.66, 0.34), 1.0, 0.28), 0.15, 3),
    'steel':  vary(mat('steel', (0.62, 0.62, 0.64), 1.0, 0.26), 0.12, 3),
    'nylon':  mat('nylon', (0.018, 0.018, 0.018), 0.0, 0.55, IOR=1.53),
    'acryl':  clear('acrylic', (0.97, 0.99, 0.99), 1.49, 0.005),
    'acryl_edge': clear('acrylic_edge', (0.93, 0.97, 0.97), 1.49, 0.08),   # laser-cut cast acrylic: glossy, faint striations
    'modpcb': mat('module_pcb', (0.008, 0.009, 0.010), 0.0, 0.35, **{'Coat Weight': 1.0, 'Coat Roughness': 0.12}),
    'subst':  mat('panel_glass', (0.30, 0.31, 0.31), 0.0, 0.15, IOR=1.52),
    'fpc':    mat('fpc', (0.42, 0.17, 0.015), 0.0, 0.3, **{'Coat Weight': 0.5, 'Coat IOR': 1.70}),   # polyimide
}
# e-paper: texture on the paper area, matte with a faint protective-film sheen. A keyed state (0 old minute, 1-2 the
# inverted partial-refresh window with the old then new minute, 3 new minute) picks among screen.py's four images.
paper = mat('paper', (1, 1, 1), 0.0, 0.62, **{'Coat Weight': 0.35, 'Coat Roughness': 0.25, 'Coat IOR': 1.65})
nt = paper.node_tree
state = node(nt, 'ShaderNodeValue')
col = None
for i, suffix in enumerate(('', '_ia', '_ib', '_b')):
    tex = node(nt, 'ShaderNodeTexImage', interpolation='Cubic')
    tex.image = bpy.data.images.load(suffix.join(os.path.splitext(SCREEN)))
    if col is None:
        col = tex.outputs['Color']
        continue
    gt = node(nt, 'ShaderNodeMath', (state.outputs[0], 0), operation='GREATER_THAN')
    gt.inputs[1].default_value = i - 0.5
    col = node(nt, 'ShaderNodeMix', (gt.outputs[0], 0), (col, 6), (tex.outputs['Color'], 7), data_type='RGBA').outputs[2]
nt.links.new(col, nt.nodes['Principled BSDF'].inputs['Base Color'])
tick = lambda t: (lambda s: 0 if s < T_TICK or s >= T_BACK else 3 if s >= T_TICK + 2 * T_FLASH
                  else 1 + int((s - T_TICK) // T_FLASH))(t % SECONDS)
keys(state.outputs[0], 'default_value', tick, interp='CONSTANT')
M['paper'] = paper

# KiCad's GLB gives every component STEP colour metallic=1, roughness=1, one material per colour shared across
# unrelated parts (white is IC pins on most parts, J15's housing and the E22P can), so component finishes are
# picked per part and model colour below. Only the board layers differ (material names are export-order numbers,
# so match on these): copper roughness 0.4 (pads grey, tracks/zones yellow), silk 0.9, FR4 0.8, mask alpha < 1.
def board_layer(b):
    r, a, c = (b.inputs[k].default_value for k in ('Roughness', 'Alpha', 'Base Color'))
    if a < 0.99:
        return 'mask'
    if abs(r - 0.4) < 0.01:
        return 'cu' if c[2] < 0.2 * c[0] else 'enig'
    return {0.9: 'silk', 0.8: 'fr4'}.get(round(r, 2))


# Component finishes: metals take F0 as base colour, None keeps the model colour. Roughness spans how much of the
# room each finish mirrors: polished hard gold and stainless sharply, reflowed solder brightly, matte tin and mould
# compound barely.
FIN = {
    'gold':       ((1.00, 0.71, 0.29), 1, 0.14),   # hard gold: SMA jacks, USB-C contacts
    'gold_flash': ((1.00, 0.71, 0.29), 1, 0.26),   # castellations, header pins, lead frames
    'tin':        ((0.78, 0.78, 0.76), 1, 0.42),   # matte-Sn terminations and leads
    'nickel':     ((0.66, 0.61, 0.53), 1, 0.24),   # nickel-plated holder contacts
    'shield':     ((0.80, 0.80, 0.78), 1, 0.30),   # stamped tinplate RF cans
    'stainless':  ((0.67, 0.64, 0.60), 1, 0.16),   # USB-C and u.FL shells, tact switch covers, coin cell can
    'epoxy':      (None, 0, 0.70, {'IOR': 1.57}),  # IC / diode / FET mould compound (silica-filled epoxy)
    'molded':     (None, 0, 0.78, {'IOR': 1.60}),  # moulded power inductors, ferrite beads
    'ceramic':    (None, 0, 0.55, {'IOR': 1.90}),  # MLCC bodies: bare titanate ceramic, a brighter sheen than plastic
    'overcoat':   (None, 0, 0.30, {'IOR': 1.55}),  # glassy thick-film resistor tops
    'pbt':        (None, 0, 0.50, {'IOR': 1.57}),  # header socket bodies
    'lcp':        (None, 0, 0.32, {'IOR': 1.64}),  # USB-C tongue
    'nylon_nat':  ((0.83, 0.78, 0.66), 0, 0.45, {'IOR': 1.53}),   # natural JST / Molex housings
    'ceramic_w':  ((0.85, 0.84, 0.80), 0, 0.50, {'IOR': 1.76}),   # white alumina chip filter
    'led_body':   ((0.86, 0.86, 0.84), 0, 0.35, {'IOR': 1.57}),   # white PPA reflector cups
    'lens':       ((0.55, 0.55, 0.55), 0, 0.05, {'IOR': 1.41, 'Coat Weight': 1.0, 'Coat Roughness': 0.03, 'Coat IOR': 1.41}),
    'lens_w':     ((0.80, 0.80, 0.78), 0, 0.05, {'IOR': 1.41, 'Coat Weight': 1.0, 'Coat Roughness': 0.03, 'Coat IOR': 1.41}),
    'plastic':    (None, 0, 0.45),
}
fin_mats = {}


def finish(name, colour, label=None):
    base, metal, rough, *kw = FIN[name]
    key = label or (name, None if base else tuple(round(x, 3) for x in colour))
    if key not in fin_mats:
        m = fin_mats[key] = mat(label or name, base or colour, metal, rough, **(kw[0] if kw else {}))
        if metal:
            vary(m, 0.15, 3)
        elif name in ('epoxy', 'molded', 'ceramic', 'overcoat', 'pbt', 'nylon_nat'):
            jitter(m)
    return fin_mats[key]


# Lit LEDs (lens faces emit): D11 POWER red 622 nm, D12 TIMEPULSE blue 465 nm blinking the u-blox default 1 Hz / 100 ms,
# D8 CHARGING yellow-green 570 nm; the WS2816B pair on the back chase a rainbow, 3 turns of hue per loop, offset.
LED_W, RGB_W = 1.5, 1.0                   # emission strengths: AgX turns a brighter saturated emitter pastel
LIT = {'D11': (1.0, 0.015, 0.0), 'D12': (0.02, 0.10, 1.0), 'D8': (0.42, 1.0, 0.0), 'U18': 0.0, 'U20': 0.12}


def led(ref, name):
    m = finish(name, None, f'led_{ref}')
    m.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value = (0.02, 0.02, 0.02, 1)   # dark die: lit lens shows its colour, not the key light
    nt, b, spec = m.node_tree, m.node_tree.nodes['Principled BSDF'], LIT[ref]
    if isinstance(spec, float):
        hue = node(nt, 'ShaderNodeValue')
        fr = node(nt, 'ShaderNodeMath', (hue.outputs[0], 0), operation='FRACT')
        hsv = node(nt, 'ShaderNodeCombineColor', (fr.outputs[0], 0), mode='HSV')
        hsv.inputs[1].default_value = hsv.inputs[2].default_value = 1.0
        nt.links.new(hsv.outputs[0], b.inputs['Emission Color'])
        keys(hue.outputs[0], 'default_value', lambda t: 3 * t / SECONDS + spec)
        b.inputs['Emission Strength'].default_value = RGB_W
    else:
        b.inputs['Emission Color'].default_value = (*spec, 1)
        b.inputs['Emission Strength'].default_value = LED_W
    if ref == 'D12':
        keys(b.inputs['Emission Strength'], 'default_value', lambda t: LED_W if t % 1 < 0.1 else 0.0, interp='CONSTANT')
    return m


# (refdes regex, model colour or None for any) -> finish, where a model's colours say nothing about its material
OVERRIDE = [
    ('U1', (0.114, 0.114, 0.115), 'shield'),               # WROOM can, drawn dark grey
    ('U9', (0.591, 0.638, 0.855), 'shield'),               # NEO-F10N can, drawn blue-grey
    ('U9', (1.0, 0.624, 0.216), 'gold_flash'),
    ('U12', (1.0, 1.0, 1.0), 'shield'),                    # E22P can
    ('U12', (1.0, 1.0, 0.216), 'gold_flash'),              # E22P castellations, drawn yellow
    ('J11|J13', None, 'gold'),                             # SMA jacks
    ('J7|J12', (0.645, 0.638, 0.572), 'stainless'),        # USB-C / u.FL shells
    ('J12', (0.778, 0.77, 0.626), 'stainless'),
    ('J7', (0.709, 0.504, 0.21), 'gold'),
    ('J7', (0.061, 0.061, 0.061), 'lcp'),
    ('J2|J6', (0.778, 0.77, 0.626), 'nylon_nat'),          # JST SH
    ('J5', (0.776, 0.768, 0.624), 'nylon_nat'),            # PicoBlade
    ('J10', (0.791, 0.716, 0.578), 'nylon_nat'),           # JST PH
    ('J15', (0.68, 0.631, 0.527), 'nylon_nat'),            # JST XH
    ('BT1|BT2', (0.527, 0.527, 0.527), 'nickel'),          # holder contacts
    ('BT1|BT2', (1.0, 0.216, 0.0), 'plastic'),
    ('BT3', (0.114, 0.114, 0.115), 'stainless'),           # coin cell can
    ('SW\\d+', (0.778, 0.77, 0.626), 'stainless'),         # tact switch covers
    ('D8|D11|D12|U18|U20', (0.778, 0.77, 0.626), 'led_body'),
    ('D8|D11|D12', (0.095, 0.42, 0.167), 'lens'),           # water-clear 0805 lenses, drawn green
    ('U18|U20', (0.017, 0.134, 0.487), 'lens_w'),          # WS2816B lens, drawn blue
    ('FL2', (1.0, 1.0, 1.0), 'ceramic_w'),
]


def pick(ref, c):
    for pat, col, name in OVERRIDE:
        if re.fullmatch(pat, ref) and (col is None or max(abs(a - b) for a, b in zip(col, c)) < 0.003):
            return name
    h, s, v = colorsys.rgb_to_hsv(*[x ** (1 / 2.2) for x in c])
    pre = re.match(r'[A-Z]*', ref).group(0)
    if 0.07 < h < 0.17 and s > 0.35 and v > 0.6:
        return 'gold_flash'
    if s < 0.2 and v > 0.55:
        return 'tin'
    if pre == 'C' and s > 0.25:
        return 'ceramic'
    if pre in ('R', 'RN', 'TH', 'F') and v < 0.2:
        return 'overcoat'
    if pre in ('L', 'FB'):
        return 'molded'
    if pre == 'J':
        return 'pbt'
    return 'epoxy' if v < 0.45 else 'plastic'


def part_of(o):                                       # the top-level KiCad footprint object (named by refdes)
    while o.parent and o.parent.parent:
        o = o.parent
    return o.name


board_mats = {m: board_layer(m.node_tree.nodes['Principled BSDF']) for m in bpy.data.materials
              if m.name.startswith('mat_') and m.node_tree}
parts = {}
for o in [o for o in bpy.data.objects if o.type == 'MESH']:
    ref = part_of(o)
    for slot in o.material_slots:
        m = slot.material
        if m not in board_mats:
            continue
        if board_mats[m]:
            slot.material = M[board_mats[m]]
        else:                                         # per object: KiCad instances one mesh for every same footprint
            c = m.node_tree.nodes['Principled BSDF'].inputs['Base Color'].default_value[:3]
            slot.link = 'OBJECT'
            name = pick(ref, c)
            slot.material = led(ref, name) if ref in LIT and name.startswith('lens') else finish(name, c)
    if re.fullmatch(r'[A-Z]+\d+', ref):
        parts.setdefault(ref, []).append(o)

# Pads under a fitted part are reflowed solder; bare ENIG stays only where nothing is fitted (test points, solder
# jumpers, fiducials, mounting rings, DNP footprints). A pad belongs to a part if it lies in the part's bounding box.
boxes = []
for objs in parts.values():
    pts = np.array([tuple(o.matrix_world @ Vector(c)) for o in objs for c in o.bound_box])
    boxes.append((pts.min(0) - [0.3 * MM, 0.3 * MM, 0.5 * MM], pts.max(0) + [0.3 * MM, 0.3 * MM, 0.5 * MM]))
for o in [o for o in bpy.data.objects if o.type == 'MESH' and M['enig'] in [s.material for s in o.material_slots]]:
    me, n = o.data, len(o.data.polygons)
    cen = np.empty(n * 3)
    me.polygons.foreach_get('center', cen)
    cen = cen.reshape(-1, 3) @ np.array(o.matrix_world.to_3x3()).T + np.array(o.matrix_world.translation)
    hit = np.zeros(n, bool)
    for lo, hi in boxes:
        hit |= np.all((cen >= lo) & (cen <= hi), axis=1)
    idx = np.empty(n, np.int32)
    me.polygons.foreach_get('material_index', idx)
    enig = [i for i, s in enumerate(o.material_slots) if s.material == M['enig']]
    me.materials.append(M['solder'])
    idx[np.isin(idx, enig) & hit] = len(me.materials) - 1
    me.polygons.foreach_set('material_index', idx)
    me.update()
    print(f'{o.name}: {np.count_nonzero(hit)} of {n} pad faces soldered')

# ---------------------------------------------------------------- geometry helpers (pcb mm in, GLB metres out)
extras = []


def mesh_obj(name, bm, material, smooth_deg=None, bevel=None):
    me = bpy.data.meshes.new(name)
    bm.to_mesh(me); bm.free()
    o = bpy.data.objects.new(name, me)
    sc.collection.objects.link(o)
    me.materials.append(material)
    if bevel:
        mod = o.modifiers.new('bevel', 'BEVEL')
        mod.width, mod.segments, mod.limit_method, mod.angle_limit = bevel * MM, 3, 'ANGLE', math.radians(50)
        mod.harden_normals = True
    if smooth_deg is not None:
        me.shade_smooth()
        me.set_sharp_from_angle(angle=math.radians(smooth_deg))
    extras.append(o)
    return o


def prism(name, material, poly, z0, z1, bevel=0.12, smooth_deg=35, edge_material=None):
    """poly: CCW list of pcb-mm (x, y) points (convex or star-shaped about its centroid); z0..z1 GLB mm."""
    bm = bmesh.new()
    bot = [bm.verts.new(P(x, y, z0)) for x, y in poly]
    top = [bm.verts.new(P(x, y, z1)) for x, y in poly]
    bm.faces.new(bot)                       # pcb CCW is GLB CW (y flipped): bottom faces -z as given
    bm.faces.new(list(reversed(top)))
    n = len(poly)
    for i in range(n):
        j = (i + 1) % n
        f = bm.faces.new((bot[j], bot[i], top[i], top[j]))
        f.material_index = 1 if edge_material else 0
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    o = mesh_obj(name, bm, material, smooth_deg, bevel)
    if edge_material:
        o.data.materials.append(edge_material)
    return o


def circle(cx, cy, r, seg=48):
    return [(cx + r * math.cos(2 * math.pi * k / seg), cy + r * math.sin(2 * math.pi * k / seg)) for k in range(seg)]


def hexagon(cx, cy, af):
    r = af / math.sqrt(3)
    return [(cx + r * math.cos(math.pi / 3 * k), cy + r * math.sin(math.pi / 3 * k)) for k in range(6)]


def rrect(cx, cy, w, h, r, seg=10):
    pts = []
    for qx, qy, a0 in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
        ox, oy = cx + qx * (w / 2 - r), cy + qy * (h / 2 - r)
        pts += [(ox + r * math.cos(math.radians(a0 + 90 * k / seg)), oy + r * math.sin(math.radians(a0 + 90 * k / seg)))
                for k in range(seg + 1)]
    return pts


def box(name, material, x0, y0, x1, y1, z0, z1, bevel=0.08):
    return prism(name, material, [(x0, y0), (x1, y0), (x1, y1), (x0, y1)], z0, z1, bevel, None)


def cut(o, polys, z0, z1, tag='cut', face=0):
    """Boolean prisms (pcb-mm polys, GLB z0..z1 mm) out of o; new walls take o's material slot face. The hidden cutter
    is named after o, so it explodes with it."""
    bm = bmesh.new()
    for poly in polys:
        bot = [bm.verts.new(P(x, y, z0)) for x, y in poly]
        top = [bm.verts.new(P(x, y, z1)) for x, y in poly]
        bm.faces.new(bot)
        bm.faces.new(list(reversed(top)))
        for i in range(len(poly)):
            bm.faces.new((bot[(i + 1) % len(poly)], bot[i], top[i], top[(i + 1) % len(poly)]))
    for f in bm.faces:
        f.material_index = face
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    c = mesh_obj(f'{o.name}_{tag}', bm, o.data.materials[0])
    c.data.materials.append(o.data.materials[-1])
    c.hide_render, c.display_type = True, 'WIRE'
    mod = o.modifiers.new(tag, 'BOOLEAN')
    mod.operation, mod.object = 'DIFFERENCE', c
    return o


def torx(head, cx, cy, a, z0, z1, seg=72):
    """Cut a hexalobular drive recess (a = point-to-point: T6 1.75, T10 2.74 mm; lobe roots at 0.72 a) at pcb cx, cy."""
    r = lambda t: a * (0.36 + 0.14 * (0.5 + 0.5 * math.cos(6 * t)))
    return cut(head, [[(cx + r(t) * math.cos(t), cy + r(t) * math.sin(t)) for t in (2 * math.pi * k / seg for k in range(seg))]],
               z0, z1, 'torx')


def thread(name, cx, cy, d, pitch, z0, z1, seg=24):
    """ISO metric thread (60 deg flanks, 0.61 P deep) on a shank along GLB z, as stacked rings: at this scale a helix
    reads the same. Flat-shaded flanks, chamfered tip at z0."""
    rmaj, rmin, n = d / 2, d / 2 - 0.6134 * pitch, max(2, round(2 * (z1 - z0) / pitch))
    prof = [(rmin * 0.8, z0)] + [(rmaj if k % 2 else rmin, z0 + (z1 - z0) * k / n) for k in range(1, n)] + [(rmin, z1)]
    bm = bmesh.new()
    rings = [[bm.verts.new(P(cx + r * math.cos(2 * math.pi * j / seg), cy + r * math.sin(2 * math.pi * j / seg), z))
              for j in range(seg)] for r, z in prof]
    for a, b in zip(rings, rings[1:]):
        for j in range(seg):
            bm.faces.new((a[j], a[(j + 1) % seg], b[(j + 1) % seg], b[j]))
    bm.faces.new(list(reversed(rings[0])))
    bm.faces.new(rings[-1])
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    return mesh_obj(name, bm, M['steel'])


def screw(name, cx, cy, d, dk, k, length, a, z_seat, up, pitch):
    """ISO 14580 cheese-head Torx screw, head on the seat at GLB z_seat, shank running toward +z (up) or -z."""
    s = 1 if up else -1
    h0, h1 = sorted((z_seat, z_seat - s * k))
    torx(prism(name, M['steel'], circle(cx, cy, dk / 2), h0, h1, 0.12 * dk), cx, cy, a,
         *(sorted((z_seat - s * 0.55 * k, z_seat - s * (k + 0.2)))))
    thread(f'{name}_shank', cx, cy, d, pitch, *sorted((z_seat + s * length, z_seat - s * 0.3)))


# ---------------------------------------------------------------- 21700 cells in BT1 / BT2
# Generic 21700 (21.15 x 70.35 mm) lathed from one profile: printed PVC sleeve rolled over both shoulders, crimp
# bead under the + end, nickel-plated can ends, paper insulator ring, black gasket, + button. Seated by ray-casting
# the holder model (resting on its cradle, + button on the + contact, a leaf spring taking the slack at the - end).
# The holder housing is black PA66 per its datasheet, not the export's grey.
CELL_R, CELL_L, SH = 10.575, 70.35, 0.875           # radius, length to the + button top, shoulder radius
TOP = CELL_L - 1.0                                   # crimped rim: the + button stands 1.0 mm proud of it
BEAD = TOP - 3.4                                     # crimp bead centre
WR, CN, IN, GK = range(4)                            # sleeve, nickel can/cap, insulator, gasket


def arc(cr, cs, rad, a0, a1, m, k=6):
    return [(cr + rad * math.cos(math.radians(a0 + (a1 - a0) * t / k)),
             cs + rad * math.sin(math.radians(a0 + (a1 - a0) * t / k)), m) for t in range(k + 1)]


# (radius, distance from the - face, material of the strip up to the next point)
PROFILE = ([(0, 0.10, CN), (8.70, 0.10, WR), (8.80, 0.0, WR)] + arc(CELL_R - SH, SH, SH, -90, 0, WR)
           + [(CELL_R - 0.25 * (1 + math.cos(math.pi * t / 6)), BEAD + 0.9 * t / 6, WR) for t in range(-6, 7)]
           + arc(CELL_R - SH, TOP - SH, SH, 0, 90, WR)
           + [(9.15, TOP, IN), (9.10, TOP + 0.15, IN), (4.95, TOP + 0.15, IN), (4.90, TOP, GK), (4.30, TOP - 0.05, CN),
              (4.20, TOP + 0.05, CN)] + arc(3.85, CELL_L - 0.35, 0.35, 0, 90, CN) + [(0, CELL_L, CN)])


def lathe(name, prof, mats, cx, y_neg, d, zc, n=144):
    """Revolve prof about a pcb-y axis at pcb x cx, GLB z zc; s runs from pcb y y_neg in direction d (+-1).
    UV: u around (0.5 faces away from the board, handedness kept so the print reads right), v = s / CELL_L."""
    bm = bmesh.new()
    uvl = bm.loops.layers.uv.new()
    ang = lambda j: -math.pi / 2 + d * 2 * math.pi * (j / n - 0.5)
    rings = [[bm.verts.new(P(cx + r * math.cos(ang(j)), y_neg + d * s, zc + r * math.sin(ang(j)))) for j in range(n if r else 1)]
             for r, s, _ in prof]
    for i in range(len(prof) - 1):
        a, b, sa, sb = rings[i], rings[i + 1], prof[i][1], prof[i + 1][1]
        for j in range(n):
            quad = []
            for ring, k, s in ((a, j, sa), (a, j + 1, sa), (b, j + 1, sb), (b, j, sb)):
                v = ring[k % len(ring)]
                if all(v is not q[0] for q in quad):   # a pole ring is one vertex: the quad becomes a triangle
                    quad.append((v, k, s))
            f = bm.faces.new([q[0] for q in quad])
            f.material_index = prof[i][2]
            for loop, (_, k, s) in zip(f.loops, quad):
                loop[uvl].uv = (k / n, s / CELL_L)
    bmesh.ops.recalc_face_normals(bm, faces=bm.faces)
    o = mesh_obj(name, bm, mats[0], 40)
    for m in mats[1:]:
        o.data.materials.append(m)
    return o


def seat(ref, xc, yc, r):
    """Ray-cast the holder model: (cell axis GLB z resting on the cradle, contact pcb y on the -y side, on the +y side)."""
    verts, polys = [], []
    for o in (bpy.data.objects[ref], *bpy.data.objects[ref].children_recursive):
        if o.type == 'MESH':
            k = len(verts)
            verts += [o.matrix_world @ v.co for v in o.data.vertices]
            polys += [[k + i for i in p.vertices] for p in o.data.polygons]
    bvh = BVHTree.FromPolygons(verts, polys)
    hit = lambda x, y, z, dy, dz: bvh.ray_cast(P(x, y, z), Vector((0, dy, dz)))[0]
    zc = BACK
    for yy in range(-30, 31, 3):                     # the middle 60 mm, clear of the end contacts
        for k in range(-40, 41):
            dx = r * k / 41
            h = hit(xc + dx, yc + yy, -40, 0, 1)
            zc = min(zc, (h.z / MM if h else BACK) - math.sqrt(r * r - dx * dx))
    lo, hi = hit(xc, yc, zc, 1, 0), hit(xc, yc, zc, -1, 0)    # GLB +Y is pcb -y
    return zc, -lo.y / MM, -hi.y / MM


def leaf(name, cx, y_wall, y_face, zc):
    """Stamped - contact: a 5 mm strip from low on the end wall to the middle of the - cap."""
    bm = bmesh.new()
    (y0, z0), (y1, z1) = (y_wall, zc + 4.5), (y_face, zc)
    n = math.hypot(y1 - y0, z1 - z0)
    ny, nz = -(z1 - z0) / n * 0.15, (y1 - y0) / n * 0.15
    vs = [bm.verts.new(P(cx + sx, y + sy, z + sz)) for sx in (-2.5, 2.5) for y, z in ((y0, z0), (y1, z1))
          for sy, sz in ((ny, nz), (-ny, -nz))]
    bmesh.ops.convex_hull(bm, input=vs)
    return mesh_obj(name, bm, M['nickel'], 30)


# sleeve print, drawn along the cell (x: - end to + end) and around it (y: its middle faces away from the board)
PX = 16                                              # label pixels per mm
px = lambda v: round(v * PX)
font = lambda h, bold=True: ImageFont.truetype(f'/usr/share/fonts/TTF/DejaVuSansCondensed{"-Bold" if bold else ""}.ttf', px(h))
SLEEVE, INK, ACC = (24, 60, 124), (236, 240, 244), (126, 178, 228)
lab = Image.new('RGB', (px(CELL_L), px(2 * math.pi * CELL_R)), SLEEVE)
g, yc = ImageDraw.Draw(lab), lab.height / 2
for s in (-11, 11):
    g.line([(px(1.5), yc + px(s)), (px(64), yc + px(s))], fill=ACC, width=px(0.35))
g.text((px(5), yc - px(3.2)), '21700', font=font(6.2), fill=INK, anchor='lm')
g.text((px(5) + g.textlength('21700', font=font(6.2)) + px(1.6), yc - px(2.6)), 'Li-ion', font=font(3.0, False), fill=ACC, anchor='lm')
g.text((px(5), yc + px(3.6)), '3.6 V  ·  5330 mAh  ·  19.2 Wh', font=font(2.4, False), fill=INK, anchor='lm')
g.text((px(5), yc + px(7.4)), 'INR21700  rechargeable lithium-ion cell', font=font(1.5, False), fill=INK, anchor='lm')
for i, t in enumerate(('WARNING: Do not short-circuit,', 'crush, disassemble, heat above', '100 °C, incinerate or immerse.',
                       'Charge only with a Li-ion', 'charger at 0-45 °C.', 'Keep away from children.')):
    g.text((px(38), yc - px(9.2) + i * px(1.45)), t, font=font(1.0, False), fill=INK, anchor='lm')
rng, xb = random.Random(21700), px(38)               # a barcode-like block
while xb < px(53):
    w = rng.choice((1, 1, 2, 3)) * PX // 8
    g.rectangle([xb, yc + px(1.6), xb + w - 1, yc + px(5.4)], fill=INK)
    xb += w + rng.choice((1, 2, 3)) * PX // 8
g.text((px(60), yc), '+', font=font(4.4), fill=INK, anchor='mm')
label_path = os.path.splitext(OUT)[0] + '_cell.png'
lab.rotate(90, expand=True).save(label_path)         # -> columns around (u), rows along (v, + end at the top)
sleeve = mat('cell_sleeve', (1, 1, 1), 0.0, 0.28, **{'Coat Weight': 0.6, 'Coat Roughness': 0.08})
tx = sleeve.node_tree.nodes.new('ShaderNodeTexImage')
tx.image = bpy.data.images.load(label_path)
tx.image.pack()
tx.interpolation = 'Cubic'
sleeve.node_tree.links.new(tx.outputs['Color'], sleeve.node_tree.nodes['Principled BSDF'].inputs['Base Color'])

for ref, x, y, plus_to_plus_y in (('BT1', 138.45, 83.06, True), ('BT2', 113.30, 83.15, False)):
    for o in (bpy.data.objects[ref], *bpy.data.objects[ref].children_recursive):   # dark plastic -> black PA66
        for s in getattr(o, 'material_slots', ()):
            b = s.material.node_tree.nodes.get('Principled BSDF') if s.material and s.material.node_tree else None
            if b and b.inputs['Metallic'].default_value < 0.5 and max(b.inputs['Base Color'].default_value[:3]) < 0.1:
                s.material = M['nylon']
    zc, c_lo, c_hi = seat(ref, x, y, CELL_R)
    d = 1 if plus_to_plus_y else -1
    y_neg = (c_hi if d > 0 else c_lo) - d * (CELL_L + 0.02)
    lathe(f'{ref}_cell', PROFILE, [sleeve, M['nickel'], M['insul'], M['nylon']], x, y_neg, d, zc)
    leaf(f'{ref}_spring', x, c_lo if d > 0 else c_hi, y_neg + d * 0.10, zc)
    print(f'{ref}: axis {zc:.2f} mm, contacts y {c_lo:.2f} / {c_hi:.2f}, - end gap {abs(y_neg - (c_lo if d > 0 else c_hi)):.2f} mm')

# ---------------------------------------------------------------- display mount: stock hardware (docs/display-mount.md)
# H1-H4 (MountingHole_2.2mm_M2_DIN965_Pad, 2.14 mm in from the R3 corners): M2 x 6 brass hex spacers F-F, 3.5 mm
# across flats (a 4 mm hex's corners reach J11/J13's courtyards and pass the board edge). The tallest front parts are
# U12 3.9 mm and L4/L5 3.8 mm (GLB), so 6 mm leaves the plate 2 mm over them. M2 x 4 ISO 14580 cheese-head Torx (T6,
# dk 3.8, k 1.3) from both sides: 2.0 mm into the spacer through the plate, 2.4 mm through the board, 1.6 mm apart.
HOLES = [(98.25, 37.66), (153.45, 37.66), (98.25, 162.86), (153.45, 162.86)]
SO_H, SO_AF, PLATE_T, SCREW_L, M2_CLR, M3_CLR = 6.0, 3.5, 2.0, 4.0, 2.4, 3.4   # clearance holes: ISO 273 medium
PL0, PL1 = FRONT + SO_H, FRONT + SO_H + PLATE_T
for i, (hx, hy) in enumerate(HOLES):
    cut(prism(f'standoff{i}', M['brass'], hexagon(hx, hy, SO_AF), FRONT, PL0, 0.15, None),
        [circle(hx, hy, 0.8, 24)], FRONT - 1, PL0 + 1, 'bore')              # tapped through, M2 minor 1.6
    screw(f'screw_top{i}', hx, hy, 2.0, 3.8, 1.3, SCREW_L, 1.75, PL1, False, 0.4)
    screw(f'screw_bot{i}', hx, hy, 2.0, 3.8, 1.3, SCREW_L, 1.75, BACK, True, 0.4)

# WeAct 3.7" module (PCB 53.80 x 105.79, M3 holes 2.8 mm in): M3 x 5 nylon hex spacers F-F (5 mm across flats) on the
# plate, M3 x 4 ISO 14580 Torx (T10, dk 5.5, k 2.0) through the module (2.4 mm in) and up from under the plate (2.0 mm
# in, 0.6 mm apart). Those under-plate heads hang 4.0 mm over the board: 0.66 mm over J7's shell at the +x/+y corner.
# Plate: 2 mm clear cast acrylic cut to the board outline (60 x 130, R3) with the clearance holes; --dxf writes it.
MW, ML, MSO, M3_L, PLATE = 53.80, 105.79, 5.0, 4.0, (60.0, 130.0, 3.0)
MHOLES = [(CX + sx * (MW / 2 - 2.8), CY + sy * (ML / 2 - 2.8)) for sx in (-1, 1) for sy in (-1, 1)]
cut(prism('acrylic_plate', M['acryl'], rrect(CX, CY, *PLATE), PL0, PL1, 0.25, 30, M['acryl_edge']),
    [circle(x, y, M2_CLR / 2, 32) for x, y in HOLES] + [circle(x, y, M3_CLR / 2, 32) for x, y in MHOLES],
    PL0 - 1, PL1 + 1, face=1)
z = PL1
for i, (hx, hy) in enumerate(MHOLES):
    cut(prism(f'nylon_{i}', M['nylon'], hexagon(hx, hy, 5.0), z, z + MSO, 0.15, None),
        [circle(hx, hy, 1.25, 24)], z - 1, z + MSO + 1, 'bore')               # tapped through, M3 minor 2.5
    screw(f'm3_{i}', hx, hy, 3.0, 5.5, 2.0, M3_L, 2.74, z + MSO + 1.6, False, 0.5)
    screw(f'm3b_{i}', hx, hy, 3.0, 5.5, 2.0, M3_L, 2.74, PL0, True, 0.5)
z += MSO
cut(prism('module_pcb', M['modpcb'], rrect(CX, CY, MW, ML, 1.4), z, z + 1.6, 0.1, 30),
    [circle(x, y, 1.6, 32) for x, y in MHOLES], z - 1, z + 2.6)
z += 1.6
if DXF:   # the plate's laser-cut file: R12 entities in mm, origin at its lower-left corner, y up (pcb y runs down)
    w, h, r = PLATE
    x0, y0 = CX - w / 2, CY + h / 2
    ents = [('LINE', (10, r), (20, 0), (11, w - r), (21, 0)), ('LINE', (10, w), (20, r), (11, w), (21, h - r)),
            ('LINE', (10, w - r), (20, h), (11, r), (21, h)), ('LINE', (10, 0), (20, h - r), (11, 0), (21, r))]
    ents += [('ARC', (10, ax), (20, ay), (40, r), (50, a), (51, a + 90))
             for ax, ay, a in ((w - r, r, 270), (w - r, h - r, 0), (r, h - r, 90), (r, r, 180))]
    ents += [('CIRCLE', (10, x - x0), (20, y0 - y), (40, d / 2)) for holes, d in ((HOLES, M2_CLR), (MHOLES, M3_CLR))
             for x, y in holes]
    with open(DXF, 'w') as fh:
        fh.write('0\nSECTION\n2\nHEADER\n9\n$INSUNITS\n70\n4\n0\nENDSEC\n0\nSECTION\n2\nENTITIES\n')
        for kind, *g in ents:
            fh.write(f'0\n{kind}\n8\nCUT\n' + ''.join(f'{c}\n{v:.4f}\n' for c, v in g))
        fh.write('0\nENDSEC\n0\nEOF\n')
PW, PLN = 51.4, 91.5                      # panel; FPC end toward pcb +y
pan_bot = CY + ML / 2 - 8.5
pcy = pan_bot - PLN / 2
prism('panel', M['subst'], rrect(CX, pcy, PW, PLN, 0.6, 3), z, z + 0.95, 0.05, None)
pp = prism('paper', M['paper'], [(CX - 25.4, pcy - 45.45), (CX + 25.4, pcy - 45.45), (CX + 25.4, pcy + 45.45),
                                 (CX - 25.4, pcy + 45.45)], z + 0.95, z + 1.0, None, None)
uv = pp.data.uv_layers.new(name='UV')
for loop in pp.data.loops:
    co = pp.data.vertices[loop.vertex_index].co
    uv.data[loop.index].uv = ((co.x / MM - (CX - 25.4)) / 50.8, ((-co.y / MM) - (pcy - 45.45)) / 90.9)
    uv.data[loop.index].uv[1] = 1 - uv.data[loop.index].uv[1]
box('fpc_flat', M['fpc'], CX - 5, pan_bot - 0.5, CX + 5, CY + ML / 2 - 0.6, z, z + 0.12, 0.03)
box('fpc_bend', M['fpc'], CX - 5, CY + ML / 2 - 0.6, CX + 5, CY + ML / 2 - 0.45, z - 1.6, z + 0.12, 0.03)

# ---------------------------------------------------------------- one rigid object: centre it, stand it up, put it on a turntable
roots = [o for o in sc.objects if o.parent is None]
lo, hi = Vector((1e9,) * 3), Vector((-1e9,) * 3)
for o in sc.objects:
    if o.type == 'MESH':
        for c in o.bound_box:
            w = o.matrix_world @ Vector(c)
            lo, hi = Vector(map(min, lo, w)), Vector(map(max, hi, w))
centre = Vector((CX * MM, -CY * MM, (lo.z + hi.z) / 2))
print('extent mm', [round(v * 1000, 1) for v in lo], [round(v * 1000, 1) for v in hi])
rig = bpy.data.objects.new('rig', None); sc.collection.objects.link(rig)
rig.matrix_world = Matrix.Rotation(math.radians(90), 4, 'X') @ Matrix.Translation(-centre)
for o in roots:
    o.parent = rig
    o.matrix_parent_inverse = Matrix.Identity(4)
spin = bpy.data.objects.new('turntable', None); sc.collection.objects.link(spin)
rig.parent = spin
sc.frame_start, sc.frame_end = 1, N
keys(spin, 'rotation_euler', turn, 2)     # seamless: frame N+1 is frame 1 a turn later

# Exploded view along the board normal (GLB z): each layer rides an empty: (objects, mm out at full spread, leaves,
# comes back). Front layers leave outermost first and close innermost first, STAG apart, so none overtakes the part it
# sits on; the cells slide out of the back meanwhile. Every gap opens to G, clear of the screw shanks. The board and
# everything else (None) shift too, keeping the spread centred in frame (weighted to the front end: it is nearer the
# camera and fans out wider than the cells).
G = 6.0
LAYERS = [(p, d, TX0 + 2 * STAG, TX1 - XI + 2 * STAG) for p, d in (('BT[12]_cell', -14.0), ('screw_bot.*', -(SCREW_L + G)))]
for rank, pat, d in ((0, 'm3_.*', 5 * G + M3_L), (1, 'module_pcb.*|panel|paper|fpc_.*', 4 * G),
                     (1, 'screw_top.*', 3 * G + SCREW_L), (2, 'nylon_.*', 3 * G), (3, 'acrylic_plate.*', 2 * G),
                     (4, 'standoff\\d.*|m3b_.*', G)):
    LAYERS.append((pat, d, TX0 + rank * STAG, TX1 - XI + (4 - rank) * STAG))
apart = lambda t, t_out, t_in: sstep((t % SECONDS - t_out) / X_OUT) - sstep((t % SECONDS - t_in) / X_IN)
out = {p: (lambda t, d=d, a=a, b=b: d * apart(t, a, b)) for p, d, a, b in LAYERS}
centre = lambda t: -(0.65 * out['m3_.*'](t) + 0.35 * out['BT[12]_cell'](t))      # the two ends of the spread
for chain in (('standoff\\d.*|m3b_.*', 'acrylic_plate.*', 'screw_top.*'),
              ('acrylic_plate.*', 'nylon_.*', 'module_pcb.*|panel|paper|fpc_.*', 'm3_.*')):
    for under, over in zip(chain, chain[1:]):   # no part ever sinks into the one it sits on
        assert all(out[over](f / FPS) >= out[under](f / FPS) - 1e-9 for f in range(N)), (under, over)
for pat, d, t_out, t_in in LAYERS + [(None, 0.0, 0.0, 0.0)]:
    group = ([o for o in extras if re.fullmatch(pat, o.name)] if pat else
             [o for o in rig.children if not o.name.startswith('explode ')])
    assert group, pat
    e = bpy.data.objects.new(f'explode {pat or "board"}', None); sc.collection.objects.link(e)
    e.parent = rig
    for o in group:
        o.parent = e
    keys(e, 'location', lambda t, d=d, a=t_out, b=t_in: MM * (d * apart(t, a, b) + centre(t)), 2)

# 'stack': what render.sh's bare pass hides, the display mount (every extra but the cells and their springs)
stack = bpy.data.collections.new('stack')
sc.collection.children.link(stack)
for o in extras:
    if not re.fullmatch(r'BT\d_(cell|spring)', o.name):
        stack.objects.link(o)
        sc.collection.objects.unlink(o)


# Frame plan: a still stretch renders once. A frame whose state matches an earlier one's (turntable and every layer at
# shutter open, middle and close, e-paper image, the 1 Hz LED, and the RGB hue while the back is in view) reuses that
# render. seq_<name>.txt gives the rendered frame for each output frame: spin (the loop), plain (spin without the
# explode window, which starts and ends on the same still frame) and bare (plain with 'stack' hidden).
def state(f, bare=False):
    ts = [(f - 1 + d) / FPS for d in (-0.25, 0.0, 0.25)]
    t = ts[1]
    return (tuple(round(turn(u) % (2 * math.pi), 7) for u in ts), tuple(round(v(u), 6) for v in out.values() for u in ts),
            None if bare else tick(t), t % 1 < 0.1, f if math.cos(turn(t)) < 0.3 else None)


plain = [f for f in range(1, N + 1) if not TX0 <= (f - 1) / FPS < TX1]
first, first_bare = {}, {}
seqs = {'spin': [first.setdefault(state(f), f) for f in range(1, N + 1)]}
seqs['plain'] = [first[state(f)] for f in plain]
seqs['bare'] = [first_bare.setdefault(state(f, True), f) for f in plain]
for name, seq in seqs.items():
    with open(os.path.join(os.path.dirname(os.path.abspath(OUT)), f'seq_{name}.txt'), 'w') as fh:
        fh.write(''.join(f'{f}\n' for f in seq))
    print(f'{name}: {len(seq)} frames, {len(set(seq))} rendered')

# vertical framing: object height along pcb y (world z after the stand-up)
H_OBJ = (hi.y - lo.y)                     # metres
z_mid = ((hi.y + lo.y) / 2 + CY * MM)     # GLB y of the middle, relative to the centre -> world z
cam = bpy.data.cameras.new('cam')
cam.lens, cam.sensor_fit, cam.sensor_height = 100, 'VERTICAL', 36
EL = math.radians(9)
dist = (H_OBJ / 2 * 1.16) / (18 / 100)
target = Vector((0, 0, z_mid))
camo = bpy.data.objects.new('cam', cam); sc.collection.objects.link(camo)
camo.location = target + Vector((0, -dist * math.cos(EL), dist * math.sin(EL)))
camo.rotation_euler = (target - camo.location).to_track_quat('-Z', 'Y').to_euler()
sc.camera = camo
print('object height mm', round(H_OBJ * 1000, 1), 'camera distance mm', round(dist * 1000))

# ---------------------------------------------------------------- lights: soft key, fill, two rim strips, top; dim studio HDRI for reflections
def area(name, loc, size, power, shape='RECTANGLE', size_y=None, color=(1, 1, 1)):
    L = bpy.data.lights.new(name, 'AREA')
    L.shape, L.size, L.energy, L.color = shape, size, power, color
    if size_y:
        L.size_y = size_y
    o = bpy.data.objects.new(name, L); sc.collection.objects.link(o)
    o.location = Vector(loc)
    o.rotation_euler = (target - o.location).to_track_quat('-Z', 'Y').to_euler()
    return o

area('key', (-0.58, -0.34, 0.46), 0.22, 60, size_y=0.32, color=(1.0, 0.97, 0.93))
area('fill', (0.62, -0.30, -0.08), 0.5, 14, color=(0.90, 0.95, 1.0))
area('rim_l', (-0.42, 0.40, 0.06), 0.035, 55, size_y=0.7, color=(0.80, 0.90, 1.0))
area('rim_r', (0.44, 0.38, 0.10), 0.035, 55, size_y=0.7, color=(1.0, 0.93, 0.84))
area('top', (0.05, 0.10, 0.62), 0.28, 34)

w = bpy.data.worlds.new('world'); sc.world = w
if not w.node_tree:
    w.use_nodes = True
wn = w.node_tree
env = wn.nodes.new('ShaderNodeTexEnvironment')
env.image = bpy.data.images.load(os.path.join(bpy.utils.system_resource('DATAFILES', path='studiolights/world'), 'interior.exr'))
wn.links.new(env.outputs['Color'], wn.nodes['Background'].inputs['Color'])
# Glossy rays see the room brighter than diffuse ones: polished finishes (SMA gold, stainless shells, solder) need
# something to mirror or they read as dark brown, while the diffuse fill stays the dim interior.
lp, k = wn.nodes.new('ShaderNodeLightPath'), wn.nodes.new('ShaderNodeMath')
k.operation, k.inputs[1].default_value, k.inputs[2].default_value = 'MULTIPLY_ADD', ENV_GLOSSY - 0.12, 0.12
wn.links.new(lp.outputs['Is Glossy Ray'], k.inputs[0])
wn.links.new(k.outputs['Value'], wn.nodes['Background'].inputs['Strength'])

# ---------------------------------------------------------------- render settings
sc.render.engine = 'CYCLES'
cy = sc.cycles
prefs = bpy.context.preferences.addons['cycles'].preferences
prefs.compute_device_type = 'HIP'
prefs.get_devices()
for dev in prefs.devices:
    dev.use = dev.type == 'HIP'
cy.device = 'GPU'
cy.samples, cy.adaptive_threshold, cy.use_denoising, cy.denoiser = SPP, 0.008, True, 'OPENIMAGEDENOISE'
cy.denoising_use_gpu = True
cy.max_bounces, cy.diffuse_bounces, cy.glossy_bounces = 12, 4, 6
cy.max_bounces = 32
cy.transmission_bounces, cy.transparent_max_bounces = 32, 16
cy.caustics_reflective = cy.caustics_refractive = False
cy.sample_clamp_indirect = 4.0
sc.render.film_transparent = True
cy.film_transparent_glass = True
sc.render.use_motion_blur, sc.render.motion_blur_shutter = True, 0.5
# Persistent data off: with it and motion blur, Blender 5.2 renders the first still frame after a move without its
# object sync (the 2026-11-15 run: frames 406 and 827 came out dark, silkscreen gone). Costs ~0.3 s a frame.
sc.render.use_persistent_data = False
sc.render.fps = FPS
sc.render.resolution_x, sc.render.resolution_y, sc.render.resolution_percentage = RES[0], RES[1], 100
im = sc.render.image_settings
im.file_format, im.color_mode, im.color_depth, im.compression = 'PNG', 'RGBA', '16', 15
sc.view_settings.view_transform = 'AgX'
for look in ('AgX - Punchy', 'Punchy', 'AgX - Medium High Contrast'):
    try:
        sc.view_settings.look = look
        break
    except TypeError:
        pass
sc.view_settings.exposure = 0.0
bpy.ops.wm.save_as_mainfile(filepath=OUT, compress=False)
print('frames', N, 'look', sc.view_settings.look)

if TEST:
    import time
    for f in map(int, TEST[1].split(',')):
        sc.frame_set(f)
        sc.render.filepath = f'{TEST[0]}/f{f:04d}.png'
        t = time.time()
        bpy.ops.render.render(write_still=True)
        print('rendered frame', f, 'turntable deg', round(math.degrees(turn((f - 1) / FPS)) % 360, 1),
              'apart', [round(apart((f - 1) / FPS, a, b), 2) for _, _, a, b in LAYERS], round(time.time() - t, 1), 's')
