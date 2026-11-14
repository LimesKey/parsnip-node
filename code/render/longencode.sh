#!/usr/bin/env bash
# Slow re-encode of the README spin at the same file size. SVT-AV1 preset -1 is ~19x slower than preset 0
# (~3 h per attempt for 900 frames at 1080x1512 on a Ryzen 5 7600) for ~0.3 dB more PSNR at equal size
# (~10% fewer bits; libaom cpu-used 0 was no better and blinks on keyframes). Steps the CRF to land as
# close as possible under the size of the current lib/img/render-spin.avif (whole CRF steps: with
# use-fixed-qindex-offsets=2 SVT rounds CRF, 23.5/23.75/24 give the identical file), and replaces that file only if the
# result is no bigger AND has a higher mean PSNR against the rendered frames (old one kept as $W/render-spin.prev.avif).
# Needs the frames render.sh left in $W (don't re-run render.sh meanwhile: it deletes them).
#
#   code/render/longencode.sh --detach      run as a systemd user unit: survives the terminal / Claude Code,
#                                           blocks idle suspend while it runs; HOURS=15 caps it
#   tail -f ~/.cache/parsnip-render/longencode.log     follow it      systemctl --user stop parsnip-longencode     stop it
set -euo pipefail
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=$(cd "$HERE/../.." && pwd)
W=${W:-${XDG_CACHE_HOME:-$HOME/.cache}/parsnip-render}
DST=${DST:-$ROOT/lib/img/render-spin.avif}
HOURS=${HOURS:-15} CRF=${CRF:-26} KFQ=${KFQ:-8}   # preset -1 needs ~1 CRF above preset 0 for the same size

if [ "${1:-}" = --detach ]; then
  exec systemd-run --user --unit=parsnip-longencode --collect --nice=10 \
    -E W="$W" -E DST="$DST" -E HOURS="$HOURS" -E CRF="$CRF" -E KFQ="$KFQ" -E PATH="/usr/bin:$PATH" \
    systemd-inhibit --what=idle:sleep --who=parsnip-longencode --why="AV1 encode of the README render" "$HERE/longencode.sh"
fi

exec >>"$W/longencode.log" 2>&1
log() { echo "$(date '+%F %T') $*"; }
[ -f "$W/frames/f0900.png" ] && [ -f "$W/bg.png" ] && [ -f "$DST" ] || { log "missing frames, bg.png or $DST"; exit 1; }
TARGET=$(stat -c %s "$DST") DEADLINE=$(( $(date +%s) + HOURS * 3600 ))
(( TARGET < 10400000 )) || TARGET=10400000   # GitHub raw serves files over 10 MiB as application/octet-stream

psnr() {   # mean per-frame RGB PSNR of an AVIF against the rendered frames over the backdrop
  local s; s=$(mktemp)
  ffmpeg -hide_banner -loglevel error -i "$1" -framerate 60 -i "$W/frames/f%04d.png" -loop 1 -framerate 60 -i "$W/bg.png" \
    -lavfi "[2:v][1:v]overlay=shortest=1:format=gbrp,settb=1/60,setpts=N[r];[0:v:1]format=gbrp,settb=1/60,setpts=N[d];[d][r]psnr=stats_file=$s" -f null -
  grep -oE 'psnr_avg:[0-9.]+' "$s" | cut -d: -f2 | awk '{t += $1} END {printf "%.3f", t / NR}'; rm -f "$s"
}
step() { awk -v c="$1" -v d="$2" 'BEGIN {printf "%g", c + d}'; }

REF=$(psnr "$DST")
log "start: target <= $TARGET bytes, current file $REF dB, CRF $CRF, keyframe offset $KFQ, ${HOURS} h cap"
best= tried=" " crf=$CRF last=0
while [ -n "$crf" ] && [[ "$tried" != *" $crf "* ]] && (( $(date +%s) + last < DEADLINE )); do
  tried+="$crf "; out="$W/longencode_crf$crf.avif" t0=$(date +%s)
  PRESET=-1 KFQ=$KFQ "$HERE/encode.sh" "$W/frames" "$W/bg.png" 1080 "$crf" "$out"
  last=$(( $(date +%s) - t0 )) size=$(stat -c %s "$out")
  log "CRF $crf: $size bytes in $(( last / 60 )) min"
  if (( size <= TARGET )); then best=$crf crf=$(step "$crf" -1)        # fits: try one step finer
  elif [ -n "$best" ]; then break                                        # finer step overshoots: done
  else crf=$(step "$crf" 1); fi                                          # too big: one step coarser
done
[ -n "$best" ] || { log "no attempt fit under $TARGET bytes before the deadline; $DST left as is"; exit 1; }

out="$W/longencode_crf$best.avif"
frames=$(ffprobe -v error -select_streams v:1 -count_packets -show_entries stream=nb_read_packets -of csv=p=0 "$out")
new=$(psnr "$out")
log "best CRF $best: $(stat -c %s "$out") bytes, $frames frames, $new dB (current $REF dB)"
if [ "$frames" = 900 ] && awk -v a="$new" -v b="$REF" 'BEGIN {exit !(a > b)}'; then
  cp "$DST" "$W/render-spin.prev.avif" && cp "$out" "$DST.tmp" && mv "$DST.tmp" "$DST"
  log "installed $DST"; msg="README spin re-encoded: $new dB vs $REF dB at $(stat -c %s "$DST") bytes"
else
  log "not better, $DST left as is"; msg="Long encode finished, no improvement; README spin unchanged"
fi
notify-send "parsnip render" "$msg" 2>/dev/null || true
