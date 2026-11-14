#!/usr/bin/env bash
# README turntable (lib/img/render-spin.avif + render-spin.jpg poster + render-spin.gif to share in chat): KiCad
# board -> GLB -> Blender Cycles on the GPU (HIP) -> 60 fps AV1 AVIF. ~1.7 h on an RX 6800 XT; PREVIEW=1 does a
# 360 px, 16 spp pass in ~5 min.
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
"$HERE/encode.sh" "$W/frames" "$W/bg.png" $SW 25 "$OUT/render-spin.avif"   # whole steps: SVT rounds CRF in this mode
ffmpeg -hide_banner -loglevel error -y -i "$W/bg.png" -i "$W/frames/f0001.png" \
  -filter_complex "overlay=format=yuv444p10" -frames:v 1 -q:v 3 "$OUT/render-spin.jpg"
# Chat copy (Discord embeds GIF links reliably). GIF delays are whole centiseconds: every 3rd frame at 5 cs is 20 fps
# with an exactly even cadence (3 refreshes at 60 Hz) and the same 15 s turn. One 128-colour palette with ordered
# dither keeps the static backdrop still from frame to frame. raw.githubusercontent.com sends files over 10 MiB as
# application/octet-stream (measured: 10.21 MB image/gif, 10.56 MB octet-stream), which chat embeds may refuse, so
# both outputs stay under it: 336 px wide is ~10.1 MB here.
ffmpeg -hide_banner -loglevel error -y -framerate 60 -i "$W/frames/f%04d.png" -loop 1 -framerate 60 -i "$W/bg.png" \
  -filter_complex "[1:v][0:v]overlay=shortest=1:format=gbrp,select='not(mod(n\,3))',scale=336:-2:flags=lanczos,\
settb=1/100,setpts=N*5,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];\
[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
  -fps_mode passthrough -enc_time_base 1/100 -final_delay 5 -loop 0 "$OUT/render-spin.gif"
ls -l "$OUT"/render-spin.{avif,jpg,gif}
for f in "$OUT"/render-spin.{avif,gif}; do [ "$(stat -c %s "$f")" -lt 10485760 ] || echo "WARNING: $f is over 10 MiB: GitHub raw will serve it as octet-stream"; done
