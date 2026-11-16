#!/usr/bin/env bash
# README turntable: KiCad board -> GLB -> Blender Cycles on the GPU (HIP) -> 60 fps AV1 AVIFs, each with a .jpg
# poster and a .gif to share in chat, in OUT_DIR:
#   render-spin       15 s loop: front, back, front, then the display mount comes apart and back (the README one)
#   render-assembled  the same loop without the exploded stretch (shared frames, 11 s)
#   render-bare       render-assembled without the display mount: board and cells
# ~2.7 h on an RX 6800 XT (a still stretch renders once); PREVIEW=1 does a 360 px, 16 spp pass. Also writes the
# display plate's laser-cut file, docs/display-plate.dxf.
# usage: code/render/render.sh [--detach] [BOARD.kicad_pcb] [OUT_DIR]      (defaults: parsnip.kicad_pcb, lib/img)
#   --detach: the whole job as the systemd user unit parsnip-render (survives the terminal and Claude Code, blocks
#   idle suspend), then longencode.sh on render-spin unless PREVIEW (~5 h more, notifies when done). Logs in $W.
#   systemctl --user status parsnip-render / stop parsnip-render;  tail -f ~/.cache/parsnip-render/render.log
# Needs kicad-cli-nightly, blender (Cycles HIP), ffmpeg with libsvtav1, python3 with numpy + Pillow.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
DETACH=; [ "${1:-}" = --detach ] && { DETACH=1; shift; }
BOARD=$(realpath -m "${1:-$ROOT/parsnip.kicad_pcb}") OUT=$(realpath -m "${2:-$ROOT/lib/img}")
W=${W:-${XDG_CACHE_HOME:-$HOME/.cache}/parsnip-render}    # ~5 GB of 16-bit frames: keep it off tmpfs
if [ -n "$DETACH" ]; then
  mkdir -p "$W"
  exec systemd-run --user --unit=parsnip-render --collect --expand-environment=no -E W="$W" -E PREVIEW="${PREVIEW:-}" -E PATH="/usr/bin:$PATH" \
    systemd-inhibit --what=idle:sleep --who=parsnip-render --why="README render + AV1 long encode" \
    bash -c '"$0" "$1" "$2" > "$W/render.log" 2>&1 && { [ -n "$PREVIEW" ] || DST="$2/render-spin.avif" nice -n 10 "${0%/*}/longencode.sh"; }' \
    "$HERE/render.sh" "$BOARD" "$OUT"
fi
blender() { PATH=/usr/bin:$PATH command blender -b "$@"; }  # a uv python3.x earlier on PATH breaks Blender's stdlib
if [ -n "${PREVIEW:-}" ]; then SW=360 SH=504 SPP=16; else SW=1080 SH=1512 SPP=200; fi
rm -rf "$W"/{frames,plain,bare,r_full,r_bare} && mkdir -p "$W"/{r_full,r_bare}

kicad-cli-nightly pcb export glb --no-dnp --subst-models --include-tracks --include-pads --include-zones \
  --include-silkscreen --include-soldermask -f -o "$W/board.glb" "$BOARD"
blender --factory-startup --python-expr "import bpy; bpy.ops.wm.read_factory_settings(use_empty=True); \
bpy.ops.import_scene.gltf(filepath='$W/board.glb'); bpy.ops.wm.save_as_mainfile(filepath='$W/base.blend')"
python3 "$HERE/screen.py" "$W/screen.png"
blender "$W/base.blend" --python "$HERE/dress.py" -- "$W/scene.blend" "$W/screen.png" 15 --res $SW $SH --spp $SPP \
  --dxf "$ROOT/docs/display-plate.dxf"
# Two passes, the second with the display mount hidden; dress.py's seq_*.txt give the rendered frame for every output
# frame of each loop, then each loop is a directory of hard links (render-spin's is frames/, what longencode reads).
frames_of() { sort -nu "$W/seq_$1.txt" | paste -sd,; }
blender "$W/scene.blend" -o "$W/r_full/f####" -f "$(frames_of spin)"
blender "$W/scene.blend" --python-expr "import bpy; bpy.data.collections['stack'].hide_render = True" \
  -o "$W/r_bare/f####" -f "$(frames_of bare)"
link() {   # link SEQ SRC DST
  mkdir -p "$W/$3"; local i=0 f
  while read -r f; do i=$((i + 1)); ln "$W/$2/$(printf f%04d.png "$f")" "$W/$3/$(printf f%04d.png "$i")"; done < "$W/seq_$1.txt"
}
link spin r_full frames; link plain r_full plain; link bare r_bare bare
python3 "$HERE/bg.py" $SW $SH "$W/bg"
ffmpeg -hide_banner -loglevel error -y -f rawvideo -pix_fmt rgb48le -s ${SW}x$SH -i "$W/bg.rgb48" -frames:v 1 "$W/bg.png"
# Full render width, shown at half of it in the README (width="540"): 1:1 on 2x screens, an exact 2:1
# on 1x ones (browsers scale animated images with plain bilinear, which aliases at odd ratios like 1.5:1).
for v in spin:frames assembled:plain bare:bare; do
  d=$W/${v#*:} o=$OUT/render-${v%:*}
  "$HERE/encode.sh" "$d" "$W/bg.png" $SW 25 "$o.avif"   # whole steps: SVT rounds CRF in this mode
  ffmpeg -hide_banner -loglevel error -y -i "$W/bg.png" -i "$d/f0001.png" \
    -filter_complex "overlay=format=yuv444p10" -frames:v 1 -q:v 3 "$o.jpg"
  # Chat copy (Discord embeds GIF links reliably). GIF delays are whole centiseconds: every 3rd frame at 5 cs is 20 fps
  # with an exactly even cadence (3 refreshes at 60 Hz) and the same 15 s turn. One 128-colour palette with ordered
  # dither keeps the static backdrop still from frame to frame. raw.githubusercontent.com sends files over 10 MiB as
  # application/octet-stream (measured: 10.21 MB image/gif, 10.56 MB octet-stream), which chat embeds may refuse, so
  # both outputs stay under it: 336 px wide is ~10.1 MB here.
  ffmpeg -hide_banner -loglevel error -y -framerate 60 -i "$d/f%04d.png" -loop 1 -framerate 60 -i "$W/bg.png" \
    -filter_complex "[1:v][0:v]overlay=shortest=1:format=gbrp,select='not(mod(n\,3))',scale=336:-2:flags=lanczos,\
settb=1/100,setpts=N*5,split[a][b];[a]palettegen=max_colors=128:stats_mode=diff[p];\
[b][p]paletteuse=dither=bayer:bayer_scale=5:diff_mode=rectangle" \
    -fps_mode passthrough -enc_time_base 1/100 -final_delay 5 -loop 0 "$o.gif"
done
ls -l "$OUT"/render-{spin,assembled,bare}.{avif,jpg,gif}
for f in "$OUT"/render-{spin,assembled,bare}.{avif,gif}; do [ "$(stat -c %s "$f")" -lt 10485760 ] || echo "WARNING: $f is over 10 MiB: GitHub raw will serve it as octet-stream"; done
