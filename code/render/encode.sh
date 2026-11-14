#!/usr/bin/env bash
# Composite RGBA turntable frames over the backdrop and encode a looping 60 fps AVIF.
# Every frame lasts 16.70 ms (setts bsf, 1/100000 timescale), measured in headless Chrome/Firefox:
# - Chrome skips ahead and stalls re-decoding AV1 (~20 fps) when a frame is shorter than the refresh
#   interval, even by 0.02 ms: exact 1/60 s collapses on 59.94 Hz panels; 16.70 clears them (59.9 fps shown).
# - Firefox truncates to whole ms (16), so it runs ~4% fast; a 16/17/17 ms pattern would suit it exactly
#   but costs Chrome a visible hitch every ~1.2 s at 60 Hz.
# SVT-AV1's own random-access tuning (layered quantizers, temporal filtering), 16-frame pyramid, preset 0
# (~10 min). Against a flat-quantizer pyramid at the same size: +1.3 dB mean PSNR, +1 dB on the worst frame.
# The extra frame-to-frame swing is a 30/15 Hz odd/even alternation, invisible at 60 fps; the slow (<=7.5 Hz)
# part is no larger than the flat encode's. Preset 0 is +0.2 dB over preset 1; 32-frame pyramids add nothing.
# Keyframes get +1.2 dB by default (a sharpening step every 2 s): use-fixed-qindex-offsets=2 keeps the layered
# quantizers but honours key-frame-qindex-offset, and +16 flattens the step at no size cost.
# Variance boost, QM, SCM, dlf=2: no gain on this content. Decode: Chrome 60 fps, Firefox ~10.5 ms/frame.
# usage: [PRESET=0] [KFQ=16] encode.sh FRAMES_DIR BG.png OUT_W CRF OUT.avif [PIXFMT]   (longencode.sh: PRESET=-1)
set -euo pipefail
FR=$1 BG=$2 OW=$3 CRF=$4 OUT=$5 PF=${6:-yuv420p10le}
ffmpeg -hide_banner -loglevel error -y \
  -framerate 60 -i "$FR/f%04d.png" -loop 1 -framerate 60 -i "$BG" \
  -filter_complex "[1:v][0:v]overlay=shortest=1:format=yuv444p10,\
scale=${OW}:-2:flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=full,format=${PF},\
setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709:range=full" \
  -fps_mode passthrough -enc_time_base 1/100000 \
  -bsf:v "setts=pts=N*1670:dts=N*1670:duration=1670" \
  -c:v libsvtav1 -preset "${PRESET:-0}" -crf "$CRF" -g 120 \
  -svtav1-params "pred-struct=2:hierarchical-levels=4:use-fixed-qindex-offsets=2:key-frame-qindex-offset=${KFQ:-16}" \
  "$OUT" 2> >(grep -v '^Svt' >&2)
