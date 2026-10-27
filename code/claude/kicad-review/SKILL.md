---
name: kicad-review
description: >-
  Query, review and edit KiCad projects: netlists (.net), schematics (.kicad_sch), boards (.kicad_pcb) and datasheets, with knet, kpcb, kdrc, ksheet, ksch and kdoc. Use for any question on connectivity, pins, nets, values, BOM, ERC or DRC, power rails, dividers or I2C addresses ("what does U2 pin 9 connect to"); on placement, clearance, edges, courtyards, keepouts, pour fill, trace current, 50 ohm RF, diff pairs or board height ("is U8 too close to the antenna"); on where a regulator's caps and inductor go ("lay out this buck"); on placing parts, routing, cleaning up or widening tracks on a scratch copy ("route the remaining traces"); on silkscreen, designators and logos ("clean up my silkscreen"); on where a part sits on a schematic sheet or whether the drawing misleads; and on ANY request to draw or show a circuit, even one not on the board yet: ksch.py draws real symbols, hand-written SVG is never right. Never answer connectivity or geometry from memory or from a schematic or board image.
---

# KiCad netlist review, placement review and schematic drawing

Stdlib-only tools in this skill's `scripts/`. Nothing to install (`view` uses
`rsvg-convert` when present).

```bash
K=<skill>/scripts/knet.py   P=<skill>/scripts/kpcb.py   H=<skill>/scripts/ksheet.py
D=<skill>/scripts/kdoc.py   S=<skill>/scripts/ksch.py
```

| question | file | tool | reference |
| --- | --- | --- | --- |
| what is wired to what, values, BOM | `.net` | `knet.py` | [knet](references/knet.md) |
| where is it, does it fit, does it clash, where do the passives go | `.kicad_pcb` | `kpcb.py` | [kpcb](references/kpcb.md) |
| place, route, widen, stitch or hand-edit copper on a scratch copy | `.kicad_pcb` | `kpcb.py` writers | [kpcb-writers](references/kpcb-writers.md) |
| does it pass DRC/ERC, real clearance/rule violations | `.kicad_pcb` + roots | `kdrc.py` | [kdrc](references/kdrc.md) |
| what does the part's datasheet say | PDFs | `kdoc.py` | [kdoc](references/kdoc.md) |
| where is it on the schematic sheet, is the drawing readable, show me that region | `.kicad_sch` + `.net` | `ksheet.py` | [ksheet](references/ksheet.md) |
| show me the circuit | - | `ksch.py` | [ksch](references/ksch.md) |
| rename / reorder / renumber / add a sheet, prove nothing else changed, keep the cover / block diagram / power tree true | `.kicad_pro` + sheets | `kproj.py`, `kverify.py`, `kfront.py` | [kproj](references/kproj.md) |

`knet check` and `kpcb check` are heuristics on the netlist and placement;
`kdrc.py` runs **KiCad's own** DRC (honouring `parsnip.kicad_dru`) and ERC, the
authoritative rules check. `kpcb.py FILE zones` reports pour-fill coverage.

All the tools share `kcommon.py` (the S-expr parser, the value/ref helpers, the
footprint-library resolver, the `Netlist` + `SchInfo` model and the finding
formatter). It has no CLI of its own - edit it once and every tool sees it; the
dependency only points tools -> kcommon. Parsed files over 100 kB are cached in
`~/.cache/kicad-review` (marshal, keyed on path + size + mtime; one snapshot per
file; the oldest entries go once it passes 512 MB), and zone fills are parsed only
by the commands that read them, so most kpcb calls on the 12 MB board take ~0.3 s. `kzo.py`
(trace impedance, the CPWG field solver) is kpcb's only other import.
`kpcb.py` needs `kcommon.py` beside it but does **not** need the
`.net` - pads carry their own net names and pin functions. Find the `.net` first:
usually beside the board, else `/mnt/project/*.net` or `/mnt/user-data/uploads/*.net`.
If `*.kicad_sch` files sit beside the `.net`, knet parses them as a sidecar
(no_connect flags, text notes, symbol positions). A sheet saved after the `.net`
makes every loader re-run kmerge first (~3 s, `KREVIEW_NO_REGEN=1` opts out), and a
`.net` lacking a top-level sheet the `.kicad_pro` lists is warned about.

**KiCad stable and nightly both work.** kpcb reads 10.0 `(at X Y R)` and 10.99
`(transform (translate) (rotate))` placement. Tools that shell out (`kdrc`, `kmerge`,
`kpcb height`) pick `kicad-cli-nightly` for a file saved by 10.99 (`generator_version
"10.99"`), else `kicad-cli`; `$KICAD_CLI` overrides. For a multi-root project,
`kmerge.py OUT.net ROOT.kicad_sch` builds the whole-board netlist (other top-level
sheets come from the `.kicad_pro`; a nightly export that already has them passes
through verbatim). The project's own root (`<name>.kicad_sch` beside `<name>.kicad_pro`)
is exported first whatever ROOT is named, and the auto-regen uses it too: a
front-matter page listed first in the navigator exports empty.

## Order of operations

1. **Connectivity, values, footprints, part numbers -> `knet.py`.** The netlist is
   the only authoritative record of what is wired to what.
2. **Position, clearance, floorplan -> `kpcb.py`.** The board file is the only
   record of where anything actually is. Never infer geometry from a screenshot.
3. **Design rules, absolute maximums, recommended values -> `kdoc.py grep`.**
4. **Page image -> only when the drawing itself must be read.** `kdoc.py page DOC N`
   prints a path, then `view` it.

Never derive connectivity from a schematic plot: extracted text is spatially
scrambled. The plot only tells you which sheet a symbol lives on.

## Start here - the highest-value call per tool

| you want | one call |
| --- | --- |
| fresh session on a board | `kpcb.py FILE review` - sync + summary + check + span, then names the next calls to make |
| is the board the circuit I drew | `kpcb.py FILE sync` - **run before quoting any placement finding**; two lines when clean |
| where does X connect / is X right | `knet.py FILE around X` - pins, types, nets, position, every part one hop away |
| where is X, is there room | `kpcb.py FILE where REF` or `where 130,60 -r 10` (`--origin grid` = the numbers KiCad's Properties dialog shows) |
| where do these caps/inductor go | `kpcb.py FILE ic U13` - positions, rotations, the rule behind each, and a picture |
| does the board pass real DRC + ERC | `kdrc.py FILE.kicad_pcb` - KiCad's own checks, every root, folded like `check`; flags a STALE saved zone fill (what gerbers export) |
| which PWR_FLAGs are needed / redundant | `kdrc.py FILE.kicad_pcb flags` - each flag's net, and whether ERC fails without it |
| does the SMA/USB-C sit on the edge its footprint expects / how far is a side-entry housing set back | `kpcb.py FILE check --only EDGEREF` |
| is the ground pour filled / covering | `kpcb.py FILE zones` - fill coverage per copper layer |
| can a net carry its current / is the trace too thin | `kpcb.py FILE ampacity NET --amps X` - IPC-2221 vs the routed copper + vias |
| does the power path between two pads carry X A | `kpcb.py FILE ampacity --from Q16.1 --to R41.1 --amps 6` - current split by conductance, pours as conductors, hottest tracks/vias; `--fills` adds R through the REAL saved fills (pour necks included) and names the neck |
| is the net's copper one piece | `kpcb.py FILE net NET` - ends with `copper one piece` or the pads cut off |
| pour islands KiCad removed / where a stitching via rescues one | `kpcb.py FILE zones --voids` |
| where is U7 on its sheet, what is near it, with nets | `ksheet.py FILE.net sch U7` |
| is the schematic drawing misleading (wire through a body, bypass-looking gaps) | `ksheet.py FILE.net lint [--around REF]` |
| picture of a schematic region | `ksheet.py FILE.net view REF` -> PNG path |
| can I nudge X here / where along this line is clear / can this via move | `kpcb.py FILE movecheck REF X Y [ROT]`, `REF --scan x=X y=A..B`, `via X,Y NX,NY` - new hits only, read-only |
| what is left to route | `kpcb.py FILE airwires [NET]` - per net, each missing link between copper pieces, nearest copper of each, gap |
| where a copper layer still has room for routes | `kpcb.py FILE map --layers In2.Cu` - track/pad/via density, pour-only and free cells |
| where does this unplaced part fit | `kpcb.py FILE place REF [--near IC]` - both sides, 4 rotations, ranked by ratsnest, copper-checked, every pad able to escape (a via spot or its net's copper within 1.5 mm); `--shove` nudges one small part aside when nothing fits; `-o OUT` writes it |
| route a net / everything left | `kpcb.py FILE route NET... \| --pending -o OUT` - grid A* through clearance-inflated copper, vias, all layers; necks down out of fine-pitch pads; rips up its own routes that box a pin in (`--rip-board N`: small nets already on the board too); a FAILED link names what boxes it in; `--pair` runs a diff-pair leg alongside its routed mate; `--avoid X0,Y0,X1,Y1[;..]` keeps this run out of boxes (an antenna whose keepout allows tracks) |
| diff pair skew / coupling / gap | `kpcb.py FILE pairs [NET]` - per pair leg lengths, skew, coupled length, edge gap vs the netclass |
| place and route on a scratch copy, prove each step | the loop in [recipes](references/recipes.md) "Place the rest": `kdrc.py COPY drc --refill-save --baseline r0.json` reads `0 new` when a step is clean |
| widen a routed power path / add its vias | `kpcb.py FILE fatten NET... [--layers L] [--width W] -o OUT` - same-net pour bands round its tracks, leaving its parts' GND pads reachable; `kpcb.py FILE stitch NET [X,Y] [--box ..] -o OUT` - legal via spots on its copper, written `(free yes)` |
| what copper is in this box (with uuids) / a hand edit / a staircased or faceted zone outline | `kpcb.py FILE copper X0,Y0,X1,Y1 [--net N]`, then `kpcb.py FILE edit width UUID W \| del UUID (track, via, zone) \| seg .. \| via .. \| mvvia UUID R \| vcenter all\|DRC.json \| solid REF.PAD \| simplify ZONE EPS \| unround ZONE\|all -o OUT` (unround: sharp corners + KiCad fillet, editable in KiCad) |
| how clean is the routing (corners, off-45, width steps, stubs, lane jogs, detours) and why | `kpcb.py FILE tracks [--kind jog\|detour] [--view census.png]` (16 panels per PNG) |
| clean up the routing / make the traces look nice | `kpcb.py FILE reroute -o OUT` (detours first), `smooth --all -o OUT`, `smooth --jogs [--nudge] -o OUT`, `widen [--uniform-power] -o OUT`, `loops -o OUT` on a scratch copy; what "clean" means and the order: [recipes](references/recipes.md) |
| is this net routed the best way / straighten a meandering route | `kpcb.py FILE reroute NET [-o OUT]` - rips and re-routes on a copy, kept only when shorter or fewer vias |
| where will routing jam / is the placement routable | `kpcb.py FILE congest [--all]` - demand (RUDY) vs free copper per cell and layer, hot regions with their nets and the layer with room |
| how do the pins of this dense part get out | `kpcb.py FILE fanout U5` - per pin: escape side on its layer, dogbone via spot, or boxed (via-in-pad); route a dense area: [recipes](references/recipes.md) |
| what did this edit change | `kpcb.py FILE diff OLD.kicad_pcb` - footprints, silk, vias, zones, teardrops, tracks, per-net R |
| why is this pin boxed in / what does the router see here | `kpcb.py FILE vscan NET X0,Y0,X1,Y1 [--layers L]` |
| rip out only what served a part | `kpcb.py FILE unhook REF -o OUT` |
| a picture of the routing without the pour | `kpcb.py FILE view REF --tracks [--net N]` |
| a scratch copy of the project for the writers | `kdrc.py FILE scratch DIR [--from OTHER.kicad_pcb]` - board under the project stem, rules, sheets, netlist, lib links (DRC beside a lone board checks KiCad's defaults) |
| tracks with no net / stale routes bridging two nets | `kpcb.py FILE renet [--drop] -o OUT` |
| clear the shorts and dangling copper KiCad found | `kpcb.py FILE ripup --drc REPORT.json -o OUT` (report from `kdrc.py --report`) |
| silk/courtyard lines drawn at the page origin, absurd courtyards | `kpcb.py FILE fixlines -o OUT` - the 10.99 fp_line frame bug (kpcb already reads it corrected) |
| what is almost-but-not-quite aligned (tidy placement pass) | `kpcb.py FILE tidy` - REF now -> new, each pre-checked with movecheck; the whole pass is a recipe in [recipes](references/recipes.md) |
| where does a W x H logo/label fit on the silk | `kpcb.py FILE freebox f 8 4` - best centre per free region |
| label a header's pins / put a silk label somewhere clear | `kpcb.py FILE pinlabels REF [NAME...] -o OUT` (names from the nets, through fuses/0R), `kpcb.py FILE label TEXT X,Y\|--near REF -o OUT` |
| is the silkscreen clean: designators over pads, under bodies, overlapping, too small, ambiguous, upside down; logos hidden | `kpcb.py FILE silk` (`silk REF` or `--box` lists the texts there with what `edit` needs) |
| clean up the designators | `kpcb.py FILE refdes -o OUT` - flagged ones to clear spots beside their parts (`--show`, `--size`); the rest by hand: `edit ref REF X,Y 0 \| hide REF \| textsize REF\|UUID H T \| text UUID X,Y A \| value REF X,Y A \| hide-value REF \| valuesize REF H T \| knockout UUID\|REF on\|off \| retext UUID T \| addtext T X,Y A f\|b H` (UUID: a board or footprint user text); the recipe is in [recipes](references/recipes.md) |
| look at the silkscreen | `kpcb.py FILE view REF --layers silk` - drawn from the model, flagged texts red (KiCad's plot buries designators under Fab values) |
| picture of a board region (chosen layers) | `kpcb.py FILE view REF [-r 5] [--layers B.Cu,B.SilkS]` -> PNG path |
| where is this PAD / how far apart are two pads | `kpcb.py FILE where F5.1 BT1.1` |
| everything on one net, with coordinates | `kpcb.py FILE net NET` - pads (absolute xy), copper per layer, vias, zones |
| is this 50-ohm trace right | `kpcb.py FILE rf [NET]` - width necks, microstrip Zo, GND gap + field-solved CPWG Zo, reference plane, via fence |
| what width/gap gives 50 ohm (not routed yet) | `kzo.py W S H ER [T] --mask TM ERM` - the same 2D field solver, one number |
| how thick is the assembled board / what sticks up | `kpcb.py FILE height` - 3D-model heights + the Z stack at each cell |
| vias in pads worth a fab note | `kpcb.py FILE viapad --signal` (or `--min 4` for thermal arrays) |
| what does a backwards cell or pack do | `knet.py FILE revpol` - junctions that conduct (fused?), FETs the cells switch, ICs that lose ground, IC pins pushed below GND with their ESD current |
| what voltage does this divider set | `knet.py FILE divider U5.OVLO` - nominal + worst case from real resistor values |
| what feeds what, every rail, each buck's Vout and eFuse ILIM | `knet.py FILE powertree` - sources -> FETs/regulators -> rails -> loads |
| which I2C address is each part, any duplicates | `knet.py FILE i2c` - per bus: controller, targets, addresses resolved from strap pins |
| via sizes, drills, aspect ratio | `kpcb.py FILE vias` |
| rename/reorder/renumber sheets, then prove the circuit did not change | `kproj.py DIR ...`, then `kverify.py DIR` - one PASS/FAIL line per check |
| do the cover, block diagram, power tree still match the board | `kfront.py FILE.net check` and `pages` (`pages -o OUT` rebuilds the cover table; `draw powertree\|i2c -o OUT` generates a page) |
| why won't the file open / which KiCad opens it / is KiCad holding the project | `kdrc.py FILE.kicad_pcb doctor` |
| unfamiliar board, what is on it | `knet.py FILE summary` then `check` |
| draw a circuit that exists | `knet.py FILE draw U8 -d 2 -o out.svg` |
| draw a circuit that does not exist yet | write a ksch spec, `ksch.py render` - see [ksch](references/ksch.md) |
| what does the datasheet say | `kdoc.py grep 'Y' --count` to pick the page, then grep with context |

More in [references/recipes.md](references/recipes.md), including the self-test to
run after editing any tool, and the 5-line import for an ad-hoc query (Board,
Netlist, load_sexp) - never re-implement paren matching in an inline script.

## Hard rules

- **Re-run the tool every time.** The `.net` and the board are re-exported
  constantly. Never trust a value, ref, net name or coordinate quoted earlier in
  the conversation, in a summary, or in a CLAUDE.md.
- **Never hand-regex a netlist or board file.** It bleeds nodes across net
  boundaries and produces confidently wrong counts.
- **Never hand-write SVG for a circuit.** `ksch.py` draws real symbols.
- **`check` findings are heuristics.** Confirm against the datasheet
  (`kdoc.py grep`) before calling anything a bug.
- **Output is capped on purpose.** Findings fold into `tail [N]: refs` lines and
  each rule stops after `--max`. The counts in the tails stay accurate even when
  not every item is printed: `+90 more OVERLAP line(s) (102 total)` means 102 real
  findings. Widen deliberately with `--only RULE` or `--max`, never by default.
- **The writers (`move`, `place`, `route`, `reroute`, `ripup`, `unhook`, `renet`, `fixlines`, `fatten`, `stitch`, `edit`,
  `smooth`, `widen`, `loops -o`, `refdes`, `pinlabels`, `label`; `kfront pages -o`/`draw`) write a
  copy, `-o OUT`, and refuse their input.** Work on a scratch copy of the project and
  hand back the result; the real board is edited only when the user asks.
- **Mid-placement, most of the BOM is parked beside the board.** Every kpcb check
  ignores anything outside the outline. `placed 135 of 439` is progress, not an
  error, and a part "not found" by `where` may not exist on the board yet.

Common traps that produce wrong answers - DNP as open circuit, ref-prefix
matching, rails being terminal, `unconnected-*` pseudo-nets, KiCad's `{slash}`
escaping, board y growing downward - are in
[references/gotchas.md](references/gotchas.md). Read it once per session before
any traversal or coordinate work.

Exit codes (knet, kpcb): 0 clean, 1 not found, 2 a check found an ERROR, 3 bad file.

Project config: `knet.json` / `kpcb.json` beside the file persist board defaults
and a `suppress` list for confirmed non-bugs. Precedence: defaults < json < CLI.
See the per-tool references.

# Related

Part sourcing (LCSC/DigiKey pricing, stock, verified datasheet URLs) is the
separate `part-search` skill. `part.py bom board.net` reuses this skill's parser.
Capacitor questions mid-review - "higher voltage rating or higher nominal
capacitance", DC-bias derating, effective uF in a footprint, voltage-stress life -
go to that skill's `kcap.py`, not to hand-computed derating curves.
