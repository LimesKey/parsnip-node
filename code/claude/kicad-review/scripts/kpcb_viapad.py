"""kpcb.py `viapad`: vias dropped into SMD pads."""
import os, re, json
from collections import defaultdict
from kcommon import natkey, trunc, GND_RE, rail_voltage
from kpcb_copper import pad_dist

def drill_depth(pad, via):
    """how far (mm) the via's DRILL reaches into the pad's mask opening, > 0 when it does: the
    pad's real copper (roundrect/oval corners, custom primitives) grown by its mask margin. A
    fab's tenting/plugging cares only about the hole; an annular ring on the pad is copper on
    copper. `prot` in a .kicad_pcb is already the absolute pad angle (pad_dist undoes it)."""
    return via['drill'] / 2 - (pad_dist(pad, (via['x'], via['y']), rounded=True) - pad['mask'])


def protection(vp):
    """(IPC-4761 type, what a drill in a pad's mask opening means) from Board Setup > Via
    protection: tenting I, + covering II, plugging III, + tenting IV, filling V/VI, + capping VII;
    a = one side, b = both"""
    sides = [k for k in ('tenting', 'covering', 'plugging') if any(vp[k])]
    ab = 'b' if all(all(vp[k]) for k in sides) else 'a'
    if vp['filling']:
        return ('VII filled + capped', 'planar: solders flat, the count is a quote line') if vp['capping'] else \
            (f"{'VI' if sides else 'V'} filled", 'not capped: a dimple in the pad, a small pad may void')
    if 'plugging' in sides:
        return (f"{'IV' if 'tenting' in sides or 'covering' in sides else 'III'}-{ab} mask-plugged",
                "the plug sits in the joint (voids, outgassing, solder wicking past a short plug): "
                "move the via off small pads, or order those vias filled + capped (VII)")
    if sides:
        return (f"{'II' if 'covering' in sides else 'I'}-{ab} tented",
                'a tent cannot span inside a pad opening: the hole is open there and wicks solder')
    return 'none (open vias)', 'an open hole in a pad wicks solder out of the joint'


BIG = 1.0      # a pad this wide on its narrow side (EP, 1206, power pad): a plug there is low risk


def c_viapad(b, a):
    """Every SMD pad a via's drill reaches into (its mask opening, real shape), small pads
    first: there a plug or an open hole sits in a fine joint. Different-net = the via sits
    in a foreign pad -> possible short."""
    order = {n: i for i, n in enumerate(b.copper)}

    def spans(via, layer):                          # does the via reach the pad's layer?
        vi = [order[l] for l in via['layers'] if l in order]
        if not vi or layer not in order:
            return True                             # unknown -> assume yes (conservative)
        return min(vi) <= order[layer] <= max(vi)

    # bin vias into 1 mm cells so this isn't pads x vias
    grid = defaultdict(list)
    for vi, v in enumerate(b.vias):
        grid[(int(v['x']), int(v['y']))].append(vi)

    # ref -> pad_num -> {'pad': pad, 'vias': {via_index: mismatch}}. Keying vias by
    # index dedupes a via that a split sub-pad (same number) covers twice.
    hits = defaultdict(lambda: defaultdict(lambda: {'pad': None, 'vias': {}}))
    for f in b.fps.values():
        for pad in f.pads:
            if pad['kind'] != 'smd' or pad['sx'] <= 0 or pad['sy'] <= 0 or not pad['num']:
                continue  # numberless SMD slivers (paste/mechanical) aren't via-in-pad targets
            play = next((l for l in pad['layers'] if l.endswith('.Cu')), None)
            reach = int(pad['w'] / 2 + 1)
            seen = set()
            for cx in range(int(pad['x']) - reach, int(pad['x']) + reach + 1):
                for cy in range(int(pad['y']) - reach, int(pad['y']) + reach + 1):
                    for vi in grid.get((cx, cy), ()):
                        if vi in seen:
                            continue
                        seen.add(vi)
                        v = b.vias[vi]
                        dep = drill_depth(pad, v) if spans(v, play) else 0
                        if dep <= 0:
                            continue
                        mism = bool(pad['net'] and v['net'] and pad['net'] != v['net'])
                        slot = hits[f.ref][pad['num']]
                        slot['pad'] = pad
                        slot['vias'][vi] = mism
                        slot['depth'] = max(slot.get('depth', 0), min(dep, v['drill']))

    sw = b.sw_nets()
    def role(net):                          # the netclass catches Net-(BT1-+) etc.
        n = (net or '').split('/')[-1]
        if GND_RE.match(n):
            return 'GND'
        return 'PWR' if (rail_voltage(n) is not None or len(b.nets.get(net, [])) > a.fanout
                         or net in sw or re.search(r'PWR|POWER', b.netclass(net), re.I)) else 'SIG'
    roles = defaultdict(int)
    for pads in hits.values():
        for s_ in pads.values():
            s_['role'] = role(s_['pad']['net'])
            roles[s_['role']] += 1
            # a foreign-net via (possible short) is never filtered out
            s_['show'] = any(s_['vias'].values()) or (
                (not a.signal or s_['role'] == 'SIG') and len(s_['vias']) >= a.min_vias)
    n_pads = sum(len(pads) for pads in hits.values())
    n_via = sum(len(s['vias']) for pads in hits.values() for s in pads.values())
    n_mism = sum(1 for pads in hits.values() for s in pads.values()
                 for m in s['vias'].values() if m)
    if a.json:
        out = {r: [{'pad': num, 'pad_net': s['pad']['net'], 'role': s['role'],
                    'vias': len(s['vias']), 'depth': round(s['depth'], 3),
                    'size': [s['pad']['sx'], s['pad']['sy']],
                    'via_nets': sorted({b.vias[vi]['net'] for vi in s['vias']}),
                    'mismatch': any(s['vias'].values())}
                   for num, s in pads.items() if s['show']]
               for r, pads in hits.items()}
        print(json.dumps({'components': len(hits), 'pads': n_pads, 'vias': n_via,
                          'net_mismatches': n_mism, 'protection': protection(b.viaprot)[0],
                          'hits': out}, indent=2))
        return 2 if n_mism else 0
    if not hits:
        print("no via-in-pad: no via drill reaches into any SMD pad's mask opening."); return 0
    rows = sorted(((min(s['pad']['sx'], s['pad']['sy']), -s['depth'], natkey(r), r, num, s)
                   for r, pads in hits.items() for num, s in pads.items() if s['show']),
                  key=lambda x: x[:3])
    kind, what = protection(b.viaprot)
    print(f"via-in-pad: {n_via} via drill(s) reach into {n_pads} SMD pad mask opening(s) across "
          f"{len(hits)} component(s)  [pads: {roles['GND']} GND / {roles['PWR']} PWR / {roles['SIG']} SIG]"
          + (f"\nshowing {len(rows)} (--signal/--min filter; net mismatches always shown)"
             if len(rows) < n_pads else '')
          + f"\nvia protection (Board Setup): {kind} - {what}\n"
          + f"small pads first; under {BIG:g} mm on the narrow side is a fine joint (0402, fine-pitch lead, "
            "module pad)\n")
    big = False
    for side, _d, _k, ref, num, s in rows:
        if side >= BIG and not big:
            big = True
            print(f"  -- pads >= {BIG:g} mm (EP, 1206, power): a plug or dimple there is low risk")
        p, f = s['pad'], b.fps[ref]
        mnets = sorted({b.vias[vi]['net'] or '(none)' for vi, m in s['vias'].items() if m})
        note = (f"OK same net ({p['net']})" if not mnets else
                f"!! via net {'/'.join(mnets)} != pad net {p['net']} - possible short")
        dep = 'wholly in' if any(s['depth'] >= b.vias[vi]['drill'] - 1e-6 for vi in s['vias']) \
            else f"{s['depth']:.3f} in" if s['depth'] >= 5e-4 else '<0.001 in'
        print(f"  {ref + '.' + num:<9} {trunc(f.value, 16):<16} {p['sx']:.2f}x{p['sy']:.2f} {f.layer[0]}  "
              f"{s['role']:<3} {len(s['vias']):>2} via(s)  drill {dep:<10} {note}")
    if n_mism:
        print(f"\n{n_mism} via(s) sit in a pad of a DIFFERENT net - that is a short, "
              f"not via-in-pad. Confirm with `kdrc.py {os.path.basename(a.file)}`.")
    return 2 if n_mism else 0
