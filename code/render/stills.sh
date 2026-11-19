#!/usr/bin/env bash
# README PCB stills: render-{iso,iso-back,top,bottom} in OUT_DIR, Cycles path tracing of dress.py's scene posed by
# stills.py (bare board, the turntable's studio lights). Each view renders at 2x and SPP samples, then:
#   .png   the full-size image the README links to: halved in linear light with premultiplied alpha, 8-bit dithered
#   .avif  what the README page shows, at exactly 2x its CSS width (1:1 on 2x screens, a clean 2:1 on 1x ones), 10-bit
#          4:4:4, libaom at its slowest speed (its default tune for stills is already iq). Alpha at quality 90 (RMS
#          error 0.05%, max 1.5% on edge pixels) instead of lossless pays for colour q 83 over 80 at the same total
#          bytes: +0.86 dB composited over white and over GitHub dark. 4:2:0 + sharpyuv is 1.3-2.2 dB worse at equal size.
# usage: code/render/stills.sh [BOARD.kicad_pcb] [OUT_DIR]      (defaults: parsnip.kicad_pcb, lib/img)
#   PREVIEW=1: 1/4 size, 64 spp, writes only to $W.  SPP=N overrides the 512 samples.  Q: AVIF quality (0-100).
#   ENCODE=1: skip rendering, redo the PNG/AVIF files from the last render in $W.
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
BOARD=$(realpath -m "${1:-$ROOT/parsnip.kicad_pcb}") OUT=$(realpath -m "${2:-$ROOT/lib/img}")
W=${W:-${XDG_CACHE_HOME:-$HOME/.cache}/parsnip-render/stills}
[ -n "${ENCODE:-}" ] || { [ -f "$W/scene.blend" ] && [ "$W/scene.blend" -nt "$BOARD" ]; } || "$HERE/scene.sh" "$BOARD" "$W"
V="iso:400 iso-back:400 top:250 bottom:250"   # view : its width in the README, CSS px
# 512 spp at 2x, halved: on crops of the top (mask, silk, U12 can) and iso views (SMA gold, solder) it matches 2048
# by eye; residual noise ~0.7 LSB (8-bit) per pixel and ~0.16 LSB once blurred, so no denoiser blotches. 256 looked
# the same too. Adaptive threshold 0.004 vs 0.008 saves nothing at this cap; lower bounce limits gain no speed;
# HIP-RT off is 18% slower; light tree off is 41% faster but changes the can and edge highlights (2026-11-19).
if [ -n "${PREVIEW:-}" ]; then S=0.25 SPP=64; else S=2 SPP=${SPP:-512}; fi
# Blender 5.3.0 Cycles HIP page-faults in shade_surface as a render starts when its path-state pool is the default
# size (any ROCm; 5.2.3 is clean; the "Local memory reserved 16.00E" debug line is unrelated). 0.45 of it renders,
# 26% faster than 0.25; 0.6 and up fault every time. A fault costs seconds, so the views not written yet are re-run.
export CYCLES_CONCURRENT_STATES_FACTOR=${CYCLES_CONCURRENT_STATES_FACTOR:-0.45}
ulimit -c 0   # each fault would otherwise leave a ~320 MB core in /var/lib/systemd/coredump
touch "$W/.start"
for try in 1 2 3 4 5 6; do
  [ -n "${ENCODE:-}" ] && break
  todo=; for v in $V; do [ "$W/${v%:*}.png" -nt "$W/.start" ] || todo+=" ${v%:*}"; done
  [ -z "$todo" ] && break
  [ "$try" = 6 ] && { echo "stills.sh: Blender kept failing on$todo" >&2; exit 1; }
  PATH=/usr/bin:$PATH blender -b "$W/scene.blend" --python "$HERE/stills.py" -- "$W" $S $SPP $todo ||
    echo "stills.sh: Blender failed (try $try), re-running$todo" >&2
done
[ -n "${PREVIEW:-}" ] && exit
for v in $V; do
  n=${v%:*} w=$(( 2 * ${v#*:} ))
  lin() { oiiotool "$W/$n.png" --colorconvert sRGB linear --resize:filter=lanczos3 "$1" --colorconvert linear sRGB "${@:2}"; }
  lin 50% -d uint8 --dither --attrib png:compressionLevel 9 -o "$OUT/render-$n.png"   # level 9: 6% smaller
  lin "${w}x0" -d uint16 -o "$W/$n-small.png"
  avifenc -s 0 -j all -d 10 -y 444 -q "${Q:-83}" --qalpha 90 "$W/$n-small.png" "$OUT/render-$n.avif" >/dev/null
done
ls -l "$OUT"/render-{iso,iso-back,top,bottom}.{png,avif}
