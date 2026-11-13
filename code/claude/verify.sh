#!/usr/bin/env bash
# parsnip-node's skill check: the tool commands named in this repo's own docs, then
# the pcb-skills suite with goldens on this board (golden.json = its own commands).
#
#   code/claude/verify.sh [REV]    # REV: a pcb-skills commit (default HEAD) or a snapshot dir
#
# Exit codes as pcb-skills' tools/verify.sh: 0 pass, 1 a failure, 2 only the goldens moved.
cd "$(git rev-parse --show-toplevel)" || exit 1
S=$(dirname "$(dirname "$(realpath .claude/skills/kicad-review)")")   # the repo the skills link to
echo "== tool commands named in CLAUDE.md, agents, TODO"
python3 $S/skills/kicad-review/tests/selftest.py --lint CLAUDE.md .claude/agents/*.md docs/TODO.md || exit 1
exec $S/tools/verify.sh "${1:-HEAD}" . code/claude/golden.json
