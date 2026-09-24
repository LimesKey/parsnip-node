# pick - parametric selection

```bash
part.py pick MLCC --cap 4.7u..100u --volt '>=25' --pkg 0805 --diel X7R,X5R --qty 100
part.py pick MLCC --cap 10u --volt '>=25' --pkg 0805 --basic      # no $3 line fee
part.py pick 'schottky diode' --vr '>=40' --ifwd '>=1' --pkg SOD-123
part.py pick 'ferrite bead' --w 'Impedance@Frequency=~600' --irms '>=2'
part.py pick TVS --pkg 'SMA(DO-214AC)' --vrwm '>=20' --vc '<=40'
part.py pick MOSFET --vds '>=30' --rdson '<=10m' --pkg 'DFN-8(3x3)'
part.py pick --cat 'I/O Expanders' --pkg TSSOP-16
```

## Where the candidates come from

Default `--source jlc`: JLC's search index (~7.2M parts, i.e. the LCSC catalog) with
the package, the category, in-stock and a **price-ascending sort applied server-side**,
200 attributed rows per call. So the pool is the cheapest in-stock parts of that type
in that package, not a relevance-ranked sample. Measured 2026-09-23: cheaper-or-equal
results than the LCSC pool on MOSFET/MLCC/schottky/LDO picks, 4x faster cold, and it
found 3.3 V LDOs in SOT-23-5 where the LCSC pool found none.

- **Category.** JLC's keyword matcher ANDs tokens and ignores category names (`TVS` +
  SMA finds 0 of the 2,802 SMA parts), so `pick` filters by category instead when it
  can: `--cat TEXT` (any unique part of the name; ambiguous text lists the matches),
  or inferred from the keyword when the phrase, or a word of it, names exactly one
  category (`TVS diode` -> `ESD and Surge Protection (TVS/ESD)`, `schottky` ->
  `Schottky Diodes`, `LDO`, `I2C GPIO expander` -> `I/O Expanders`, `test point`).
  The header says `jlc N in '<category>'`.
- **Package.** `--pkg` with one exact package goes server-side. The string must be
  LCSC's (`SMA(DO-214AC)`, `DFN-8(3x3)`); `search` or `show` shows it.
- **Attributes (exact pool).** With a category, every limit whose attribute JLC's
  sidebar knows goes server-side too: `pick` reads the sidebar's value list for the
  category + package, applies the limit to it locally (ranges and >= included) and
  asks JLC for exactly those values. The pool is then every in-stock part meeting
  all limits, cheapest first - not the cheapest 600 of the category filtered after.
  The header says `N limit(s) server-side`. A limit no listed value meets stops
  there (`no X value in '<cat>' meets the limit`) without an LCSC fallback.
- **Ambiguous keyword.** When a keyword names several categories, the one that IS
  the word wins (`MOSFET` -> MOSFETs, not the SiC one), else the only one stocking
  `--pkg` (`MLCC` + 0805 -> MLCC - SMD/SMT). Still ambiguous: keyword match, as before.
- **Fallback.** JLC finding nothing falls back to the LCSC pool (keyword search, then
  one `product/detail` call per candidate, `--pool` cap) and says so. `--source lcsc`
  forces it, `both` unions them.
- **Price of record.** The shown rows are re-priced from LCSC (ladder, stock,
  lifecycle), so `pick` quotes LCSC retail. `--basic` is the exception: it quotes the
  JLC assembly catalog on purpose.
- **Dropped.** Stock under `--minstock` (default 100; `--anystock` = no floor) and
  EOL/NRND parts, counted in the header.

## alt: what "equal or better" holds

`alt C..` looks the part up, then pools its exact JLC category and package. Passive
value, voltage, tolerance and dielectric hold as before (value equal, voltage >=,
tolerance <=, dielectric same class or better). Every other parameter goes through a
direction table: voltage/current/power ratings `>=`; RDS(on), Vf, leakage, clamping,
DCR/ESR, threshold, dropout `<=`; type, polarity, channel count, output voltage,
frequency, colour equal; capacitances, charge, breakdown and temperature ranges not
held. A negative value (P-channel) flips the direction. It prints every hold as a
`--w` flag, so loosen by re-running `pick` with a subset. RDS(on) is compared as the
number before `@`: a `4.6mΩ@4.5V` part beats `5mΩ@10V` on paper, and the column
shows the condition so a different test voltage is visible.

## Constraint syntax

Every `--flag` and every `--w NAME=SPEC` takes the same spec grammar:

| spec | meaning |
| --- | --- |
| `22uF` | equal (numeric, so `22uF` never matches `2.2uF`) |
| `4.7u..100u` | inclusive range |
| `>=25` `<=50` `>1k` `<10` | comparison |
| `X7R,X5R` | any-of; numeric-aware, so `25` matches `25V` |
| `~ceramic` | case-insensitive substring |
| `!X5R` | negated |

Values parse as engineering notation: `4u7`, `100nF`, `4R7`, `10k`, `±20%`, `25V`
all work, `10V~35V` takes the first figure, and LCSC's test conditions are cut at
`@` (`5mΩ@10V` -> 5 mΩ, `450mV@1A` -> 450 mV, `15A@8/20us` -> 15 A).

## Attribute shorthands

`--cap --res --ind --volt --pkg --diel --tol --current --power --freq --temp --type
--dcr --esr --vr --vf --ifwd --ir --vds --id --vgsth --rdson --isat --irms --vrwm --vc
--vout --iout`, plus the long spellings `--capacitance --resistance --inductance
--voltage --package --dielectric --tolerance`. An unknown flag gets a "did you mean"
and the reminder that limits go in the value (`--volt '>=50'`, not `--voltage-min`).

These resolve onto whatever LCSC actually calls the parameter, deterministically
(exact alias, then prefix, then containment of an alias; ties break
shortest-then-alphabetical, so resolution never depends on parameter ordering). A
shorthand never matches as a bare substring (that is how `res` once resolved to "Gate
Th**res**hold Voltage"); a verbatim `--w` name still can. `pick` prints a `resolved:`
line whenever a shorthand mapped to a differently-named attribute:

```
resolved: ifwd -> Current - Rectified;  vr -> Voltage - DC Reverse (Vr) (Max)
```

**Check that line.** If a shorthand resolved to the wrong attribute, use `--w` with
the verbatim LCSC name instead: `--w 'Voltage - DC Reverse (Vr)=>=40'`.

## --fields is the cheap discovery pass

When you do not know what a category's attributes are called, run the same query with
`--fields`. It lists the attribute names and their commonest values across the
candidate pool and skips filtering entirely. One call, small output. Do this before
guessing at flag names for an unfamiliar part type.

```
part.py pick 'schottky diode' --pkg SOD-123 --fields
  Current - Rectified        1A, 3A, 5A, 2A, 10A
  Voltage - DC Reverse (Vr)  100V, 50V, 40V
  Voltage - Forward(Vf@If)   450mV@1A, 850mV@3A
```

## Why pick fans out into several queries

A single query for a *range* would never surface the mid-range values, so `pick`
expands a value range over E6 preferred values (`--e12` to widen) and issues one query
per value, capped by `--maxq` (default 14). The JLC pool takes the bare values
(`4.7uF`, `6.8uF`, ...) with the package server-side, paging deeper (up to 3 x 200
rows) when there are few values. The LCSC pool, which ranks on relevance only, also
fans out over the standard voltage series: a single `>=25V` query fills its pool with
cheap 6.3/10/16 V parts that all fail the filter. The `pick:` header line shows the
queries it actually ran.

If a result set comes back empty, `pick` prints which constraint rejected how many
candidates, e.g. `every candidate failed on: volt(131), diel(20)`. That tells you which
one to loosen.

## Flags

`--sort price|stock|cap|volt` (default price), `--qty N` (default 100, MOQ/multiple
applied), `-n N` rows (default 8), `--basic`, `--source jlc|lcsc|both` (default jlc),
`--cat TEXT`, `--minstock N` (default 100), `--anystock`, `--fields`, `--e12`,
`--pool N` LCSC parts to detail (default 240, LCSC pool only), `--maxq N` sub-queries
(also the JLC call budget), `--jobs N` threads, `--nojlc`, `--json`, `--fresh`.

`--pool` exists because LCSC cannot filter server-side: its candidates must be fetched
by `product/detail` one at a time to learn their parameters. The JLC pool needs none
of that. See [endpoints.md](endpoints.md).

## When nothing matches

`pick`/`alt` print, instead of an empty table:

- `parts in '<cat>' <pkg> meeting each limit on its own (any stock): Ipp 85, ...`
  from the facet counts - the scarcest limit first, over the whole category;
- `candidates failing each limit (one part can fail several)` - EVERY failure
  counted, not just each candidate's first;
- `the only match was C85402 itself` when `alt`'s base part was the sole match;
- **near misses**: up to 5 parts failing exactly ONE limit, cheapest first, with
  the value that failed (`Reverse Stand-Off Voltage (Vrwm) = 4.8V (limit >=5)`,
  `(not listed)` when LCSC has no such attribute for it). Loosen that one `--w`.

## Brands

`pick` ranks on price and never on brand. "Is this unknown MLCC brand OK?" is
answered in [brands](brands.md) (sourced, dated) - read it instead of searching.
