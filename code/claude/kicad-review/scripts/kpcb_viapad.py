"""kpcb.py `viapad`: vias dropped into SMD pads."""
import os, re, json, math
from collections import defaultdict
from kcommon import natkey, trunc, GND_RE, rail_voltage
from kpcb_board import point_in_poly

def _via_in_pad(pad, vx, vy):
    """True if (vx,vy) lands inside the pad's copper rectangle. The pad's `at`
    angle in a .kicad_pcb is ABSOLUTE (the footprint rotation is already baked
    in - unlike a .kicad_mod, where it's relative), so undo just `prot`, not
    f.rot+prot, and test the via against the pad half-extents. A roundrect/oval/
    circle pad is treated as its bounding box - a hair generous at the corners,
    which is the safe direction for a manufacturing flag."""
    dx, dy = vx - pad['x'], vy - pad['y']
    th = math.radians(pad['prot'])
    c, s = math.cos(th), math.sin(th)
    u, v = c * dx - s * dy, s * dx + c * dy
    if abs(u) <= pad['sx'] / 2 and abs(v) <= pad['sy'] / 2:
        return True
    # a custom pad's copper is its anchor PLUS its primitives (pad-local frame)
    return any(point_in_poly((u, v), poly) for poly in pad.get('prims') or ())

def c_viapad(b, a):
    """Every component with a via centred inside one of its SMD pads (via-in-pad).
    Same-net = intentional via-in-pad (needs filled+capped/type-VII vias at the
    fab). Different-net = the via sits in a foreign pad -> possible short."""
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
                        if not spans(v, play) or not _via_in_pad(pad, v['x'], v['y']):
                            continue
                        mism = bool(pad['net'] and v['net'] and pad['net'] != v['net'])
                        slot = hits[f.ref][pad['num']]
                        slot['pad'] = pad
                        slot['vias'][vi] = mism

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
                    'vias': len(s['vias']),
                    'via_nets': sorted({b.vias[vi]['net'] for vi in s['vias']}),
                    'mismatch': any(s['vias'].values())}
                   for num, s in pads.items() if s['show']]
               for r, pads in hits.items()}
        print(json.dumps({'components': len(hits), 'pads': n_pads, 'vias': n_via,
                          'net_mismatches': n_mism, 'hits': out}, indent=2))
        return 2 if n_mism else 0
    if not hits:
        print("no via-in-pad: no via centre lands inside any SMD pad."); return 0
    shown = sum(1 for pads in hits.values() for s in pads.values() if s['show'])
    print(f"via-in-pad: {n_via} via(s) in {n_pads} SMD pad(s) across {len(hits)} "
          f"component(s)  [pads: {roles['GND']} GND / {roles['PWR']} PWR / {roles['SIG']} SIG]"
          + (f"\nshowing {shown} (--signal/--min filter; net mismatches always shown)"
             if shown < n_pads else '')
          + "\nSame-net via-in-pad needs filled+capped (or type-VII) vias - flag it in the "
            "fab quote.\n")
    for ref in sorted(hits, key=natkey):
        if not any(s['show'] for s in hits[ref].values()):
            continue
        f = b.fps[ref]
        print(f"  {ref:<6} {trunc(f.value, 22):<22} {'B.Cu' if f.back else 'F.Cu'}")
        for num in sorted(hits[ref], key=natkey):
            s = hits[ref][num]
            if not s['show']:
                continue
            p = s['pad']
            mnets = sorted({b.vias[vi]['net'] or '(none)' for vi, m in s['vias'].items() if m})
            note = (f"OK same net ({p['net']})" if not mnets else
                    f"!! via net {'/'.join(mnets)} != pad net {p['net']} - possible short")
            print(f"       pad {num:<4} {s['role']:<3} {len(s['vias']):>2} via(s)   {note}")
    if n_mism:
        print(f"\n{n_mism} via(s) sit in a pad of a DIFFERENT net - that is a short, "
              f"not via-in-pad. Confirm with `kdrc.py {os.path.basename(a.file)}`.")
    return 2 if n_mism else 0
