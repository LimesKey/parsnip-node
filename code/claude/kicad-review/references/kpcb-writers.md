# kpcb.py writers - place, route and edit on a scratch copy

The read-only commands are in [kpcb](kpcb.md); this is the writers' model, the
scratch-copy loop and every flag. The whole place-and-route pass is a recipe in
[recipes](recipes.md) "Place the rest".

Every writer writes a NEW file (`-o OUT`) and refuses to overwrite its input. They
edit the board as text, by span: a top-level item opens on a `\n\t(` line and closes
on the next `\n\t)` line, items are found by uuid, every regex match count is
asserted, and nothing that was not edited is re-serialised. Verified exact: a flip
applied twice, and a move+rotate and back, reproduce all 471 parsnip footprints
byte for byte; a flipped board reads every pad mirrored in x about its footprint
(1475/1475, 0 error) and KiCad's `export pos` agrees on side and position (465/465).

**After any writer**, `kpcb.py OUT diff BEFORE.kicad_pcb` is the fingerprint: footprints,
silk, vias, zones and teardrops identical or not; tracks removed/added/re-laid/width-only;
net or layer changes on kept tracks (should be none); length and per-net series R deltas.
**Teardrops**: the writers delete the teardrops of tracks they change; kicad-cli's
`--refill-save` re-creates one only where its track is <= 0.9x the pad/via
(`td_width_to_size_filter_ratio`): widening dropped 1092 -> 1059, and a smooth pass's
deleted ones came back (3545 -> 3546). Count them in `diff`.

**The loop** (scratch dir holds `parsnip.kicad_pcb` + the project's `.kicad_pro`,
`.kicad_dru`, sheets and `.net` - the board must keep the project's stem, see gotchas):

```bash
P=kpcb.py; B=$S/parsnip.kicad_pcb
kdrc.py $B drc --refill-save --report r0.json          # baseline, fresh fill saved
$P $B airwires                                        # what is left
$P $B ripup --drc r0.json -o n.kicad_pcb && mv n.kicad_pcb $B   # shorts/clearance/dangling
$P $B place U22 -o n.kicad_pcb && mv n.kicad_pcb $B   # one part at a time
$P $B route --pending -o n.kicad_pcb && mv n.kicad_pcb $B
kdrc.py $B drc --refill-save --baseline r0.json --report r1.json   # only NEW findings
$P $B airwires --since OLD.kicad_pcb                  # a plane region the routes cut off?
$P $B pairs                                           # diff pairs: skew, coupling
```

`kdrc --baseline` lists only what the edit added (multiset on type + descriptions),
so a clean step reads `0 new`; `--refill-save` is refused for a git-tracked file.

**`route`** builds one window around the gap (`--margin` 3 mm, widened x2.5 then x6
on failure) at `--grid` 0.05 mm, aligned to a grid point on the source pad. Per
layer, foreign copper is painted as exact row intervals (pads as rectangles - a
roundrect's corners are never assumed away -, tracks as capsules, vias as disks)
grown by the clearance `_clearance_fn` gives for that item pair on that layer
(netclasses, `.kicad_dru` `NetName` pairs, and its type rules: "Pad to Track" 0.1,
track-track 0.127 outer / 0.1 inner on parsnip, never relaxing a raised class or pair
rule; before 2026-10-19 it held 0.15 everywhere and called U22.13's 0.4 mm escape
between C193's pads "boxed in") + half the track width + s^2/(8R) (the most a
straight grid step can dip into a convex obstacle, ~0.003 mm; a blanket half-cell
margin made a 0.4 mm-pitch QFN pad unreachable). The board edge is copper-to-edge
clearance, rule areas with tracks/vias not allowed block, and a through via needs
its pad clear on every layer plus 0.25 mm hole to hole; no via lands in its own SMD
pad. A foreign PLANE (GND, or any net filling > 20% of the board on that layer) is
passable at `--pour-cost` (0.6: a route cuts the plane, the refill flows round it);
every other foreign pour is a power island and counts as copper for tracks and vias -
a first parsnip pass that let routes through them split PPHV from 3 pieces into 5.
Inner layers cost `--inner-cost` (1.25), a via `--via-cost` (1.5 mm), a bend `--turn`
(0.3 cells); the inner layer under an outer layer's RF-netclass traces (their
reference plane: In1 on parsnip) costs `--ref-cost` x that (2, so 2.5 vs In2's 1.25).
**Rip-up:** links go shortest net first, so an early route can box a later pin in
(parsnip 2026-10-18: U22 LGA pins 4/13, REGN R54.1, D3.3, U1.4). A link that fails
is retried with this run's own routes of other nets passable at `--rip-cost` (10 per
mm: a 10 mm detour beats a rip). The links it comes within clearance of are deleted
and their nets routed again at once (no further rips); if one cannot rejoin, the
whole attempt is undone and the link reports FAILED with why. `--rips` (10) caps
rips per run, 0 turns it off. Board copper that was there before the run is never
ripped, so on parsnip's working-tree board (2026-10-18) it did little: of 56
failures one cleared (U2.B1, covered by the run's own GND stub on U2.A1, which was
ripped and re-laid; the net joined later without it anyway), one was undone (R53's
two ends block each other, so ripping only swaps which net fails), and the rest are
stale copper over the source pad (29) or boxed in by copper that predates the run (26).
**`--pour W`** (off by default): a link no track width fits (PWR_HIGH in the dense
charger corner fails at its 1.0 mm floor) is found at probe width W (0.3) and laid as
same-net zones, one per same-layer run: a band the netclass track width wide round
the probe path, solid pad connection, min thickness 0.25, priority above every zone
on the board. The refill fits copper wherever the clearances leave room, so it necks
through a gap a track cannot use; the probe path stays in the model (not the file) so
later links route round it. Verified on a fixture with KiCad's own refill (one
island, no unconnected item). Refill, then `airwires NET` and `ampacity`: the neck
is as narrow as the gap allowed. NPTH holes keep the 0.2 mm hole-to-track rule, copper drawn as graphics
(a `gr_rect` on In1 with a net) is copper, and a rule area blocks any copper that
touches it, not just centres inside it. Each path ends on a KiCad anchor - a pad or
via centre, or a point on a track's centreline - with a stub no wider than the copper
it lies in: copper that merely overlaps (a pad under a same-net track) is not joined
in KiCad, and a path ending there would add nothing. Ends prefer cells near their
anchor (2x distance), so tracks leave pads at the centre. Widths: the netclass width, then 0.2, then
0.15; PWR_HIGH 1.5 then its 1.0 floor; PWR_RAIL down to 0.3; `--width` forces one.
RF netclasses are refused (hand-route impedance-controlled nets; `--force`).
Each link is added to the model before the next, so later links see it. The path
snaps onto the pad centre at each end when the track is no wider than the pad.
Verdict: KiCad's DRC on the copy - the grid is conservative, not a proof.

**`place`** rasterises each side at 0.1 mm (courtyards, through pads on both sides,
the outline shrunk by `--edge`) plus, per net of the part, the foreign copper on
that side's outer layer grown by 0.15 mm; summed-area tables make every candidate
(0.25 mm pitch within `-r`, 4 rotations, `--side f|b|both`) a few O(1) tests: the
courtyard as boxes, one per vertex-y slab (an L is two), not its bounding box.
It never moves a placed part to make room. Score:
per pad the distance to the nearest same-net copper on the board (rails x0.3, +1.5
for an SMD pad on the other side), `--near REF`'s pin on that net (+2 mm off its
side), + 0.2 x the distance to the anchor. A part whose every net is a rail (a
decoupling cap) needs `--near`. The best spots are re-checked with movecheck's World.

**`ripup --drc`** takes the item uuids kicad-cli puts in its JSON report (`kdrc.py
--report`), for `--types` (default shorts, clearance, hole clearance, hole-to-hole,
copper-to-edge, dangling track/via), and deletes the tracks/vias among them. The
prune (`kpcb_edit.prune`, shared by `unhook` and `loops`) then repeats until stable,
spreading only from removed copper - an item is re-tested only when it touches
something just removed, so an old dangling stub elsewhere on GND is left alone (a
whole-net prune once deleted two, one C138.2 needed): a track end with no pad, via or
fill of its net under it and no real junction (a shared endpoint that is not also this
track's other end, or a T into another track's interior) goes, and so does a via that
joins fewer than two LAYERS (KiCad's via_dangling). Teardrop zones on removed copper go too.

**`unhook REF... -o OUT`** rips only the copper that served those parts: the track ends
on their pads (on a layer the pad has) that sit on nothing else of the net - no via, no
other part's pad - then the prune with the parts treated as absent. A stub that only fed
them goes; a via in their pads goes only if it then joins fewer than two layers; a trunk
that also serves others stays. Parsnip: `unhook C65` takes C65's three GND stubs and keeps
the 4-layer VSYS via-in-pad and its trunk (`ripup --box` would have cascaded into both nets).

**Neck-down, diagnostics, `--rip-board`, `--pair`.** When no class width can leave a pad
(a 0.5 mm-pitch USB-C VBUS pin, a WSON TVS pin: "foreign copper covers every cell of the
source pad"), `route` walks narrow free cells (the widest of 0.4/0.3/0.25/0.2 that fits,
`--neck W` to force) for at most `--neck-len` (1.5 mm, 0 = off) to where the class width
fits, does the same at the far end, and joins them at full width; a neck that reaches
the far copper itself is the whole link. Each is printed `NECK: L mm at W mm` (under the
PWR_HIGH floor DRC TRACK_WIDTH flags it - expected). A link that still FAILS names what
bounds it: the clearance-grown copper round the explored region (or, when the pad is
covered, over its cells), copper first, the board edge last (`boxed in by W track on F.Cu
(291), B track on F.Cu (6), the board edge (370)`). `--rip-board N` makes small nets
already on the board rip-up units too: 2..N pads, unlocked, not GND/power/RF/USB, no pour
of their own, a whole net's tracks and vias at once (with their teardrops); ripped ones are
re-routed at once and put back if they cannot be. `--pair`: a diff-pair leg whose mate is
already routed pays per mm for straying from the netclass pitch (diff_pair_width +
diff_pair_gap) off the mate's tracks, so it runs alongside; the coupled length prints after.

**`fatten NET... [--layers L,..] [--width W] [--minw 0.5] [--box ..] [--keep BOX;..]`** wraps
each net's tracks (>= --minw, so sense legs stay thin) per layer in a same-net zone band W
wide (2x the netclass width, >= 2 mm), rastered at 0.05 mm (the traced staircase fitted with
straight edges, `simplify`, so a diagonal band is one diagonal edge), minus other nets' zone outlines
(+0.3), pour-forbidding rule areas, the inner layer under outer RF traces (1.5 mm each
side), a 1.0 mm halo round the GND pads of every part on the net (the GND pour keeps its
thermal spokes: without it parsnip's -BATT band cut R41.4's ground off), bands of other nets
from the same run and --keep boxes. Holes are slit open (one ring per zone outline); each
zone gets its own priority above every zone on the board. Then refill, `airwires --since`
(a band can cut a plane region off) and `ampacity --fills`. Parsnip 2026-10-19: PPHV
In2/B.Cu 3 mm bands, 0 new DRC, PPHV U5->Q7 52.5 -> 32.9 mohm through the real fills.

**`stitch NET [X,Y] [-r 1.5] [--box ..] [--n N] [--via S/D] [--in-pad]`**: through-via spots
the router's Grid calls legal (all-layer clearances incl. `.kicad_dru` pairs, hole to hole,
edge, rule areas), on NET copper (track body, pad, saved fill) on >= 2 layers and in no
other net's power pour - the N nearest X,Y within -r, or N beside each NET via in --box
(0.6-1.6 mm out). `-o` writes them `(free yes)`: KiCad re-nets a trackless via from what it
touches otherwise. `--in-pad` lets one sit wholly inside a same-net SMD pad >= 3 mm2 (FET,
fuse, inductor; never a small pad - paste wicks).

**`copper X0,Y0,X1,Y1 | X,Y -r R [--layers ..] [--net N]`** lists every track, arc, via (free
flag), pad and zone outline touching the box with its uuid prefix; **`edit OP... -o OUT`**
applies `width UUID W`, `del UUID` (a deleted track's teardrops go with it, or KiCad reports
"Missing connection ... Teardrop"), `seg NET LAYER W X1,Y1 X2,Y2`, `via NET X,Y S/D` (free),
`solid REF.PAD` (zone_connect 2), `simplify UUID EPS` (a zone outline's corners within EPS mm
of the edge replacing them dropped, Douglas-Peucker, refused if it would cross itself; the stale
fill goes, refill after: 0.0625 turns a 0.05 mm raster staircase into its straight/diagonal edge;
parsnip's two pre-fix `fatten -BATT` In1 zones 1006 -> 39 and 146 -> 20 corners, 0 new DRC), and
`del` takes a zone too (its fills go with it); `mvvia UUID R` re-places a trackless
(stitching) via at the nearest legal spot within R mm by `stitch`'s search (same size, drill
and free flag; refused with a track on it; "already on a legal spot" when nothing makes it
move, so lay the clashing copper first) and prints the move and the net's via count within
3 mm before/after; `vcenter all|DRC.json` snaps track ends inside a same-net via onto its
centre and deletes segments left zero-length (in-via stubs keep the via's teardrops), a snap
that would newly break clearance to any item skipped and listed (parsnip 2026-10-19: 307
snapped, 479 stubs, 86 skipped, 0 new DRC errors; the project IGNOREs
track_not_centered_on_via, so pass a `kdrc --report` JSON to limit it to what KiCad
flags); for the silkscreen `ref REF X,Y ANG|keep` (move a designator: board X,Y, absolute
angle, justification centred), `hide REF` / `show REF`, `textsize REF|UUID H T` (a
designator, or a board text by uuid), `value REF X,Y ANG|keep`, `hide-value REF` /
`show-value REF`, `valuesize REF H T` (the Value field, which parsnip uses as a label:
BOOT, UART0, PGOOD) and `text UUID X,Y ANG|keep` (a board text; uuids from `silk REF`, a
TrueType text's stale `render_cache` is dropped on any move or resize), in order, a prefix matching anything but
exactly one item refusing the lot. **`move`** is KiCad's Drag (D), `kpcb_move.drag_plan`:
segment ends on the part's pads follow ONLY on a layer that pad has (a through pad: all);
a via inside one of its pads moves with it, and every end on that via (any layer) with
it; an end on any other via stays tied to it. Arcs and a flip's ends stay (counted).
Before 2026-10-19 it dragged ends on every layer inside a pad's box and left the via:
`move C65` pulled the B.Cu/In1/In2/F.Cu ends 0.1 mm off a VSYS via-in-pad.

**`smooth [NET...] [--layers ..] [--box ..] [--overlaps|--corners|--square|--pads|--jogs|--all] -o OUT`**
(`kpcb_smooth.py`; it, `widen` and `loops` need a `.kicad_pro` in the board's directory,
any stem: the rules are read by directory glob). The chain pass:
a chain is a run of same-net, same-layer, same-width segments between anchors - a pad or
via its END CAP touches (KiCad joins on the cap, not the centreline end: a cut through a
cap-only contact disconnected Q14 on 2026-10-19), a 3+ junction, a width change, a T, an
arc or a locked track; a segment whose body crosses a pad/via it does not end on is
frozen. A DP over the chain's own vertices joins any vertex to any later one straight or
by a 45 dogleg when clear; cost = length + bends (45: 0.5, 90: 3, acute: 15) + 2 and 10%
per off-45 segment, the old chain always a candidate, a result > 9% longer refused; then
leftover 90s chamfer as deep as clears. `--overlaps`: doubled same-net copper deleted,
hairpin tips cut back to the first attachment. `--corners`: every visible corner >= 46 deg
at a node of two tracks (width steps included, connector at the wider width) cut at
P1/P2 on its legs (0.05 mm steps, <= 1.5 mm), straight or a 45 dogleg, every new turn
<= 45. `--square`: an off-45 segment ending in a same-net pad slides that end inside the
pad onto the nearest octilinear line (<= 0.5 mm, >= w/4 inside). `--pads`: a dead-end
trace (no other same-net track or via at its end) whose cap only grazes a same-net pad,
or stops >= 0.05 mm short of its centre, is run onto the centre at its own width (a
narrower stub broke 21 PWR track_width rules): the end slid along its own line when that
line runs through the centre (out to it, or back from an overshoot past the pad), else
the last segment re-laid from its far end where the turn there stays <= 45, else a stub
from the end, octilinear first, never an acute hook, no leg under 0.05 mm. Parsnip
2026-10-19: 175 such ends (most from before `route`; the rest were `route`'s centre
stubs that `loops` deleted, see below), 165 fixed, 0 new DRC, corner counts unchanged.
`--jogs` (`kpcb_jogs.py`, not in `--all`): a lane jog (run, 0.05-1 mm sideways leg, run
parallel) collapses by extending the leg before it onto the second run's line (A) or the
mirror (B), <= 9% longer, when `Clear` passes; when exactly one free GND stitching via
blocks it, the via is lifted, the chain re-laid, and the via put back at its own size at
the nearest legal spot within 2.5 mm (`stitch`'s search), or the jog is left and says so.
`--nudge` also drags exactly one blocking routed signal via (never RF/USB/pair) and the
track ends in it <= 0.3 mm straight away, when `movecheck via` finds the drag clean. One
jog per chain per pass. `tracks --kind jog` lists every jog with what blocks each side.
Parsnip 2026-10-19 (after the hand pass): 116 jogs; 1 collapsed by a nudge, P16's GND via
has no spot, 0 new DRC; the rest touch their own net somewhere new or are boxed in.
`--all` chains, overlaps, corners, square, pads, repeated until a round changes nothing
(two fixed temp names beside the board). Clearance (`Clear`): route's Grid at 0.02 mm grown 0.8 cell (a free point is
free wherever it rounds from) with the type rules; where the grid refuses, `Exact`
decides - segment-to-item distances (pads exact in their frame, custom/roundrect as
their box, copper graphics too), hole clearance, edge, keepouts, non-plane fills, a
2 um margin that only shrinks acceptance (the opposite slack once accepted 0.0993 <
0.1 at J14). New copper may touch its own net's other copper only within 0.5 mm of
where the old chain did (a sense tap keeps its one contact; GND exempt). RF/USB/diff
nets are skipped. Parsnip 2026-10-19 (after the prototype pass): `--all` 18 edits in 3
rounds (~200 s), 0 new DRC.

**`widen [NET...] [--width W] [--uniform-power] -o OUT`** (`kpcb_tracks.py`): each under-width track to the
widest legal step up to its target - a PWR_* class's track width, the class width for
GND, W (0.2) for signals - checked like `smooth`. Power classes >= 1 mm go per segment
(R first) and skip legs under 0.5 mm (sense); the rest take one width per run (joined at
two-track nodes off pads/vias), so a 0.15 run never alternates 0.15/0.2 (that took width
steps 44 -> 176). Capped at an SMD pad's narrow side; never a new same-net contact;
skipped: tracks wholly inside a same-net fill, switch nodes (`Board.sw_nets`: the first
try took SW1 0.5 -> 1.5), inner tracks within 2 mm of RF traces, RF/USB/diff nets.
Parsnip: 88 tracks, 0 new DRC (an earlier Exact judged a 9 mm battery-holder pad by its
centre and passed 0.016 mm). Its under-floor tracks read as `changed` in `kdrc --baseline`.
`--uniform-power` then raises every segment of a per-segment power run to the run's widest
where that clears (never narrows; sense legs < 0.5 mm and tracks in their own fill left)
and prints the width steps left mid-run (boxed in): parsnip 2026-10-19, +1 track
(Net-(BT1--) B.Cu 3 -> 4 mm), 55 steps left.

**`loops [NET...] [-o OUT]`** (`kpcb_tracks.py`): per signal net (not GND, power classes,
rails or poured nets: parallel copper there is lower R), greedily the longest chain whose
removal, with the prune, keeps the net's pad partition - both as KiCad joins copper and
on STRICT joins (`Copper(strict=True)`: an end cap wholly inside the pad or via, a real
T). Entry stubs lying inside one pad or via are not loops. Without the strict check a
graze kept partitions "unchanged" while removing F2's only link to J3 (0.01 mm overlap);
comparing strict alone let a pad already hanging by a graze lose its whole 128 mm path.
A third test: no pad may lose its last centreline entry (a track end or via inside the
pad). Without it, pass 3 (2026-10-19) deleted `route`'s centre stubs wherever the pad was
already strictly apart (LED_DO U20.3, DVDT U11.7): the partitions read the same and the
trace was left grazing the pad edge.
`-o` deletes them (prune, teardrops); then `kdrc --unconnected --baseline` (parsnip: 23
chains, 0 new, 0 unconnected).

**`refdes [REF...] [--all] [--show] [--size H] [--thick T] -o OUT`** (`kpcb_silk.py`):
designators to the best clear spot beside what a reader sees of the part (pads + Fab
body): 4 sides, slid along each (centre first), 3 rings out (clearance +0.02, +0.35,
+0.8), upright 0 or 90; clear of exposed pads (+ silk clearance), part bodies, other silk
text (each placed one becomes an obstacle for the next), silk outlines, logos and the
edge, and never ambiguous the way `silk` judges it. Score: side preference + distance
from the part + 0.15 x the move, a bonus for the side the previous part of the same
footprint and rotation in the row took (rows read alike). Default targets: visible
designators with a PAD/UNDER/OVERLAP/EDGE/AMBIG/ORIENT finding; REF... named ones;
`--all` every visible one; `--show` hidden ones too (unhidden). Size kept, raised to the
fab minimum (`--size`/`--thick` normalise). The field's `(at)` is written footprint-local
with the absolute angle, justification centred (mirror kept). Parsnip: 82 of 118 placed
on the first pass; the rest print "no clear spot": shrink, hide, or move by hand
(`edit ref`). Check with `silk` and `view --layers silk`, then `kdrc` (silk only: 0 new).

**`pinlabels REF [NAME...] [--size H] [-r MAX] -o OUT`** and **`label TEXT X,Y | --near REF
[--side b] -o OUT`** (`kpcb_label.py`): new board silk texts at spots `silk` passes.
`pinlabels`: one label per pin in an aligned column, at the first clear offset from the pin
row on either side up to MAX (3 mm); NAMEs go to pins 1..N, else every netted pad gets its
net name, followed through a fuse or 0R while it is `Net-(..)`; with no column it names
the blockers. `label`: the clear spot nearest X,Y (or REF's designator, which it hides),
0 then 90 deg, within -r (3 mm). Both dry-run without -o.

**`place` escapes and `--shove`.** A copper-clear spot is kept only if every SMD pad there
reaches its net's track/via/pad or a legal via spot within 1.5 mm of free board (0.15 mm,
0.5/0.2 vias); a fill does not count (it can be an island: parsnip's D28 GND pad). `--shove`
(only when nothing is clear): spots blocked by ONE small part (<= 3 pads, courtyard <= 8 mm2,
not U/J/BT/H/SW/Y/X/NT, not --near) that open when it moves <= 1.5 mm, each checked like
movecheck both ways; `-o` writes the nudge (tracks dragged) and the part.
