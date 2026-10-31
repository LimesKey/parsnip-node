# kpcb.py - placement review

Placement review from the `.kicad_pcb` alone. No DRC (that is `kdrc.py`) - the
placement checks work the moment a footprint is dropped; `ampacity`, `rf` and
`zones` read the routed copper once it exists, and `height` reads the 3D models.
Reads KiCad 10.0 `(at X Y R)` and 10.99 nightly `(transform (translate) (rotate))`
footprint placement (validated against `pcb export pos` 443/443 and IPC-2581 pad
centres 1329/1329).

Code map (to patch one command, open only its module): `kpcb.py` is the CLI plus
summary/where/map/sheet/unplaced/span/net/sync/review; the Board model, part
classification regexes and geometry are `kpcb_board.py`; `check` rules are
`kpcb_check.py`; `ic`, `zones`, `ampacity`, `viapad`, `rf`, `height`, `view`,
`freebox`, `tidy`, `diff` are `kpcb_<ic|zones|amp|viapad|rf|height|view|freebox|tidy|diff>.py`,
`silk` + `refdes` are `kpcb_silk.py`, `tracks` + `widen` + `loops` are `kpcb_tracks.py`,
and `movecheck` (with KiCad's Drag model, `drag_set`/`drag_plan`) is `kpcb_move.py`.
`kpcb_copper.py` is one net's copper as a graph (pads, tracks split at tees, via barrels,
fill fragments as conductors): `net` counts its pieces and `ampacity --from/--to` solves
current through it; custom pads join on their real primitive polygons, not their box.
The clearance model is `kpcb_zones._clearance_fn`: netclass, `.kicad_dru` NetName pairs
and, given the two item kinds and the layer, the `.kicad_dru` type rules (`A.Type ==
'Pad' && B.Type == 'Track'` and the like, layer-scoped `outer`/`inner`) - which never
relax a raised netclass (RF) or a pair rule; `route`, `movecheck`, `place`, `stitch`,
`smooth` and `widen` all use it, and it warns once when no project rules sit beside the
board. The writers are `kpcb_edit.py` (the text-span editor plus `fixlines`, `renet`,
`ripup`, `unhook`, `move`, `edit`), `kpcb_route.py` (`airwires`, `route`, `pairs`),
`kpcb_place.py` (`place`), `kpcb_power.py` (`fatten`, `stitch`, the `vscan` reader),
`kpcb_smooth.py` (`smooth`), `kpcb_tracks.py` (`widen`, `loops -o`), `kpcb_silk.py`
(`refdes`) and `kpcb_dense.py` (`reroute`, beside the readers `congest` and `fanout`); their
model, the scratch-copy loop and every flag are in [kpcb-writers](kpcb-writers.md).

Every `(footprint` block is one Board entry. KiCad allows a repeated ref (logos are
all `REF**`): the second and later copies read `REF**~dup2`, `REF**~dup3`, ... A
padless footprint's side is where its art is, not its `(layer)` (a B.Cu logo can
carry F.SilkS art), so `summary`, `map` and `where` count it on the art's side.

| command | use |
| --- | --- |
| `summary` | outline size, stackup, zones, how much of each sheet is placed, biggest parts. Run first. |
| `check` | rule-based findings (`--rules` lists the rules), grouped ERROR/WARN/INFO. Exit 2 if any ERROR. |
| `where REF...` | **highest value per call.** Position, rotation, courtyard, Fab-only body (the housing a connector setback is measured from), edge distance, class, nets, and every neighbour within `-r`. Footprint `(scale)` is applied. `--origin grid\|aux` quotes KiCad's dialog coordinates. Use instead of eyeballing coordinates. |
| `where X,Y -r N` | same, around a bare coordinate - "is there room here". |
| `map [--side f\|b] [--cols N]` | ASCII occupancy map, one letter per schematic sheet. Shows the floorplan and the free space in ~50 lines. |
| `map --layers In2.Cu` | the same grid for one copper layer: `#`/`+`/`:` track/pad/via copper density, `~` pour only (passable at a cost), `.` free. Where a long route can still go, per layer, before asking `route` to find it. |
| `sheet [PATH]` | per-sheet placed/left counts, bounding box, spread, and parts that drifted from their block. |
| `unplaced` | what is still parked off the outline, ref-ranged by sheet, biggest first. |
| `ic REF` | **where the passives should go**, not just what is wrong. See below. `ic` alone lists the parts worth asking about. |
| `span` | nets ranked by how far apart their placed pads sit. The one routing-quality number that exists before routing does. |
| `sync [board.net]` | **run this first.** Board vs netlist: same parts, footprints, values, DNP flags and net on every pad. Finds a `.net` beside the board automatically. |
| `review [board.net]` | one call for a fresh session: sync, summary, check, longest nets, how many links are unrouted, then the specific next calls worth making. |
| `ampacity [NET...]` | current a routed net can carry (IPC-2221): narrowest segment per layer, via bound, length -> R/Vdrop. With `--amps X` or a kpcb.json budget it warns TRACE-THIN / VIA-FEW / LONG-DROP. See below. |
| `ampacity --from U8.1 --to C5.2 --amps X` | **the real power path**: a nodal solve between two pads (tracks + via barrels resistive, pours ideal), so current splits by conductance; lists the hottest tracks/vias at their share. Use it on a plane net, or when the budget runs between known pads. |
| `zones` | pour coverage per layer from the last saved fill: area%, island count, edge margins. |
| `zones REF...` | does a net's fill actually cover THIS footprint's courtyard - point-sampled, not just the fill's bounding box - and which foreign tracks SLOT that plane under it. Closes "is GND continuous under U9" without a KiCad render. See below. |
| `zones --voids [--area 2]` | copper-free regions in the plane pour (removed islands) and, for each, a via spot that clears everything and lands in the plane on another layer. See below. |
| `vias` | every via by size / drill / layer span (through or blind), the top nets on each, and the worst drill aspect ratio against the stackup thickness. |
| `viapad [--signal] [--min N]` | every SMD pad a via's drill reaches into (its mask opening, real pad shape), small pads first, depth per pad, GND/PWR/SIG, advice from the board's via protection. See below. |
| `where REF.PAD ...` | a pad's absolute centre, size, layer, net and nearest same-net pads. Two specs (pads, refs or x,y) also print the distance between them. |
| `net NET` | every pad on a net with absolute xy, copper per layer (segments, length, width range), vias, zones, extent, and whether the copper is **one piece** (else the pads in each cut-off piece) plus padless islands, a `note` for a pad whose only join is a track body clipping its corner (centreline misses, no end on it: KiCad counts it, a marginal join), then a `dead` line per branch that serves no pad (non-plane nets; `ripup --deadend`). Accepts the short name (`LORA_ANT`). |
| `rf [NET...]` | 50-ohm trace review. No net = every net whose netclass names RF/50. See below. |
| `height [REF...]` | 3D-model height of each part and the board's Z stack. Shells out to KiCad's GLB export (~15 s, then cached until the board, a `${KIPRJMOD}` model or kicad-cli changes). See below. |
| `movecheck REF X Y [ROT]` | what moving REF there would **newly** break: foreign pads/tracks/vias inside the clearance, copper to the edge, a same-side courtyard overlap, and the tracks the move drags swinging into foreign copper. `REF --scan x=X y=A..B [--step 0.05]` slides it and prints the clear stretches; `via X,Y NX,NY` moves one via and says whether tracks are tied to it; `--no-drag`: KiCad's Move (M), its tracks and in-pad vias stay. Read-only. See below. |
| `tidy` | near-miss alignment, each fix pre-checked with `movecheck`: a row/column with one part 0.005-0.15 mm off, a row of >= 3 with an uneven pitch, mounting-hole insets that nearly agree, rotations off a multiple of 90, a small part a little off a big same-side part's long centre line (a thermistor under a cell holder). Prints `REF now -> new`; writes nothing. See below. |
| `freebox SIDE W H` | where a W x H mm silk box (a logo, a label) fits on side `f`/`b`: clear of that side's courtyards, every through-hole pad and that side's silk (logos, board art, refdes and other footprint text), >= 1 mm inside the edge. Best centre per free region, by margin. See below. |
| `silk` | **silkscreen review**: every visible designator, value, user and board text over exposed pads, under a part body, overlapping other silk or a silk outline, off/near the edge, under the fab minimum size, ambiguous (nearer another part), mirrored wrongly or upside down; hidden designators; logos (art layer, scaled extent, the body hiding it, pads it crosses). Folded like `check`, `--only SILK-...`. See below. |
| `silk REF... \| --box X0,Y0,X1,Y1` | every silk text in the region with position, angle, size, uuid (board texts) and its findings: what `edit ref/hide/show/textsize/text` takes. |
| `refdes [REF...] [--all] [--show] [--size H] -o OUT` | re-place designators beside their parts, clear of all of the above (writer). [kpcb-writers](kpcb-writers.md). |
| `view REF... [-r 5] [--layers L,L] [-o x.png]` | PNG of the board around parts (their courtyards + `-r`), an `X,Y`, or `--box X0,Y0,X1,Y1`, on the part's side layers (Cu, SilkS, Fab, CrtYd + Edge.Cuts) unless `--layers`. `--layers silk`: that side's silkscreen drawn from the model (pads, Fab bodies, silk outlines, texts; flagged texts red with their box). `--tracks [--layers L,..] [--net N].. [--mark X,Y;..]`: tracks/pads/vias from the model, no fills, every `--net` green; `--layers` picks the copper (4 layers overlaid are unreadable in a dense area: one or two). `--by-net [--layers L,..]`: the copper fills included, one hue per net, GND grey, a legend - a foreign band in a plane, a dead pour or an island stands out. `--sites LAYER:X,Y;..`: one panel per site. See below. |
| `tracks [--kind K] [--view P.png]` | trace-quality census: visible corners (46-90 deg, acute), off-45 segments, width steps mid-run, segments < 0.05 mm, each dropped when hidden under the union of same-net copper (a corner also when one leg's whole capsule sits inside the other's end cap: a 0.1 mm stub under a 3 mm track); listed rows say why `smooth` left them (pad/via anchor, width step, T, frozen, boxed in, or `fix: smooth --corners`); lane jogs (`--kind jog`) with which side collapses (fully, or `slides A 60% of the way`) or what blocks it, priced only for listed rows; detours (`--kind detour`): signal nets routed > 1.5x their pad MST and > 3 mm longer, or > 2 vias per link (`reroute` proves or refutes each); `overlap`: two same-width runs entering a small SMD pad at different points, joined only through its copper (`smooth --overlaps`); `twin`: two signal runs side by side (< 1 mm, > 1 mm long) into one pad (`smooth --twins`); `inpour`: GND/plane tracks wholly inside their own saved fill (`ripup --inpour`; refill first); `inpourpwr`: any other poured net's (`ripup NET --inpour`, opt-in per net); `deadend`: copper hanging off one point that serves no pad - net, layers, mm, vias, pour mm2, attach point and a uuid line (`cut UUID`: a track dead only past its last tee; `zone UUID`: a pour in it). A plane net (GND) gives only its track stubs (one wholly in fill is its `inpour` row), never a pour island; an RF-netclass branch is INFO (a matching stub?). `zonepts`: a zone outline hard to edit in KiCad (> 24 corners or 4+ edges < 0.3 mm; Ryan's own are <= 22 and <= 3); `zonedup`: half or more of a zone inside another zone of its net on a shared layer (a plane's patch excepted): a script band inside Ryan's zone is named for deletion with the mm2 it pours outside his (check the fill after); a band holding his zone is "keep both" (its extra area is likely the current path). `--box X0,Y0,X1,Y1`: the census of one region. `--view` draws the listed rows, 16 panels per PNG (OUT-1.png, OUT-2.png...). |
| `diff OLD.kicad_pcb` | what an edit changed: footprints moved/rotated/flipped, silk text, vias, zones, teardrops, tracks removed/added/re-laid/width-only, net or layer changes on kept tracks, length per layer, per-net series R of the changed tracks, and last a do-nothing copper delta on the changed nets (dead branches, pass-through vias, segments < 0.05 mm, in-fill tracks per class: +added with uuids / -removed). |
| `vscan NET X0,Y0,X1,Y1 [--width W] [--via S/D] [--layers L]` | the router's own model of a box as ASCII at 0.1 mm: legal via spots and the free layers per cell - what `route` saw when it reports a pin boxed in. |
| `airwires [NET...]` | **what is left to route**: every net whose copper is not one piece, and per missing link the nearest copper of the two pieces (pad, track, via or pour) and the gap. Matches KiCad's unconnected count (135/135 on parsnip). Reads the saved fill: refill first. `--since OLD` (both refilled): the nets a later pass split into more pieces (a plane region cut off) and the new tracks that crossed OLD's pour, `--report R.json` for `ripup --drc`. |
| `route NET... \| --pending [-o OUT]` | grid A* router: one piece of NET to the rest, 8 directions, through vias, every copper layer, ripping up and re-queuing its own earlier routes when they box a pin in. Dry run prints the path; `-o` writes it into a copy. See "Writers" below. |
| `congest [--all] [--cell 2] [--layers L,..]` | **where routing will jam**: routing demand (RUDY: each link's wire spread over its bounding box) vs capacity (free copper per layer / the Default pitch) per cell, as a map plus the regions over 50% with their nets, per-layer free share and what to do. Default demand = the unrouted links; `--all` = every signal net's pad MST over pads and pours with no tracks (the placement view). See below. |
| `fanout REF [--all]` | **escape plan for a dense part**: per netted pad, a track out of the courtyard on its own layer (side, width, neck), else the nearest dogbone via spot it reaches, else boxed in (via-in-pad or move the neighbour). Each net alone against today's copper. See below. |
| `refslots [NET...] [--layers F.Cu]` | per outer-layer signal net, mm of track over GND fill on the layer below (its reference), over another net's plane, and over a gap, each gap credited to the reference-layer track that cuts it (else a void/pad/via antipad). See `rf` below. |
| `pairs [NET...]` | diff pairs (nets `X+`/`X-` or `XP`/`XN`; a stem or prefix of a leg's name will do): routed length per leg, skew, vias, widths, and the coupled length (same layer, parallel, perpendicular foot on the other leg, edge gap <= 2x the netclass `diff_pair_gap` + width) with that gap's median/min/max; flags a gap under the netclass's. `route` lays each leg alone, so run this after it. |
| `place REF [--near REF] [-o OUT]` | **where a part fits**: both sides, 4 rotations, ranked by ratsnest length to copper already on the board, each top spot copper-checked. `-o` writes the first clear one (flipping it if needed). |
| `move REF X Y [ROT] [--flip] -o OUT` | write a footprint's new place into a copy (pads/text turn with it; `--flip` mirrors it to the other side the way KiCad does). |
| `ripup NET... \| --drc R.json [-o OUT]` | delete tracks/vias (every one of NET, `--box`, or exactly the items a kicad-cli DRC JSON flags), prune what that leaves dangling, drop their teardrops. |
| `renet [--drop] -o OUT` | netless tracks/vias (a net renamed under them) take the one net their ends touch; `--drop` deletes the clusters that touch two nets (stale routes) or none. |
| `fixlines -o OUT` | repair the KiCad 10.99 fp_line frame bug in a copy (see "fp_line frame bug" below). |
| `fatten NET... --amps A -o OUT` / `stitch NET [X,Y] -o OUT` | same-net pour bands round the tracks of a power path too thin for A / legal via spots on its copper. [kpcb-writers](kpcb-writers.md). |
| `copper X0,Y0,X1,Y1` / `edit OP... -o OUT` | every copper item in a box with its uuid / a hand edit (`width`, `del`, `seg`, `via`, `solid`, `simplify`, `unround`; silk: `ref`, `hide`, `show`, `textsize`, `text`, `knockout`, `retext`, `addtext`). [kpcb-writers](kpcb-writers.md). |
| `reroute [NET...] [-o OUT]` | rip each detour net (or the named ones) and route it again on a copy; kept only when complete and length + via cost drops >= 5%. Dry run without -o. [kpcb-writers](kpcb-writers.md). |
| `smooth` / `widen` / `loops` / `unhook` `-o OUT` | trace clean-up writers: pull chains tight and octilinear (+ `--overlaps`, `--corners`, `--square`, `--pads`: a trace stopping at a pad edge onto the pad centre, `--all`), grow under-width tracks, delete redundant chains, rip only what served a part. [kpcb-writers](kpcb-writers.md). |

## Flags

`-r N` (neighbour radius, 5), `--max N` (cap per rule / per neighbour list, 12),
`--cols N` / `--side f|b` (for `map`), `--only`/`--skip RULE,RULE`, `--rules`,
`--json`, `--no-suppress`, and one threshold per rule: `--edge 0.5` `--hole 1.5`
`--conn 10` `--rf 8` `--therm 8` `--bypass 3` `--clear 0` `--fb 4` `--gap 0.25`
`--tol 1` `--span 0` (all mm; `--span 0` means half the board diagonal), plus
`--fanout 8`. For `ic`: `--anchor REF`, `--cin REF,REF`, `--cout REF,REF`,
`--ncin 3`, `--ncout 3`, `--assoc 6`. For `ampacity`: `--amps X` (required
current on the named net), `--net NAME` (repeatable), `--dt 10` (allowed temp rise, C), `--plating 20` (via barrel copper,
um), `--vdrop 0.25` (V-drop flag threshold), `--from REF.PIN --to REF.PIN` (path
solve). For `zones`: `--voids`, `--area 2` (smallest void core, mm2), `--net=NAME`
(the plane net, default the biggest pour). For `viapad`: `--signal`, `--min N`.
For `rf`: `--freq MHz`, `--fence 1.5` (mm from the trace edge that counts as fence).
For `congest`: `--cell 2` (map cell, mm), `--all` (placement view), `--layers`. For `fanout`:
`--all` (routed pads too).
For `where`: `--origin page|grid|aux` reads x,y arguments and prints coordinates
relative to the board's grid origin or aux (drill/place) origin, which is what KiCad's
Properties dialog shows when Display origin is set that way (Y still grows down;
an inverted-Y display is not modelled). `summary` prints both origins.

## Project config

`kpcb.json` next to the board, same shape and precedence as `knet.json`:

```json
{"edge": 0.3, "bypass": 4.0, "suppress": ["OVERLAP:BT1", "UNPLACED"],
 "current": {"VSYS": 2.7, "+5V": 3.0, "-BATT": 2.7},
 "height": {"BT1": 23.1, "BT2": 23.1}}
```

`height{}` is a measured part height (mm above its board face) that replaces the
3D model's figure in `height` - for a model known to be incomplete.

`current` is the per-net amp budget `ampacity` checks against when no `--amps`
is given (and in a bare `ampacity` scan). Fill it once with the real worst-case
current each power net carries; signal nets can be left out.

Use `suppress` for placements already confirmed deliberate (thermistors under a
cell, a module antenna overhanging the edge) so they stop costing tokens on every
review.

## `sync` - is the board even the circuit you drew

`kpcb.py board.kicad_pcb sync` (the `.net` beside the board is found on its own).
**Nothing else here means anything if this fails.** A board that was never
re-synced after a schematic change carries pad net names that look perfectly valid,
so neither the board file nor any check on it can tell you it is stale - only the
netlist can. Rules: `SYNCPART SYNCFP SYNCVAL SYNCDNP SYNCNET SYNCART SYNCBOARD SYNCLINK`.

Clean output is two lines (`IN SYNC`), so it is cheap to run every time. A `SYNCNET`
error means re-run KiCad's *Update PCB from Schematic* before reading any other
finding. `SYNCVAL`/`SYNCDNP` alone are annotation drift: worth fixing, but the
connectivity is still right and placement findings still hold.

A footprint on the board only that has no pads and no sheet path (a logo, pasted
silk art, `REF**`) is `INFO SYNCART`, not a missing part: it never came from the
schematic, so it cannot make the board stale and the output stays `IN SYNC`. One
without the `board_only` attr is also `WARN SYNCART`, named with its library footprint
and position: Update PCB from Schematic with "Delete footprints with no symbols"
deletes it silently (tick "Not in schematic" in its properties). So is a
footprint with the `board_only` attr ("Not in schematic", e.g. B.Cu fiducials): `INFO
SYNCBOARD`, which KiCad's own parity check skips too.

A matching refdes is not a link: Update PCB pairs footprints with symbols by path. A
footprint whose `(path)` is missing or is not its symbol's sheet tstamps + uuid is
`ERROR SYNCLINK` (KiCad would delete and re-add it); a `board_only` footprint that does
have a symbol is `WARN SYNCLINK`.

`unconnected-*` pseudo-nets get a `_1` suffix on the board and not in the netlist;
that is normalised away rather than reported as 439 differences.

## `review` - the whole placement picture in one call

Runs sync, `summary`, `check` and the top of `span`, then prints a **next** block
naming the exact calls worth making on *this* board - `ic` on the unplaced
regulators, `where` on whichever refs appear in the most ERROR findings, `map` if
much is still unplaced, `tracks --kind deadend,stub,inpour` when the one-line do-nothing
copper total (dead branches, pass-through vias, micro segments, in-fill tracks) is not zero. That last block is the point: it is the reasoning a session
would otherwise have to do itself from a summary it has not read yet. If the board
is out of sync it says STOP and explains that everything below is about a different
circuit.

## `ampacity` - can the copper carry the current

Works off the ROUTED tracks and vias, so it means nothing before routing and it
re-reads on every save. Three ways to call it:

- `ampacity VSYS --amps 2.7` - judge one net against a required current.
- `ampacity VSYS` - same, current taken from kpcb.json `current{}` if set there.
- `ampacity` - scan: verdict every budgeted net, then list the heaviest routed
  nets (most copper length) with their capacity so the power nets surface without
  a budget file.

A net name that starts with `-` works as typed: `ampacity -BATT --amps 2.7` and
`--net -BATT` (every tool reads a dash + uppercase token that is not one of its
options as a value; kcommon `parse_args`).

To make a finding fixable it prints, per net: a **`find it:`** line listing the
components on the net (ICs/connectors first) - click any in KiCad to highlight the
whole net - and, for the bottleneck, the **nearest pin** (`near U5.23 (2.8 mm
away)`, Ctrl+F the ref) plus the raw `cursor to X,Y` (KiCad shows the cursor in mm,
bottom-right). Highlight the net, go to that ref, scan for the skinny segment. It
also prints the **required width**; when that is impractically large (7-8 mm) the
bottleneck is a thin inner layer (0.0152 mm / ~0.43 oz here) and the real fix is to
route the net on an outer layer or a plane, not to draw an 8 mm inner trace.

**Pours are conductors, not a verdict.** A net with fill reads like any other: the
pours are ideal nodes in the copper graph, so a track a pour parallels is never a
bridge, and a `pours:` line lists each layer's fill area and fragment count with a
reminder that a pour's own neck is not measured. On a plane net (GND) every pad's
stub into the plane is a pendant bridge, so the bottleneck names the thinnest pad
stub, which only matters if that pad carries the budget: `--from PAD --to PAD`
answers the real source-to-load path. The R/Vdrop line still sums tracks only.

With no kpcb.json `current{}`, the scan derives budgets from the protection parts on the
board: a fuse's rating (`10A Hold`) on both its nets, an eFuse's ILIM max (knet.json `ilim`
row for its family, resistor on its ILM/ILIM pin) on its OUT net - upper bounds, labelled.
`--json` carries the `pour` block.

### How it reads the copper (the important part)

It does **not** just take the smallest `(width)` on the net. It builds the net's
**copper graph** with `kpcb_copper.Copper`, the same one `net` and `--from/--to`
use: a node is one piece of metal (a pad, a pour fragment, track ends joined at a
tee, a crossing, a via on a track's body, a pad any part of a track's body overlaps, as
KiCad joins it), an edge is a track piece between those
joins or a via barrel half. The **bottleneck** is then the narrowest **bridge**
track piece - one whose removal would split the net, so all the current must
cross it. A segment sitting in a parallel loop is *not* a bridge, so a trace that
splits and reconverges is no longer mis-read as one thin strand. Each segment is
rated with IPC-2221 `I = k*dT^0.44*A^0.725` (k=0.048 outer copper, 0.024 inner).
Inner layers here are 0.0152 mm (~0.43 oz), so a 1 mm inner trace rates below a
0.2 mm outer one. Vias are a plated barrel (`width = pi*drill`, `--plating` thick),
summed as a parallel bound. Length feeds resistance and voltage drop. The output
prints the graph state (`N pad(s)`, island count) and, when they differ, both the
mandatory-bridge ampacity and the `narrowest single seg` value. `N pieces, not one
conductor` is a real split (the same pieces `net` lists), not a graph artefact.

A bridge whose only far-side pads are sense taps (a thermistor `TH*`, a test point
`TP*`, or a resistor >= 1k ohm - never a real power path) is never the bottleneck nor
the narrowest single seg; the graph line names them (`sense-tap legs skipped: TH1`).
When taps are the only bridges the net reads `meshed, no series bottleneck`.
A bridge with at most one part that can source or sink DC on one side (caps, test
points and taps don't count) and two or more on the other is a **pad stub**: it
carries only that part's current, so it is neither the bottleneck nor the narrowest
single seg. On a plane net every pad's stub is one, which used to name a 2-pin
connector's 0.2 mm stub as the GND bottleneck. A three-way tee makes every arm a
stub: without `--from/--to` the tool can't tell which part is the source.

Warnings (each fires only with a known current; exit 2 on TRACE-THIN or VIA-FEW):

- **TRACE-THIN** - narrowest-bridge ampacity below the required current. It names
  the bridge segment and the nearest pin. Widen it, or move it to an outer layer -
  when the bottleneck is an inner layer and the needed width exceeds 2 mm
  (impractical), the message also gives the width an outer layer would need instead.
- **VIA-FEW** - the net's vias can't carry the current even in parallel.
- **LONG-DROP** - series-bound voltage drop over `--vdrop` (0.25 V). Advisory.
- **PARALLEL-CHECK** (advisory) - a thinner segment exists but is paralleled, so
  it is not mandatory; fine only if its parallel group's widths sum to the current.
- **MESH-CHECK** (advisory) - the net is a full mesh with no single mandatory
  segment; confirm the parallel copper sums to the current. Not on a poured net
  (its loops close through the pour, which is not measured).
- **STUB-CHECK** (advisory) - pad stubs under the current, narrowest first, as
  `REF.PIN width amps` (narrowest per part). Only a part that sources or sinks the
  budget carries it: `--from` that pad `--to` the load to solve the real path.
- **PAD-NECK** (advisory) - the picked bottleneck is a short (< 2x its width), wide
  stub landing right on a pad. IPC-2221's long-trace formula overstates the risk
  here because the pad copper sinks heat locally; not a real limiter unless the
  copper stays that narrow past the pad.
- **SHORT-NECK** (advisory, not a failure) - IPC-2221 says the bottleneck is thin,
  but it is a short piece with both ends in big copper (a pad, a pour, or a
  junction with >= 3x its width of other track). A rod heated along its length
  with its ends held at ambient rises `dT = P*L/(8*k*A)`; when that is under
  `--dt` the long-trace fit is the wrong model. A TRACE-THIN on a sunk piece also
  prints this bound. (5V_RAW's 1.5 x 0.3 mm stub into U15.5 at 3 A: ~1.1 C.)

What it still can't see: a parallel group that individually passes but **sums
short** is left to you (PARALLEL-CHECK), a pour neck is ideal, and IPC-2221 internal
ampacity is conservative for a planed board (see the footer). `--from/--to`
answers the first and the last, below.

### `--from PAD --to PAD` - current along the real path

`--fills` adds a grid solve through the REAL copper: saved fills with their necks, tracks,
pads, via and plated-hole barrels between layers, on `--cell` squares (0.1 mm with numpy,
else 0.2, pure Python), Jacobi-PCG. It prints R / Vdrop / P and the densest current off
the pads and vias as the width a uniform track would need to carry it all: the neck to
widen. Refill first (it reads the saved fill). Parsnip: PPHV 32.9 mohm vs 16.6 with
pours ideal; SW1's neck the In2 trace at U8's vias (0.86 mm equivalent).

`ampacity --from Q16.1 --to R41.1 --amps 6` solves the copper between two pads of
one net as a resistor network (Jacobi-preconditioned CG, ~0.02 s): tracks and via
barrels are resistors (`rho*L/A`, barrel = two halves of the board thickness),
pads and zone fills are ideal nodes. The copper graph (`kpcb_copper.py`) joins
what KiCad joins: track ends on pads/vias, a track teeing into another's body, two
tracks crossing mid-span, a via or pad sitting on a track mid-span, overlapping
pads (fused leads), fills to
the pads/vias/tracks they touch and to other fills of the net on the same layer.
On parsnip it finds 0 split nets and 0 islands, matching KiCad's 0 unconnected / 0
isolated_copper. Output: R, Vdrop, P between the pads, the pours crossed, and the
hottest elements at THEIR share of the current (rise from IPC-2221, or the
end-sunk bound for a short piece between big copper, whichever is lower; vias
IPC-2221 barrel only). A sense tap (thermistor leg) carries nothing, so it can
never be named. Pours are ideal: a pour neck is invisible here, so check it in
KiCad. Exit 2 when an element exceeds `--dt`; not joined at all also exits 2.

## `zones REF...` - does the plane actually reach under this part

Teardrops are zones in KiCad (1410 of 1434 on parsnip); every pour figure here
and in `summary`/`ampacity` leaves them out, `summary` just counts them.

`zones` alone reports per-layer coverage for the WHOLE board (area%, islands,
edge margins) - useful for "is the pour fragmented", useless for "is GND solid
under this one RF part", since a dominant island and all-edges-reached can both
be true while the fill still has a hole right where it matters. `zones REF...`
answers that directly: an 11x11 grid of sample points inside the footprint's
courtyard, each tested against every net's fill that could plausibly reach
there (a bbox prefilter, then discarded if the net scores zero real hits - a
net's overall pour can be substantial elsewhere and still never actually touch
this courtyard, which the prefilter alone can't tell apart from touching).

Coverage is reported per (layer, net): **continuous** (100%), **has gaps**
(>=50% - normal, since every non-plane pad or via under the part carries its
own clearance moat, so this is not itself a finding), or **MOSTLY MISSING**
(<50%, or no net reaches the courtyard on that layer at all) - the last is the
one worth exit code 2 and a look, listing a few uncovered sample coordinates so
you know where to click. Only trust a MOSTLY MISSING verdict, or a large
contiguous run of misses inside a "has gaps" layer, as a real broken reference;
a handful of scattered misses next to signal pads is expected and not a defect.

**SLOT**: the samples can miss a thin cut that runs between grid columns, so for
each plane (GND/rail net) it also measures foreign-net tracks on that layer inside
the courtyard and prints `!! SLOT: +5V track(s) (1.80 mm) run 52.2 mm through the
GND plane under U12 (...)` for runs across >= half the courtyard's shorter side
(two per layer, then a count). The part's own nets on its own side are its fan-out,
not a slot. The coverage tag then reads `has gaps, and a SLOT cuts it`.

Edge-mount parts (SMA, USB-C) are judged on the ON-BOARD part of the courtyard only:
the grid covers the courtyard clipped to the outline, inset 0.5 mm on clipped sides
for the pour's edge pullback, and the header says what % hangs off. A net that is
neither GND-named nor a rail (a small signal pour) is listed without a verdict,
`local signal pour, not a reference plane`, and never fails the exit code.

## `zones --voids` - removed islands, and where one via rescues each

Rasterises every plane layer at 0.25 mm (layers where the plane net covers > 30%)
from the last saved fill, marks ALL copper (fills, tracks, pads, vias), and keeps
only board cells >= 0.5 mm from any copper and from the edge - a clearance moat
never reads as a void. Connected regions over `--area` mm2 are reported with their
extent; one mostly inside a no-pour keepout (U1's antenna) says `intended`.

For every other void it looks for a **rescue via** (the plane netclass's via,
0.6/0.3 here): a cell in the void where, on every layer, no foreign track, pad or
via is within via radius + clearance (netclass clearances raised by any
`.kicad_dru` rule naming the net pair with `A.NetName`/`B.NetName`, so the 0.4 mm
unfused-pack rule counts), no drill is within 0.25 mm hole-to-hole, no via keepout
covers it, and the plane net's fill is there on another layer. A spot clear of
foreign pours too is preferred; else it names the pours the via would punch (they
refill around it). No spot prints how many cells each constraint blocked.
Validated: on the board before 573c130 it proposes 113.72,155.13 and 108.97,150.38;
the two GND stitching vias placed by hand landed at 113.45,155.16 and 109.05,150.36.

## check rules

`OVERLAP EDGECLR EDGEREF HOLECLR CONNACC RFNOISE THERMAL BYPASS NETSPAN NOCRTYD CRTYDPAD UNPLACED FABFLAGS DEADCU`.
`--rules` for the legend.

- **`OVERLAP`** screens on courtyard bounding boxes, then confirms against the real
  CrtYd outlines (lines/arcs/rects/polys chained into rings): a cross- or L-shaped
  courtyard whose box overlaps but whose outline does not is dropped, as KiCad's
  DRC drops it (L4's 0603 neighbours clear by 0.02-0.06 mm). A part with no
  chainable CrtYd falls back to its box. `where`'s neighbour distances and OVERLAP
  flags use the same outline distance. Parts on opposite sides only collide where a
  drilled barrel actually lands in the other's area; a back-side battery holder
  sitting over front-side 0402s is not a finding.
- **`EDGECLR`** is an ERROR when the courtyard crosses the outline, a WARN when it
  is merely inside `--edge`. An edge-mount part (SMA, U.FL, USB-C, `EdgeMount` in
  the footprint name) crossing the edge downgrades to INFO - that is what it is for.
- **`EDGEREF`** reads the footprint's own idea of the edge. An edge-launch part (SMA,
  USB-C) whose footprint carries a Dwgs.User line plus a "PCB Edge" text: the line
  more than 0.05 mm off the outline is a WARN, with the move that puts it on the edge
  and where the nearest pad then lands vs the copper-to-edge clearance (the larger
  of `.kicad_pro` `min_copper_edge_clearance` and any `.kicad_dru` `edge_clearance`).
  A side-entry connector (`Horizontal` in the footprint name, MP pads, which sit on
  the mating side) within `--conn` of an edge is an INFO giving its Fab housing
  front's setback and how far it can come forward before a pad is inside that
  clearance; a WARN when a pad already is.
- **`HOLECLR`** keepout radius is the hole's own pad/drill radius plus `--hole`, a
  circle, measured to what the hardware meets: the part's Fab body and its pads (the
  courtyard only when it has neither). The courtyard's assembly margin and a square
  keepout's corners flagged parts 2.95-3.75 mm from an M2 hole centre on parsnip. It
  also catches a mounting hole placed outside the board entirely.
- **`CRTYDPAD`** (WARN): pad centres outside every ring of the footprint's own courtyard
  - a CrtYd drawn round the housing alone (easyeda2kicad headers, SOT-23s, a 21700
  holder's tabs), so OVERLAP and KiCad's courtyard DRC cannot see that pad field.
  `allow_missing_courtyard` footprints (net ties) are skipped. Fix the footprint.
- **`CONNACC`** has two halves: a connector further than `--conn` from any edge, and
  a connector whose cable-exit corridor (its courtyard swept straight out to the
  nearest edge) is blocked by another part. The corridor is axis-aligned only - a
  diagonal exit is not modelled, and neither is component height.
- **`RFNOISE`** needs a switching node. Those are found by pin NAME (`SW`, `LX`,
  `PH`, ...) plus, as a fallback for a switcher whose pin is unnamed, the nets of a
  power inductor (>=1uH **and** >=6 mm2 of courtyard, which is what separates a buck
  inductor from a 470nH 0603 antenna match). Named power rails are excluded, or
  every load on +3V3 would look noisy.
- **`THERMAL`** pairs heat sources (power inductors, switching/charger/eFuse/LDO
  parts, RF PA modules >= 27 dBm such as the E22P) against heat-sensitive ones
  (crystals, oscillators, battery cells, the GPS module). A thermistor (TH/NTC/RT)
  near a heat source is reported separately as a **measurement bias**: it reads
  that part instead of what it is wired to watch (e.g. a charger TS thermistor
  meant for the cells). Suppress the intentional ones in kpcb.json
  (`"THERMAL:TH5"`). Cross-side pairs are reported and labelled: heat couples
  through the board.
- **`BYPASS`** measures each supply pin to the nearest **placed** cap on the same
  net. It stays quiet when no cap on that net is placed yet. Matched on pin NAME,
  never on `pintype`: easyeda2kicad types nearly every pin `passive`, so a
  type-based test would silently check nothing - the same trap `PARPIN` documents
  in knet.
- **`NETSPAN`** is the only rule that looks at wiring: a **fully placed** non-rail
  net whose pads sit further apart than `--span` (default half the board diagonal).
  Fully placed only, because a net still waiting on parts will move. Rails are
  excluded by node count **and** by name. `span` is the detailed view of the same
  data, ranked and capped.
- **`UNPLACED`** prints **one folded line per sheet**, never one per part.
- **`DEADCU`** (WARN; INFO on an RF netclass) is routed copper that serves no pad, from
  `kpcb_copper.dead_copper` on every non-plane net: the net's pads, fills and the track ends
  on them contracted to nodes, track pieces and via barrels the edges, a block-cut tree, and
  leaf blocks holding no pad peeled until stable. So a two-layer run through two vias with a
  band round it (a cycle) is dead when it hangs off ONE point (the PPHV branch Ryan found,
  2026-10-27: 25 mm, 7 vias, 84 mm2), while a pour stitched by vias along a through path, a
  run leaving a pour and meeting it again > 0.5 mm away along a thin corridor, a track into a
  pad the pour also joins, vias stitching two layers' pours and a pour on thermal vias within
  1.5 mm of a pad (a heatsink) are not. A segment under 0.05 mm is a join, never a conductor
  (a 14 um dot whose cap grazes the next track does not keep a via alive: VCC_RF). Reads the
  saved fill: refill first. `tracks --kind deadend` lists the uuids, `ripup --deadend` strips it.
- **`FABFLAGS`** reads the footprint `(attr ...)`, `LCSC Part`/`MPN` and 3D model against
  KiCad's semantics, not a fab's workaround: DNP is the `dnp` flag (export the fab BOM/CPL
  with `--exclude-dnp`), and Exclude from BOM is for what is not a component, which the
  library footprint says (KiCad's Fiducial is `smd exclude_from_bom` on purpose: machine
  vision reads it from the CPL). WARN: DNP and excluded from the BOM (the BOM loses it); a
  part with an LCSC/MPN or 3D model excluded from the BOM where its library footprint is
  not (never bought). INFO, one folded line per difference: exclude_from_bom/pos_files
  set (+) or cleared (-) vs the library `.kicad_mod` (a hand-soldered part out of the CPL,
  or Update PCB copying the symbol's flag over the library's). `board_only` footprints
  (logos) and footprints whose library is not found skip the comparison.
- Two things that used to fire on a half-placed board and no longer do: a mounting
  hole sitting in the parked pile is INFO, not an ERROR about being off the board
  (an error still, if it is out there on its own); and an RF module whose antenna
  end overhangs the outline is INFO with a note to check the keepout, because that
  is how a WROOM or an E22 is meant to be placed.

**These are geometric heuristics.** They know nothing about component height, the
enclosure, keep-out zones you have not drawn, or your assembler's rules. Confirm
anything that matters against KiCad's own DRC and the mechanical drawing.

## `ic REF` - where the passives go

The only command here that answers "where should this go" instead of "what is
wrong". One call per regulator, ~35 lines, no reasoning required from the caller.
Run `ic` with no ref first to see which parts it can advise on.

Nothing is templated per part number: pins are classified by NAME (`VIN` `PGND`
`SW` `FB` `BOOT` `VCC/BIAS` `OUT`, with a blank pin sitting on GND counted as a
ground pin), and every position comes out of the real pad coordinates in the board
file. So a part it has never seen works, and a part whose pins are unnamed degrades
to "nothing positionable found" rather than to a confident wrong answer.

| role | where it is put | why |
| --- | --- | --- |
| input caps | straddling the **tightest VIN/PGND pad pair**, on the outside face of the package, smallest value innermost, extra caps stacked outward | that pad pair carries the high-di/dt loop; a cap across it *is* the small loop |
| inductor | hard against the SW pad(s), body pointing away from the package. Two switch nodes (a 4-switch buck-boost like the BQ25798) -> it bridges them instead | SW-pad-to-inductor is the other edge of the same loop, and the SW node is the antenna |
| output caps | a bank across the output node at the inductor's far pad, largest first | load current flows through them; a line of caps end to end would put the last one 25 mm downstream |
| feedback divider | out the far side of the package from SW, FB pad facing the IC | FB is the high-impedance node; it must not run past the SW pad or under the inductor |
| boot / bias caps | against the pins they serve | small loops of their own |
| a linear reg or load switch | no inductor, so the output cap straddles OUT/GND the same way the input cap straddles IN/GND | same rule, no switching node |
| a plain IC (no VIN pin) | supply pins found via the same name table `BYPASS` uses, caps straddling supply/GND | bypass placement is the loop rule at a smaller scale |

Rotations are snapped to 90 degrees. A pin pair on a diagonal would otherwise ask
for a 165.3-degree part, and the few degrees of loop cost nothing.

**`--anchor` is what makes it usable mid-placement.** The big parts land first, so
the inductor is usually down while the regulator is still in the parked pile. Given
that, "L4 is 90 mm from its slot" is true and useless - the inductor is not what
should move. So when the IC is unplaced and one of its passives is not, `ic` hangs
the whole layout off that part and reports **where the IC itself goes**.
`--anchor REF` picks a different one, `--anchor none` turns it off. Translation
only: if the anchor also needs turning, it says so and asks you to rotate the IC in
KiCad and re-run, because rotating it moves every pad.

The `now` column compares against what is already placed: `OK` (within `--tol`),
`4.8 mm / 90 deg off`, or `unplaced`. It also reports parts standing in a suggested
slot, feedback parts within `--fb` of a switching node, and the current input-loop
length against the suggested one.

**The honest limit is which caps belong to which regulator.** On a rail like `VSYS`
(20+ caps here) nothing in a `.kicad_pcb` says which cap the schematic drew next to
which pin. `ic` ranks by schematic sheet and by refdes proximity to the IC's own
private-net parts, prefers one cap of each distinct value so "smallest innermost"
means something, takes `--ncin`/`--ncout` of them, and **says in the output that it
guessed**. Two regulators on one rail can be offered the same cap. Check it against
the schematic and pass `--cin`/`--cout` once - that is the one number worth a
second call.

## `viapad` - which components have a via inside an SMD pad

`kpcb.py board.kicad_pcb viapad` lists every SMD pad a via's DRILL reaches into (the
pad's mask opening), one row per pad, small pads first:

```
via-in-pad: 67 via drill(s) reach into 41 SMD pad mask opening(s) across 38 component(s)  [pads: ...]
via protection (Board Setup): IV-b mask-plugged - the plug sits in the joint (...): move the via
  off small pads, or order those vias filled + capped (VII)
small pads first; under 1 mm on the narrow side is a fine joint (0402, fine-pitch lead, module pad)

  Q5.1      AON7544          0.45x0.70 F  PWR  1 via(s)  drill 0.075 in   OK same net (+BATT)
  -- pads >= 1 mm (EP, 1206, power): a plug or dimple there is low risk
  U8.EP     BQ25798          ...
```

- What a fab's tenting/plugging cares about is the hole inside the pad's opening, not
  the via centre or its annular ring (copper on copper). `drill N in` is how far the
  drill circle reaches into the opening; `wholly in` = the whole hole is inside.
- The advice line reads Board Setup > Via protection (KiCad 9+: tenting I, + covering
  II, plugging III, + tenting IV, filling V/VI, + capping VII; -a one side, -b both).
  Filled + capped (VII) makes every row a quote line, not a defect.
- **Same-net** (`OK same net (...)`) is intentional via-in-pad: exit 0. A small pad
  (narrow side < 1 mm: 0402, a fine-pitch lead, a module pad) is where a plug or an
  open hole spoils the joint; fix those first (`movecheck via X,Y NX,NY`, `edit dragvia`).
- **Different-net** (`!! via net X != pad net Y - possible short`) means a via of a
  foreign net sits inside a pad. That is a short, not via-in-pad - exit 2. Confirm
  with `kdrc.py` (KiCad's own DRC is the authority on shorts).
- Only numbered SMD pads count; numberless paste/mechanical slivers are skipped.
- Only free board vias count. KiCad 10 cannot store a via inside a footprint (the
  grammar has no `via` under `footprint`), so every via is a board object anyway;
  the reader takes only top-level `(via)` and never a footprint child. There is no
  "manually placed vs auto" flag on a via in the file - a via is a via.
- The test is drill-circle vs the pad's real copper grown by its mask margin (pad, else
  footprint `solder_mask_margin`, else the board's `pad_to_mask_clearance`), layer-aware
  (a blind/buried via that never reaches the pad's outer layer is not counted).
  Roundrect/oval corners are drawn round; a `custom`-shape pad is its anchor PLUS its `(primitives ...)` (gr_poly,
  gr_rect, gr_circle as a circumscribed 16-gon, gr_line as its stroke box). The
  anchor alone can be far smaller than the copper: BQ25798's pads are a 0.15 mm
  anchor with a 0.65 mm polygon.
- Roles: GND by name; PWR if a named rail, a net over `--fanout` nodes, a switching
  node, or a netclass matching PWR/POWER in the `.kicad_pro` (catches `Net-(BT1-+)`);
  else SIG. `--signal` shows only SIG pads, `--min N` only pads holding >= N vias.
  A different-net (possible short) row always prints, whatever the filter.

Back-side parts work without a mirror step: a `.kicad_pcb` stores each pad's `at`
**angle** as absolute (footprint rotation already baked in, unlike a `.kicad_mod`),
so the pad orientation is used directly. Validated against pcbnew's own `HitTest`
on the real board: 54/54 via-pad hits (5 on custom pads), zero false positives or
negatives, re-run 2026-09-22 after primitives parsing (validate with the pad's REAL
copper layer: `GetPrincipalLayer()` returns F.Cu for a back-side pad).

## `rf [NET...]` - a 50-ohm trace in one call

Per net: the routed widths by length (dominant width, and every NECK narrower than
95% of it, with location); microstrip Zo of the dominant width from the stackup
(Hammerstad + Wheeler thickness correction, uncoated, +/-1-2 ohm; solder mask pulls
it down ~1-2 ohm); the same-layer GND gap each side and, when it is close, the
field-solved CPWG Zo (below); what fills the reference layer
under the trace (`GND 100%`, else `BROKEN/NON-GND RETURN PATH`, exit 2); the GND
via fence within `--fence` mm (1.5) of the trace edge: count per side and the widest
nearest-neighbour gap against lambda/20 in the medium (frequency from `--freq` MHz,
else inferred GNSS 1575.42 / LoRa 915 from the net or sheet name); and every branch off
the populated pad-to-pad path (`dead_copper` with only fitted pads as terminals): tee
point, length, electrical length at f, what ends it (`JP14.1 DNP`, or nothing). One
ending in a DNP pad or nothing longer than lambda/50 is an `OPEN STUB` (exit 2): JP14
across FL2 left 4.6 mm, 9 deg at 915 MHz on LORA_LPF_OUT (2026-10-27). A DNP pad sitting
on the line is no branch.

When a same-layer GND pour sits within 5h of the trace (median gap per side), the
line is **CPWG** and `rf` field-solves it: `Zo 51.6 ohm CPWG, field-solved with
F.Mask 15 um er 3.8 (52.8 uncoated)`, the mask read from the stackup. The closed
forms (Wadell/Ghione) are not used: they assume the backing plane is far vs the
slot, and on a 4-layer stackup (h 0.2 mm, w+2s ~1 mm) they read ABOVE microstrip,
which is physically backwards.

The solver is `scripts/kzo.py` (stdlib, also a CLI: `kzo.py W S H ER [T] [--mask TM
ERM]`): quasi-static finite-volume Laplace on a graded grid over half the cross-
section, solved directly (skyline Cholesky), capacitance from the field energy,
Zo = eta0 / sqrt(C C0). Unequal gaps solve each half with its own gap (within 0.1%
of a full solve). `kzo.py --selftest` holds it to exact stripline and CPW and to
Hammerstad microstrip, each within 2%; on a real 35 um trace it reads ~1% low.
Against JLC's own calculator (Polar SI9000, 2026-09-23, JLC04161H-7628 uncoated,
50 ohm widths for gaps 5/8/10 mil and microstrip) it reads 0.7-1.6% low, i.e.
JLC would put an `rf` line ~0.5-0.8 ohm higher. At a 16 mil gap JLC returns the
bare microstrip width (ignores the side ground); kzo does not, and reads 2.6% low there.
It models rectangular copper and the board file's stackup - the fab's trapezoid and
its own stackup numbers (check h and er in the `Zo ... microstrip` line against the
fab's) are the arbiter. ~0.1-0.2 s per solve; every solve of one `rf` run goes in one
parallel batch across all cores (cold parsnip run ~1.4 s), memoised
on disk (a re-run is instant until a gap, width or the stackup changes); gaps round to 0.01 mm.

### `refslots` - signals over a slot in their plane

Per outer-layer signal net (not GND, not a poured net), the copper is sampled every 0.05
mm against the saved fill of the layer below (`_ref_below`, from the stackup: In1 under
F.Cu, In2 under B.Cu): over GND (`(^|/)GND$` - not -BATT), over another net's plane, or a
gap. A gap sample is credited to the reference-layer track within w/2 + 0.3 mm of it (the
one that cuts the slot), else `void/pad/via`. Nets with >= 0.5 mm of gap print, worst
first, with the top four causes. The fix is the cutting track (re-route it off the
reference, or onto the other inner layer), or stitching the slot; refill first, it reads
the fill on disk. Parsnip 2026-10-27: I2C_HOST_SCL 87 mm over gaps, 46 mm of it cut by
UART1_TX on In1.

## `movecheck` - try a nudge before making it

Read-only: it prints what a move would do and never writes the board. REF's pads
are re-placed at X,Y (and ROT, when given, turning them about the footprint origin)
as boxes, then measured against every foreign pad, track and via that shares a copper
layer (a through pad is on all of them), at the clearance `_clearance_fn` gives
(netclasses, `.kicad_dru` `NetName` pairs and type rules; same-net copper is never a
hit), plus copper to the edge (`Board.edge_clearance`) and a same-side courtyard overlap.
The same test runs at the current spot and only hits the new one adds print as
`NEW`, so a pre-existing violation is not blamed on the nudge. What KiCad's Drag (D)
takes along moves too and is checked at its new place (`drag_set`): segment ends on
REF's pads on a layer that pad has (a through pad: all), the vias inside its pads and
every end on them; an end on a via outside the pads stays tied to it. A dragged
segment that swings into foreign copper prints `NEW   dragged NET LAYER @..`; `tidy`
runs the same test. Zones are left out: a refill flows around the part. Pads are
boxes, conservative for round pads on a diagonal. Exit 2 when something new hits.
`--no-drag` checks KiCad's Move (M) instead: nothing drags, the part's own tracks and
in-pad vias stay where they are as obstacles like any other copper, and it prints what
is `left behind` (pad ends, vias in its pads). With Drag, R20's spot off its via-in-pad
read 11 hits (the via dragged along); with `--no-drag`, CLEAR.

`--scan x=113.3 y=81..87` slides REF's centre along a line (one axis fixed, one a
range, `--step` mm) and prints contiguous clear and blocked stretches with what
blocks each, so a centring move can pick its spot. `via X,Y NX,NY` takes the via
nearest X,Y (within 0.15 mm), measured as a disk (not its bounding square): `tied:
nothing` means a bare stitching via that is free to move (the pour reconnects it on
refill); otherwise the track ends inside it drag with it and their new paths are checked
too (a stub with both ends inside drags only its nearer end; an end on the rim stays).

## `tidy` - near misses, with the fix already checked

Five passes over the placed parts (mounting holes only in the third):
- **near-miss**: same-side parts of one footprint within 6 mm (or siblings of a
  small family anywhere - <= 4 parts of that prefix and footprint, so SW1/SW2,
  J4/J8, BT1/BT2, never every 0402 cap) whose x or y agree within 0.15 mm; a part
  0.005-0.15 mm off the value most of the group shares (a tie: the lowest ref's)
  is moved onto it.
- **pitch**: a line of >= 3 same-footprint parts (one y, or one x) chained <= 6 mm
  apart whose pitches differ by 0.02 mm to 25% of the mean: the middle parts move
  to an even pitch between the two ends.
- **hole inset**: each mounting hole's x and y inset from the outline bbox; a hole
  0.005-0.5 mm off the common inset moves to it.
- **rotation**: a part whose rotation is not a multiple of 90 goes to the nearest.
- **centring**: a part whose centre sits inside a same-side part with >= 20x its
  courtyard area, 0.005-2 mm off that part's long centre line (Fab body, else
  courtyard), moves onto the line; the offset along the line is printed too.

Every suggestion runs through `movecheck` at its new spot (`clear`, or the first new
hit), so a fix that would break clearance says so before anyone drags the part.

## `freebox` - room for a silk box

A 0.25 mm raster of the side: blocked where outside the outline or within 1 mm of
it, under a placed same-side courtyard, under any through-hole pad (pad box, both
sides) or that side's SMD pad (a holder's contacts sit outside its courtyard), or under
that side's silk: padless art items, board-level `gr_*` silk, and
each footprint's visible silk text. Text boxes are estimated (0.9 x font width per
character, line height from the font, placed by `(justify)`, footprint text `(at)`
local with an absolute angle). Every centre whose W x H box is empty is a candidate;
4-connected candidates form a region, and each region reports the centre with the
largest margin (how far the box could grow on every side and stay clear; ties go to
the region's middle). Blocked cells are any cell an obstacle touches, so margins
err low by up to one cell. Tracks and pours are not obstacles: silk over masked
copper is fine. ~0.5 s.

## `silk` - the silkscreen review

Every visible silk text - designators (the Reference field), values and user texts on
a silk layer, board `gr_text` - has a box (0.9 x size per character, 1.6 x size per line,
placed by its `(justify)`: KiCad's own text box, the plot's `textLength`) and is judged by
its **parts**, the ink: a TrueType text with a `render_cache` by one box per glyph polygon
(KiCad's own outline, exact, unless the cache misses the estimate: stale after a move);
otherwise one box per word at the stroke font's ink, 0.82 x width per character with
0.28/0.31 x width bearing at a left/right anchor (fitted to 68 parsnip texts in KiCad's
plot; residual -0.22..+0.15 x width per word), 0.75 for a TrueType face without a cache, a
lone '-' as a bar at mid-height. So a pad in a word gap ('21700  LI-ION ONLY' over TH1)
is clear. A **knockout** text is one solid box, its ink grown by KiCad's knockout margin,
height / 9 (0.122 at 1.1 mm, 0.167 at 1.5, measured on the plot). Rules, on the text's
own side:
- **SILK-PAD** a part, grown by the silk clearance (`.kicad_dru` `silk_clearance`,
  `.kicad_pro` `min_silk_clearance`), touches a pad open in that side's mask (`F.Mask`/
  `*.Mask`: a tented via-in-pad is not): the fab clips silk at the mask opening. Prints
  `over U5.3` when the ink overlaps the pad, else the gap: `0.123 mm from F5.1 (silk
  clearance 0.15)`. KiCad's own `silk_over_copper` passes a gap > 0 (parsnip 2026-10-24:
  it flags only the knockout 'POWER' over C8), so a gap line is the fab margin, not a clip.
- **SILK-UNDER** the box overlaps a placed part's body on that side, its own or a
  neighbour's: hidden once assembled. The body is the Fab outline, else the courtyard when
  something physical stands there (a 3D model, or not TP/JP/NT/H/MH/FID: the E22P, cell
  holders and easyeda headers have no Fab); a part's own user marks (+/-) are exempt there.
- **SILK-OVERLAP** another visible silk text, a footprint's silk outline (stroke width
  counted; a footprint's own user texts on its own outline are by design), a logo, or
  board art (filled `gr_poly`/`gr_rect`/`gr_circle` by box; unfilled ones and lines by
  stroke, so a label inside a frame is not under it).
- **SILK-EDGE** off the board or within the silk clearance of the edge.
- **SILK-SIZE** height or thickness under the fab minimum (`.kicad_dru` text_height /
  text_thickness, `.kicad_pro` min_text_*). A TrueType text's (thickness) is ignored: KiCad
  measures the glyph stroke, ~0.116 x height for DIN Mittelschrift, so it needs height >=
  min_thickness / 0.116 (1.3 mm at 0.15; bold does not help). A knockout text's margin
  (height / 9) is what KiCad measures there, so it needs height >= 9 x min_thickness
  (1.35 mm: knockout DIN failed at 1.3, passed at 1.5). Matched KiCad's text_thickness hits
  on parsnip 2026-10-19 and 2026-10-24.
- **SILK-AMBIG** a designator whose box is 0.15 mm nearer another same-side part
  (pads + Fab body, no courtyard margin) than its own - unless it sits in line with its
  own part within 0.6 mm - or more than 2.5 mm from its part.
- **SILK-MIRROR** back-side text not mirrored or front-side text mirrored.
- **SILK-ORIENT** (info) a front designator at 180/270 (KiCad reads 0/90 from the
  bottom/right edge) or off a multiple of 90. Back-side reading direction depends on the
  flip convention, so it is not judged.
- **SILK-HIDDEN** (info) designators hidden or off the silk, one summary line.
- **SILK-ART** a logo HIDDEN >= 50% under a body, or over pads (below).
Then the logo table and the designator size histogram (pick the normalisation from it).
`silk REF... [-r]` / `--box` lists the texts in a region instead, with board and user
text uuids and the TTF/knockout flags. KiCad's own `silk_over_copper` / `silk_overlap` may
be IGNORED in the `.kicad_pro` (parsnip's are): plain `kdrc` will not show them, `kdrc --all`
does (it re-enables ignored rules on a temp copy); its TEXT_HEIGHT/THICKNESS overlap
SILK-SIZE. Parsnip 2026-10-19: 242 visible designators of 469; ~400 warnings, about half
gone after one `refdes` pass (0 new DRC, 12 TEXT_* violations gone).

### logos

A footprint with no pads is art. Its side is where its items are (a B.Cu logo can
carry F.SilkS art), its extent is the union of its items' boxes with the footprint
`(scale)` applied (strokes not added), and it is HIDDEN when a placed same-side part's
body (Fab outline, else courtyard) covers >= 50% of that box: a module (U1, U12) or a
cell holder sits on it after assembly. `pads` counts same-side pads (and through
pads) whose copper box an art item's box touches; silk there is clipped at the mask
opening. Board-level silk graphics (`gr_poly` on *.SilkS) are not listed; `freebox`
treats them as obstacles.

## `congest` - where routing will jam

Demand is RUDY (Rectangular Uniform wire DensitY, the placement-tool estimator): each link's
wire length, taken as its bounding box's half perimeter, spread evenly over that box (grown
half a pitch), so a cell's demand is the tracks expected to cross it; a power-class net counts
its pitch over the Default's. Capacity per cell is the free share of each copper layer times
cell / pitch (Default track width + clearance), summed over `--layers` (all): pads, tracks
and vias are copper, a plane (GND, or > 20% of a layer) counts as half a lane (a route may cut
it; the refill flows round), any other pour as copper - `route`'s own rules. Default demand:
the links `airwires` lists, over everything on the board. `--all`: every signal net's pad MST
(octilinear; not GND, not poured nets) over pads and pours with no tracks or vias - the
floorplan's density before any routing, the time to fix it. The map marks each cell's use (#
>= 100%, * >= 75%, + >= 50%, : >= 25%); regions over 50% (8-connected, worst first, `--max`)
print their box, peak, demand vs capacity, each layer's free share and the nets through
them, then a hint: the layer with the most room after `route`'s own layer preference (outer
1, inner 0.8, the RF traces' reference plane 0.4), or, when demand exceeds capacity on every
layer, spread the parts, turn an IC so its pins face their loads, or fan out through vias.
RUDY is optimistic about detours (a wall it must go round is not demand) and spreads a long
diagonal link thin; `route` is the proof. Parsnip 2026-10-24, `--all`: 112-124, 117-129
peaks at 149%, 132-144, 141-151 at 106% (21 regions over 50%).

## `fanout REF` - escaping a dense part

Per netted pad (pads already joined to a track, via or pour are `routed`, listed with `--all`;
a multi-copper pad such as a thermal pad's via grid prints once with a count), on a Grid
(`route`'s clearance model, 0.05 mm) of the courtyard + 2 mm for that net alone: a BFS on
each layer the pad has, from the cells a track centre may start at, to the first cell 0.1 mm
outside the courtyard (an escape: layer, side N/E/S/W, steps to clear) and to the nearest
cell a through via is legal at (a dogbone). The class width first, then `route`'s neck widths
(0.4 ... 0.15) - a neck is printed. An escape is preferred unless a via is much nearer (the
exit > 3 mm away). `boxed` names what is left: no track leaves the pad (via-in-pad, or move
what boxes it), or it leaves but reaches neither within 6 mm. Each net is judged against
today's copper alone, so two pins may claim one channel: route the boxed pins first, then the
dogbones, then the escapes. To plan a part's re-route, `unhook REF -o COPY` first (parsnip
U5 unhooked: 29 escapes E 14 / W 10 / N 5, the power pins necked to 0.15).

## `view` - a picture of the board

`kicad-cli pcb export svg --layers .. --mode-single --exclude-drawing-sheet` (the right
CLI per file), cached under `~/.cache/kicad-review/pcbsvg/` per board save and layer
list. That SVG's viewBox is the page in mm with the board at its own coordinates, so
the crop is a viewBox rewrite (the ksheet `view` recipe), rasterised by `rsvg-convert`
at ~1200 px across the box (20 px/mm, at least 1200 and at most 4000 px wide; `--px` sets
px/mm) on white; `-o x.svg` keeps the cropped SVG. Always seen from the
top: a B.* layer is not mirrored, so coordinates stay the board's. Zones show their
saved fill, which hides routing and pads.

The model renders skip kicad-cli: `--tracks` draws tracks (arcs as a quadratic through
their mid), pads (custom ones as their polygons) and vias per copper layer from the
Board model, each `--net N` (repeatable) green, `--mark` rings points, 60 px/mm, on the
`--layers` given (default every copper layer: pick one or two in a dense area); `--by-net`
draws the same layers with their saved fills, each net its own hue (ranked by copper area in
the box, golden-angle spaced), GND dim grey, and a legend of the 12 largest; `--sites LAYER:X,Y;..`
draws one 1.6 mm panel per site (`-r` half size) in a 4-wide grid, 120 px/mm, the
input `tracks --view` feeds. `--layers silk` draws one side's silkscreen from the model
- exposed pads, Fab outlines, silk outlines, logos, every visible text as text at its
size and angle, each line laid on the ink of its own line (justified, glyph boxes from the
render_cache) - with each flagged text red and boxed; the back side (a back part, or `--side
b` for a bare box) is drawn seen from the bottom so it reads. KiCad's plot of SilkS+Fab cannot do this: the Fab values cover
the designators.

## `height [REF...]` - the Z budget

Runs `kicad-cli pcb export glb --no-board-body --no-dnp` (the right CLI per file),
so OCCT meshes the real STEP/WRL models as placed, then reads each part's node
bounds from the glTF (Y-up, metres; each node origin sits on its own board face).
Prints the tallest parts per side, then the Z stack at the tallest back-side parts:
`back part + board (between the model faces) + tallest front part whose courtyard
overlaps it`.

What it will not do is guess:
- a part with no loadable model is **UNKNOWN**, never 0 mm; one with several models
  where one file is missing is flagged PARTIAL;
- jumpers, net ties and test pads/holes with no model are "assumed flat copper",
  listed, with the note that a header pin in a TH test point adds height;
- a padless footprint with no model (a logo, pasted silk art) is not a part and is
  left out entirely, never UNKNOWN;
- a battery holder's model is flagged: it may omit the cell (the parsnip BT1/BT2
  model reads 15.8 mm; a 21700 is 21.7 mm across). Put the measured figure in
  kpcb.json `"height": {"BT1": 23.1}` - an override wins over the model and prints
  with `*`.
Enclosure, gasket, display and standoffs are not on the board and not counted.

## fp_line frame bug (KiCad 10.99 nightly, the 2026-10-08 parsnip save)

Build ~4975 wrote every footprint `fp_line` through the board-to-footprint transform
a second time: stored = T^-1(local), so KiCad itself draws the silk, courtyard and fab
lines near the page origin while pads, polys, rects, arcs and circles stay right
(the same file version, 20260901). `Board` detects it per footprint (lines read as
local are large, and small once transformed - the reverse of a sound footprint) and
reads them corrected, with one stderr WARNING; `fixlines -o OUT` writes the repaired
copy (exact: 4842/4842 line points equal HEAD's for every footprint not moved since).
Symptom on a corrupt board without the fix: thousands of OVERLAPs, a 6.8 mm coin
cell with an 8750 mm2 courtyard, connectors "100 mm from the nearest edge".
