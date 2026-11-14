#!/usr/bin/env bash
# Composite RGBA turntable frames over the backdrop and encode a looping 60 fps AVIF.
# Every frame lasts 16.70 ms (setts bsf, 1/100000 timescale), measured in headless Chrome/Firefox:
# - Chrome skips ahead and stalls re-decoding AV1 (~20 fps) when a frame is shorter than the refresh
#   interval, even by 0.02 ms: exact 1/60 s collapses on 59.94 Hz panels; 16.70 clears them (59.9 fps shown).
# - Firefox truncates to whole ms (16), so it runs ~4% fast; a 16/17/17 ms pattern would suit it exactly
#   but costs Chrome a visible hitch every ~1.2 s at 60 Hz.
# Random access (8-frame pyramid) with every layer on the same quantizer: ~25-30% smaller than low delay
# at equal PSNR and visibly sharper at equal size. Per-frame PSNR swings 0.4 dB at CRF 22 (the default offsets
# swing more and shimmer at 7.5 Hz); tf=0 keeps the hidden ARFs unsmoothed. Intra frames come out ~2 dB
# softer than inter frames at the same quantizer (a one-frame blink every 2 s), so keyframes get -28 qindex:
# flat PSNR and no size cost. Measured at 1080x1512: Chrome 60 fps, Firefox decode ~10 ms avg / 28 ms worst.
# usage: encode.sh FRAMES_DIR BG.png OUT_W CRF OUT.avif [PIXFMT]
set -euo pipefail
FR=$1 BG=$2 OW=$3 CRF=$4 OUT=$5 PF=${6:-yuv420p10le}
ffmpeg -hide_banner -loglevel error -y \
  -framerate 60 -i "$FR/f%04d.png" -loop 1 -framerate 60 -i "$BG" \
  -filter_complex "[1:v][0:v]overlay=shortest=1:format=yuv444p10,\
scale=${OW}:-2:flags=lanczos+accurate_rnd+full_chroma_int:out_color_matrix=bt709:out_range=full,format=${PF},\
setparams=color_primaries=bt709:color_trc=iec61966-2-1:colorspace=bt709:range=full" \
  -fps_mode passthrough -enc_time_base 1/100000 \
  -bsf:v "setts=pts=N*1670:dts=N*1670:duration=1670" \
  -c:v libsvtav1 -preset 2 -crf "$CRF" -g 120 \
  -svtav1-params pred-struct=2:hierarchical-levels=3:use-fixed-qindex-offsets=1:key-frame-qindex-offset=-28:qindex-offsets=0,0,0,0:enable-tf=0 \
  "$OUT" 2> >(grep -v '^Svt' >&2)
