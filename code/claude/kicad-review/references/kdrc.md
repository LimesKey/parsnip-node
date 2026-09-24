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

- **The CLI is picked per file**: a board/sheet saved by 10.99 nightly gets
  `kicad-cli-nightly` (stable `kicad-cli` fails "Failed to load" on it), else
  `kicad-cli`; `$KICAD_CLI` overrides.
- **DRC** runs on the board with `--refill-zones` so results reflect current
  pours. It never passes `--save-board`: the refill is in memory, the on-disk
  `.kicad_pcb` is not modified.
- **Stale-fill check**: a second DRC pass runs on the fill AS SAVED, which is what
  `export gerbers` writes. Any violation only that pass finds is reported as
  `DRC:STALE_FILL` (ERROR if the underlying one is) with a `!! SAVED ZONE FILL IS
  STALE` banner: refill (B) and save before exporting. Found on parsnip 2026-09-22:
  two teardrop-vs-track clearance violations that exist only in the saved fill.
  `--no-stale-check` skips the pass (~5 s).
- **ERC** runs per **root**: the `.kicad_pro`'s `top_level_sheets` (primary first),
  else every `*.kicad_sch` that no other schematic pulls in as a sub-sheet. A root
  already covered by an earlier report is skipped. kicad-cli-nightly's ERC of the
  primary root covers every top-level sheet (parsnip: all 7 sheets in one report,
  header says `one report covers every top-level sheet`); stable kicad-cli covers
  only the root it is given.
- `DRC:SHORTING_ITEMS` and a `DRC:CLEARANCE` at actual 0.0 mm are never suppressed.
  Clearance/width messages lead with the number: `actual 0.1 < 0.127 mm (rule)`.
- Output: findings grouped by rule, capped per rule with a `+N more` tail,
  suppressible via `kdrc.json`, exit 0/2/3 like `kpcb`.

## The one caveat that matters

**Per-root (stable) ERC cannot see cross-root nets.** A pin powered or driven
through a global label whose driver is on another root (`I2C_HOST_*`, `USB D+/-`,
the shared rails) reads as `power_pin_not_driven` or `pin_to_pin` there. Those are
multi-root false positives. When the header does NOT say one report covers every
top-level sheet, confirm each against the merged netlist before believing it:

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
| `--no-stale-check` | skip the saved-fill DRC pass |

## kdrc.json (beside the board)

Same shape and precedence as `kpcb.json`. A bare `PREFIX:RULE` mutes the whole
rule; `PREFIX:RULE:TOKEN` mutes only findings whose refs or message contain
TOKEN. Rule names are the kicad-cli `type`, upper-cased, prefixed `DRC:`/`ERC:`.

```json
{"max": 25,
 "suppress": ["ERC:LIB_SYMBOL_MISMATCH", "ERC:FOUR_WAY_JUNCTION"]}
```

Pre-muted classes and why: `LIB_SYMBOL_MISMATCH` = the 16 deliberately edited
symbols (CLAUDE.md); `FOUR_WAY_JUNCTION` = drawing style. `SINGLE_GLOBAL_LABEL` /
`ISOLATED_PIN_LABEL` were muted while ERC ran per root (labels joining across roots
looked single-use); removed 2026-09-22 because the whole-project nightly report has
zero of them, so a new one is a real dangling label. Re-add only if you go back to
per-root stable ERC. `pin_to_pin`, `power_pin_not_driven` and
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
- kdrc shells `kicad-cli` / `kicad-cli-nightly` (KiCad 8-10.99). If it is not on
  PATH, kdrc exits 3.
- Mid-layout, DRC `unconnected_items` (unrouted) dominate. Route first, then care
  about them; until then `--unconnected` is opt-in.
