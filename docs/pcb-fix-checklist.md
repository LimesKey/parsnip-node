# parsnip-node PCB fix checklist

Live open work only. Verified 2026-09-17 against the board and `parsnip-merged.net`
(kpcb sync/review/zones, kdrc DRC+ERC on all 3 roots, knet). Board is fully placed
(437/437). **Live DRC+ERC: 418 error / 424 warn** (204 documented false positives
suppressed via `kdrc.json`).

Global verify after each fix:
`python3 code/claude/kicad-review/scripts/kdrc.py parsnip.kicad_pcb`

---

## P0 - Blockers (do not ship, do not trust downstream analysis)

- [ ] **2 copper shorts (`SHORTING_ITEMS`).**
      - **`/Root/P16` <-> GND** @143.3,114.3 - the P16 test-point net touches GND
        copper. Pull it clear (or, if P16 is meant to be GND, name it GND).
      - **GND <-> `/Root/Peripherals/LED_BDO`** @153.3,93.5 - a GND via (F.Cu-B.Cu)
        crosses the LED_BDO track. Move the via or the track.
      Re-run `kdrc --only DRC:SHORTING_ITEMS` after each - target 0.
- [ ] **`production/` bundle predates a clean board.** The board still carries the
      2 shorts + 43 undersized vias below, so `parsnip.zip` is not fab-clean.
      Regenerate the whole bundle (Gerber + drill + P&P + BOM) only **after** the
      P0 shorts and the P1 via/track items land, then diff `production/bom.csv`
      against the live 437-part netlist. (`positions.csv` has fewer rows than the
      part count because P&P excludes through-hole parts - not a missing-parts bug.)

---

## P1 - Fab-blocking geometry (blocks a clean DRC release + bundle regen)

- [ ] **Undersized via fence in the GNSS antenna box - 43 `VIA_DIAMETER`.** The GND
      stitching around J11/D10/FL1 (x103-104, y36-48) uses vias below the board's
      own 0.5 mm min diameter and 0.15 mm min annular - below JLC/PCBWay standard.
      Re-stitch to the standard 0.5 mm pad / 0.2 mm drill via. Biggest fab blocker.
      `kdrc --only DRC:VIA_DIAMETER`.
- [ ] **75 `ANNULAR_WIDTH`** (board min 0.15 mm) = the ~43 GNSS-fence vias above
      + rail/GND stitching vias at 0.10 mm elsewhere + 2 @0.00 mm BT1/BT2 holder
      PTH posts (mechanical, see "Needs Ryan"). Bump every signal/GND via pad to
      0.5 mm on 0.2 mm drill; leave the holder posts for the mechanical review.
- [ ] **199 `TRACK_WIDTH`.**
      - **192 `pwr_min_width`** (rule min 1.0 mm on every PWR_HIGH net) - the tool
        crying wolf on pad necks and short segments (-BATT, VSYS, PPHV, +PACK).
        Relax the rule to the real trunk widths so it stops masking real ones; see
        the ampacity note in P2. Not 192 real thin trunks.
      - **7 `rf_50ohm_width`** - real RF traces below the 0.36 mm 50-ohm class. Fix
        with the RF-width pass in P2 (tied to the copper-weight reconcile).
- [ ] **18 `PTH_INSIDE_COURTYARD` + 1 `NPTH_INSIDE_COURTYARD` - drilled holes under
      part bodies.** Test points **TP1, TP2, TP4, TP6, TP8, TP9, TP12, TP16** land
      inside the **BT1/BT2 holder** courtyards; **J7 and J1** have PTH+NPTH inside
      each other's courtyard @149,144. The cells will not seat over the barrels.
      Convert the TPs to SMD pads or relocate; check J7/J1 against the mechanical
      drawing.
- [ ] **17 `COURTYARDS_OVERLAP` (hard DRC).** The real ones are the TPs-under-
      holders and BT/TH/J9/J15 same-side overlaps below. Fixing the P2 mechanical
      items and the TP relocation clears most; re-run after.
- [ ] **4 `HOLE_CLEARANCE`: J7 pads (A1/A12/B1/B12) too close to a hole** @~149,144
      - same J7/J1 cluster as the NPTH item. Resolve together.

---

## P2 - Real, not fab-blocking

### RF (the board's whole purpose)

- [ ] **4 `RF_50OHM` clearance violations** (netclass 0.20 mm): D10/A2 near J11
      @104.5,37.9 and @104.0,39.8 (GNSS antenna ESD trace to the SMA); FL1
      @104.8,51.3 (GNSS filter); JP14 @147.2,47.3 (LoRa side). Tighten to 0.20 mm.
- [ ] **7 `rf_50ohm_width` - RF traces below the 0.36 mm 50-ohm class.** Widen to
      the class. **Settle the copper-weight / 50-ohm width question first** (below):
      the `.dru` itself flags 0.36 vs 0.40 mm as unresolved.
- [ ] **E22P / L5 distance 7.2 mm** (`kpcb RFNOISE`, want >= 8), so U12 was not
      moved. If the settled fix (GND via fence across the U12/L5 overlap, x134-144)
      was added, confirm it is continuous, then suppress this RFNOISE in
      `kpcb.json`. If no fence, still open. (Do NOT retune the buck - settled.)
- [ ] **`/Root/EN` track routed inside the ESP32-S3 antenna keepout** @125.9,158.9
      (`ITEMS_NOT_ALLOWED`). Detunes U1's 2.4 GHz radio. Reroute clear of the module
      antenna (keepout is all-layer). The one real U1 finding.
- [ ] **In1.Cu GND is 74% / 24 islands** (top island 78%). Likely continuous under
      U9/U12, but **render In1.Cu and eyeball for a split under the RF trace
      columns** (~x104 y37-57 GNSS, ~x147 y37-50 LoRa) - no tool localizes a void
      under a footprint.

### Zones / copper

- [ ] **Duplicate same-net zones intersect** (`ZONES_INTERSECT`): +PACK/PACK_1
      @106,123.6 and -BATT/-BATT_1 @109.8,127.6 on F.Cu. Same net, no short, but a
      clean-signoff blocker (fill non-deterministic). Merge each pair or give
      distinct priorities, then refill. (Clears the lone `ISOLATED_COPPER` -BATT
      fragment @109.8,127.6 too.)
- [ ] **`3V3_GNSS` F.Cu zone declared-but-not-filled** (NEO-F10N rail via eFuse
      U11, low current). Delete the empty zone or fill it as a local pour under the
      GNSS front end.

### Power / charge-path ampacity

- [ ] **VBUS / +PACK / VSYS - verify only the outer 0.2 mm necks are not trunks.**
      In2 flags are inner-layer (IPC k=0.024, conservative). Check the 0.2 mm F.Cu
      necks: VSYS @142.9,115.5 and the VBUS/+PACK outer necks - widen only a real
      trunk. +BATT already meets its 1.5 mm class. `Net-(BT1--)` is the 2S midpoint
      (not GND), isolated by R41 - topology correct, no action. (-BATT / PPHV flags
      are mixed-net / pad-neck artifacts, logged in SKILL-BACKLOG, not real.)

### Other real

- [ ] **Real clearance hits (of 36 `CLEARANCE`, 18 are the documented SMD-pad FP):**
      3 `Min Trace Spacing (outer)` @0.127 mm - +5V/5V_RAW @150.6,92.1 and
      @150.6,94.3, plus Q5 @102.5,124.3; 3 `Pad to Track`; ~8 `Default` 0.0 mm
      which are the TP-under-holder / BT courtyard overlaps (clear with P1). Tidy
      the 6 trace/pad ones.
- [ ] **D7/SMF24A solar TVS clamps 38.9 V > BQ25798 VAC2 30 V abs max, solar path
      is POPULATED.** Open call (solar is a declared afterthought): (a) swap D7 for
      a TVS standing off ~14 V and clamping < 30 V, or (b) mark the solar path DNP
      for rev-A (J10, D7, D9, Q10, C90, C91). Layout can't fix a rating mismatch.
      Do NOT touch the eFuse OVLO or any 5 V-rail TVS (settled).
- [ ] **ESD gap on the expansion headers.** The 4 primary ports are covered (D10
      GNSS, D18 LoRa, U4 USB-C, D7 solar). J14 (GPIO expander -> U17) and the JST
      headers (J1/J2/J3/J5/J6/J15) have **no series R and no TVS**. For the
      IP54/rugged spec, any header whose cable exits the sealed enclosure needs an
      IEC 61000-4-2 path. **Ryan:** mark internal-only vs external, add TVS/series-R
      to the external ones.
- [ ] **Reconcile copper weight, then the 50-ohm width - gating the RF pass.**
      Stackup on disk says 1 oz outer / 0.5 oz inner; MEMORY.md and the `.dru`
      comment say **2 oz outer intended**. That sets the correct 50-ohm width
      (RF_50OHM class is 0.36 mm; the `.dru` flags 0.36 vs 0.40 mm unresolved) and
      whether the outer min-trace floor is 0.127 mm (1 oz) or ~0.15 mm (2 oz). Set
      the stackup to what's actually ordered, recompute the 50-ohm width from the
      fab impedance calculator, set the RF_50OHM class + `rf_50ohm_width` rule to
      it, then fix the 7 RF-width violations. Settle before ordering.

### Mechanical / placement

- [ ] **Test points under the BT1/BT2 holders** (hard DRC, see P1): TP1, TP2, TP4,
      TP6, TP8, TP9, TP12, TP16 - 0.8 mm-drill TH on F.Cu, unprobeable once cells
      populate and the barrel/ring under the holder foot risks the cell not seating.
      Convert to SMD pads (TP11 is already done - copy it) or relocate. Bench is
      scope-only, so decide bring-up nodes now.
- [ ] **J15 (LED 5 V, populated) overlaps the BT2 holder** same-side (B.Cu),
      ~6 mm2. Assembly/seating interference (not fab-blocking). Shift J15 clear or
      confirm the overhang is cell-envelope, not holder body, against the drawing.
- [ ] **Corner SMAs J11/J13 and D7/J14/SW2 sit inside the H1-H4 3.4 mm screw
      keepouts** (`kpcb HOLECLR`). An M2 washer/pan-head could foul the connector
      flange. Confirm the standoff hardware clears the SMA flanges, or spread them
      apart. SW2 is the BOOT button - leave finger access; D7 moves anyway per the
      solar item.
- [ ] **EDGECLR: internal connectors hugging the routed edge** - J1/J5/J10 (0.01),
      J2 (0.03), J6 (0.07), J3 (0.09), J9 (0.10), J14 (0.14), SW1 (0.21), SW2
      (0.31), J7 (0.02), J8 (0.38). Confirm each connector **body** (not courtyard)
      stays >= 0.5 mm inside the routed edge for V-score/mouse-bite margin;
      J1/J5/J7/J10 worst. (U1 0.05 mm is the documented antenna-overhang FP.)
- [ ] **Long SPI bus** (`NETSPAN`): SCK/MOSI multi-drop with the J5 e-ink stub.
      Keep source-terminated near the driver, e-ink branch a short stub; series R's
      (R1/R2 33R, R10/R11 22R) exist - confirm which end drives.

---

## P3 - Cleanup (do after the P1 re-route churns these counts)

- [ ] **Run KiCad Edit -> Cleanup Tracks & Vias, then refill.** Clears most of
      `TRACK_NOT_CENTERED_ON_VIA` (107 - the re-route added off-center joins) and
      `CONNECTION_WIDTH` (15). `HOLE_TO_HOLE` 7.
- [ ] **`STARVED_THERMAL` 14** - weak pad-to-pour spokes on GND/rail. Worth a look
      before assembly, not blocking.
- [ ] **GPS_ANT / RF width segments** - fold into the RF-width pass in P2 once the
      50-ohm width is settled; nothing enforces it until the copper-weight decision.
- [ ] **Silkscreen/DFM** (warnings): `SILK_EDGE_CLEARANCE` 5, `TEXT_HEIGHT` 23,
      `TEXT_THICKNESS` 17. Before fab, confirm legible refdes, pin-1 and polarity
      marks, nothing over a pad or off the edge.

---

## Verified false positives - do NOT chase

- **SYNCNET (149)** - all pure `/Root/` prefix skew, board vs merged netlist, zero
  connectivity change. Not a net error. Run KiCad "Update PCB from Schematic" once
  before fab so it stops masking future real SYNCNET; non-destructive.
- **18 of 36 CLEARANCE** = fine-pitch SMD pad-to-pad (`SMD Pad to Pad (diff net)`
  rule, U16 X2SON 0.35 mm, Q1-Q4 FET lands). Inherent footprint geometry.
- **U1 EDGECLR** (antenna overhang) and **same-side U1 OVERLAP = 0** (none exist -
  any future one would be real).
- **TH1-TH5 courtyard overlap** with the cells - intended (heat-sensitive parts).
- **0% localized F.Cu power pours** (VSYS/+PACK/PPHV/U8-SW/C84 etc.) - filled small
  pours (1 island, top1=100%); the 0% is a board-area rounding artifact. Only
  `3V3_GNSS` on F.Cu is actually unfilled.
- **U10 `Net-(U10-EN)`** - IN and EN are intentionally tied (always-on LDO), fed
  from +5V via R134 0R. The auto-name is cosmetic, not a short.
- ERC `lib_symbol_mismatch` (suppressed), per-root `power_pin_not_driven` (1 finding
  folding 15 PWR flags) and `pin_to_pin` on global-label nets - three-root
  artifacts. Optional: add PWR_FLAGs on eFuse/buck outputs to quiet ERC.
- **ERC `PIN_NOT_DRIVEN` on Q1 and U8** - almost certainly cross-root false
  positives (U8 = BQ25798 charger). Confirm once with `knet.py parsnip-merged.net
  around U8` / `around Q1` before dismissing; do not act blind.

---

## Needs Ryan / not settleable by the tools

- [ ] **BT1/BT2 @0.00 mm annular PTH posts** (2 of the ANNULAR_WIDTH count, also
      `PADSTACK` warn) - the 21700-holder mechanical posts. May be intentional
      (mechanical anchor, no plating). Confirm against the holder footprint.
- [ ] **Z-height** - no tool measures part height. The 21700 cell dominates Z on
      the back; the optional 2.9" E-Ink on M.2 standoffs adds more. No Z headroom
      (~24-25 mm ceiling). Settle with the STEP/3D max-Z if a hard number is needed.
- [ ] **kcap DC-bias sweep on charger/BMS bulk caps** - values verified (all
      >= 25 V nominal); the effective derated C at the real operating voltage was
      not swept. Run `kcap.py compare ... --vop <real V>` to close.
- [ ] **Enclosure boundary** for the ESD item - which headers exit the case.
- [ ] **GNSS sheet NC hygiene** - knet reports only 10/12 no_connect markers land
      on a pin (2 stray NC flags on `/GNSS/`). Minor schematic cleanup, not a board
      issue; clean it so NC validation re-enables.

---

## Skill/tool gaps noted (logged to SKILL-BACKLOG.md, not fixed here)

- No per-layer void probe under a footprint (In1 GND continuity needs a render).
- No part-height / Z check.
- `pwr_min_width` (1.0 mm) generates 192 of 199 TRACK_WIDTH errors by flagging pad
  necks - the rule needs per-net trunk widths, not a flat 1.0 mm, to stop crying
  wolf.
