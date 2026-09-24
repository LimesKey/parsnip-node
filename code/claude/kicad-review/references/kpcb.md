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
`kpcb_check.py`; `ic`, `zones`, `ampacity`, `viapad`, `rf`, `height` are
`kpcb_<ic|zones|amp|viapad|rf|height>.py`.

| command | use |
| --- | --- |
| `summary` | outline size, stackup, zones, how much of each sheet is placed, biggest parts. Run first. |
| `check` | 10 rule-based findings, grouped ERROR/WARN/INFO. Exit 2 if any ERROR. |
| `where REF...` | **highest value per call.** Position, rotation, courtyard, edge distance, class, nets, and every neighbour within `-r`. Use instead of eyeballing coordinates. |
| `where X,Y -r N` | same, around a bare coordinate - "is there room here". |
| `map [--side f\|b] [--cols N]` | ASCII occupancy map, one letter per schematic sheet. Shows the floorplan and the free space in ~50 lines. |
| `sheet [PATH]` | per-sheet placed/left counts, bounding box, spread, and parts that drifted from their block. |
| `unplaced` | what is still parked off the outline, ref-ranged by sheet, biggest first. |
| `ic REF` | **where the passives should go**, not just what is wrong. See below. `ic` alone lists the parts worth asking about. |
| `span` | nets ranked by how far apart their placed pads sit. The one routing-quality number that exists before routing does. |
| `sync [board.net]` | **run this first.** Board vs netlist: same parts, footprints, values, DNP flags and net on every pad. Finds a `.net` beside the board automatically. |
| `review [board.net]` | one call for a fresh session: sync, summary, check, longest nets, then the specific next calls worth making. |
| `ampacity [NET...]` | current a routed net can carry (IPC-2221): narrowest segment per layer, via bound, length -> R/Vdrop. With `--amps X` or a kpcb.json budget it warns TRACE-THIN / VIA-FEW / LONG-DROP. See below. |
| `zones` | pour coverage per layer from the last saved fill: area%, island count, edge margins. |
| `zones REF...` | does a net's fill actually cover THIS footprint's courtyard - point-sampled, not just the fill's bounding box. Closes "is GND continuous under U9" without a KiCad render. See below. |
| `viapad [--signal] [--min N]` | every component with a via centred inside one of its SMD pads (via-in-pad), each pad tagged GND/PWR/SIG. See below. |
| `where REF.PAD ...` | a pad's absolute centre, size, layer, net and nearest same-net pads. Two specs (pads, refs or x,y) also print the distance between them. |
| `net NET` | every pad on a net with absolute xy, copper per layer (segments, length, width range), vias, zones, extent. Accepts the short name (`LORA_ANT`). |
| `rf [NET...]` | 50-ohm trace review. No net = every net whose netclass names RF/50. See below. |
| `height [REF...]` | 3D-model height of each part and the board's Z stack. Shells out to KiCad's GLB export (~3 s). See below. |

## Flags

`-r N` (neighbour radius, 5), `--max N` (cap per rule / per neighbour list, 12),
`--cols N` / `--side f|b` (for `map`), `--only`/`--skip RULE,RULE`, `--rules`,
`--json`, `--no-suppress`, and one threshold per rule: `--edge 0.5` `--hole 1.5`
`--conn 10` `--rf 8` `--therm 8` `--bypass 3` `--clear 0` `--fb 4` `--gap 0.25`
`--tol 1` `--span 0` (all mm; `--span 0` means half the board diagonal), plus
`--fanout 8`. For `ic`: `--anchor REF`, `--cin REF,REF`, `--cout REF,REF`,
`--ncin 3`, `--ncout 3`, `--assoc 6`. For `ampacity`: `--amps X` (required
current on the named net), `--net=NAME` (repeatable, for a net name that starts
with `-`), `--dt 10` (allowed temp rise, C), `--plating 20` (via barrel copper,
um), `--vdrop 0.25` (V-drop flag threshold). For `viapad`: `--signal`, `--min N`.
For `rf`: `--freq MHz`, `--fence 1.5` (mm from the trace edge that counts as fence).

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
netlist can. Rules: `SYNCPART SYNCFP SYNCVAL SYNCDNP SYNCNET`.

Clean output is two lines (`IN SYNC`), so it is cheap to run every time. A `SYNCNET`
error means re-run KiCad's *Update PCB from Schematic* before reading any other
finding. `SYNCVAL`/`SYNCDNP` alone are annotation drift: worth fixing, but the
connectivity is still right and placement findings still hold.

`unconnected-*` pseudo-nets get a `_1` suffix on the board and not in the netlist;
that is normalised away rather than reported as 439 differences.

## `review` - the whole placement picture in one call

Runs sync, `summary`, `check` and the top of `span`, then prints a **next** block
naming the exact calls worth making on *this* board - `ic` on the unplaced
regulators, `where` on whichever refs appear in the most ERROR findings, `map` if
much is still unplaced. That last block is the point: it is the reasoning a session
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

A net name that starts with `-` (e.g. `-BATT`) looks like an option to argparse;
pass it as `ampacity --amps 2.7 --net=-BATT` (repeatable, `=` required - a space
before the dash still confuses argparse), as `ampacity --amps 2.7 -- -BATT +PACK`
(flags first, then `--`, then the nets), or put it in the kpcb.json `current{}`
budget where the dash is harmless.

To make a finding fixable it prints, per net: a **`find it:`** line listing the
components on the net (ICs/connectors first) - click any in KiCad to highlight the
whole net - and, for the bottleneck, the **nearest pin** (`near U5.23 (2.8 mm
away)`, Ctrl+F the ref) plus the raw `cursor to X,Y` (KiCad shows the cursor in mm,
bottom-right). Highlight the net, go to that ref, scan for the skinny segment. It
also prints the **required width**; when that is impractically large (7-8 mm) the
bottleneck is a thin inner layer (0.0152 mm / ~0.43 oz here) and the real fix is to
route the net on an outer layer or a plane, not to draw an 8 mm inner trace.

**POURED is not a pass.** When a net has > 50 mm2 of fill on a layer the verdict is
POURED and the header reads `POURED - thinnest TRACK x A ... is not the net's
capacity`: the track bottleneck, the per-layer table and the R/Vdrop line then
describe only the thin tracks, never the plane that carries the current (they have
been misquoted as the net's limit before). The verdict lists each pour's area,
fragment count and largest-fragment share and says UNVERIFIED: the pour's
narrowest neck is not measured, so check the path between the end pads in KiCad.
`--json` carries the same `pour` block.

### How it reads the copper (the important part)

It does **not** just take the smallest `(width)` on the net. It builds a
**connectivity graph** of the routed copper: every `(segment)`/`(arc)` endpoint is
a node, endpoints within 0.05 mm merge, a `(via)` stitches its layers, and a shared
pad joins the tracks landing on it. The **bottleneck** is then the narrowest
**bridge** - a segment whose removal would split the net, so all the current must
cross it. A segment sitting in a parallel loop is *not* a bridge, so a trace that
splits and reconverges is no longer mis-read as one thin strand. Each segment is
rated with IPC-2221 `I = k*dT^0.44*A^0.725` (k=0.048 outer copper, 0.024 inner).
Inner layers here are 0.0152 mm (~0.43 oz), so a 1 mm inner trace rates below a
0.2 mm outer one. Vias are a plated barrel (`width = pi*drill`, `--plating` thick),
summed as a parallel bound. Length feeds resistance and voltage drop. The output
prints the graph state (`N pad(s)`, island count) and, when they differ, both the
mandatory-bridge ampacity and the `narrowest single seg` value.

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
  segment; confirm the parallel copper sums to the current.
- **PAD-NECK** (advisory) - the picked bottleneck is a short (< 2x its width), wide
  stub landing right on a pad. IPC-2221's long-trace formula overstates the risk
  here because the pad copper sinks heat locally; not a real limiter unless the
  copper stays that narrow past the pad.
- **MIXED-NET** (advisory) - a bridge is excluded from bottleneck-picking when its
  only far-side pads are sense taps (a thermistor `TH*`, a test point `TP*`, or a
  resistor >= 1k ohm - never a real power path). If *every* bridge on the net turns
  out to be a tap this way, there is no real series bottleneck left to name, and
  MIXED-NET fires instead of TRACE-THIN: the budgeted current almost certainly runs
  through copper this net mixes with a sense path, so verify visually rather than
  trusting the (near-zero) tap current as the net's real bottleneck.

What it still can't see: two traces that only **cross mid-span** with no shared
end/via/pad are separate copper here (KiCad would merge them); a parallel group
that individually passes but **sums short** is left to you (PARALLEL-CHECK). And
IPC-2221 internal ampacity is conservative for a planed board (see the footer).

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

Edge-mount parts (SMA, USB-C) are judged on the ON-BOARD part of the courtyard only:
the grid covers the courtyard clipped to the outline, inset 0.5 mm on clipped sides
for the pour's edge pullback, and the header says what % hangs off. A net that is
neither GND-named nor a rail (a small signal pour) is listed without a verdict,
`local signal pour, not a reference plane`, and never fails the exit code.

## check rules

`OVERLAP EDGECLR HOLECLR CONNACC RFNOISE THERMAL BYPASS NETSPAN NOCRTYD UNPLACED`.
`--rules` for the legend.

- **`OVERLAP`** compares courtyard **bounding boxes**, not their true outlines, so a
  rotated or L-shaped part reports a slightly larger box than it occupies -
  conservative, never permissive. Parts on opposite sides only collide where a
  drilled barrel actually lands in the other's area; a back-side battery holder
  sitting over front-side 0402s is not a finding.
- **`EDGECLR`** is an ERROR when the courtyard crosses the outline, a WARN when it
  is merely inside `--edge`. An edge-mount part (SMA, U.FL, USB-C, `EdgeMount` in
  the footprint name) crossing the edge downgrades to INFO - that is what it is for.
- **`HOLECLR`** keepout radius is the hole's own pad/drill radius plus `--hole`. It
  also catches a mounting hole placed outside the board entirely.
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

`kpcb.py board.kicad_pcb viapad` lists every component where a via's centre lands
inside one of its SMD pads (via-in-pad). One line per pad, grouped by component:

```
via-in-pad: 106 via(s) in 58 SMD pad(s) across 49 component(s).
Same-net via-in-pad needs filled+capped (or type-VII) vias - flag it in the fab quote.

  U8     BQ25798                F.Cu
       pad EP   9 via(s)   OK same net (GND)
```

- **Same-net** (`OK same net (...)`) is intentional via-in-pad: the fab must fill
  and cap (or plate over) those via holes so the part still solders flat. It is a
  cost/process line item, not an error - exit 0.
- **Different-net** (`!! via net X != pad net Y - possible short`) means a via of a
  foreign net sits inside a pad. That is a short, not via-in-pad - exit 2. Confirm
  with `kdrc.py` (KiCad's own DRC is the authority on shorts).
- Only numbered SMD pads count; numberless paste/mechanical slivers are skipped.
- Only free board vias count. KiCad 10 cannot store a via inside a footprint (the
  grammar has no `via` under `footprint`), so every via is a board object anyway;
  the reader takes only top-level `(via)` and never a footprint child. There is no
  "manually placed vs auto" flag on a via in the file - a via is a via.
- The test is centre-in-pad, layer-aware (a blind/buried via that never reaches the
  pad's outer layer is not counted). Rect/roundrect/oval pads use the pad rectangle;
  a `custom`-shape pad is its anchor rectangle PLUS its `(primitives ...)` (gr_poly,
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
under the trace (`GND 100%`, else `BROKEN/NON-GND RETURN PATH`, exit 2); and the GND
via fence within `--fence` mm (1.5) of the trace edge: count per side and the widest
nearest-neighbour gap against lambda/20 in the medium (frequency from `--freq` MHz,
else inferred GNSS 1575.42 / LoRa 915 from the net or sheet name).

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
fab's) are the arbiter. ~0.1 s per solve, memoised; gaps round to 0.01 mm.

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
- a battery holder's model is flagged: it may omit the cell (the parsnip BT1/BT2
  model reads 15.8 mm; a 21700 is 21.7 mm across). Put the measured figure in
  kpcb.json `"height": {"BT1": 23.1}` - an override wins over the model and prints
  with `*`.
Enclosure, gasket, display and standoffs are not on the board and not counted.
