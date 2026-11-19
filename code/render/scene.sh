#!/usr/bin/env bash
# KiCad board -> GLB -> Blender import -> dress.py's dressed scene, $W/scene.blend (used by render.sh and stills.sh).
# usage: scene.sh BOARD.kicad_pcb W [dress.py options: --res W H --spp N --dxf PLATE.dxf]
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
BOARD=$1 W=$2; shift 2
blender() { PATH=/usr/bin:$PATH command blender -b "$@"; }  # a uv python3.x earlier on PATH breaks Blender's stdlib
mkdir -p "$W"
kicad-cli-nightly pcb export glb --no-dnp --subst-models --include-tracks --include-pads --include-zones \
  --include-silkscreen --include-soldermask -f -o "$W/board.glb" "$BOARD"
blender --factory-startup --python-expr "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); \
bpy.ops.import_scene.gltf(filepath='$W/board.glb'); bpy.ops.wm.save_as_mainfile(filepath='$W/base.blend')"
python3 "$HERE/screen.py" "$W/screen.png"
blender "$W/base.blend" --python "$HERE/dress.py" -- "$W/scene.blend" "$W/screen.png" 15 "$@"
