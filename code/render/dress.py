"""Dress base.blend (KiCad GLB import) for the README turntable: PBR materials, render-only extras
(2x 21700 cells, M2 standoffs, acrylic plate, WeAct 3.7" e-paper),
studio lights, camera, 60 fps turntable, Cycles on the HIP GPU. Run by render.sh.

blender -b base.blend -P dress.py -- OUT.blend SCREEN.png SECONDS [--test DIR deg,deg,...] [--res W H] [--spp N]
GLB frame: metres, X = pcb x, Y = -pcb y, Z = out of the front; board body z 0..1.5 mm.
Board coordinates below (holes, BT1/BT2) come from `kpcb.py parsnip.kicad_pcb where REF`:
re-check them after a layout change."""
import bpy, bmesh, math, os, sys, colorsys, random
from mathutils import Vector, Matrix
from mathutils.bvhtree import BVHTree
from PIL import Image, ImageDraw, ImageFont

argv = sys.argv[sys.argv.index('--') + 1:]
OUT, SCREEN, SECONDS = argv[0], argv[1], float(argv[2])
opt = lambda k, n=1: argv[argv.index(k) + 1:argv.index(k) + 1 + n] if k in argv else None
TEST = opt('--test', 2)
RES = [int(v) for v in (opt('--res', 2) or (1080, 1512))]
SPP = int((opt('--spp') or [256])[0])
FPS = 60

sc = bpy.context.scene
MM = 0.001
FRONT, BACK = 1.55, -0.05            # board surfaces, GLB z in mm (mask level)
CX, CY = 125.85, 100.26              # board centre, pcb mm
P = lambda x, y, z=0.0: Vector((x * MM, -y * MM, z * MM))   # pcb mm + GLB z mm -> GLB metres


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


M = {
    'enig':   mat('enig', (1.0, 0.74, 0.36), 1.0, 0.24),
    'cu':     mat('cu_under_mask', (0.80, 0.46, 0.25), 1.0, 0.35),
    'mask':   mat('mask', (0.010, 0.011, 0.012), 0.0, 0.42, **{'Coat Weight': 1.0, 'Coat Roughness': 0.10, 'Alpha': 0.93}),
    'silk':   mat('silk', (0.80, 0.80, 0.78), 0.0, 0.7),
    'fr4':    mat('fr4', (0.42, 0.40, 0.24), 0.0, 0.65),
    'nickel': mat('nickel', (0.70, 0.68, 0.63), 1.0, 0.30),
    'insul':  mat('insul', (0.75, 0.72, 0.62), 0.0, 0.6),
    'brass':  mat('brass', (0.86, 0.66, 0.34), 1.0, 0.28),
    'steel':  mat('steel', (0.62, 0.62, 0.64), 1.0, 0.26),
    'nylon':  mat('nylon', (0.018, 0.018, 0.018), 0.0, 0.55),
    'acryl':  clear('acrylic', (0.97, 0.99, 0.99), 1.49, 0.005),
    'acryl_edge': clear('acrylic_edge', (0.93, 0.97, 0.97), 1.49, 0.35),
    'modpcb': mat('module_pcb', (0.008, 0.009, 0.010), 0.0, 0.35, **{'Coat Weight': 1.0, 'Coat Roughness': 0.12}),
    'subst':  mat('panel_glass', (0.30, 0.31, 0.31), 0.0, 0.15),
    'fpc':    mat('fpc', (0.42, 0.17, 0.015), 0.0, 0.3, **{'Coat Weight': 0.5}),
}
# e-paper: texture on the paper area, matte with a faint protective-film sheen
paper = mat('paper', (1, 1, 1), 0.0, 0.62, **{'Coat Weight': 0.35, 'Coat Roughness': 0.25})
nt = paper.node_tree
tex = nt.nodes.new('ShaderNodeTexImage')
tex.image = bpy.data.images.load(SCREEN)
tex.interpolation = 'Cubic'
nt.links.new(tex.outputs['Color'], nt.nodes['Principled BSDF'].inputs['Base Color'])
M['paper'] = paper

# KiCad's GLB gives every component STEP colour metallic=1, roughness=1 (reclassified in place below). Only the
# board layers differ (material names are export-order numbers, so match on these): copper roughness 0.4
# (pads grey, tracks/zones yellow), silk 0.9, FR4 0.8, mask alpha < 1.
def board_layer(b):
    r, a, c = (b.inputs[k].default_value for k in ('Roughness', 'Alpha', 'Base Color'))
    if a < 0.99:
        return 'mask'
    if abs(r - 0.4) < 0.01:
        return 'cu' if c[2] < 0.2 * c[0] else 'enig'
    return {0.9: 'silk', 0.8: 'fr4'}.get(round(r, 2))


for m in [m for m in bpy.data.materials if m.name.startswith('mat_')]:
    b = m.node_tree.nodes['Principled BSDF']
    layer = board_layer(b)
    if layer:
        new = M[layer]
    else:
        c = b.inputs['Base Color'].default_value[:3]
        h, s, v = colorsys.rgb_to_hsv(*[x ** (1 / 2.2) for x in c])
        if s < 0.2 and v > 0.6:                       # tin / nickel / steel pins and shields
            b.inputs['Metallic'].default_value, b.inputs['Roughness'].default_value = 1.0, 0.3
        elif 0.07 < h < 0.17 and s > 0.35 and v > 0.6:  # gold / brass contacts
            b.inputs['Metallic'].default_value, b.inputs['Roughness'].default_value = 1.0, 0.25
        else:                                          # epoxy, ceramic, plastic
            b.inputs['Metallic'].default_value, b.inputs['Roughness'].default_value = 0.0, (0.55 if v < 0.25 else 0.45)
        continue
    for o in bpy.data.objects:
        for slot in o.material_slots:
            if slot.material == m:
                slot.material = new

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

# ---------------------------------------------------------------- standoffs, acrylic plate, screws
HOLES = [(98.25, 37.66), (153.45, 37.66), (98.25, 162.86), (153.45, 162.86)]
SO_H, PLATE_T = 8.0, 2.0
PL0, PL1 = FRONT + SO_H, FRONT + SO_H + PLATE_T
for i, (hx, hy) in enumerate(HOLES):
    prism(f'standoff{i}', M['brass'], hexagon(hx, hy, 4.0), FRONT, PL0, 0.15, None)
    prism(f'screw_top{i}', M['steel'], circle(hx, hy, 1.9), PL1, PL1 + 1.3, 0.45)
    prism(f'screw_bot{i}', M['steel'], circle(hx, hy, 1.9), BACK - 1.3, BACK, 0.45)
prism('acrylic_plate', M['acryl'], rrect(CX, CY, 60.0, 130.0, 3.0), PL0, PL1, 0.25, 30, M['acryl_edge'])

# ---------------------------------------------------------------- WeAct 3.7" module (PCB 53.80 x 105.79, M3 holes 2.8 mm in)
MW, ML, MSO = 53.80, 105.79, 5.0
z = PL1
for sx in (-1, 1):
    for sy in (-1, 1):
        hx, hy = CX + sx * (MW / 2 - 2.8), CY + sy * (ML / 2 - 2.8)
        prism(f'nylon_{sx}{sy}', M['nylon'], hexagon(hx, hy, 5.0), z, z + MSO, 0.15, None)
        prism(f'm3_{sx}{sy}', M['steel'], circle(hx, hy, 2.6), z + MSO + 1.6, z + MSO + 1.6 + 1.6, 0.5)
z += MSO
prism('module_pcb', M['modpcb'], rrect(CX, CY, MW, ML, 1.4), z, z + 1.6, 0.1, 30)
z += 1.6
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

N = round(SECONDS * FPS)
A0 = math.radians(-35)                    # frame 1: front three-quarter view (also the still/poster frame)
sc.frame_start, sc.frame_end = 1, N
drv = spin.driver_add('rotation_euler', 2).driver      # exactly linear, seamless (frame N+1 == frame 1)
drv.type, drv.expression = 'SCRIPTED', f'{A0!r} + 2*pi*(frame - 1)/{N}'

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
wn.nodes['Background'].inputs['Strength'].default_value = 0.12

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
sc.render.use_persistent_data = True
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
    tdir, angles = TEST[0], [float(a) for a in TEST[1].split(',')]
    for a in angles:
        f = 1 + round(((math.radians(a) - A0) % (2 * math.pi)) / (2 * math.pi) * N)
        sc.frame_set(f)
        print('turntable deg', round(math.degrees(spin.matrix_world.to_euler().z), 2))
        sc.render.filepath = f'{tdir}/t{int(a):03d}.png'
        t = time.time()
        bpy.ops.render.render(write_still=True)
        print('rendered', a, 'deg frame', f, round(time.time() - t, 1), 's')
