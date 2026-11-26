#!/usr/bin/env bash
# The Blender that scene.sh, render.sh and stills.sh run, in background mode; BLENDER=/path/to/blender overrides it.
# Pinned to the 5.4.0 alpha daily cf42c1d99faf (builder.blender.org, 2026-11-19, tarball sha256 in tarball.sha256 next
# to it) until a 5.3.x fixes 5.3.0's Cycles HIP fault. Do not update it. On 2026-11-19 (render.sh's scene, frames 1/7/16,
# 200 spp): 5.3.0 page-faults in shade_surface as a render starts with the default path-state pool (2/2), 5.4 is clean
# (5/5) at 13.2 s a frame, 5.3.0 needs CYCLES_CONCURRENT_STATES_FACTOR=0.45 (0.6 and up fault) and takes 15.1 s.
# Same image (79.5 dB vs 5.3.0; 5.3.0 vs itself 82). Going back to a system 5.3.x: BLENDER=/usr/bin/blender, and set
# that factor again unless the fix is in.
# The tarball bundles Python 3.13 with numpy; dress.py also needs Pillow, matched to the system's:
#   ~/opt/blender-5.4.0-alpha+main.cf42c1d99faf/5.4/python/bin/python3.13 -I -m pip install Pillow==13.0.0
# /usr/bin first: a uv python3.x earlier on PATH breaks the system Blender's stdlib.
PATH=/usr/bin:$PATH exec "${BLENDER:-$HOME/opt/blender-5.4.0-alpha+main.cf42c1d99faf/blender}" -b "$@"
