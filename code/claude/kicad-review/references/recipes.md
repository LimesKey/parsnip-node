# Recipes

**"Review this board's layout"** - one call: `kpcb.py FILE review`. It ends by
naming the next calls to make, so there is nothing to work out first.

**"Did the board get updated from the schematic?"** `kpcb.py FILE sync`. Two lines
when clean. Run it before quoting any placement finding.

**"Review this placement / where should X go?"** `kpcb.py FILE summary`, then
`check`, then `map` to see the free space, then `where REF` on anything the check
named. `sheet` tells you which blocks are still scattered. Say plainly that these
are geometric heuristics.

**"Is there room for X here?"** `kpcb.py FILE where 130,60 -r 10`. One call.

**"Where do I put the caps around U13 / how do I lay out this buck?"**
`kpcb.py FILE ic U13`. One call gives positions, rotations, the reason for each and
a picture. Do not reason it out from pad coordinates by hand, and do not answer it
from the datasheet's layout figure alone - the figure does not know where this
board's inductor already is.

**"Which nets are stretched across the board?"** `kpcb.py FILE span`. Answers the
routing question that is answerable before routing exists.

**"Review this board."** `summary`, `check`, `around` each flagged IC, `divider` on
any FB/OVLO/UVLO net that check or around surfaces, then `kdoc.py grep` the
datasheet rule for anything that looks wrong. Say plainly which findings are
unconfirmed heuristics.

**"Where does X connect / is X right?"** `around X`. One call.

**"What voltage does this divider set / is this OVLO threshold right?"**
`divider REF.PIN` (the IC pin, e.g. `U5.OVLO`) or `divider NET` directly - one call
gives nominal and worst-case trip voltage from the actual resistor values and
tolerances, instead of `pin`/`net`/`comp` calls plus doing the math by hand.

**"Draw / show me / how do I wire ..."** On the board:
`knet.py FILE draw X -d 2 -o out.svg`. Not on the board yet: hand-write a ksch spec,
adding `--net FILE` if part of it exists. Then `present_files`.

**"What changed?"** `diff old.net` for wiring, `check --since old.net` for findings.

**"Is this RF trace right?"** `kpcb.py FILE rf` (every RF/50-ohm netclass net) or
`rf NET`. Widths/necks, microstrip Zo, side-ground gap and field-solved CPWG Zo (with
the mask), reference plane, via fence. A trace not on the board yet: `kzo.py W S H ER`.

**"How thick is the board / does it fit the pocket?"** `kpcb.py FILE height`. Check
the battery-holder caveat before quoting the stack: set measured heights in kpcb.json.

**"What if a cell goes in backwards?"** `knet.py FILE revpol`, then one `kdoc.py grep`
per IC pin it lists, for that pin's abs-max.

**"Where exactly is this pad / how far apart?"** `kpcb.py FILE where F5.1 BT1.1`.
Everything on one net: `kpcb.py FILE net NET`.

**"Get me the datasheet so I can grep it."** `part.py ds C... --save` downloads the
verified PDF into docs/datasheets/<MPN>.pdf and indexes it; then `kdoc.py grep -d MPN`.

**"What does the datasheet say about Y?"** `kdoc.py grep 'Y' --count` for the page,
then grep with context, or `page` + `view` for figures.

**"Do my footprints and values match the actual parts?"** Not this skill - use
`part-search`'s `part.py fpcheck FILE.net`. It joins each symbol's KiCad
footprint AND value to the assigned LCSC part (this skill is offline, so it can't
reach LCSC): wrong-size/family footprints, a right-footprint-wrong-value part
(36R part on a 37.4R symbol), one code on two bodies, and EOL parts. Chip sizes
and MPN-named footprints clear automatically; divergent nomenclature -> REVIEW.

# Self-test after editing a tool

```bash
python3 references/selftest.py     # 35 checks, asserts, exit 0 = all pass (~5 s)
```

Before a refactor, record the real board's outputs, then check after - a pure
refactor must be byte-identical (fixtures only prove each rule still fires):

```bash
python3 references/selftest.py --golden /tmp/golden BOARD.kicad_pcb BOARD.net   # 1st run records
python3 references/selftest.py --golden /tmp/golden BOARD.kicad_pcb BOARD.net   # later: diffs, exit 1 on change
uvx pyflakes scripts/*.py        # must print nothing (exit 0)
```

`selftest.py` is the source of truth for the exact commands and expected counts -
one `(label, argv, exit, substrings)` line per check, so covering a new rule is a
one-line add. Fixtures: `selftest_nightly.kicad_pcb` is `selftest.kicad_pcb` in
10.99 `(transform ...)` form (must give identical `check`); `mini_project()` writes a
throwaway project for what a static fixture can't carry (an NC marker on a wire
stub, a .kicad_sch newer than the .net, a missing top-level sheet, a BAV199 on the
wrong dual-diode symbol, a one-cell stack with an unfused TVS for `revpol`, a tiny
`t.pretty` footprint library for FPPAD and fused-pad PARPIN, `rf.net` for RFSTUB's
shunt/choke exclusions, `fet.net` for revpol's gate-driven FET), plus formula checks
(microstrip Zo, the CPWG field solver vs exact cases, glTF transform). `kdrc`, `kmerge` and `height` need
kicad-cli, so they are verified against the real board, not in selftest. `sync` on a real board+net that *match* prints IN SYNC in two lines;
the two fixtures here are deliberately different circuits, so `sync` is the
negative test - all five SYNC rules fire at once (23 error, 7 warn).

The fixture has no regulator, so `ic` is exercised against the real board instead:
`kpcb.py board.kicad_pcb ic` must list the switchers, and `ic <a buck>` must name an
input cap, an inductor and a feedback part and print a diagram whose IC body is
visible inside the frame. A blank-looking frame means the anchor shift was applied
to the slots but not to the IC's own geometry.

`selftest.kicad_pcb` is a 40x30 mm fixture carrying one deliberate fault per rule,
including both `EDGECLR` severities (R4 crosses, R5 is merely close), both `CONNACC`
halves (J1 buried, J3's exit blocked by C2) and both `OVERLAP` cases (R1/R2 same
side, TP1's drill under U1 from the back). If a rule stops firing on it, that rule is
dead - kpcb's thresholds are loose enough that a clean board reports nothing, which
looks identical to a broken check.

**After editing any tool, `kcommon.py`, SKILL.md or a reference file**, run
`python3 references/selftest.py` - green means the documented counts still hold.
They are the only thing that distinguishes a working rule from a silently dead one.
