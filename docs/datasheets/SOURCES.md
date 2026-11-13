# Datasheet sources

One row per PDF in `docs/datasheets/<sheet>/`. **Source** is the maker's stable link where
one exists (ti.com `symlink`, u-blox, Espressif, JST, ... always serve the newest
revision), otherwise the LCSC copy, which is a frozen snapshot. To refresh: fetch the
source, compare its revision with **Rev on disk**, replace the file if newer, update the
row. LCSC rows: `part.py ds C.. --save --fresh --dir docs/datasheets/<sheet>`.
"Undated" rows have no stated revision; the PDF creation month stands in for one.
Companion docs (integration manuals, TRMs, reference designs) belong to the part named.

URLs checked 2026-11-13 (served a PDF) unless marked otherwise.


## mcu/ (Root, the MCU page)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| ESP32-S3-WROOM-1.pdf | ESP32-S3-WROOM-1U-N16R8 | C3013946 | v1.8 (2026-03-02) | https://www.espressif.com/sites/default/files/documentation/esp32-s3-wroom-1_wroom-1u_datasheet_en.pdf |  |
| ESP32-S3.pdf | ESP32-S3 (SoC inside the WROOM-1U) |  | v2.2 | https://www.espressif.com/sites/default/files/documentation/esp32-s3_datasheet_en.pdf | companion |
| ESP32-S3_HardwareDesignGuidelines.pdf | ESP32-S3 |  | master build 2026-09-23 | https://docs.espressif.com/projects/esp-hardware-design-guidelines/en/latest/esp32s3/esp-hardware-design-guidelines-en-master-esp32s3.pdf | companion |
| 3131M-32.768DT06LL-R50L.pdf | 3131M-32.768DT06LL-R50L | C41349295 | undated (PDF 2022-05) | https://datasheet.lcsc.com/datasheet/pdf/30cea5fb4d3c9bcf5a50b1cd00711031.pdf?productCode=C41349295 |  |

## rails/ (Rails)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| LM61460-Q1.pdf | LM61460AASQRJRRQ1 | C1855832 | SNVSB70F (2021-06) | https://www.ti.com/lit/ds/symlink/lm61460-q1.pdf |  |
| TPS25947.pdf | TPS259474ARPWR, TPS259474LRPWR | C3662807 | SLVSFC9C (2026-05) | https://www.ti.com/lit/ds/symlink/tps25947.pdf | the L variant (C2864845) is on GNSS |
| SN74LVC1G74.pdf | SN74LVC1G74DQER | C2870716 | SCES794G (2021-09) | https://www.ti.com/lit/ds/symlink/sn74lvc1g74.pdf |  |
| SX1M4.000M10F30TNN.pdf | SX1M4.000M10F30TNN | C5830655 | undated | https://datasheet.lcsc.com/datasheet/pdf/b2ab9bc57b3cb3682e9a86a5a5fb3e86.pdf?productCode=C5830655 |  |
| SMF6.0A.pdf | SMF6.0A (UMW) | C2758490 | undated (PDF 2026-05) | https://datasheet.lcsc.com/datasheet/pdf/becbeb3b8e08b3ed39608c67b9101395.pdf?productCode=C2758490 |  |
| CMLO1040H2R2MTT.pdf | CMLO1040H2R2MTT, CMLO1040H3R3MTT | C439019 | Rev.01 (PDF 2016-12) | https://datasheet.lcsc.com/datasheet/pdf/7029bbea28904fe50f59ffe7f6ef38a1.pdf?productCode=C439019 | series sheet, covers both values |

## gnss/ (GNSS)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| NEO-F10N.pdf | NEO-F10N-00B | C21709333 | UBX-23002117 R05 (2025-10-09) | https://content.u-blox.com/sites/default/files/documents/NEO-F10N_DataSheet_UBX-23002117.pdf |  |
| NEO-F10N_IntegrationManual.pdf | NEO-F10N-00B |  | UBXDOC-963802114-12193 R04 (2024-12-12) | https://content.u-blox.com/sites/default/files/documents/NEO-F10N_IntegrationManual_UBXDOC-963802114-12193.pdf | companion |
| B39162B8389P810.pdf | B39162B8389P810 | C5556161 | undated (PDF 2023-02) | https://datasheet.lcsc.com/datasheet/pdf/eab07ffddc7092a73be1e657f827b881.pdf?productCode=C5556161 |  |
| TPS7A20.pdf | TPS7A2033PDBVR | C2862740 | SBVS338H (2024-07) | https://www.ti.com/lit/ds/symlink/tps7a20.pdf |  |
| TPS22945.pdf | TPS22945DCKR | C47507 | SLVS832D (2014-07) | https://www.ti.com/lit/ds/symlink/tps22945.pdf | also on Connectors |
| TPD1E10B06.pdf | TPD1E10B06DPYR | C48260 | SLLSEB1G (2024-08) | https://www.ti.com/lit/ds/symlink/tpd1e10b06.pdf | also on LoRa |
| ESD351.pdf | ESD351DPYR | C1974929 | SLVSEO3 (2018-07, original) | https://www.ti.com/lit/ds/symlink/esd351.pdf |  |
| ESD501.pdf | ESD501DPYR | C42409135 | SLVSI68 (2024-11, original) | https://www.ti.com/lit/ds/symlink/esd501.pdf |  |
| U.FL-R-SMT-1.pdf | U.FL-R-SMT-1(80) | C88374 | undated (PDF 2016-11) | https://datasheet.lcsc.com/datasheet/pdf/ade8681cf25347ad817fa65440db6a2c.pdf?productCode=C88374 |  |
| MS621FE.pdf | MS621FE-FL11E |  | Micro Battery catalog rev05 (2023-03-30) | https://www.sii.co.jp/hubfs/40217095/MicroBattery_E_20230330_rev05-security.pdf | whole SII catalog; the file name changes per issue, find the newest on https://www.sii.co.jp/en/me/datasheets/ms-rechargeable/ms621fe/ |

## lora/ (LoRa)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| E22P_UserManual.pdf | E22P-915M30S | C51912672 | V1.3 (2026-03-02) | https://www.cdebyte.com/pdf-down.aspx?id=4333 | LCSC has no working copy |
| SX1262.pdf | SX1262 (core of the E22P) |  | DS.SX1261-2.W.APP Rev 2.2 (2024-12) | https://www.semtech.com/products/wireless-rf/lora-connect/sx1262 (unverified, Documentation tab) | companion |
| 0900LP15B0063001E.pdf | 0900LP15B0063001E | C17287560 | Ver. 1.3 (PDF 2018-03) | https://www.johansontechnology.com/datasheets/0900LP15B0063/0900LP15B0063.pdf |  |
| PGB1010603NR.pdf | PGB1010603NR | C10677 | revised 2015-07-27 | https://datasheet.lcsc.com/datasheet/pdf/bd2ede3a009945aca6237d3479e9b8ca.pdf?productCode=C10677 |  |
| SMA-KE-347-H13.5-1.6.pdf | SMA-KE-347-H13.5-1.6 | C5723200 | undated | https://datasheet.lcsc.com/datasheet/pdf/e3e9b75c1c094a8b2b2fd16eda6a22a3.pdf?productCode=C5723200 | also on GNSS |

## peripherals/ (Peripherals)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| TCAL9539-Q1.pdf | TCAL9539RTWRQ1 | C5377958 | SCPS285A (2023-08) | https://www.ti.com/lit/ds/symlink/tcal9539-q1.pdf |  |
| LSM6DSV16X.pdf | LSM6DSV16XTR | C5267406 | DS13510 Rev 4 | https://www.st.com/resource/en/datasheet/lsm6dsv16x.pdf (blocks scripted fetch; LCSC copy below): https://datasheet.lcsc.com/datasheet/pdf/7d66556bb254f99e058243c7371e051d.pdf?productCode=C5267406 |  |
| QMC6309.pdf | QMC6309 | C5439871 | Rev A (PDF 2023-02) | https://datasheet.lcsc.com/datasheet/pdf/205911d993bf6331650635faa1d02faa.pdf?productCode=C5439871 |  |
| 74LV1T34.pdf | 74LV1T34GWH | C5199768 | Rev. 4 (2023-12-06) | https://assets.nexperia.com/documents/data-sheet/74LV1T34.pdf (blocks scripted fetch; LCSC copy below): https://datasheet.lcsc.com/datasheet/pdf/df19ec8cb2d6ea0eac24c61fbab5c99e.pdf?productCode=C5199768 |  |
| TPD2E2U06QDCKRQ1.pdf | TPD2E2U06QDCKRQ1 | C915089 | SLLSEJ9E (2022-10) | https://www.ti.com/lit/ds/symlink/tpd2e2u06-q1.pdf |  |
| WS2816B.pdf | WS2816B | C22371534 | V1.0 (PDF 2022-05) | https://datasheet.lcsc.com/datasheet/pdf/33bcf57fa38933354736aabd35a09e83.pdf?productCode=C22371534 |  |
| HX_PM2.54-2X8P_WT.pdf | HX PM2.54-2x8P WT | C46635853 | undated | https://datasheet.lcsc.com/datasheet/pdf/e8393ec392585e7236b48b974e07907b.pdf?productCode=C46635853 |  |
| S4B-XH-SM4-TB.pdf | S4B-XH-SM4-TB(LF)(SN) | C161861 | undated (PDF 2025-07) | https://www.jst-mfg.com/product/pdf/eng/eXH.pdf | JST XH series |

## connectors/ (Connectors)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| ESDS314.pdf | ESDS314DBVR | C1847859 | SLVSEG9C (2024-02) | https://www.ti.com/lit/ds/symlink/esds314.pdf |  |
| SM04B-SRSS-TB.pdf | SM04B-SRSS-TB(LF)(SN), SM03B-SRSS-TB(LF)(SN) | C160404 | undated (PDF 2016-11) | https://www.jst-mfg.com/product/pdf/eng/eSH.pdf | JST SH series, covers both |
| WAFER-MX1.25-8PWB.pdf | WAFER-MX1.25-8PWB | C3029365 | undated | https://datasheet.lcsc.com/datasheet/pdf/f6cb73e789ca6a2f7227478908a083e9.pdf?productCode=C3029365 |  |
| HX_PM2.54-1X4P_WT.pdf | HX PM2.54-1x4P WT | C46061768 | undated | https://datasheet.lcsc.com/datasheet/pdf/99ddc9d8507d86889040b9d15f9062e7.pdf?productCode=C46061768 |  |
| HX_PM2.54-1X6P_WT.pdf | HX PM2.54-1x6P WT | C46061770 | undated | https://datasheet.lcsc.com/datasheet/pdf/36e50f33715c9e0afff257c29c686749.pdf?productCode=C46061770 |  |
| DS1023-1X4SF11.pdf | DS1023-1x4SF11 | C7509546 | undated (PDF 2021-03) | https://datasheet.lcsc.com/datasheet/pdf/e746529444b0a48d4f291628523ddcfb.pdf?productCode=C7509546 |  |

## charger/ (Charger)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| BQ25798.pdf | BQ25798RQMR | C2876593 | SLUSDV2C (2026-06) | https://www.ti.com/lit/ds/symlink/bq25798.pdf |  |
| TIDA-050047_DesignGuide.pdf | BQ25798 + TPS25751 reference design |  | TIDUEY1C (2025-06) | https://www.ti.com/lit/pdf/tiduey1 | companion |
| CSD18512Q5B.pdf | CSD18512Q5B | C2863663 | SLPS624A (2019-03) | https://www.ti.com/lit/ds/symlink/csd18512q5b.pdf |  |
| CSD17577Q3A.pdf | CSD17577Q3A | C201632 | SLPS515A (2016-01) | https://www.ti.com/lit/ds/symlink/csd17577q3a.pdf |  |
| SI2308.pdf | SI2308 (HXY) | C5337194 | undated (PDF 2010-08) | https://datasheet.lcsc.com/datasheet/pdf/2cc3affb8fe8db16c53ba117249df2e3.pdf?productCode=C5337194 |  |
| PZU12B2L.pdf | PZU12B2L,315 | C553593 | Rev. 2 (2019-11-11) | https://datasheet.lcsc.com/datasheet/pdf/cb07444574d94d9572a871aec1cc48b4.pdf?productCode=C553593 | Nexperia PZUxBL series |
| TMPA0603S.pdf | TMPA0603S-2R2MN-D | C357100 | undated (PDF 2020-05) | https://datasheet.lcsc.com/datasheet/pdf/5f2e591fa661f4b59d541b4dafce0a94.pdf?productCode=C357100 |  |
| S2B-PH-SM4-TB.pdf | S2B-PH-SM4-TB(LF)(SN) | C295747 | undated (PDF 2019-08) | https://www.jst-mfg.com/product/pdf/eng/ePH.pdf | JST PH series |

## bms/ (BMS)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| MAX17320.pdf | MAX17320G22+ | C2914309 | 19-100749 Rev 12 (7/25) | https://www.analog.com/media/en/technical-documentation/data-sheets/max17320.pdf (blocks scripted fetch; LCSC copy below): https://datasheet.lcsc.com/datasheet/pdf/24ef1a11d0cecb8c476f3ef3aa71b506.pdf?productCode=C2914309 |  |
| MAX17320_ImplementationGuide.pdf | MAX17320G22+ |  | UG7177 Rev 0 (3/20) | https://www.analog.com/en/products/max17320.html (unverified, Documentation tab) | companion |
| AON7544.pdf | AON7544 | C315567 | Rev 0 (2012-04) | https://datasheet.lcsc.com/datasheet/pdf/284fe12c8a629d82609bec3e8b59373e.pdf?productCode=C315567 |  |
| TPN2R203NC.pdf | TPN2R203NC,L1Q(M | C471286 | Rev.5.0.A | https://datasheet.lcsc.com/datasheet/pdf/c504eca6fa6e6679e9133e3d84b63218.pdf?productCode=C471286 |  |
| BZX585-C15.pdf | BZX585-C15,135 | C550633 | Rev. 9 (2026-09-11) | https://assets.nexperia.com/documents/data-sheet/BZX585_SER.pdf (blocks scripted fetch; LCSC copy below): https://datasheet.lcsc.com/datasheet/pdf/14f361462d28dd8161b8ec04bb35ef76.pdf?productCode=C550633 |  |
| BSMD2920L-1000-21V.pdf | BSMD2920L-1000-21V | C50338483 | 2025 (PDF 2025-08) | https://datasheet.lcsc.com/datasheet/pdf/14d81d7958d374dbae10273735207212.pdf?productCode=C50338483 |  |
| YHR0805SE1DR005F5.pdf | YHR0805SE1DR005F5 | C42454064 | 2025-04-22 | https://datasheet.lcsc.com/datasheet/pdf/0b4e40e8836ae059409dd4c822a860ed.pdf?productCode=C42454064 |  |
| NCP18XH103F03RB.pdf | NCP18XH103D03RB | C89600 | Murata cat. R44E-15 (2012-12-17) | https://datasheet.lcsc.com/datasheet/pdf/235eb95183ff04d8ebcd64ad74d822ff.pdf?productCode=C89600 | NCP18 series catalog; thermistors are on BMS and Charger |
| BH-21700-B1BJ001.pdf | BH-21700-B1BJ001 | C20606791 | undated (PDF 2023-11) | https://datasheet.lcsc.com/datasheet/pdf/f19fdde684eed5e7f56342763817f937.pdf?productCode=C20606791 |  |
| 1.25-2PWB.pdf | 1.25-2PWB | C2905009 | undated (PDF 2023-11) | https://datasheet.lcsc.com/datasheet/pdf/06ca0829dd2139bfdbe6671a038f533c.pdf?productCode=C2905009 | BAT_PORT, DNP |

## usb/ (USB Interface)

| File | MPN | LCSC | Rev on disk | Source | Note |
|---|---|---|---|---|---|
| TPS25751.pdf | TPS25751DREFR | C30169739 | SLVSH93A (2024-03) | https://www.ti.com/lit/ds/symlink/tps25751.pdf |  |
| TPS25751_TechnicalReferenceManual.pdf | TPS25751DREFR |  | SLVUCR8B (2026-08) | https://www.ti.com/lit/pdf/slvucr8 | companion |
| TPD8S300A.pdf | TPD8S300ARUKR | C2649824 | SLVSEL9 (2018-06, original) | https://www.ti.com/lit/ds/symlink/tpd8s300a.pdf |  |
| TVS2200.pdf | TVS2200DRVR | C523793 | SLVSED5C (2023-08) | https://www.ti.com/lit/ds/symlink/tvs2200.pdf | also on Charger |
| CSD15380F3.pdf | CSD15380F3 | C529568 | SLPS579B (2022-02) | https://www.ti.com/lit/ds/symlink/csd15380f3.pdf |  |
| CSD23280F3T.pdf | CSD23280F3T | C139374 | Rev A (2017-08) | https://www.ti.com/lit/ds/symlink/csd23280f3.pdf |  |
| M24512-DF.pdf | M24512-DFMC6TG | C222237 | Rev 31 (2020-10) | https://www.st.com/resource/en/datasheet/m24512-df.pdf (blocks scripted fetch; LCSC copy below): https://datasheet.lcsc.com/datasheet/pdf/7b78bae50192fc535030ea4049063e16.pdf?productCode=C222237 |  |
| USB4105-GF-A-060.pdf | USB4105-GF-A-060 | C3025063 | drawing 2023-12-18 | https://gct.co/files/drawings/usb4105.pdf |  |
