# Endpoints, and the things that were already tried

Read this before touching `part.py`'s HTTP layer. Do not re-probe what is here.

## Endpoint status as recorded (probed 2026-08-18)

| endpoint | status |
| --- | --- |
| `wmsc.lcsc.com/ftps/wm/product/detail` | works, detail by C-code, full parameters |
| `easyeda.com/api/eda/product/search` (POST, form-encoded) | works, keyword search, `pageSize` up to 200 |
| `jlcpcb.com/api/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList` (POST, JSON) | works, no auth, 200 rows/call **with parsed attributes**; package/category/price-sort/attribute filters server-side (see below). `pick`'s default pool |
| `jlcpcb.com/api/overseas-pcb-order/v1/componentSearch/filterComponentAttribute` (POST, JSON) | works, no auth: the parts sidebar's facets - every attribute value with its part count, per category id (+ package). Unfiltered: all ~850 category names with ids (2.5 MB). Captured 2026-09-23 |
| `easyeda.com/api/products/{C-code}/components?version=6.4.19.5` (GET) | works with a browser UA + `Referer: https://easyeda.com/`: the symbol and the footprint LCSC links to the code. `result.packageDetail.dataStr.shape[]` holds `PAD~shape~x~y~w~h~layer~net~num~holeR~pts~rot~...` in 10-mil units. CloudFront **403s after ~150 quick calls** (any client, for a while), so `fpcheck` asks only for leadless parts, 2 at a time, cached 30 days. Probed 2026-09-23 |
| `wmsc.lcsc.com/wmsc/product/detail` | dead, 404 JSON |
| `wmsc.lcsc.com/ftps/wm/search/global` | blocked, Akamai Access Denied |
| `wmsc.lcsc.com/ftps/wm/{search/product,product/search,product/list,catalog/list}` | dead, "static resource unavailable" |
| `cart.jlcpcb.com/.../selectSmtComponentList` | superseded by the `jlcpcb.com/api/...` path above |

The constants are at the top of `part.py`. When `selftest` shows DEAD, fix the
constant. Never write a replacement scraper.

## LCSC cannot filter or sort server-side. Do not go looking again.

Probed exhaustively against the easyeda search endpoint: `sortField`, `sortOrder`,
`orderBy`, `catalogId`, `paramNameValueMap`, `attributes`, `paramList`, `filters`,
`paramValueList`, `selectedParams`, `paramMap`, `attributeFilter`, `params`,
`searchParam`, `stockFlag`. Every one either changed nothing or returned `total=0`.
Its search rows carry **no parameters at all** - only mpn, number, manufacturer,
package, stock, price, url. `needAggs=true` returns facet lists but they cannot be
sent back as filters.

This is why the LCSC pool works the way it does: candidates must be fetched by
`product/detail` one at a time to learn their parameters. That is cached and parallel,
so it is fast, but it is why there is a `--pool` cap.

One thing `needAggs=true` IS good for: its `paramList` carries a `Category` facet (plus
`Package`, `Manufacturer`), names only, no counts. The union over four broad keywords
(`1`, `SMD`, `IC`, `resistor capacitor diode transistor connector`) is ~475 category
names, the same strings JLC's category filter takes. `part.py` caches that list 7 days
(`categories()`) to resolve `--cat` and to infer a category from a `pick` keyword. An
empty keyword returns nothing; `a` returns total 0.

## JLC search: what filters server-side (probed 2026-09-23)

The `selectSmtComponentList` index holds ~7.26M parts with no filter, i.e. the LCSC
catalog, not just JLC's assembly library. Prices match LCSC retail within ~1% on the
same break points (JLC quotes from qty 1); JLC's stock runs higher (its own warehouse).
Attribute names match LCSC's on the specs `pick` filters (a few secondary names
differ, e.g. `Input Capacitance(Ciss)` vs `Ciss-Input Capacitance`).

| body field | effect |
| --- | --- |
| `componentSpecificationList: ["SOT-23"]` | **works**: exact package filter (MOSFET 75,127 -> 5,744) |
| `secondSortName: "MOSFETs"` | **works**: category filter, JLC's exact name (a row's `componentTypeEn`). The response's `firstSortName` is the leaf, `secondSortName` the parent - the request uses them the other way round |
| `firstSortName: "MOSFETs"` | total 0 |
| `sortMode: "PRICE_SORT", sortASC: "ASC"` | **works**: price ascending (all 75k MOSFETs, cheapest first) |
| `currentPage: N` | **works**, keeps the sort order |
| `stockFlag: true` | works: in stock only |
| `componentLibraryType: "base"` | works: Basic only |
| `stockSort: "desc"` | error response |
| `componentAttributeList: [{"Drain to Source Voltage": ["30V", "40V"]}, {"Type": ["N-Channel"]}]` | **works** (captured from the jlcpcb.com/parts sidebar in a browser, 2026-09-23): a list of one-key maps, attribute name -> exact values; values OR, maps AND. The `/v2` path the site uses behaves the same. Earlier guesses (`attributeName`/`attributeValueList`, `attribute_name_en`/`attribute_value_name`, `componentAttributes`) error or return 0 |
| `needAggs`, `searchSource`, `searchType`, `needSortAndCount` | `sortAndCountVoList` / `brandList` stay null: no facets |

Values match exactly and there are no ranges: the sidebar's Min/Max boxes only
narrow the value LIST client-side. So `pick` fetches the facet values for the
category (+ package) - body `{"baseQueryDto": {"componentTypeIdList": [ID],
"componentSpecificationList": [PKG], ...}, "catalogLevel": 2, "paramList": []}`,
see `_facet_query` - runs its own predicate (ranges, >=, any-of) over them, and
sends the passing values (`_server_attrs`). The id comes from an unfiltered facet
call's `productTypeAggs` (`jlc_category_ids`, cached 7 days). Facet responses
carry `docCount` per value, which is how `pick` reports how many parts in the
category meet each limit on its own.

Keyword matching ANDs tokens and does not reliably index category names: `TVS` 355,
`TVS diode` 5 (mostly LEDs), `TVS` + `SMA(DO-214AC)` 0, while the category filter on
`SMA(DO-214AC)` finds 515 in-stock TVS parts. Hence the category resolution in `pick`.
Rows carry `describe`, a one-line spec string (`30V 40A 7.5mΩ@10V DFN-8(3x3) MOSFETs`),
used as `desc`.

## JLCPCB Basic vs Extended

LCSC has no concept of "Basic" - it is a JLC assembly-library field. So:

- `--basic` pools straight from JLC's base library (server-side filter, authoritative,
  ~1 call per keyword, and much faster than filtering LCSC results).
- Without `--basic`, JLC-pooled rows carry their library already; LCSC-pooled rows are
  annotated one lookup per displayed C-code. The `jlc` column shows `BASIC`, `ext` or `-`.
- **Do not infer Basic/Extended from a JLC keyword search**: its relevance ranking
  drops parts that do exist in the library, which shows up as a false `-`.

```bash
part.py jlc /mnt/project/meshtastic.net       # whole board, with refdes
part.py jlc C45783 C1525 C2650956
```

For "source this whole board" or "what's blocking assembly", use `check board.net`
instead of running `bom` then `jlc` separately - one netlist parse, one table with both
LCSC price and JLC Basic/Extended per line, plus the components with no LCSC code at
all (the ones actually blocking assembly, not just costing more).

`jlc` on a `.net` file parses the `LCSC Part` property via kicad-review's `knet.py`.
It must not regex for `C\d+` on a netlist - that matches capacitor refdes like `C108`.

## DigiKey setup

Optional. LCSC and JLC work with no configuration. For DigiKey, get free credentials at
developer.digikey.com (create an app, Production, Product Information V4), then:

```bash
export DIGIKEY_CLIENT_ID=...
export DIGIKEY_CLIENT_SECRET=...
```

or write `~/.config/partsearch/config.json` as
`{"digikey_client_id": "...", "digikey_client_secret": "..."}`.

Defaults are `--site CA --currency CAD`. The OAuth2 token is cached with its expiry.
DigiKey participates in `search`/`show` only, not in `pick`.

The DigiKey response normaliser (`_dk_norm`) has not been exercised against a live v4
response. If `selftest` shows auth OK but search DEAD, the field mapping there is the
place to look, not the request code.
