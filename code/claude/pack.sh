#!/bin/sh
# Rebuild the .skill bundles from the tracked folders. Run after editing one.
cd "$(dirname "$0")" || exit 1
for d in */; do
    d=${d%/}
    [ -f "$d/SKILL.md" ] || continue
    rm -f "$d.skill"
    zip -q -r -D "$d.skill" "$d" -x '*__pycache__*' '*.pyc' '*/prototypes/*' || exit 1
    echo "packed $d.skill ($(du -h "$d.skill" | cut -f1))"
done
