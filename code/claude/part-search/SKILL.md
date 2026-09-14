---
name: part-search
description: Look up and choose electronic components on LCSC, JLCPCB and DigiKey with part.py. Does parametric selection (e.g. a ceramic cap 4.7uF-100uF rated >=25V, cheapest first), finds cheaper drop-in alternates for a part on the board, reports JLCPCB Basic vs Extended and $3/line assembly fees, runs one-shot board sourcing triage, and returns pricing ladders, stock, MOQ and a verified datasheet URL. Use this whenever the user asks about a specific component, an LCSC C-number, an MPN, a distributor price, whether something is in stock, what a part costs at some quantity, what the datasheet link is, asks to compare two parts, asks to pick or find a part meeting electrical constraints, asks whether a part is JLC Basic, or asks to price or source a whole board. Use it instead of web search for anything part-shaped, and never hand-write a scraping script for LCSC - the working endpoints are already recorded here and the obvious ones are blocked.
---

# Distributor part search and selection

`scripts/part.py` is stdlib-only Python 3. No install, no API key needed for LCSC or JLC.

```bash
P=<this-skill>/scripts/part.py
python3 $P selftest
python3 $P pick MLCC --cap 4.7u..100u --volt '>=25' --pkg 0805 --diel X7R,X5R
```

## Pick the command by what is being asked

| the question | command |
| --- | --- |
| "find me a cap 4.7-100 uF, >=25 V, cheapest" | `pick` - see [pick](references/pick.md) |
| "is there something cheaper than C45783" | `alt C45783` |
| "what does C18164413 cost / is it stocked" | `show` |
| "is this part JLC Basic", "what are my assembly fees" | `jlc` - see [endpoints](references/endpoints.md) |
| "datasheet link for X" | `ds` |
| "X vs Y" | `compare` |
| "what does this whole board cost" | `bom` |
| "source this board" / "what's blocking assembly" | `check` - `bom` + `jlc` + missing-code triage in one call |
| "do my footprints/values match the parts" / "any wrong packages or values" | `fpcheck` - KiCad footprint + value vs the LCSC part, per part |
| bare MPN, no constraints, just find it | `search` |
| "higher voltage rating or more nominal uF?" / DC-bias derating | `kcap.py` - see [kcap](references/kcap.md) |

`search` is keyword-only and ranks badly on parametric queries.
`search '22uF 25V X7R 1206'` returns electrolytics and tantalums in the top hits,
because LCSC has no category filter. **For anything with electrical constraints, use
`pick`, not `search`.**

## Commands

| command | use |
| --- | --- |
| `pick 'MLCC' --cap 4.7u..100u --volt '>=25'` | parametric selection, sorted, filtered |
| `alt C45783` | cheaper/stocked/Basic equivalents of a part already on the board |
| `jlc board.net` | Basic vs Extended per part + total $3/line assembly fees |
| `show C18164413` | ladder, stock, MOQ, multiple, params, category, verified datasheet |
| `ds C42409135` | datasheet URL only, with pass/fail per candidate |
| `compare C1525 C60474` | side-by-side, differing parameters only |
| `search 'TPS61033'` | keyword search. Add `--instock`. |
| `bom board.net --qty 5` | prices a whole KiCad netlist by its LCSC Part property |
| `check board.net --qty 5` | one-shot sourcing triage: `bom` pricing + `jlc` Basic/Extended, one table, one pass over the netlist |
| `fpcheck board.net` | each symbol's KiCad footprint AND value vs the assigned LCSC part. Footprint: flags size/family mismatches and one LCSC code reused across different bodies (chip sizes and MPN-named footprints auto-clear; divergent nomenclature -> a small REVIEW bucket). Value: R/C/L symbol value vs LCSC's resistance/capacitance/inductance param (catches a right-footprint, wrong-value part, e.g. a 36R part on a 37.4R symbol). Also lists any assigned part LCSC marks EOL/NRND. `--show-ok`, `--json`; no `.net` runs the offline self-test. |
| `selftest` | which providers and the FX rate source work right now |

`show`, `ds`, `compare` and `alt` accept a C-code, a bare MPN, or a pasted LCSC URL.
`show` with several SKUs prints one verbose block per part by default; `--table`
switches to one compact row per part, `--json` for machine-readable output.
Exit codes: 0 ok, 1 nothing found.

## fpcheck: the REVIEW bucket and confirming equivalences

`fpcheck` sorts every part into MISMATCH (footprint size/family or value
disagrees - real, fix it), REVIEW (the LCSC package string and the KiCad
footprint name read differently and the matcher can't tell if the body is the
same), and OK. REVIEW is a "look at these", not a bug list. Work it once:

- **Same body, different wording** -> record it so it never comes back.
  `part.py fpcheck board.net --confirm C22445413 C233771 --note "why"` writes the
  (package, footprint) pair to `fpcheck.json` beside the board; future runs
  auto-clear it as "confirmed equivalent". The objective test for "same body" is
  **dimensions + pin count**: LCSC `WQFN-24-EP(4x4)` vs KiCad `TQFN-24_L4.0-W4.0...EP`
  is the same land, just different names. easyeda2kicad-generated footprints
  (`..._L#-W#-P#`, `Texas_*`, vendor-named) are the part's own land pattern, so a
  name-only difference there is safe to confirm.
- **Genuinely a different part** -> do NOT confirm. This is a footprint borrowed
  from another vendor's similarly sized part, or the pad count differs from the
  part's lead count (e.g. a 4-pin switch on a 2-pad footprint). Verify the land
  pattern against the datasheet and fix the footprint if wrong. A false confirm
  hides a real footprint bug, so when unsure, leave it in REVIEW.

`fpcheck.json` is board data like `knet.json`/`kpcb.json` - **commit it** so the
confirmations persist across sessions. `--confirm` only clears the nomenclature
REVIEW path, never a MISMATCH and never the "same code on two bodies" flag.

## Run selftest first in a new session

LCSC has no public API. `part.py` uses undocumented endpoints that can start
returning 403/404 without notice. `selftest` reports in one line which providers are
alive, so you never debug endpoints by hand or rebuild a scraper.

If a provider shows DEAD, the endpoint moved. **The constants are at the top of
`part.py`. Do not write a replacement script; fix the constant.** The full probed
endpoint table, and the exhaustive list of LCSC filter/sort parameters that were
tested and do not work, are in [references/endpoints.md](references/endpoints.md).
That file exists so nobody re-probes them. Read it before touching the HTTP layer,
not before a normal query.

## Hard rules

- **Two catalogs, never add them together.** `show`, `search`, `compare`, `bom` and
  `pick --source lcsc` report **LCSC retail** prices. `jlc` and `pick --basic` report
  **JLCPCB assembly-catalog** prices. Different numbers for the same part. Always say
  which one you are quoting.
- **A trailing `!` on a price means FX was unreachable** and the native currency is
  shown unconverted. Never add a `!` price to a converted one. Default currency is
  CAD; LCSC arrives in USD, converted at a daily cached rate with the native figure
  in parentheses. `--currency USD` switches everything.
- **`bom` totals exclude shipping, tax, PCB fabrication and assembly.** Say so when
  quoting a per-board cost. Extended line fees are `jlc`'s job, not `bom`'s.
- **Never regex a netlist for `C\d+`** to find LCSC codes - that matches capacitor
  refdes like `C108`. `jlc`/`bom` parse the `LCSC Part` property via kicad-review's
  `knet.py`.
- **Do not infer Basic/Extended from a JLC keyword search** - its relevance ranking
  drops parts that do exist in the library, showing as a false `-`. Use `--basic` or
  the `jlc` command.
- **Check the `resolved:` line** that `pick` prints when a shorthand mapped to a
  differently-named LCSC attribute. If it resolved wrong, use `--w` with the verbatim
  name.
- **Datasheet links are verified, not returned on faith** (HTTP status, content type,
  `%PDF` magic). A BROKEN report is real - do not paste the URL anyway.

## What the tool does that a hand-rolled script will not

- Verified datasheet links, falling through to the next candidate then the product
  page. Manufacturer links with expiring `?ts=` tokens are exactly what this catches.
- Quantity pricing respects MOQ and order multiple, and says when it rounded up.
- Numeric comparison is numeric - value normalisation drops the decimal point, so a
  text match would treat `2.2uF` and `22uF` as identical.
- Disk cache, 24 h TTL, `~/.cache/partsearch`. A warm `pick` is ~1 s against a ~10-30 s
  cold one. `--fresh` when stock matters.
- `bom` honours DNP and `exclude_from_bom`, and separately lists placed parts with no
  LCSC number at all - the ones actually blocking assembly.

Stock is authoritative from the detail endpoint; a 0 in search results is a real 0.
`pick` shows `unit@N` and `ext` (= unit x the actual buy quantity after MOQ rounding).

DigiKey is optional and participates in `search`/`show` only, not `pick` - setup in
[references/endpoints.md](references/endpoints.md).

## Related

`kicad-review` provides `knet.py`, whose netlist parser `bom` and `jlc` import when
both skills are installed. Without it, `bom` falls back to regex and `jlc` refuses
`.net` input rather than risk matching refdes.
