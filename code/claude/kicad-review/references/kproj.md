# kproj / kverify / kfront - project structure, equivalence, front matter

Three tools for the edits that are not circuit edits: renaming or reordering
sheets, renumbering pages, adding a top-level sheet, then proving nothing about
the circuit moved, and keeping the cover / block diagram / power tree true.

## kproj.py - structure edits

```bash
kproj.py DIR rename OLD.kicad_sch NEW.kicad_sch
kproj.py DIR order NAME [NAME ...]          # navigator order of the top-level sheets
kproj.py DIR renumber                       # pages 1..N depth-first
kproj.py DIR add-sheet FILE NAME [--after NAME]
```

- `rename`: `git mv`; the `.kicad_pro` `top_level_sheets[].filename`, or each parent
  sheet symbol's `Sheetfile`; every `(sheetfile "OLD")` in the `.kicad_pcb` (metadata,
  so no Update PCB is needed, which would drag in unrelated pending edits); then
  lists every file in the repo that still names OLD (docs, CLAUDE.md, scripts).
- `order`: rewrites `schematic.top_level_sheets`, which the hierarchy navigator
  loads verbatim. Names not given keep their relative order after the named ones.
- `renumber`: top-level sheets in navigator order, each followed by its children in
  their current page order. A top-level page is its own file's
  `(sheet_instances (path "/" (page N)))`; a subsheet's is the parent's sheet symbol
  `(instances (project (path PARENT (page N))))`, edited inside that symbol only.
- `add-sheet`: creates FILE minimal (header copied from the first top-level sheet)
  if it does not exist, adds a fresh uuid to `top_level_sheets` and `sheets`, then
  renumbers.

Every command builds all edits in memory and asserts each replacement's exact
count; a mismatch writes nothing. The `.kicad_pro` must survive a JSON round trip
byte-for-byte (KiCad's own format does). It refuses while KiCad holds the project
(`kcommon.kicad_running`: a KiCad process, a `~*.lck` lock, an `_autosave-*` file);
`--force` overrides, `--dry-run` prints the edits. UUIDs and sheet names are never
touched, so instance paths and footprint links survive.

Tested on a copy of parsnip (2026-10-04): order Root first, renumber, rename
rails -> power_rails (1 Sheetfile + 96 board sheetfile entries), add a Notes sheet:
kicad-cli-nightly loads it, the PDF has 13 pages in order, `knet diff` is empty.

## kverify.py - did anything but the structure change?

```bash
kverify.py [DIR] [--ref HEAD] [--pages 6,7] [--no-erc] [--keep]
```

One PASS/FAIL line each, both trees frozen into a temp dir (KiCad may be saving):

| check | PASS means |
| --- | --- |
| netlist | `knet diff` of the two kmerge netlists: no part, field, connection or net name changed |
| ERC | violations keyed by type + item descriptions (no coordinates, no sheet names): none added or removed |
| sync | `kpcb sync` output identical, paths and dates stripped |
| pages | the working tree's PDF title-block Ids run 1..N in print order |
| sheets (INFO) | per changed sheet: `page 4 -> 6; 1119 coordinates moved, all by (-2.54,+0)` or `structure changed (N diff lines)` |
| render (`--pages`) | those pages vs the ref page with the same sheet path at 50 dpi, pixels differing > 40/255, bbox in mm |

~13 s with ERC. Exit 0 all PASS, 2 a FAIL, 3 a tool failed. Run it after any
`kproj` edit and after a GUI save that should only have moved things.

## kfront.py - front matter that quotes the design

```bash
kfront.py NET check [--all]     # refdes, "PART (REF)" pairs, I2C addresses on part-less sheets
kfront.py NET pages             # the cover's PAGE/SHEET table vs the .kicad_pro hierarchy
```

- `check` reads text, text boxes and table cells. ERROR on a refdes the netlist
  lacks (ranges `J1-J4` expanded, only prefixes the board uses), on `BQ25798 (U8)`
  when U8's value/MPN is something else (only a parenthesis holding nothing but
  refdes counts, compared with the clause right before it), and on an address line
  (`0x36, 0x0B`) under a `PART (REF)` box that `knet i2c` resolves differently.
  `--all` adds every sheet's notes.
- `pages` finds the table whose header starts PAGE, SHEET and checks each row
  against the depth-first page list (`kproj renumber`'s numbering) and each row's
  `#N` link; prints the true list when stale.

Still drawn by hand: the block diagram and power tree themselves. Their content
comes from `knet.py NET i2c` and `knet.py NET powertree`; the 2026-10-03 generator
is in `prototypes/gen_front_matter.py` (parsnip content hardcoded). KiCad details it
learned: native `(table ...)` cells with `(href "#N")` page links; arrowheads as
filled polylines; run `kicad-cli sch upgrade --force` afterwards so the first GUI
save does not reformat; the first top-level sheet keeps `(embedded_fonts no)`
(re-add it, the CLI drops it).
