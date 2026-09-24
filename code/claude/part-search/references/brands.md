# MLCC brands - who they are (researched 2026-09-23)

Read this when `pick` ranks an unfamiliar MLCC brand first and the question is
"is this brand OK". It exists so that question is not re-searched every session.
**It is not a ranking input**: `pick` never sorts or filters on brand, and should
not. No public per-brand field-failure data exists, so an unknown brand is
*unknown*, not bad. What the brand does change is what you can verify.

| tier | brands (LCSC names) | what you get |
| --- | --- | --- |
| 1 - global majors | Murata, Samsung Electro-Mechanics, TDK, Taiyo Yuden, Kyocera AVX, KEMET, Yageo, Walsin | per-part DC-bias and temperature curves (Murata SimSurfing, Samsung/TDK/Taiyo tools), full reliability data, automotive (AEC-Q200) lines |
| 2 - large listed makers | FH (Guangdong Fenghua), CCTC (Chaozhou Three-Circle), EYANG, PSA / PDC (Walsin's group) | series datasheets with ratings and dimensions, some AEC-Q200 lines; per-part DC-bias curves usually absent, so derate with `kcap.py` |
| 3 - provenance unknown | Chinocera, AIDE, most names that appear only on LCSC/JLC | a spec line and maybe a series sheet; no company profile found. Fine for non-critical bypass, verify before a derated bulk or safety position |

Facts behind the tiers, with sources:

- Murata held over 40% of the MLCC market and Samsung Electro-Mechanics 24% in
  H1 2024; Chinese makers (Fenghua, Three-Circle, Eyang) reached ~10% of revenue in
  H2 2024 and "still lag behind in technical capabilities" (passive-components.eu,
  2025-06-16, citing Business Post / Futubull).
  https://passive-components.eu/chinas-mlcc-makers-reach-10-market-share/
- Chaozhou Three-Circle (CCTC): founded 1970, listed Shenzhen 300408 and Hong
  Kong 6951; Frost & Sullivan ranked it the world's 7th-largest supplier of
  electronic ceramic materials and components in 2025 (Wikipedia). Its own site
  lists AEC-Q200 MLCC series. https://en.wikipedia.org/wiki/Chaozhou_Three-Circle ,
  https://www.cctc.cc/index_en.html
- Guangdong Fenghua Advanced Technology (FH): founded 1984, Zhaoqing, listed
  Shenzhen 000636; MLCCs plus resistors, inductors and ceramic powder (investing.com
  profile). https://www.investing.com/equities/fenghua-adv-a-company-profile
- PSA / Prosperity Dielectrics (PDC): Taiwan, founded 1990, allied with Walsin
  Technology since 2005 in the Walsin-led Passive System Alliance (PSA) group
  (Digitimes; PDC's own profile). PDC's "number 5 worldwide" claim is its own
  marketing, not an independent ranking.
  https://www.digitimes.com/news/a20210128PD205.html
- EYANG (Shenzhen Eyang Tech Development): founded 2001, plants in Guangdong and
  Anhui. Its "first in China, third in the world" micro-MLCC capacity figure is
  self-reported. https://www.lcsc.com/brand-detail/1268.html
- Chinocera: 438 MLCCs on LCSC (2026-09-23), no company description on LCSC's
  own brand page, no independent profile found. AIDE: nothing found.
  https://www.lcsc.com/brand/1142-12099.html

How to use it:

1. Bypass / decoupling at a low fraction of rated voltage: any tier; take the cheap one.
2. A position where effective capacitance matters (buck input/output, bulk at a
   high DC bias): `kcap.py compare` either way. Tier 1 lets you check the kcap
   estimate against the maker's own curve; tier 2/3 cannot, so leave more margin.
3. A safety or across-the-battery position: prefer tier 1-2 with a real series
   datasheet (`part.py ds C... --save`, then `kdoc.py grep`).

Not yet researched (seen in `pick` output, do not guess a tier): SAMWHA, Holy
Stone, FOJAN. Refresh this file when a brand keeps showing up at the top of `pick`
and is not listed; keep each claim sourced and dated like the ones above.
