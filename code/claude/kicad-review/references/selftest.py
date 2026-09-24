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
import os, sys, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SK   = os.path.dirname(HERE)                       # skill root
S    = os.path.join(SK, 'scripts')
NET  = os.path.join(HERE, 'selftest.net')
PCB  = os.path.join(HERE, 'selftest.kicad_pcb')
PCB2 = os.path.join(HERE, 'selftest_amp2.kicad_pcb')
PCB3 = os.path.join(HERE, 'selftest_nightly.kicad_pcb')   # PCB in 10.99 (transform) form
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
    ("kdrc selftest", ['kdrc.py', '--selftest'],                0, ["6/6 passed"]),
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
    # selftest_amp2.kicad_pcb: false-positive classes from SKILL-BACKLOG.md.
    ("kpcb amp-neck", ['kpcb.py', PCB2, 'ampacity', 'NECK', '--amps', '1.0'],
     0, ["PAD-NECK"]),  # short wide stub AT a pad reads as PAD-NECK, not TRACE-THIN
    ("kpcb amp-tap",  ['kpcb.py', PCB2, 'ampacity', 'TAP', '--amps', '1.2'],
     2, ["TRACE-THIN", "near J1"]),  # real backbone bottleneck, TH2 tap branch excluded
    ("kpcb amp-alltap", ['kpcb.py', PCB2, 'ampacity', 'ALLTAP', '--amps', '1.0'],
     0, ["MIXED-NET"]),  # only bridge is a thermistor tap -> advisory, not TRACE-THIN
    ("kpcb zones-under", ['kpcb.py', PCB2, 'zones', 'U2', 'U3'], 2,
     ["100% of samples covered  (continuous)", "27% of samples covered  (MOSTLY MISSING)"]),
    # a +3V3 via dropped on U1 pad 1's centre -> one same-net via-in-pad, no mismatch
    ("kpcb viapad",  ['kpcb.py', PCB, 'viapad'],                 0,
     ["1 via(s) in 1 SMD pad(s)", "U1", "OK same net (+3V3)"]),
    ("kpcb viapad --signal", ['kpcb.py', PCB, 'viapad', '--signal'], 0, ["1 PWR / 0 SIG", "showing 0"]),
    ("kpcb where pad", ['kpcb.py', PCB, 'where', 'U1.1', 'U1.2'], 0,
     ["8.500, 8.500  (absolute)", "U1.1 -> U1.2: 4.24 mm"]),
    ("kpcb net",     ['kpcb.py', PCB, 'net', '+3V3'],            0, ["2 pad(s)", "U1.1", "1 via(s)"]),
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
    return os.path.join(d, 't.net')

GOLDEN = [['kpcb.py', '{pcb}', c] for c in ('summary', 'check', 'span', 'zones', 'rf', 'viapad', 'ic')] \
    + [['kpcb.py', '{pcb}', 'sync', '{net}']] \
    + [['knet.py', '{net}', c] for c in ('summary', 'check', 'rails', 'revpol', 'unconnected', 'bom')]

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

def run(argv):
    p = subprocess.run([sys.executable, os.path.join(S, argv[0])] + argv[1:],
                       capture_output=True, text=True)
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
    fet = os.path.join(tmp.name, 'fet.net')
    CASES.append(("knet revpol fet", ['knet.py', fet, 'revpol'], 0,
                  ["FET channel(s) on, gate driven from the cells: Q1", "FET channels vs normal: Q1 off",
                   "isolated from the cells: U1", "bidirectional TVS/ESD, breakdown not in the part number: D1"]))
    CASES.append(("knet nc-stub+stale", ['knet.py', mini, 'around', 'U1'], 0,
                  ["NC (flagged)", "floating  <-- no NC flag", "1.0 h older than t.kicad_sch",
                   "lacks top-level sheet(s) Other"]))
    for label, argv, want_exit, needles in CASES:
        code, out = run(argv)
        prob = []
        if code != want_exit:
            prob.append(f"exit {code} != {want_exit}")
        prob += [f"missing {n!r}" for n in needles if n not in out]
        if label == "ksch render" and "ERROR" in out:
            prob.append("ERROR line in render output")
        if label == "kpcb amp-tap" and "near TH2" in out:
            prob.append("thermistor tap TH2 leaked into the bottleneck (tap exclusion broke)")
        if label == "knet fppad+parpin" and "pad MP" in out:
            prob.append("MP mounting pad reported (it is meant to have no net)")
        if prob:
            fails += 1
            print(f"FAIL  {label}: {'; '.join(prob)}")
        else:
            print(f"ok    {label}")
    # rf's microstrip Zo: 0.36 mm on 0.203 mm / er 4.4 / 35 um is ~49.7 ohm (hand-computed)
    sys.path.insert(0, S)
    import kpcb
    z = kpcb.microstrip(0.36, 0.203, 4.4, 0.035)[0]
    ok = 49.0 < z < 51.0
    fails += not ok
    print(f"{'ok  ' if ok else 'FAIL'}  microstrip Zo {z:.1f} ohm")
    # rf's CPWG field solver vs exact stripline / CPW and Hammerstad microstrip,
    # and side grounds can only LOWER Zo (the closed forms got that backwards)
    import kzo
    zs = kzo._selftest()
    ms = kzo.field_zo(0.306, 0.203, 4.4, 0.035)[0]
    cp = [kzo.field_zo(0.306, 0.203, 4.4, 0.035, s=g)[0] for g in (0.15, 0.3, 1.0)]
    ok4 = all(abs(z - r) / r < 0.02 for _, z, r in zs) and cp[0] < cp[1] < cp[2] < ms
    fails += not ok4
    print(f"{'ok  ' if ok4 else 'FAIL'}  field solver " + ', '.join(f"{(z - r) / r * 100:+.1f}%" for _, z, r in zs)
          + f"; CPWG {cp[0]:.1f} < {cp[1]:.1f} < {cp[2]:.1f} < microstrip {ms:.1f}")
    # `height`'s glTF transform: 90 deg about Y (x,y,z,w quaternion) then +2 in Y
    M = kpcb._mat({'rotation': [0, 0.7071068, 0, 0.7071068], 'translation': [0, 2, 0]})
    p = [sum(M[r][c] * v for c, v in enumerate((1, 0, 0, 1))) for r in range(3)]
    ok2 = all(abs(x - y) < 1e-6 for x, y in zip(p, (0, 2, -1)))
    fails += not ok2
    print(f"{'ok  ' if ok2 else 'FAIL'}  glTF node transform {[round(x, 3) for x in p]}")
    # kdoc: a plain pattern crosses separator/dash variants (but a leading space still
    # anchors), and a filename or path addresses its doc
    import kdoc
    ok3 = bool(kdoc.smart_re('keep-out').search('antenna keepout zone')
               and kdoc.smart_re('-40').search('−40 C')
               and not kdoc.smart_re(' EN').search('ENABLE')
               and kdoc._fkey('docs/datasheets/max17320.pdf') == kdoc._fkey('max17320'))
    fails += not ok3
    print(f"{'ok  ' if ok3 else 'FAIL'}  kdoc pattern folding + -d path")
    print(f"\n{len(CASES) + 4 - fails}/{len(CASES) + 4} passed")
    return 1 if fails else 0

if __name__ == '__main__':
    if sys.argv[1:2] == ['--golden'] and len(sys.argv) == 5:
        sys.exit(golden(*sys.argv[2:5]))
    sys.exit(main())
