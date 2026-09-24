# knet.py - netlist connectivity, values, ERC, BOM

`knet.py FILE <cmd>` - file first, then subcommand.

| command | use |
| --- | --- |
| `summary` | comps, sheets, rails, DNP list, largest nets. Run first on an unfamiliar board. |
| `around REF...` | **highest value per call.** Pins, types, nets, position, every part one hop away. Use instead of `comp` + several `pin` calls. |
| `check` | 17 rule-based findings, grouped ERROR/WARN/INFO. Exit 2 if any ERROR. Three or more findings sharing a message fold into one line, `tail [N]: refs` - `[49]` means 49 separate findings, not one. |
| `check --since old.net` | only findings NEW vs an older export, plus a fixed/unchanged tally. |
| `draw REF\|NET [-d N] [-o x.svg] [--spec]` | KiCad-style schematic from the netlist; `--spec` gives the editable ksch source. |
| `notes` | schematic text notes by sheet - designer intent that exists nowhere in the netlist. |
| `rails` | each power rail: what feeds it, total decoupling, loads. |
| `divider REF.PIN\|NET` | resistor-divider trip voltage from netlist resistor values, worst case from tolerance if stated. See below. |
| `diff old.net` | what changed vs an earlier export (rename-safe: compares neighbour sets). |
| `comp REF...` | pin table with net, node count, supply rail. |
| `net PATTERN` | exact name, then sheet-local name, then regex. |
| `pin U2.9` | net at a pin plus everything else on it. |
| `find PATTERN` | searches refs, values, footprints, properties, pin functions, net names. |
| `walk REF[.PIN] -d N` | expand outward through 2-pin passives. |
| `path A B` | shortest electrical path between two parts. |
| `bom [--sheet PATH]` | grouped, DNP separated, flags parts with no LCSC number. `--sheet /Root/Rails/` scopes to one hierarchical sheet and everything nested under it. |
| `unconnected` | floating pins, `[NC flag]` vs `[NO NC flag]` with the sidecar, plus single-node nets. |
| `sheets` | components per hierarchical sheet. |
| `revpol [--stack BT1,BT2 \| --pair P,N --volts V]` | reverse-polarity what-if. See below. |

Every load prints a `WARNING:` on stderr when a `.kicad_sch` beside the netlist
was saved after it (KiCad `_autosave-*` files ignored), or when the `.kicad_pro`
lists a top-level sheet the netlist lacks (a single-root export). Both print the
exact `kmerge.py` command that fixes them.

## Reading `around`

The header names the symbol (`sym Device:D_Dual_Series_ACK`): a generic
variant symbol carrying a specific part number is where pinout bugs hide.
One line per pin, then its peers indented under it. An `unconnected-*` pin reads
`NC (flagged)` when the schematic has a no_connect marker on it (directly, or at
the end of a wire stub from the pin), else `floating  <-- no NC flag`. The pin-type column is omitted
when the symbol types every pin `unspecified` (easyeda2kicad does), so a missing
type means unknown, never `passive`. A net already shown on an earlier pin of the
same part prints bare, or `(peers listed at pin N)` - that is two pins on one net,
not a different net.

## `divider`

`REF.PIN` accepts the pin's declared NAME (`U5.OVLO`, or the raw KiCad-escaped
`U14.EN{slash}UVLO`) as well as its number. Handles a plain 2-resistor divider
automatically. For TI's 3-resistor UVLO/OVLO string
(`IN->R1->EN/UVLO->R2->OVLO->R3->GND`, every TPS2594x/hot-swap part) it auto-detects
the shape and reports VIN_UV/VIN_OV once given `--vth <threshold from the
datasheet>`; add `--vth-tol <%>`, since the IC's own threshold accuracy usually
dominates the resistor tolerance. Use it for any FB/OVLO/UVLO/ADC-sense divider
instead of tracing it by hand across several `pin`/`net` calls.

## Flags

`--through R,L,FB,F,FL,JP` (pass-through prefixes; add `C`/`D` to trace through caps
or diodes), `--fanout N` (nets above N nodes are rails, default 8), `--as-drawn`
(traverse DNP parts too; **default treats DNP as open circuits**),
`--rail '+5V=5,VCC_RF=3.3'`, `--peer-max N`, `-o FILE`, `--spec`, `--theme`,
`--json`, `--only`/`--skip RULE,RULE`, `--rules` for the rule legend,
`--no-suppress` (for `check`), `--sheet PATH` (for `bom`), `--vth V` /
`--vth-tol PCT` (for a 3-resistor `divider`).

`walk` caps a rail's load list at 10 with a `+N more (rail net, N total)` tail once
a net is a named rail or above `--fanout`. The count in the tail stays accurate even
when not every load is printed.

## Project config

`knet.json` next to the netlist persists board defaults:

```json
{"rails": {"+BATT": 8.4}, "fanout": 8, "suppress": ["RFSTUB:GPS_ANT", "OCNOPULL:U9"]}
```

Precedence: defaults < knet.json < CLI.

`suppress` mutes `check` findings already confirmed not to be bugs, so they stop
costing tokens on every future review instead of restating "don't re-flag this"
each session. An entry is `"RULE"` (mute the whole rule) or `"RULE:TOKEN"` (mute
only findings naming that ref or net - a confirmed false positive on one net must
not hide a real one elsewhere). `check` reports how many it muted; verify the count
looks right, and use `--no-suppress` after a big rewire to see everything again.

## check rules

`FLOATPWR NCDRIVEN SOLO CONTEND DOMAIN DECOUPLE OCNOPULL I2CPULL PASSONLY RFSTUB
DNPPATH CAPRATING NOVALUE GNDISLAND CLAMPRATING PARPIN PINOUT FPPAD`. `--rules` for one-line
descriptions. With the sidecar, FLOATPWR downgrades to WARN when the schematic
carries an explicit NC flag.

- **`DOMAIN`** is the useful one: it infers each net's pulled-to voltage from series
  resistors and ferrites to named rails and compares against each IC's supply rail,
  catching pull-ups to 5 V on 3.3 V logic.
- **`PARPIN`** catches a paralleled pad left floating: two or more pins on the same
  part sharing the same declared pin NAME (e.g. multiple BAT pins on a charger IC)
  where one is wired to a real net and a sibling is not. Matches on pin name rather
  than pin type, so it still fires when the symbol types the dangling pin `passive`
  rather than `power_in` - the case that let a real floating BAT pin slip past
  FLOATPWR. A dangling pin whose number the footprint has NO pad for is fused into
  a sibling's pad (TPS25751 `REF0038A`: pad 20 spans pins 20-22) and reads INFO
  `fused into pad N - the copper is right`, not ERROR.
- **`FPPAD`** diffs each part's symbol pin numbers against its footprint's pad
  numbers, read from the `.kicad_mod` the footprint field names (resolved through
  the project `fp-lib-table`, then the user's global one, with `${VARS}` from the
  environment and `kicad_common.json`). A pad with no pin imports with NO net
  (WARN: an exposed pad or FET tab left floating, e.g. a DFN EEPROM's EP on a
  generic 8-pin symbol); a wired pin with no pad loses that connection on the board
  (ERROR). `MP` mounting pads are exempt. Works before the part is on the board,
  which KiCad's own parity check cannot. No reachable library = one INFO line.
- **`RFSTUB`** counts the parts in series on an RF-netclass net. Not counted: DNP
  parts, a 2-pin R/C/L/D/FB whose other end is GND (a shunt at the node: pad shunt,
  ESD diode, matching element), and an RF choke (an L whose far end is bypassed by
  a capacitor, or only feeds resistors >= 1k: a bias tee, an antenna-detect line).
  Net names (`ANT`, `_RF`) count as RF only in a project with no RF netclass.
- **Rails** come from names: a whole-name voltage (`+3V3`, `12V`), `KNOWN_RAILS`,
  one voltage token inside a longer name (`PD_LDO_3V3`, `3V3_GNSS`, `5V_RAW`) unless
  another token is a signal word (`5V_EN`, `PG_3V3`), and `--rail`/knet.json.
  OCNOPULL also accepts an LED + resistor to a rail (a status pin sinking an LED);
  I2CPULL reads a dual I2C/SPI name (`SCL/SPI_CLK`) with no pull-up as INFO.
- **`CLAMPRATING`** parses a TVS's standoff voltage from a recognised part-number
  series (SMF/SMAJ/SMBJ/SMCJ/SM6T/SM8S/P6KE/1.5KE/P4KE) and flags it against the
  named rail it sits on between rail and GND - narrow by design (only those series,
  no margin math beyond "not below") to avoid a confidently wrong number. The rail
  figure is name-derived or `--rail`/knet.json, not the regulator's true analog
  output, so a finding here can understate the real gap; it will not overstate it.
- **`PINOUT`** compares a 3-pin SOT-23/323 part's real pinout (a small curated
  table in knet.py: BAV99/BAV199/BAT54S series `AKC`, BAT54A `KKA`, BAT54C `AAK`,
  2N7002/SI2308/AO3400/AO3401/BSS138/BSS84 `GSD`) against the symbol's pin order,
  read from a generic symbol's name suffix (`D_Dual_Series_ACK`, `Q_NMOS_GSD`) or
  from G/S/D pin names. Stock DIODE pin names are never used: `Diode:BAV99`'s hidden
  names K/A/K contradict its graphics and `Diode:BAT54A` has none. Found D6 (BAV199
  on `D_Dual_Series_ACK`, should be `_AKC`). Extend the table for a new part; do not
  loosen the match.
- **`GNDISLAND`** catches a different shape of bug than FLOATPWR: pins named like
  ground (GND, AGND, VSS, ...) that ARE wired to each other but never reach the
  board's real GND net - a merge that silently didn't happen. `is_gnd()` recognises
  these by pin name even on an auto-generated net (`Net-(U7-GND-Pad1)`), so DECOUPLE
  no longer misfires on them as "missing a bypass cap".

**These are heuristics. Confirm against the datasheet before calling anything a
bug.** Worked example: `U7.5 (FB)` on a TPS61033 sat on the same net as `VIN` -
looks like a broken feedback loop, but the datasheet says that is the documented
fixed-5.0 V configuration. The correct move was `kdoc.py grep 'FB' -d tps` first.

## `revpol` - what a backwards cell or pack does

Cells come from `--stack` (default: every `BT*`, chained by their `+`/`-` nets) or a
`--pair P,N --volts V`. Cases: whole pack reversed, then each single cell reversed,
each compared against normal operation so only CHANGES print:

- **conducting between two cell-driven nodes** - a loop only the cells limit, with
  `fuse in loop: F5` or `NO FUSE in this loop`;
- **conducting through a series resistor** - limited, names the R;
- **conducting into another node** - a junction now feeding an undriven node;
- **IC pins pushed outside their normal range** - volts vs that IC's own GND pin,
  the series R back to the cell, the current that R lets into the pin's ESD diode
  (`~350mA into its ESD diode` - injection limits are ~10 mA), a pin driven above
  its own supply pin (back-powering it), and the `kdoc.py grep` for its abs-max;
- **FET channels vs normal** - a FET whose gate the cells drive past 2 V conducts
  (source-drain joined), re-solved until stable: a low-side reverse-polarity NMOS
  (Q16) is ON in normal operation and OFF reversed;
- **ICs that lose their ground** - isolated (no two pins cell-driven > 0.7 V apart),
  or ground floating while pins still sit on cell taps at different voltages (the
  IC's internal/ESD network conducts between them: the BMS on its cell-sense pins).

A normal-operation section first lists junctions ALREADY conducting between two
driven nodes (LEDs excluded) - how a wrong-pinout diode shows up (D6).

Model: cells ideal; F/FB/L, R <= 1 ohm (incl. 4-pin Kelvin shunts), `Bridged`
solder jumpers and ON FET channels are wires; other resistors pass their node's
voltage one hop (a sense line at open circuit); every forward junction is 0.7 V.
Diode orientation follows the REAL part (PINOUT table by pad number), then the
symbol suffix, then A/K pin names, then KiCad's pad-1-is-cathode convention for a
2-pin diode (true for `Diode:SMF*A` despite its pin NAMES being A1/A2). A zener
(voltage from `BZX..-C15` / `5V1`) or unidirectional TVS (~1.1x the standoff in its
part number) also conducts backwards at breakdown; a bidirectional TVS (`CA`
suffix, `bidirectional` in its description, or A1/A2 pins without `unidirectional`)
both ways at breakdown, or is listed not-modelled when the part number gives no
standoff. An ESD array with a GND pin gets GND->I/O diodes (and I/O->supply when it
has a supply pin); a BJT (NPN/PNP in its symbol or description) both base
junctions; FET polarity comes from any text field (`N-CH`, `PMOS`, symbol name)
before the small value tables. Only forward junctions whose anode is not ground
DRIVE an undriven node: breakdown paths and GND-to-signal clamps conduct but power
nothing. ICs are open circuits whose ESD diodes only clamp. A what-if to aim
datasheet checks, not a simulation.
