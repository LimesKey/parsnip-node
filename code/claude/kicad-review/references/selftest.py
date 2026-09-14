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
"""
import os, sys, subprocess, tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
SK   = os.path.dirname(HERE)                       # skill root
S    = os.path.join(SK, 'scripts')
NET  = os.path.join(HERE, 'selftest.net')
PCB  = os.path.join(HERE, 'selftest.kicad_pcb')
KSCH = os.path.join(HERE, 'selftest.ksch')
TMP  = tempfile.gettempdir()
svg1 = os.path.join(TMP, 'selftest_draw.svg')
svg2 = os.path.join(TMP, 'selftest_ksch.svg')

# (label, [tool, *args], expected_exit, [required substrings in stdout+stderr])
CASES = [
    ("knet check",   ['knet.py', NET, 'check'],                 2, ["4 error, 6 warn, 1 info"]),
    ("knet divider", ['knet.py', NET, 'divider', '/VSENSE'],    0, ["1.6667 V", "1.6445 - 1.6890 V"]),
    ("knet walk",    ['knet.py', NET, 'walk', '/DANGLE'],       0, ["R2.2", "DNP"]),
    ("knet draw",    ['knet.py', NET, 'draw', 'U1', '-d', '2', '-o', svg1], 0, []),
    ("ksch render",  ['ksch.py', 'render', '-o', svg2, KSCH],   0, []),   # + no ERROR (checked below)
    ("kpcb check",   ['kpcb.py', PCB, 'check'],                 2, ["4 error, 7 warn, 2 info"]),
    ("kpcb summary", ['kpcb.py', PCB, 'summary'],               0, ["40.00 x 30.00 mm", "placed 16"]),
    ("kpcb map",     ['kpcb.py', PCB, 'map'],                   0, []),
    ("kpcb span",    ['kpcb.py', PCB, 'span'],                  0, ["1 net(s)"]),
    ("kpcb sheet",   ['kpcb.py', PCB, 'sheet'],                 0, ["/Test/", "/Test/Spare/"]),
    ("kpcb sync",    ['kpcb.py', PCB, 'sync', NET],             2, ["23 error, 7 warn"]),
    ("kpcb ic",      ['kpcb.py', PCB, 'ic'],                    1, ["no regulator-shaped part found"]),  # 1 = not found
    ("kpcb ampacity",['kpcb.py', PCB, 'ampacity', 'PWR', '--amps', '5'], 2, ["TRACE-THIN", "VIA-FEW"]),
    ("kpcb amp-par", ['kpcb.py', PCB, 'ampacity', 'PAR', '--amps', '5'], 0, ["MESH-CHECK"]),  # parallel edges: no bridge, not flagged thin
]

def run(argv):
    p = subprocess.run([sys.executable, os.path.join(S, argv[0])] + argv[1:],
                       capture_output=True, text=True)
    return p.returncode, p.stdout + p.stderr

def main():
    fails = 0
    for label, argv, want_exit, needles in CASES:
        code, out = run(argv)
        prob = []
        if code != want_exit:
            prob.append(f"exit {code} != {want_exit}")
        prob += [f"missing {n!r}" for n in needles if n not in out]
        if label == "ksch render" and "ERROR" in out:
            prob.append("ERROR line in render output")
        if prob:
            fails += 1
            print(f"FAIL  {label}: {'; '.join(prob)}")
        else:
            print(f"ok    {label}")
    print(f"\n{len(CASES) - fails}/{len(CASES)} passed")
    return 1 if fails else 0

if __name__ == '__main__':
    sys.exit(main())
