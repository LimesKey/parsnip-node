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

## Multi-top-level projects (KiCad 10.99)

- **The project manager tree lists only the .kicad_sch named after the project.**
  Other top-level sheets show up there only when KiCad's git integration attaches
  (`project_tree_pane.cpp` `showAllSchematics`). Membership is the `.kicad_pro`
  `schematic.top_level_sheets` list plus sheet symbols, not that tree.
- **Navigator order = `top_level_sheets` order** (loaded verbatim); **plot/PDF order
  = page numbers.** A top-level page number lives in its own file's
  `(sheet_instances (path "/" (page N)))`, a subsheet's in the parent's sheet symbol
  `(instances ... (page N))`. `ksheet lint` flags PAGEORDER/NAVORDER.
- **"Root" is KiCad's placeholder name** for a converted single-root project.
  Renaming a top-level sheet renames every local net under it (`/Root/Rails/5V_RAW`)
  and any netclass pattern that uses the path.
- **Stable 10.0 cannot open 10.99 files**, and a double-click may launch stable.
  `kdrc.py FILE doctor` shows which app opens each type.

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
- **Teardrops are zones** (`(attr (teardrop ...))`). Leave them out of any pour sum. A
  deleted track's teardrop left behind reads "Missing connection ... Teardrop"; the
  writers drop them, and a refill re-creates one only where the track is <= 0.9x the
  pad/via.
- **KiCad joins a track to a pad or via on its END CAP** (end within w/2 of the shape),
  not its centreline end; a cap that grazes a pad edge by 0.01 mm is a connection. Never
  cut or remove copper on the strength of such a graze (`loops` checks strict joins too).
  A graze is a connection but looks like a trace stopping at the pad edge: never leave one
  as the pad's only contact (`loops` keeps each pad's last centreline entry; `tracks` lists
  them as `ends at a pad edge`, `smooth --pads` runs them onto the centre).
- **A custom pad's `(size)` is only its anchor** (BQ25798: 0.15 mm, the copper a 0.65 mm
  polygon). Join and hit tests use the primitive polygons, never their bounding box: the
  box once kept U8.1 "connected" through a link that was deleted.
- **`.kicad_dru` type rules override netclass clearance** in KiCad ("Pad to Track" 0.1
  beats the 0.15 Default for every pad-track pair, last matching rule wins, `(layer
  outer|inner)` scopes). kpcb applies them the same way except it never relaxes a raised
  netclass (RF) or a NetName pair rule below its value.
- **The zone fill on disk can be stale** (teardrops or tracks moved after the last
  fill). kicad-cli's DRC with `--refill-zones` hides that; `export gerbers` does
  not refill. `kdrc.py` reports it as DRC:STALE_FILL.
- **KiCad 10.99 can write footprint `fp_line`s in the wrong frame** (build ~4975,
  the 2026-10-08 parsnip save): KiCad then draws silk/courtyard/fab lines near the
  page origin. kpcb reads them corrected and WARNs once; `kpcb.py FILE fixlines -o
  OUT` repairs a copy. A file version number does not tell you which frame you got.
- **kicad-cli pairs a board with `<its stem>.kicad_pro`.** A copy named `base.kicad_pcb`
  beside `parsnip.kicad_pro` silently runs KiCad's default rules (clearance 0.2, min
  hole 0.3, edge 0.5: 499 fake clearance errors on parsnip). A scratch copy keeps the
  project's stem; keep snapshots under other names and copy back before a DRC.
- **Angles are written in two ranges**: footprint `(rotate)` in (-180, 180], pad and
  text `(at x y a)` in [0, 360), and a pad omits a 0 angle while a field never does.
- **kicad-cli's DRC JSON names every item by uuid**, so a violation maps straight back
  to the track or via to delete (`kpcb.py ripup --drc`).
- **Netless tracks that still touch pads** are routes whose net was renamed or deleted
  under them on an Update PCB. One that bridges two nets is a stale route from before
  a schematic change (a series resistor inserted, an ESD channel reassigned), not a
  short to "fix": `kpcb.py renet --drop` rips it up for re-routing.

**A trackless via written into a board must say `(free yes)`.** kicad-cli's `--save-board`
(and KiCad on load) re-nets a via with no track from the copper it touches: a new GND via
beside R41 came back `-BATT` (2026-10-19). `stitch` and `edit via` write it free; DRC then
reports a real clash instead of hiding it behind a re-net.
