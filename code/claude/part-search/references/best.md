# best - "find the best part for <application>"

`pick` answers "the cheapest part meeting these limits". "Best" is a judgement over
many specs at once, so the tool does **not** decide it. Its job is to gather the
shortlist's full specs, test conditions visible, in one table; Claude judges case
by case and says why.

## 1. Limits from the board, not from memory

The application sets the limits, so read them off the board first:

- `knet.py parsnip-merged.net around U10` - what the part connects to, the real rails.
- Real V and I at the part: the rail it sits on, the load it feeds, the worst case
  (VSYS is 7-8.4 V, not "12 V"; the E22P TX burst, not its average).
- MLCC: `kcap.py compare ... --vop <real V>` - effective uF at the DC bias.

## 2. The figure of merit for the part type

| part | governs the choice | `--sort` / `--w` name |
| --- | --- | --- |
| LDO | Iq, dropout at the real load, PSRR at the ripple frequency, noise | `iq`, `dropout`, `psrr`, `noise` |
| MOSFET | RDS(on) **at the gate drive you have**, Qg, Vgs(th) | `rdson`, `vgsth`, `--w 'Gate Charge(Qg)=..'` |
| TVS/ESD | Vc at Ipp, capacitance on fast lines, Vrwm over the rail max | `vc`, `vrwm`, `--w 'Junction Capacitance=..'` |
| buck/boost | Iq (sleep), switching frequency, EMI features (spread spectrum, sync) | `iq`, `freq` |
| MLCC | effective C at the operating voltage (`kcap`), not nominal | `kcap.py` |
| inductor | Isat, DCR, size | `isat`, `dcr` |
| GNSS/RF filter, LNA | NF, insertion loss in band, rejection at 915 MHz | datasheet only |

LCSC/JLC list some of these (Iq, RDS(on) at one Vgs, Vc), DigiKey others (Qg, PSRR
by frequency). NF, IL at band, PSRR at your frequency and RDS(on) at a 4.5 V drive
are **datasheet only**: say so, and read them there.

## 3. Shortlist, both catalogs, one table

```bash
part.py pick LDO --pkg SOT-23-5 --vout 3.3 --pareto iq --xcheck
part.py compare C2887324 C5142805 TPS7A0233PDBVR --provider digikey --attrs
```

- `--sort ATTR` ranks on the figure of merit and pools that attribute's **best
  values first**, so the shortlist is the true top-N, not the cheapest 600 re-sorted.
- `--pareto ATTR` keeps only rows no other row beats on both price and ATTR: the
  "best value" set.
- `--xcheck` runs the same limits on DigiKey and prints both shortlists' top 3 in one
  spec table (rows merged across catalog spellings, values in SI with their test
  condition). `compare --attrs` does the same for any parts you name.
- Read the `coverage:` line: an attribute listed on half the category means half the
  category was never ranked.
- A `clone? of <mfr> <MPN>` row (another maker's MPN inside its own, listed later) is
  the least trustworthy figure on the page: TECH PUBLIC's TLV74333PDBVR-TP lists Iq
  800 nA, TI's own part is 34 uA typ. A clone's figure counts only after its own
  datasheet confirms it.

## 4. Verify the top 3 in the datasheet

`ds C.. --save`, then `kdoc.py grep '<figure>' -d <MPN>` for each. Distributor
figures mix typ, max and test conditions without saying which (the same TPS7A0233
reads 25 nA Iq on JLC and 60 nA on DigiKey); the datasheet says which. Only then
call one best, and give the reason in one line.

## 5. LCSC first, off-LCSC when clearly better

LCSC is the primary source (CLAUDE.md "Part selection"). An off-LCSC part costs a JLC
Global Sourcing or consignment line, or a hand-solder, so it has to be clearly
better for this application. A rough guide, not a gate:

- >= 2x on Iq, RDS(on) or leakage,
- >= 3 dB on NF or PSRR,
- or it meets a limit nothing on LCSC meets (`pick` empty, `--xcheck` not).

Never rank on JLC Basic/Extended or the $3 line fee: the fab house is not settled.
