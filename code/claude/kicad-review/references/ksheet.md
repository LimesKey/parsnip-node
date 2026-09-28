# ksheet.py - where things are on the schematic sheets

`ksheet.py FILE.net <cmd>`: reads the `.kicad_sch` files beside the netlist and
names every wire and label by its real net (from the netlist). Use it instead of
regexing a `.kicad_sch` by hand, and instead of eyeballing a plot.

| command | use |
| --- | --- |
| `sch REF [-r MM]` | the symbol's sheet, `at`/rot/mirror/unit, body box, each pin's end point, side (where the wire leaves: L/R/U/D) and net, then every symbol, power symbol, wire, label, junction and no-connect within R mm (5) of the body and pin ends, nearest first. |
| `lint [SHEET] [--around REF] [-r MM]` | readability findings ERC never raises, for one sheet, the region around a part, or every sheet. |
| `view REF [-r MM] [-o x.png]` | PNG crop of kicad-cli's own plot around a part (10 mm margin). |
| `view SHEET --box X0,Y0,X1,Y1` | PNG crop of any region, sheet mm. |

SHEET is a file (`bms.kicad_sch`) or a sheet name (`BMS`, `/Root/GNSS/`).
Coordinates are sheet mm, +y down, as KiCad's status bar shows them.

Connectivity follows KiCad: a wire end, pin, label or junction lying on a wire
joins it; two wires crossing with no junction do not. A cluster's net is the
netlist net of the pins in it; a cluster with no pin takes its label (local
labels get the sheet path). Pins of one cluster on two different netlist nets
print `A | B (?)` (a drawing KiCad reads differently from this model).

## lint rules

| rule | fires on | why it misleads |
| --- | --- | --- |
| `WIREBODY` | a wire through a symbol body's interior (0.1 mm in), unless it ends at that symbol's own pin and leaves the way the pin faces (an LED's arrows reach past its pin ends) | a shunt cap drawn on the wire reads as series |
| `GAPLINE` | two collinear wires of different nets, an empty gap <= 2.54 mm between them (not two pins of one part, no symbol or power symbol in the gap) | reads as one wire: a bypassed part |
| `PINJOG` | the wire at a pin leaves sideways by one 50 mil step (<= 1.27 mm) and then runs parallel | a net drawn off its pin's row; a 2.54 mm sideways stub is the usual pin tie and does not fire |
| `CROWD` (INFO) | on a part with >= 6 pins: another part's pin, or a junction/label/wire bend of a different net, within `--crowd` (2.54) mm of a pin end | too much in the pin's neighbourhood to read which wire is whose |

`--only`/`--skip RULE,RULE`, `--max N` as in knet/kpcb. Exit 2 when a WARN fires.

## view

Plots every sheet once per schematic save with `kicad-cli sch export svg -e`
(the right CLI per file; nightly for 10.99 files), cached under
`~/.cache/kicad-review/svg/<hash of sheet mtimes>`, rewrites the SVG viewBox to the
crop (the plot is in mm, so no pixel arithmetic) and rasterises with
`rsvg-convert` at `--px 20` pixels per mm. `-o x.svg` keeps the cropped SVG. The
PNG path is printed; `Read` it to look. ~0.7 s cold, ~0.1 s cached.
