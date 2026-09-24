# kcap.py - MLCC effective-density and voltage-stress-life comparator

Answers "higher voltage rating or higher nominal capacitance?" objectively -
minimising capacitor volume while keeping *effective* uF (after DC-bias derating,
density-driven dielectric thinning, temperature and aging) and wear-out life in spec.
Fully offline by default (fitted formulas); imports this skill's own `part_core`/`part_value` client
automatically when present for LCSC C-number resolution and a live density-ceiling
refinement, but never requires it.

```bash
python3 $skill/scripts/kcap.py compare '10u/25V/X5R/0805' '10u/25V/X7R/1206' --vop 8.4 --temp 45
python3 $skill/scripts/kcap.py compare C19666 C1791 --vop 5.0        # LCSC C-numbers work too
python3 $skill/scripts/kcap.py solve MLCC --need 8uF --vop 8.4 --temp 45   # smallest/cheapest that clears it
```

A spec string is slash-separated and order-independent: `CAP/VOLT/DIEL/PKG`, e.g.
`10u/25V/X5R/0805` or `22nF/50V/C0G/0603`.

**`--vop` is required** - the actual operating voltage the part will see. Never assume
Vrated. Full flag reference: `python3 $skill/scripts/kcap.py --help`.

`solve` pools like `pick` (JLC MLCC category per `--pkg`, nominal >= `--need`,
Vr >= 1.15 x Vop, `--diel`, stock >= 100, cheapest 600 per case), keeps the cheapest
part per (case, nominal, Vr, dielectric) that clears the target, and sorts by volume
then price. "nothing found" is then a real answer: 0805 X7R cannot give 8 uF at 8.4 V.

## Model and its limits

`eff_C = C_nom * bias * densitySeverity * tempPenalty * agingPenalty`, reported as
retained %, effective uF, uF/mm^3, uF/mm^2, % of density ceiling, and a relative
voltage-stress life verdict.

Case L/W is confirmed against manufacturer dimension tables; H is the middle of
KEMET's documented per-case thickness-code range (it varies with capacitance and layer
count within one case code - override with `--dim-a`/`--dim-b` `L,W,H` for a specific
real SKU when it matters).

Voltage-stress life covers TDDB/insulation-resistance wear-out only, **not** flex
cracking, thermal cycling or moisture, which dominate real field failures.
`L_ref`/`T_ref`/`Ea` are an assumed illustrative reference point, not a
datasheet-certified figure; override with `--lref-hours`/`--tref`/`--ea` if a
manufacturer publishes real endurance numbers.

MLCC aging is referenced to 1000 h, not t=1 h.

## Verified on parsnip

- X7R beat X5R on effective uF at every bulk position checked.
- Density traps: 10uF/25V in 0603, and 10uF/50V in 0805, both lose 35-45% to bias at a
  typical ~50% Vrated operating point - use 0805/1210 instead.
- A smaller case usually still wins on uF/mm^3 despite steeper derating, unless run
  near rated voltage.
- At 2.5x or more Vrated/Vop, voltage-stress life is never the binding constraint
  (2.5x margin gives ~15.6x relative life, matching `n=3`).

For a capacitor question that comes up mid netlist review, use this instead of
hand-computing derating from a datasheet curve.
