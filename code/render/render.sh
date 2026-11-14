#!/usr/bin/env bash
# README turntable (lib/img/render-spin.avif + render-spin.jpg poster): KiCad board -> GLB -> Blender Cycles
# on the GPU (HIP) -> 60 fps AV1 AVIF. ~1.5 h on an RX 6800 XT; PREVIEW=1 does a 360 px, 16 spp pass in ~5 min.
# usage: code/render/render.sh [BOARD.kicad_pcb] [OUT_DIR]      (defaults: parsnip.kicad_pcb, lib/img)
# Needs kicad-cli-nightly, blender (Cycles HIP), ffmpeg with libsvtav1, python3 with numpy + Pillow.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
BOARD=${1:-$ROOT/parsnip.kicad_pcb} OUT=${2:-$ROOT/lib/img}
W=${W:-${XDG_CACHE_HOME:-$HOME/.cache}/parsnip-render}    # ~3 GB of 16-bit frames: keep it off tmpfs
blender() { PATH=/usr/bin:$PATH command blender -b "$@"; }  # a uv python3.x earlier on PATH breaks Blender's stdlib
if [ -n "${PREVIEW:-}" ]; then SW=360 SH=504 SPP=16; else SW=1080 SH=1512 SPP=200; fi
mkdir -p "$W/frames" && rm -f "$W"/frames/f*.png

kicad-cli-nightly pcb export glb --no-dnp --subst-models --include-tracks --include-pads --include-zones \
  --include-silkscreen --include-soldermask -f -o "$W/board.glb" "$BOARD"
blender --factory-startup --python-expr "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); \
bpy.ops.import_scene.gltf(filepath='$W/board.glb'); bpy.ops.wm.save_as_mainfile(filepath='$W/base.blend')"
python3 "$HERE/screen.py" "$W/screen.png"
blender "$W/base.blend" --python "$HERE/dress.py" -- "$W/scene.blend" "$W/screen.png" 15 --res $SW $SH --spp $SPP
blender "$W/scene.blend" -o "$W/frames/f####" -a
python3 "$HERE/bg.py" $SW $SH "$W/bg"
ffmpeg -hide_banner -loglevel error -y -f rawvideo -pix_fmt rgb48le -s ${SW}x$SH -i "$W/bg.rgb48" -frames:v 1 "$W/bg.png"
# Full render width, shown at half of it in the README (width="540"): 1:1 on 2x screens, an exact 2:1
# on 1x ones (browsers scale animated images with plain bilinear, which aliases at odd ratios like 1.5:1).
"$HERE/encode.sh" "$W/frames" "$W/bg.png" $SW 22 "$OUT/render-spin.avif"
ffmpeg -hide_banner -loglevel error -y -i "$W/bg.png" -i "$W/frames/f0001.png" \
  -filter_complex "overlay=format=yuv444p10" -frames:v 1 -q:v 3 "$OUT/render-spin.jpg"
ls -l "$OUT/render-spin.avif" "$OUT/render-spin.jpg"
