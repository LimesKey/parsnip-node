# Passive brands (R and C) - who they are, and breaking a price tie

Researched 2026-09-23 (MLCC makers), extended to resistors and capacitors 2026-11-12.

Read this when `pick` ranks an unfamiliar brand first ("is this brand OK"), or when
two or more parts pass every limit at nearly the same price and one must be chosen.
It exists so neither question is re-searched every session. **Brand is not a `pick`
ranking input**: `pick` never sorts or filters on it, and should not. Brand only
breaks ties. No public per-brand field-failure data exists, so an unknown brand is
*unknown*, not bad.

## Breaking a price tie

A tie: every candidate passes every electrical limit and the stock floor, and its
`ext` at the real buy quantity is within 10% of the cheapest, or within ~0.50 (display
currency) on the line.

1. The highest `score` wins.
2. Equal score: higher Q, then higher E, then more stock, then cheaper.
3. A brand in neither table: research it (the same kinds of source as below, ~3
   searches) and add a row before it decides a tie. If that can't be done now, score
   it 6 (Q1 D1 E3) and say so.
4. Say it in the answer, e.g. `price tie (+6%): Murata 20 over FH 11`.
5. Outside a tie, brand never beats a limit, the stock floor or a cheaper price. The
   one exception is the MLCC rule 2 at the bottom.

**score = 2Q + D + E**, max 20. Q counts double: a part that does not fail outweighs
better paperwork or conduct. Ownership and location are listed, not scored.

| brand (LCSC name) | makes | HQ, owner | score | Q | D | E | why |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Murata | MLCC | Kyoto JP, listed | 20 | 5 | 5 | 5 | #1 MLCC (>40%); SimSurfing; RBA |
| TDK | MLCC, film | Tokyo JP, listed | 20 | 5 | 5 | 5 | DC-bias SPICE models; RBA (2020) |
| Samsung Electro-Mechanics | MLCC | Suwon KR, Samsung group | 19 | 5 | 5 | 4 | #2 MLCC (24%); 2021 KFTC fine cancelled on appeal 2026-04 (KFTC appealing); said to join RBA 2023, absent from its list |
| Taiyo Yuden | MLCC | Tokyo JP, listed | 19 | 5 | 5 | 4 | DC-bias/temperature SPICE models; not on RBA list |
| KEMET | Ta, MLCC, film, Al | Fort Lauderdale US; YAGEO since 2020 | 19 | 5 | 5 | 4 | K-SIM; own validated conflict-free DRC tantalum chain; not on RBA list |
| Vishay (Dale, Draloric, Beyschlag) | R, Ta, film, MLCC | Malvern US, listed | 19 | 5 | 4 | 5 | RBA; its 2018 EU cartel line (Vishay Polytech) is Holy Stone's pre-acquisition conduct |
| Kyocera AVX | MLCC, Ta | Fountain Inn US; Kyocera 100% since 2020 | 18 | 5 | 5 | 3 | SpiCAP; RBA; USD 366M New Bedford Harbor PCB settlement (2012) |
| Würth Elektronik | MLCC, Al, film | Waldenburg DE; Würth Group, family-owned | 17 | 4 | 5 | 4 | REDEXPERT; not on RBA list |
| KOA (KOA Speer) | R | Nagano JP, listed | 17 | 5 | 4 | 3 | resistor price-fixing suits settled: US USD 18.5M, CA CAD 3.3M |
| ROHM | R | Kyoto JP, listed | 17 | 5 | 4 | 3 | resistor suits settled: US USD 6.5M, CA CAD 1.55M |
| Susumu | thin-film R | Kyoto JP, unlisted | 17 | 5 | 4 | 3 | precision thin film; CA resistor suit settled CAD 90k |
| Bourns | R, protection | Riverside US, private | 17 | 4 | 4 | 5 | RBA |
| Panasonic | R, Al, polymer, film | Osaka JP; Panasonic Holdings | 16 | 5 | 4 | 2 | in the 1998-2012 capacitor cartel (via Sanyo), EU immunity as whistle-blower; resistor suits settled US USD 12M, CA CAD 2.35M; RBA |
| YAGEO | R, MLCC | Taipei TW, listed | 15 | 4 | 3 | 4 | owns KEMET, Pulse, Shibaura (2025); no per-part DC-bias tool found; not on RBA list |
| Walsin | MLCC, R | TW, listed; Walsin Lihwa ~18% | 15 | 4 | 3 | 4 | leads the PSA group with PDC; no per-part DC-bias tool found |
| Stackpole | R | Raleigh US, private; Akahane group (JP) | 15 | 4 | 3 | 4 | plants JP, TW, CN, US, MX |
| Nichicon | Al, polymer | Kyoto JP, listed | 15 | 5 | 4 | 1 | US cartel plea 2017 USD 42M; EU 2018 EUR 72.9M with no leniency cut, upheld 2021 |
| CCTC (Three-Circle) | MLCC | Chaozhou CN; SZ 300408 + HK 6951 | 13 | 3 | 3 | 4 | Chaozhou Sanjiang Investment ~34%; English-only search |
| UNI-ROYAL (Uniroyal Elec) | R | Hsinchu TW, private; CN + TH plants | 13 | 3 | 3 | 4 | IATF 16949; ROYALOHM / UNIOHM; English-only search |
| FH (Guangdong Fenghua) | MLCC, R | Zhaoqing CN, SZ 000636; Guangdong Rising (provincial) ~24% | 11 | 3 | 3 | 2 | CSRC 2019: 2015-16 reports overstated profit by >CNY 60M |

Also seen, part-researched (`3?` = E not researched, scored 3):

| brand | makes | HQ, owner | score | Q | D | E | why |
| --- | --- | --- | --- | --- | --- | --- | --- |
| Nippon Chemi-Con | Al, polymer | Tokyo JP, listed | 15 | 5 | 4 | 1 | US plea USD 60M + 5 y probation; EU 2018 EUR 97.9M (largest) |
| Rubycon | Al | Nagano JP | 15 | 5 | 4 | 1 | US plea 2016; EU 2018 ~EUR 28M, upheld 2021 |
| Viking Tech | thin-film R | Hsinchu TW, listed 2011 | 12 | 3 | 3 | 3? | |
| EYANG | MLCC | Shenzhen CN | 12 | 3 | 3 | 3? | see facts below |
| PSA / PDC | MLCC | TW, Walsin-led PSA | 12 | 3 | 3 | 3? | see facts below |
| FOJAN | R | Ma'anshan CN, private; FOSAN's resistor arm | 12 | 3 | 3 | 3? | |
| Holy Stone | MLCC | TW | 10 | 3 | 3 | 1 | US plea 2016; EU 2018 fine |
| Chinocera, AIDE | MLCC | unknown | 6 | 1 | 1 | 3? | no company profile found |

### What the scores mean

**Q**, quality and process maturity: 5 top-tier maker of that part type, broad
AEC-Q200 range; 4 large established maker with AEC-Q200 lines, commodity-focused or
a catalog brand; 3 large CN/TW maker, AEC-Q200 / IATF 16949 on some series, little
independent reliability data; 2 small maker, minimal documentation; 1 provenance
unknown.

**D**, data you can check a choice against (MLCC-weighted; for a resistor 3 is
plenty): 5 per-part DC-bias/temperature curves or a simulator (Murata SimSurfing,
TDK and Taiyo Yuden DC-bias SPICE, KEMET K-SIM, Kyocera AVX SpiCAP, Würth
REDEXPERT); 4 full series datasheets with derating, life and ripple curves; 3 series
datasheet with ratings and dimensions; 2 a series sheet at best; 1 a spec line.

**E**, conduct and governance: the lowest row that applies, counting only the
company's own conduct (not a seller's before an acquisition). 5 nothing adverse
found and on the RBA (Responsible Business Alliance) member list; 4 nothing adverse
found, not on it (weak evidence for a small private maker); 3 a settled civil
price-fixing suit (no admission) or a major environmental settlement; 2 a cartel
fine or admission in one jurisdiction (whistle-blower immunity included) or a
securities-fraud penalty; 1 a US criminal cartel plea plus an EU cartel fine.

Facts behind the scores, with sources (MLCC makers 2026-09-23):

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

Resistors, capacitors and conduct (2026-11-12):

- RBA member list, fetched 2026-11-12: Murata, TDK, Kyocera and Kyocera AVX,
  Panasonic Holdings, Vishay, Bourns and TE Connectivity are on it. Taiyo Yuden,
  KEMET, YAGEO, Walsin, Samsung Electro-Mechanics, ROHM, KOA, Würth, Nichicon,
  Stackpole, Susumu, FH, CCTC, UNI-ROYAL, Nippon Chemi-Con and Rubycon are not.
  https://www.responsiblebusiness.org/about/members/
- Electrolytic/tantalum capacitor cartel, 1998-2012: EU fined eight groups ~EUR 254M
  in 2018 (Nippon Chemi-Con 97.9M, Nichicon 72.9M, Rubycon ~28M, Hitachi Chemical
  18.5M, ELNA 18.2M, Tokin/NEC 16.4M, Matsuo 0.8M, Vishay Polytech / Holy Stone
  0.8M). Panasonic/Sanyo got full immunity as the whistle-blower (~EUR 32.4M
  avoided); Nichicon alone got no leniency cut. The General Court upheld the fines
  2021-09-29. https://cms.law/en/bel/legal-updates/european-commission-imposes-fine-of-254-million-on-capacitor-cartel ,
  https://passive-components.eu/five-japanese-capacitor-makers-lose-challenge-against-298-mln-eu-cartel-fine
- Same cartel, US: eight companies pleaded guilty, >USD 150M in fines; Nichicon
  USD 42M (2017), Nippon Chemi-Con USD 60M plus 5 years' probation, NEC Tokin
  USD 13.8M, Rubycon, ELNA and Holy Stone pleaded in 2016. Vishay bought Holy Stone
  Polytech (now Vishay Polytech) and Holy Stone keeps the liability under an
  indemnity. https://www.justice.gov/opa/pr/seventh-company-agrees-plead-guilty-fixing-prices-electrolytic-capacitors ,
  https://justice.gov/archives/opa/pr/leading-electrolytic-capacitor-manufacturer-ordered-pay-60-million-criminal-fine-price-fixing ,
  https://www.justice.gov/opa/pr/three-companies-agree-plead-guilty-fixing-prices-electrolytic-capacitors ,
  https://www.justice.gov/media/918951/dl
- Linear-resistor price-fixing (class period 2003-2015), civil settlements, no
  admission: US direct purchasers Panasonic USD 12M, ROHM 6.5M, Kamaya 5.25M,
  Hokuriku 2M; US indirect >USD 33M incl. KOA 18.5M. Canada: KOA CAD 3.3M, Panasonic
  2.35M, ROHM 1.55M, HDK 0.91M, Kamaya 0.77M, Susumu 0.09M.
  https://topclassactions.com/lawsuit-settlements/consumer-products/electronics/33m-settlement-ends-panasonic-resistor-price-fixing-class-action ,
  https://www.siskinds.com/class-action/linear-resistors/
- Inductor price-fixing suit (2018, N.D. Cal.) against Murata, TDK, Taiyo Yuden,
  Panasonic, Sumida and others: dismissed with prejudice. Not scored against anyone.
  https://www.lw.com/people/admin/upload/SiteAttachments/REC2142022544288Latham.pdf
- Kyocera AVX: USD 366.25M in 2012 to finish the New Bedford Harbor PCB cleanup, as
  successor to Aerovox, the largest single-site Superfund cash-out.
  https://www.justice.gov/opa/pr/avx-corp-pay-366-million-settlement-accelerating-cleanup-new-bedford-harbor-contamination
- KEMET: a closed-pipe tantalum chain from the Kisengo mine (DRC), validated under
  RMAP, per its SEC conflict-minerals reports to FY2020.
  https://www.sec.gov/Archives/edgar/data/887730/000088773020000025/fy2020_exhibit101-conflict.htm
- Samsung Electro-Mechanics: KRW 10.5bn of the 2021 KFTC Welstory catering fine,
  cancelled by the Seoul High Court 2026-04-23; the KFTC appealed to the Supreme
  Court 2026-05. Said to join the RBA in 2023, but it is absent from the list above.
  https://www.asiae.co.kr/en/article/2026042315432217144 ,
  https://amp.kedglobal.com/newsAmp/ked202307120002
- YAGEO: Shibaura Electronics tender succeeded 2025-10 (87.3%) over Minebea Mitsumi;
  raised MLCC prices repeatedly in the 2017-18 shortage, no enforcement found. No
  per-part MLCC DC-bias tool found (TDK, Murata and Taiyo Yuden have one).
  https://english.adnkronos.com/2025/10/21/yageos-tender-offer-for-shibaura-electronics-succeeds/ ,
  https://www.taipeitimes.com/News/biz/archives/2017/11/20/2003682564 ,
  https://product.tdk.com/en/products/capacitor/ceramic/mlcc/designtool.html
- FH: CSRC Guangdong penalty 2019-11 for false 2015-2016 reports (profit overstated
  by >CNY 60M): company fined CNY 400k, 26 staff incl. three chairmen CNY 1.87M.
  Largest holder Guangdong Rising (~24%), controlled by Guangdong province.
  https://www.yicai.com/news/100414254.html ,
  https://simplywall.st/stocks/cn/tech/szse-000636/guangdong-fenghua-advanced-technology-holding-shares/ownership
- CCTC: largest holder Chaozhou Sanjiang Investment (~34%); HK listing 2026-07-09.
  https://simplywall.st/stocks/cn/tech/szse-300408/chaozhou-three-circle-groupltd-shares/ownership
- Walsin Technology: Walsin Lihwa largest holder (~18%).
  https://simplywall.st/stocks/tw/tech/twse-2492/walsin-technology-shares/ownership
- UNI-ROYAL: founded 1978 Hsinchu; plants Kunshan, Xiamen, Thailand; IATF 16949;
  brands ROYALOHM, UNIOHM (LCSC's profile). https://www.lcsc.com/blog/?p=669
- Stackpole: private, Raleigh NC, part of the Japanese Akahane group; plants JP, TW,
  CN, US, MX (directory profiles, no filing).
  https://www.manufacturing.net/home/electrical-electronic/company/20831480/stackpole-electronics-inc
- Susumu: Kyoto, 1964, new plant Itoigawa 2020; one profile says private, another
  a subsidiary. https://us.metoree.com/companies/3082/
- FOJAN: Anhui Fujie Electronic Technology, Ma'anshan, private, the resistor
  division of FOSAN. https://www.lcsc.com/brand-detail/13046.html
- Viking Tech: Hsinchu, founded 1997, listed in Taiwan 2011, thin-film resistors.
  https://www.viking.com.tw/en/page/milestone.html

MLCC: how much the brand matters by position:

1. Bypass / decoupling at a low fraction of rated voltage: any brand; the cheapest
   wins unless it is a price tie.
2. A position where effective capacitance matters (buck input/output, bulk at a
   high DC bias): `kcap.py compare` either way. D 5 lets you check the kcap estimate
   against the maker's own curve; D 3 cannot, so leave more margin, or take a D 5
   part even outside a price tie when the margin is thin.
3. A safety or across-the-battery position: Q >= 3 with a real series datasheet
   (`part.py ds C... --save`, then `kdoc.py grep`).

Not yet researched: SAMWHA, RALEC, Lelon, TE Connectivity (resistors; on the RBA
list). Add a row when a brand keeps showing up at the top of `pick`; keep each claim
sourced and dated like the ones above.
