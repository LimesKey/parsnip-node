# Recipes

**An ad-hoc query no command covers.** Import the parsers; never hand-match
parentheses or regex a KiCad file:

```python
import sys; sys.path.insert(0, '<skill>/scripts')
from kcommon import load_sexp, kids, kid, val, Netlist   # parse_sexp(text) for a string
from kpcb_board import Board
b = Board('parsnip.kicad_pcb')   # b.fps[REF]: x y rot layer value pads[{num net x y w drill}] crtyd;
                                 # b.tracks[{net layer w a b len}] b.vias[{net size drill x y layers}]
                                 # b.zones b.fills[{net layer pts area}] b.copper b.stack b.outline
nl = Netlist('parsnip-merged.net')  # nl.comps[REF] nl.nets[NET] -> [{ref pin}] nl.cpins[REF][PIN] -> NET
t = load_sexp('charger.kicad_sch'); [kid(s, 'at') for s in kids(t, 'symbol')]
```

Via sizes and counts are already `kpcb.py FILE vias`.

**"Review this board's layout"** - one call: `kpcb.py FILE review`. It ends by
naming the next calls to make, so there is nothing to work out first.

**"Did the board get updated from the schematic?"** `kpcb.py FILE sync`. Two lines
when clean. Run it before quoting any placement finding.

**"Review this placement / where should X go?"** `kpcb.py FILE summary`, then
`check`, then `map` to see the free space, then `where REF` on anything the check
named. `sheet` tells you which blocks are still scattered. Say plainly that these
are geometric heuristics.

**"Is there room for X here?"** `kpcb.py FILE where 130,60 -r 10`. One call.
For a silk box or logo of a given size: `kpcb.py FILE freebox f W H`.

**"Tidy the placement" (the pre-order polish pass).** In this order, all read-only:
1. `kpcb.py FILE sync` - a stale board makes every later step about the wrong circuit.
2. `check` - HOLECLR and EDGECLR already catch hole hardware and edge problems
   (the 2026-09-29 pass skipped this and re-found D7 at H4 by hand).
3. `tidy` - near-miss rows/columns, uneven pitch, hole insets, odd rotations,
   centring under a big part, each already run through movecheck.
4. `check --only EDGEREF` - edge-launch parts on their PCB Edge mark, side-entry
   housing setbacks.
5. `silk` - logos hidden under a module or holder, art over pads; `freebox` for a
   new spot.
6. `movecheck REF X Y` on each nudge you settle on (`--scan` to centre along a line,
   `via X,Y NX,NY` for a stitching via in the way).
Then hand the list to the user: they Drag (D) each part in KiCad (its tracks follow)
or type the position in Properties, refill zones (B), save, and re-run `check`. The
skill writes only copies (`-o OUT`), never the board itself.

**"Place the rest / route what is left"** - on a scratch copy only, never the real
board (the loop and each writer's model are in [kpcb-writers](kpcb-writers.md)):
0. `kdrc.py BOARD scratch DIR` (`--from` a board kept elsewhere) - COPY = DIR/<stem>.kicad_pcb.
1. `kdrc.py COPY drc --refill-save --report r0.json` - fresh fill + the baseline.
2. `kpcb.py COPY renet --drop` if `summary`-time loads warned of netless copper, then
   `ripup --drc r0.json` to clear shorts, clearance hits and dangling copper.
3. `place REF` (decoupling caps: `--near IC`) one part at a time, writing each before
   the next so it sees the last; `airwires` for what is left.
4. `route --pending` (or `route NET...`), then `kdrc.py COPY drc --refill-save
   --baseline r0.json`: a clean step reads `0 new`. Hand-route RF nets (refused) and
   check power nets with `ampacity`.
5. `airwires --since <the copy before step 4>` (both refilled): a net in more pieces
   than before means a route cut a plane region off; `pairs` for diff-pair skew/gap.

**"Make this power path carry X A" (after routing, on the scratch copy).** Budgets: kpcb.json
`current{}`, else the `ampacity` scan's derived ones. Then per path:
1. `ampacity --from SRC --to LOAD --amps X --fills` - R, Vdrop and the neck, through the real fills.
2. `fatten NET [--layers ..] -o n.kicad_pcb` - bands round its tracks; refill (`kdrc --refill-save
   --baseline`), `airwires --since` the copy before it (a band can cut a plane off).
3. `stitch NET --box ..` at each layer change the solve showed crowding; `-o` writes free vias.
4. `ampacity ... --fills` again; what is left needs a placement change, say so.
A spot no via fits: `stitch NET X,Y -r 2` says so in one call; `copper BOX` + `edit` for the
hand fix (a width, a stray stub, `solid REF.PAD` for a pad on spokes that must carry current).

**"Clean up the routing" / "make the traces look nice" (after it is complete, on the scratch
copy).** What a reviewer calls clean, each measured by a `tracks` row, so "done" is a number:
every track octilinear (`off45`), no corner past 45 deg (`90`, `acute`), one width per run
(`neck`), no stub under 0.05 mm (`stub`), every dead end on its pad's centre (`pad`), no lane
jogs (`jog`), every signal net close to its shortest path with few vias (`detour`), no
redundant parallel chain (`loops`), power at its class width (`widen`). The writers are in
[kpcb-writers](kpcb-writers.md); after each step `kdrc.py COPY drc --refill-save --baseline
r0.json` (0 new) and `kpcb.py COPY diff BEFORE.kicad_pcb` (no net/layer change on kept tracks):
1. `kpcb.py COPY tracks --view census.png` - the baseline numbers, and why each row was left.
2. `reroute -o n.kicad_pcb` - the detours first (it re-lays whole nets; the others polish what
   is there): each kept only when shorter or with fewer vias.
3. `smooth --all -o n.kicad_pcb` - chains tight and octilinear, doubled copper, corners, pad ends.
4. `smooth --jogs --nudge -o n.kicad_pcb` - lane jogs, lifting a stitching via when one blocks.
5. `widen -o n.kicad_pcb` (`--uniform-power`) - under-width tracks to their class width.
6. `loops -o n.kicad_pcb` - redundant chains on signal nets.
7. `tracks` again: the rest are anchors, width steps or boxed in at the real clearance; quote the
   before/after counts. `ends at a pad edge` rows left by steps 5-6: `smooth --pads -o n.kicad_pcb`.
Hand fixes for what is left: `copper BOX` for uuids, `edit seg/del/width`, `movecheck via` for a
via in the way. Never chase a row the census calls hidden or boxed in by re-routing a dense
neighbourhood by hand: `reroute NET` it, or leave it. A pin `route` calls boxed in: `vscan NET
BOX --layers L` shows the cells it saw. A part to re-place without its old routes: `unhook REF
-o n.kicad_pcb`, then `place`/`route`.

**"Route this dense area" / "how do I fit these traces" (high density, on the scratch copy).**
Order matters more than any single route; dense boards fail on the last 10 links:
1. `congest --all` - where the floorplan forces density, before any track. Over 100% on every
   layer is a placement problem: `tidy`/`movecheck` to spread parts, turn an IC so its pins face
   their loads, swap equivalent pins/gates in the schematic. Fix it here; routing cannot.
2. Planes and power first: GND is a plane (In1 under RF), power by pours (`fatten`), so their
   copper is in the model before signals claim the channels.
3. `fanout REF` on each fine-pitch part (QFN, module, connector field): boxed pins get
   via-in-pad or a neighbour moved; dogbones next (a via row one pitch out); escapes last.
4. Hand-route what `route` refuses or must not improvise: RF (`rf` checks it), diff pairs
   (`route --pair`, then `pairs`), switch nodes (short, wide, on their own layer).
5. `congest` (pending links) and `map --layers L`: send the long links to the layer with room
   (`route NET --layers In2.Cu`), keep short ones on the outer layers, keep the RF reference
   plane whole (route charges it 2x).
6. `route --pending -o n.kicad_pcb` (shortest first); a FAILED link names what boxes it in:
   `--rip-board 3` lets it rip small nets already on the board, `--pour 0.3` lays a power link
   no track width fits as a zone band, a neck is automatic out of fine-pitch pads.
7. `airwires` until 0, `airwires --since` the copy before (a route that cut a plane region off),
   then the clean-up recipe above.
What does not work on a dense board: Freerouting on a poured board (it stalls on the planes),
widening before everything is connected (it eats the channels), and many hand detours (each
blocks the next link: `reroute` them).

**"Clean up the silkscreen" (on the scratch copy; the real board only when asked).**
1. `kpcb.py COPY silk` - every designator/text over pads, under bodies, overlapping,
   off the edge, undersized, ambiguous, mirrored or upside down; logos; the size histogram.
2. `refdes -o n.kicad_pcb` (`--size 1 --thick 0.15` to normalise, `--show` to bring hidden
   designators back) - the flagged designators to clear spots beside their parts.
3. `silk` on the copy: what is left prints "no clear spot". For each, `silk REF` (the
   texts there), `view REF --layers silk` (flagged ones red), then `edit ref REF X,Y 0`,
   `hide REF` or `textsize REF H T`; a board or footprint user text (uuid in `silk REF`):
   `edit text UUID X,Y ANG`, `del UUID`, `retext UUID TEXT`, `knockout UUID|REF on|off`; a new
   label at an exact spot: `edit addtext TEXT X,Y ANG f|b H`; a logo: `move REF X Y`
   (`freebox f W H` for room).
4. `kpcb.py COPY diff BEFORE.kicad_pcb` - silk only, copper identical; `kdrc.py COPY drc
   --refill-save --baseline r0.json` - 0 new (TEXT_HEIGHT/THICKNESS go down).

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

**"What changed?"** `diff old.net` for wiring, `check --since old.net` for findings;
on the board `kpcb.py FILE diff OLD.kicad_pcb` (placement, silk, vias, zones, tracks, R).

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
python3 references/selftest.py     # every check, asserts, exit 0 = all pass (~20 s)
```

Before a refactor, record the real board's outputs, then check after - a pure
refactor must be byte-identical (fixtures only prove each rule still fires):

```bash
python3 references/selftest.py --golden /tmp/golden BOARD.kicad_pcb BOARD.net   # 1st run records
python3 references/selftest.py --golden /tmp/golden BOARD.kicad_pcb BOARD.net   # later: diffs, exit 1 on change
uvx pyflakes scripts/*.py        # must print nothing (exit 0)
```

In the parsnip repo `code/claude/verify.sh` runs all of this, part-search's
selftest and the goldens (HEAD vs the working tree on a frozen copy) in one go.

`selftest.py` is the source of truth for the exact commands and expected counts -
one `(label, argv, exit, substrings)` line per check, so covering a new rule is a
one-line add. Fixtures: `selftest_nightly.kicad_pcb` is `selftest.kicad_pcb` in
10.99 `(transform ...)` form (must give identical `check`); `mini_project()` writes a
throwaway project for what a static fixture can't carry (an NC marker on a wire
stub, a .kicad_sch newer than the .net, a missing top-level sheet, a BAV199 on the
wrong dual-diode symbol, a one-cell stack with an unfused TVS for `revpol`, a tiny
`t.pretty` footprint library for FPPAD and fused-pad PARPIN, `rf.net` for RFSTUB's
shunt/choke exclusions, `fet.net` for revpol's gate-driven FET), plus formula checks
(microstrip Zo, the CPWG field solver vs exact cases, glTF transform). Small boards
generated in the temp dir carry one case each: duplicate refs and a B.Cu logo with
F.SilkS art (`dup_board`), footprint scale + origins (`scale_board`), the hash-seed
tee (`tee_board`), PAD-NECK (`neck_board`), STUB-CHECK (`stub_board`), EDGEREF (`edge_board`), `movecheck`,
`silk`, `freebox` and `tidy` boards, and netlists for knet draw's note line and
multi-pin symbols. Newer ones: `power_net` (powertree + rails, with a knet.json),
`i2c_net` (a resistor strap and a duplicate address), `lint2_project` (SHUNTBUS,
TEXTOVER, and a `pages/` project for PAGEORDER/NAVORDER), `kproj_project` (all
four kproj commands, then a lock refusal), `front_project` (kfront check/pages),
`writer_board` (in KiCad's own tab layout, which the writers need: the fp_line frame
bug, fixlines, airwires, route, renet, place, ripup, then the flip / move round trips
and the router's plane-vs-island pour rule). Static fixtures beside this file: `selftest_cross.kicad_pcb`
(tracks crossing mid-span) and `selftest_repeat/` (one sheet file used twice). `kdrc`, `kmerge` and `height` need
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
