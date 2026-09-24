#!/usr/bin/env python3
"""Merge the netlists of several root schematics into one .net.

parsnip keeps battery.kicad_sch and usb_interface.kicad_sch as their own
top-level sheets rather than as hierarchical sheets. Stable kicad-cli (10.0.x)
exports one root at a time, so no single export sees the whole board; this runs
an export per root and splices the results into one netlist that knet.py and
kpcb.py can read. kicad-cli-nightly (10.99) already exports every top-level
sheet from the first root: a root whose sheet is already in an earlier export is
skipped, and a complete export is written through verbatim.

    kmerge.py [--if-stale] OUT.net ROOT.kicad_sch [[NAME=]ROOT2.kicad_sch ...]

The other top-level sheets listed in the .kicad_pro (KiCad 10
schematic.top_level_sheets) are added automatically, and NAME defaults to the
name recorded there, so `kmerge.py OUT.net parsnip.kicad_sch` is enough.
The kicad-cli binary is picked per file (kcommon.kicad_cli, $KICAD_CLI wins).

Net scoping follows what kicad-cli already encodes in the name:

    GND, +3V3, I2C_HOST_SDA   no leading slash -> power symbol or global label,
                              shared across roots, so nodes merge by name
    /EN, /Rails/5V_RAW        leading slash -> local to that root's hierarchy,
                              so it is renamed /<root name>/EN, the same path
                              pcbnew puts on the board pads
    Net-(C6-Pad2)             auto-generated from a refdes, unique board-wide

Refdes collisions between roots are a hard error: two parts with one designator
cannot both reach the board.
"""

import argparse
import datetime
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from kcommon import kicad_cli, sch_files, top_level_sheets  # noqa: E402

# ---------------------------------------------------------------- s-expressions


def parse(text):
    """Return a nested list. Atoms stay strings; quoted strings keep a \0 marker
    so re-serialising can tell "1" (a string) from 1 (a bare token)."""
    out, stack, i, n = [], [], 0, len(text)
    while i < n:
        c = text[i]
        if c == "(":
            new = []
            (stack[-1] if stack else out).append(new)
            stack.append(new)
            i += 1
        elif c == ")":
            stack.pop()
            i += 1
        elif c == '"':
            j, buf = i + 1, []
            while j < n and text[j] != '"':
                if text[j] == "\\":
                    buf.append(text[j + 1])
                    j += 2
                else:
                    buf.append(text[j])
                    j += 1
            (stack[-1] if stack else out).append("\0" + "".join(buf))
            i = j + 1
        elif c.isspace():
            i += 1
        else:
            j = i
            while j < n and not text[j].isspace() and text[j] not in "()\"":
                j += 1
            (stack[-1] if stack else out).append(text[i:j])
            i = j
    return out[0] if len(out) == 1 else out


def dump(node, indent=0):
    pad = "  " * indent
    if isinstance(node, str):
        if node.startswith("\0"):
            return '"%s"' % node[1:].replace("\\", "\\\\").replace('"', '\\"')
        return node
    if not node:
        return "()"
    head = node[0]
    simple = all(isinstance(x, str) for x in node)
    if simple:
        return "(" + " ".join(dump(x) for x in node) + ")"
    parts = [dump(head)]
    for child in node[1:]:
        parts.append("\n" + pad + "  " + dump(child, indent + 1))
    return "(" + " ".join(parts[:1]) + "".join(parts[1:]) + "\n" + pad + ")"


def kids(node, tag):
    return [c for c in node if isinstance(c, list) and c and c[0] == tag]


def kid(node, tag):
    got = kids(node, tag)
    return got[0] if got else None


def val(node, tag):
    """First value of (tag value), with the quoted-string marker stripped."""
    c = kid(node, tag)
    if not c or len(c) < 2 or not isinstance(c[1], str):
        return None
    return c[1][1:] if c[1].startswith("\0") else c[1]


def q(s):
    return "\0" + s


# ---------------------------------------------------------------- export


def export(root, workdir):
    out = os.path.join(workdir, os.path.basename(root) + ".net")
    cli = kicad_cli(root)
    r = subprocess.run(
        [cli, "sch", "export", "netlist", "--format", "kicadsexpr", "-o", out, root],
        capture_output=True, text=True)
    if r.returncode != 0 or not os.path.exists(out):
        sys.exit("%s failed on %s:\n%s%s" % (cli, root, r.stdout, r.stderr))
    text = open(out).read()
    return parse(text), text


# ---------------------------------------------------------------- merge


def scope(name, prefix):
    """Rename a hierarchy-local net so two roots cannot collide on it.

    prefix "/" leaves it untouched (the export already scoped it, or the
    project names no top-level sheets); "/Charger/" turns "/EN" into
    "/Charger/EN"."""
    if prefix == "/" or not name.startswith("/"):
        return name
    return prefix.rstrip("/") + name


def merge(roots):
    """roots: [(label, path)], primary first. Returns (tree or verbatim text,
    stats, n_comps, n_nets, n_sheets)."""
    comps, libparts, libraries = [], {}, {}
    nets = {}          # merged name -> list of node nodes
    order = []         # merged name, first-seen order
    design_sheets = []  # (name, tstamps) in page order
    seen_ref = {}
    stats = []
    first_text, renamed = None, False

    for idx, (label, root) in enumerate(roots):
        stem = label or os.path.splitext(os.path.basename(root))[0]
        if label and any(nm.startswith("/%s/" % label) for nm, _ in design_sheets):
            stats.append((stem, None, None))    # nightly: already in the first export
            continue
        with tempfile.TemporaryDirectory() as td:
            top, text = export(root, td)
        first_text = first_text if idx else text
        sheets = kids(kid(top, "design") or [], "sheet")
        # stable exports a root as "/" and its children as "/Rails/"; nightly
        # already writes "/Root/Rails/". Only the former needs the root's name.
        scoped = bool(sheets) and (val(sheets[0], "name") or "/") != "/"
        prefix = "/" if scoped or not label else "/%s/" % label
        renamed |= prefix != "/"

        # design sheet records: what knet's summary groups by
        for sh in sheets:
            nm = val(sh, "name") or "/"
            design_sheets.append((scope(nm, prefix), val(sh, "tstamps") or "/"))

        block = kid(top, "components")
        n_c = 0
        for comp in (kids(block, "comp") if block else []):
            ref = val(comp, "ref")
            if ref in seen_ref:
                sys.exit("refdes collision: %s is in both %s and %s"
                         % (ref, seen_ref[ref], stem))
            seen_ref[ref] = stem
            # record which root it came from, so `sheets` stays meaningful
            sheetpath = kid(comp, "sheetpath")
            if sheetpath is not None and prefix != "/":
                names = kid(sheetpath, "names")
                if names and len(names) > 1 and isinstance(names[1], str):
                    cur = names[1][1:] if names[1].startswith("\0") else names[1]
                    names[1] = q(scope(cur, prefix))
            comps.append(comp)
            n_c += 1

        for lp in kids(kid(top, "libparts") or [], "libpart"):
            libparts.setdefault((val(lp, "lib"), val(lp, "part")), lp)
        for lb in kids(kid(top, "libraries") or [], "library"):
            libraries.setdefault(val(lb, "logical"), lb)

        n_n = 0
        for net in kids(kid(top, "nets") or [], "net"):
            name = scope(val(net, "name") or "", prefix)
            if name not in nets:
                nets[name] = []
                order.append(name)
            nets[name].extend(kids(net, "node"))
            n_n += 1
        stats.append((stem, n_c, n_n))

    # one export already held everything under its own names: keep it byte-exact
    if not renamed and sum(1 for st in stats if st[1] is not None) == 1:
        return first_text, stats, len(comps), len(order), len(design_sheets)

    merged_nets = ["nets"]
    for code, name in enumerate(order, 1):
        merged_nets.append(["net", ["code", q(str(code))], ["name", q(name)]] + nets[name])

    design = ["design", ["source", q(os.path.abspath(roots[0][1]))],
              ["date", q(datetime.datetime.now().isoformat(timespec="seconds"))],
              ["tool", q("kmerge.py")]]
    for n, (nm, ts) in enumerate(design_sheets, 1):
        design.append(["sheet", ["number", q(str(n))], ["name", q(nm)],
                       ["tstamps", q(ts)]])

    top = ["export", ["version", q("E")],
           design,
           ["components"] + comps,
           ["libparts"] + list(libparts.values()),
           ["libraries"] + list(libraries.values()),
           merged_nets]
    return top, stats, len(comps), len(order), len(design_sheets)


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--if-stale", action="store_true",
                    help="do nothing if OUT is newer than every .kicad_sch beside the "
                         "roots (the SessionStart hook uses this)")
    ap.add_argument("out")
    ap.add_argument("roots", nargs="+", metavar="[NAME=]ROOT.kicad_sch",
                    help="first root is primary; NAME defaults to the .kicad_pro "
                         "top_level_sheets name, else the filename stem (the primary "
                         "stays unscoped when the project names no top-level sheets)")
    a = ap.parse_args()

    d = os.path.dirname(os.path.abspath(a.roots[0].rpartition("=")[2]))
    tls = dict(top_level_sheets(d))
    roots = []
    for i, spec in enumerate(a.roots):
        label, _, path = spec.rpartition("=")
        if not os.path.exists(path):
            sys.exit("no such schematic: %s" % path)
        base = os.path.basename(path)
        roots.append((label or tls.get(base)
                      or ("" if i == 0 else os.path.splitext(base)[0]), path))
    given = {os.path.basename(p) for _, p in roots}
    roots += [(nm, os.path.join(d, fn)) for fn, nm in tls.items() if fn not in given]

    if a.if_stale and os.path.exists(a.out):
        t = os.path.getmtime(a.out)
        if all(os.path.getmtime(f) <= t for f in sch_files(d)):
            return

    top, stats, n_comp, n_net, n_sheet = merge(roots)
    tmp = "%s.%d" % (a.out, os.getpid())
    with open(tmp, "w") as fh:
        fh.write(top if isinstance(top, str) else dump(top) + "\n")
    os.replace(tmp, a.out)          # a knet reading meanwhile never sees half a netlist

    for stem, c, n in stats:
        if c is None:
            print("  %-22s (already in the %s export)" % (stem, stats[0][0]))
        else:
            print("  %-22s %4d comps  %4d nets" % (stem, c, n))
    print("  %-22s %4d comps  %4d nets  %d sheets -> %s"
          % ("MERGED", n_comp, n_net, n_sheet, a.out))


if __name__ == "__main__":
    main()
