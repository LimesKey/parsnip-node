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
[substrings that must appear in output]). Anything else is one line in main():
`fails += check(ok, "label")` - counted for the total automatically.

Refactor safety net on a REAL board (fixtures only prove the rules fire):

    selftest.py --golden DIR BOARD.kicad_pcb BOARD.net

The first run records GOLDEN (read-only commands, no kicad-cli) into DIR; every
later run diffs against it and exits 1 on any change. Record before a refactor,
check after: a pure refactor must be byte-identical. Delete DIR to re-record.
"""
import math, os, re, sys, glob, json, subprocess, tempfile

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
    ("kdrc selftest", ['kdrc.py', '--selftest'],                0, ["14/14 passed"]),
    ("kpcb check",   ['kpcb.py', PCB, 'check'],                 2, ["4 error, 10 warn, 3 info"]),
    ("kpcb summary", ['kpcb.py', PCB, 'summary'],               0, ["40.00 x 30.00 mm", "placed 16"]),
    # nightly writes (transform (translate X Y) (rotate R)); read as (at) it parks all at 0,0
    ("kpcb nightly", ['kpcb.py', PCB3, 'summary'],              0, ["pcbnew 10.99", "placed 16"]),
    ("kpcb nightly check", ['kpcb.py', PCB3, 'check'],          2, ["4 error, 10 warn, 3 info"]),
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
     ["1 via drill(s) reach into 1 SMD pad mask opening(s)", "U1.1", "drill wholly in", "OK same net (+3V3)",
      "via protection (Board Setup): none (open vias)"]),
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
        + comp('R2', '10') + comp('R3', '10') + comp('J2', 'SMA') + comp('FL1', 'LPF', part='Filter')
        + comp('JP1', 'BYPASS', part='Jumper')[:-1] + ' (dnp))' + comp('U2', 'PA', 'x', 'ic') + ')'
        ' (libparts (libpart (lib "x") (part "ic") (pins (pin (num "1") (name "IN") (type "input"))'
        ' (pin (num "2") (name "GND") (type "power_in")))))'
        ' (nets' + net(1, 'ANT', 'RF_50OHM', ('J1', '1'), ('R1', '1'), ('C1', '1'), ('L1', '1'), ('U1', '1'))
        + net(2, 'GND', 'Default', ('C1', '2'), ('C2', '2'), ('U1', '2'), ('J1', '2'))
        + net(3, 'VB', 'Default', ('L1', '2'), ('C2', '1'))
        + net(4, 'VCC_RF', 'Default', ('R1', '2'), ('R2', '1'), ('R3', '1'))
        + net(5, 'N2', 'Default', ('R2', '2')) + net(6, 'N3', 'Default', ('R3', '2'))
        + net(7, 'RFA', 'RF_50OHM', ('J2', '1'), ('FL1', '1'), ('JP1', '1'))
        + net(8, 'RFB', 'RF_50OHM', ('FL1', '2'), ('JP1', '2'), ('U2', '1')) + '))')
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

def fab_board(d):
    """FABFLAGS, against a t.pretty library: C2 DNP and excluded from the BOM, SW1 an LCSC
    part excluded from the BOM, TP1 in the CPL where its library footprint is not. C1 (DNP,
    in the BOM), FID1 (excluded from the BOM like its library footprint, in the CPL) and the
    board_only logo are fine."""
    d = os.path.join(d, 'fab'); os.makedirs(os.path.join(d, 't.pretty'), exist_ok=True)
    open(os.path.join(d, 'fp-lib-table'), 'w').write(
        '(fp_lib_table (lib (name "t") (type "KiCad") (uri "${KIPRJMOD}/t.pretty")))')
    for n, attr in (('TP', 'exclude_from_pos_files exclude_from_bom'), ('FID', 'smd exclude_from_bom')):
        open(os.path.join(d, 't.pretty', n + '.kicad_mod'), 'w').write(f'(footprint "{n}" (attr {attr}))')
    p = os.path.join(d, 'fab.kicad_pcb')
    fp = lambda ref, x, attr, lcsc='', lib='': (
        f' (footprint "{lib or "x:" + ref}" (layer "F.Cu") (at {x} 5) (property "Reference" "{ref}")'
        + (f' (property "LCSC Part" "{lcsc}")' if lcsc else '') + f' (attr {attr})'
        f' (fp_rect (start -.5 -.3) (end .5 .3) (layer "F.CrtYd"))'
        f' (pad "1" smd rect (at 0 0) (size .3 .3) (layers "F.Cu")))')
    open(p, 'w').write('(kicad_pcb (version 20240108) (generator "pcbnew")'
                       ' (layers (0 "F.Cu" signal) (25 "Edge.Cuts" user)) (gr_rect (start 0 0) (end 30 10) (layer "Edge.Cuts"))'
                       + fp('C1', 3, 'smd dnp', 'C1525') + fp('C2', 6, 'smd exclude_from_bom dnp', 'C1525')
                       + fp('SW1', 9, 'smd exclude_from_bom', 'C202388') + fp('TP1', 12, 'exclude_from_bom', lib='t:TP')
                       + fp('FID1', 18, 'smd exclude_from_bom', lib='t:FID')
                       + fp('G1', 15, 'board_only exclude_from_pos_files exclude_from_bom') + ')')
    return p

def crtyd_board(d):
    """CRTYDPAD: J1's courtyard wraps its housing (y -3..-1) but not its pads (y 0, 2).
    HOLECLR: H1 (3.2 mm hole, keepout 3.1 at --hole 1.5) - R1's courtyard corner reaches
    2.3 mm from the centre on the diagonal but its pads stay 3.25 mm out (clear); R2's pad
    sits 2.3 mm out (inside)."""
    p = os.path.join(d, 'crtyd.kicad_pcb')
    def fp(ref, x, y, crt, pads, name='', size=.4, drill=''):
        return (f' (footprint "{name or "x:" + ref}" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") (attr smd)'
                f' (fp_rect (start {crt[0]} {crt[1]}) (end {crt[2]} {crt[3]}) (layer "F.CrtYd"))'
                + ''.join(f' (pad "{n}" {"thru_hole" if drill else "smd"} circle (at {px} {py}) (size {size} {size})'
                          f'{drill} (layers "F.Cu"))' for n, px, py in pads) + ')')
    open(p, 'w').write('(kicad_pcb (version 20240108) (generator "pcbnew")'
                       ' (layers (0 "F.Cu" signal) (25 "Edge.Cuts" user)) (gr_rect (start 0 0) (end 40 30) (layer "Edge.Cuts"))'
                       + fp('J1', 30, 15, (-1, -3, 1, -1), (('1', 0, 0), ('2', 0, 2)))
                       + fp('H1', 10, 10, (-1.7, -1.7, 1.7, 1.7), (('1', 0, 0),), 'MountingHole_3.2mm', 3.2, ' (drill 3.2)')
                       + fp('R1', 12.1, 12.1, (-.5, -.5, 1, 1), (('1', .4, .4), ('2', .7, .7)))
                       + fp('R2', 10, 7.5, (-.5, -.5, .5, .5), (('1', 0, 0),)) + ')')
    return p

def rip_board(d, sealed=False):
    """30 x 20, route on F.Cu: a W wall along y=10 with one 0.9 mm gap at x=20. B (TP1/TP2
    at y=9.3, the shorter net, routed first) runs straight over the gap's mouth and seals
    it; A (TP3 above, TP4 below) has no other way through, so it must rip B, and B then
    goes round over TP3 - unless `sealed` walls that off too, and the rip is undone.
    D_P / D_N on B.Cu: a pair 6 / 7 mm long at a 0.15 mm edge gap."""
    p = os.path.join(d, 'ripsealed.kicad_pcb' if sealed else 'rip.kicad_pcb')
    fp = lambda ref, x, y, net: (f' (footprint "x:TP" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") (attr smd)'
                                 f' (fp_rect (start -.4 -.4) (end .4 .4) (layer "F.CrtYd"))'
                                 f' (pad "1" smd rect (at 0 0) (size .5 .5) (layers "F.Cu") (net "{net}")))')
    seg = lambda x0, y0, x1, y1, net='W', ly='F.Cu': (f' (segment (start {x0} {y0}) (end {x1} {y1}) (width 0.2)'
                                                      f' (layer "{ly}") (net "{net}") (uuid "{net}{x0}{y0}"))')
    open(p, 'w').write('(kicad_pcb (version 20240108) (generator "pcbnew")'
                       ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
                       ' (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts"))'
                       + fp('TP1', 16, 9.3, 'B') + fp('TP2', 24, 9.3, 'B') + fp('TP3', 20, 3, 'A') + fp('TP4', 20, 15, 'A')
                       + seg(0, 10, 19.55, 10) + seg(20.45, 10, 30, 10) + (seg(20, 0, 20, 2.6) if sealed else '')
                       + seg(2, 18, 8, 18, 'D_P', 'B.Cu') + seg(2, 18.35, 9, 18.35, 'D_N', 'B.Cu') + '\n)\n')
    return p

def pour_board(d):
    """20 x 10 in KiCad's tab layout: P pads at x=3 and x=17, a W wall at x=10 with a
    1.2 mm gap - no 1.0 mm track fits it (1.4 with clearance), a 0.3 mm probe does.
    KiCad's refill of the `--pour` copy joins P through the gap (checked by hand)."""
    p = os.path.join(d, 'pour.kicad_pcb')
    fp = lambda ref, x: (f'\t(footprint "x:TP"\n\t\t(layer "F.Cu")\n\t\t(uuid "f{ref}")\n\t\t(at {x} 5)\n'
                         f'\t\t(property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS") (effects (font (size 1 1))))\n'
                         f'\t\t(attr smd)\n\t\t(fp_rect (start -.8 -.8) (end .8 .8) (stroke (width 0.05) (type solid)) (layer "F.CrtYd"))\n'
                         f'\t\t(pad "1" smd rect (at 0 0) (size 1.2 1.2) (layers "F.Cu") (net "P"))\n\t)\n')
    seg = lambda y0, y1: (f'\t(segment\n\t\t(start 10 {y0})\n\t\t(end 10 {y1})\n\t\t(width 0.2)\n\t\t(layer "F.Cu")\n'
                          f'\t\t(net "W")\n\t\t(uuid "w{y0}")\n\t)\n')
    open(p, 'w').write('(kicad_pcb\n\t(version 20240108)\n\t(generator "pcbnew")\n\t(layers\n\t\t(0 "F.Cu" signal)\n'
                       '\t\t(31 "B.Cu" signal)\n\t\t(44 "Edge.Cuts" user)\n\t)\n'
                       '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 20 10)\n\t\t(layer "Edge.Cuts")\n\t)\n'
                       + fp('TP1', 3) + fp('TP2', 17) + seg(0, 4.4) + seg(5.6, 10) + ')\n')
    return p

def _tab_fp(ref, x, y, pads, crt=(.4, .4), rot=0):
    """a footprint in KiCad's tab layout (the writers edit by span); pads [(num, dx, dy, sx, sy, net)]"""
    import uuid as U
    u = lambda: f'(uuid "{U.uuid4()}")'                 # noqa: E731
    return (f'\t(footprint "x:P"\n\t\t(layer "F.Cu")\n\t\t{u()}\n\t\t(at {x} {y}' + (f' {rot}' if rot else '') + ')\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at 0 -1.5 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t{u()}\n'
            f'\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
            f'\t\t(fp_rect\n\t\t\t(start {-crt[0]} {-crt[1]})\n\t\t\t(end {crt[0]} {crt[1]})\n\t\t\t(stroke\n'
            f'\t\t\t\t(width 0.05)\n\t\t\t\t(type solid)\n\t\t\t)\n\t\t\t(layer "F.CrtYd")\n\t\t\t{u()}\n\t\t)\n'
            + ''.join(f'\t\t(pad "{n}" smd rect\n\t\t\t(at {dx} {dy})\n\t\t\t(size {sx} {sy})\n\t\t\t(layers "F.Cu")\n'
                      f'\t\t\t(net "{net}")\n\t\t\t{u()}\n\t\t)\n' for n, dx, dy, sx, sy, net in pads) + '\t)\n')


def _tab_board(d, name, w, h, body):
    p = os.path.join(d, name)
    open(p, 'w').write('(kicad_pcb\n\t(version 20260901)\n\t(generator "pcbnew")\n\t(generator_version "10.99")\n'
                       '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
                       f'\t(gr_rect\n\t\t(start 0 0)\n\t\t(end {w} {h})\n\t\t(layer "Edge.Cuts")\n\t)\n' + body + ')\n')
    return p


def _tseg(x0, y0, x1, y1, net, w=0.2, ly='F.Cu', uid=None):
    import uuid as U
    return (f'\t(segment\n\t\t(start {x0} {y0})\n\t\t(end {x1} {y1})\n\t\t(width {w})\n\t\t(layer "{ly}")\n'
            f'\t\t(net "{net}")\n\t\t(uuid "{uid or U.uuid4()}")\n\t)\n')


def _tvia(x, y, net):
    import uuid as U
    return (f'\t(via\n\t\t(at {x} {y})\n\t\t(size 0.6)\n\t\t(drill 0.3)\n\t\t(layers "F.Cu" "B.Cu")\n'
            f'\t\t(net "{net}")\n\t\t(uuid "{U.uuid4()}")\n\t)\n')


def power_board(d):
    """20 x 10: net P from TP1 (2,5) to TP2 (18,5) as a 1 mm F.Cu track, a 1 mm B.Cu track
    6..14 with vias at both ends; C1 decouples P at 10,5 with its GND pad at 10,6.2 (the
    band must leave room round it). For fatten, stitch and ampacity --fills."""
    return _tab_board(d, 'power.kicad_pcb', 20, 10,
                      _tab_fp('TP1', 2, 5, [('1', 0, 0, 1, 1, 'P')], (.6, .6))
                      + _tab_fp('TP2', 18, 5, [('1', 0, 0, 1, 1, 'P')], (.6, .6))
                      + _tab_fp('C1', 10, 5.6, [('1', 0, -0.6, .5, .5, 'P'), ('2', 0, 0.6, .5, .5, 'GND')], (.4, 1.0))
                      + _tseg(2, 5, 18, 5, 'P', 1.0) + _tseg(6, 5, 14, 5, 'P', 1.0, 'B.Cu')
                      + _tvia(6, 5, 'P') + _tvia(14, 5, 'P'))


def stair_zone_board(d):
    """power_board plus a P zone whose hypotenuse (8,8) -> (2,2) is a 0.05 mm raster
    staircase, the shape `fatten` traced before 2026-10-19, with a stale fill"""
    st = ''.join(f'(xy {8 - k * .05:g} {8 - (k + 1) * .05:g}) (xy {8 - (k + 1) * .05:g} {8 - (k + 1) * .05:g}) '
                 for k in range(120))
    return _tab_board(d, 'stair.kicad_pcb', 20, 10,
                      '\t(zone\n\t\t(net "P")\n\t\t(layer "F.Cu")\n\t\t(uuid "zstair")\n\t\t(name "stair")\n'
                      f'\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy 2 8) (xy 8 8) {st}\n\t\t\t)\n\t\t)\n'
                      '\t\t(filled_polygon\n\t\t\t(layer "F.Cu")\n\t\t\t(pts\n\t\t\t\t(xy 2 8) (xy 8 8) (xy 2 2)\n'
                      '\t\t\t)\n\t\t)\n\t)\n')

def neck_route_board(d):
    """16 x 10: P1 (net P) a 0.3 mm pad between X and Y pads at 0.5 mm pitch, P2 a big pad
    10 mm away: no 1 mm track can leave P1, a neck can"""
    return _tab_board(d, 'neckr.kicad_pcb', 16, 10,
                      _tab_fp('U1', 4, 5, [('1', 0, -0.5, .6, .3, 'X'), ('2', 0, 0, .6, .3, 'P'), ('3', 0, 0.5, .6, .3, 'Y')], (.5, .9))
                      + _tab_fp('TP2', 13, 5, [('1', 0, 0, 1.5, 1.5, 'P')], (.9, .9)))


def shove_board(d):
    """12 x 2.5: R1 at 3 (its courtyard 2..4), C2 at 6.2 with a track on its pad 2, R3 far
    right, C1 parked: no spot within 2 mm of R1 fits until C2 moves 1.25 mm right"""
    return _tab_board(d, 'shove.kicad_pcb', 12, 2.5,
                      _tab_fp('R1', 3, 1.25, [('1', -.5, 0, .5, .5, 'A'), ('2', .5, 0, .5, .5, 'B')], (1, .5))
                      + _tab_fp('C2', 6.2, 1.25, [('1', -.5, 0, .5, .5, 'X'), ('2', .5, 0, .5, .5, 'Y')], (1, .5))
                      + _tab_fp('R3', 10.5, 1.25, [('1', -.5, 0, .5, .5, 'P'), ('2', .5, 0, .5, .5, 'Q')], (1, .5))
                      + _tab_fp('C1', 30, 1.25, [('1', -.5, 0, .5, .5, 'B'), ('2', .5, 0, .5, .5, 'GND')], (1, .5))
                      + _tseg(6.7, 1.25, 6.7, 2.2, 'Y'))


def pocket_board(d, h=0.5):
    """10 x 4: Z tracks (y 2 +- h) fence a pocket along y=2 (x 0..6, closed at x=6); TP9's
    GND pad placed in it reaches no GND copper. h 0.5: no legal via spot, boxed in. h 0.58:
    a 0.5/0.2 via fits, the class (here default) 0.6/0.3 route lays does not: boxed in too"""
    return _tab_board(d, f'pocket{h}.kicad_pcb', 10, 4,
                      _tab_fp('TP9', 30, 2, [('1', 0, 0, .3, .3, 'GND')], (.2, .2))
                      + _tseg(0.3, 2 - h, 6, 2 - h, 'Z') + _tseg(0.3, 2 + h, 6, 2 + h, 'Z') + _tseg(6, 2 - h, 6, 2 + h, 'Z'))


def padstack_board(d):
    """12 x 6: J1 (origin 9,3) has its N pad 3 x 1 at 3,3, outside its own courtyard (as an
    easyeda header's); TP9 (N) parked off-board. place --near 3,3 once put TP9 on J1.1"""
    return _tab_board(d, 'padstack.kicad_pcb', 12, 6,
                      _tab_fp('J1', 9, 3, [('1', -6, 0, 3, 1, 'N')], (.5, .5))
                      + _tab_fp('TP9', 30, 3, [('1', 0, 0, .6, .6, 'N')], (.4, .4)))


def prerouted_rip_board(d):
    """rip_board with B already routed straight over the gap's mouth: only --rip-board
    lets A through"""
    p = rip_board(d)
    q = os.path.join(d, 'riprb.kicad_pcb')
    open(q, 'w').write(open(p).read().replace('\n)\n', ' (segment (start 16 9.3) (end 24 9.3) (width 0.2) (layer "F.Cu")'
                                                       ' (net "B") (uuid "B169.3"))\n)\n'))
    return q


def pair_board(d):
    """30 x 20: D_P / D_N pads 0.5 mm apart at both ends, a W wall from y 0 to 12 at x=15"""
    p = os.path.join(d, 'pair.kicad_pcb')
    fp = lambda ref, x, y, net: (f' (footprint "x:TP" (layer "F.Cu") (at {x} {y}) (property "Reference" "{ref}") (attr smd)'
                                 f' (fp_rect (start -.2 -.2) (end .2 .2) (layer "F.CrtYd"))'
                                 f' (pad "1" smd rect (at 0 0) (size .3 .3) (layers "F.Cu") (net "{net}")))')
    open(p, 'w').write('(kicad_pcb (version 20240108) (generator "pcbnew")'
                       ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
                       ' (gr_rect (start 0 0) (end 30 20) (layer "Edge.Cuts"))'
                       + fp('TP1', 5, 9.8, 'D_P') + fp('TP2', 5, 10.3, 'D_N') + fp('TP3', 25, 13.8, 'D_P')
                       + fp('TP4', 25, 14.3, 'D_N') + ' (segment (start 15 0) (end 15 12) (width 0.2) (layer "F.Cu")'
                       ' (net "W") (uuid "w1"))\n)\n')
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

def vcenter_board(d):
    """A: a track ending 0.15 mm off its via's centre (snaps), a 0.1 mm stub inside that via
    (goes); C: a track ending off-centre whose snap would tilt it into B's via at 9,11.5
    (skipped). Default clearance 0.2."""
    return _tab_board(d, 'vcenter.kicad_pcb', 20, 20,
                      _tseg(2, 5, 10.15, 5, 'A') + _tseg(9.95, 5, 10.05, 5, 'A') + _tvia(10, 5, 'A')
                      + _tseg(2, 12.2, 10.2, 12.2, 'C') + _tvia(10, 12, 'C') + _tvia(9, 11.5, 'B'))

def _tzone(net, ly, x0, y0, x1, y1, uid):
    sq = ''.join(f'\t\t\t\t(xy {x} {y})\n' for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1)))
    return (f'\t(zone\n\t\t(net "{net}")\n\t\t(layer "{ly}")\n\t\t(uuid "{uid}")\n\t\t(polygon\n\t\t\t(pts\n{sq}'
            f'\t\t\t)\n\t\t)\n\t\t(filled_polygon\n\t\t\t(layer "{ly}")\n\t\t\t(pts\n{sq}\t\t\t)\n\t\t)\n\t)\n')

def faceted_board(d):
    """20 x 10: za "fatten P", a 2 mm band (2,4)-(12,4) whose round ends are 15-degree chord
    runs (as fatten / route --pour wrote them before kpcb_fillet), and zb, a hand-drawn
    rectangle (no chord run: stays sharp, no fillet)"""
    def zone(uid, name, pts):
        xy = ''.join(f'\t\t\t\t(xy {x:.4f} {y:.4f})\n' for x, y in pts)
        return (f'\t(zone\n\t\t(net "P")\n\t\t(layer "F.Cu")\n\t\t(uuid "{uid}")\n\t\t(name "{name}")\n'
                f'\t\t(hatch edge 0.5)\n\t\t(connect_pads yes\n\t\t\t(clearance 0.2)\n\t\t)\n\t\t(min_thickness 0.25)\n'
                f'\t\t(fill yes\n\t\t\t(thermal_gap 0.5)\n\t\t\t(thermal_bridge_width 0.5)\n\t\t\t(island_removal_mode 0)\n'
                f'\t\t)\n\t\t(polygon\n\t\t\t(pts\n{xy}\t\t\t)\n\t\t)\n\t)\n')
    arc = lambda cx, a0: [(cx + math.cos(math.radians(a0 + 15 * k)), 4 + math.sin(math.radians(a0 + 15 * k)))  # noqa: E731
                          for k in range(13)]
    return _tab_board(d, 'faceted.kicad_pcb', 20, 10, zone('za', 'fatten P', arc(12, -90) + arc(2, 90))
                      + zone('zb', 'hand', [(14, 2), (18, 2), (18, 8), (14, 8)]))

def mvvia_board(d):
    """GND filled on F.Cu and B.Cu; a free GND via at 10,10 that a SIG track down x=10 now
    runs through (as after a chain is re-laid over it): mvvia must move it >= 0.6 mm"""
    v = _tvia(10, 10, 'GND').replace('\t\t(net "GND")', '\t\t(free yes)\n\t\t(net "GND")')
    return _tab_board(d, 'mvvia.kicad_pcb', 20, 20, _tseg(10, 2, 10, 18, 'SIG') + v.replace('(uuid "', '(uuid "vv')
                      + _tzone('GND', 'F.Cu', 1, 1, 19, 19, 'zf') + _tzone('GND', 'B.Cu', 1, 1, 19, 19, 'zb'))

def uniform_board(d):
    """PWR_HIGH (1 mm) runs P (y 5) and P2 (y 15), each 1.5 / 1.0 / 1.5 mm between test points;
    a foreign Q track at y 16 boxes P2's middle in at 1.0. P3 (y 10) is 4 / 1 / 4 mm with a
    0.2 mm R track 1.8 mm off its middle: a 4 mm middle's edge would cross it, though R lies
    outside the centreline box + 1 mm that Exact once gathered items in. widen --uniform-power
    takes P's middle to 1.5 and leaves P2's and P3's steps (4)"""
    os.makedirs(d, exist_ok=True)
    json.dump({'net_settings': {'classes': [{'name': 'Default', 'clearance': 0.2, 'track_width': 0.2},
                                            {'name': 'PWR_HIGH', 'clearance': 0.2, 'track_width': 1.0}],
                                'netclass_patterns': [{'pattern': 'P*', 'netclass': 'PWR_HIGH'}]}},
              open(os.path.join(d, 'u.kicad_pro'), 'w'))
    run = lambda n, y, wo=1.5: (_tab_fp('TP' + n, 2, y, [('1', 0, 0, 2, 2, 'P' + n)], (1.1, 1.1))   # noqa: E731
                        + _tab_fp('TQ' + n, 18, y, [('1', 0, 0, 2, 2, 'P' + n)], (1.1, 1.1))
                        + _tseg(2, y, 8, y, 'P' + n, wo) + _tseg(8, y, 14, y, 'P' + n, 1.0) + _tseg(14, y, 18, y, 'P' + n, wo))
    return _tab_board(d, 'u.kicad_pcb', 20, 20, run('', 5) + run('2', 15) + run('3', 10, 4.0)
                      + _tseg(9, 16, 13, 16, 'Q') + _tseg(10, 11.8, 12, 11.8, 'R'))

def jog_board(d, nudge=False):
    """typed rules, GND filled on F and B. Net S: TP1 (2,9) up to (2,5), along to (8,5), a 45
    jog of 0.5 mm, along y 5.5 to (14,5.5), down to TP2 (14,9). Collapse A (the run onto
    y 5.5) is blocked by a free GND via at 5,6.0; collapse B (onto y 5) by a Q track at y 4.7.
    nudge: no GND via; A blocked by an R track at y 5.8, B by a Q via at 11,4.55 with its Q
    track down to 11,1 (0.1 mm down is exactly the clearance, Exact's margin refuses it: 0.15)"""
    sd = _typed_dir(os.path.join(d, nudge if isinstance(nudge, str) else 'nudge' if nudge else 'jog'))
    v = _tvia(5, 6.0, 'GND').replace('\t\t(net "GND")', '\t\t(free yes)\n\t\t(net "GND")')
    # 'fresh': Q's track to the via has a jog of its own, listed first, so it is re-laid (no uuid)
    # before S's nudge would drag it (KeyError: None in the writer, 2026-10-27)
    body = (_tseg(11, 4.55, 11, 3, 'Q') + _tseg(11, 3, 13, 3, 'Q') + _tseg(13, 3, 13.3, 2.7, 'Q')
            + _tseg(13.3, 2.7, 17, 2.7, 'Q') + _tseg(17, 2.7, 17, 1, 'Q') if nudge == 'fresh' else '')
    body += (_tab_fp('TP1', 2, 9, [('1', 0, 0, 1, 1, 'S')], (.6, .6)) + _tab_fp('TP2', 14, 9, [('1', 0, 0, 1, 1, 'S')], (.6, .6))
             + _tseg(2, 9, 2, 5, 'S') + _tseg(2, 5, 8, 5, 'S') + _tseg(8, 5, 8.5, 5.5, 'S') + _tseg(8.5, 5.5, 14, 5.5, 'S')
             + _tseg(14, 5.5, 14, 9, 'S'))
    if nudge == 'neck':                              # A: S onto y 5.5 leaves GND 0.4 mm beside R at y 6.1
        body += _tseg(3, 6.1, 7, 6.1, 'R') + _tseg(9, 4.85, 13, 4.85, 'Q')
    elif nudge:
        body += _tseg(3, 5.8, 7, 5.8, 'R') + _tvia(11, 4.55, 'Q') + ('' if nudge == 'fresh' else _tseg(11, 4.55, 11, 1, 'Q'))
    else:
        body += v + _tseg(9, 4.7, 13, 4.7, 'Q')
    return _tab_board(sd, 't.kicad_pcb', 20, 12, body + _tzone('GND', 'F.Cu', .5, .5, 19.5, 11.5, 'zf')
                      + _tzone('GND', 'B.Cu', .5, .5, 19.5, 11.5, 'zb'))

def cusp_board(d):
    """typed rules, GND filled on F and B. Net S leaves via V (5,9) up to (5,8.3), 45 to (6.55,6.75),
    a 0.55 mm jog leg to (7.1,6.75), 45 to TP2 (8,5.85). Full collapse A bends 0.15 mm off V's
    centre, inside its antipad (the GPIO_4 In1 cusp): refused; 20% keeps the bend 0.59 mm out"""
    sd = _typed_dir(os.path.join(d, 'cusp'))
    body = (_tvia(5, 9, 'S') + _tab_fp('TP2', 8, 5.85, [('1', 0, 0, .6, .6, 'S')], (.4, .4))
            + _tseg(5, 9, 5, 8.3, 'S') + _tseg(5, 8.3, 6.55, 6.75, 'S') + _tseg(6.55, 6.75, 7.1, 6.75, 'S')
            + _tseg(7.1, 6.75, 8, 5.85, 'S'))
    return _tab_board(sd, 't.kicad_pcb', 14, 12, body + _tzone('GND', 'F.Cu', .5, .5, 13.5, 11.5, 'zf')
                      + _tzone('GND', 'B.Cu', .5, .5, 13.5, 11.5, 'zb'))

def link_board(d):
    """sync by symbol path: R1 linked, R2 refdes-only (no path), FID1 board_only and
    not in the schematic (INFO, not an error). Returns (board, net)."""
    fp = lambda r, extra: (f' (footprint "R" (layer "F.Cu") (at 5 5){extra} (property "Reference" "{r}")'
                           f' (property "Value" "1k") (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu")))')
    p, n = os.path.join(d, 'link.kicad_pcb'), os.path.join(d, 'link.net')
    open(p, 'w').write('(kicad_pcb (version 20240108) (generator "pcbnew")'
                       ' (layers (0 "F.Cu" signal) (25 "Edge.Cuts" user))'
                       + fp('R1', ' (path "/s1/u1")') + fp('R2', '') + fp('FID1', ' (attr smd board_only)') + ')')
    comp = lambda r, u: (f' (comp (ref "{r}") (value "1k") (libsource (lib "x") (part "R"))'
                         f' (sheetpath (names "/") (tstamps "/s1/")) (tstamps "{u}"))')
    open(n, 'w').write('(export (version "E") (design (source "t.kicad_sch")) (components'
                       + comp('R1', 'u1') + comp('R2', 'u2') + ') (libparts) (nets))')
    return p, n

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

def writer_board(d):
    """30 x 20 board in KiCad's own tab layout (the writers edit by span): R1 at 5,10 and
    R2 at 25,10 (rot 90) share net A behind a W wall on F.Cu (x=15, y 1..19); C1 is
    parked off the board (B to R1.2, GND); a netless stub sits on R2.2 (net C); R2's
    fp_lines are stored through the 10.99 double transform (stored = T^-1(local))."""
    import uuid as U
    u = lambda: f'(uuid "{U.uuid4()}")'                 # noqa: E731
    def lines(ref, x, y, rot, bad):
        out = ''
        for (x0, y0), (x1, y1) in (((-1, -.5), (1, -.5)), ((1, -.5), (1, .5)), ((1, .5), (-1, .5)), ((-1, .5), (-1, -.5))):
            if bad:                                     # T^-1 of the local point, rot 90 only
                (x0, y0), (x1, y1) = (y - y0, x0 - x), (y - y1, x1 - x)
            out += (f'\t\t(fp_line\n\t\t\t(start {x0} {y0})\n\t\t\t(end {x1} {y1})\n'
                    f'\t\t\t(stroke\n\t\t\t\t(width 0.05)\n\t\t\t\t(type solid)\n\t\t\t)\n'
                    f'\t\t\t(layer "F.CrtYd")\n\t\t\t{u()}\n\t\t)\n')
        return out
    def fp(ref, x, y, rot, n1, n2, bad=False):
        pads = ''.join(f'\t\t(pad "{k}" smd rect\n\t\t\t(at {px} 0' + (f' {rot}' if rot else '') + ')\n'
                       f'\t\t\t(size 0.5 0.5)\n\t\t\t(layers "F.Cu")\n\t\t\t(net "{nn}")\n\t\t\t{u()}\n\t\t)\n'
                       for k, px, nn in (('1', -.5, n1), ('2', .5, n2)))
        return (f'\t(footprint "R"\n\t\t(layer "F.Cu")\n\t\t{u()}\n\t\t(transform\n\t\t\t(translate {x} {y})\n'
                f'\t\t\t(rotate {rot})\n\t\t\t(scale 1 1)\n\t\t)\n\t\t(property "Reference" "{ref}"\n'
                f'\t\t\t(at 0 -1.5 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t{u()}\n\t\t\t(effects\n\t\t\t\t(font\n'
                f'\t\t\t\t\t(size 1 1)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n' + lines(ref, x, y, rot, bad) + pads + '\t)\n')
    seg = lambda x0, y0, x1, y1, net: (f'\t(segment\n\t\t(start {x0} {y0})\n\t\t(end {x1} {y1})\n\t\t(width 0.2)\n'
                                       f'\t\t(layer "F.Cu")\n\t\t(net "{net}")\n\t\t{u()}\n\t)\n')
    p = os.path.join(d, 'writer.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb\n\t(version 20260901)\n\t(generator "pcbnew")\n\t(generator_version "10.99")\n'
        '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(2 "B.Cu" signal)\n\t\t(25 "Edge.Cuts" user)\n\t)\n'
        f'\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 30 20)\n\t\t(layer "Edge.Cuts")\n\t\t{u()}\n\t)\n'
        + fp('R1', 5, 10, 0, 'A', 'B') + fp('R2', 25, 10, 90, 'A', 'C', bad=True) + fp('C1', 40, 10, 0, 'B', 'GND')
        + seg(15, 1, 15, 19, 'W') + seg(25, 9.5, 25, 8, '') + ')\n')
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
    footprint whose art is on F.SilkS; only the first is board_only) beside one real part, R1."""
    logo = lambda x, side, art, at='': (f' (footprint "Logo" (layer "{side}") (at {x} 5){at}'
                                        ' (property "Reference" "REF**") (property "Value" "LOGO")'
                                 f' (fp_poly (pts (xy -1 -1) (xy 1 -1) (xy 1 1)) (layer "{art}")))')
    p = os.path.join(d, 'dup.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 20) (layer "Edge.Cuts"))'
        + logo(5, 'F.Cu', 'F.SilkS', ' (attr board_only)') + logo(10, 'F.Cu', 'F.SilkS') + logo(15, 'B.Cu', 'F.SilkS')
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
    +3V3 -> D1 Schottky -> U4 switch IN -> +3V3_EXT (a series diode is a link); U4's
    open-drain ST sinks R6 -> LED D2 -> +3V3 (resistor first: still a pull-up).
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
        + comp('J1', 'USB', 'conn') + comp('R5', '0', 'R') + comp('D1', 'BAT54', 'D_Schottky')
        + comp('U4', 'LSW', 'lsw') + comp('D2', 'RED', 'LED') + comp('R6', '1k', 'R') + ')'
        ' (libparts (libpart (lib "x") (part "cell")' + pins('+', '-') + ')'
        ' (libpart (lib "x") (part "D_Schottky")' + pins('K', 'A') + ') (libpart (lib "x") (part "LED")' + pins('K', 'A') + ')'
        ' (libpart (lib "x") (part "lsw") (pins (pin (num "1") (name "IN") (type "power_in"))'
        ' (pin (num "2") (name "OUT") (type "power_out")) (pin (num "3") (name "ST") (type "open_collector"))'
        ' (pin (num "4") (name "GND") (type "power_in"))))'
        ' (libpart (lib "x") (part "Fuse")' + pins('1', '2') + ')'
        ' (libpart (lib "x") (part "buck")' + pins('VIN', 'SW', 'FB', 'GND') + ')'
        ' (libpart (lib "x") (part "L")' + pins('1', '2') + ') (libpart (lib "x") (part "R")' + pins('1', '2') + ')'
        ' (libpart (lib "x") (part "efuse")' + pins('IN', 'OUT', 'ILM', 'GND') + ')'
        ' (libpart (lib "x") (part "load")' + pins('VCC') + ') (libpart (lib "x") (part "conn")' + pins('VBUS') + '))'
        ' (nets' + net(1, 'N_BT', ('BT1', '1'), ('F1', '1'))
        + net(2, 'GND', ('BT1', '2'), ('U1', '4'), ('R2', '2'), ('R4', '2'), ('U3', '4'), ('U4', '4'))
        + net(3, 'VSYS', ('F1', '2'), ('U1', '1'), ('R5', '2'), cls='PWR_HIGH')
        + net(4, 'Net-(U1-SW)', ('U1', '2'), ('L1', '1')) + net(5, 'Net-(U1-FB)', ('U1', '3'), ('R1', '2'), ('R2', '1'))
        + net(6, '+3V3', ('L1', '2'), ('R1', '1'), ('U3', '1'), ('D1', '2'), ('D2', '2'))
        + net(7, 'Net-(U3-ILM)', ('U3', '3'), ('R4', '1'))
        + net(8, '3V3_OUT', ('U3', '2'), ('U2', '1')) + net(9, 'VBUS', ('J1', '1'), ('R5', '1'))
        + net(10, 'Net-(D1-K)', ('D1', '1'), ('U4', '1')) + net(11, '+3V3_EXT', ('U4', '2'))
        + net(12, 'Net-(D2-K)', ('D2', '1'), ('R6', '1')) + net(13, 'Net-(U4-ST)', ('U4', '3'), ('R6', '2')) + '))')
    return p

def i2c_net(d):
    """U1 (an MCU, IO1/IO2) with two TCAL9539: U2 A0 via R1 10k to +3V3, A1 GND (0x75);
    U3 A0 straight to +3V3, A1 GND (0x75 again: a duplicate). R3 pulls SDA up; J1's
    SDA_RAW hangs off it through R4 33 ohm (one bus, not a second)."""
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
        + comp('R1', '10k', 'R') + comp('R3', '2.2k', 'R') + comp('R4', '33', 'R') + comp('J1', 'I2C', 'R') + ')'
        ' (libparts (libpart (lib "x") (part "mcu")' + pins('IO1', 'IO2') + ')'
        ' (libpart (lib "x") (part "io")' + pins('SDA', 'SCL', 'A0', 'A1') + ')'
        ' (libpart (lib "x") (part "R")' + pins('1', '2') + '))'
        ' (nets' + net(1, 'SDA', ('U1', '1'), ('U2', '1'), ('U3', '1'), ('R3', '1'), ('R4', '1'))
        + net(2, 'SCL', ('U1', '2'), ('U2', '2'), ('U3', '2'))
        + net(3, '+3V3', ('R1', '2'), ('U3', '3'), ('R3', '2')) + net(4, 'Net-(U2-A0)', ('U2', '3'), ('R1', '1'))
        + net(5, 'GND', ('U2', '4'), ('U3', '4')) + net(6, 'SDA_RAW', ('R4', '2'), ('J1', '1')) + '))')
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

# ---- 2026-10-19 backlog pass: typed DRU rules, drag, prune, smooth/tracks/widen/loops, silk

def _typed_dir(d, dru=True):
    """a project dir with Default clearance 0.15, an RF* class at 0.3, and (dru) the
    parsnip type rules: Pad to Track 0.1, outer track-track 0.127"""
    os.makedirs(d, exist_ok=True)
    pro = {'board': {'design_settings': {'rules': {'min_copper_edge_clearance': 0.2, 'min_text_height': 0.8,
                                                   'min_text_thickness': 0.1, 'min_silk_clearance': 0.15}}},
           'net_settings': {'classes': [{'name': 'Default', 'clearance': 0.15, 'track_width': 0.15, 'via_diameter': 0.6,
                                         'via_drill': 0.3}, {'name': 'RFC', 'clearance': 0.3}],
                            'netclass_patterns': [{'pattern': 'RF*', 'netclass': 'RFC'}]}}
    for stem in ('t', 's'):
        json.dump(pro, open(os.path.join(d, stem + '.kicad_pro'), 'w'))
    if dru:
        open(os.path.join(d, 't.kicad_dru'), 'w').write(
            '(version 1)\n(rule "Pad to Track"\n    (constraint clearance (min 0.1mm))\n'
            '    (condition "A.Type == \'Pad\' && B.Type == \'Track\'"))\n'
            '(rule "Min Trace Spacing (outer)"\n    (layer outer)\n    (constraint clearance (min 0.127mm))\n'
            '    (condition "A.Type == \'Track\' && B.Type == \'Track\'"))\n'
            '(rule "Min Silkscreen Text"\n    (layer "?.Silkscreen")\n    (constraint text_thickness (min 0.15mm))\n'
            '    (constraint text_height (min 1.0mm)))\n')
    return d


def typed_board(d, dru=True):
    """10 x 6: net A from TP1 (1,3) to TP2 (9,3) past U1's X/Y pads 0.4 mm apart at x=5
    (y 2.2..2.8 and 3.2..3.8), W walls above and below: a 0.15 track fits the gap only
    under the 0.1 Pad to Track rule (the 0.15 netclass needs 0.45)"""
    return _tab_board(_typed_dir(d, dru), 't.kicad_pcb', 10, 6,
                      _tab_fp('TP1', 1, 3, [('1', 0, 0, .6, .6, 'A')], (.4, .4))
                      + _tab_fp('TP2', 9, 3, [('1', 0, 0, .6, .6, 'A')], (.4, .4))
                      + _tab_fp('U1', 5, 3, [('1', 0, -0.5, 1, .6, 'X'), ('2', 0, 0.5, 1, .6, 'Y')], (.6, .9))
                      + _tseg(5, 0.2, 5, 2.0, 'W') + _tseg(5, 4.0, 5, 5.8, 'W'))


def stair_board(d):
    """10 x 14 beside typed_board's rules: net S a 0.15 staircase TP3 (1,2) -> TP4 (9,8)
    with four 90s; net L joins TP5/TP6 twice, straight at y=11 and by a detour at 12.5"""
    return _tab_board(d, 's.kicad_pcb', 10, 14,
                      _tab_fp('TP3', 1, 2, [('1', 0, 0, .6, .6, 'S')], (.4, .4))
                      + _tab_fp('TP4', 9, 8, [('1', 0, 0, .6, .6, 'S')], (.4, .4))
                      + _tab_fp('TP5', 1.5, 11, [('1', 0, 0, .6, .6, 'L')], (.4, .4))
                      + _tab_fp('TP6', 8.5, 11, [('1', 0, 0, .6, .6, 'L')], (.4, .4))
                      + ''.join(_tseg(*a, *c, 'S', 0.15) for a, c in zip([(1, 2), (3, 2), (3, 4), (6, 4), (6, 8)],
                                                                          [(3, 2), (3, 4), (6, 4), (6, 8), (9, 8)]))
                      + _tseg(1.5, 11, 8.5, 11, 'L') + _tseg(1.5, 11, 2.5, 12.5, 'L') + _tseg(2.5, 12.5, 7.5, 12.5, 'L')
                      + _tseg(7.5, 12.5, 8.5, 11, 'L'))


def dense_board(d):
    """20 x 12 beside typed_board's rules: net D TP20 (2,6) -> TP21 (18,6) routed the long way
    round by y=1 (26 mm vs a 16 mm MST: a detour `reroute` straightens). U2 at 10,9 (courtyard
    +/-3 x +/-1): pin 1 (E, x 8) free; pin 2 (B, x 10) in a tight W ring 0.45 mm out (no track
    or via gets out); pin 3 (V, x 12) in a W ring 1.2 mm out on F.Cu (room for a via inside).
    TP22/23/24 give E, V and B a pending link each (congest)."""
    sq = lambda cx, cy, r, net='W': ''.join(_tseg(*a, *b_, net) for a, b_ in zip(       # noqa: E731
        [(cx - r, cy - r), (cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r)],
        [(cx + r, cy - r), (cx + r, cy + r), (cx - r, cy + r), (cx - r, cy - r)]))
    return _tab_board(d, 'dn.kicad_pcb', 20, 12,
                      _tab_fp('TP20', 2, 6, [('1', 0, 0, .6, .6, 'D')]) + _tab_fp('TP21', 18, 6, [('1', 0, 0, .6, .6, 'D')])
                      + _tseg(2, 6, 2, 1, 'D') + _tseg(2, 1, 18, 1, 'D') + _tseg(18, 1, 18, 6, 'D')
                      + _tab_fp('U2', 10, 9, [('1', -2, 0, .3, .3, 'E'), ('2', 0, 0, .3, .3, 'B'), ('3', 2, 0, .3, .3, 'V')],
                                (3, 1))
                      + sq(10, 9, .45) + sq(12, 9, 1.2)
                      + _tab_fp('TP22', 2, 10.5, [('1', 0, 0, .6, .6, 'E')]) + _tab_fp('TP23', 18, 10.5, [('1', 0, 0, .6, .6, 'V')])
                      + _tab_fp('TP24', 10, 11.3, [('1', 0, 0, .6, .6, 'B')]))


def channel_board(d):
    """10 x 6 beside typed_board's rules: 1 mm W walls filling y 3..4 on F.Cu and B.Cu leave a
    1.2 mm gap at x 4.4..5.6; nets N0..N5 (TPs at y 1 and 5.5, x 3..7) all cross that row,
    four of them over solid wall (congest: no capacity there)"""
    walls = ''.join(_tseg(x0, 3.5, x1, 3.5, 'W', 1.0, ly) for x0, x1 in ((0, 4.4), (5.6, 10)) for ly in ('F.Cu', 'B.Cu'))
    tps = ''.join(_tab_fp(f'TP{30 + 2 * i + k}', 3 + 0.8 * i, 1 + 4.5 * k, [('1', 0, 0, .5, .5, f'N{i}')], (.3, .3))
                  for i in range(6) for k in (0, 1))
    return _tab_board(d, 'ch.kicad_pcb', 10, 6, walls + tps)


def graze_board(d):
    """10 x 6 beside typed_board's rules: net G a 0.2 track at y=1 that only grazes TP7 (1,1)
    and stops 0.2 short of TP8's centre (8,1), 0.1 inside its edge: left as laid; net K grazes TP10 (1,4) and TP11 (8,4), with a
    stub from the TP10 graze to its centre (that pad's only entry: no loop); net H ends 0.08
    left of TP12 (5,5.5)'s corner, off its line (a stub, not a slide)"""
    return _tab_board(d, 'g.kicad_pcb', 10, 6,
                      _tab_fp('TP7', 1, 1, [('1', 0, 0, .6, .6, 'G')], (.4, .4))
                      + _tab_fp('TP8', 8, 1, [('1', 0, 0, .6, .6, 'G')], (.4, .4))
                      + _tab_fp('TP10', 1, 4, [('1', 0, 0, .6, .6, 'K')], (.4, .4))
                      + _tab_fp('TP11', 8, 4, [('1', 0, 0, .6, .6, 'K')], (.4, .4))
                      + _tab_fp('TP12', 5, 5.5, [('1', 0, 0, .6, .6, 'H')], (.4, .4))
                      + _tseg(1.35, 1, 7.8, 1, 'G') + _tseg(1.35, 4, 7.65, 4, 'K') + _tseg(1.35, 4, 1, 4, 'K')
                      + _tseg(3, 5.2, 4.62, 5.2, 'H'))


def drag_board(d):
    """R1 (pads A at 9.5,10 / B at 10.5,10, 0.6 mm) with a 0.4 mm A via in pad 1; an F.Cu A
    track from 9.25,10.2 (in pad 1, off the via) to 5,10.2; a B.Cu A track from the via
    centre to 9.5,14; a B.Cu A track ending at 9.75,9.8 (under pad 1, off the via: B.Cu,
    so it must not drag); a C track at y 10.75 x 7..8.5 that the F.Cu leg swings into
    when R1 moves up 0.5"""
    import uuid as U
    via = (f'\t(via\n\t\t(at 9.5 10)\n\t\t(size 0.4)\n\t\t(drill 0.2)\n\t\t(layers "F.Cu" "B.Cu")\n'
           f'\t\t(net "A")\n\t\t(uuid "{U.uuid4()}")\n\t)\n')
    return _tab_board(d, 'drag.kicad_pcb', 20, 20,
                      _tab_fp('R1', 10, 10, [('1', -0.5, 0, .6, .6, 'A'), ('2', 0.5, 0, .6, .6, 'B')], (1, .5))
                      + via + _tseg(9.25, 10.2, 5, 10.2, 'A') + _tseg(9.5, 10, 9.5, 14, 'A', ly='B.Cu')
                      + _tseg(9.75, 9.8, 13, 6, 'A', ly='B.Cu') + _tseg(7, 10.75, 8.5, 10.75, 'C'))


def prune_board(d):
    """G: TP1 -> track t1 to 6,2; an old dangling G stub elsewhere (8,8)-(9,8). P trunk
    TP2 (1,5) - TP3 (9,5) with C1.1 teed into it by a stub; C1.2 G to TP4 and on to TP5
    (a chain through C1.2: unhook C1 cuts TP5 off). A teardrop zone sits on track td1 (net Q)"""
    tear = ('\t(zone\n\t\t(net "Q")\n\t\t(layer "F.Cu")\n\t\t(uuid "tz1")\n\t\t(attr\n\t\t\t(teardrop\n'
            '\t\t\t\t(type padvia)\n\t\t\t)\n\t\t)\n\t\t(polygon\n\t\t\t(pts\n\t\t\t\t(xy 12 11.8) (xy 13 11.8)'
            ' (xy 13 12.2) (xy 12 12.2)\n\t\t\t)\n\t\t)\n\t)\n')
    return _tab_board(d, 'prune.kicad_pcb', 20, 14,
                      _tab_fp('TP1', 2, 2, [('1', 0, 0, .6, .6, 'G')], (.4, .4))
                      + _tab_fp('TP2', 1, 5, [('1', 0, 0, .6, .6, 'P')], (.4, .4))
                      + _tab_fp('TP3', 9, 5, [('1', 0, 0, .6, .6, 'P')], (.4, .4))
                      + _tab_fp('C1', 5, 7, [('1', -0.5, 0, .5, .5, 'P'), ('2', 0.5, 0, .5, .5, 'G')], (1, .5))
                      + _tab_fp('TP4', 8, 9, [('1', 0, 0, .6, .6, 'G')], (.4, .4))
                      + _tab_fp('TP7', 12, 12, [('1', 0, 0, .6, .6, 'Q')], (.4, .4))
                      + _tab_fp('TP5', 3, 9, [('1', 0, 0, .6, .6, 'G')], (.4, .4))
                      + _tseg(2, 2, 6, 2, 'G', uid='t1') + _tseg(8, 8, 9, 8, 'G') + _tseg(1, 5, 9, 5, 'P')
                      + _tseg(4.5, 7, 4.5, 5, 'P') + _tseg(5.5, 7, 8, 9, 'G') + _tseg(5.5, 7, 3, 9, 'G')
                      + _tseg(12, 12, 16, 12, 'Q', uid='td1')
                      + tear)


def custom_pad_board(d):
    """U1.1 an L-shaped custom pad (2 x 2 box, arms 0.4 wide); a track from the empty
    corner of that box (1.5,1.5 local) to TP2: inside the box, nowhere near the copper"""
    p = os.path.join(d, 'cpad.kicad_pcb')
    open(p, 'w').write(
        '(kicad_pcb (version 20240108) (generator "pcbnew")'
        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
        ' (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts"))'
        ' (footprint "U" (layer "F.Cu") (at 5 5) (property "Reference" "U1")'
        ' (pad "1" smd custom (at 0 0) (size 0.2 0.2) (layers "F.Cu") (net "N")'
        ' (primitives (gr_poly (pts (xy 0 0) (xy 2 0) (xy 2 0.4) (xy 0.4 0.4) (xy 0.4 2) (xy 0 2)) (width 0)))))'
        ' (footprint "TP" (layer "F.Cu") (at 12 6.5) (property "Reference" "TP2")'
        ' (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net "N")))'
        ' (segment (start 6.5 6.5) (end 12 6.5) (width 0.2) (layer "F.Cu") (net "N")))')
    return p


def _silk_fp(ref, x, y, pads, ref_at=(0, -1.5, 0), size=1, thick=0.15, fab=None, crt=(.9, .5), user=()):
    import uuid as U
    u = lambda: f'(uuid "{U.uuid4()}")'                 # noqa: E731
    fb = ''.join(f'\t\t(fp_text user "{txt}"\n\t\t\t(at {ux} {uy} 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t(uuid "{uu}")\n'
                 f'\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
                 for txt, ux, uy, uu in user)
    fb += (f'\t\t(fp_rect\n\t\t\t(start {-fab[0]} {-fab[1]})\n\t\t\t(end {fab[0]} {fab[1]})\n\t\t\t(stroke\n'
          f'\t\t\t\t(width 0.1)\n\t\t\t\t(type solid)\n\t\t\t)\n\t\t\t(layer "F.Fab")\n\t\t\t{u()}\n\t\t)\n') if fab else ''
    return (f'\t(footprint "x:P"\n\t\t(layer "F.Cu")\n\t\t{u()}\n\t\t(at {x} {y})\n'
            f'\t\t(property "Reference" "{ref}"\n\t\t\t(at {ref_at[0]} {ref_at[1]} {ref_at[2]})\n\t\t\t(layer "F.SilkS")\n'
            f'\t\t\t{u()}\n\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size {size} {size})\n\t\t\t\t\t(thickness {thick})\n'
            f'\t\t\t\t)\n\t\t\t)\n\t\t)\n'
            f'\t\t(property "Value" "V{ref}"\n\t\t\t(at 0 1.5 0)\n\t\t\t(layer "F.Fab")\n\t\t\t{u()}\n'
            f'\t\t\t(effects\n\t\t\t\t(font\n\t\t\t\t\t(size 1 1)\n\t\t\t\t\t(thickness 0.15)\n\t\t\t\t)\n\t\t\t)\n\t\t)\n'
            f'\t\t(fp_rect\n\t\t\t(start {-crt[0]} {-crt[1]})\n\t\t\t(end {crt[0]} {crt[1]})\n\t\t\t(stroke\n'
            f'\t\t\t\t(width 0.05)\n\t\t\t\t(type solid)\n\t\t\t)\n\t\t\t(layer "F.CrtYd")\n\t\t\t{u()}\n\t\t)\n' + fb
            + ''.join(f'\t\t(pad "{n}" smd rect\n\t\t\t(at {dx} {dy})\n\t\t\t(size {sx} {sy})\n\t\t\t(layers "F.Cu" "F.Mask")\n'
                      f'\t\t\t(net "{net}")\n\t\t\t{u()}\n\t\t)\n' for n, dx, dy, sx, sy, net in pads) + '\t)\n')


def silk_art_board(d):
    """typed rules (min text 1.0/0.15). U5: a module with no Fab layer (body = courtyard, its
    designator at its centre: UNDER); P1: a 6 x 6 SMD pad far outside its 0.4 mm courtyard
    (freebox must not offer it); a filled board poly at 20..24,2..6 with board text 'AB' on
    it (OVERLAP); 'TTF' in DIN at 1.2 mm (SIZE); 'RC' justified left bottom at 5,18 whose
    render_cache spans 5..7, 16.5..18 (the box)"""
    sd = _typed_dir(os.path.join(d, 'silkart'))
    gt = lambda txt, x, y, extra='', font='': (f'\t(gr_text "{txt}"\n\t\t(at {x} {y} 0)\n\t\t(layer "F.SilkS")\n'  # noqa: E731
                                              f'\t\t(uuid "gt{txt}")\n\t\t(effects\n\t\t\t(font\n{font}\t\t\t\t(size 1.2 1.2)\n'
                                              f'\t\t\t\t(thickness 0.2)\n\t\t\t)\n{extra}\t\t)\n\t)\n')
    din = '\t\t\t\t(face "DIN Mittelschrift Std")\n'
    rc = ('\t\t\t(justify left bottom)\n\t\t)\n\t\t(render_cache "RC" 0\n\t\t\t(polygon\n\t\t\t\t(pts\n'
          '\t\t\t\t\t(xy 5 16.5) (xy 7 16.5) (xy 7 18) (xy 5 18)\n\t\t\t\t)\n\t\t\t)\n')
    poly = ('\t(gr_poly\n\t\t(pts\n\t\t\t(xy 20 2) (xy 24 2) (xy 24 6) (xy 20 6)\n\t\t)\n\t\t(stroke\n'
            '\t\t\t(width 0.1)\n\t\t\t(type solid)\n\t\t)\n\t\t(fill yes)\n\t\t(layer "F.SilkS")\n\t\t(uuid "art1")\n\t)\n')
    return _tab_board(sd, 't.kicad_pcb', 30, 20,
                      _silk_fp('U5', 8, 8, [('1', -3.5, 0, .6, .6, 'A'), ('2', 3.5, 0, .6, .6, 'B')],
                               ref_at=(0, 0, 0), crt=(4, 4))
                      + _silk_fp('P1', 15, 15, [('1', 4, 0, 6, 6, 'C')], ref_at=(0, -1.2, 0), crt=(.4, .4))
                      + _silk_fp('J1', 27, 9, [('1', 0, -1.27, 1, 1, 'VCC'), ('2', 0, 1.27, 1, 1, 'Net-(F1-Pad2)')],
                                 ref_at=(0, -3, 0), crt=(.6, 1.9))
                      + _silk_fp('F1', 26, 16, [('1', -.5, 0, .6, .6, 'Net-(F1-Pad2)'), ('2', .5, 0, .6, .6, '/Pwr/VBAT')],
                                 ref_at=(0, 1.5, 0))
                      + poly + gt('AB', 22, 4) + gt('TTF', 14, 3, font=din) + gt('RC', 5, 18, rc, din))


def silk_review_board(d):
    """beside typed_board's rules (min text 1.0/0.15): R1's designator on its own pads
    (PAD), U1's inside its own Fab body (UNDER), R2's upside down at 0.8/0.1 (ORIENT, SIZE)"""
    sd = _typed_dir(os.path.join(d, 'silkrev'))
    two = lambda a, b: [('1', -0.5, 0, .6, .6, a), ('2', 0.5, 0, .6, .6, b)]   # noqa: E731
    return _tab_board(sd, 't.kicad_pcb', 30, 20,
                      _silk_fp('R1', 5, 5, two('A', 'B'), ref_at=(0, 0, 0))
                      + _silk_fp('U1', 15, 10, [('1', -2.5, -2.5, .6, .6, 'A'), ('2', 2.5, 2.5, .6, .6, 'B')],
                                 ref_at=(0, 0, 0), fab=(3, 3), crt=(3.3, 3.3))
                      + _silk_fp('R2', 25, 5, two('C', 'D'), ref_at=(0, -1.5, 180), size=0.8, thick=0.1))


def utext_board(d):
    """JP1 with user texts '1' (ut1) and '3' (ut3), a knockout board text 'KO' (gtKO)"""
    sd = _typed_dir(os.path.join(d, 'utext'))
    return _tab_board(sd, 't.kicad_pcb', 30, 20,
                      _silk_fp('JP1', 10, 10, [('1', -1, 0, .6, .6, 'A'), ('3', 1, 0, .6, .6, 'B')],
                               user=[('1', -2.2, 0, 'ut1'), ('3', 2.2, 0, 'ut3')], crt=(1.5, .5))
                      + '\t(gr_text "KO"\n\t\t(at 20 5 0)\n\t\t(layer "F.SilkS" knockout)\n\t\t(uuid "gtKO")\n'
                        '\t\t(effects\n\t\t\t(font\n\t\t\t\t(size 1.5 1.5)\n\t\t\t\t(thickness 0.2)\n\t\t\t)\n\t\t)\n\t)\n')


# ---- 2026-10-27 backlog pass

def ovia_board(d, offc=True):
    """typed rules. Net A: TP1 (8,5); ti 0.3 wide (8,5)->(10,5) ends on via V's centre, tj 0.6
    wide (8,5)->(14,5) runs over V; offc: tk ends inside V 0.1 mm off its centre, so deleting
    ti would leave V with an off-centre end and none centred (TRACK_NOT_CENTERED_ON_VIA)"""
    sd = _typed_dir(os.path.join(d, 'ovia' + ('1' if offc else '0')))
    return _tab_board(sd, 't.kicad_pcb', 20, 10, _tab_fp('TP1', 8, 5, [('1', 0, 0, .6, .6, 'A')], (.4, .4))
                      + _tseg(8, 5, 10, 5, 'A', 0.3) + _tseg(8, 5, 14, 5, 'A', 0.6) + _tvia(10, 5, 'A')
                      + (_tseg(10.1, 5, 10.1, 9, 'A', 0.2) if offc else ''))


def prec_board(d):
    """net A: a via at 10.12345,5.5 and a track 2,5.5 -> 10.12345,5.5 (copper must print every decimal)"""
    return _tab_board(d, 'prec.kicad_pcb', 20, 10, _tvia(10.12345, 5.5, 'A') + _tseg(2, 5.5, 10.12345, 5.5, 'A'))


def sliver_board(d):
    """typed rules. Net S: TP1 (2,5) -> 0.3 to (6,5) -> a 0.15 sliver to (6.1,5) -> 0.3 to TP2 (12,5)"""
    sd = _typed_dir(os.path.join(d, 'sliver'))
    return _tab_board(sd, 't.kicad_pcb', 14, 10, _tab_fp('TP1', 2, 5, [('1', 0, 0, .6, .6, 'S')], (.4, .4))
                      + _tab_fp('TP2', 12, 5, [('1', 0, 0, .6, .6, 'S')], (.4, .4))
                      + _tseg(2, 5, 6, 5, 'S', 0.3) + _tseg(6, 5, 6.1, 5, 'S', 0.15) + _tseg(6.1, 5, 12, 5, 'S', 0.3))


def _gzone(ly, outline, fill, name='GND', extra='', net='GND', uid=None):
    sq = lambda pts: ''.join(f'\t\t\t\t(xy {x} {y})\n' for x, y in pts)   # noqa: E731
    return (f'\t(zone\n\t\t(net "{net}")\n\t\t(layer "{ly}")\n\t\t(uuid "{uid or f"z{ly[0]}{len(outline)}"}")\n\t\t(name "{name}")\n'
            f'{extra}\t\t(polygon\n\t\t\t(pts\n{sq(outline)}\t\t\t)\n\t\t)\n'
            + (f'\t\t(filled_polygon\n\t\t\t(layer "{ly}")\n\t\t\t(pts\n{sq(fill)}\t\t\t)\n\t\t)\n' if fill else '')
            + '\t)\n')


def rect(x0, y0, x1, y1):
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def sparse_board(d):
    """typed rules, 30 x 10: GND poured and filled on F and B, one GND via at 2,5: the far end
    of the pour is ~26 mm from a via"""
    sd = _typed_dir(os.path.join(d, 'sparse'))
    return _tab_board(sd, 't.kicad_pcb', 30, 10, _tvia(2, 5, 'GND') + _tab_fp('TP1', 2, 2, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))
                      + _gzone('F.Cu', rect(.5, .5, 29.5, 9.5), rect(.5, .5, 29.5, 9.5))
                      + _gzone('B.Cu', rect(.5, .5, 29.5, 9.5), rect(.5, .5, 29.5, 9.5)))


def sparse_tht_board(d):
    """sparse_board plus J1, a 1.4 mm round GND THT pad (0.8 drill) at 28.5,5 in a notch of both
    fills (its relief: the centre is not in the fill): it already ties the far end, so the worst
    point is mid-board, and no via may land on its annular ring (TP1.2 / J4.2, 2026-10-27)"""
    sd = _typed_dir(os.path.join(d, 'sparse_tht'))
    tht = ('\t(footprint "x:J"\n\t\t(layer "F.Cu")\n\t\t(uuid "fj1")\n\t\t(at 28.5 5)\n'
           '\t\t(property "Reference" "J1"\n\t\t\t(at 0 -1.5 0)\n\t\t\t(layer "F.SilkS")\n\t\t\t(uuid "fj1r")\n\t\t)\n'
           '\t\t(pad "1" thru_hole circle\n\t\t\t(at 0 0)\n\t\t\t(size 1.4 1.4)\n\t\t\t(drill 0.8)\n'
           '\t\t\t(layers "*.Cu" "*.Mask")\n\t\t\t(net "GND")\n\t\t\t(uuid "fj1p")\n\t\t)\n\t)\n')
    notch = [(.5, .5), (29.5, .5), (29.5, 4), (27.5, 4), (27.5, 6), (29.5, 6), (29.5, 9.5), (.5, 9.5)]
    return _tab_board(sd, 't.kicad_pcb', 30, 10, _tvia(2, 5, 'GND') + tht
                      + _gzone('F.Cu', rect(.5, .5, 29.5, 9.5), notch) + _gzone('B.Cu', rect(.5, .5, 29.5, 9.5), notch))


def dead_board(d):
    """Net P: TP1 (2,5) -> TP2 (16,5) on F, the track overshooting TP2 to 17.5 (cut back to
    16). Via V1 (6,5) on it starts a pad-less cycle: F (6,5)->(9,8)->(12,8), via V2 (12,8), B
    (12,8)->(6,8)->(6,5), a B pour band round the B run: dead, V1 with it. TP3 (14,2) tees in
    at (14,5) with two thermal vias in its pad and a B pour under it: a heatsink, not dead. TP4
    (12,2) is fed from (9,5) through V3 (9,2) and V4 (11,2) on F and B in parallel: live, until
    `unhook TP4` leaves that cycle serving nothing"""
    body = (_tab_fp('TP1', 2, 5, [('1', 0, 0, 1, 1, 'P')], (.6, .6)) + _tab_fp('TP2', 16, 5, [('1', 0, 0, 1, 1, 'P')], (.6, .6))
            + _tab_fp('TP3', 14, 2, [('1', 0, 0, 2, 1.2, 'P')], (1.1, .7)) + _tab_fp('TP4', 12, 2, [('1', 0, 0, 1, 1, 'P')], (.6, .6))
            + _tseg(2, 5, 17.5, 5, 'P', 0.3, uid='live') + _tvia(6, 5, 'P')
            + _tseg(6, 5, 9, 8, 'P', 0.3, uid='df1') + _tseg(9, 8, 12, 8, 'P', 0.3, uid='df2') + _tvia(12, 8, 'P')
            + _tseg(12, 8, 6, 8, 'P', 0.3, 'B.Cu', uid='db1') + _tseg(6, 8, 6, 5, 'P', 0.3, 'B.Cu', uid='db2')
            + _tseg(14, 2, 14, 5, 'P', 0.3) + _tvia(13.5, 2, 'P') + _tvia(14.5, 2, 'P')
            + _tseg(9, 5, 9, 2, 'P', 0.3) + _tvia(9, 2, 'P') + _tvia(11, 2, 'P') + _tseg(9, 2, 11, 2, 'P', 0.3)
            + _tseg(9, 2, 11, 2, 'P', 0.3, 'B.Cu') + _tseg(11, 2, 12, 2, 'P', 0.3)
            + _gzone('B.Cu', rect(5.5, 7.6, 12.5, 8.4), rect(5.5, 7.6, 12.5, 8.4), 'band', net='P', uid='zband')
            + _gzone('B.Cu', rect(12.5, .8, 15.5, 3.2), rect(12.5, .8, 15.5, 3.2), 'sink', net='P', uid='zsink'))
    return _tab_board(d, 'dead.kicad_pcb', 20, 10, body)


def micro_board(d):
    """typed rules. Net N: TP1 (2,5) -> (8,5), a 30 um link (8,5)->(8.03,5), -> TP2 (14,5): merged.
    Net M: TP3 (2,8) -> (8,8) w0.3, then a 20 um w0.2 dot at its end, under its cap: deleted"""
    sd = _typed_dir(os.path.join(d, 'micro'))
    return _tab_board(sd, 't.kicad_pcb', 16, 10, _tab_fp('TP1', 2, 5, [('1', 0, 0, .6, .6, 'N')]) + _tab_fp('TP2', 14, 5, [('1', 0, 0, .6, .6, 'N')])
                      + _tab_fp('TP3', 2, 8, [('1', 0, 0, .6, .6, 'M')]) + _tseg(2, 5, 8, 5, 'N') + _tseg(8, 5, 8.03, 5, 'N')
                      + _tseg(8.03, 5, 14, 5, 'N') + _tseg(2, 8, 8, 8, 'M', 0.3) + _tseg(8, 8, 8.02, 8, 'M', 0.2))


def rfbranch_board(d):
    """RFX: TP1 (2,5) -> TP2 (18,5) on F.Cu, and a 3 mm branch (10,5)->(10,8) to JP1.1, a DNP
    pad: an open stub, over lambda/50 at --freq 5000 (1.2 mm with no stackup, eeff 1)"""
    jp = _tab_fp('JP1', 10, 8, [('1', 0, 0, .6, .6, 'RFX')]).replace('\t\t(layer "F.Cu")\n', '\t\t(layer "F.Cu")\n\t\t(attr smd dnp)\n', 1)
    return _tab_board(d, 'rfbr.kicad_pcb', 20, 12, _tab_fp('TP1', 2, 5, [('1', 0, 0, .6, .6, 'RFX')])
                      + _tab_fp('TP2', 18, 5, [('1', 0, 0, .6, .6, 'RFX')]) + jp
                      + _tseg(2, 5, 18, 5, 'RFX', 0.3) + _tseg(10, 5, 10, 8, 'RFX', 0.3))


def pwr_inpour_board(d):
    """PWR (not a plane: 16 of 200 mm2) poured on F.Cu 3..11 x 4..6, its track (4,5)->(10,5) inside"""
    return _tab_board(d, 'pwrin.kicad_pcb', 20, 10, _tseg(4, 5, 10, 5, 'PWR', 0.3, uid='tpwr')
                      + _gzone('F.Cu', rect(3, 4, 11, 6), rect(3, 4, 11, 6), 'pwr', net='PWR', uid='zpwr'))


def inpour_board(d):
    """GND filled on F.Cu (a thermal zone: no connect_pads yes). t1 (4,5)->(8,5) lies in the
    fill; t2 (12,5)->(14,5) too, but ends in TP1, a thermal pad: its track is a spoke"""
    return _tab_board(d, 'inpour.kicad_pcb', 20, 10, _tab_fp('TP1', 14, 5, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))
                      + _tseg(4, 5, 8, 5, 'GND', 0.3) + _tseg(12, 5, 14, 5, 'GND', 0.3)
                      + _gzone('F.Cu', rect(.5, .5, 19.5, 9.5), rect(.5, .5, 19.5, 9.5)))


def stale_pour_board(d):
    """GND zone over all of F.Cu, its saved fill only x < 8 (stale): TP1/TP3 in the fill, TP2
    at 15,5 inside the zone but not yet in any fill - a refill joins it, route must not"""
    return _tab_board(d, 'stale.kicad_pcb', 20, 10, _tab_fp('TP1', 3, 3, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))
                      + _tab_fp('TP3', 3, 7, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))
                      + _tab_fp('TP2', 15, 5, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))
                      + _gzone('F.Cu', rect(.5, .5, 19.5, 9.5), rect(.5, .5, 8, 9.5)))


def refslot_board(d):
    """4 layers with a stackup. Net S on F.Cu (2,5)->(18,5); In1 GND filled only x < 12, and
    an In1 track of X at x=15 cutting across the gap under S"""
    p = os.path.join(d, 'refslot.kicad_pcb')
    st = ''.join(f'\t\t\t(layer "{n}"\n\t\t\t\t(type "{t}")\n\t\t\t\t(thickness {th})\n' + ('\t\t\t\t(epsilon_r 4.4)\n' if t != 'copper' else '')
                 + '\t\t\t)\n' for n, t, th in (('F.Cu', 'copper', 0.035), ('dielectric 1', 'prepreg', 0.2), ('In1.Cu', 'copper', 0.03),
                                                ('dielectric 2', 'core', 1.0), ('In2.Cu', 'copper', 0.03),
                                                ('dielectric 3', 'prepreg', 0.2), ('B.Cu', 'copper', 0.035)))
    open(p, 'w').write('(kicad_pcb\n\t(version 20260901)\n\t(generator "pcbnew")\n\t(generator_version "10.99")\n'
                       '\t(layers\n\t\t(0 "F.Cu" signal)\n\t\t(4 "In1.Cu" signal)\n\t\t(6 "In2.Cu" signal)\n\t\t(2 "B.Cu" signal)\n'
                       '\t\t(25 "Edge.Cuts" user)\n\t)\n\t(setup\n\t\t(stackup\n' + st + '\t\t)\n\t)\n'
                       '\t(gr_rect\n\t\t(start 0 0)\n\t\t(end 20 10)\n\t\t(layer "Edge.Cuts")\n\t)\n'
                       + _tseg(2, 5, 18, 5, 'S') + _tseg(15, 1, 15, 9, 'X', 0.2, 'In1.Cu')
                       + _gzone('In1.Cu', rect(.5, .5, 12, 9.5), rect(.5, .5, 12, 9.5)) + ')\n')
    return p


def join_board(d):
    """typed rules. Net N: run A TP1 (4,5.2) -> (9.8,5.2) ends inside P1 (1.0 x 1.4 at 10,5),
    run B starts inside it at (10.2,4.8) -> TP2 (16,4.8): joined only through P1's copper"""
    sd = _typed_dir(os.path.join(d, 'join'))
    return _tab_board(sd, 't.kicad_pcb', 20, 10, _tab_fp('TP1', 4, 5.2, [('1', 0, 0, .6, .6, 'N')], (.4, .4))
                      + _tab_fp('TP2', 16, 4.8, [('1', 0, 0, .6, .6, 'N')], (.4, .4))
                      + _tab_fp('P1', 10, 5, [('1', 0, 0, 1.0, 1.4, 'N')], (.6, .8))
                      + _tseg(4, 5.2, 9.8, 5.2, 'N', 0.3) + _tseg(10.2, 4.8, 16, 4.8, 'N', 0.3))


def stubpad_board(d):
    """typed rules. Net N: via V (10,7) -> P1's centre (10,5) -> a stub to (10.2,5.15), still
    inside P1 (1.0 x 0.4) -> TP1 (13,5.15). Both P1 nodes are two-track nodes whose other end
    sits in P1: neither may be released, or the DP cuts past P1 (U6.5, 2026-10-27)"""
    sd = _typed_dir(os.path.join(d, 'stubpad'))
    return _tab_board(sd, 't.kicad_pcb', 16, 10, _tab_fp('P1', 10, 5, [('1', 0, 0, 1.0, .4, 'N')], (.6, .3))
                      + _tab_fp('TP1', 13, 5.15, [('1', 0, 0, .6, .6, 'N')], (.4, .4)) + _tvia(10, 7, 'N')
                      + _tseg(10, 7, 10, 5, 'N') + _tseg(10, 5, 10.2, 5.15, 'N') + _tseg(10.2, 5.15, 13, 5.15, 'N'))


def twin_board(d):
    """typed rules. Net T: U1.1 (16,5.25) runs to R1.1 (0.6 x 0.6 at 10,5) along y 5.25, while
    via V (13,4.75) runs there along y 4.75: twins 0.5 mm apart"""
    sd = _typed_dir(os.path.join(d, 'twin'))
    return _tab_board(sd, 't.kicad_pcb', 20, 10, _tab_fp('U1', 16, 5.25, [('1', 0, 0, .6, .3, 'T')], (.4, .4))
                      + _tab_fp('R1', 10, 5, [('1', 0, 0, .6, .6, 'T')], (.4, .4)) + _tvia(13, 4.75, 'T')
                      + _tseg(16, 5.25, 10.1, 5.25, 'T') + _tseg(13, 4.75, 10.1, 4.75, 'T'))


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
       ['ksheet.py', '{net}', 'lint'], ['ksheet.py', '{net}', 'sch', 'U7'],
       ['kpcb.py', '{pcb}', 'congest', '--all'], ['kpcb.py', '{pcb}', 'tracks'], ['kpcb.py', '{pcb}', 'silk']]

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

def router_lint():
    """SKILL.md is the router. Its description must fit the spec's 1024 chars (claude.ai
    refuses more, Claude Code clips the listing), and every link, reference file and
    `tool.py FILE cmd` it names must exist (commands from each tool's --help choices)."""
    t = open(os.path.join(SK, 'SKILL.md')).read()
    desc = ' '.join(t.split('\n---', 1)[0].split('description: >-', 1)[1].split())
    bad = [f'description {len(desc)} chars > 1024'] if len(desc) > 1024 else []
    docs = [os.path.join(SK, 'SKILL.md')] + sorted(glob.glob(os.path.join(HERE, '*.md')))
    for d in docs:
        bad += [f'{os.path.basename(d)} links {ln}' for ln in re.findall(r'\]\(([^)#\s]+)', open(d).read())
                if '://' not in ln and not os.path.exists(os.path.join(os.path.dirname(d), ln))]
    bad += [f'{os.path.basename(d)} not linked from SKILL.md' for d in docs[1:]
            if f'(references/{os.path.basename(d)})' not in t]
    calls, wrong = bad_calls(t, 'SKILL.md')
    return bad + wrong, calls, len(desc)

HELP = {}                       # tool -> its subcommands, from --help (None: no choices)

def bad_calls(text, where):
    """Every `tool.py FILE cmd` in text (FILE, DIR or a .net/.kicad_* path) whose cmd is
    not one of that tool's --help choices. Returns (calls found, problems)."""
    calls = set(re.findall(r'`(k\w+)\.py (?:(?:FILE|DIR|\S+\.(?:net|kicad_pcb|kicad_sch))\S*\s+)?([a-z][\w-]*)(?![\w.-])',
                           text))
    bad = []
    for tool, cmd in sorted(calls):
        if tool not in HELP:
            sets = re.findall(r'\{(\w[\w-]*(?:,[\w-]+)+)\}', run([tool + '.py', '--help'])[1])
            HELP[tool] = max((s.split(',') for s in sets), key=len, default=None)
        if HELP[tool] is not None and cmd not in HELP[tool]:
            bad.append(f'{where} names `{tool}.py {cmd}`: not a {tool} command')
    return len(calls), bad

CHECKS = []                     # main()'s ad-hoc checks, one bool each, for the total

def check(ok, msg):
    """Print one ad-hoc check and count it; returns 1 if it failed."""
    CHECKS.append(bool(ok))
    print(f"{'ok  ' if ok else 'FAIL'}  {msg}")
    return int(not ok)

def main():
    fails = 0
    rb, nc, nd = router_lint()
    ok20 = not rb and nc >= 30           # >= 30: the call regex still finds the router's calls
    fails += check(ok20, f"router: description {nd} chars, {nc} tool calls named, links resolve"
          + ''.join(f'\n      {b}' for b in rb))
    tmp = tempfile.TemporaryDirectory()
    mini = mini_project(tmp.name)
    CASES.append(("knet revpol", ['knet.py', mini, 'revpol'], 0,
                  ["D2    SMF10A", "NO FUSE in this loop", "U2.1    IN           -4.2 V below its GND, via 10ohm (R1)"]))
    CASES.append(("knet pinout", ['knet.py', mini, 'check', '--only', 'PINOUT'], 2,
                  ["D1 BAV199 on Device:D_Dual_Series_ACK", "use Device:D_Dual_Series_AKC"]))
    CASES.append(("knet fppad+parpin", ['knet.py', mini, 'check', '--only', 'FPPAD,PARPIN'], 2,
                  ["U3.4 (VIN) has no pad of its own in QFN: fused into pad 3 (VIN)",
                   "U3 pad 5 (smd) of QFN has no symbol pin", "U3.6 (X) is on NX but t:QFN has no pad 6"]))
    CASES.append(("kpcb edit vcenter", ['kpcb.py', vcenter_board(tmp.name), 'edit', 'vcenter', 'all', '-o',
                                        os.path.join(tmp.name, 'vc2.kicad_pcb')], 0,
                  ["vcenter: 1 track end(s) snapped to the via centre, 1 zero-length segment(s) deleted, "
                   "1 skipped (the snap breaks clearance)"]))
    CASES.append(("kpcb edit mvvia", ['kpcb.py', mvvia_board(tmp.name), 'edit', 'mvvia', 'vv', '2.5', '-o',
                                      os.path.join(tmp.name, 'mv2.kicad_pcb')], 0,
                  ["mvvia vv", "GND 0.6/0.3 free 10.000,10.000 -> ", "GND vias within 3 mm 1 -> 1"]))
    jb = jog_board(tmp.name)
    CASES.append(("kpcb tracks jog", ['kpcb.py', jb, 'tracks', '--kind', 'jog'], 0,
                  ["lane jogs 0.05-1 mm       1 visible", "A: via GND free 5.00,6.00 (--jogs moves it)", "B: track Q"]))
    CASES.append(("kpcb smooth --jogs", ['kpcb.py', jb, 'smooth', '--jogs', '-o', os.path.join(tmp.name, 'jog', 'j2.kicad_pcb')], 0,
                  ["jogs: 1 collapsed, 1 via(s) moved", "A: collapsed, GND via 5.00,6.00 -> "]))
    CASES.append(("kpcb smooth --jogs result", ['kpcb.py', os.path.join(tmp.name, 'jog', 'j2.kicad_pcb'), 'tracks', '--kind', 'jog'], 0,
                  ["lane jogs 0.05-1 mm       0 visible"]))
    CASES.append(("kpcb smooth --jogs --nudge", ['kpcb.py', jog_board(tmp.name, True), 'smooth', '--jogs', '--nudge',
                                                 '-o', os.path.join(tmp.name, 'nudge', 'j3.kicad_pcb')], 0,
                  ["jogs: 1 collapsed, 1 via(s) moved", "B: collapsed, Q via nudged 0.15 mm to 11.000,4.400"]))
    CASES.append(("kpcb smooth --jogs plane neck", ['kpcb.py', jog_board(tmp.name, 'neck'), 'smooth', '--jogs', '-o',
                                                    os.path.join(tmp.name, 'neck', 'j5.kicad_pcb')], 0,
                  ["S                            F.Cu   0.50 mm at 8.000,5.000  A: collapsed 70% of the way (the jog is now 0.15 mm)"]))
    CASES.append(("kpcb smooth --jogs antipad cusp", ['kpcb.py', cusp_board(tmp.name), 'smooth', '--jogs', '-o',
                                                     os.path.join(tmp.name, 'cusp', 'j6.kicad_pcb')], 0,
                  ["S                            F.Cu   0.39 mm at 6.550,6.750  A: collapsed 20% of the way (the jog is now 0.31 mm)"]))
    CASES.append(("kpcb smooth --jogs --nudge fresh track", ['kpcb.py', jog_board(tmp.name, 'fresh'), 'smooth', '--jogs',
                                                           '--nudge', '-o', os.path.join(tmp.name, 'fresh', 'j4.kicad_pcb')], 0,
                  ["jogs: 2 collapsed", "Q                            F.Cu   0.30 mm at 13.000,3.000  A: collapsed",
                   "S                            F.Cu   0.50 mm at 8.000,5.000  A: collapsed 90%"]))
    lb, ln = link_board(tmp.name)
    CASES.append(("kpcb sync link+board_only", ['kpcb.py', lb, 'sync', ln], 2,
                  ["1 error, 0 warn, 1 info", "R2 is not linked to its symbol", "board-only footprints", "FID1"]))
    CASES.append(("knet rfstub",['knet.py', os.path.join(tmp.name, 'rf.net'), 'check', '--only', 'RFSTUB'], 0,
                  ["ANT [RF_50OHM] has 3 populated parts on it (J1, R1, U1)", "3 warn",
                   "RFA [RF_50OHM]: DNP JP1 bridges it to RF net RFB", "RFB [RF_50OHM]: DNP JP1 bridges it to RF net RFA"]))
    # /Root/CS has one node, /Root/Sub/CS the rest: a local label on parent and child
    sp = os.path.join(tmp.name, 'split.net')
    open(sp, 'w').write(
        '(export (version "E") (design (source "s.kicad_sch")) (components'
        ' (comp (ref "U1") (value "X") (libsource (lib "x") (part "y")))'
        ' (comp (ref "J1") (value "C") (libsource (lib "x") (part "y")))) (libparts)'
        ' (nets (net (code "1") (name "/Root/CS") (node (ref "U1") (pin "1")))'
        ' (net (code "2") (name "/Root/Sub/CS") (node (ref "J1") (pin "1")) (node (ref "U1") (pin "2")))))')
    # diff: FILE is NEW; unix-order misuse (OLD exported later) gets a note
    for nm, dt, refs in (('d0', '2026-01-01T00:00:00', 'R1 R2'), ('d1', '2026-01-02T00:00:00', 'R1')):
        open(os.path.join(tmp.name, nm + '.net'), 'w').write(
            f'(export (version "E") (design (source "s.kicad_sch") (date "{dt}")) (components'
            + ''.join(f' (comp (ref "{r}") (value "1k") (libsource (lib "x") (part "R")))' for r in refs.split())
            + ') (libparts) (nets))')
    CASES.append(("knet diff order note", ['knet.py', os.path.join(tmp.name, 'd0.net'), 'diff',
                                           os.path.join(tmp.name, 'd1.net')], 0,
                  ["was exported after the NEW side", "=== components added (1) ===", "R2 [1k]"]))
    CASES.append(("knet solo split label", ['knet.py', sp, 'check', '--only', 'SOLO'], 2,
                  ["net /Root/CS has only U1.1 on it; /Root/Sub/CS (J1.1, U1.2) has the same name on another sheet"]))
    CASES.append(("knet i2c", ['knet.py', i2c_net(tmp.name), 'i2c'], 2,
                  ["=== SDA + SDA_RAW  /  SCL", "pull-ups : R3 2.2k (SDA) to +3V3", "connectors: J1",
                   "target   : U2    TCAL9539             0x75        A0: R1 10k to +3V3; A1: GND",
                   "controller: U1 MCU (IO1)", "ERROR address 0x75 used by U2 U3"]))
    CASES.append(("knet powertree", ['knet.py', power_net(tmp.name), 'powertree'], 0,
                  ["cells BT1: N_BT", "-> F1 1A -> VSYS  (also fed from VBUS via R5)",
                   "-> U1 BUCK1  [FB 402k/174k: Vout = 3.310 V] -> L1 2.2u -> +3V3  3.3 V",
                   "-> U3 TPS25947  [ILIM 5.40/6.07/6.60 A (R4 549)] -> 3V3_OUT  3.3 V  loads U2",
                   "J1 VBUS: VBUS  5 V", "-> D1 BAT54  [-Vf ~0.3-0.5 V] -> U4 LSW -> +3V3_EXT  3.3 V"]))
    CASES.append(("knet ocnopull R then LED", ['knet.py', os.path.join(tmp.name, 'pwr', 'pwr.net'), 'check',
                                               '--only', 'OCNOPULL'], 0, ["0 error, 0 warn"]))
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
    CASES.append(("kpcb check FABFLAGS", ['kpcb.py', fab_board(tmp.name), 'check', '--only', 'FABFLAGS'], 0,
                  ["0 error, 2 warn, 1 info", "C2 is DNP and excluded from the BOM: the BOM loses it",
                   "SW1 has LCSC C202388 but is excluded from the BOM",
                   "1 part(s) whose BOM/pos flags differ from their library footprint: TP1 (-pos_files)"]))
    CASES.append(("kpcb check CRTYDPAD HOLECLR", ['kpcb.py', crtyd_board(tmp.name), 'check', '--only', 'CRTYDPAD,HOLECLR'], 2,
                  ["1 error, 1 warn", "J1: 2 of 2 pad(s) outside its own courtyard",
                   "R2 is inside H1's 3.1 mm screw keepout: its pad 2.30 mm from the centre"]))
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
    wb = writer_board(tmp.name)
    w2, w3, w4 = (os.path.join(tmp.name, f'w{i}.kicad_pcb') for i in (2, 3, 4))
    CASES.append(("kpcb frame bug read corrected", ['kpcb.py', wb, 'where', 'R2'], 0,
                  ["fp_line graphics stored in the wrong frame", "courtyard : 24.50,9.00 .. 25.50,11.00"]))
    CASES.append(("kpcb fixlines", ['kpcb.py', wb, 'fixlines', '-o', w2], 0, ["fixlines: 4 fp_line(s) in 1 footprint(s)"]))
    CASES.append(("kpcb fixlines result", ['kpcb.py', w2, 'where', 'R2'], 0, ["courtyard : 24.50,9.00 .. 25.50,11.00"]))
    CASES.append(("kpcb airwires", ['kpcb.py', w2, 'airwires'], 0, ["airwires: 1 net(s) not in one piece", "R1.1 4.50,10.00"]))
    CASES.append(("kpcb route", ['kpcb.py', w2, 'route', 'A', '-o', w3], 0, ["A: R2.1 -> rest", "wrote "]))
    CASES.append(("kpcb route result", ['kpcb.py', w3, 'airwires', 'A'], 0, ["every net is one piece"]))
    CASES.append(("kpcb renet", ['kpcb.py', w3, 'renet', '-o', w4], 0, ["1 track(s) + 0 via(s) get the one net they touch"]))
    CASES.append(("kpcb place", ['kpcb.py', w4, 'place', 'C1', '--near', 'R1', '-r', '4'], 0, ["  1 ", "CLEAR"]))
    CASES.append(("kpcb ripup", ['kpcb.py', w3, 'ripup', 'A'], 1, ["ripup: ", "on 1 net(s)", "add -o OUT"]))
    CASES.append(("kpcb move drags", ['kpcb.py', w3, 'move', 'R1', '5', '12', '-o', os.path.join(tmp.name, 'w5.kicad_pcb')], 0,
                  ["move R1 5.000,10.000 rot 0 -> 5.000,12.000", "dragged 3 track end(s) that sat on its pads"]))
    CASES.append(("kpcb move drag result", ['kpcb.py', os.path.join(tmp.name, 'w5.kicad_pcb'), 'airwires', 'A'], 0,
                  ["every net is one piece"]))
    CASES.append(("kpcb edit solid", ['kpcb.py', w3, 'edit', 'solid', 'R1.1', '-o', os.path.join(tmp.name, 'w6.kicad_pcb')], 0,
                  ["solid R1.1", "edit: 1 op(s)"]))
    rb = rip_board(tmp.name)
    CASES.append(("kpcb route boxed in, no rip-up", ['kpcb.py', rb, 'route', '--pending', '--layers', 'F.Cu', '--rips', '0'],
                  2, ["B: TP2.1 -> rest   w 0.2  8.00 mm", "A: TP4.1 - FAILED", "boxed in by W track on F.Cu"]))
    CASES.append(("kpcb route rip-up", ['kpcb.py', rb, 'route', '--pending', '--layers', 'F.Cu'], 0,
                  ["A: TP4.1 -> rest   w 0.2  12.00 mm", "- ripped up B, re-routed:",
                   "      B: TP2.1 -> rest   w 0.2  17.51 mm", "(dry run: 9 segment(s), 0 via(s), 29.5 mm"]))
    CASES.append(("kpcb route rip-up undone", ['kpcb.py', rip_board(tmp.name, sealed=True), 'route', '--pending',
                                               '--layers', 'F.Cu'], 2,
                  ["A: TP4.1 - FAILED: no path", "ripping B did not help, it could not re-route (undone)",
                   "(dry run: 2 segment(s), 0 via(s), 8.0 mm"]))
    pb = pour_board(tmp.name)
    CASES.append(("kpcb route no 1 mm path", ['kpcb.py', pb, 'route', 'P', '--layers', 'F.Cu', '--width', '1'], 2,
                  ["P: TP2.1 - FAILED: no path within 18 mm"]))
    CASES.append(("kpcb route --pour", ['kpcb.py', pb, 'route', 'P', '--layers', 'F.Cu', '--width', '1', '--pour', '0.3',
                                        '-o', os.path.join(tmp.name, 'pour2.kicad_pcb')], 0,
                  ["P: TP2.1 -> rest   w 0.3  14.00 mm", "POUR: 1 zone(s), a 1 mm band on F.Cu round a 0.3 mm corridor",
                   "wrote 0 segment(s), 0 via(s), 0.0 mm, 1 zone(s) ->"]))
    CASES.append(("kpcb pairs", ['kpcb.py', rb, 'pairs'], 0,
                  ["pairs: 1 diff pair(s)", "D_P / D_N   Default: no diff_pair_gap set",
                   "routed 6.00 / 7.00 mm   skew 1.00 mm   vias 0 / 0",
                   "coupled 6.00 / 6.00 mm (uncoupled 0.00 / 1.00), edge gap median 0.150, min 0.150, max 0.150 mm"]))
    T = lambda n: os.path.join(tmp.name, n)             # noqa: E731
    pw = power_board(tmp.name)
    CASES.append(("kpcb fatten", ['kpcb.py', pw, 'fatten', 'P', '--layers', 'F.Cu', '--width', '3', '-o', T('pw2.kicad_pcb')], 0,
                  ["fatten P F.Cu: 1 track(s) >= 0.5 mm ->", "a 3 mm band", "outline, 12 corners", "wrote "]))
    CASES.append(("kpcb fatten needs a purpose", ['kpcb.py', pw, 'fatten', 'P', '-o', T('pw4.kicad_pcb')], 1,
                  ["fatten needs a purpose: --amps A"]))
    CASES.append(("kpcb fatten --amps", ['kpcb.py', pw, 'fatten', 'P', '--amps', '3', '-o', T('pw5.kicad_pcb')], 0,
                  ["fatten P F.Cu: 1 track(s) >= 0.5 mm -> 1 zone(s), a 1.4 mm band", "fatten P B.Cu: 1 track(s)"]))
    CASES.append(("kpcb fatten --amps carried", ['kpcb.py', pw, 'fatten', 'P', '--amps', '1', '-o', T('pw6.kicad_pcb')], 1,
                  ["fatten P F.Cu: 1 track(s) already carry 1 A at 10 C (>= 0.35 mm), no band", "fatten: no tracks to widen"]))
    CASES.append(("kpcb edit simplify", ['kpcb.py', stair_zone_board(tmp.name), 'edit', 'simplify', 'zstair', '0.0625',
                                         '-o', T('stair2.kicad_pcb')], 0,
                  ["simplify zstair 242 -> 3 corner(s)", "(1 stale fill polygon(s) dropped: refill)"]))
    CASES.append(("kpcb edit del zone", ['kpcb.py', os.path.join(tmp.name, 'stair.kicad_pcb'), 'edit', 'del', 'zstair',
                                         '-o', T('stair3.kicad_pcb')], 0, ["del   zstair (zone)"]))
    CASES.append(("kpcb fatten halo", ['kpcb.py', T('pw2.kicad_pcb'), 'copper', '9,3,11,7', '--layers', 'F.Cu'], 0,
                  ["Z F        P", "'fatten P'"]))
    CASES.append(("kpcb stitch", ['kpcb.py', pw, 'stitch', 'P'], 0,
                  ["stitch P: via 0.6/0.3 mm", "beside via 6.00,5.00: ", "(dry run: 4 via(s)"]))
    CASES.append(("kpcb stitch write", ['kpcb.py', pw, 'stitch', 'P', '10,5', '-r', '2', '--n', '1', '-o', T('pw3.kicad_pcb')], 0,
                  ["near 10.00,5.00: ", "wrote 1 via(s)"]))
    CASES.append(("kpcb stitch free", ['kpcb.py', T('pw3.kicad_pcb'), 'copper', '8,3,12,7', '--net', 'P'], 0, ["  free"]))
    CASES.append(("kpcb ampacity --fills", ['kpcb.py', pw, 'ampacity', '--from', 'TP1.1', '--to', 'TP2.1', '--amps', '2',
                                            '--fills'], 0,
                  ["THROUGH THE REAL FILLS (necks included): R ", "densest current off the pads and vias"]))
    CASES.append(("kpcb route neck-down", ['kpcb.py', neck_route_board(tmp.name), 'route', 'P', '--width', '1',
                                           '--layers', 'F.Cu'], 0, ["P: TP2.1 -> rest   w 1", "NECK: ", "out of a fine-pitch pad, then 1 mm"]))
    CASES.append(("kpcb route --rip-board", ['kpcb.py', prerouted_rip_board(tmp.name), 'route', 'A', '--layers', 'F.Cu',
                                             '--rip-board', '4'], 0,
                  ["--rip-board 4: 1 small unlocked signal net(s)", "- ripped up B, re-routed:", "ripped from the board: B"]))
    CASES.append(("kpcb route --avoid", ['kpcb.py', pair_board(tmp.name), 'route', 'D_P', '--layers', 'F.Cu',
                                         '--avoid', '14,12,16,20'], 2,
                  ["(--avoid 14,12,16,20: no track or via", "D_P: TP3.1 - FAILED", "a rule area"]))
    CASES.append(("kpcb route --pair", ['kpcb.py', pair_board(tmp.name), 'route', 'D_P', 'D_N', '--layers', 'F.Cu', '--pair'], 0,
                  ["D_N: following D_P at a 0.35 mm pitch", "coupled "]))
    sh = shove_board(tmp.name)
    CASES.append(("kpcb place --shove", ['kpcb.py', sh, 'place', 'C1', '--near', 'R1', '-r', '2', '--side', 'f', '--shove'], 0,
                  ["shove: ", "move C2 +1.25,+0.00 to 7.45,1.25"]))
    CASES.append(("kpcb place --shove write", ['kpcb.py', sh, 'place', 'C1', '--near', 'R1', '-r', '2', '--side', 'f', '--shove',
                                               '-o', T('sh2.kicad_pcb')], 0,
                  ["wrote C2 -> 7.450,1.250 (1 track end(s) dragged), then C1 -> 5.250,1.250"]))
    CASES.append(("kpcb place --shove result", ['kpcb.py', T('sh2.kicad_pcb'), 'check', '--only', 'OVERLAP,EDGECLR'], 0,
                  ["0 error, 0 warn"]))
    CASES.append(("kpcb place not on a pad", ['kpcb.py', padstack_board(tmp.name), 'place', 'TP9', '--near', '3,3', '-r',
                                              '1.5', '--side', 'f', '-o', T('padstack2.kicad_pcb')], 0,
                  ["wrote TP9 -> 3.000,2.000 rot 0 F"]))
    CASES.append(("kpcb place escape", ['kpcb.py', pocket_board(tmp.name), 'place', 'TP9', '--near', '3,2', '-r', '0.5',
                                        '--side', 'f'], 2, ["boxed in: TP9.1 (GND)", "skipped: a pad there reaches neither"]))
    CASES.append(("kpcb place escape class via", ['kpcb.py', pocket_board(tmp.name, 0.58), 'place', 'TP9', '--near', '3,2',
                                                  '-r', '0.5', '--side', 'f'], 2, ["boxed in: TP9.1 (GND)"]))
    po = pour_board(tmp.name)
    CASES.append(("kpcb copper", ['kpcb.py', po, 'copper', '9,0,11,10'], 0,
                  ["copper in 9.00,0.00 .. 11.00,10.00: 2 item(s)", "T F.Cu     W", "w0.20  10.000,0.000 -> 10.000,4.400  w0"]))
    CASES.append(("kpcb edit", ['kpcb.py', po, 'edit', 'width', 'w0', '0.3', 'del', 'w5.6', 'via', 'P', '10,8', '0.6/0.3',
                                '-o', T('pe.kicad_pcb')], 0, ["width w0 -> 0.3", "del   w5.6 (segment)", "edit: 3 op(s)"]))
    CASES.append(("kpcb edit result", ['kpcb.py', T('pe.kicad_pcb'), 'copper', '9,0,11,10'], 0,
                  ["w0.30  10.000,0.000 -> 10.000,4.400", "0.60/0.30  10.000,8.000  free"]))
    CASES.append(("kpcb edit refuses", ['kpcb.py', po, 'edit', 'del', 'nothing', '-o', T('px.kicad_pcb')], 1,
                  ["matches 0 item(s); nothing written"]))
    os.makedirs(T('sc0'))                               # its own dir: tmp holds mini_project's t.kicad_pro
    import shutil as _sh
    _sh.copy(wb, T('sc0'))
    CASES.append(("kdrc scratch", ['kdrc.py', os.path.join(T('sc0'), 'writer.kicad_pcb'), 'scratch', T('scr')], 0,
                  ["board: writer.kicad_pcb", "WARN: no .kicad_pro beside the board"]))
    CASES.append(("kpcb silk logos", ['kpcb.py', silk_board(tmp.name), 'silk'], 0,
                  ["HIDDEN under U1", "[footprint layer B.Cu, art on F]", "crosses 1 F-side pad(s): R2.1",
                   "logo L1 100% under U1's body"]))
    # ---- 2026-10-19 backlog pass
    tb = typed_board(T('typed'))
    CASES.append(("kpcb route typed DRU rules", ['kpcb.py', tb, 'route', 'A', '--layers', 'F.Cu', '--width', '0.15'], 0,
                  ["-> rest   w 0.15"]))
    CASES.append(("kpcb route netclass only", ['kpcb.py', typed_board(T('untyped'), dru=False), 'route', 'A', '--layers',
                                               'F.Cu', '--width', '0.15'], 2, ["FAILED"]))
    CASES.append(("kpcb vscan", ['kpcb.py', tb, 'vscan', 'A', '4,2,6,4', '--layers', 'F.Cu'], 0,
                  ["vscan A  4,2 .. 6,4", "F.Cu: # blocked, . free for the track", "   3.00 "]))
    os.makedirs(T('mvw'))
    CASES.append(("kpcb no-project warning", ['kpcb.py', move_board(T('mvw')), 'movecheck', 'R1', '10.5', '10'], 0,
                  ["WARN: no .kicad_pro beside the board - kpcb's netclasses and clearances use"]))
    sb = stair_board(T('typed'))
    CASES.append(("kpcb tracks census", ['kpcb.py', sb, 'tracks'], 0,
                  ["corners 46-90 deg         6 visible", "fix: smooth --corners", "pad end (smooth --square)"]))
    CASES.append(("kpcb smooth", ['kpcb.py', sb, 'smooth', '-o', T('typed/s2.kicad_pcb')], 0,
                  ["chains: 3, re-laid 2", "5->2 segs", "wrote "]))
    CASES.append(("kpcb diff", ['kpcb.py', T('typed/s2.kicad_pcb'), 'diff', sb], 0,
                  ["tracks: ", "series R of the changed tracks", "identical: footprints (4)"]))
    CASES.append(("kpcb widen", ['kpcb.py', sb, 'widen', 'S'], 0, ["widen: 5 track(s)", "0.15 -> 0.2"]))
    CASES.append(("kpcb widen --uniform-power", ['kpcb.py', uniform_board(T('uni')), 'widen', '--uniform-power'], 0,
                  ["widen: 1 track(s)", "4 width step(s) left mid-run in power classes", "P    ", "1 -> 1.5"]))
    CASES.append(("kpcb loops", ['kpcb.py', sb, 'loops'], 0, ["loops: 1 redundant chain(s) on 1 net(s)", "L "]))
    CASES.append(("kpcb loops -o", ['kpcb.py', sb, 'loops', '-o', T('typed/s3.kicad_pcb')], 0,
                  ["loops: 3 item(s) selected", "-> "]))
    dn = dense_board(T('typed'))
    CASES.append(("kpcb fanout", ['kpcb.py', dn, 'fanout', 'U2'], 0,
                  ["0 routed, 1 escape on their layer (S 1), 1 need a dogbone via, 1 boxed in",
                   "U2.2       B                    boxed", "U2.3       V                    via     dogbone via 0.6/0.3",
                   "U2.1       E                    escape  F.Cu out S"]))
    CASES.append(("kpcb tracks detour", ['kpcb.py', dn, 'tracks', '--kind', 'detour'], 0,
                  ["detours (> 1.5x MST)      1 visible", "26.0 vs MST 16.0 mm, 0 via reroute D"]))
    CASES.append(("kpcb reroute", ['kpcb.py', dn, 'reroute', '-o', T('typed/dn2.kicad_pcb')], 0,
                  ["26.0 mm 0 via(s) (MST 16.0)  ->   16.0 mm 0 via(s)  REROUTED", "1 net(s) re-laid"]))
    CASES.append(("kpcb reroute result", ['kpcb.py', T('typed/dn2.kicad_pcb'), 'tracks', '--kind', 'detour'], 0,
                  ["detours (> 1.5x MST)      0 visible"]))
    CASES.append(("kpcb congest pending", ['kpcb.py', dn, 'congest'], 0,
                  ["congest: 3 unrouted link(s) over the copper on the board", "no cell over 50%"]))
    CASES.append(("kpcb congest hotspot", ['kpcb.py', channel_board(T('typed')), 'congest', '--cell', '1'], 0,
                  ["6 unrouted link(s)", "region(s) over 50%, worst first", "nets: N", "over capacity on every layer"]))
    gb = graze_board(T('typed'))
    CASES.append(("kpcb tracks pad ends", ['kpcb.py', gb, 'tracks', '--kind', 'pad'], 0,
                  ["ends at a pad edge        3 visible  (1 more end inside their pad off centre", "0.35 mm off TP7.1",
                   "fix: smooth --pads"]))
    CASES.append(("kpcb loops keeps a pad's only entry", ['kpcb.py', gb, 'loops', 'K'], 0, ["loops: no redundant chain"]))
    CASES.append(("kpcb smooth --pads", ['kpcb.py', gb, 'smooth', '--pads', '-o', T('typed/g2.kicad_pcb')], 0,
                  ["pads: 3 of 3 trace end(s)", "TP12.1", "wrote "]))
    CASES.append(("kpcb smooth --pads result", ['kpcb.py', T('typed/g2.kicad_pcb'), 'copper', '0,0,10,6'], 0,
                  ["1.000,1.000 -> 7.800,1.000", "8.000,4.000", "4.700,5.200 -> 5.000,5.500"]))
    CASES.append(("kpcb view --tracks", ['kpcb.py', sb, 'view', 'TP3', 'TP4', '--tracks', '--net', 'S', '-o',
                                         T('st.png')], 0, ["tracks/pads/vias, no fills", "S green"]))
    db = drag_board(tmp.name)
    CASES.append(("kpcb move drag layers+via", ['kpcb.py', db, 'move', 'R1', '10.5', '10', '-o', T('drag2.kicad_pcb')], 0,
                  ["dragged 2 track end(s)", "moved 1 via(s) inside its pads with it"]))
    CASES.append(("kpcb movecheck dragged segment", ['kpcb.py', db, 'movecheck', 'R1', '10', '10.5'], 2,
                  ["NEW   dragged A F.Cu", "from a C track on F.Cu"]))
    CASES.append(("kpcb movecheck --no-drag", ['kpcb.py', db, 'movecheck', 'R1', '10', '10.5', '--no-drag'], 0,
                  ["--no-drag (Move, M)", "left behind: 1 track end(s) on its pads, 1 via(s) in them", "CLEAR: nothing new"]))
    CASES.append(("kpcb move --no-drag", ['kpcb.py', db, 'move', 'R1', '10', '10.5', '--no-drag', '-o', T('drag3.kicad_pcb')], 0,
                  ["1 track end(s) on its pads stay behind (--no-drag)"]))
    CASES.append(("kpcb move --no-drag result", ['kpcb.py', T('drag3.kicad_pcb'), 'diff', db], 0,
                  ["moved    R1 +0.000,+0.500", "identical: vias (1)"]))
    dv = _tab_board(_typed_dir(T('dv')), 't.kicad_pcb', 14, 10,     # A via 0.2 into TP1, a B.Cu run to TP2 (B.Cu)
                    _tab_fp('TP1', 5, 5, [('1', 0, 0, .6, .6, 'A')])
                    + _tab_fp('TP2', 10, 5, [('1', 0, 0, .6, .6, 'A')]).replace('"F.Cu"', '"B.Cu"')
                    + _tvia(5.25, 5, 'A').replace('(uuid "', '(uuid "dv1', 1) + _tseg(5.25, 5, 10, 5, 'A', 0.3, 'B.Cu')
                    + _tseg(5.25, 5, 5.25, 5.1, 'A', 0.3, 'In1.Cu') + _tseg(5.25, 5.1, 5.3, 5.15, 'A', 0.3, 'In1.Cu'))
    CASES.append(("kpcb edit dragvia auto", ['kpcb.py', dv, 'edit', 'dragvia', 'dv1', 'auto', '-o', T('dv/d2.kicad_pcb')], 0,
                  ["dragvia dv1", "-> 5.45,5 (0.20 mm): 1 track(s) dragged, 2 in-via sliver(s) folded away"]))
    CASES.append(("kpcb edit dragvia stub", ['kpcb.py', dv, 'edit', 'dragvia', 'dv1', '5.8,5', '-o', T('dv/d3.kicad_pcb')], 0,
                  ["-> 5.8,5 (0.55 mm)", "stub 0.3 mm F.Cu from TP1.1"]))
    CASES.append(("kpcb edit dragvia refuses a pad", ['kpcb.py', dv, 'edit', 'dragvia', 'dv1', '5.3,5', '-o', T('dv/d4.kicad_pcb')], 1,
                  ["its drill still reaches into a pad's mask opening; nothing written"]))
    pb = prune_board(tmp.name)
    json.dump({'violations': [{'type': 'clearance', 'items': [{'uuid': 't1'}]}]}, open(T('prune.json'), 'w'))
    CASES.append(("kpcb ripup prune stays local", ['kpcb.py', pb, 'ripup', '--drc', T('prune.json')], 1,
                  ["ripup: 1 item(s) selected, 0 more pruned as dangling"]))
    CASES.append(("kpcb unhook", ['kpcb.py', pb, 'unhook', 'C1'], 1,
                  ["unhook: 3 item(s) selected, 0 more pruned",
                   "CUT  TP5.1 now off G's other 1 pad(s): its only link ran through C1", "add -o OUT"]))
    fz = faceted_board(tmp.name)
    CASES.append(("kpcb edit unround", ['kpcb.py', fz, 'edit', 'unround', 'all', 'unround', 'zb', '-o', T('faceted2.kicad_pcb')], 0,
                  ["unround za fatten P: 26 -> 8 pts, outline +0.200 -0.000 mm2", "unround all: 1 zone(s)",
                   "unround zb hand: no arc runs"]))
    CASES.append(("kpcb edit unround again", ['kpcb.py', T('faceted2.kicad_pcb'), 'edit', 'unround', 'za', '-o',
                                              T('faceted3.kicad_pcb')], 0, ["za fatten P: already (smoothing fillet)"]))
    CASES.append(("kpcb tracks zonepts", ['kpcb.py', fz, 'tracks', '--kind', 'zonepts'], 0,
                  ["zones hard to edit        1 visible", "'fatten P' 26 corners, 24 edge(s) < 0.3 mm"]))
    dz = _tab_board(tmp.name, 'dupzone.kicad_pcb', 20, 10, _gzone('F.Cu', rect(2, 2, 10, 8), None, 'hand', net='P', uid='zh')
                    + _gzone('F.Cu', rect(3, 3, 9, 7), None, 'route P', net='P', uid='zr'))
    CASES.append(("kpcb tracks zonedup", ['kpcb.py', dz, 'tracks', '--kind', 'zonedup'], 0,
                  ["zones over own-net zone    1 visible", "'route P' 100% inside 'hand' delete the script band 'route P'"]))
    dz2 = _tab_board(tmp.name, 'dupzone2.kicad_pcb', 20, 10, _gzone('F.Cu', rect(3, 3, 9, 7), None, 'hand', net='P', uid='zh')
                     + _gzone('F.Cu', rect(2, 2, 10, 8), None, 'route P', net='P', uid='zr'))
    CASES.append(("kpcb tracks zonedup keeps a band round his zone", ['kpcb.py', dz2, 'tracks', '--kind', 'zonedup'], 0,
                  ["'hand' 100% inside 'route P' keep both: band 'route P' reaches 24.0 mm2 past 'hand'"]))
    CASES.append(("kpcb edit del teardrop", ['kpcb.py', pb, 'edit', 'del', 'td1', '-o', T('prune2.kicad_pcb')], 0,
                  ["1 teardrop(s) of deleted tracks dropped"]))
    CASES.append(("kpcb custom pad copper", ['kpcb.py', custom_pad_board(tmp.name), 'net', 'N'], 0,
                  ["copper  2 PIECES"]))
    srb = silk_review_board(tmp.name)
    CASES.append(("kpcb silk review", ['kpcb.py', srb, 'silk'], 0,
                  ["R1's designator over R1.1", "U1's designator under its own body",
                   "R2's designator at 180 deg reads upside down", "R2's designator 0.8/0.1 mm under the 1/0.15 minimum"]))
    sab = silk_art_board(tmp.name)
    CASES.append(("kpcb silk art/ttf/body", ['kpcb.py', sab, 'silk'], 0,
                  ["U5's designator under its own body", "board text 'AB' over board art",
                   "board text 'TTF' TrueType at 1.2 mm: glyph stroke under the 0.15 mm minimum (needs >= ~1.3 mm)"]))
    CASES.append(("kpcb pinlabels", ['kpcb.py', sab, 'pinlabels', 'J1', '-o', T('silkart/pl.kicad_pcb')], 0,
                  ["pinlabels J1: 1=VCC 2=VBAT", "F side, -x of the pads at ", "wrote 2 text(s)"]))
    CASES.append(("kpcb pinlabels result", ['kpcb.py', T('silkart/pl.kicad_pcb'), 'silk', 'J1'], 0, ["'VBAT'", "'VCC'"]))
    CASES.append(("kpcb label --near", ['kpcb.py', sab, 'label', 'PWR', '--near', 'J1'], 0,
                  ["label 'PWR' F at ", "J1's designator hidden"]))
    CASES.append(("kpcb silk list", ['kpcb.py', srb, 'silk', 'R1'], 0, ["R1 designator", "(pad"]))
    ub = utext_board(tmp.name)
    CASES.append(("kpcb silk list user texts", ['kpcb.py', ub, 'silk', '--box', '0,0,30,20'], 0,
                  ["JP1 user             '1'", "[ut1]", "1.5/0.2 knockout  [gtKO]"]))
    CASES.append(("kpcb edit user text/knockout/retext/addtext",
                  ['kpcb.py', ub, 'edit', 'text', 'ut1', '7.5,10', 'keep', 'del', 'ut3', 'knockout', 'gtKO', 'off',
                   'retext', 'gtKO', 'OK', 'knockout', 'JP1', 'on', 'addtext', 'GPIO', '20,15', '0', 'f', '1.2',
                   '-o', T('utext/u2.kicad_pcb')], 0,
                  ["del   ut3 (footprint user text)", "retext gtKO -> 'OK'", "addtext 'GPIO' 20,15 0 deg F.SilkS 1.2 mm"]))
    CASES.append(("kpcb edit refuses a designator by uuid", ['kpcb.py', ub, 'edit', 'del', 'ut', '-o', T('utext/u3.kicad_pcb')],
                  1, ["matches 2 board/footprint text(s); nothing written"]))
    CASES.append(("kpcb diff silk restyle", ['kpcb.py', T('utext/u2.kicad_pcb'), 'diff', ub], 0,
                  ["visible text changed on 1 footprint(s) (1 of them did not move)", "JP1: Reference knockout on, user moved, user removed",
                   "1 board text(s) restyled in place", "board text 'OK': text knockout off"]))
    CASES.append(("kpcb refdes", ['kpcb.py', srb, 'refdes', '-o', T('silkrev/t2.kicad_pcb')], 0,
                  ["refdes: 3 of 3 designator(s) get a clear spot", "wrote "]))
    CASES.append(("kpcb refdes result", ['kpcb.py', T('silkrev/t2.kicad_pcb'), 'silk', '--only',
                                         'SILK-PAD,SILK-UNDER,SILK-ORIENT,SILK-SIZE'], 0, ["silk 0 error, 0 warn, 0 info"]))
    CASES.append(("kpcb edit silk ops", ['kpcb.py', srb, 'edit', 'ref', 'R1', '5,3.5', '0', 'hide', 'R2', 'textsize',
                                         'U1', '1.2', '0.18', '-o', T('silkrev/t3.kicad_pcb')], 0,
                  ["ref   R1 5,3.5 0", "hide  R2", "edit: 3 op(s)"]))
    CASES.append(("kpcb edit value ops", ['kpcb.py', srb, 'edit', 'value', 'R1', '5,7', '0', 'hide-value', 'R2',
                                          'valuesize', 'U1', '0.8', '0.12', '-o', T('silkrev/t4.kicad_pcb')], 0,
                  ["value R1 5,7 0", "hide-value R2", "valuesize U1 0.8 0.12", "edit: 3 op(s)"]))
    CASES.append(("kpcb view --layers silk", ['kpcb.py', srb, 'view', 'R1', '--layers', 'silk', '-o', T('silk.png')], 0,
                  ["F silk from the model"]))
    CASES.append(("kfront draw powertree", ['kfront.py', os.path.join(tmp.name, 'pwr', 'pwr.net'), 'draw', 'powertree',
                                            '-o', T('pt.kicad_sch')], 0, ["kfront draw powertree: ", "graphic item(s)"]))
    CASES.append(("kfront draw i2c", ['kfront.py', os.path.join(tmp.name, 'i2c', 'i2c.net'), 'draw', 'i2c',
                                      '-o', T('i2c.kicad_sch')], 0, ["kfront draw i2c: "]))
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
                  ["IN SYNC", "[INFO] SYNCART", "(3): REF** REF**~dup2 REF**~dup3", "[WARN] SYNCART",
                   "REF**~dup2 (Logo at 10.0,5.0 F) lacks board_only: Update PCB deletes it",
                   "REF**~dup3 (Logo at 15.0,5.0 B) lacks board_only"]))
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
    CASES.append(("ksheet labelspot", ['ksheet.py', l2net, 'labelspot', 'GND'], 0,
                  ["label 'GND' on GND:", "lint2.kicad_sch      at 50 50.8 270  justify right bottom  clear 2.54 mm",
                   '(label "GND" (at 50 50.8 270)']))
    CASES.append(("ksheet labelspot none", ['ksheet.py', l2net, 'labelspot', '/A', 'X'], 1, ["no wire of it"]))
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
    # ---- 2026-10-27 backlog pass
    CASES.append(("kpcb smooth --overlaps keeps a via's centred end", ['kpcb.py', ovia_board(tmp.name), 'smooth', '--overlaps'],
                  0, ["overlaps: 0 deleted, 0 cut back"]))
    CASES.append(("kpcb smooth --overlaps deletes the doubled seg", ['kpcb.py', ovia_board(tmp.name, False), 'smooth',
                                                                       '--overlaps'], 0, ["overlaps: 1 deleted"]))
    CASES.append(("kpcb view --tracks two nets", ['kpcb.py', sb, 'view', 'TP3', 'TP4', '--tracks', '--net', 'S', '--net', 'L',
                                                  '-o', T('st2.png')], 0, ["S L green"]))
    CASES.append(("kpcb pairs stem", ['kpcb.py', rb, 'pairs', 'D_'], 0, ["pairs: 1 diff pair(s)"]))
    pc = prec_board(tmp.name)
    CASES.append(("kpcb copper full precision", ['kpcb.py', pc, 'copper', '9,5,11,6'], 0,
                  ["10.12345,5.500", "2.000,5.500 -> 10.12345,5.500"]))
    CASES.append(("kpcb edit seg snaps to a via centre", ['kpcb.py', pc, 'edit', 'seg', 'A', 'F.Cu', '0.2', '10.1239,5.5',
                                                          '10.1239,8', '-o', T('prec2.kicad_pcb')], 0, ["(snapped onto the centre"]))
    CASES.append(("kpcb edit seg snapped", ['kpcb.py', T('prec2.kicad_pcb'), 'copper', '9,5,11,9'], 0,
                  ["10.12345,5.500 -> 10.1239,8.000"]))
    CASES.append(("kpcb edit text keep shifts the render_cache", ['kpcb.py', sab, 'edit', 'text', 'gtRC', '6,18', 'keep',
                                                                  '-o', T('silkart/rc2.kicad_pcb')], 0, ["text  gtRC -> 6,18 keep"]))
    CASES.append(("kpcb diff vias + box -v", ['kpcb.py', T('pe.kicad_pcb'), 'diff', po, '--box', '9,0,11,10', '-v'], 0,
                  ["added   P 0.6/0.3 10.000,8.000  free", "a SUM over them", "- F.Cu    w0.2  10.0000,5.6000", "~ F.Cu    w0.2->0.3"]))
    CASES.append(("kpcb tracks --box", ['kpcb.py', sb, 'tracks', '--box', '0,0,10,9'], 0, ["tracks in 0,0,10,9: "]))
    CASES.append(("kpcb widen --uniform", ['kpcb.py', sliver_board(tmp.name), 'widen', '--uniform', '--width', '0.01'], 0,
                  ["widen: 1 track(s)", "0.15 -> 0.3"]))
    CASES.append(("kpcb stitch --sparse", ['kpcb.py', sparse_board(tmp.name), 'stitch', 'GND', '--sparse', '5'], 0,
                  ["stitch GND --sparse 5", "via 0.6/0.3 at", "pour > 5 mm from a via:", "(dry run: -o OUT writes them, free)"]))
    stb = sparse_tht_board(tmp.name)
    CASES.append(("kpcb stitch --sparse THT anchor", ['kpcb.py', stb, 'stitch', 'GND', '--sparse', '5'], 0,
                  ["for 14.0 mm at 15.25,0.50", "worst 14.0 -> 4.7 mm"]))
    CASES.append(("kpcb stitch off a THT ring", ['kpcb.py', stb, 'stitch', 'GND', '28.5,5', '-r', '1.5', '--n', '4'], 0,
                  ["near 28.50,5.00: 28.50,6.30 (1.30 mm), 28.50,3.65 (1.35 mm)"]))
    mb = micro_board(tmp.name)
    CASES.append(("kpcb smooth --micro", ['kpcb.py', mb, 'smooth', '--micro', '-o', os.path.join(tmp.name, 'micro', 'm2.kicad_pcb')], 0,
                  ["micro: 1 segment(s) < 0.05 mm merged into a neighbour, 1 deleted (under other copper), 0 left"]))
    CASES.append(("kpcb smooth --micro result", ['kpcb.py', os.path.join(tmp.name, 'micro', 'm2.kicad_pcb'), 'tracks'], 0,
                  ["segments < 0.05 mm        0 visible", "tracks: 3 segments, 18.0 mm"]))
    pib = pwr_inpour_board(tmp.name)
    CASES.append(("kpcb tracks inpourpwr", ['kpcb.py', pib, 'tracks', '--kind', 'inpourpwr'], 0,
                  ["plane tracks inside fill    0 visible", "other tracks inside fill    1 visible",
                   "ripup PWR --inpour (opt-in: may it rely on its fill?)"]))
    CASES.append(("kpcb ripup NET --inpour non-plane", ['kpcb.py', pib, 'ripup', 'PWR', '--inpour', '-o',
                                                       os.path.join(tmp.name, 'pwrin2.kicad_pcb')], 0,
                  ["inpour: 1 track(s) wholly inside their own saved fill", "1 track(s) + 0 via(s) on 1 net(s)"]))
    CASES.append(("kpcb ripup --inpour leaves non-plane", ['kpcb.py', pib, 'ripup', '--inpour'], 0,
                  ["inpour: 0 plane-net track(s)"]))
    CASES.append(("kpcb rf branch", ['kpcb.py', rfbranch_board(tmp.name), 'rf', 'RFX', '--freq', '5000'], 2,
                  ["branch off F.Cu track @10.00,5.00: 3.00 mm, 18 deg at 5000 MHz, ends in JP1.1 DNP  <-- OPEN STUB > lambda/50 (1.2 mm)"]))
    db = dead_board(tmp.name)
    CASES.append(("kpcb view --by-net", ['kpcb.py', db, 'view', '--box', '0,0,20,10', '--by-net', '-o',
                                         os.path.join(tmp.name, 'bynet.svg')], 0,
                  ["F.Cu/B.Cu by net, fills included, GND grey", "largest: P "]))
    CASES.append(("kpcb tracks deadend", ['kpcb.py', db, 'tracks', '--kind', 'deadend'], 0,
                  ["dead branches (no pad)    2 visible", "F.Cu/B.Cu 16.2 mm, 2 via, 6 mm2 pour off F.Cu track; ripup --deadend",
                   "df1 df2 db2 db1", "zone zband", "F.Cu 1.5 mm          off TP2.1; ripup --deadend", "cut live"]))
    CASES.append(("kpcb check DEADCU", ['kpcb.py', db, 'check', '--only', 'DEADCU'], 0,
                  ["[WARN] DEADCU", "P F.Cu 1.5 mm off TP2.1 @16.00,5.00 serves no pad - `ripup --deadend P`"]))
    CASES.append(("kpcb net dead branch", ['kpcb.py', db, 'net', 'P'], 0,
                  ["dead    branch F.Cu/B.Cu 16.2 mm, 2 via, 6 mm2 pour off F.Cu track @6.00,5.00: serves no pad"]))
    CASES.append(("kpcb ripup --deadend", ['kpcb.py', db, 'ripup', '--deadend', '-o', os.path.join(tmp.name, 'dead2.kicad_pcb')], 0,
                  ["deadend: 2 branch(es) serving no pad, 5 track(s), 2 via(s), 1 whole zone(s)",
                   "6 item(s) deleted (0 more pruned as dangling), 1 cut back, 1 zone(s)"]))
    CASES.append(("kpcb ripup --deadend result", ['kpcb.py', os.path.join(tmp.name, 'dead2.kicad_pcb'), 'tracks', '--kind', 'deadend'], 0,
                  ["dead branches (no pad)    0 visible"]))
    CASES.append(("kpcb unhook leaves no cycle", ['kpcb.py', db, 'unhook', 'TP4', '-o', os.path.join(tmp.name, 'dead3.kicad_pcb')], 0,
                  ["unhook: 1 item(s) selected, 5 more pruned as dangling", "4 trk  2 via"]))
    CASES.append(("kpcb fatten/stitch skip dead", ['kpcb.py', db, 'fatten', 'P', '--layers', 'B.Cu', '--minw', '0.2', '--width', '2', '-o',
                                                    os.path.join(tmp.name, 'dead4.kicad_pcb')], 0,
                  ["fatten P: 4 track(s) serve no pad, left out", "fatten P B.Cu: 1 track(s) >= 0.2 mm -> 1 zone(s)"]))
    CASES.append(("kpcb refslots", ['kpcb.py', refslot_board(tmp.name), 'refslots'], 0,
                  ["refslots: 1 signal net(s)", "S                          F.Cu/In1  gnd  10.", "void/pad/via", ", X 0.8"]))
    ib = inpour_board(tmp.name)
    CASES.append(("kpcb tracks inpour", ['kpcb.py', ib, 'tracks', '--kind', 'inpour'], 0,
                  ["plane tracks inside fill    2 visible", "ripup --inpour"]))
    gs = _tab_board(tmp.name, 'gstub.kicad_pcb', 20, 10, _tab_fp('TP1', 6, 5, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))
                    + _tseg(6, 5, 11, 5, 'GND', 0.3) + _gzone('F.Cu', rect(.5, .5, 19.5, 9.5), rect(.5, .5, 8, 9.5)))
    CASES.append(("kpcb tracks deadend plane stub", ['kpcb.py', gs, 'tracks', '--kind', 'deadend'], 0,
                  ["dead branches (no pad)    1 visible", "F.Cu 5.0 mm          off TP1.1; ripup --deadend"]))
    CASES.append(("kpcb ripup --inpour", ['kpcb.py', ib, 'ripup', 'GND', '--inpour'], 0,
                  ["inpour: 2 plane-net track(s) wholly inside their own saved fill, 1 run(s) kept", "(feeds TP1.1)",
                   "(dry run: 1 track(s)"]))
    CASES.append(("kpcb route leaves a stale pour's pad to the refill", ['kpcb.py', stale_pour_board(tmp.name), 'route', 'GND'],
                  0, ["GND: TP2.1 - left to the pour: TP2.1 sits in its F.Cu zone 'GND' and 4 spoke(s) land"]))
    jn = join_board(tmp.name)
    CASES.append(("kpcb tracks overlap", ['kpcb.py', jn, 'tracks', '--kind', 'overlap'], 0,
                  ["joined only through pad    1 visible", "two runs only through P1.1"]))
    CASES.append(("kpcb smooth --overlaps merge", ['kpcb.py', jn, 'smooth', '--overlaps', '-o', T('join/j2.kicad_pcb')], 0,
                  ["merge: 1 pair(s) of runs joined only through a pad", "wrote "]))
    CASES.append(("kpcb smooth --overlaps merge result", ['kpcb.py', T('join/j2.kicad_pcb'), 'tracks', '--kind', 'overlap'], 0,
                  ["joined only through pad    0 visible"]))
    jg = _tab_board(_typed_dir(T('joing')), 't.kicad_pcb', 20, 10,   # both runs near P1's top edge: one straight
                    _tab_fp('TP1', 4, 5.55, [('1', 0, 0, .6, .6, 'N')], (.4, .4))   # run 0.55 off its centre would only graze it
                    + _tab_fp('TP2', 16, 5.55, [('1', 0, 0, .6, .6, 'N')], (.4, .4)) + _tab_fp('P1', 10, 5, [('1', 0, 0, 1.0, 1.4, 'N')], (.6, .8))
                    + _tseg(4, 5.55, 9.8, 5.55, 'N', 0.3) + _tseg(10.2, 5.55, 16, 5.55, 'N', 0.3))
    CASES.append(("kpcb smooth --overlaps merge refuses a graze", ['kpcb.py', jg, 'smooth', '--overlaps', '-o', T('joing/j2.kicad_pcb')], 0,
                  ["merge: 0 pair(s) of runs joined only through a pad"]))
    mk = _tab_board(_typed_dir(T('mk')), 't.kicad_pcb', 16, 10,     # a 40 um side-step between 2 mm legs: either merge tilts one
                    _tab_fp('TP1', 6, 2, [('1', 0, 0, .6, .6, 'K')]) + _tab_fp('TP2', 10, 2.04, [('1', 0, 0, .6, .6, 'K')])
                    + _tseg(6, 2, 8, 2, 'K') + _tseg(8, 2, 8, 2.04, 'K') + _tseg(8, 2.04, 10, 2.04, 'K'))
    CASES.append(("kpcb smooth --micro keeps 0/45/90", ['kpcb.py', mk, 'smooth', '--micro', '-o', T('mk/m2.kicad_pcb')], 0,
                  ["micro: 0 segment(s) < 0.05 mm merged into a neighbour, 0 deleted (under other copper), 1 left",
                   "tilts one off 0/45/90"]))
    vc = _tab_board(tmp.name, 'viacut.kicad_pcb', 20, 10,          # the strap overshoots a live via 0.1 off its line
                    _tab_fp('TP1', 2, 5, [('1', 0, 0, 1, 1, 'P')], (.6, .6)) + _tab_fp('TP2', 16, 5.1, [('1', 0, 0, 1, 1, 'P')], (.6, .6))
                    + _tseg(2, 5, 10, 5, 'P', 0.3) + _tvia(8, 5.1, 'P') + _tseg(8, 5.1, 14, 5.1, 'P', 0.3, 'B.Cu')
                    + _tvia(14, 5.1, 'P') + _tseg(14, 5.1, 16, 5.1, 'P', 0.3))
    CASES.append(("kpcb ripup --deadend stubs onto an off-line via", ['kpcb.py', vc, 'ripup', '--deadend', '-o', T('viacut2.kicad_pcb')], 0,
                  ["ends in a via off its line: to 8,5 + a 0.3 mm stub onto the centre 8,5.1 (clear)",
                   "1 cut back (1 with a stub onto its via)"]))
    vb = _tab_board(_typed_dir(T('viacutb')), 't.kicad_pcb', 20, 10,   # a Q track 0.35 past the via centre blocks the stub
                    _tab_fp('TP1', 2, 5, [('1', 0, 0, 1, 1, 'P')], (.6, .6)) + _tab_fp('TP2', 16, 5.1, [('1', 0, 0, 1, 1, 'P')], (.6, .6))
                    + _tseg(2, 5, 10, 5, 'P', 0.3) + _tvia(8, 5.1, 'P') + _tseg(8, 5.1, 14, 5.1, 'P', 0.3, 'B.Cu')
                    + _tvia(14, 5.1, 'P') + _tseg(14, 5.1, 16, 5.1, 'P', 0.3) + _tseg(7, 5.45, 9, 5.45, 'Q', 0.2))
    CASES.append(("kpcb ripup --deadend leaves a blocked stub", ['kpcb.py', vb, 'ripup', '--deadend', '-o', T('viacutb/v2.kicad_pcb')], 0,
                  ["1 track(s) reported, not cut", "0 cut back"]))
    ip2 = _tab_board(tmp.name, 'inpour2.kicad_pcb', 20, 10,        # a via-to-via pair in the fill goes as one run; a leg whose
                     _tab_fp('TP1', 17, 5, [('1', 0, 0, .6, .6, 'GND')], (.4, .4))   # rest runs on out of the fill stays
                     + _tvia(3, 5, 'GND') + _tseg(3, 5, 6, 5, 'GND', 0.3) + _tseg(6, 5, 6, 8, 'GND', 0.3) + _tvia(6, 8, 'GND')
                     + _tvia(10, 5, 'GND') + _tseg(10, 5, 13, 5, 'GND', 0.3) + _tseg(13, 5, 17, 5, 'GND', 0.3)
                     + _gzone('F.Cu', rect(.5, .5, 19.5, 9.5), rect(.5, .5, 14, 9.5)))
    CASES.append(("kpcb ripup --inpour guards", ['kpcb.py', ip2, 'ripup', 'GND', '--inpour'], 0,
                  ["inpour: 3 plane-net track(s) wholly inside their own saved fill, 1 run(s) kept",
                   "(the rest would end loose @13.00,5.00)", "(dry run: 2 track(s)"]))
    sp2 = stubpad_board(tmp.name)
    CASES.append(("kpcb smooth keeps a pad a chain ran through", ['kpcb.py', sp2, 'smooth', '-o', T('stubpad/s2.kicad_pcb')],
                  0, ["chains: "]))
    CASES.append(("kpcb smooth keeps a pad: result", ['kpcb.py', T('stubpad/s2.kicad_pcb') if True else sp2, 'net', 'N'], 0,
                  ["copper  one piece joins all 2 pad(s)"]))
    tw = twin_board(tmp.name)
    CASES.append(("kpcb tracks twin", ['kpcb.py', tw, 'tracks', '--kind', 'twin'], 0,
                  ["twin runs into one pad    1 visible", "smooth --twins"]))
    CASES.append(("kpcb smooth --twins", ['kpcb.py', tw, 'smooth', '--twins', '-o', T('twin/t2.kicad_pcb')], 0,
                  ["twins: 1 pair(s)", "to R1.1: ", "-> 3.0", "to 13.00,4.75"]))
    CASES.append(("kpcb smooth --jogs partial", ['kpcb.py', jog_board(T('part'), True), 'smooth', '--jogs',
                                                 '-o', os.path.join(T('part'), 'nudge', 'j4.kicad_pcb')], 0,
                  ["jogs: 1 collapsed", "of the way (the jog is now"]))
    for label, argv, want_exit, needles in CASES:
        code, out = run(argv)
        prob = []
        if code != want_exit:
            prob.append(f"exit {code} != {want_exit}")
        prob += [f"missing {n!r}" for n in needles if n not in out]
        if label in ("ksch render", "knet draw multi-pin verify") and ("ERROR" in out or "overlaps" in out):
            prob.append("ERROR line in render output")
        if label == "kpcb sync art" and "REF** (Logo" in out:
            prob.append("the board_only logo was warned as deletable")
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
    rc2 = open(os.path.join(tmp.name, 'silkart', 'rc2.kicad_pcb')).read() if os.path.exists(
        os.path.join(tmp.name, 'silkart', 'rc2.kicad_pcb')) else ''
    fails += check('(render_cache "RC"' in rc2 and '(xy 6 16.5) (xy 8 16.5)' in rc2.replace('\n\t\t\t\t\t', ' ')
                   or '(xy 6 16.5)' in rc2 and '(xy 8 18)' in rc2,
                   "edit text keep: a TrueType text's render_cache moves with it (5..7 -> 6..8), not dropped")
    t4 = open(os.path.join(tmp.name, 'silkrev', 't4.kicad_pcb')).read()
    fails += check('(property "Value" "VR1"\n\t\t\t(at 0 2 0)' in t4 and t4.count('(hide yes)') == 1,
                   "edit value: R1's Value at local 0,2 (board 5,7), R2's hidden, nothing else")
    # a sheet saved after the .net triggers kmerge; one that fails keeps the old
    # netlist and says so, next to the stale warning
    code, out = run(['knet.py', mini, 'summary'], {'KREVIEW_NO_REGEN': '', 'KICAD_CLI': 'false'})
    ok5 = 'auto-regenerate of t.net failed' in out and '1.0 h older than t.kicad_sch' in out
    fails += check(ok5, "stale netlist auto-regenerate (failure path)")
    # writers: a flip twice, and a move+rotate and back, give each footprint back byte
    # for byte; a flipped copy reads every pad mirrored in x about its footprint
    sys.path.insert(0, S)
    import kpcb_board, kpcb_edit
    kpcb_board.Board.warn_rules = lambda self: None              # fixtures have no project rules; the CLI case checks it
    import kpcb_freebox
    ab = kpcb_board.Board(sab)
    rcb = next(x['box'] for x in ab.silktexts if x['text'] == 'RC')
    og, ox, oy = kpcb_freebox.occupancy(ab, False)
    fails += check(rcb == (5.0, 16.5, 7.0, 18.0) and og[int((15 - oy) / 0.25)][int((19 - ox) / 0.25)] == 1,
                   f"TTF render_cache is the text box {rcb}; freebox counts P1's SMD pad outside its courtyard")
    import kpcb_zones, kpcb_smooth, kfront
    clr = kpcb_zones._clearance_fn(kpcb_board.Board(os.path.join(tmp.name, 'typed', 't.kicad_pcb')))[0]
    got = (clr('A', 'X'), clr('A', 'X', 'track', 'pad', 'F.Cu'), clr('A', 'W', 'track', 'track', 'F.Cu'),
           clr('RF1', 'X', 'track', 'pad', 'F.Cu'), clr('A', 'X', 'via', 'pad', 'F.Cu'))
    fails += check(got == (0.15, 0.1, 0.127, 0.3, 0.15),
                   f"typed DRU clearance (netclass, pad-track, outer track-track, RF class kept, via-pad) = {got}")
    fails += check(kpcb_smooth.selftest() == 'smooth geometry ok', "smooth geometry: staircase, chamfer, connectors, pad distance")
    cov = os.path.join(tmp.name, 'cover.kicad_sch')
    cell = lambda t, x, y, pg: (f'\n\t\t\t(table_cell "{t}"\n\t\t\t\t(at {x} {y} 0)\n\t\t\t\t(effects\n\t\t\t\t\t(href "#{pg}")'
                                f'\n\t\t\t\t)\n\t\t\t\t(uuid "u{t}{y}")\n\t\t\t)')         # noqa: E731
    open(cov, 'w').write('(kicad_sch\n\t(table\n\t\t(column_count 2)\n\t\t(row_heights 6 6)\n\t\t(cells'
                         + cell('PAGE', 10, 20, 1) + cell('SHEET', 22, 20, 1) + cell('1', 10, 26, 1) + cell('Main', 22, 26, 1)
                         + '\n\t\t)\n\t)\n)\n')
    kfront.write_table(cov, [('1', 'Main'), ('2', 'Front')], cov + '2')
    t2 = open(cov + '2').read()
    fails += check(re.findall(r'table_cell "([^"]*)"', t2) == ['PAGE', 'SHEET', '1', 'Main', '2', 'Front']
                   and '(row_heights 6 6 6)' in t2 and '(at 10 32 0)' in t2 and '(href "#2")' in t2,
                   "kfront pages -o: a missing row added below the last, href and row height with it")
    wb2 = os.path.join(tmp.name, 'w2.kicad_pcb')                 # fixlines output, above
    bb, tt = kpcb_board.Board(wb2), kpcb_edit.PcbText(wb2)
    G = lambda f: type('G', (), {'ref': f.ref, 'x': f.x + 1, 'y': f.y, 'rot': f.rot + 90})()   # noqa: E731
    ok7 = all(kpcb_edit.flip_text(kpcb_edit.flip_text(tt.text(f.uuid), f), f) == tt.text(f.uuid) and
              kpcb_edit.place_text(kpcb_edit.place_text(tt.text(f.uuid), f, f.x + 1, f.y, f.rot + 90), G(f),
                                   f.x, f.y, f.rot) == tt.text(f.uuid) for f in bb.fps.values())
    for f in bb.fps.values():
        tt.sub(f.uuid, kpcb_edit.flip_text(tt.text(f.uuid), f))
    tt.save(os.path.join(tmp.name, 'wflip.kicad_pcb'))
    fb = kpcb_board.Board(os.path.join(tmp.name, 'wflip.kicad_pcb'))
    ok7 &= all(abs(q['x'] - (2 * f.x - p['x'])) < 1e-9 and abs(q['y'] - p['y']) < 1e-9 and fb.fps[r].back != f.back
               for r, f in bb.fps.items() for p, q in zip(f.pads, fb.fps[r].pads))
    fails += check(ok7, "writers: flip / move+rotate round trips exact, flipped pads mirrored")
    # router: a GND plane is passable at a cost, another net's pour (a power island) is
    # copper - a route through it would split it (parsnip's PPHV went 3 -> 5 pieces)
    import kpcb_route
    pp = os.path.join(tmp.name, 'pours.kicad_pcb')
    sq = lambda x0, y0, x1, y1: f'(pts (xy {x0} {y0}) (xy {x1} {y0}) (xy {x1} {y1}) (xy {x0} {y1}))'   # noqa: E731
    open(pp, 'w').write('(kicad_pcb (version 20240108) (generator "pcbnew")'
                        ' (layers (0 "F.Cu" signal) (2 "B.Cu" signal) (25 "Edge.Cuts" user))'
                        ' (gr_rect (start 0 0) (end 20 10) (layer "Edge.Cuts"))'
                        f' (zone (net "GND") (layer "F.Cu") (polygon {sq(0, 0, 10, 10)}) (filled_polygon (layer "F.Cu") {sq(.5, .5, 9.5, 9.5)}))'
                        f' (zone (net "P") (layer "F.Cu") (polygon {sq(12, 3, 16, 7)}) (filled_polygon (layer "F.Cu") {sq(12, 3, 16, 7)})))')
    g = kpcb_route.Grid(kpcb_board.Board(pp), 'A', (0, 0, 20, 10), 0.1, 0.2, 0.6, 0.3, ['F.Cu', 'B.Cu'])
    at = lambda x, y: int(round(y / 0.1)) * g.W + int(round(x / 0.1))                                   # noqa: E731
    ok8 = (not g.blk['F.Cu'][at(5, 5)] and g.pour['F.Cu'][at(5, 5)] and g.blk['F.Cu'][at(14, 5)]
           and not g.blk['B.Cu'][at(14, 5)] and g.vpour[at(14, 5)] and not g.vpour[at(5, 5)]
           and not g.vblk[at(14, 5)])
    fails += check(ok8, "route: GND plane passable at a cost, a power-island pour blocks tracks, costs a via")
    # place: an L-shaped courtyard is two boxes, not its bounding box
    import kpcb_place
    Lb = kpcb_place._bands([[(-2, -2), (2, -2), (2, 0), (0, 0), (0, 2), (-2, 2), (-2, -2)]], 0.1)
    okL = sorted(Lb) == [(-2.0, -2.0, 2.0, 0.0), (-2.0, 0.0, 0.0, 2.0)]
    fails += check(okL, f"place: an L courtyard tests as 2 boxes {Lb}")
    # end-sunk rod bound: 3 A in 0.3 x 0.035 x 1.5 mm = I^2*rho*L^2/(8*k*A^2) ~ 1.0 C
    import kpcb_amp
    r = kpcb_amp.dt_rod(3.0, 0.3, 0.035, 1.5)
    ok6 = 0.95 < r < 1.1 and kpcb_amp.dt_ipc(kpcb_amp.ipc_current(0.3, 0.035, True, 10), 0.3, 0.035, True) - 10 < 1e-6
    fails += check(ok6, f"ampacity rise model: rod {r:.2f} C, IPC inverse round-trip")
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
    # the regen root is <pro>.kicad_sch, not a front-matter page listed first (exports empty)
    pr = os.path.join(tmp.name, 'proot')
    os.makedirs(pr)
    open(os.path.join(pr, 'p.kicad_pro'), 'w').write(
        '{"schematic": {"top_level_sheets": [{"filename": "front.kicad_sch"}, {"filename": "p.kicad_sch"}]}}')
    for f in ('front', 'p'):
        open(os.path.join(pr, f + '.kicad_sch'), 'w').close()
    ok8 &= kcommon.project_root(pr) == 'p.kicad_sch' and kcommon.project_root(pages) == 'b.kicad_sch'
    fails += check(ok8, "cache_prune drops oldest/whole dirs; kmerge escaped round trip; project_root")
    # SLOT: the voids board's SIG track crosses a 10 x 10 box on the F.Cu GND plane
    import kpcb_zones, kpcb_board
    vb = kpcb_board.Board(os.path.join(tmp.name, 'voids.kicad_pcb'))
    sl = kpcb_zones._slots(vb, 'F.Cu', 'GND', (5, 5, 15, 15))
    ok7 = len(sl) == 1 and sl[0][0] == 'SIG' and abs(sl[0][1][0] - 10) < 1e-6 and sl[0][1][2] == 'x=12.0' \
        and not kpcb_zones._slots(vb, 'F.Cu', 'GND', (0, 0, 20, 20), min_len=19)
    fails += check(ok7, f"zones SLOT measure {sl}")
    # touches() with tol > cell/2 must scan the middle grid cell too (0.10 mm from the edge)
    sqi = kpcb_board.PolyIndex([(0.55, 0.55), (0.65, 0.55), (0.65, 0.95), (0.55, 0.95)])
    fails += check(sqi.touches((0.75, 0.75), 0.3) and not sqi.touches((0.75, 0.75), 0.05),
                   "PolyIndex.touches scans every cell inside tol")
    # a 1 mm band of Q's own pour across a 17 mm Q track between the old 0/.25/.5/.75/1 samples,
    # no outline vertex on the track: one piece (+3V3 In2, 2026-10-27)
    import kpcb_copper
    bnd = kpcb_board.Board(_tab_board(tmp.name, 'band.kicad_pcb', 20, 10, _tab_fp('TP1', 1, 5, [('1', 0, 0, .6, .6, 'Q')])
                                      + _tab_fp('TP2', 18, 5, [('1', 0, 0, .6, .6, 'Q')]) + _tseg(1, 5, 18, 5, 'Q', 0.3)
                                      + _gzone('F.Cu', rect(3, 3, 4, 7), rect(3, 3, 4, 7), 'band', net='Q', uid='zq')))
    fails += check(len(kpcb_copper.Copper(bnd, 'Q').pieces()) == 1, "Copper joins a fill crossing a track mid-span")
    # 1 x 1 roundrect pads (r 0.25), 0.1 mm tracks: Q's end 0.03 past the corner diagonally is
    # 0.146 off the rounded copper (the old box grown by w/2 joined it); R's end 0.04 off the
    # straight edge has its cap on the pad
    rr = lambda s: s.replace('smd rect', 'smd roundrect').replace('(layers "F.Cu")', '(layers "F.Cu")\n\t\t\t(roundrect_rratio 0.25)')  # noqa: E731
    rnd = kpcb_board.Board(_tab_board(tmp.name, 'round.kicad_pcb', 20, 10, rr(
        _tab_fp('TP1', 5, 3, [('1', 0, 0, 1, 1, 'Q')]) + _tab_fp('TP2', 5, 7, [('1', 0, 0, 1, 1, 'R')]))
        + _tab_fp('TP3', 15, 3, [('1', 0, 0, .6, .6, 'Q')]) + _tab_fp('TP4', 15, 7, [('1', 0, 0, .6, .6, 'R')])
        + _tseg(15, 3, 5.53, 2.47, 'Q', 0.1) + _tseg(15, 7, 5.54, 7, 'R', 0.1)))
    fails += check(len(kpcb_copper.Copper(rnd, 'Q').pieces()) == 2 and len(kpcb_copper.Copper(rnd, 'R').pieces()) == 1,
                   "Copper joins a cap on a roundrect's edge, not one past its rounded corner")
    # a 0.2 track along x + y = 11.1 passes 0.071 off TP1's corner (5.5,5.5): no end on TP1, its
    # centreline misses, its edge overlaps 29 um. KiCad joins it (CC2 / C33.1, 2026-10-28)
    clb = kpcb_board.Board(_tab_board(tmp.name, 'clip.kicad_pcb', 12, 10, _tab_fp('TP1', 5, 5, [('1', 0, 0, 1, 1, 'Q')])
                                      + _tab_fp('TP2', 4.1, 7, [('1', 0, 0, .6, .6, 'Q')]) + _tseg(4.1, 7, 7, 4.1, 'Q', 0.2)))
    cq = kpcb_copper.Copper(clb, 'Q')
    co = cq.clip_only()
    fails += check(len(cq.pieces()) == 1 and len(co) == 1 and co[0][1].ref == 'TP1' and abs(co[0][3] - 0.0293) < 1e-3,
                   "Copper joins a track body clipping a pad's corner; clip_only names it")
    import random
    rg = random.Random(7)
    pads = [p for f in kpcb_board.Board(PCB).fps.values() for p in f.pads] + [p for f in rnd.fps.values() for p in f.pads]
    agree = all(kpcb_copper.pad_within(p, pt, d, r) == (kpcb_copper.pad_dist(p, pt, r) <= d)
                for p in pads for _ in range(40)
                for pt in [(p['x'] + rg.uniform(-2, 2), p['y'] + rg.uniform(-2, 2))] for d in (-0.1, 0, 0.3)
                for r in (False, True))
    fl1, fl2, ns = {'pts': [(0, 0), (1, 0), (1, 1)]}, {'pts': [(5, 5), (6, 5), (6, 6)]}, type('B', (), {})()
    i1 = kpcb_board._pidx(ns, fl1)
    ns._pidx[id(fl2)] = (fl1, i1)                    # a freed fill's id now naming another fill
    fails += check(agree and kpcb_board._pidx(ns, fl2) is not i1 and (5.5, 5.2) in kpcb_board._pidx(ns, fl2),
                   "pad_within agrees with pad_dist; _pidx never serves a reused id's index")
    # viapad: a 0.3 drill 0.6 off a 1 x 1 roundrect (r 0.25) reaches 0.05 in past the edge; one
    # 0.55,0.55 off sits past the rounded corner (the old box test took it); -0.05 mask shrinks it
    import kpcb_viapad
    vp = {'x': 0, 'y': 0, 'prot': 0, 'sx': 1, 'sy': 1, 'shape': 'roundrect', 'rr': 0.25, 'prims': [], 'mask': 0}
    dd = lambda x, y, m=0: kpcb_viapad.drill_depth({**vp, 'mask': m}, {'x': x, 'y': y, 'drill': 0.3})   # noqa: E731
    fails += check(abs(dd(0.6, 0) - 0.05) < 1e-9 and dd(0.55, 0.55) < 0 and dd(0.6, 0, -0.05) < 1e-9
                   and kpcb_viapad.protection({'tenting': (1, 1), 'covering': (0, 0), 'plugging': (1, 1),
                                               'capping': False, 'filling': False})[0] == 'IV-b mask-plugged',
                   "viapad: drill vs the real pad shape + mask margin; tent + plug both sides = IV-b")
    # drop_layer: an F+B zone, F joining TP1/TP2, its B fill one fragment on a via joined by F:
    # B can go. A B.Cu pad TP3 in a second B fragment (via to F): B carries a pad, so no
    sq = lambda pts: ''.join(f'\t\t\t\t(xy {x} {y})\n' for x, y in pts)   # noqa: E731
    fz = lambda ly, pts: f'\t\t(filled_polygon\n\t\t\t(layer "{ly}")\n\t\t\t(pts\n{sq(pts)}\t\t\t)\n\t\t)\n'   # noqa: E731
    mz = lambda extra: ('\t(zone\n\t\t(net "Z")\n\t\t(layers "F.Cu" "B.Cu")\n\t\t(uuid "zz")\n\t\t(name "Z")\n'   # noqa: E731
                        f'\t\t(polygon\n\t\t\t(pts\n{sq(rect(1, 1, 19, 9))}\t\t\t)\n\t\t)\n'
                        + fz('F.Cu', rect(1, 1, 19, 9)) + fz('B.Cu', rect(8, 3, 10, 7)) + extra + '\t)\n')
    zb = _tab_fp('TP1', 3, 5, [('1', 0, 0, 1, 1, 'Z')]) + _tab_fp('TP2', 17, 5, [('1', 0, 0, 1, 1, 'Z')]) + _tvia(9, 5, 'Z')
    dz1 = kpcb_board.Board(_tab_board(tmp.name, 'drop1.kicad_pcb', 20, 10, zb + mz('')))
    dz2 = kpcb_board.Board(_tab_board(tmp.name, 'drop2.kicad_pcb', 20, 10, zb + _tvia(13, 5, 'Z')
                                      + _tab_fp('TP3', 15, 5, [('1', 0, 0, 1, 1, 'Z')]).replace('"F.Cu"', '"B.Cu"')
                                      + mz(fz('B.Cu', rect(12, 3, 17, 7)))))
    fails += check(kpcb_copper.drop_layer(dz1, 'zz', 'B.Cu') == ['F.Cu'] and kpcb_copper.drop_layer(dz2, 'zz', 'B.Cu') is None
                   and kpcb_copper.drop_layer(dz1, 'zz', 'F.Cu') is None,
                   "drop_layer: a zone's B fill joining only an F-joined via can go; not with a pad on it")
    # census on the drawn pad: runs A/B into P1 (1 x 1 roundrect, r 0.25) joined only through it,
    # plus a dead-end sliver ending 12 um past the corner arc (a box called it inside): a bridge
    import kpcb_smooth
    pj = kpcb_board.Board(_tab_board(tmp.name, 'pj.kicad_pcb', 20, 10, rr(_tab_fp('P1', 10, 5, [('1', 0, 0, 1, 1, 'N')]))
                                     + _tab_fp('TP1', 5, 5, [('1', 0, 0, .6, .6, 'N')])
                                     + _tab_fp('TP2', 15, 5, [('1', 0, 0, .6, .6, 'N')])
                                     + _tseg(5, 5, 9.8, 5, 'N') + _tseg(10.2, 5, 15, 5, 'N') + _tseg(10, 5.2, 10.4355, 5.4355, 'N')))
    rows = kpcb_smooth.pad_joins(pj, set())
    fails += check(len(rows) == 1 and rows[0][2] == 'P1.1' and len(rows[0][6]) == 1,
                   f"pad_joins on the drawn roundrect: a sliver capped past the corner arc is a bridge {[(r[2], len(r[6])) for r in rows]}")
    tk = lambda a, b, w: {'a': a, 'b': b, 'w': w, 'mid': ((a[0] + b[0]) / 2, (a[1] + b[1]) / 2)}   # noqa: E731
    fat = tk((0, 0), (5, 0), 3)                      # a 0.1 mm leg at 90 deg under its 1.5 mm end cap: hidden
    fails += check(kpcb_smooth.leg_in_cap(tk((5, 0), (5, 0.1), 0.2), fat)
                   and not kpcb_smooth.leg_in_cap(tk((5, 0), (5, 1.5), 0.2), fat)
                   and not kpcb_smooth.leg_in_cap(fat, tk((5, 0), (5, 0.1), 0.2)),
                   "leg_in_cap: a stub under a fat end cap, not one poking out")
    import kpcb_route
    win = (-1, -1, 6, 4)                             # straddles the outline corner
    gf = kpcb_route.Grid(rnd, 'Q', win, 0.05, 0.1, 0.6, 0.3, ['F.Cu'])
    gt = kpcb_route.Grid(rnd, 'Q', win, 0.05, 0.1, 0.6, 0.3, ['F.Cu'], vias=False)
    try:
        gt.vblk[0]
        rd = False
    except TypeError:
        rd = True                                    # reading the skipped raster fails loudly
    fails += check(gf.blk == gt.blk and any(gf.blk['F.Cu']) and rd,
                   "Grid(vias=False) paints the same track raster, its via raster unreadable")
    # a raster trace's near-45 end goes on the 0/45/90 grid lines; a 30 deg edge keeps its angle
    import kpcb_fillet
    q1 = kpcb_fillet.octilinear([(0, 0), (10, 0), (10.98, 1.0), (11, 5), (0, 5)])
    q2 = kpcb_fillet.octilinear([(0, 0), (4, 0), (5.732, 1), (0, 1)])
    fails += check(sorted(q1) == [(0, 0), (0, 5), (10, 0), (11, 1), (11, 5)] and (5.732, 1.0) in q2,
                   f"octilinear: 45.6/89.7 deg edges snapped {q1}, 30 deg kept {q2}")
    # rf's microstrip Zo: 0.36 mm on 0.203 mm / er 4.4 / 35 um is ~49.7 ohm (hand-computed)
    sys.path.insert(0, S)
    import kzo, kpcb_height
    z = kzo.microstrip(0.36, 0.203, 4.4, 0.035)[0]
    ok = 49.0 < z < 51.0
    fails += check(ok, f"microstrip Zo {z:.1f} ohm")
    # rf's CPWG field solver vs exact stripline / CPW and Hammerstad microstrip,
    # and side grounds can only LOWER Zo (the closed forms got that backwards)
    zs = kzo._selftest()
    ms = kzo.field_zo(0.306, 0.203, 4.4, 0.035)[0]
    cp = [kzo.field_zo(0.306, 0.203, 4.4, 0.035, s=g)[0] for g in (0.15, 0.3, 1.0)]
    ok4 = all(abs(z - r) / r < 0.02 for _, z, r in zs) and cp[0] < cp[1] < cp[2] < ms
    fails += check(ok4, "field solver " + ', '.join(f"{(z - r) / r * 100:+.1f}%" for _, z, r in zs)
          + f"; CPWG {cp[0]:.1f} < {cp[1]:.1f} < {cp[2]:.1f} < microstrip {ms:.1f}")
    # `height`'s glTF transform: 90 deg about Y (x,y,z,w quaternion) then +2 in Y
    M = kpcb_height._mat({'rotation': [0, 0.7071068, 0, 0.7071068], 'translation': [0, 2, 0]})
    p = [sum(M[r][c] * v for c, v in enumerate((1, 0, 0, 1))) for r in range(3)]
    ok2 = all(abs(x - y) < 1e-6 for x, y in zip(p, (0, 2, -1)))
    fails += check(ok2, f"glTF node transform {[round(x, 3) for x in p]}")
    # kdoc: a plain pattern crosses separator/dash variants (but a leading space still
    # anchors), and a filename or path addresses its doc
    import kdoc, argparse
    ok3 = bool(kdoc.smart_re('keep-out').search('antenna keepout zone')
               and kdoc.smart_re('-40').search('−40 C')
               and not kdoc.smart_re(' EN').search('ENABLE')
               and kdoc._fkey('docs/datasheets/max17320.pdf') == kdoc._fkey('max17320'))
    fails += check(ok3, "kdoc pattern folding + -d path")
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
    # a replaced source with fewer pages drops the old slot's extra pages
    xa = os.path.join(tmp.name, 'kda', 'X1.txt')
    open(xa, 'w').write('x\n' * 450)
    os.utime(xa, (1, 1))
    with contextlib.redirect_stderr(io.StringIO()):
        n3 = len(kdoc.pages(kdoc.extract(xa)))
        open(xa, 'w').write('y\n')
        n1 = len(kdoc.pages(kdoc.extract(xa)))
    ok14 &= (n3, n1) == (3, 1)
    fails += check(ok14, f"kdoc same-basename slots {os.path.basename(ka)}, {os.path.basename(kb)}; -p sets; "
          "a replaced source drops stale pages")
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
    fails += check(ok15, f"parse_args dash values {pn.args} --net {pn.net}; lock guard")
    # ksheet page order: depth-first reads A(1) C(3) B(2); the navigator lists B first.
    # _xf rotates before it mirrors: at rot 270 + mirror x, lib +x points sheet up
    import ksheet
    po = ksheet.page_order(pages)[0]
    ok16 = [x['rule'] for x in po] == ['PAGEORDER', 'NAVORDER'] and '1 /A/, 3 /A/C/, 2 /B/' in po[0]['msg'] \
        and kcommon._xf(1, 0, 270, 'x') == (0, -1) and kcommon._xf(1, 0, 90, '') == (0, -1)
    fails += check(ok16, f"ksheet PAGEORDER/NAVORDER {[x['rule'] for x in po]}; _xf rotate-then-mirror")
    # duplicate refs: every footprint block becomes one Board entry, and a padless
    # logo's side is its art layer, not its (layer)
    db = kpcb_board.Board(dp)
    ok8 = len(db.fps) == open(dp).read().count('(footprint ') == 4 \
        and {'REF**', 'REF**~dup2', 'REF**~dup3'} <= set(db.fps) and not db.fps['REF**~dup3'].back
    fails += check(ok8, f"Board keeps duplicate refs {sorted(db.fps)}")
    # `height` with no models loaded: R1 is UNKNOWN, the padless logos are not parts
    kpcb_height.heights = lambda b: ({}, [])
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        kpcb_height.c_height(db, argparse.Namespace(args=[], height_cfg={}, max=8))
    nm = [x for x in buf.getvalue().splitlines() if 'NO 3D MODEL' in x]
    ok9 = len(nm) == 1 and nm[0].endswith(': R1')
    fails += check(ok9, f"height skips padless art: {nm}")
    # a horizontal C's value and the ref of an NTC one 3-unit row below must not
    # touch (glyphs: 0.35 above the baseline, 0.075 below), and ksch says so
    import ksch
    kd = ksch.Doc({})
    kd.parse('c C1 0,0 h 10nF\nntc T1 0,3 hr 10k\nnote 0,6 "a long note running right"\nnote 4,6 x')
    cv = [f[2] for f in kd.syms['C1'].fields() if f[-1] == 'val'][0]
    tr = [f[2] for f in kd.syms['T1'].fields() if f[-1] == 'ref'][0]
    ow = [m for _sv, m in ksch.check_doc(kd) if 'overlaps' in m]
    ok11 = tr - .35 > cv + .075 and len(ow) == 1 and "'x'" in ow[0]
    fails += check(ok11, f"ksch row-pitch text: C1 value {cv:.2f}, T1 ref {tr:.2f}; {ow}")
    # an ic's top pin name sits inside its body, clear of the first left pin's name
    pr, _pn, bb = ksch.s_ic({'L': [('2', 'ENABLE')], 'T': [('1', 'VCC_RF')], 'B': [('3', 'GND')]})
    tt = [q for q in pr if q[0] == 't']
    tn = next(q for q in tt if q[3] == 'VCC_RF')
    w = ksch.twid('VCC_RF', tn[4])
    t0, t1 = (tn[2], tn[2] + w) if tn[5] == 'end' else (tn[2] - w, tn[2])
    ln = next(q for q in tt if q[3] == 'ENABLE')
    bn = next(q for q in tt if q[3] == 'GND')
    ok12 = 0 < t0 and t1 < ln[2] - .35 and bn[2] < bb[3] and bn[2] - ksch.twid('GND', bn[4]) > ln[2]
    fails += check(ok12, f"ksch ic top/bottom pin names inside the body: VCC_RF y {t0:.2f}..{t1:.2f}, "
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
    fails += check(ok17, f"ksch junction rule {j}, ic value above body, TVS pin map, text-over-wire")
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
    fails += check(ok18, f"ksch lib-symbol geometry 1:1 {lbb}, stretched R span {spr['2'][1]}")
    # kverify's sheet summary: a uniform GUI move reads as one offset, renumbering apart
    import kverify
    va = '(sheet_instances (path "/" (page "4"))) (at 10 20) (xy 1 2) (start 0 0)'
    vb = '(sheet_instances (path "/" (page "6"))) (at 7.46 20) (xy -1.54 2) (start -2.54 0)'
    vs = kverify.delta_summary(va, vb)
    ok19 = vs == 'page 4 -> 6; 3 coordinates moved, all by (-2.54,+0)' \
        and kverify.delta_summary(va, va.replace('(at', '(foo (at')).startswith('structure changed')
    fails += check(ok19, f"kverify sheet delta: {vs}")
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
    fails += check(ok13, f"kpcb view crop + plot: {vo.strip()[-70:]}")
    # the copper graph must not depend on the hash seed (set order)
    tp = tee_board(tmp.name)
    rs = {run(['kpcb.py', tp, 'ampacity', '--from', 'J1.1', '--to', 'J2.1', '--amps', '1'],
              {'PYTHONHASHSEED': str(sd)})[1].split('   R ')[1].split(' mohm')[0] for sd in range(8)}
    ok10 = rs == {'8.27'}
    fails += check(ok10, f"copper graph independent of hash seed: R {sorted(rs)} mohm")
    n = len(CASES) + len(CHECKS)
    print(f"\n{n - fails}/{n} passed")
    return 1 if fails else 0

if __name__ == '__main__':
    if sys.argv[1:2] == ['--lint']:           # command names in other docs (CLAUDE.md, agents)
        probs = [b for f in sys.argv[2:] for b in bad_calls(open(f).read(), f)[1]]
        print('\n'.join(probs) or f"lint: every tool command named in {len(sys.argv) - 2} file(s) exists")
        sys.exit(1 if probs else 0)
    if sys.argv[1:2] == ['--golden'] and len(sys.argv) == 5:
        sys.exit(golden(*sys.argv[2:5]))
    sys.exit(main())
