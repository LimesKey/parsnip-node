"""kpcb.py `height`: 3D part heights from KiCad's GLB export."""
import sys, os, re, json
from kcommon import refrange, natkey, trunc, prefix
from kpcb_board import _f, hit

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
