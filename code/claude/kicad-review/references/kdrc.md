# kdrc.py - real DRC + ERC via kicad-cli

The authoritative rules check. `kpcb.py check` and `knet.py check` are heuristics
on geometry and the netlist; `kdrc.py` runs KiCad's **own** Design Rules Check
(which applies `parsnip.kicad_dru` automatically) and Electrical Rules Check, and
folds the results into the same finding format as the other tools.

```bash
D=<skill>/scripts/kdrc.py
python3 $D parsnip.kicad_pcb            # DRC + ERC
python3 $D parsnip.kicad_pcb drc        # DRC only
python3 $D parsnip.kicad_pcb erc        # ERC only, every root schematic
```

## What it does

- **DRC** runs on the board with `--refill-zones` so results reflect current
  pours. It never passes `--save-board`: the refill is in memory, the on-disk
  `.kicad_pcb` is not modified.
- **ERC** runs once per **root** schematic. Roots are auto-discovered: every
  `*.kicad_sch` beside the board that no other schematic pulls in as a sub-sheet.
  For parsnip that is `parsnip`, `battery`, `usb_interface`. A bare single-root
  ERC misses two of them, the same blind spot as a bare netlist export.
- Output: findings grouped by rule, capped per rule with a `+N more` tail,
  suppressible via `kdrc.json`, exit 0/2/3 like `kpcb`.

## The one caveat that matters

**Per-root ERC cannot see cross-root nets.** A pin powered or driven through a
global label whose driver is on another root (`I2C_HOST_*`, `USB D+/-`, the
shared rails) reads as `power_pin_not_driven` or `pin_to_pin` here. Those are
three-root false positives. Confirm each against the merged netlist before
believing it:

```bash
python3 <skill>/scripts/knet.py parsnip-merged.net around REF
```

DRC has no such blind spot - it is one board file.

## Flags

| flag | effect |
| --- | --- |
| `--max N` | cap lines per rule (default 25, or `max` in kdrc.json) |
| `--only R1,R2` / `--skip R` | keep or drop rules by name (e.g. `DRC:CLEARANCE`) |
| `--unconnected` | include the DRC `unconnected_items` (unrouted nets; hidden by default because mid-layout there are hundreds) |
| `--parity` | add DRC schematic-parity. Noisy: it uses the single project root, so it will flag all of /Charger/ and /USB Interface/. |
| `--all` | pass `--severity-all` to kicad-cli. Here kicad-cli already ignores the `.kicad_pro` severities, so this rarely changes anything. |
| `--no-suppress` | ignore kdrc.json |
| `--rules` | print the rule legend |
| `--json` | machine-readable findings |
| `--selftest` | run the offline suppression-logic self-test and exit (no board or kicad-cli needed) |

## kdrc.json (beside the board)

Same shape and precedence as `kpcb.json`. A bare `PREFIX:RULE` mutes the whole
rule; `PREFIX:RULE:TOKEN` mutes only findings whose refs or message contain
TOKEN. Rule names are the kicad-cli `type`, upper-cased, prefixed `DRC:`/`ERC:`.

```json
{"max": 25,
 "suppress": ["ERC:LIB_SYMBOL_MISMATCH", "ERC:SINGLE_GLOBAL_LABEL",
              "ERC:ISOLATED_PIN_LABEL", "ERC:FOUR_WAY_JUNCTION"]}
```

Pre-muted classes and why: `LIB_SYMBOL_MISMATCH` = the 16 deliberately edited
symbols (CLAUDE.md); `SINGLE_GLOBAL_LABEL` / `ISOLATED_PIN_LABEL` = global labels
that join across the three roots and so look single-use per root;
`FOUR_WAY_JUNCTION` = drawing style. `pin_to_pin`, `power_pin_not_driven` and
`footprint_filter` are left visible on purpose - triage them, then add the
confirmed-benign ones here.

A `DRC:CLEARANCE` finding at an actual **0.0 mm** (copper touching, a real
short) is never suppressed no matter what matches it here - it always shows,
tagged `0.0mm ACTUAL!` at the front of the line so 70-char truncation can't
hide the number. If a suppress rule would otherwise have caught it, the run
prints how many were forced visible this way.

## Notes

- ERC item positions are sheet-local coordinates, not board mm; the ref is the
  locator. DRC item positions are board mm and are kept in the message.
- kdrc shells `kicad-cli` (KiCad 8-10). If it is not on PATH, kdrc exits 3.
- Mid-layout, DRC `unconnected_items` (unrouted) dominate. Route first, then care
  about them; until then `--unconnected` is opt-in.
