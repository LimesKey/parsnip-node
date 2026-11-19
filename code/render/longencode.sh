#!/usr/bin/env bash
# Slow re-encode of the README spin at the same file size. SVT-AV1 preset -1 (the slowest real preset in 4.2; -2 is
# a fast debug mode) takes ~2.5 h per attempt for 900 frames at 1080x1512 on a Ryzen 5 7600 and gave 6.7% fewer
# bytes with +0.13 dB over preset 0 (libaom cpu-used 0 was no better and blinks on keyframes).
# Size search in qindex steps (Q = 4 x CRF + d): with use-fixed-qindex-offsets=2 SVT rounds CRF to whole steps
# (~7% of size apart), so d = 0-3 rides on qindex-offsets for every layer and the keyframe (~1.8% and ~0.05 dB per
# step here). Secant on ln(size) from Q: typically 2-3 attempts to the finest Q that fits under the current
# lib/img/render-spin.avif (or TARGET bytes). Installs it only if it also has a higher mean PSNR against the rendered
# frames (old one kept as $W/render-spin.prev.avif). KFQ 12 keeps keyframes level with their neighbours at preset -1:
# on the 2026-11-15 frames at Q 103, 16 left them 0.24 dB below (a softening blink), 12 -0.04 dB for +0.10 dB mean at
# +0.4% bytes; 8 put them +0.3 dB above (a sharpening blink every 2 s). encode.sh's 16 is for preset 0.
# Needs the frames render.sh left in $W (don't re-run render.sh meanwhile: it deletes them).
# render.sh --detach runs this after the render, on render-spin.
#
#   code/render/longencode.sh --detach      run as a systemd user unit: survives the terminal / Claude Code,
#                                           blocks idle suspend while it runs; HOURS=15 caps it
#   TARGET=8800000 code/render/longencode.sh --detach    aim under a smaller size (still must beat the current PSNR)
#   tail -f ~/.cache/parsnip-render/longencode.log     follow it      systemctl --user stop parsnip-longencode     stop it
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
W=${W:-${XDG_CACHE_HOME:-$HOME/.cache}/parsnip-render}
DST=${DST:-$ROOT/lib/img/render-spin.avif}
HOURS=${HOURS:-15} Q=${Q:-} KFQ=${KFQ:-12} TARGET=${TARGET:-}   # Q unset: the seed below

if [ "${1:-}" = --detach ]; then
  exec systemd-run --user --unit=parsnip-longencode --collect --nice=10 \
    -E W="$W" -E DST="$DST" -E HOURS="$HOURS" -E Q="$Q" -E KFQ="$KFQ" -E TARGET="$TARGET" -E PATH="/usr/bin:$PATH" \
    systemd-inhibit --what=idle:sleep --who=parsnip-longencode --why="AV1 encode of the README render" "$HERE/longencode.sh"
fi

exec >>"$W/longencode.log" 2>&1
log() { echo "$(date '+%F %T') $*"; }
[ -f "$W/frames/f0900.png" ] && [ -f "$W/bg.png" ] && [ -f "$DST" ] || { log "missing frames, bg.png or $DST"; exit 1; }
TARGET=${TARGET:-$(stat -c %s "$DST")} DEADLINE=$(( $(date +%s) + HOURS * 3600 ))
(( TARGET < 10400000 )) || TARGET=10400000   # GitHub raw serves files over 10 MiB as application/octet-stream
# First attempt: preset -1 at Q 108 came out 0.912 x the size of render.sh's preset 0 CRF 25 (Q 100) file
# (2026-11-15), so aim the first try at TARGET from that; the secant below takes it from there.
[ -n "$Q" ] || Q=$(awk -v s="$(stat -c %s "$DST")" -v t="$TARGET" \
  'BEGIN {n = 108 + log(0.912 * s / t) / 0.0183; print (n == int(n) ? n : int(n) + 1)}')

quality() {   # "mean worst kf" RGB PSNR of an AVIF against the rendered frames over the backdrop; kf = how far the
              # keyframes sit above the 4 frames either side (a sharpening blink every 2 s when it passes ~0.2 dB)
  local s; s=$(mktemp)
  ffmpeg -hide_banner -loglevel error -i "$1" -framerate 60 -i "$W/frames/f%04d.png" -loop 1 -framerate 60 -i "$W/bg.png" \
    -lavfi "[2:v][1:v]overlay=shortest=1:format=gbrp,settb=1/60,setpts=N[r];[0:v:1]format=gbrp,settb=1/60,setpts=N[d];[d][r]psnr=stats_file=$s" \
    -f null - 2> >(grep -v 'non monotonically' >&2)
  grep -oE 'psnr_avg:[0-9.]+' "$s" | cut -d: -f2 | awk '{p[NR - 1] = $1; t += $1; if (NR == 1 || $1 < m) m = $1}
    END {for (k = 120; k + 4 < NR; k += 120) {a = 0; for (i = 1; i <= 4; i++) a += p[k - i] + p[k + i]; d += p[k] - a / 8; n++}
         printf "%.3f %.2f %+.2f", t / NR, m, n ? d / n : 0}'
  rm -f "$s"
}
enc() {   # qindex $1 = 4 x CRF + d
  local d=$(( $1 % 4 ))
  PRESET=-1 KFQ=$(( KFQ + d )) XP=":qindex-offsets=$d,$d,$d,$d,$d" "$HERE/encode.sh" "$W/frames" "$W/bg.png" 1080 $(( $1 / 4 )) "$2"
}

read -r REF REF_MIN REF_KF <<< "$(quality "$DST")"
log "start: target <= $TARGET bytes, current file $REF dB (worst $REF_MIN, keyframes $REF_KF), Q $Q, keyframe offset $KFQ, ${HOURS} h cap"
K=0.0183   # ln(size) per qindex at preset -1 on this render (Q 104 -> 108: 9.90 -> 9.20 MB), re-fitted from each pair of attempts
fit= over= tried=" " q=$Q pq= ps= last=0
while [[ "$tried" != *" $q "* ]] && (( $(date +%s) + last < DEADLINE )); do
  tried+="$q "; out="$W/longencode_q$q.avif" t0=$(date +%s)
  enc "$q" "$out"
  last=$(( $(date +%s) - t0 )) size=$(stat -c %s "$out")
  log "Q $q (CRF $(( q / 4 )) + $(( q % 4 ))): $size bytes in $(( last / 60 )) min, PSNR mean/worst/keyframes $(quality "$out")"
  if (( size <= TARGET )); then [ -n "$fit" ] && (( fit < q )) || fit=$q; else [ -n "$over" ] && (( over > q )) || over=$q; fi
  [ -n "$fit" ] && [ -n "$over" ] && (( fit == over + 1 )) && break           # finest qindex that fits: done
  [ -n "$pq" ] && K=$(awk -v a="$ps" -v b="$size" -v d=$(( q - pq )) 'BEGIN {k = log(a / b) / d; print (k > 0.005 ? k : 0.005)}')
  pq=$q ps=$size
  q=$(awk -v q="$q" -v s="$size" -v t="$TARGET" -v k="$K" 'BEGIN {n = q + log(s / t) / k; print (n == int(n) ? n : int(n) + (n > 0))}')
  [ -n "$fit" ] && (( q >= fit )) && q=$(( fit - 1 ))                         # ceil of the secant, kept between the
  [ -n "$over" ] && (( q <= over )) && q=$(( over + 1 ))                       # finest fit and the coarsest overshoot
done
[ -n "$fit" ] || { log "no attempt fit under $TARGET bytes before the deadline; $DST left as is"; exit 1; }

out="$W/longencode_q$fit.avif"
frames=$(ffprobe -v error -select_streams v:1 -count_packets -show_entries stream=nb_read_packets -of csv=p=0 "$out")
read -r new new_min new_kf <<< "$(quality "$out")"
log "best Q $fit: $(stat -c %s "$out") bytes, $frames frames, $new dB (worst $new_min, keyframes $new_kf; current $REF dB)"
if [ "$frames" = 900 ] && awk -v a="$new" -v b="$REF" 'BEGIN {exit !(a > b)}'; then
  cp "$DST" "$W/render-spin.prev.avif" && cp "$out" "$DST.tmp" && mv "$DST.tmp" "$DST"
  log "installed $DST"; msg="README spin re-encoded: $new dB vs $REF dB at $(stat -c %s "$DST") bytes"
else
  log "not better, $DST left as is"; msg="Long encode finished, no improvement; README spin unchanged"
fi
notify-send "parsnip render" "$msg" 2>/dev/null || true
