---
name: kicad-review
description: Query and review KiCad netlists (.net), schematics (.kicad_sch), board placement (.kicad_pcb) and datasheets, and draw schematics: knet.py, kpcb.py, ksheet.py, ksch.py, kdoc.py. Use for any question touching a KiCad schematic, netlist, ERC, BOM, footprint, pin, net, connectivity, decoupling or power rail ("what does U2 pin 9 connect to"), and for board layout, placement, floorplan, footprint position, courtyard, edge clearance, mounting hole or keepout ("is U8 too close to the antenna"), and schematic-sheet geometry or readability ("where is U7 on the sheet", "is this drawing misleading", "show me that part of the schematic"). `kpcb.py FILE ic U13` also RECOMMENDS where a regulator's caps, inductor and feedback divider go, with a diagram: use it for "where do these go", "lay out this buck/LDO", "minimise the switching loop". Never answer connectivity or geometry from memory or by eyeballing a schematic or board image; exports go stale within a session. Use it too for ANY request to draw or show a circuit, even one not on the board yet: ksch.py draws real symbols from a text spec and hand-written SVG is never right.
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
| does it pass DRC/ERC, real clearance/rule violations | `.kicad_pcb` + roots | `kdrc.py` | [kdrc](references/kdrc.md) |
| what does the part's datasheet say | PDFs | `kdoc.py` | [kdoc](references/kdoc.md) |
| where is it on the schematic sheet, is the drawing readable, show me that region | `.kicad_sch` + `.net` | `ksheet.py` | [ksheet](references/ksheet.md) |
| show me the circuit | - | `ksch.py` | [ksch](references/ksch.md) |

`knet check` and `kpcb check` are heuristics on the netlist and placement;
`kdrc.py` runs **KiCad's own** DRC (honouring `parsnip.kicad_dru`) and ERC, the
authoritative rules check. `kpcb.py FILE zones` reports pour-fill coverage.

All the tools share `kcommon.py` (the S-expr parser, the value/ref helpers, the
footprint-library resolver, the `Netlist` + `SchInfo` model and the finding
formatter). It has no CLI of its own - edit it once and every tool sees it; the
dependency only points tools -> kcommon. Parsed files over 100 kB are cached in
`~/.cache/kicad-review` (marshal, keyed on path + size + mtime; one snapshot per
file), so a kpcb call on a 12 MB board costs ~0.3 s instead of ~0.9 s. `kzo.py`
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
through verbatim).

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
| does the power path between two pads carry X A | `kpcb.py FILE ampacity --from Q16.1 --to R41.1 --amps 6` - current split by conductance, pours as conductors, hottest tracks/vias |
| is the net's copper one piece | `kpcb.py FILE net NET` - ends with `copper one piece` or the pads cut off |
| pour islands KiCad removed / where a stitching via rescues one | `kpcb.py FILE zones --voids` |
| where is U7 on its sheet, what is near it, with nets | `ksheet.py FILE.net sch U7` |
| is the schematic drawing misleading (wire through a body, bypass-looking gaps) | `ksheet.py FILE.net lint [--around REF]` |
| picture of a schematic region | `ksheet.py FILE.net view REF` -> PNG path |
| can I nudge X here / where along this line is clear / can this via move | `kpcb.py FILE movecheck REF X Y [ROT]`, `REF --scan x=X y=A..B`, `via X,Y NX,NY` - new hits only, read-only |
| what is almost-but-not-quite aligned (tidy placement pass) | `kpcb.py FILE tidy` - REF now -> new, each pre-checked with movecheck; the whole pass is a recipe in [recipes](references/recipes.md) |
| where does a W x H logo/label fit on the silk | `kpcb.py FILE freebox f 8 4` - best centre per free region |
| are the logos visible / what hides them / do they run over pads | `kpcb.py FILE silk` |
| picture of a board region (chosen layers) | `kpcb.py FILE view REF [-r 5] [--layers B.Cu,B.SilkS]` -> PNG path |
| where is this PAD / how far apart are two pads | `kpcb.py FILE where F5.1 BT1.1` |
| everything on one net, with coordinates | `kpcb.py FILE net NET` - pads (absolute xy), copper per layer, vias, zones |
| is this 50-ohm trace right | `kpcb.py FILE rf [NET]` - width necks, microstrip Zo, GND gap + field-solved CPWG Zo, reference plane, via fence |
| what width/gap gives 50 ohm (not routed yet) | `kzo.py W S H ER [T] --mask TM ERM` - the same 2D field solver, one number |
| how thick is the assembled board / what sticks up | `kpcb.py FILE height` - 3D-model heights + the Z stack at each cell |
| vias in pads worth a fab note | `kpcb.py FILE viapad --signal` (or `--min 4` for thermal arrays) |
| what does a backwards cell or pack do | `knet.py FILE revpol` - junctions that conduct (fused?), FETs the cells switch, ICs that lose ground, IC pins pushed below GND with their ESD current |
| what voltage does this divider set | `knet.py FILE divider U5.OVLO` - nominal + worst case from real resistor values |
| unfamiliar board, what is on it | `knet.py FILE summary` then `check` |
| draw a circuit that exists | `knet.py FILE draw U8 -d 2 -o out.svg` |
| draw a circuit that does not exist yet | write a ksch spec, `ksch.py render` - see [ksch](references/ksch.md) |
| what does the datasheet say | `kdoc.py grep 'Y' --count` to pick the page, then grep with context |

More in [references/recipes.md](references/recipes.md), including the self-test to
run after editing any tool.

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
- **Mid-placement, most of the BOM is parked beside the board.** Every kpcb check
  ignores anything outside the outline. `placed 135 of 439` is progress, not an
  error, and a part "not found" by `where` may not exist on the board yet.

Common traps that produce wrong answers - DNP as open circuit, ref-prefix
matching, rails being terminal, `unconnected-*` pseudo-nets, KiCad's `{slash}`
escaping, board y growing downward - are in
[references/gotchas.md](references/gotchas.md). Read it once per session before
any traversal or coordinate work.

Exit codes (both tools): 0 clean, 1 not found, 2 a check found an ERROR, 3 bad file.

Project config: `knet.json` / `kpcb.json` beside the file persist board defaults
and a `suppress` list for confirmed non-bugs. Precedence: defaults < json < CLI.
See the per-tool references.

# Related

Part sourcing (LCSC/DigiKey pricing, stock, verified datasheet URLs) is the
separate `part-search` skill. `part.py bom board.net` reuses this skill's parser.
Capacitor questions mid-review - "higher voltage rating or higher nominal
capacitance", DC-bias derating, effective uF in a footprint, voltage-stress life -
go to that skill's `kcap.py`, not to hand-computed derating curves.
