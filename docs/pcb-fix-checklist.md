# parsnip-node PCB fix checklist

Regenerated **2026-09-15** from a full tool-verified review, each dimension
re-checked against the live board / merged netlist. **The GNSS NEO-M9N ->
NEO-F10N rework (commit `5b76dbf`) is mid-flight**: the board is **not** fully
placed (432/439, 7 GNSS parts unplaced + 2 duplicate footprints) and the rework
re-introduced copper shorts. Finish the rework (P0) before trusting anything
downstream.

**Live DRC+ERC: 425 error / 437 warn** (`kdrc.py parsnip.kicad_pcb`, all 3
roots; 204 documented false positives suppressed via `kdrc.json`) - up from
381/425 on 2026-09-13, and the increase is almost entirely GNSS-rework fallout.

Global verify after each fix: `python3 code/claude/kicad-review/scripts/kdrc.py parsnip.kicad_pcb`

## What changed since 2026-09-13

- **GNSS rework landed mid-flight (commit `5b76dbf`, 2026-09-14).** NEO-M9N ->
  NEO-F10N: new LDO U10 (TPS7A2033), dual-band SAW filter + matching (L6/L7),
  extra caps. It left **7 parts unplaced** (C104 C107 C130 L6 L7 R134 C100~dup)
  and **2 duplicate footprints** (C100~dup, C104~dup). See P0.
- **Copper shorts are BACK - the old "35 shorts GONE, retired" line is reversed.**
  `kdrc --only DRC:SHORTING_ITEMS` = **15** now. Not the old 35; a new set from the
  rework (C100 dup, U10) plus a few plane-via crossings. P0 below.
- **Undersized vias 11 -> 4** (7 fixed), **annular 56 -> 49**. Partial progress.
- **CLEARANCE 22 -> 47** and **SOLDER_MASK_BRIDGE 7 -> 22** - both jumps are almost
  entirely the GNSS rework (D10 RF path, C100 dup, U10). The real connector
  mask-bridges (J6/J10/J2) are unchanged at 7.
- Merged netlist is now **437 comps / 279 nets** (was 455/281; CLAUDE.md's count
  is stale, but that is a separate doc). `parsnip.kicad_pcb.bak-2026-09-09`
  predates all of this.
- **`sync` is no longer pure SYNCNET skew:** 137 SYNCNET (net-name prefix, a
  non-issue) **plus 2 SYNCPART** (C100~dup / C104~dup, the rework duplicates -
  real, fix in P0).
- Still true from 2026-09-13: `copper_finish` ENIG, R51 has C861485, U2 decoupling
  done.

---

## P0 - Blockers (do not ship, do not trust downstream analysis)

- [ ] **GNSS NEO-F10N rework is incomplete - finish it first.** Commit `5b76dbf`
      swapped the GNSS front end but left it half-placed:
      - **7 unplaced parts, all `/GNSS/`:** C104, C107, C130, L6, L7, R134,
        C100~dup (`kpcb summary`). L6/L7 are the NEO-F10N dual-band SAW matching
        (memory: B39162B8389P810, needs ~5.1 nH). Place them.
      - **2 duplicate footprints, C100~dup + C104~dup** (`sync` SYNCPART). C100~dup
        physically **shorts 3V3_GNSS to GND** and drives 6 of the mask-bridge hits.
        Delete the duplicates.
      - **U10 (TPS7A2033 LDO) input path unresolved:** pins IN(1) and EN(3) both
        sit on `Net-(U10-EN)`, its 0R (R134) is unplaced, and +5V copper **shorts
        to Net-(U10-EN)** at U10 @104,87. Wire +5V -> U10.IN and place R134.
      - **Schematic NC markers off** (knet: only 10/12 no_connect markers land on a
        pin) - clean the GNSS sheet too, not just the board.
      Until this lands, `sync`/DRC over the GNSS box is meaningless and every RF /
      connectivity claim about the GNSS front end is unverifiable.
- [ ] **15 copper shorts (`SHORTING_ITEMS`) - real, do not ship.** Beyond the GNSS
      ones above (C100, U10):
      - **+3V3 <-> GND:** 4 GND vias @144.2/144.3/145.1/145.4, y83-87 cross a +3V3
        track on In2.Cu (near U15/L5). Move the vias or the track.
      - **Net-(U7-TH2) <-> GND** @127.5,87.6 (x4) - the BMS thermistor-2 return
        touches GND copper. Pull it clear.
      Re-run `kdrc --only DRC:SHORTING_ITEMS` after each - target 0.
- [ ] **`production/` bundle is stale (2026-09-13) - regenerate before submission.**
      It predates the GNSS rework and the current shorts, so it does not match the
      board on disk. Regenerate the whole bundle (Gerber + drill + P&P + BOM) only
      **after** the P0 shorts and the P1 track/via/mask items land, then diff
      `production/bom.csv` against the live 437-part netlist. (`positions.csv` has
      fewer rows than the part count because P&P excludes the through-hole parts -
      not a missing-parts bug, don't chase it.)

---

## P1 - Fab-blocking geometry (blocks a clean DRC release + bundle regen)

- [ ] **199 `TRACK_WIDTH` errors** = 49 signal + 150 power (`kdrc --only DRC:TRACK_WIDTH`).
      - **49 outer signal tracks at 0.10 mm** (rule 'Min Trace Width (outer)').
        Dominant nets now: I2C_PMIC_IRQ 12, PD_LDO_3V3 7, I2C_PMIC_SDA 4,
        /USB Interface/FAULT 4, plus scattered GND. Widen to >= 0.15 mm (see the
        copper-weight reconcile in P2 - if outer is actually 2 oz, 0.10 mm will not
        etch). Low-current nets, width is free.
      - The **150 power segments** (rule 'pwr_min_width', 1.0 mm) are the same
        copper as the ampacity item in P2 and are mostly the tool crying wolf on
        pad necks - -BATT 83, VSYS 41, PPHV 23. Fix/relax there, not here.
- [ ] **4 undersized through-vias: 0.25 mm pad / 0.15 mm drill** (down from 11 -
      7 were fixed). Each trips `VIA_DIAMETER`, `DRILL_OUT_OF_RANGE` and 1 annular
      hit at once. 0.15 mm drill is below JLC/PCBWay standard 0.2 mm and the
      board's own DRU. Remaining: @109.6,130.2 (I2C_HOST_SCL) ; @109.8,130.5
      (I2C_HOST_SDA) ; @114.3,106.3 (I2C_PMIC_IRQ) ; @126.3,145.5 (PD_LDO_3V3).
      Re-stitch to the standard 0.5 mm pad / 0.2 mm drill through-via.
- [ ] **49 `ANNULAR_WIDTH`** (board min 0.15 mm) = 4 @0.05 mm (the vias above)
      + ~43 @0.10 mm GND-stitching / rail vias (unambiguous rule violations, at or
      below JLC ~0.13 mm min annular - e.g. @119.2,108.7 GND, @131.2,80.4 +5V)
      + 2 @0.00 mm BT1/BT2 holder PTH posts. Bump the 43 via pads to 0.5 mm on
      0.2 mm drill.
- [ ] **22 `SOLDER_MASK_BRIDGE`, but only 7 are real fab work.** The real ones are
      the connector pads (unchanged since 2026-09-13): J6 x2 @97.3,139.1 (front) ;
      J10 x4 @136-145,163.x (rear) ; J2 x1 @136.7,163.8 (rear) - different-net pads
      sharing a mask aperture thinner than ~0.1 mm. Widen the per-pad dam or tent;
      J10 (solar) and J2 rear worst. The other **15 (C100 x6, U10 x9) are
      GNSS-rework artifacts** and clear when P0 lands - do not chase them.
- [ ] **11 `COPPER_EDGE_CLEARANCE`: a GND via cluster @97.8,35.3** (top-left
      corner) sits inside the 'Copper to Board Edge' rule margin. Pull the via(s)
      in from the corner.
- [ ] **9 `PTH`/`NPTH_INSIDE_COURTYARD` - drilled holes under other parts' bodies.**
      TP1/TP2/TP4 PTH land inside the **BT2 holder** courtyard (@102-123, y93-124);
      J7 and J1 have PTH+NPTH inside each other's courtyard (@148-153, y142-144).
      This is the DRC-hard version of the "test points under the holders" item in
      P2 - the cells will not seat over the barrels. Convert the TPs to SMD or
      relocate; check the J7/J1 overlap against the mechanical drawing.
- [ ] **4 `MALFORMED_COURTYARD` (footprint-level): BT1 and J14.** BT1's courtyard
      is self-intersecting / not closed; J14's is self-intersecting. Fix the
      footprints - they also make the OVERLAP/edge checks noisier. Not routing.

---

## P2 - Real, not fab-blocking

### Power / charge-path ampacity (re-verified 2026-09-13 - mostly tool artifacts)

The `ampacity` TRACE-THIN flags on these nets are heavily confounded and were
over-stated in the first pass. Real residual is small. `pwr_min_width` (1.0 mm on
every PWR_HIGH net) generates 150 of the 199 TRACK_WIDTH errors by flagging pad
necks and short segments (-BATT alone is 83) - relax it to the real trunk widths
so it stops crying wolf.

- [x] **-BATT - not a real bottleneck (mixed-net artifact).** knet confirms -BATT
      is the MAX17320 ground/sense reference (22 nodes: BT2-, R41 shunt, U7 GND,
      7 decoupling caps, **all four thermistors TH1-TH4**). The 0.27 A flag is on
      the TH2 return tap at y~59 (~0 A). Only the short BT2.2 -> R41(shunt) link
      carries the 2.7 A pack current - confirm that link is wide and ignore the
      rest. (Logged as a known `ampacity` mixed-net limitation in SKILL-BACKLOG.)
- [x] **PPHV - pad neck, cannot and need not widen.** The 1.45 A F.Cu bottleneck
      is the cap-pad connection at C64.1 (0.3 mm away), already as wide as the pad.
      1.45 A on a short outer neck is fine for realistic PPHV current. Pad-neck
      artifact (also logged).
- [ ] **VBUS / +PACK / VSYS - verify only the outer 0.2 mm necks are not trunks.**
      The In2 flags are inner-layer (IPC-2221 k=0.024, ~2-3x conservative with the
      GND planes) and several are paralleled/short. Left to check: the 0.2 mm F.Cu
      necks - VSYS @142.9,115.5 (short 1.4 mm seg by C143.2) and VBUS/+PACK outer
      necks. If any is a trunk (not a pad neck), widen it; otherwise leave.
      **+BATT already meets its 1.5 mm class** - no action.
- [ ] Topology correct, no action: `Net-(BT1--)` is the 2S cell midpoint (not GND),
      isolated from system GND by R41.

### RF (the board's whole purpose)

- [ ] **NEW: 20 `RF_50OHM` clearance violations, mostly the GNSS antenna path.**
      The rework's D10 (NEO-F10N antenna ESD) trace runs inside the RF_50OHM
      0.20 mm clearance to J11 and R69 @104.5,37-41 (D10/A2 x15+; plus D18/LoRa x2
      @146-148,44, JP14 x1 @148.5,45.4). Re-route to 0.20 mm **after** the GNSS
      parts are placed (P0). The whole GNSS RF chain (widths, match, pad, filter)
      needs a fresh review once placed - **nothing below about GNSS RF is
      trustworthy while the front end is in flux.**
- [ ] **RF widths - LoRa half only until GNSS settles.** `Net-(JP14-A)` and
      `Net-(JP14-B)` are 0.36 mm (fixed). `Net-(D18-A2)` (LoRa, 7.3 mm segment near
      C113.1) is still 0.30 mm - widen to 0.36 mm; confirm it is the live LoRa
      path, not the C113/C114 DNP-bypass, first. `Net-(D10-A2)` (GNSS) is deferred
      to the post-rework GNSS RF review above. `netclass_patterns` still holds only
      GPS_ANT + LORA_ANT, so nothing enforces RF width - the JP14 fix was manual;
      add the live RF nets to a class so width is enforced, not hand-set.
- [ ] **E22P / L5 - distance still 7.2 mm; confirm which fix was applied.**
      `kpcb RFNOISE` still reports U12 7.2 mm from L5 (want >= 8) - so U12 was NOT
      moved. If you added the GND via fence instead (the settled fix), good, but
      the check is distance-only and cannot see a fence. Confirm the fence is
      continuous across the U12/L5 overlap (x134-144), then suppress this RFNOISE
      in `kpcb.json` so it stops re-flagging. If no fence was added, this is still
      open. (Do NOT retune the buck - settled.)
- [ ] **`/Root/EN` track is routed inside the ESP32-S3 antenna keepout**
      @125.9,158.9 (`ITEMS_NOT_ALLOWED`). Detunes U1's 2.4 GHz radio. Reroute the
      track laterally clear of the module antenna (keepout is all-layer). This is
      the one real U1 finding - distinct from the expected U1 EDGECLR overhang.

### Zones / copper

- [ ] **Duplicate same-net zones intersect** (`ZONES_INTERSECT`): +PACK/+PACK and
      -BATT/-BATT on F.Cu. Same net, so no short - clean-signoff blocker (fill is
      non-deterministic), not a physical fab blocker. Merge each pair or give them
      distinct priorities, then refill.
- [ ] **-BATT isolated copper island** @109.8,127.6 (`ISOLATED_COPPER`, WARN) -
      a fill fragment from the -BATT zone overlap above. Fixing that overlap
      should clear it; re-run `kpcb zones` to confirm, else stitch or trim.
- [ ] **`Net-(C84-Pad1)` - In2/B.Cu pours now filled; F.Cu orphan zone remains.**
      (re-verified 2026-09-13) The BQ25798 BAT node now pours on In2.Cu and B.Cu,
      but the F.Cu zone is still declared-but-not-filled. Delete that F.Cu zone (or
      fill it) so it stops reporting. Also de-dupe the co-located 0.0 mm drill pair
      on this net @111.8,112.0 if not already done.

### Other real

- [ ] **4 real trace-to-trace clearance hits** (of 22 `CLEARANCE`; the other 18
      are the documented fine-pitch SMD-pad false positive): CC1/SBU2 @138.1,147.7
      (0.118) ; WC/FAULT @132.6,148.4 (0.129) ; +5V/5V_RAW @150.6,92.1 (0.100) and
      @150.6,94.3 (0.100). Below the declared rule - tidy them.
- [ ] **D7/SMF24A solar TVS clamps 38.9 V > BQ25798 VAC2 30 V abs max, and the
      solar path is POPULATED (not DNP).** Open call (solar is a declared
      afterthought): either (a) swap D7 for a TVS that stands off ~14 V and clamps
      < 30 V, or (b) mark the whole solar path DNP for rev-A (J10, D7, D9, Q10,
      C90, C91). Layout can't fix a rating mismatch. Do NOT touch the eFuse OVLO or
      any 5 V-rail TVS (settled).
- [ ] **ESD gap on the expansion headers.** The 4 primary ports are covered (D10
      GNSS, D18 LoRa, U4/TPD8S300 USB-C, D7 solar). The GPIO expander J14 goes
      straight into U17 (TCAL9539) with **no series R and no TVS** on any pin; the
      JST headers (J1/J2/J3/J5/J6/J15) are likewise bare. For the IP54/rugged spec,
      any header whose cable leaves the sealed enclosure needs an IEC 61000-4-2
      path. **Ryan:** mark which headers are internal-only vs external, then add
      TVS/series-R to the external ones.
- [ ] **Reconcile copper weight, then the 50-ohm width.** The stackup on disk says
      1 oz outer / 0.5 oz inner, but MEMORY.md and the board's own `.dru` comment
      say **2 oz outer intended**. That choice sets both the correct 50-ohm width
      (RF_50OHM is 0.36 mm now; 1 oz wants 0.38-0.40 mm) and whether the outer
      min-trace rule is 0.127 mm (1 oz) or ~0.15 mm (2 oz, which the P1 signal
      widths assume). Set the stackup to what's actually being ordered, recompute
      the 50-ohm width from the fab impedance calculator, set RF_50OHM to it.
      Settle before ordering.

### Mechanical / placement

- [ ] **10 through-hole test points land under the BT1/BT2 holders** (was 6; now
      also hard DRC `PTH_INSIDE_COURTYARD`, see P1): TP1, TP2, TP4, TP5 (under BT2) ;
      TP6, TP8, TP9, TP12, TP13, TP16 (under BT1).
      All 0.8 mm-drill TH on F.Cu - unprobeable once cells populate, and the barrel
      + back ring under the holder's flat foot risks the cell not seating. Convert
      to SMD pads (TP11 is already done - copy it) or relocate clear of the
      courtyards. Your bench is scope-only, so decide bring-up nodes now.
- [ ] **J15 (LED 5 V, populated) overlaps the BT2 holder** same-side (both B.Cu),
      6.19 mm2 / ~0.86 mm strip. Assembly/seating interference (not fab-blocking).
      Shift J15 clear of the holder, or confirm the 0.86 mm is cell-envelope
      overhang, not holder body, against the mechanical drawing. (This is the live
      analog of the settled J9/BT1 case - J9 is DNP, J15 is not.)
- [ ] **Corner SMAs J11/J13 sit inside the H1/H2 screw keepouts** (2.18 mm
      flange-to-hole-center). An M2 washer OD (~5 mm) or pan head (~3.8-4 mm) would
      foul the connector. Confirm the standoff/screw hardware clears the SMA flange
      or spread them apart. Lower-risk keepout encroachers: J14, SW2 (BOOT - leave
      finger access), D7 (moves anyway per the solar item).
- [ ] **EDGECLR: internal connectors hugging the routed edge** - live set
      J1, J2, J3 (0.09), J5, J6, J7 (0.02), J8 (0.38), J9, J10, J14, J15, SW1
      (0.36). Confirm each connector **body** (not courtyard) stays >= 0.5 mm
      inside the routed edge for V-score/mouse-bite margin; J7/J3/SW1/J8 borderline.
      (Old set C11/C133/SW2 is gone. U1 0.05 mm is the documented false positive.)
- [ ] **Long SPI bus** (`NETSPAN`): SCK ~100 mm / MOSI ~97 mm multi-drop with the
      J5 e-ink stub. Keep it source-terminated near the driver and the e-ink branch
      a short stub; series R's (R1/R2 33R, R10/R11 22R) exist - confirm which end
      drives. The DC control GPIOs (EN/RESET/DIO/CS/UART) are electrically short at
      their edge rates - no action.

---

## P3 - Cleanup (do after the P1 re-route churns these counts)

- [ ] **Run KiCad Edit -> Cleanup Tracks & Vias, then refill.** Clears the bulk of
      `TRACK_NOT_CENTERED_ON_VIA` (86, down from 253) and most `CONNECTION_WIDTH`
      (19). `HOLE_TO_HOLE` 26, `HOLES_CO_LOCATED` 1 (BT1 @113.2,40.1).
- [ ] **`STARVED_THERMAL` 40** - weak pad-to-pour spokes on GND/rail. Worth a look
      before assembly (thermal relief that can't carry current) but not blocking.
- [ ] **`3V3_GNSS` F.Cu zone declared but not filled** (NEO-M9N VCC rail via eFuse
      U11, 15 nodes, low current). Delete the empty zone (dead clutter) or fill it
      as a local pour under the GNSS front end - the trace is sufficient either way.
- [ ] **GPS_ANT has a 1.06 mm segment at 0.40 mm** @104.82,54.66 - reset to the
      0.36 mm class in the RF netclass pass above.
- [ ] **In1.Cu GND is 62% / 25 islands** - the RF reference plane. Automated data
      says the dominant island reaches all edges and stitching is dense (108 GND
      vias in the GNSS box, 71 in LoRa), so it's likely continuous under U9/U12.
      **Render In1.Cu and eyeball for a split under the RF trace columns** (~x104.5
      y37-57 GNSS, ~x147 y37-50 LoRa) to close it - no tool localizes a void under
      a footprint.
- [ ] **Silkscreen/DFM** (warnings): `SILK_EDGE_CLEARANCE` 19, plus text
      height/thickness. Before fab, confirm legible refdes, pin-1 and polarity
      marks, nothing over a pad or off the edge.

---

## Verified false positives - do NOT chase

- **SYNCNET (149)** - all pure `/Root/` prefix skew, board vs merged netlist,
  zero connectivity change (spot-checked `/Rails/5V_RAW` = 13 nodes, identical).
  Not a net error. Run KiCad "Update PCB from Schematic" once before fab so it
  stops masking future real SYNCNET; non-destructive.
- **18 of 22 CLEARANCE** = fine-pitch SMD pad-to-pad (U16 X2SON 0.35 mm, Q1-Q4
  FET lands). Inherent footprint geometry; suppress in `kdrc.json`.
- **U1 EDGECLR** (antenna overhang) and **same-side U1 OVERLAP = 0** (none exist -
  any future one would be real).
- **TH1-TH5 courtyard overlap** with the cells - intended (heat-sensitive parts).
- **0% localized F.Cu power pours** (VSYS/+PACK/PPHV/U8-SW etc.) - these are
  filled small pours (1 island, top1=100%); the 0% is a board-area rounding
  artifact. Only 3V3_GNSS and Net-(C84-Pad1) are actually unfilled.
- ERC `lib_symbol_mismatch` on the 16 edited symbols; per-root ERC
  `power_pin_not_driven` / `pin_to_pin` on global-label nets. Optional: PWR_FLAGs
  on eFuse/buck outputs to quiet ERC.

---

## Needs Ryan / not settleable by the tools

- [ ] **BT1/BT2 @0.00 mm annular PTH posts** - the 21700-holder mechanical posts
      read 0 mm annular (also `PADSTACK` warn). May be intentional (mechanical
      anchor, no plating). Confirm against the holder footprint before fab.
- [ ] **Z-height** - no tool measures part height. The 21700 cell dominates Z on
      the back; nothing known stacks above it, but the optional 2.9" E-Ink on M.2
      standoffs would. Settle with the STEP/3D model max-Z if a hard number is
      needed. No Z headroom (~24-25 mm ceiling).
- [ ] **kcap DC-bias sweep on charger/BMS bulk caps** - values verified (C90/C57/
      C92/C82 10uF, C31 4.7uF, charger/PD path, all >= 25 V nominal); the effective
      derated C at the real operating voltage was not swept. Run
      `kcap.py compare ... --vop <real V>` on the charger/BMS bulk caps to close.
- [ ] Enclosure boundary for the ESD item above - which headers exit the case.

---

## Skill/tool gaps noted (logged to SKILL-BACKLOG.md, not fixed here)

- No per-layer void probe under a footprint (In1 GND continuity needs a render).
- No part-height / Z check.
- `kdrc.json` silently suppresses 13 zone-clearance items (3 at 0.0 mm) that raw
  kicad-cli reports - the 0.0 mm ones deserve a glance before fab.
