# Display mount: WeAct 3.7" e-paper over a clear plate

The stack in the README render, as stock parts. `code/render/dress.py` models exactly these
(ISO 14580 head sizes, the board's own 3D models for clearances) and writes the plate's cut
file, [display-plate.dxf](display-plate.dxf), on every render.

| Qty | Part | Where |
| --- | --- | --- |
| 4 | M2 x 6 mm brass hex spacer, female-female, 3.5 mm across flats | board front, on H1-H4 |
| 8 | M2 x 4 mm ISO 14580 cheese-head Torx T6, A2 stainless | 4 down through the plate, 4 up through the board |
| 1 | 2 mm clear cast acrylic, laser cut from `display-plate.dxf` | on the M2 spacers |
| 4 | M3 x 5 mm nylon hex spacer, female-female, 5 mm across flats | plate to module |
| 8 | M3 x 4 mm ISO 14580 cheese-head Torx T10, A2 stainless | 4 down through the module, 4 up from under the plate |

Plate: 60 x 130 mm, R3 corners (the board outline), 4 x 2.4 mm holes over H1-H4 and
4 x 3.4 mm holes for the module spacers (ISO 273 medium clearance). The DXF is in mm with its
origin at the lower-left corner.

## Why these sizes

- **6 mm spacers.** The tallest front parts are U12 (E22P) at 3.9 mm and L4/L5 at 3.8 mm, so the
  plate clears them by 2 mm. That is 2 mm less Z than the 8 mm in earlier renders.
- **3.5 mm hex, not 4 mm.** H1/H2 sit beside J11/J13, and every hole is 2.14 mm from the rounded
  corner. A 4 mm hex's corners (2.31 mm radius) reach the SMA courtyards and pass the board edge.
- **Screw lengths.** The M2 screws go 2.0 mm (top) and 2.4 mm (bottom) into the 6 mm spacer and
  stop 1.6 mm apart. If your spacers are not tapped all the way through, check that the thread is
  at least 2.4 mm deep. The M3 screws go 2.4 mm (top) and 2.0 mm (bottom) into the 5 mm nylon
  spacer and stop 0.6 mm apart. Anything longer meets the other screw.
- **Under-plate M3 heads** hang 4.0 mm above the board. At the corner over J7 that leaves
  0.66 mm above the USB-C shell.

## Open before ordering

- **H3, back side:** an M2 head (3.8 mm across) touches J14's housing at nominal (TODO.md,
  "B-side hardware at H3/H4"). A 3.5 mm head, such as an ISO 7380 button head, clears it by
  0.14 mm.
- **Module size** (53.80 x 105.79 mm board, M3 holes 2.8 mm in from the edges) comes from earlier
  notes and has not been measured. Check a real module before cutting the plate.
- **Module header and cable** are not modelled. If the module's 8-pin header points down, the
  5 mm spacers may not clear it. The cable to the board's E-Ink header needs a slot in the plate
  or a path around its edge.
- TODO.md still puts the e-ink "over the cells" (back side). This mount and the render put it
  on the front.
