#!/usr/bin/env bash
# Every check a skill change must pass, in one command (~2 min):
#
#   code/claude/verify.sh [REV]        # REV = the golden baseline, default HEAD
#
# Exit 0 = all pass, 1 = a test, lint or pyflakes failed, 2 = only the goldens moved.
# Goldens: REV's scripts record the real board's outputs on a frozen copy of the
# project, then the working tree's scripts diff against that same copy, so KiCad
# saving mid-run can't fake a diff. A pure refactor must read N/N unchanged; a
# feature's diff is expected - read it, don't waive it. Known non-regressions:
# a part.py diff that is only `fx: 1 USD = ...` and the prices it scales (the FX
# rate refreshes daily: rerun), and a golden command REV doesn't know yet (it
# records as REV's usage error until committed). REV may also be a directory holding
# a code/claude copy: snapshot it (`mkdir S; cp -r code S/`) before a refactor on top of
# uncommitted skill work, since HEAD would then diff the earlier work too.
set -uo pipefail
cd "$(git rev-parse --show-toplevel)" || exit 1
R=code/claude K=code/claude/kicad-review P=code/claude/part-search
REV=${1:-HEAD}
fail=0 moved=0

echo "== kicad-review selftest"
python3 $K/references/selftest.py | grep -v '^ok ' || fail=1
echo "== part-search offline selftest"
python3 $P/scripts/part.py selftest --offline || fail=1
echo "== pyflakes"
uvx pyflakes $R/*/scripts/*.py $K/references/selftest.py && echo "clean" || fail=1
echo "== tool commands named in CLAUDE.md, agents, TODO"
python3 $K/references/selftest.py --lint CLAUDE.md .claude/agents/*.md docs/TODO.md || fail=1

echo "== goldens: $REV's scripts vs the working tree, on a frozen copy"
F=$(mktemp -d) H=$(mktemp -d)
trap 'rm -rf "$F" "$H"' EXIT
if [ -d "$REV" ]; then mkdir -p "$H/code" && cp -r "$REV/$R" "$H/code/"
else git archive "$REV" $R | tar -x -C "$H"; fi
cp -p ./*.kicad_sch ./*.kicad_pro ./*.kicad_dru parsnip.kicad_pcb parsnip-merged.net kdrc.json \
      fp-lib-table sym-lib-table "$F"
if [ -d "$REV" ]; then git show HEAD:fpcheck.json; else git show "$REV":fpcheck.json; fi > "$F/fpcheck.json"
ln -s "$PWD/lib" "$PWD/docs" "$F/"
for T in "$H" "$PWD"; do                    # REV records, the working tree diffs
    (cd "$F" && python3 "$T/$K/references/selftest.py" --golden g parsnip.kicad_pcb parsnip-merged.net \
                | grep -v '^same') || moved=1
    (cd "$F" && python3 "$T/$P/scripts/part.py" selftest --golden pg parsnip-merged.net \
                | grep -v '^same') || moved=1
done

if [ $fail = 1 ]; then echo "== FAIL"; exit 1; fi
if [ $moved = 1 ]; then echo "== tests pass; goldens CHANGED vs $REV (read the diff above)"; exit 2; fi
echo "== all pass, goldens unchanged vs $REV"
