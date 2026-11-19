"""README PCB stills from dress.py's scene: front and back three-quarter, top and bottom, bare board (display mount
and cells hidden). dress.py's camera and studio lights stay put and the board is posed in front of them for each view
(path traced, the turntable's materials and lights). The camera is fitted to the board's silhouette. Run by stills.sh.

blender -b scene.blend -P stills.py -- OUT_DIR SCALE SPP [VIEW ...]      writes OUT_DIR/VIEW.png, 16-bit RGBA,
SCALE x the full README size below (stills.sh renders 2x and halves it)."""
import bpy, math, sys
import numpy as np
from mathutils import Matrix, Vector

argv = sys.argv[sys.argv.index('--') + 1:]
OUT, SCALE, SPP = argv[0], float(argv[1]), int(argv[2])
EL = math.radians(9)                      # dress.py's camera elevation: the lights are set around it
# Three-quarter views, matched by eye to the KiCad renders they replace (a close, wide-angle camera): long axis turned
# PHI from the image plane, seen from EL3 above the board, camera D3 m away, rolled ROLL (+ turns the image clockwise).
PHI, EL3, D3, ROLL = math.radians(32), math.radians(34), 0.16, math.radians(-6)
# Lit for an upright board, a flat one is flooded, and the overhead 'top' light falls in the mask's broad highlight
# and greys the whole board out: the three-quarter views turn the lights down and that one off.
DIM = {'key': 0.35, 'fill': 0.35, 'rim_l': 0.35, 'rim_r': 0.35, 'top': 0.0}
VIEWS = {   # name: (width px, pcb -y (SMA end) and front-normal directions with the board flat in front of the
            #        camera, X right and Y away; view elevation, camera distance m or None = orthographic, lights)
    'iso':      (2400, (math.cos(PHI), math.sin(PHI), 0), (0, 0, 1), EL3, D3, DIM),
    'iso-back': (2400, (math.cos(PHI), math.sin(PHI), 0), (0, 0, -1), EL3, D3, DIM),
    'top':      (1400, (0, 1, 0), (0, 0, 1), math.radians(90), None, {}),
    'bottom':   (1400, (0, 1, 0), (0, 0, -1), math.radians(90), None, {}),
}
views = argv[3:] or list(VIEWS)
# The room (dress.py's HDRI) tilts with the board, so the face in view always looks up at the ceiling, and is turned so
# a flat board's mirror direction lands on plain wall: unturned, the three-quarter views mirror a ceiling lamp.
ROOM_YAW = math.radians(-146)

sc = bpy.context.scene
sc.frame_set(1)                           # assembled, LEDs lit
bpy.data.collections['stack'].hide_render = True
for n in ('BT1_cell', 'BT2_cell', 'BT1_spring', 'BT2_spring'):
    bpy.data.objects[n].hide_render = True
sc.render.use_motion_blur = False
power = {n: bpy.data.objects[n].data.energy for n in ('key', 'fill', 'rim_l', 'rim_r', 'top')}
sc.cycles.samples, sc.cycles.adaptive_threshold = SPP, 0.004
prefs = bpy.context.preferences.addons['cycles'].preferences   # user prefs, not saved in the .blend: without this
prefs.compute_device_type = 'HIP'                                  # Cycles falls back to the CPU (hours, not minutes)
prefs.use_hiprt = True      # 18% faster here (512 spp per view); render.sh turns it off, its frames are BVH-build bound
prefs.get_devices()
for dev in prefs.devices:
    dev.use = dev.type == 'HIP'
assert any(dev.use for dev in prefs.devices), 'no HIP device'
tt = bpy.data.objects['turntable']
tt.animation_data_clear()
cam = sc.camera
cam.animation_data_clear()
cam.data.clip_start, cam.data.sensor_fit = 0.01, 'AUTO'
wn = sc.world.node_tree
room = wn.nodes.new('ShaderNodeMapping')
room.vector_type = 'VECTOR'
wn.links.new(wn.nodes.new('ShaderNodeTexCoord').outputs['Generated'], room.inputs['Vector'])
wn.links.new(room.outputs['Vector'], next(n for n in wn.nodes if n.type == 'TEX_ENVIRONMENT').inputs['Vector'])
meshes = [o for o in sc.objects if o.type == 'MESH' and not o.hide_render
          and not any(c.hide_render for c in o.users_collection)]


def basis(up, normal):
    """Rotation taking the stood-up board (pcb +x = X, pcb -y = Z, front = -Y) to the pcb -y / normal directions."""
    up, n = Vector(up).normalized(), Vector(normal).normalized()
    src = Matrix(((1, 0, 0), (0, 0, 1), (0, -1, 0))).transposed()   # columns: pcb +x, pcb -y, normal
    return Matrix([up.cross(n), up, n]).transposed() @ src.inverted()


for name in views:
    width, up, normal, el, dist, dim = VIEWS[name]
    for n, p in power.items():
        bpy.data.objects[n].data.energy = p * dim.get(n, 1)
    # Flat pose seen from elevation el == the pose tipped (el - EL) toward dress.py's camera, which stays at EL.
    d = Vector((0, -math.cos(EL), math.sin(EL)))
    tilt = Matrix.Rotation(el - EL, 3, 'X')
    if dist:
        tilt = Matrix.Rotation(-ROLL, 3, d) @ tilt
    tt.matrix_world = (tilt @ basis(up, normal)).to_4x4()
    room.inputs['Rotation'].default_value = (tilt @ Matrix.Rotation(ROOM_YAW, 3, 'Z')).inverted().to_euler('XYZ')
    cam.location = d * (dist or 0.5)
    cam.rotation_euler = (-d).to_track_quat('-Z', 'Y').to_euler()
    cam.data.type = 'PERSP' if dist else 'ORTHO'
    bpy.context.view_layer.update()
    inv, pts = cam.matrix_world.inverted(), []
    for o in meshes:
        v = np.empty(len(o.data.vertices) * 3)
        o.data.vertices.foreach_get('co', v)
        m = np.array(inv @ o.matrix_world)
        pts.append(v.reshape(-1, 3) @ m[:3, :3].T + m[:3, 3])
    p = np.concatenate(pts)
    xy = p[:, :2] / -p[:, 2:] if dist else p[:, :2]
    (x0, y0), (x1, y1) = xy.min(0), xy.max(0)
    pad = 0.02 * max(x1 - x0, y1 - y0)
    x0, y0, x1, y1 = x0 - pad, y0 - pad, x1 + pad, y1 + pad
    big = max(x1 - x0, y1 - y0)           # AUTO fit: lens / ortho scale and both shifts refer to the larger side
    if dist:
        cam.data.lens = cam.data.sensor_width / big
    else:
        cam.data.ortho_scale = big
    cam.data.shift_x, cam.data.shift_y = (x0 + x1) / 2 / big, (y0 + y1) / 2 / big
    sc.render.resolution_x = round(width * SCALE)
    sc.render.resolution_y = round(width * SCALE * (y1 - y0) / (x1 - x0))
    sc.render.filepath = f'{OUT}/{name}.png'
    bpy.ops.render.render(write_still=True)
    print('still', name, sc.render.resolution_x, 'x', sc.render.resolution_y)
