#!/usr/bin/env python3
"""PROTOTYPE (2026-10-03, parsnip-specific, see SKILL-BACKLOG "front matter").
Import the front matter into a parsnip project dir: register Block Diagram and
Power Tree as top-level sheets 2-3, shift pages 2-10 to 4-12, set REV/DATE text
variables, then generate the three sheets with gen_front_matter.py. Run with KiCad closed."""
import json, os, subprocess, sys, uuid

D = sys.argv[1]
pro = os.path.join(D, 'parsnip.kicad_pro')
p = json.load(open(pro))
tl = p['schematic']['top_level_sheets']
assert [s['name'] for s in tl] == ['Overview', 'USB Interface', 'Charger', 'BMS', 'Root'], tl
assert p['sheets'][0][1] == 'Overview', p['sheets'][:2]
assert not p.get('text_variables'), p.get('text_variables')
assert not os.path.exists(os.path.join(D, 'block_diagram.kicad_sch'))
assert not os.path.exists(os.path.join(D, 'power_tree.kicad_sch'))

new = [{'filename': 'block_diagram.kicad_sch', 'name': 'Block Diagram', 'uuid': str(uuid.uuid4())},
       {'filename': 'power_tree.kicad_sch', 'name': 'Power Tree', 'uuid': str(uuid.uuid4())}]
p['schematic']['top_level_sheets'] = tl[:1] + new + tl[1:]
p['sheets'] = p['sheets'][:1] + [[n['uuid'], n['name']] for n in new] + p['sheets'][1:]
p['text_variables'] = {'REV': '1.1', 'DATE': '2026-10-03'}
open(pro, 'w').write(json.dumps(p, indent=2, ensure_ascii=False) + '\n')


def renum(f, pairs):
    f = os.path.join(D, f)
    t = open(f).read()
    for a, b in pairs:   # descending, so a new number never collides with an old one
        k = f'(page "{a}")'
        assert t.count(k) == 1, (f, k, t.count(k))
        t = t.replace(k, f'(page "{b}")')
    open(f, 'w').write(t)


renum('usb_interface.kicad_sch', [(2, 4)])
renum('charger.kicad_sch', [(3, 5)])
renum('bms.kicad_sch', [(4, 6)])
renum('parsnip.kicad_sch', [(10, 12), (9, 11), (8, 10), (7, 9), (6, 8), (5, 7)])
subprocess.run([sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), 'gen_front_matter.py'), D],
               check=True)
print('imported')
