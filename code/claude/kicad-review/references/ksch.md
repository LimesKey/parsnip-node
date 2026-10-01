# ksch.py - drawing circuits

**Never hand-write SVG for a circuit, and never build one in the visualizer.**
Write a short spec, pipe it to `ksch.py`, `present_files` the `.svg`. A spec is ~20
short lines where hand-drawn SVG is hundreds, and the result looks like KiCad
instead of like boxes with text in them.

| situation | do this |
| --- | --- |
| the circuit is on the board | `knet.py FILE draw U8 -d 2 -o out.svg` - laid out from the netlist |
| ...and needs tidying | `knet.py FILE draw U8 --spec > x.ksch`, edit, `ksch.py render x.ksch -o out.svg` |
| the circuit is proposed / from a datasheet | write the spec by hand (below) |

## Spec language

One statement per line, `#` comments. Grid unit = 2.54 mm = 100 mil, **+y is down**.
Coordinates are integers. **Two-pin parts are 2 units long and the coordinate you
give is pin 1**, so `r R1 4,2 v` puts pin 1 at (4,2) and pin 2 at (4,4).

```
title  <text>
ic   REF x,y NAME [w=N] [h=N] [p=N] [flat] [dnp] L:1=EN,2=IN R:6=OUT T:5=VCC B:8=GND
conn REF x,y NAME R:1=VBUS,2=GND               connector / one-sided box
r|c|cp|l|fb|d|ds|dz|led|tvs|sw|fuse|xtal|bat|jp|tp|ant|ntc  REF x,y [ORIENT] [value] [dnp]
d2s|d2a|d2c  REF x,y [ORIENT] [value] [schottky]   dual diode: series / common A / common K
rsense  REF x,y [ORIENT] [value]     Kelvin shunt, KiCad R_Shunt pins (1/4 force, 2/3 sense)
nmos|pmos|npn|pnp  REF x,y [l] [value]
wire  A.1 [x,y ...] B.2              orthogonal route through the points IN ORDER
net   NAME [@x=N|@y=N] A.1 B.2 ...   named net; @x/@y gives it a trunk column/row
gnd   A.2 [earth|gnda|NAME]          pwr  +3V3 A.1
label NAME A.1     hlabel NAME A.1 [in|out]     glabel NAME A.1
nc    U1.7         note x,y <text>              group x,y w,h <text>
```

- `ic` anchor is the **top-left of the body**; pins run down each side at 2-unit
  pitch in the order listed, and `L:1=EN,,3=RST` leaves a blank slot to keep
  alignment. Height and width are automatic unless you set `h=`/`w=`.
- ORIENT: `v` (default, pin 1 top) `h` (pin 1 left) `vr` (pin 1 bottom) `hr` (pin 1
  right). Transistors: gate/base left by default, `l` mirrors, anchor is the gate or
  base pin, drain/collector down-side of the channel; p-channel and PNP are drawn
  source/emitter up, the way they are actually used.
- Address pins by number or name: `U8.6`, `U8.OUT`, `Q1.g`, `D1.k`, `R1.2`.
- Junction dots, routing and text placement are automatic. A wire always leaves a
  pin the way the pin faces, so it never dives back through its own body.
- `wire` visits its points in the order written: `wire U7.22 30,13 30,9 U7.24`, never
  `wire U7.22 U7.24 30,13 30,9` (that draws pin to pin, then doubles back). ksch
  WARNs when a statement's segments retrace each other.
- `gnd PIN` is a named GND: `--verify` warns when the pin is on another net. For a
  return that is not GND (a pack negative across a shunt) write `gnd PIN -BATT`, or
  `pwr -BATT PIN`. Both glyphs flip to face the pin they hang off.
- Dual diodes put COM (pin 3) out on the left and their text on the right; `l`
  mirrors both. `rsense` takes its text to the left, clear of the sense leads.

## Worked example

One bash call, then `present_files`:

```bash
python3 $S render -o /mnt/user-data/outputs/efuse.svg - <<'EOF'
title U8 GNSS eFuse
ic U8 12,2 TPS259474L L:1=EN/UVLO,2=OVLO R:6=OUT T:5=IN B:8=GND
r R40 4,3 v 100k
r R41 4,9 v 150k
c C40 7,3 v 1uF
pwr +3V3 U8.5
pwr +3V3 R40.1
gnd U8.8
gnd R41.2
gnd C40.2
wire R40.2 R41.1
wire R40.2 U8.1
wire C40.1 7,1 U8.5
label 3V3_GNSS U8.6
nc U8.2
EOF
```

## Getting it right first time

- `ksch.py syms` prints every symbol with its pins and anchor; read it once per
  session if you need a part you have not drawn before. `ksch.py help` prints the
  full language.
- **`--net board.net` is the accuracy feature.** It fills IC pin names and values
  from the netlist, so `ic U8 10,4 @` alone draws the whole part correctly, renumbers
  diode/FET/BJT pins to the real part's by pin name (KiCad's `Device:D` is 1=K, so
  `D1.1` is the cathode once `--net` is given; `D1.k` always is), and it
  verifies every drawn connection against the real board: a wire the board does not
  have, a pin number the part does not have, or a label sitting on the wrong net all
  come back as errors. Use it for any diagram of a circuit that exists.
- Errors name the line and say what was expected. Fix and re-run; do not fall back
  to hand-written SVG.
- The tool also warns about overlapping bodies, wires crossing a body, overlapping
  text (refs, values, pin names, notes, label names; widths estimated from DejaVu
  Sans advances), and pins drawn but left unwired. A horizontal 2-pin part keeps
  its ref within 0.95 above and its value within 1.2 below the pin line, so stacked
  parts at a 3-unit row pitch never touch.
- Flags: `--theme kicad|mono|dark`, `--px N` (22), `--us` (zigzag resistors),
  `--grid`, `--frame`, `--quiet`. Exit 3 = spec error, 2 = verify found an error.
- Layout habits that avoid rework: 5-6 units between columns, 3 between rows, inputs
  left, outputs right, supplies up, grounds down. Put a `pwr`/`gnd` symbol on every
  rail pin instead of running long wires, and use a `label` instead of dragging a
  wire across the sheet.
