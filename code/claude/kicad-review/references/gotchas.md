# Facts that are easy to get wrong

Read this once per session before any traversal or coordinate work.

## Netlist

- **The `.net` is re-exported constantly.** Re-run the tool. Do not trust a value,
  ref or net name quoted earlier in the conversation or in a summary.
- **DNP parts are open circuits by default.** A DNP 0 R in series means the path
  does not exist on the built board; `walk`/`path`/`draw` stop there. `--as-drawn`
  restores schematic-as-drawn traversal; DNP parts still render, dashed.
- **Ref prefix matching must use the full alphabetic prefix.** `JP` is not `J`.
- **Rails are terminal in traversal.** A path through GND or +3V3 is not a path.
  Treat nets over ~8 nodes as rails.
- **Floating pins rarely show as "absent from all nets".** KiCad emits pseudo-nets
  named `unconnected-(REF-PIN-PadN)`. A **named** single-node net is usually a real
  bug (a label going nowhere); `unconnected-*` is an intentional NC or a forgotten
  pin - the sidecar NC annotation tells you which.
- **A part is pass-through if it has exactly two connected pins**, rails included.
  Series resistors bridge two nets: trace both ends.
- **KiCad escapes text**: `{slash}` is `/`, `~{FLT}` is an overbar. Drawings decode
  both; netlist queries use the raw escaped name.
- **Label *shape* is cosmetic; only the *name* connects.**
- **Pin NAMES on stock diode symbols lie.** `Diode:BAV99` K/A/K contradicts its own
  graphics, `Diode:BAT54A` has none, and unidirectional `Diode:SMF*A` names its
  pins A1/A2 (pin 1 is the cathode). Use the generic symbol's name suffix or the
  datasheet, never the pin name. `check`'s PINOUT rule encodes this.
- **Stable vs nightly exports differ.** Stable `kicad-cli` (10.0.x) exports ONE root
  and names it `/`; nightly (10.99) exports every top-level sheet and names the
  root after the `.kicad_pro` (`/Root/`). Stable cannot open a file nightly saved.
  kmerge and kdrc pick the CLI per file; a netlist without a `.kicad_pro` top-level
  sheet gets a WARNING on load.
- **An open eeschema writes `_autosave-*.kicad_sch` files.** Every tool ignores them.

## Board

- **Mid-placement, most of the BOM is parked in a pile beside the board.** Every
  kpcb check ignores anything whose centre is outside the outline, or the pile would
  drown the findings. `placed 135 of 439` in a header is progress, not an error. A
  part "not found" by `where` may simply not exist on the board yet.
- **`.kicad_pcb` y grows downward** and rotation is counter-clockwise on screen, so
  a footprint's child geometry transforms as `x + lx*cos - ...`, not the textbook
  rotation. `where` already prints board coordinates; do not re-derive them from
  `(at ...)` by hand.
- **A `.kicad_pcb` pad carries its own net name and pin function**, so kpcb needs no
  `.net` export. That also means the board can be out of date with the schematic: if
  kpcb and knet disagree about a net, the board has not been re-synced. Run
  `kpcb.py FILE sync` before quoting any placement finding.
- **Ref designators move during layout.** Do not cache them across sessions.
- **Nightly writes placement as `(transform (translate X Y) (rotate R) (scale 1 1))`**,
  not `(at X Y R)`. Pad `(at)` is unchanged and its angle is still ABSOLUTE. A
  reader that only knows `(at)` parks every footprint at 0,0.
- **Teardrops are zones** (`(attr (teardrop ...))`). Leave them out of any pour sum.
- **The zone fill on disk can be stale** (teardrops or tracks moved after the last
  fill). kicad-cli's DRC with `--refill-zones` hides that; `export gerbers` does
  not refill. `kdrc.py` reports it as DRC:STALE_FILL.
