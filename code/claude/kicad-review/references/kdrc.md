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
python3 $D parsnip.kicad_pcb flags      # PWR_FLAG audit (~9 s)
python3 $D parsnip.kicad_pcb doctor     # install + project sanity, exit 2 on a WARN
python3 $D parsnip.kicad_pcb scratch DIR [--from OTHER.kicad_pcb]   # a project copy for the writers
```

`scratch DIR` copies the project beside FILE (.kicad_pro, .kicad_dru, sheets, .net, k*.json,
lib tables, a symlink per `${KIPRJMOD}` dir they name) into a new empty DIR, with FILE - or
`--from` another board, e.g. one worked in ~/Downloads - under the project's stem, so DRC
there checks the project's rules. A board opened alone gets a default .kicad_pro: every DRC
header (and `doctor`) WARNs when the .kicad_pro beside a board has only the Default netclass,
no patterns and no .kicad_dru.

`doctor` prints each kicad-cli's version, the project's newest file format, what
a double-click opens for .kicad_sch/.kicad_pcb/.kicad_pro (`xdg-mime`; WARN when a
stable app would get a 10.99 file, with the `xdg-mime default` fix) and whether
KiCad holds the project (`kcommon.kicad_running`: exact process names, `~*.lck`
locks, `_autosave-*`). Any tool that writes project files refuses while that list
is non-empty.

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
- kicad-cli reports at most 199 violations per type; the header names each type
  that hit it. For TRACK_WIDTH kdrc counts the real number on the board (the last
  matching `.kicad_dru` track_width rule, else the `.kicad_pro` minimum; a rule
  whose condition is more than `A.Type/NetClass/NetName == '..'` terms is named as
  skipped): parsnip 456 where the CLI shows 199.
- Refs come from the item text minus its `[net]`: `Pad 2 [Net-(U8-SW2)] of U8`
  gives U8, not `U8 SW2`, so labelling a net does not change a finding.

## `flags` - which PWR_FLAGs ERC actually needs

The netlist drops `#FLG` symbols, so knet cannot say where a flag sits. `flags`
copies the sheets to a temp dir with every PWR_FLAG made an ordinary part with a
passive pin (`#FLG` -> `FLG`), kmerges that copy to read each flag's net, and runs
whole-project ERC on the original and on the copy. A `power_pin_not_driven` only
the copy has marks a net that needs its flag:

- `LOAD-BEARING` - that net goes undriven without it (names the pin, usually a `#PWR`).
- `DUPLICATE` - a second flag on a net that needs one.
- `REDUNDANT` - ERC passes without it (names a `power_out` driver if the net has one),
  or it is connected to nothing.

Parsnip 2026-09-24: 13 flags, all load-bearing. Sheets must sit in the project
dir (a sub-sheet in a subfolder is not copied). `--json` for rows.

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

## Demoted rules

The header lists what the `.kicad_pro` demoted, since kicad-cli leaves an
`ignore` rule out of its report entirely: `DRC demoted in parsnip.kicad_pro:
IGNORED pth_inside_courtyard, silk_over_copper, silk_overlap; warning-only fab
limits connection_width, copper_sliver, holes_co_located, starved_thermal`, and the
same for ERC. "Fab limits" are the manufacturability rules (clearance, drill,
annular ring, courtyard, hole-to-hole...) where a warning still means a board the
fab may reject. `--all` checks them at full severity: kicad-cli's `--severity-all` alone
never runs an `ignore` rule (its report lists them under `ignored_checks`), so `--all`
copies the project to a temp dir (`copy_project`, what `scratch` uses), sets every ignored
DRC/ERC rule to warning there and runs on that; the header names them. It refuses
`--refill-save` (that would save the temp copy).

## Flags

| flag | effect |
| --- | --- |
| `--max N` | cap lines per rule (default 25, or `max` in kdrc.json) |
| `--only R1,R2` / `--skip R` | keep or drop rules by name (e.g. `DRC:CLEARANCE`) |
| `--unconnected` | include the DRC `unconnected_items` (unrouted nets; hidden by default because mid-layout there are hundreds) |
| `--parity` | add DRC schematic-parity. The CLI's parity reads one hierarchy and names Root nets `/X` where the board has `/Root/X`: those NET_CONFLICTs, and EXTRA_FOOTPRINTs whose ref a sheet beside the board places, are dropped and counted in a header note (parsnip: 199 + 168). What is left is real. |
| `--all` | every severity, and the `.kicad_pro`'s `ignore` rules too, run on a temp copy of the project with them set to warning (`--severity-all` alone never runs an ignored check); the header names them. Without it a demoted rule is silent. |
| `--no-suppress` | ignore kdrc.json |
| `--rules` | print the rule legend |
| `--json` | machine-readable findings |
| `--selftest` | run the offline suppression-logic self-test and exit (no board or kicad-cli needed) |
| `--no-stale-check` | skip the saved-fill DRC pass |
| `--refill-save` | save the refilled board (what `kpcb airwires` reads). Scratch copies only: refused for a git-tracked file. Skips the stale-fill pass (the saved fill is the refill). |
| `--report FILE` | write kicad-cli's raw DRC JSON (for `--baseline`, and `kpcb ripup --drc`, which deletes the items it names by uuid) |
| `--baseline FILE` | an earlier `--report`: list only violations and unconnected items it lacks (multiset on type + descriptions, positions ignored; a text item by its uuid, as its text can hold a date variable; a `track_width` by its track's uuid, so a widened or re-laid under-floor track reads `changed: actual 0.6 -> 0.7`, not new + gone), with `N new, M gone, K changed` counts. When a type hits kicad-cli's 199-per-type cap in either report it says so: that type's new/gone lists compare two arbitrary samples. The check after every scratch-copy edit. |

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
