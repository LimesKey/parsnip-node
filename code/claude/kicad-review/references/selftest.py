#!/usr/bin/env python3
"""Runnable self-test for the kicad-review tools.

Executes every check the recipes.md "Self-test" section documents and asserts on
its exit code and key output markers, so a tool edit or a refactor is verified
with ONE command instead of eyeballing counts by hand:

    python3 references/selftest.py         # from the skill root
    ./selftest.py                          # from references/ (it finds itself)

Exit 0 = all pass, 1 = something regressed. It lives beside the fixtures it uses
(selftest.net, selftest.kicad_pcb, selftest.ksch), which each carry one
deliberate fault per rule; a changed count here means either a rule broke or a
fixture did - see recipes.md for what each fixture exercises.

Adding a case is one line in CASES: (label, [tool, *args], expected_exit,
[substrings that must appear in output]).

Refactor safety net on a REAL board (fixtures only prove the rules fire):

    selftest.py --golden DIR BOARD.kicad_pcb BOARD.net

The first run records GOLDEN (read-only commands, no kicad-cli) into DIR; every
later run diffs against it and exits 1 on any change. Record before a refactor,
check after: a pure refactor must be byte-identical. Delete DIR to re-record.
"""
import os, sys, json, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SK   = os.path.dirname(HERE)                       # skill root
S    = os.path.join(SK, 'scripts')
NET  = os.path.join(HERE, 'selftest.net')
PCB  = os.path.join(HERE, 'selftest.kicad_pcb')
PCB2 = os.path.join(HERE, 'selftest_amp2.kicad_pcb')
PCB3 = os.path.join(HERE, 'selftest_nightly.kicad_pcb')   # PCB in 10.99 (transform) form
CROSS = os.path.join(HERE, 'selftest_cross.kicad_pcb')     # two N tracks crossing mid-span
REPEAT = os.path.join(HERE, 'selftest_repeat', 'repeat.net')  # sub.kicad_sch used as sheets A and B
KSCH = os.path.join(HERE, 'selftest.ksch')
TMP  = tempfile.gettempdir()
svg1 = os.path.join(TMP, 'selftest_draw.svg')
svg2 = os.path.join(TMP, 'selftest_ksch.svg')

# (label, [tool, *args], expected_exit, [required substrings in stdout+stderr])
CASES = [
    # 5 warn: /DATA's third part C2 is a shunt to GND, not a stub (RFSTUB is
    # exercised by rf.net in mini_project instead)
    ("knet check",   ['knet.py', NET, 'check'],                 2, ["4 error, 5 warn, 2 info"]),
    ("knet divider", ['knet.py', NET, 'divider', '/VSENSE'],    0, ["1.6667 V", "1.6445 - 1.6890 V"]),
    ("knet walk",    ['knet.py', NET, 'walk', '/DANGLE'],       0, ["R2.2", "DNP"]),
    ("knet draw",    ['knet.py', NET, 'draw', 'U1', '-d', '2', '-o', svg1], 0, []),
    ("ksch render",  ['ksch.py', 'render', '-o', svg2, KSCH],   0, []),   # + no ERROR (checked below)
    ("kdrc selftest", ['kdrc.py', '--selftest'],                0, ["8/8 passed"]),
    ("kpcb check",   ['kpcb.py', PCB, 'check'],                 2, ["4 error, 7 warn, 2 info"]),
    ("kpcb summary", ['kpcb.py', PCB, 'summary'],               0, ["40.00 x 30.00 mm", "placed 16"]),
    # nightly writes (transform (translate X Y) (rotate R)); read as (at) it parks all at 0,0
    ("kpcb nightly", ['kpcb.py', PCB3, 'summary'],              0, ["pcbnew 10.99", "placed 16"]),
    ("kpcb nightly check", ['kpcb.py', PCB3, 'check'],          2, ["4 error, 7 warn, 2 info"]),
    ("kpcb map",     ['kpcb.py', PCB, 'map'],                   0, []),
    ("kpcb span",    ['kpcb.py', PCB, 'span'],                  0, ["1 net(s)"]),
    ("kpcb sheet",   ['kpcb.py', PCB, 'sheet'],                 0, ["/Test/", "/Test/Spare/"]),
    ("kpcb sync",    ['kpcb.py', PCB, 'sync', NET],             2, ["23 error, 7 warn"]),
    ("kpcb ic",      ['kpcb.py', PCB, 'ic'],                    1, ["no regulator-shaped part found"]),  # 1 = not found
    ("kpcb ampacity",['kpcb.py', PCB, 'ampacity', 'PWR', '--amps', '5'], 2, ["TRACE-THIN", "VIA-FEW"]),
    ("kpcb amp-par", ['kpcb.py', PCB, 'ampacity', 'PAR', '--amps', '5'], 0, ["MESH-CHECK"]),  # parallel edges: no bridge, not flagged thin
    # selftest_amp2.kicad_pcb: false-positive classes from SKILL-BACKLOG-DONE.md.
    # NECK's 0.5 mm track end overlaps U1's pad (its cap reaches x 5.05, the pad edge is
    # 5.15), so the 0.2 mm stub is inside one piece of metal: no neck at all
    ("kpcb amp-neck", ['kpcb.py', PCB2, 'ampacity', 'NECK', '--amps', '1.0'],
     0, ["bottleneck 1.45 A on F.Cu (narrowest bridge)", "OK"]),
    ("kpcb amp-tap",  ['kpcb.py', PCB2, 'ampacity', 'TAP', '--amps', '1.2'],
     2, ["TRACE-THIN", "near J1"]),  # real backbone bottleneck, TH2 tap branch excluded
    ("kpcb amp-alltap", ['kpcb.py', PCB2, 'ampacity', 'ALLTAP', '--amps', '1.0'],
     0, ["meshed, no series bottleneck (sense-tap legs skipped: TH1)   OK"]),  # the only bridge is a tap
    # nodal solve J1 -> J2: the TH2 tap leg carries nothing; R = 13.10 + 6.88 mohm by hand
    ("kpcb amp-path", ['kpcb.py', PCB2, 'ampacity', '--from', 'J1.1', '--to', 'J2.1', '--amps', '1.2'], 2,
     ["R 19.98 mohm", "0.30 x  8.00 mm  1.20 A (100%)", "1 element(s) over"]),
    ("kpcb zones-under", ['kpcb.py', PCB2, 'zones', 'U2', 'U3'], 2,
     ["100% of samples covered  (continuous)", "27% of samples covered  (MOSTLY MISSING)"]),
    # a +3V3 via dropped on U1 pad 1's centre -> one same-net via-in-pad, no mismatch
    ("kpcb viapad",  ['kpcb.py', PCB, 'viapad'],                 0,
     ["1 via(s) in 1 SMD pad(s)", "U1", "OK same net (+3V3)"]),
    ("kpcb vias",    ['kpcb.py', PCB, 'vias'],                   0,
     ["2 via(s), 2 net(s)", "0.45  0.20      1  through  +3V3 1"]),
    ("kpcb viapad --signal", ['kpcb.py', PCB, 'viapad', '--signal'], 0, ["1 PWR / 0 SIG", "showing 0"]),
    ("kpcb where pad", ['kpcb.py', PCB, 'where', 'U1.1', 'U1.2'], 0,
     ["8.500, 8.500  (absolute)", "U1.1 -> U1.2: 4.24 mm"]),
    ("kpcb net",     ['kpcb.py', PCB, 'net', '+3V3'],            0, ["2 pad(s)", "U1.1", "1 via(s)",
                                                                  "copper  2 PIECES", "C1.1  (@9.5,20.0)"]),
    # J1 -> J2 only through the crossing at 5,5: 2 x 7.071 mm of 0.3 x 0.035 = 23.17 mohm by hand
    # one resistor in a sheet used twice: R1 in /A/, R2 in /B/, each on its own nets
    ("ksheet repeated sheet", ['ksheet.py', REPEAT, 'sch', 'R2', '-r', '7'], 0,
     ["sheet sub.kicad_sch (/B/)", "100,96.19         /B/SIG", "SIG (label) at 100,90  /B/SIG"]),
    ("knet repeated sheet", ['knet.py', REPEAT, 'around', 'R2'], 0, ["sheet /B/  at (100,100)mm"]),
    ("kpcb net crossing", ['kpcb.py', CROSS, 'net', 'N'],        0, ["copper  one piece joins all 2 pad(s)"]),
    ("kpcb amp crossing", ['kpcb.py', CROSS, 'ampacity', '--from', 'J1.1', '--to', 'J2.1', '--amps', '0.5'], 0,
     ["R 23.17 mohm", "4 track piece(s)"]),
]

def mini_project(d):
    """A throwaway project for what the fixtures above cannot carry: a no_connect
    marker at the END of a wire stub (not on the pin), a .kicad_sch saved after
    the .net, and a .kicad_pro naming a top-level sheet the .net lacks."""
    open(os.path.join(d, 't.kicad_pro'), 'w').write(
        '{"schematic": {"top_level_sheets": [{"filename": "t.kicad_sch", "name": "Root"},'
        ' {"filename": "other.kicad_sch", "name": "Other"}]}}')
    open(os.path.join(d, 't.net'), 'w').write(
        '(export (version "E") (design (source "t.kicad_sch") (sheet (number "1") (name "/Root/")))'
        ' (components (comp (ref "U1") (value "X") (libsource (lib "x") (part "y"))'
        ' (sheetpath (names "/Root/")))'
        ' (comp (ref "D1") (value "BAV199") (footprint "Package_TO_SOT_SMD:SOT-23")'
        ' (libsource (lib "Device") (part "D_Dual_Series_ACK")) (sheetpath (names "/Root/")))'
        ' (comp (ref "BT1") (value "cell") (libsource (lib "Device") (part "Battery_Cell")))'
        ' (comp (ref "D2") (value "SMF10A") (libsource (lib "Diode") (part "SMF10A")))'
        ' (comp (ref "R1") (value "10") (libsource (lib "Device") (part "R")))'
        ' (comp (ref "U2") (value "MAX1") (libsource (lib "x") (part "ic")))'
        ' (comp (ref "U3") (value "QFN") (footprint "t:QFN") (libsource (lib "x") (part "q"))))'
        ' (libparts (libpart (lib "x") (part "y") (pins (pin (num "1") (name "A") (type "passive"))'
        ' (pin (num "2") (name "B") (type "passive"))))'
        ' (libpart (lib "Device") (part "D_Dual_Series_ACK") (pins (pin (num "1") (name "A") (type "passive"))'
        ' (pin (num "2") (name "common") (type "passive")) (pin (num "3") (name "K") (type "passive"))))'
        ' (libpart (lib "Device") (part "Battery_Cell") (pins (pin (num "1") (name "+") (type "passive"))'
        ' (pin (num "2") (name "-") (type "passive"))))'
        ' (libpart (lib "Diode") (part "SMF10A") (pins (pin (num "1") (name "A1") (type "passive"))'
        ' (pin (num "2") (name "A2") (type "passive"))))'
        ' (libpart (lib "Device") (part "R") (pins (pin (num "1") (name "~") (type "passive"))'
        ' (pin (num "2") (name "~") (type "passive"))))'
        ' (libpart (lib "x") (part "ic") (pins (pin (num "1") (name "IN") (type "input"))'
        ' (pin (num "2") (name "GND") (type "power_in"))))'
        ' (libpart (lib "x") (part "q") (pins (pin (num "1") (name "A") (type "passive"))'
        ' (pin (num "3") (name "VIN") (type "power_in")) (pin (num "4") (name "VIN") (type "power_in"))'
        ' (pin (num "6") (name "X") (type "passive")))))'
        ' (nets (net (code "1") (name "unconnected-(U1-A-Pad1)") (node (ref "U1") (pin "1")))'
        ' (net (code "2") (name "unconnected-(U1-B-Pad2)") (node (ref "U1") (pin "2")))'
        ' (net (code "3") (name "VP") (node (ref "BT1") (pin "1")) (node (ref "D2") (pin "1"))'
        ' (node (ref "R1") (pin "1")))'
        ' (net (code "4") (name "GND") (node (ref "BT1") (pin "2")) (node (ref "D2") (pin "2"))'
        ' (node (ref "U2") (pin "2")))'
        ' (net (code "5") (name "Net-(R1-Pad2)") (node (ref "R1") (pin "2")) (node (ref "U2") (pin "1")))'
        ' (net (code "6") (name "N1") (node (ref "U3") (pin "1")))'
        ' (net (code "7") (name "VIN") (node (ref "U3") (pin "3")))'
        ' (net (code "8") (name "NX") (node (ref "U3") (pin "6")))))')
    # footprint library for U3: pad 3 spans pins 3-4 (fused), pad 5 is an EP the
    # symbol has no pin for, MP is a mounting pad, and pin 6 has no pad at all
    open(os.path.join(d, 'fp-lib-table'), 'w').write(
        '(fp_lib_table (lib (name "t") (type "KiCad") (uri "${KIPRJMOD}/t.pretty")))')
    os.makedirs(os.path.join(d, 't.pretty'))
    open(os.path.join(d, 't.pretty', 'QFN.kicad_mod'), 'w').write(
        '(footprint "QFN" ' + ' '.join(f'(pad "{n}" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
                                       for n in ('1', '3', '5', 'MP'))
        + ' (pad "" np_thru_hole circle (at 0 0) (size 1 1) (drill 1)))')
    # RF nets: ANT has a series R, a shunt C, a bias-tee choke L1 (far end
    # bypassed by C2) and the IC = 3 series parts, a stub. VCC_RF is a DC line
    # named like RF: not flagged, the project has an RF netclass.
    def comp(r, v, lib='Device', part='R'):
        return f' (comp (ref "{r}") (value "{v}") (libsource (lib "{lib}") (part "{part}")))'
    def net(code, name, cls, *nodes):
        return (f' (net (code "{code}") (name "{name}") (class "{cls}")'
                + ''.join(f' (node (ref "{r}") (pin "{p}"))' for r, p in nodes) + ')')
    open(os.path.join(d, 'rf.net'), 'w').write(
        '(export (version "E") (design (source "t.kicad_sch"))'
        ' (components' + comp('J1', 'SMA') + comp('R1', '0') + comp('C1', '1p', part='C')
        + comp('L1', '27n', part='L') + comp('C2', '100n', part='C') + comp('U1', 'LNA', 'x', 'ic')
        + comp('R2', '10') + comp('R3', '10') + ')'
        ' (libparts (libpart (lib "x") (part "ic") (pins (pin (num "1") (name "IN") (type "input"))'
        ' (pin (num "2") (name "GND") (type "power_in")))))'
        ' (nets' + net(1, 'ANT', 'RF_50OHM', ('J1', '1'), ('R1', '1'), ('C1', '1'), ('L1', '1'), ('U1', '1'))
        + net(2, 'GND', 'Default', ('C1', '2'), ('C2', '2'), ('U1', '2'), ('J1', '2'))
        + net(3, 'VB', 'Default', ('L1', '2'), ('C2', '1'))
        + net(4, 'VCC_RF', 'Default', ('R1', '2'), ('R2', '1'), ('R3', '1'))
        + net(5, 'N2', 'Default', ('R2', '2')) + net(6, 'N3', 'Default', ('R3', '2')) + '))')
    # a second netlist: one cell with a low-side NMOS reverse-polarity FET (gate
    # pulled to the cell's + through R2), a bidirectional ESD part, a zener
    open(os.path.join(d, 'fet.net'), 'w').write(
        '(export (version "E") (design (source "t.kicad_sch") (sheet (number "1") (name "/Root/")))'
        ' (components (comp (ref "BT1") (value "cell") (libsource (lib "Device") (part "Battery_Cell")))'
        ' (comp (ref "Q1") (value "X") (description "MOSFET N-CH 30V") (libsource (lib "y") (part "fet")))'
        ' (comp (ref "R2") (value "10k") (libsource (lib "Device") (part "R")))'
        ' (comp (ref "R1") (value "10") (libsource (lib "Device") (part "R")))'
        ' (comp (ref "D1") (value "ESD5") (description "Bidirectional TVS") (libsource (lib "Device") (part "D_TVS")))'
        ' (comp (ref "D2") (value "BZX84-C5V1") (description "Zener diode") (libsource (lib "Device") (part "D_Zener")))'
        ' (comp (ref "U1") (value "IC1") (libsource (lib "x") (part "ic"))))'
        ' (libparts (libpart (lib "Device") (part "Battery_Cell") (pins (pin (num "1") (name "+") (type "passive"))'
        ' (pin (num "2") (name "-") (type "passive"))))'
        ' (libpart (lib "y") (part "fet") (pins (pin (num "1") (name "G") (type "input"))'
        ' (pin (num "2") (name "S") (type "passive")) (pin (num "3") (name "D") (type "passive"))))'
        ' (libpart (lib "Device") (part "R") (pins (pin (num "1") (name "~") (type "passive"))'
        ' (pin (num "2") (name "~") (type "passive"))))'
        ' (libpart (lib "Device") (part "D_TVS") (pins (pin (num "1") (name "A1") (type "passive"))'
        ' (pin (num "2") (name "A2") (type "passive"))))'
        ' (libpart (lib "Device") (part "D_Zener") (pins (pin (num "1") (name "K") (type "passive"))'
        ' (pin (num "2") (name "A") (type "passive"))))'
        ' (libpart (lib "x") (part "ic") (pins (pin (num "1") (name "IN") (type "input"))'
        ' (pin (num "2") (name "GND") (type "power_in")))))'
        ' (nets (net (code "1") (name "VP") (node (ref "BT1") (pin "1")) (node (ref "R2") (pin "1"))'
        ' (node (ref "R1") (pin "1")) (node (ref "D1") (pin "1")))'
        ' (net (code "2") (name "VN") (node (ref "BT1") (pin "2")) (node (ref "Q1") (pin "3")))'
        ' (net (code "3") (name "GND") (node (ref "Q1") (pin "2")) (node (ref "U1") (pin "2"))'
        ' (node (ref "D1") (pin "2")) (node (ref "D2") (pin "2")))'
        ' (net (code "4") (name "NG") (node (ref "Q1") (pin "1")) (node (ref "R2") (pin "2"))'
        ' (node (ref "D2") (pin "1")))'
        ' (net (code "5") (name "NI") (node (ref "R1") (pin "2")) (node (ref "U1") (pin "1")))))')
    sch = os.path.join(d, 't.kicad_sch')
    open(sch, 'w').write(
        '(kicad_sch (lib_symbols (symbol "x:y" (symbol "y_1_1"'
        ' (pin passive line (at 0 0 0) (length 2.54) (number "1"))'
        ' (pin passive line (at 0 -2.54 0) (length 2.54) (number "2")))))'
        ' (symbol (lib_id "x:y") (at 100 100 0) (unit 1) (property "Reference" "U1"))'
        ' (wire (pts (xy 100 100) (xy 97.46 100))) (no_connect (at 97.46 100))'
        ' (sheet_instances (path "/" (page "1"))))')
    t = os.path.getmtime(os.path.join(d, 't.net')) + 3600
    os.utime(sch, (t, t))
    lint_project(os.path.join(d, 'lint'))
    sq = lambda x0, y0, x1, y1: f'(pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))'
    fill = lambda ly, *boxes: (f'(zone (net "GND") (layer "{ly}") (polygon {sq(0, 0, 20, 20)})'
                               + ''.join(f' (filled_polygon (layer "{ly}") {sq(*bx)})' for bx in boxes) + ')')
    # B.Cu GND leaves x 9.5..14.5 bare (a removed island); F.Cu is solid GND with a
    # SIG track down x=12, so the rescue via must sit clear of x=12 (>= 0.3+0.15+0.1)
    open(os.path.join(d, 'voids.kicad_pcb'), 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))'
        ' (segment (start 12 1) (end 12 19) (width 0.2) (layer "F.Cu") (net "SIG"))'
        + fill('F.Cu', (0.5, 0.5, 19.5, 19.5)) + fill('B.Cu', (0.5, 0.5, 9.5, 19.5), (14.5, 0.5, 19.5, 19.5)) + ')')
    return os.path.join(d, 't.net')

def tidy_board(d):
    """one near miss per tidy class: C3 0.08 mm off the C1/C2 row, R2 off an even
    pitch, TP1 at 60 deg, H4 inset 3.1 vs 3.0, D1 0.3 mm off U1's long centre line (an X
    track then sits 0.15 mm from its pads, so movecheck flags that one)"""
    p = os.path.join(d, 'tidy.kicad_pcb')
    def fp(ref, name, x, y, r=0, crt=(.5, .3), pads=(('1', -.4, 0), ('2', .4, 0)), drill=''):
        return (f' (footprint "{name}" (layer "F.Cu") (at {x} {y} {r}) (property "Reference" "{ref}")'
                f' (fp_rect (start {-crt[0]} {-crt[1]}) (end {crt[0]} {crt[1]}) (layer "F.CrtYd"))'
                + ''.join(f' (pad "{n}" {"thru_hole" if drill else "smd"} rect (at {px} {py}) (size .3 .3)'
                          f'{drill} (layers "F.Cu") (net "{ref}{n}"))' for n, px, py in pads) + ')')
    hole = lambda r, x, y: fp(r, 'MountingHole_3.2mm', x, y, crt=(2, 2), pads=(('1', 0, 0),), drill=' (drill 3.2)')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))'
        + fp('C1', 'C_0402', 5, 10) + fp('C2', 'C_0402', 7, 10) + fp('C3', 'C_0402', 9, 10.08)
        + fp('R1', 'R_0603', 5, 15) + fp('R2', 'R_0603', 7.1, 15) + fp('R3', 'R_0603', 9, 15)
        + fp('TP1', 'TestPoint', 15, 15, 60)
        + hole('H1', 3, 3) + hole('H2', 37, 3) + hole('H3', 3, 27) + hole('H4', 36.9, 27)
        + fp('U1', 'Holder', 25, 10, crt=(10, 3)) + fp('D1', 'D_0402', 28, 10.3)
        + ' (segment (start 26 9.6) (end 30 9.6) (width 0.2) (layer "F.Cu") (net "X")))')
    return p

def freebox_board(d):
    """30 x 20 board: U1's courtyard over x 1..13, a logo over x 14..19, and board text
    "AB" anchored left-bottom at 25,2 - so a 6 x 4 box only fits right of x 19, below y 2"""
    p = os.path.join(d, 'freebox.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts"))'
        ' (gr_text "AB" (at 25 2 0) (layer "F.SilkS") (effects (font (size 1 1)) (justify left bottom)))'
        ' (footprint "M" (layer "F.Cu") (at 7 10) (property "Reference" "U1")'
        ' (fp_rect (start -6 -9) (end 6 9) (layer "F.CrtYd"))'
        ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "A")))'
        ' (footprint "Logo" (layer "F.Cu") (at 16.5 10) (property "Reference" "REF**")'
        ' (fp_poly (pts (xy -2.5 -9) (xy 2.5 -9) (xy 2.5 9) (xy -2.5 9)) (layer "F.SilkS"))))')
    return p

def move_board(d):
    """R1 (pads A/B at 9.5/10.5,10) with its own A track running to 9.5,5; a foreign C
    track down x=12 (0.2 mm); a bare D via at 8,14. Default clearance 0.2, edge 0.3."""
    p = os.path.join(d, 'move.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))'
        ' (footprint "R" (layer "F.Cu") (at 10 10) (property "Reference" "R1")'
        ' (fp_rect (start -1 -0.5) (end 1 0.5) (layer "F.CrtYd"))'
        ' (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))'
        ' (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "B")))'
        ' (segment (start 9.5 10) (end 9.5 5) (width 0.2) (layer "F.Cu") (net "A"))'
        ' (segment (start 12 2) (end 12 18) (width 0.2) (layer "F.Cu") (net "C"))'
        ' (via (at 8 14) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "D")))')
    return p

def silk_board(d):
    """L1: a logo under U1's 10 x 10 Fab body. L2: a B.Cu footprint whose art is on
    F.SilkS, running over R2's pad 1."""
    p = os.path.join(d, 'silk.kicad_pcb')
    logo = lambda r, x, side: (f' (footprint "Logo" (layer "{side}") (at {x} 10) (property "Reference" "{r}")'
                               ' (fp_poly (pts (xy -2 -1) (xy 2 -1) (xy 2 1) (xy -2 1)) (layer "F.SilkS")))')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 40 20) (layer "Edge.Cuts"))'
        ' (footprint "Module" (layer "F.Cu") (at 10 10) (property "Reference" "U1")'
        ' (fp_rect (start -5 -5) (end 5 5) (layer "F.Fab"))'
        ' (pad "1" smd rect (at -4.5 4.5) (size 0.5 0.5) (layers "F.Cu") (net "A")))'
        ' (footprint "R" (layer "F.Cu") (at 26 10) (property "Reference" "R2")'
        ' (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))'
        ' (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "B")))'
        + logo('L1', 10, 'F.Cu') + logo('L2', 24, 'B.Cu') + ')')
    return p

def edge_board(d):
    """J1: an edge-launch part whose PCB Edge mark sits 0.5 mm inside the top edge.
    J2: a side-entry connector (Horizontal, MP pads toward the bottom edge) whose MP
    pad ends 0.2 mm from the edge, inside the 0.3 mm default edge clearance."""
    p = os.path.join(d, 'edge.kicad_pcb')
    pad = lambda n, x, y, w, h: f' (pad "{n}" smd rect (at {x} {y}) (size {w} {h}) (layers "F.Cu") (net "N{n}"))'
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))'
        ' (footprint "SMA_EdgeMount" (layer "F.Cu") (at 10 1.5) (property "Reference" "J1")'
        ' (fp_line (start -4 -1) (end 4 -1) (layer "Dwgs.User")) (fp_text user "PCB Edge" (at 0 -1.5) (layer "Dwgs.User"))'
        + pad('1', 0, 0, 1, 1) + ')'
        ' (footprint "JST_SH_Horizontal" (layer "F.Cu") (at 10 18) (property "Reference" "J2")'
        ' (fp_rect (start -4 -3) (end 4 1.5) (layer "F.Fab"))'
        + pad('1', 0, -2, .6, 1.5) + pad('MP', -3, .8, 1, 2) + pad('MP', 3, .8, 1, 2) + '))')
    return p

def neck_board(d):
    """U1's pad -> a 0.2 x 0.35 mm stub -> a 0.3 mm track to C1: the track's end stops
    short of the pad (0.35 - 0.15 > 0.15), so the stub is the only way in: PAD-NECK."""
    p = os.path.join(d, 'neck.kicad_pcb')
    pad = lambda r, x: (f' (footprint "P" (layer "F.Cu") (at {x} 5) (property "Reference" "{r}")'
                        ' (pad "1" smd rect (at 0 0) (size 0.3 0.3) (layers "F.Cu") (net "N")))')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts"))' + pad('U1', 5) + pad('C1', 15)
        + ' (segment (start 5 5) (end 5.35 5) (width 0.2) (layer "F.Cu") (net "N"))'
        ' (segment (start 5.35 5) (end 15 5) (width 0.3) (layer "F.Cu") (net "N")))')
    return p

def stub_board(d):
    """J1 -> J2 on a 1.5 mm trunk, with U1 and C1 each on a 0.2 mm stub off it. Every
    arm of the tee is a pad stub (three DC parts), U1's is the only one under 2 A, and
    C1's cap stub is no carrier at all."""
    p = os.path.join(d, 'stub.kicad_pcb')
    pad = lambda r, x, y: (f' (footprint "P" (layer "F.Cu") (at {x} {y}) (property "Reference" "{r}")'
                           ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))')
    seg = lambda a, b, w: f' (segment (start {a}) (end {b}) (width {w}) (layer "F.Cu") (net "N"))'
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts"))'
        + pad('J1', 2, 5) + pad('J2', 18, 5) + pad('U1', 10, 8) + pad('C1', 14, 8)
        + seg('2 5', '10 5', 1.5) + seg('10 5', '14 5', 1.5) + seg('14 5', '18 5', 1.5)
        + seg('10 5', '10 8', 0.2) + seg('14 5', '14 8', 0.2) + ')')
    return p

def dup_board(d):
    """Three padless `REF**` logos (KiCad allows duplicate refs; the third is a B.Cu
    footprint whose art is on F.SilkS) beside one real part, R1."""
    logo = lambda x, side, art: (f' (footprint "Logo" (layer "{side}") (at {x} 5)'
                                 ' (property "Reference" "REF**") (property "Value" "LOGO")'
                                 f' (fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1)) (layer "{art}")))')
    p = os.path.join(d, 'dup.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))'
        + logo(5, 'F.Cu', 'F.SilkS') + logo(10, 'F.Cu', 'F.SilkS') + logo(15, 'B.Cu', 'F.SilkS')
        + ' (footprint "R_0402" (layer "F.Cu") (at 10 12) (property "Reference" "R1") (property "Value" "10k")'
        ' (pad "1" smd rect (at -0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "A"))'
        ' (pad "2" smd rect (at 0.5 0) (size 0.5 0.5) (layers "F.Cu") (net "B"))))')
    open(os.path.join(d, 'dup.net'), 'w').write(
        '(export (version "E") (design (source "dup.kicad_sch"))'
        ' (components (comp (ref "R1") (value "10k") (footprint "R_0402") (libsource (lib "Device") (part "R"))))'
        ' (libparts) (nets (net (code "1") (name "A") (node (ref "R1") (pin "1")))'
        ' (net (code "2") (name "B") (node (ref "R1") (pin "2")))))')
    return p

def tee_board(d):
    """A via whose barrel reaches two tracks' bodies, where track B ends on track A:
    the joins must not depend on set order. J1 -> J2 by hand: 2 mm of 1.5 mm
    (0.655 mohm) + 3.1 mm of 0.2 mm from the via on (7.617) = 8.27 mohm."""
    p = os.path.join(d, 'tee.kicad_pcb')
    pad = lambda r, x, y: (f' (footprint "P" (layer "F.Cu") (at {x} {y}) (property "Reference" "{r}")'
                           ' (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "N")))')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start -2 -2) (end 6 6) (layer "Edge.Cuts"))' + pad('J1', 0, 0) + pad('J2', 2, 4)
        + ' (segment (start 0 0) (end 4 0) (width 1.5) (layer "F.Cu") (net "N"))'
        ' (segment (start 2 0) (end 2 4) (width 0.2) (layer "F.Cu") (net "N"))'
        ' (via (at 2 0.9) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net "N")))')
    return p

def scale_board(d):
    """One 10.99-form footprint scaled 0.5 (CrtYd 8 x 4, Fab 6 x 2 in its own frame)
    at 10,10 on a board whose grid origin is 5,5."""
    p = os.path.join(d, 'scale.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb (version 20250901) (generator "pcbnew") (generator_version "10.99")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (setup (grid_origin 5 5) (aux_axis_origin 2 3))'
        ' (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))'
        ' (footprint "Logo" (layer "F.Cu") (transform (translate 10 10) (rotate 0) (scale 0.5 0.5))'
        ' (property "Reference" "S1") (property "Value" "ART")'
        ' (fp_rect (start -4 -2) (end 4 2) (layer "F.CrtYd"))'
        ' (fp_rect (start -3 -1) (end 3 1) (layer "F.Fab"))'
        ' (fp_poly (pts (xy -2 -1) (xy 2 -1) (xy 2 1)) (layer "F.SilkS"))))')
    return p

def also_net(d):
    """U1.1 on the global net IRQ with two other ICs and a pull-up: `draw U1` puts
    the other ICs on a note line of their own."""
    os.makedirs(os.path.join(d, 'also'))                # no sidecar .kicad_sch beside it
    p = os.path.join(d, 'also', 'also.net')
    ic = lambda r: f' (comp (ref "{r}") (value "IC") (libsource (lib "x") (part "ic3")))'
    nd = lambda *rp: ''.join(f' (node (ref "{r}") (pin "{q}"))' for r, q in rp)
    open(p, 'w').write(
        '(export (version "E") (design (source "also.kicad_sch"))'
        ' (components' + ic('U1') + ic('U2') + ic('U3')
        + ' (comp (ref "R1") (value "10k") (libsource (lib "Device") (part "R"))))'
        ' (libparts (libpart (lib "x") (part "ic3") (pins (pin (num "1") (name "IRQ") (type "output"))'
        ' (pin (num "2") (name "A") (type "input")) (pin (num "3") (name "B") (type "input"))))'
        ' (libpart (lib "Device") (part "R") (pins (pin (num "1") (name "~") (type "passive"))'
        ' (pin (num "2") (name "~") (type "passive")))))'
        ' (nets (net (code "1") (name "IRQ")' + nd(('U1', '1'), ('U2', '1'), ('U3', '1'), ('R1', '1')) + ')'
        ' (net (code "2") (name "VP")' + nd(('R1', '2')) + ')'
        ' (net (code "3") (name "/A")' + nd(('U1', '2'), ('U2', '2'), ('U3', '2')) + ')'
        ' (net (code "4") (name "/B")' + nd(('U1', '3'), ('U2', '3'), ('U3', '3')) + ')))')
    return p

def xref_net(d):
    """U1.2 -> R1 -> /B -> R2 -> GND, and U1.3 on /B too: the second visit to /B is
    one stub row naming the parts drawn above (rows were allocated that way)."""
    os.makedirs(os.path.join(d, 'xref'))
    p = os.path.join(d, 'xref', 'xref.net')
    r = lambda q: f' (comp (ref "{q}") (value "10k") (libsource (lib "Device") (part "R")))'
    nd = lambda *rp: ''.join(f' (node (ref "{a}") (pin "{q}"))' for a, q in rp)
    open(p, 'w').write(
        '(export (version "E") (design (source "xref.kicad_sch"))'
        ' (components (comp (ref "U1") (value "IC") (libsource (lib "x") (part "ic3")))' + r('R1') + r('R2') + ')'
        ' (libparts (libpart (lib "x") (part "ic3") (pins (pin (num "1") (name "IRQ") (type "output"))'
        ' (pin (num "2") (name "A") (type "input")) (pin (num "3") (name "B") (type "input"))))'
        ' (libpart (lib "Device") (part "R") (pins (pin (num "1") (name "~") (type "passive"))'
        ' (pin (num "2") (name "~") (type "passive")))))'
        ' (nets (net (code "1") (name "/A")' + nd(('U1', '2'), ('R1', '1')) + ')'
        ' (net (code "2") (name "/B")' + nd(('R1', '2'), ('U1', '3'), ('R2', '1')) + ')'
        ' (net (code "3") (name "GND")' + nd(('R2', '2')) + ')))')
    return p

def multi_net(d):
    """U1 with a BAT54S (D1), a Kelvin R_Shunt (R1) and a Device:D (D2, pin 1 = K)
    on its pins: real symbols, every pin ended, and D2's cathode toward U1."""
    os.makedirs(os.path.join(d, 'multi'))
    p = os.path.join(d, 'multi', 'multi.net')
    pins = lambda *pn: ' (pins' + ''.join(f' (pin (num "{n}") (name "{m}") (type "passive"))'
                                          for n, m in pn) + ')'
    comp = lambda r, v, lib, part: f' (comp (ref "{r}") (value "{v}") (libsource (lib "{lib}") (part "{part}")))'
    nd = lambda *rp: ''.join(f' (node (ref "{r}") (pin "{q}"))' for r, q in rp)
    open(p, 'w').write(
        '(export (version "E") (design (source "multi.kicad_sch"))'
        ' (components' + comp('U1', 'IC', 'x', 'ic3') + comp('D1', 'BAT54S', 'Diode', 'BAT54S')
        + comp('R1', '5m', 'Device', 'R_Shunt') + comp('D2', 'BZX', 'Device', 'D')
        + comp('R2', '1k', 'Device', 'R') + ')'
        ' (libparts (libpart (lib "x") (part "ic3")' + pins(('1', 'IN'), ('2', 'CS'), ('3', 'G')) + ')'
        ' (libpart (lib "Diode") (part "BAT54S")' + pins(('1', 'A'), ('2', 'K'), ('3', 'COM')) + ')'
        ' (libpart (lib "Device") (part "R_Shunt")' + pins(('1', ''), ('2', ''), ('3', ''), ('4', '')) + ')'
        ' (libpart (lib "Device") (part "D")' + pins(('1', 'K'), ('2', 'A')) + ')'
        ' (libpart (lib "Device") (part "R")' + pins(('1', '~'), ('2', '~')) + '))'
        ' (nets (net (code "1") (name "GND")' + nd(('D1', '1'), ('R1', '1'), ('R1', '4'), ('D2', '2'), ('R2', '2')) + ')'
        ' (net (code "2") (name "/NIN")' + nd(('U1', '1'), ('D1', '2')) + ')'
        ' (net (code "3") (name "/MID")' + nd(('D1', '3'), ('R2', '1')) + ')'
        ' (net (code "4") (name "/NCS")' + nd(('U1', '2'), ('R1', '2')) + ')'
        ' (net (code "5") (name "/NCSN")' + nd(('R1', '3')) + ')'
        ' (net (code "6") (name "/NG")' + nd(('U1', '3'), ('D2', '1')) + ')))')
    return p

def power_net(d):
    """BT1 -> F1 -> VSYS -> U1 buck (SW -> L1, FB 402k/174k) -> +3V3 -> U3 eFuse (ILM
    549 R to GND) -> 3V3_OUT -> U2; and J1 VBUS -> R5 0 R -> VSYS, a second way in.
    knet.json beside it gives VFB and the ILIM table row."""
    os.makedirs(os.path.join(d, 'pwr'))
    open(os.path.join(d, 'pwr', 'knet.json'), 'w').write(
        '{"vref": {"BUCK": 1.0}, "ilim": {"TPS25947": {"549": "5.40/6.07/6.60 A"}}}')
    p = os.path.join(d, 'pwr', 'pwr.net')
    pins = lambda *pn: ' (pins' + ''.join(f' (pin (num "{i + 1}") (name "{m}") (type "passive"))'
                                          for i, m in enumerate(pn)) + ')'
    comp = lambda r, v, part: f' (comp (ref "{r}") (value "{v}") (libsource (lib "x") (part "{part}")))'
    net = lambda c, name, *rp, cls='Default': (f' (net (code "{c}") (name "{name}") (class "{cls}")'
                                               + ''.join(f' (node (ref "{r}") (pin "{q}"))' for r, q in rp) + ')')
    open(p, 'w').write(
        '(export (version "E") (design (source "pwr.kicad_sch")) (components'
        + comp('BT1', 'cell', 'cell') + comp('F1', '1A', 'Fuse') + comp('U1', 'BUCK1', 'buck')
        + comp('L1', '2.2u', 'L') + comp('R1', '402k', 'R') + comp('R2', '174k', 'R')
        + comp('U3', 'TPS25947', 'efuse') + comp('R4', '549', 'R') + comp('U2', 'MCU', 'load')
        + comp('J1', 'USB', 'conn') + comp('R5', '0', 'R') + ')'
        ' (libparts (libpart (lib "x") (part "cell")' + pins('+', '-') + ')'
        ' (libpart (lib "x") (part "Fuse")' + pins('1', '2') + ')'
        ' (libpart (lib "x") (part "buck")' + pins('VIN', 'SW', 'FB', 'GND') + ')'
        ' (libpart (lib "x") (part "L")' + pins('1', '2') + ') (libpart (lib "x") (part "R")' + pins('1', '2') + ')'
        ' (libpart (lib "x") (part "efuse")' + pins('IN', 'OUT', 'ILM', 'GND') + ')'
        ' (libpart (lib "x") (part "load")' + pins('VCC') + ') (libpart (lib "x") (part "conn")' + pins('VBUS') + '))'
        ' (nets' + net(1, 'N_BT', ('BT1', '1'), ('F1', '1'))
        + net(2, 'GND', ('BT1', '2'), ('U1', '4'), ('R2', '2'), ('R4', '2'), ('U3', '4'))
        + net(3, 'VSYS', ('F1', '2'), ('U1', '1'), ('R5', '2'), cls='PWR_HIGH')
        + net(4, 'Net-(U1-SW)', ('U1', '2'), ('L1', '1')) + net(5, 'Net-(U1-FB)', ('U1', '3'), ('R1', '2'), ('R2', '1'))
        + net(6, '+3V3', ('L1', '2'), ('R1', '1'), ('U3', '1')) + net(7, 'Net-(U3-ILM)', ('U3', '3'), ('R4', '1'))
        + net(8, '3V3_OUT', ('U3', '2'), ('U2', '1')) + net(9, 'VBUS', ('J1', '1'), ('R5', '1')) + '))')
    return p

def i2c_net(d):
    """U1 (an MCU, IO1/IO2) with two TCAL9539: U2 A0 via R1 10k to +3V3, A1 GND (0x75);
    U3 A0 straight to +3V3, A1 GND (0x75 again: a duplicate). R3 pulls SDA up."""
    os.makedirs(os.path.join(d, 'i2c'))
    p = os.path.join(d, 'i2c', 'i2c.net')
    pins = lambda *pn: ' (pins' + ''.join(f' (pin (num "{i + 1}") (name "{m}") (type "passive"))'
                                          for i, m in enumerate(pn)) + ')'
    comp = lambda r, v, part: f' (comp (ref "{r}") (value "{v}") (libsource (lib "x") (part "{part}")))'
    net = lambda c, name, *rp: (f' (net (code "{c}") (name "{name}")'
                                + ''.join(f' (node (ref "{r}") (pin "{q}"))' for r, q in rp) + ')')
    open(p, 'w').write(
        '(export (version "E") (design (source "i2c.kicad_sch")) (components'
        + comp('U1', 'MCU', 'mcu') + comp('U2', 'TCAL9539', 'io') + comp('U3', 'TCAL9539', 'io')
        + comp('R1', '10k', 'R') + comp('R3', '2.2k', 'R') + ')'
        ' (libparts (libpart (lib "x") (part "mcu")' + pins('IO1', 'IO2') + ')'
        ' (libpart (lib "x") (part "io")' + pins('SDA', 'SCL', 'A0', 'A1') + ')'
        ' (libpart (lib "x") (part "R")' + pins('1', '2') + '))'
        ' (nets' + net(1, 'SDA', ('U1', '1'), ('U2', '1'), ('U3', '1'), ('R3', '1'))
        + net(2, 'SCL', ('U1', '2'), ('U2', '2'), ('U3', '2'))
        + net(3, '+3V3', ('R1', '2'), ('U3', '3'), ('R3', '2')) + net(4, 'Net-(U2-A0)', ('U2', '3'), ('R1', '1'))
        + net(5, 'GND', ('U2', '4'), ('U3', '4')) + '))')
    return p

def fet_pinout_project(d):
    """Q1 (Q_NMOS_DGS) on a footprint that numbers its pads by function like KiCad's
    VSONP-8: pad 1 three pins (S), pad 2 the gate, pad 3 four pins + EP (D)"""
    os.makedirs(os.path.join(d, 't.pretty'))
    open(os.path.join(d, 'fp-lib-table'), 'w').write(
        '(fp_lib_table (lib (name "t") (type "KiCad") (uri "${KIPRJMOD}/t.pretty")))')
    open(os.path.join(d, 't.pretty', 'SON.kicad_mod'), 'w').write(
        '(footprint "SON" ' + ' '.join(f'(pad "{n}" smd rect (at 0 0) (size 1 1) (layers "F.Cu"))'
                                       for n in '111233333') + ')')
    p = os.path.join(d, 'q.net')
    open(p, 'w').write(
        '(export (version "E") (design (source "q.kicad_sch"))'
        ' (components (comp (ref "Q1") (value "CSD18512Q5B") (footprint "t:SON")'
        ' (libsource (lib "Transistor_FET") (part "Q_NMOS_DGS"))))'
        ' (libparts (libpart (lib "Transistor_FET") (part "Q_NMOS_DGS") (pins (pin (num "1") (name "D") (type "passive"))'
        ' (pin (num "2") (name "G") (type "input")) (pin (num "3") (name "S") (type "passive")))))'
        ' (nets (net (code "1") (name "/D") (node (ref "Q1") (pin "1")))'
        ' (net (code "2") (name "/G") (node (ref "Q1") (pin "2")))'
        ' (net (code "3") (name "/S") (node (ref "Q1") (pin "3")))))')
    return p

def lint_project(d):
    """ksheet fixture, one hit per lint rule: a wire across R1's body (WIREBODY),
    /NA and /NB end-to-end 2.54 mm apart (GAPLINE), U1.1 jogging 1.27 mm at the
    pin (PINJOG), and R3.2 1.27 mm from U1.2's end (CROWD)."""
    os.makedirs(d)
    pin = lambda n, x, y, a, ln: (f'(pin passive line (at {x} {y} {a}) (length {ln}) '
                                   f'(name "{chr(64 + int(n))}") (number "{n}"))')
    sym = lambda lib, ref, x, y, r=0: (f'(symbol (lib_id "{lib}") (at {x} {y} {r}) (unit 1) '
                                        f'(property "Reference" "{ref}") (property "Value" "v"))')
    open(os.path.join(d, 'lint.kicad_sch'), 'w').write(
        '(kicad_sch (lib_symbols'
        ' (symbol "x:R" (symbol "R_0_1" (rectangle (start -1.016 2.54) (end 1.016 -2.54)))'
        '  (symbol "R_1_1" ' + pin(1, 0, 3.81, 270, 1.27) + pin(2, 0, -3.81, 90, 1.27) + '))'
        ' (symbol "x:U" (symbol "U_0_1" (rectangle (start -2.54 6.35) (end 2.54 -8.89)))'
        '  (symbol "U_1_1" ' + ''.join(pin(i + 1, -5.08, 5.08 - 2.54 * i, 0, 2.54) for i in range(6)) + ')))'
        + sym('x:R', 'R1', 50, 50) + sym('x:U', 'U1', 100, 100) + sym('x:R', 'R3', 89.84, 97.46, 90)
        + ' (wire (pts (xy 45 50) (xy 55 50)))'
        ' (wire (pts (xy 60 40) (xy 70 40))) (wire (pts (xy 72.54 40) (xy 80 40)))'
        ' (label "NA" (at 60 40 0)) (label "NB" (at 80 40 0))'
        ' (wire (pts (xy 94.92 94.92) (xy 94.92 93.65))) (wire (pts (xy 94.92 93.65) (xy 90 93.65)))'
        ' (sheet_instances (path "/" (page "1"))))')
    open(os.path.join(d, 'lint.net'), 'w').write(
        '(export (version "E") (design (source "lint.kicad_sch") (sheet (number "1") (name "/")))'
        ' (components' + ''.join(f' (comp (ref "{r}") (value "v") (libsource (lib "x") (part "{p}")))'
                                 for r, p in (('R1', 'R'), ('R3', 'R'), ('U1', 'U'))) + ')'
        ' (nets (net (code "1") (name "/J") (node (ref "U1") (pin "1")))'
        ' (net (code "2") (name "/K") (node (ref "U1") (pin "2")))'
        ' (net (code "3") (name "/L") (node (ref "R3") (pin "2")))))')

def kproj_project(d):
    """a/ (navigator first, page 2) owns child c.kicad_sch (page 3) via sheet symbol
    U1; b/ is page 1. The board names c.kicad_sch on two footprints."""
    os.makedirs(d)
    open(os.path.join(d, 't.kicad_pro'), 'w').write(json.dumps(
        {'schematic': {'top_level_sheets': [{'filename': 'a.kicad_sch', 'name': 'A', 'uuid': 'ua'},
                                            {'filename': 'b.kicad_sch', 'name': 'B', 'uuid': 'ub'}]},
         'sheets': [['ua', 'A'], ['ub', 'B'], ['U1', 'C']]}, indent=2) + '\n')
    inst = lambda pg: f'\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "{pg}")\n\t\t)\n\t)\n'
    open(os.path.join(d, 'a.kicad_sch'), 'w').write(
        '(kicad_sch\n\t(version 20260830)\n\t(generator_version "10.99")\n\t(sheet\n\t\t(at 10 10)\n'
        '\t\t(uuid "U1")\n\t\t(property "Sheetname" "C")\n\t\t(property "Sheetfile" "c.kicad_sch")\n'
        '\t\t(instances\n\t\t\t(project "t"\n\t\t\t\t(path "/ua"\n\t\t\t\t\t(page "3")\n'
        '\t\t\t\t)\n\t\t\t)\n\t\t)\n\t)\n' + inst(2) + ')\n')
    open(os.path.join(d, 'b.kicad_sch'), 'w').write('(kicad_sch\n' + inst(1) + ')\n')
    open(os.path.join(d, 'c.kicad_sch'), 'w').write('(kicad_sch\n)\n')
    open(os.path.join(d, 't.kicad_pcb'), 'w').write(
        '(kicad_pcb\n\t(footprint "R"\n\t\t(sheetfile "c.kicad_sch")\n\t)\n'
        '\t(footprint "C"\n\t\t(sheetfile "c.kicad_sch")\n\t)\n)\n')
    return d

def front_project(d):
    """main.kicad_sch (page 1) holds U8 (BQ25798) and J1; front.kicad_sch (page 2,
    no parts) quotes them: a right pair, a wrong address, a wrong part, a missing
    ref, a range running past J1, and a cover page table one row stale."""
    os.makedirs(d)
    open(os.path.join(d, 't.kicad_pro'), 'w').write(json.dumps(
        {'schematic': {'top_level_sheets': [{'filename': 'main.kicad_sch', 'name': 'Main', 'uuid': 'um'},
                                            {'filename': 'front.kicad_sch', 'name': 'Front', 'uuid': 'uf'}]}},
        indent=2) + '\n')
    open(os.path.join(d, 't.net'), 'w').write(
        '(export (version "E") (design (source "main.kicad_sch") (sheet (number "1") (name "/Main/"))'
        ' (sheet (number "2") (name "/Front/"))) (components'
        ' (comp (ref "U8") (value "BQ25798") (libsource (lib "x") (part "c")) (sheetpath (names "/Main/")))'
        ' (comp (ref "J1") (value "USB") (libsource (lib "x") (part "j")) (sheetpath (names "/Main/"))))'
        ' (libparts) (nets (net (code "1") (name "N") (node (ref "U8") (pin "1")) (node (ref "J1") (pin "1")))))')
    txt = lambda s, x, y: f' (text "{s}" (at {x} {y} 0))'
    cell = lambda s, pg: f' (table_cell "{s}" (at 0 0 0) (effects (href "#{pg}")))'
    open(os.path.join(d, 'main.kicad_sch'), 'w').write('(kicad_sch (sheet_instances (path "/" (page "1"))))')
    open(os.path.join(d, 'front.kicad_sch'), 'w').write(
        '(kicad_sch' + txt('BQ25798 (U8)', 50, 50) + txt('0x6A', 50, 52) + txt('TPS25751 (U8)', 90, 50)
        + txt('debug header U99', 90, 60) + txt('USB (J1-J2)', 90, 70) + txt('CHARGER (E2-E0 by U8)', 90, 80)
        + ' (table (column_count 2) (cells' + cell('PAGE', 1) + cell('SHEET', 1) + cell('1', 1) + cell('Main', 1)
        + cell('2', 3) + cell('Front', 3) + '))' + ' (sheet_instances (path "/" (page "2"))))')
    return os.path.join(d, 't.net')

def lint2_project(d):
    """C1 and C2 (horizontal) feed one vertical GND wire at x=50 from opposite sides
    with no GND symbol on it (SHUNTBUS, both forms); R9's reference sits on C2's
    value (TEXTOVER). A pages/ project: navigator B, A; pages A=1, its child C=3, B=2."""
    os.makedirs(d)
    lib = ('(lib_symbols (symbol "x:C" (pin_names (hide yes)) (pin_numbers (hide yes))'
           ' (symbol "C_0_1" (rectangle (start -0.5 1) (end 0.5 -1)))'
           ' (symbol "C_1_1" (pin passive line (at 0 2.54 270) (length 1.27) (name "~") (number "1"))'
           ' (pin passive line (at 0 -2.54 90) (length 1.27) (name "~") (number "2")))))')
    fld = lambda k, v, x, y: f' (property "{k}" "{v}" (at {x} {y} 0) (effects (font (size 1.27 1.27))))'
    sym = lambda ref, val_, x, y, rx, ry: (f' (symbol (lib_id "x:C") (at {x} {y} 90) (unit 1)'
                                          + fld('Reference', ref, rx, ry) + fld('Value', val_, x, y + 3) + ')')
    open(os.path.join(d, 'lint2.kicad_sch'), 'w').write(
        '(kicad_sch ' + lib + sym('C1', '1u', 45, 50, 45, 47) + sym('C2', '1u', 55, 50, 55, 47)
        + sym('R9', 'x', 70, 70, 55.3, 53)
        + ' (wire (pts (xy 50 40) (xy 50 60))) (wire (pts (xy 47.54 50) (xy 50 50)))'
        ' (wire (pts (xy 50 50) (xy 52.46 50))) (sheet_instances (path "/" (page "1"))))')
    open(os.path.join(d, 'lint2.net'), 'w').write(
        '(export (version "E") (design (source "lint2.kicad_sch") (sheet (number "1") (name "/")))'
        ' (components' + ''.join(f' (comp (ref "{r}") (value "1u") (libsource (lib "x") (part "C")))'
                                 for r in ('C1', 'C2', 'R9')) + ')'
        ' (nets (net (code "1") (name "GND") (node (ref "C1") (pin "2")) (node (ref "C2") (pin "1")))'
        ' (net (code "2") (name "/A") (node (ref "C1") (pin "1"))) (net (code "3") (name "/B") (node (ref "C2") (pin "2")))))')
    pg = os.path.join(d, 'pages')
    os.makedirs(pg)
    open(os.path.join(pg, 't.kicad_pro'), 'w').write(
        '{"schematic": {"top_level_sheets": [{"filename": "b.kicad_sch", "name": "B"},'
        ' {"filename": "a.kicad_sch", "name": "A"}]}}')
    open(os.path.join(pg, 'a.kicad_sch'), 'w').write(
        '(kicad_sch (sheet (at 10 10) (size 10 10) (property "Sheetname" "C") (property "Sheetfile" "c.kicad_sch")'
        ' (instances (project "t" (path "/u1" (page "3"))))) (sheet_instances (path "/" (page "1"))))')
    open(os.path.join(pg, 'b.kicad_sch'), 'w').write('(kicad_sch (sheet_instances (path "/" (page "2"))))')
    open(os.path.join(pg, 'c.kicad_sch'), 'w').write('(kicad_sch)')
    return os.path.join(d, 'lint2.net'), pg

GOLDEN = [['kpcb.py', '{pcb}', c] for c in ('summary', 'check', 'span', 'zones', 'rf', 'viapad', 'ic',
                                            'height', 'unplaced', 'sheet', 'map', 'review')] \
    + [['kpcb.py', '{pcb}', 'sync', '{net}'], ['kpcb.py', '{pcb}', 'ic', 'U13'],
       ['kpcb.py', '{pcb}', 'ampacity', 'VSYS'], ['kpcb.py', '{pcb}', 'where', 'U12'],
       ['kpcb.py', '{pcb}', 'net', 'VSYS'], ['kpcb.py', '{pcb}', 'check', '--json']] \
    + [['knet.py', '{net}', c] for c in ('summary', 'check', 'rails', 'revpol', 'unconnected', 'bom',
                                         'powertree', 'i2c')] \
    + [['kpcb.py', '{pcb}', 'zones', '--voids'], ['kpcb.py', '{pcb}', 'zones', 'U12'],
       ['kpcb.py', '{pcb}', 'ic', 'U8'], ['kpcb.py', '{pcb}', 'net', 'GND'],
       ['kpcb.py', '{pcb}', 'ampacity', '--from', 'Q16.1', '--to', 'R41.1', '--amps', '6'],
       ['knet.py', '{net}', 'divider', 'U8.TS'], ['knet.py', '{net}', 'draw', 'U7', '--spec'],
       ['ksheet.py', '{net}', 'lint'], ['ksheet.py', '{net}', 'sch', 'U7']]

def golden(d, pcb, net):
    """Record GOLDEN outputs into d, or diff against what d already holds."""
    import difflib
    rec = not os.path.isdir(d) or not os.listdir(d)
    os.makedirs(d, exist_ok=True)
    bad = 0
    for argv in GOLDEN:
        argv = [x.format(pcb=pcb, net=net) for x in argv]
        code, out = run(argv)
        out = f"{out}\nexit={code}\n"
        f = os.path.join(d, '_'.join(argv[0:1] + argv[2:]).replace('.py', '') + '.txt')
        if rec:
            open(f, 'w').write(out)
            continue
        old = open(f).read() if os.path.exists(f) else ''
        if old != out:
            bad += 1
            diff = list(difflib.unified_diff(old.splitlines(), out.splitlines(), 'golden', 'now', n=0, lineterm=''))
            print(f"CHANGED  {' '.join(argv)}  ({len(diff)} diff lines)")
            print('\n'.join('    ' + x for x in diff[2:14]))
        else:
            print(f"same     {' '.join(argv)}")
    print(f"\nrecorded {len(GOLDEN)} outputs in {d}" if rec else f"\n{len(GOLDEN) - bad}/{len(GOLDEN)} unchanged")
    return 1 if bad else 0

def run(argv, env=None):
    # fixture sheets are fake and golden inputs are frozen: never auto-regenerate
    e = {**os.environ, 'KREVIEW_NO_REGEN': '1', **(env or {})}
    p = subprocess.run([sys.executable, os.path.join(S, argv[0])] + argv[1:],
                       capture_output=True, text=True, env={k: v for k, v in e.items() if v})
    return p.returncode, p.stdout + p.stderr

def main():
    fails = 0
    tmp = tempfile.TemporaryDirectory()
    mini = mini_project(tmp.name)
    CASES.append(("knet revpol", ['knet.py', mini, 'revpol'], 0,
                  ["D2    SMF10A", "NO FUSE in this loop", "U2.1    IN           -4.2 V below its GND, via 10ohm (R1)"]))
    CASES.append(("knet pinout", ['knet.py', mini, 'check', '--only', 'PINOUT'], 2,
                  ["D1 BAV199 on Device:D_Dual_Series_ACK", "use Device:D_Dual_Series_AKC"]))
    CASES.append(("knet fppad+parpin", ['knet.py', mini, 'check', '--only', 'FPPAD,PARPIN'], 2,
                  ["U3.4 (VIN) has no pad of its own in QFN: fused into pad 3 (VIN)",
                   "U3 pad 5 (smd) of QFN has no symbol pin", "U3.6 (X) is on NX but t:QFN has no pad 6"]))
    CASES.append(("knet rfstub", ['knet.py', os.path.join(tmp.name, 'rf.net'), 'check', '--only', 'RFSTUB'], 0,
                  ["ANT [RF_50OHM] has 3 populated parts on it (J1, R1, U1)", "1 warn"]))
    # /Root/CS has one node, /Root/Sub/CS the rest: a local label on parent and child
    sp = os.path.join(tmp.name, 'split.net')
    open(sp, 'w').write(
        '(export (version "E") (design (source "s.kicad_sch")) (components'
        ' (comp (ref "U1") (value "X") (libsource (lib "x") (part "y")))'
        ' (comp (ref "J1") (value "C") (libsource (lib "x") (part "y")))) (libparts)'
        ' (nets (net (code "1") (name "/Root/CS") (node (ref "U1") (pin "1")))'
        ' (net (code "2") (name "/Root/Sub/CS") (node (ref "J1") (pin "1")) (node (ref "U1") (pin "2")))))')
    CASES.append(("knet solo split label", ['knet.py', sp, 'check', '--only', 'SOLO'], 2,
                  ["net /Root/CS has only U1.1 on it; /Root/Sub/CS (J1.1, U1.2) has the same name on another sheet"]))
    CASES.append(("knet i2c", ['knet.py', i2c_net(tmp.name), 'i2c'], 2,
                  ["=== SDA  /  SCL", "pull-ups : R3 2.2k (SDA) to +3V3",
                   "target   : U2    TCAL9539             0x75        A0: R1 10k to +3V3; A1: GND",
                   "controller: U1 MCU (IO1)", "ERROR address 0x75 used by U2 U3"]))
    CASES.append(("knet powertree", ['knet.py', power_net(tmp.name), 'powertree'], 0,
                  ["cells BT1: N_BT", "-> F1 1A -> VSYS  (also fed from VBUS via R5)",
                   "-> U1 BUCK1  [FB 402k/174k: Vout = 3.310 V] -> L1 2.2u -> +3V3  3.3 V",
                   "-> U3 TPS25947  [ILIM 5.40/6.07/6.60 A (R4 549)] -> 3V3_OUT  3.3 V  loads U2",
                   "J1 VBUS: VBUS  5 V"]))
    CASES.append(("knet rails powertree", ['knet.py', os.path.join(tmp.name, 'pwr', 'pwr.net'), 'rails'], 0,
                  ["=== VSYS   ? V", "source : F1 [1A] from N_BT  (powertree)"]))
    fet = os.path.join(tmp.name, 'fet.net')
    CASES.append(("knet revpol fet", ['knet.py', fet, 'revpol'], 0,
                  ["FET channel(s) on, gate driven from the cells: Q1", "FET channels vs normal: Q1 off",
                   "isolated from the cells: U1", "bidirectional TVS/ESD, breakdown not in the part number: D1"]))
    CASES.append(("kpcb tidy", ['kpcb.py', tidy_board(tmp.name), 'tidy'], 0,
                  ["5 tidy suggestion(s)", "C3     9.000,10.080 -> 9.000,10.000   y to the row of C1 C2 (0.080 off)",
                   "R2     7.100,15.000 -> 7.000,15.000   even 2.000 pitch along x between R1 and R3",
                   "H4     36.900,27.000 -> 37.000,27.000   x inset 3.100 -> 3.000 mm like H1 H2 H3",
                   "TP1    15.000,15.000 rot 60 -> 15.000,15.000 rot 90",
                   "D1     28.000,10.300 -> 28.000,10.000   0.300 off U1's long centre line y=10.000",
                   "movecheck: 2 new hit(s): D1.1 0.15 mm from a X track", "4 of the shown suggestions are clear"]))
    CASES.append(("kpcb freebox", ['kpcb.py', freebox_board(tmp.name), 'freebox', 'f', '6', '4'], 0,
                  ["1 region(s)", "centre   24.12,10.38    margin 1.75 mm  (region   52.0 mm2"]))
    mv = move_board(tmp.name)
    CASES.append(("kpcb movecheck hit", ['kpcb.py', mv, 'movecheck', 'R1', '11', '10'], 2,
                  ["NEW   R1.2 0.15 mm from a C track on F.Cu @12.00,10.00, needs 0.20", "1 own track(s) drag"]))
    CASES.append(("kpcb movecheck clear", ['kpcb.py', mv, 'movecheck', 'R1', '10.5', '10'], 0, ["CLEAR: nothing new"]))
    CASES.append(("kpcb movecheck scan", ['kpcb.py', mv, 'movecheck', 'R1', '--scan', 'y=10', 'x=9..12', '--step', '0.25'], 0,
                  ["x    9.000 ..   10.750  clear", "x   11.000 ..   12.000  blocked by a C track"]))
    CASES.append(("kpcb movecheck via", ['kpcb.py', mv, 'movecheck', 'via', '8,14', '8,15'], 0,
                  ["a bare stitching via, free to move", "CLEAR: nothing new"]))
    CASES.append(("kpcb silk", ['kpcb.py', silk_board(tmp.name), 'silk'], 0,
                  ["HIDDEN under U1", "[footprint layer B.Cu, art on F]", "art crosses 1 F-side pad(s): R2.1",
                   "1 of 2 hidden under a same-side part body"]))
    CASES.append(("kpcb check EDGEREF", ['kpcb.py', edge_board(tmp.name), 'check', '--only', 'EDGEREF'], 0,
                  ["J1's PCB Edge mark (Dwgs.User) is 0.50 mm inside the outline",
                   "move it +0.00,-0.50 mm (nearest pad then 0.50 mm from the edge, edge_clearance 0.3)",
                   "J2 side-entry, mates +y: Fab housing front 0.50 mm inside the edge; a pad is already "
                   "0.10 mm inside the 0.3 mm edge_clearance"]))
    CASES.append(("kpcb amp-pad-neck", ['kpcb.py', neck_board(tmp.name), 'ampacity', 'N', '--amps', '1.0'], 0,
                  ["PAD-NECK", "stub landing right on U1.1"]))
    CASES.append(("kpcb amp-stub", ['kpcb.py', stub_board(tmp.name), 'ampacity', 'N', '--amps', '2'], 0,
                  ["meshed, no series bottleneck; 3 part(s) hang on pad stubs",
                   "STUB-CHECK: 1 pad stub(s) under 2.00 A, each the only copper to one part: U1.1 0.20 mm"]))
    dp = dup_board(tmp.name)
    CASES.append(("kpcb sync art", ['kpcb.py', dp, 'sync', os.path.join(tmp.name, 'dup.net')], 0,
                  ["IN SYNC", "[INFO] SYNCART", "(3): REF** REF**~dup2 REF**~dup3"]))
    sp = scale_board(tmp.name)
    CASES.append(("kpcb where scale+origin", ['kpcb.py', sp, 'where', 'S1', '--origin', 'grid'], 0,
                  ["relative to the grid origin 5,5", "at        : 5.000, 5.000  rot 0  layer F.Cu  scale 0.5 x 0.5",
                   "courtyard : 3.00,4.00 .. 7.00,6.00  (4.00 x 2.00 mm)", "fab body  : 3.50,4.50 .. 6.50,5.50"]))
    CASES.append(("kpcb where xy origin", ['kpcb.py', sp, 'where', '8,7', '--origin', 'aux', '-r', '0'], 0,
                  ["=== 8.00,7.00   inside the outline", "within 0 mm (1)"]))
    CASES.append(("kpcb summary origins", ['kpcb.py', sp, 'summary'], 0, ["origins: grid 5,5  aux 2,3"]))
    CASES.append(("knet draw also-line", ['knet.py', also_net(tmp.name), 'draw', 'U1', '--spec'], 0,
                  ['note 6.4,0.6 IRQ\nnote 6.4,0.05 "+ U2 U3"']))
    CASES.append(("knet draw xref", ['knet.py', xref_net(tmp.name), 'draw', 'U1', '--spec'], 0,
                  ['note -12.69,4.18 "to U1.3"\nwire R1.2 -10.5,4',
                   'note -11.54,10.18 "to R1.2 R2.1 (drawn above)"\nwire U1.3 -4.5,10']))
    mn = multi_net(tmp.name)
    CASES.append(("knet draw multi-pin spec", ['knet.py', mn, 'draw', 'U1', '--spec'], 0,
                  ["d2s D1 ", "rsense R1 ", "label MID D1.3", "gnd R1.4", "wire U1.3 D2.1"]))
    CASES.append(("knet draw multi-pin verify", ['knet.py', mn, 'draw', 'U1', '-o', os.path.join(TMP, 'selftest_multi.svg')],
                  0, ["wrote"]))
    CASES.append(("knet pinout power-FET", ['knet.py', fet_pinout_project(os.path.join(tmp.name, 'qfet')),
                                            'check', '--only', 'PINOUT'], 2,
                  ["Q1 CSD18512Q5B on Transistor_FET:Q_NMOS_DGS: SON numbers its pads by function",
                   "pins 1-3 must be SGD; as drawn they are DGS and the FET mounts with D and S swapped",
                   "use Transistor_FET:Q_NMOS_SGD"]))
    lnet = os.path.join(tmp.name, 'lint', 'lint.net')
    wide = os.path.join(tmp.name, 'wide.ksch')
    open(wide, 'w').write('r R1 0,0 v 1k\nr R2 120,0 v 1k\nwire R1.2 R2.2\n')
    CASES.append(("ksch --paper", ['ksch.py', 'check', '--paper', 'A4', wide], 0,
                  ["over A4's usable 277 x 190 mm"]))
    l2net, pages = lint2_project(os.path.join(tmp.name, 'lint2'))
    fn = front_project(os.path.join(tmp.name, 'front'))
    CASES.append(("kfront check", ['kfront.py', fn, 'check'], 2,
                  ["0x6A under \"BQ25798 (U8)\" but `knet i2c` gives 0x6B for U8",
                   "\"TPS25751 (U8)\" but U8 is BQ25798", "U99 is not in the netlist", "J2 is not in the netlist",
                   "4 error(s)"]))
    CASES.append(("kfront pages", ['kfront.py', fn, 'pages'], 2, ["row 2: page 2 links to #3"]))
    kp = kproj_project(os.path.join(tmp.name, 'kproj'))
    for lab, args, ex, want in (
            ("kproj renumber", ['renumber'], 0, ["a.kicad_sch: 1 page 2 -> 1 (A)", "page 3 -> 2 (C in a.kicad_sch)",
                                                "b.kicad_sch: 1 page 1 -> 3 (B)"]),
            ("kproj rename", ['rename', 'c.kicad_sch', 'd.kicad_sch'], 0,
             ["a.kicad_sch: 1 Sheetfile c.kicad_sch -> d.kicad_sch", "t.kicad_pcb: 2 (sheetfile) c.kicad_sch -> d.kicad_sch"]),
            ("kproj order", ['order', 'B'], 0, ["navigator order B, A"]),
            ("kproj add-sheet", ['add-sheet', 'n.kicad_sch', 'N', '--after', 'B'], 0,
             ["n.kicad_sch: created", "at position 2", "n.kicad_sch: 1 page 0 -> 2 (N)"]),
            ("kproj refuses on a lock", ['renumber'], 3, ["KiCad holds the project"])):
        if lab.endswith('lock'):         # a second copy, so the sequence above still runs
            kp = kproj_project(os.path.join(tmp.name, 'kproj2'))
            open(os.path.join(kp, '~a.kicad_sch.lck'), 'w').close()
        else:                            # a KiCad open on any project must not fail the edits
            args = args + ['--force']
        CASES.append((lab, ['kproj.py', kp] + args, ex, want))
    CASES.append(("ksheet lint shunt+text", ['ksheet.py', l2net, 'lint', '--only', 'SHUNTBUS,TEXTOVER'], 2,
                  ["C1 C2: horizontal 2-pin shunts into one vertical GND wire at x=50 with no GND symbol within any distance",
                   "C1 and C2 meet the vertical GND wire at 50,50 from opposite sides",
                   "C2 value '1u' overlaps R9 reference 'R9'"]))
    CASES.append(("ksheet lint", ['ksheet.py', lnet, 'lint'], 2,
                  ["R1's body", "/NA ends at 70,40 and /NB starts 2.54 mm", "U1.1 (A) at 94.92,94.92: /J runs 1.27 mm",
                   "U1: 1 object(s) within 2.54 mm of its pin ends: R3.2 (1.27 mm from pin 2)"]))
    CASES.append(("ksheet sch", ['ksheet.py', lnet, 'sch', 'U1', '-r', '2'], 0,
                  ["1     A            L     94.92,94.92       /J", "body 97.46,93.65 - 102.54,108.89",
                   "R3         at 89.84,97.46  1 unconnected, 2 /L"]))
    CASES.append(("knet nc-stub+stale", ['knet.py', mini, 'around', 'U1'], 0,
                  ["NC (flagged)", "floating  <-- no NC flag", "1.0 h older than t.kicad_sch",
                   "lacks top-level sheet(s) Other"]))
    CASES.append(("kpcb zones --voids", ['kpcb.py', os.path.join(tmp.name, 'voids.kicad_pcb'), 'zones', '--voids'], 0,
                  ["B.Cu    void core", "rescue via at", "GND on F.Cu there, nothing foreign"]))
    for label, argv, want_exit, needles in CASES:
        code, out = run(argv)
        prob = []
        if code != want_exit:
            prob.append(f"exit {code} != {want_exit}")
        prob += [f"missing {n!r}" for n in needles if n not in out]
        if label in ("ksch render", "knet draw multi-pin verify") and ("ERROR" in out or "overlaps" in out):
            prob.append("ERROR line in render output")
        if label == "kpcb amp-tap" and "near TH2" in out:
            prob.append("thermistor tap TH2 leaked into the bottleneck (tap exclusion broke)")
        if label == "kpcb zones --voids" and 'rescue via at' in out:
            vx = float(out.split('rescue via at ')[1].split(',')[0])
            if abs(vx - 12) < 0.55 or not 9.5 < vx < 14.5:
                prob.append(f"rescue via x={vx} is on the SIG track or outside the void")
        if label == "knet fppad+parpin" and "pad MP" in out:
            prob.append("MP mounting pad reported (it is meant to have no net)")
        if prob:
            fails += 1
            print(f"FAIL  {label}: {'; '.join(prob)}")
        else:
            print(f"ok    {label}")
    # a sheet saved after the .net triggers kmerge; one that fails keeps the old
    # netlist and says so, next to the stale warning
    code, out = run(['knet.py', mini, 'summary'], {'KREVIEW_NO_REGEN': '', 'KICAD_CLI': 'false'})
    ok5 = 'auto-regenerate of t.net failed' in out and '1.0 h older than t.kicad_sch' in out
    fails += not ok5
    print(f"{'ok  ' if ok5 else 'FAIL'}  stale netlist auto-regenerate (failure path)")
    # end-sunk rod bound: 3 A in 0.3 x 0.035 x 1.5 mm = I^2*rho*L^2/(8*k*A^2) ~ 1.0 C
    sys.path.insert(0, S)
    import kpcb_amp
    r = kpcb_amp.dt_rod(3.0, 0.3, 0.035, 1.5)
    ok6 = 0.95 < r < 1.1 and kpcb_amp.dt_ipc(kpcb_amp.ipc_current(0.3, 0.035, True, 10), 0.3, 0.035, True) - 10 < 1e-6
    fails += not ok6
    print(f"{'ok  ' if ok6 else 'FAIL'}  ampacity rise model: rod {r:.2f} C, IPC inverse round-trip")
    # cache_prune: past the cap the oldest entries go, and a plot directory goes whole
    import kcommon, kmerge
    pd, keep = os.path.join(tmp.name, 'cache'), kcommon.CACHE
    os.makedirs(os.path.join(pd, 'svg', 'old'))
    for i, f in enumerate(('svg/old/a.svg', 'svg/old/b.svg', 'old.bin', 'new.bin')):
        open(os.path.join(pd, f), 'wb').write(b'x' * 100)
        os.utime(os.path.join(pd, f), (i, i))
    os.utime(os.path.join(pd, 'svg', 'old'), (1, 1))
    kcommon.CACHE = pd
    kcommon.cache_prune(250)
    kcommon.CACHE = keep
    ok8 = sorted(os.listdir(pd)) == ['new.bin', 'old.bin', 'svg'] and not os.listdir(os.path.join(pd, 'svg'))
    # kmerge's raw round trip: an escaped string survives parse -> dump byte-exact
    tree = kcommon.parse_sexp('(design (source "C:\\\\x \\"q\\"") (n 1))', mark='\0')
    ok8 &= kcommon.parse_sexp(kmerge.dump(tree), mark='\0') == tree and \
        kmerge.val(tree, 'source') == 'C:\\x "q"' and kmerge.q('C:\\x "q"') == tree[1][1]
    fails += not ok8
    print(f"{'ok  ' if ok8 else 'FAIL'}  cache_prune drops oldest/whole dirs; kmerge escaped round trip")
    # SLOT: the voids board's SIG track crosses a 10 x 10 box on the F.Cu GND plane
    import kpcb_zones, kpcb_board
    vb = kpcb_board.Board(os.path.join(tmp.name, 'voids.kicad_pcb'))
    sl = kpcb_zones._slots(vb, 'F.Cu', 'GND', (5, 5, 15, 15))
    ok7 = len(sl) == 1 and sl[0][0] == 'SIG' and abs(sl[0][1][0] - 10) < 1e-6 and sl[0][1][2] == 'x=12.0' \
        and not kpcb_zones._slots(vb, 'F.Cu', 'GND', (0, 0, 20, 20), min_len=19)
    fails += not ok7
    print(f"{'ok  ' if ok7 else 'FAIL'}  zones SLOT measure {sl}")
    # rf's microstrip Zo: 0.36 mm on 0.203 mm / er 4.4 / 35 um is ~49.7 ohm (hand-computed)
    sys.path.insert(0, S)
    import kzo, kpcb_height
    z = kzo.microstrip(0.36, 0.203, 4.4, 0.035)[0]
    ok = 49.0 < z < 51.0
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  microstrip Zo {z:.1f} ohm")
    # rf's CPWG field solver vs exact stripline / CPW and Hammerstad microstrip,
    # and side grounds can only LOWER Zo (the closed forms got that backwards)
    zs = kzo._selftest()
    ms = kzo.field_zo(0.306, 0.203, 4.4, 0.035)[0]
    cp = [kzo.field_zo(0.306, 0.203, 4.4, 0.035, s=g)[0] for g in (0.15, 0.3, 1.0)]
    ok4 = all(abs(z - r) / r < 0.02 for _, z, r in zs) and cp[0] < cp[1] < cp[2] < ms
    fails += not ok4
    print(f"{'ok  ' if ok4 else 'FAIL'}  field solver " + ', '.join(f"{(z - r) / r * 100:+.1f}%" for _, z, r in zs)
          + f"; CPWG {cp[0]:.1f} < {cp[1]:.1f} < {cp[2]:.1f} < microstrip {ms:.1f}")
    # `height`'s glTF transform: 90 deg about Y (x,y,z,w quaternion) then +2 in Y
    M = kpcb_height._mat({'rotation': [0, 0.7071068, 0, 0.7071068], 'translation': [0, 2, 0]})
    p = [sum(M[r][c] * v for c, v in enumerate((1, 0, 0, 1))) for r in range(3)]
    ok2 = all(abs(x - y) < 1e-6 for x, y in zip(p, (0, 2, -1)))
    fails += not ok2
    print(f"{'ok  ' if ok2 else 'FAIL'}  glTF node transform {[round(x, 3) for x in p]}")
    # kdoc: a plain pattern crosses separator/dash variants (but a leading space still
    # anchors), and a filename or path addresses its doc
    import kdoc, argparse
    ok3 = bool(kdoc.smart_re('keep-out').search('antenna keepout zone')
               and kdoc.smart_re('-40').search('−40 C')
               and not kdoc.smart_re(' EN').search('ENABLE')
               and kdoc._fkey('docs/datasheets/max17320.pdf') == kdoc._fkey('max17320'))
    fails += not ok3
    print(f"{'ok  ' if ok3 else 'FAIL'}  kdoc pattern folding + -d path")
    # kdoc: two files sharing a basename get two cache slots (the second used to
    # repoint -d at itself and win), and -p takes lists, ranges and pasted labels
    import io, contextlib
    kdoc.CACHE = os.path.join(tmp.name, 'kdoc')
    for sub in 'ab':
        os.makedirs(os.path.join(tmp.name, 'kd' + sub))
        open(os.path.join(tmp.name, 'kd' + sub, 'X1.txt'), 'w').write(sub * 9)
    with contextlib.redirect_stderr(io.StringIO()):
        ka, kb = (kdoc.extract(os.path.join(tmp.name, 'kd' + sub, 'X1.txt')) for sub in 'ab')
    ok14 = ka != kb and kdoc.docs('X1.txt') == ['X1_txt'] and kb.endswith('X1_txt_kdb') \
        and kdoc._pageset('2,5-7,p9') == {2, 5, 6, 7, 9}
    fails += not ok14
    print(f"{'ok  ' if ok14 else 'FAIL'}  kdoc same-basename slots {os.path.basename(ka)}, {os.path.basename(kb)}; -p sets")
    # kcommon.parse_args: dash + uppercase tokens that are not options are values;
    # real options, attached short values and negative numbers are untouched.
    # kicad_running sees a KiCad lock file in the project dir.
    import kcommon
    pa = argparse.ArgumentParser(add_help=False)
    pa.add_argument('args', nargs='*'); pa.add_argument('--net', default=''); pa.add_argument('-d', default='')
    pn = kcommon.parse_args(pa, ['net', '-BATT', '--net', '-VIN', '-dX', '-40'])
    open(os.path.join(tmp.name, '~t.kicad_sch.lck'), 'w').close()
    ok15 = pn.args == ['net', '-BATT', '-40'] and pn.net == '-VIN' and pn.d == 'X' \
        and any('lock file' in r for r in kcommon.kicad_running(tmp.name))
    fails += not ok15
    print(f"{'ok  ' if ok15 else 'FAIL'}  parse_args dash values {pn.args} --net {pn.net}; lock guard")
    # ksheet page order: depth-first reads A(1) C(3) B(2); the navigator lists B first.
    # _xf rotates before it mirrors: at rot 270 + mirror x, lib +x points sheet up
    import ksheet
    po = ksheet.page_order(pages)[0]
    ok16 = [x['rule'] for x in po] == ['PAGEORDER', 'NAVORDER'] and '1 /A/, 3 /A/C/, 2 /B/' in po[0]['msg'] \
        and kcommon._xf(1, 0, 270, 'x') == (0, -1) and kcommon._xf(1, 0, 90, '') == (0, -1)
    fails += not ok16
    print(f"{'ok  ' if ok16 else 'FAIL'}  ksheet PAGEORDER/NAVORDER {[x['rule'] for x in po]}; _xf rotate-then-mirror")
    # duplicate refs: every footprint block becomes one Board entry, and a padless
    # logo's side is its art layer, not its (layer)
    db = kpcb_board.Board(dp)
    ok8 = len(db.fps) == open(dp).read().count('(footprint ') == 4 \
        and {'REF**', 'REF**~dup2', 'REF**~dup3'} <= set(db.fps) and not db.fps['REF**~dup3'].back
    fails += not ok8
    print(f"{'ok  ' if ok8 else 'FAIL'}  Board keeps duplicate refs {sorted(db.fps)}")
    # `height` with no models loaded: R1 is UNKNOWN, the padless logos are not parts
    kpcb_height.heights = lambda b: ({}, [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kpcb_height.c_height(db, argparse.Namespace(args=[], height_cfg={}, max=8))
    nm = [x for x in buf.getvalue().splitlines() if 'NO 3D MODEL' in x]
    ok9 = len(nm) == 1 and nm[0].endswith(': R1')
    fails += not ok9
    print(f"{'ok  ' if ok9 else 'FAIL'}  height skips padless art: {nm}")
    # a horizontal C's value and the ref of an NTC one 3-unit row below must not
    # touch (glyphs: 0.35 above the baseline, 0.075 below), and ksch says so
    import ksch
    kd = ksch.Doc({})
    kd.parse('c C1 0,0 h 10nF\nntc T1 0,3 hr 10k\nnote 0,6 "a long note running right"\nnote 4,6 x')
    cv = [f[2] for f in kd.syms['C1'].fields() if f[-1] == 'val'][0]
    tr = [f[2] for f in kd.syms['T1'].fields() if f[-1] == 'ref'][0]
    ow = [m for _sv, m in ksch.check_doc(kd) if 'overlaps' in m]
    ok11 = tr - .35 > cv + .075 and len(ow) == 1 and "'x'" in ow[0]
    fails += not ok11
    print(f"{'ok  ' if ok11 else 'FAIL'}  ksch row-pitch text: C1 value {cv:.2f}, T1 ref {tr:.2f}; {ow}")
    # an ic's top pin name sits inside its body, clear of the first left pin's name
    pr, _pn, bb = ksch.s_ic({'L': [('2', 'ENABLE')], 'T': [('1', 'VCC_RF')], 'B': [('3', 'GND')]})
    tt = [q for q in pr if q[0] == 't']
    tn = next(q for q in tt if q[3] == 'VCC_RF')
    w = ksch.twid('VCC_RF', tn[4])
    t0, t1 = (tn[2], tn[2] + w) if tn[5] == 'end' else (tn[2] - w, tn[2])
    ln = next(q for q in tt if q[3] == 'ENABLE')
    bn = next(q for q in tt if q[3] == 'GND')
    ok12 = 0 < t0 and t1 < ln[2] - .35 and bn[2] < bb[3] and bn[2] - ksch.twid('GND', bn[4]) > ln[2]
    fails += not ok12
    print(f"{'ok  ' if ok12 else 'FAIL'}  ksch ic top/bottom pin names inside the body: VCC_RF y {t0:.2f}..{t1:.2f}, "
          f"ENABLE baseline {ln[2]:.2f}, body h {bb[3]:.2f}")
    # ksch papercuts: KiCad's junction rule (a wire through a pin end gets a dot, two
    # wires up one stub from a pin do not), an ic with bottom pins keeps its value
    # above the body, a TVS2200-style part maps onto a diode (GND = anode), and a
    # note laid over a wire is reported
    j = (ksch.junctions([((0, 0), (4, 0))], [(2, 0)]), ksch.junctions([((0, 0), (0, -1)), ((0, -1), (0, 0))], [(0, 0)]))
    sy = ksch.Sym('ic', 'U9', 0, 0, sides={'L': [('1', 'IN')], 'B': [('2', 'GND')]}, value='X')
    vy = [f[2] for f in sy.fields() if f[-1] == 'val']
    bn = ksch.by_name(ksch.s_d('dz')[1], {'1': ('IN', 'p'), '2': ('GND', 'p'), '3': ('IN', 'p'), '7': ('EP', 'p')})
    kx = ksch.Doc({})
    kx.parse('r R1 0,0 h 1k\nr R2 6,0 h 1k\nwire R1.2 R2.1\nnote 2.5,0.2 "over the wire"')
    cr = [m for _s, m in ksch.check_doc(kx) if 'running through' in m]
    ok17 = j == ([(2, 0)], []) and vy and vy[0] < sy.bbox()[1] and bn \
        and bn[1] == {'1': 'k', '2': 'a', '3': 'k', '7': 'a'} and len(cr) == 1 and 'over the wire' in cr[0]
    fails += not ok17
    print(f"{'ok  ' if ok17 else 'FAIL'}  ksch junction rule {j}, ic value above body, TVS pin map, text-over-wire")
    # ksch 1:1 geometry: a lib symbol (10.16 mm body, pin 1 left, 2 right, 3+4 stacked
    # at the bottom) lands on grid units; a Device:R-length span re-spaces a 2-pin part
    sys.path.insert(0, S)
    import kcommon as kc
    lib = kc.parse_sexp('(symbol "x:U" (symbol "U_0_1" (rectangle (start -5.08 5.08) (end 5.08 -5.08)))'
                        ' (symbol "U_1_1" (pin input line (at -7.62 2.54 0) (length 2.54) (name "IN") (number "1"))'
                        ' (pin output line (at 7.62 0 180) (length 2.54) (name "OUT") (number "2"))'
                        ' (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "3"))'
                        ' (pin power_in line (at 0 -7.62 90) (length 2.54) (name "GND") (number "4"))))')
    _pr, lp, lbb, lnm = ksch.s_lib(lib, 1)
    spr = ksch.stretch(*ksch.s_r(), 3)[1]
    ok18 = lp == {'1': (-1.0, 1.0, 'L'), '2': (5.0, 2.0, 'R'), '3': (2.0, 5.0, 'D'), '4': (2.0, 5.0, 'D')} \
        and lbb == (0.0, 0.0, 4.0, 4.0) and lnm['2'] == 'OUT' and spr == {'1': (0, 0, 'U'), '2': (0, 3, 'D')}
    fails += not ok18
    print(f"{'ok  ' if ok18 else 'FAIL'}  ksch lib-symbol geometry 1:1 {lbb}, stretched R span {spr['2'][1]}")
    # kverify's sheet summary: a uniform GUI move reads as one offset, renumbering apart
    import kverify
    va = '(sheet_instances (path "/" (page "4"))) (at 10 20) (xy 1 2) (start 0 0)'
    vb = '(sheet_instances (path "/" (page "6"))) (at 7.46 20) (xy -1.54 2) (start -2.54 0)'
    vs = kverify.delta_summary(va, vb)
    ok19 = vs == 'page 4 -> 6; 3 coordinates moved, all by (-2.54,+0)' \
        and kverify.delta_summary(va, va.replace('(at', '(foo (at')).startswith('structure changed')
    fails += not ok19
    print(f"{'ok  ' if ok19 else 'FAIL'}  kverify sheet delta: {vs}")
    # `view`: the crop is a viewBox rewrite in board mm; the real plot needs kicad-cli
    import kpcb_view, shutil
    vbox = kpcb_view.crop('<svg width="297mm" height="210mm" viewBox="0 0 297 210">', (10, 20, 30, 50))
    vpng = os.path.join(TMP, 'selftest_view.png')
    if shutil.which('kicad-cli') or shutil.which('kicad-cli-nightly'):
        vc, vo = run(['kpcb.py', PCB, 'view', 'U1', '-r', '1', '-o', vpng])
        vreal = vc == 0 and '6.8,6.8 - 13.2,13.2 mm' in vo and os.path.getsize(vpng) > 1000
    else:
        vreal, vo = True, 'kicad-cli not installed, plot skipped'
    ok13 = vbox == '<svg width="20mm" height="30mm" viewBox="10 20 20 30">' and vreal
    fails += not ok13
    print(f"{'ok  ' if ok13 else 'FAIL'}  kpcb view crop + plot: {vo.strip()[-70:]}")
    # the copper graph must not depend on the hash seed (set order)
    tp = tee_board(tmp.name)
    rs = {run(['kpcb.py', tp, 'ampacity', '--from', 'J1.1', '--to', 'J2.1', '--amps', '1'],
              {'PYTHONHASHSEED': str(sd)})[1].split('   R ')[1].split(' mohm')[0] for sd in range(8)}
    ok10 = rs == {'8.27'}
    fails += not ok10
    print(f"{'ok  ' if ok10 else 'FAIL'}  copper graph independent of hash seed: R {sorted(rs)} mohm")
    print(f"\n{len(CASES) + 19 - fails}/{len(CASES) + 19} passed")
    return 1 if fails else 0

if __name__ == '__main__':
    if sys.argv[1:2] == ['--golden'] and len(sys.argv) == 5:
        sys.exit(golden(*sys.argv[2:5]))
    sys.exit(main())
