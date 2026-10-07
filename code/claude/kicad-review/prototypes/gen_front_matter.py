#!/usr/bin/env python3
"""PROTOTYPE (2026-10-03, parsnip-specific, see SKILL-BACKLOG "front matter").
Front matter for parsnip-node: cover (p1), block diagram (p2), power tree (p3).
Conventions follow the NXP i.MX8M EVK and BeaglePlay schematic sets: cover with
page list + revision history + notes, MCU-centred block diagram, colour-per-rail
power tree. Every refdes/value was checked with knet on 2026-10-03.
Usage: gen_front_matter.py PROJECT_DIR   (writes overview/block_diagram/power_tree.kicad_sch)"""
import os, sys, uuid

D = sys.argv[1]
GRAY = (225, 225, 225)
RAIL = {'vbus': (220, 110, 0), 'solar': (190, 150, 0), 'vsys': (200, 0, 0),
        'bat': (130, 70, 20), '5v': (180, 0, 180), '3v3': (0, 90, 220),
        'gnss': (0, 140, 60), 'clk': (110, 110, 110)}


class Sheet:
    def __init__(self):
        self.out = []
        self.oy = 0  # y offset applied to everything drawn after it is set

    def add(self, s):
        self.out.append(s)

    # -- primitives
    def text(self, x, y, s, size=1.27, bold=False, italic=False, justify=None, page=None, angle=0):
        f = f'(font (size {size} {size})' + (' (bold yes)' if bold else '') + (' (italic yes)' if italic else '') + ')'
        j = f' (justify {justify})' if justify else ''
        h = f' (href "#{page}")' if page else ''
        self.add(f'(text {q(s)} (exclude_from_sim no) (at {x} {round(y + self.oy, 3)} {angle}) (effects {f}{j}{h}) {u()})')

    def rect(self, x, y, w, h, width=0.25, dash=False, fill=None):
        f = f'(fill (type color) (color {fill[0]} {fill[1]} {fill[2]} 1))' if fill else '(fill (type none))'
        self.add(f'(rectangle (start {x} {round(y + self.oy, 3)}) (end {x + w} {round(y + h + self.oy, 3)}) {stroke(width, "dash" if dash else "solid")} {f} {u()})')

    def line(self, pts, col=None, w=0.25, dash=False, end=False, start=False):
        pts = [(x, round(y + self.oy, 3)) for x, y in pts]
        xy = ' '.join(f'(xy {x} {y})' for x, y in pts)
        self.add(f'(polyline (pts {xy}) {stroke(w, "dash" if dash else "solid", col)} (fill (type none)) {u()})')
        if end:
            self.head(pts[-2], pts[-1], col)
        if start:
            self.head(pts[1], pts[0], col)

    def head(self, a, b, col, L=1.6, W=0.6):
        (ax, ay), (bx, by) = a, b
        n = ((bx - ax) ** 2 + (by - ay) ** 2) ** 0.5
        dx, dy = (bx - ax) / n, (by - ay) / n
        p1 = (round(bx - L * dx - W * dy, 3), round(by - L * dy + W * dx, 3))
        p2 = (round(bx - L * dx + W * dy, 3), round(by - L * dy - W * dx, 3))
        fill = f'(fill (type color) (color {col[0]} {col[1]} {col[2]} 1))' if col else '(fill (type outline))'
        self.add(f'(polyline (pts (xy {bx} {by}) (xy {p1[0]} {p1[1]}) (xy {p2[0]} {p2[1]}) (xy {bx} {by})) '
                 f'{stroke(0.1, "solid", col)} {fill} {u()})')

    def dot(self, x, y, col=None, r=0.5):
        fill = f'(fill (type color) (color {col[0]} {col[1]} {col[2]} 1))' if col else '(fill (type outline))'
        self.add(f'(circle (center {x} {round(y + self.oy, 3)}) (radius {r}) {stroke(0.1, "solid", col)} {fill} {u()})')

    def block(self, x, y, w, h, lines, page=None, fill=None, width=0.25, dash=False, size=1.27):
        """Box with centred lines: first bold (function), the rest plain."""
        self.rect(x, y, w, h, width, dash, fill)
        pitch = size * 1.75
        y0 = y + h / 2 - pitch * (len(lines) - 1) / 2
        for i, s in enumerate(lines):
            self.text(round(x + w / 2, 3), round(y0 + i * pitch, 3), s, size if i else size + 0.1,
                      bold=(i == 0), page=page if i == 0 else None)

    def antenna(self, x, y, label):
        """Antenna glyph; (x, y) is the feed point."""
        self.line([(x, y), (x, y - 5)])
        self.line([(x, y - 5), (x - 2, y - 8.5), (x + 2, y - 8.5), (x, y - 5)])
        self.text(x, y + 2.2, label, 1.0)

    def table(self, x, y, widths, heights, rows, header_size=1.27, size=1.27, hrefs=None):
        cells = []
        cy = y + self.oy
        for r, (row, h) in enumerate(zip(rows, heights)):
            cx = x
            for c, (s, w) in enumerate(zip(row, widths)):
                f = f'(font (size {header_size if r == 0 else size} {header_size if r == 0 else size})' + \
                    (' (bold yes)' if r == 0 else '') + ')'
                href = f' (href "#{hrefs[r]}")' if hrefs and hrefs[r] else ''
                cells.append(f'(table_cell {q(s)} (exclude_from_sim no) (at {cx} {cy} 0) (size {w} {h}) '
                             f'(margins 0.95 0.95 0.95 0.95) (span 1 1) (fill (type none)) '
                             f'(effects {f} (justify left){href}) {u()})')
                cx += w
            cy += h
        self.add(f'(table (column_count {len(widths)}) '
                 f'(border (external yes) (header yes) {stroke(0.3)}) '
                 f'(separators (rows yes) (cols yes) {stroke(0.15)}) '
                 f'(column_widths {" ".join(map(str, widths))}) (row_heights {" ".join(map(str, heights))}) '
                 f'{u()} (cells {" ".join(cells)}))')

    def write(self, name, title, page):
        path = os.path.join(D, name)
        tail = ''
        if os.path.exists(path):
            src = open(path).read()
            head = src[:src.index('(title_block')]
            head = head.replace('(paper "A3")', '(paper "A4")')
            if '(embedded_fonts no)' in src:   # project-wide flag, kept by the first top-level sheet
                tail = '\t(embedded_fonts no)\n'
        else:
            head = (f'(kicad_sch\n\t(version 20260830)\n\t(generator "eeschema")\n\t(generator_version "10.99")\n'
                    f'\t(uuid "{uuid.uuid4()}")\n\t(paper "A4")\n\t')
        tb = (f'(title_block\n\t\t(title {q(title)})\n\t\t(date "${{DATE}}")\n\t\t(rev "${{REV}}")\n'
              f'\t\t(company "LimesKey")\n\t)\n\t(lib_symbols)\n\t')
        body = '\n\t'.join(self.out)
        open(path, 'w').write(head + tb + body + f'\n\t(sheet_instances\n\t\t(path "/"\n\t\t\t(page "{page}")\n\t\t)\n\t)\n{tail})\n')
        print(f'{len(self.out):4d} items -> {name}')


def q(s):
    return '"' + s.replace('\\', '\\\\').replace('"', '\\"').replace('\n', '\\n') + '"'


def u():
    return f'(uuid "{uuid.uuid4()}")'


def stroke(w, typ='solid', col=None):
    c = f' (color {col[0]} {col[1]} {col[2]} 1)' if col else ''
    return f'(stroke (width {w}) (type {typ}){c})'


PAGES = [('Overview', 'Cover: page list, revision history, notes'),
         ('Block Diagram', 'MCU, buses and peripherals'),
         ('Power Tree', 'Inputs, charger, rails, loads'),
         ('USB Interface', 'USB-C, TPS25751 PD controller, ESD'),
         ('Charger', 'BQ25798, input FETs, ship FET'),
         ('BMS', '2S 21700 cells, MAX17320, F5, Q16'),
         ('Root', 'ESP32-S3 MCU'),
         ('Rails', '5 V / 3.3 V bucks, eFuse, sync clock'),
         ('GNSS', 'NEO-F10N, LDO, eFuse, antenna bias'),
         ('LoRa', 'E22P-915M30S, antenna detect'),
         ('Peripherals', 'GPIO expander, sensors, LEDs'),
         ('Connectors', 'Expansion headers, e-Ink display')]

# ================================================================== page 1: cover
s = Sheet()
s.text(14, 27, 'PARSNIP-NODE', 5.0, bold=True, justify='left bottom')
s.text(14, 34, 'Meshtastic LoRa mesh node, 915 MHz', 2.0, justify='left bottom')
s.text(14, 39, 'Schematic', 2.0, italic=True, justify='left bottom')

s.text(14, 47, 'PAGE LIST', 1.8, bold=True, justify='left bottom')
rows = [('PAGE', 'SHEET', 'CONTENTS')] + [(str(i + 1), n, c) for i, (n, c) in enumerate(PAGES)]
s.table(14, 49, [12, 28, 62], [6] + [6] * len(PAGES), rows,
        hrefs=[None] + [i + 1 for i in range(len(PAGES))])

s.text(126, 47, 'REVISION HISTORY', 1.8, bold=True, justify='left bottom')
s.table(126, 49, [12, 22, 104, 20], [6, 8, 8, 8, 8, 8],
        [('REV', 'DATE', 'DESCRIPTION OF CHANGES', 'BY'),
         ('1.1', '2026-10-03', 'Prototype schematic; PCB layout in progress', 'LimesKey'),
         ('', '', '', ''), ('', '', '', ''), ('', '', '', ''), ('', '', '', '')])

s.text(126, 107, 'NOTES, UNLESS OTHERWISE SPECIFIED:', 1.8, bold=True, justify='left bottom')
notes = ['Resistance in ohms, capacitance in farads. All voltages are DC.',
         'Resistors and capacitors are 0402.',
         'DNP = do not populate. DNP symbols are drawn with a red X.',
         'An overbar on a pin or net name denotes an active-low signal.',
         'Labels with the same name are connected. Top-level sheets join through\n'
         'global labels and power symbols, subsheets of Root through sheet pins.',
         'PWR_FLAG symbols are for ERC only and are not parts.',
         'Some symbols are edited in the schematic. Do not run\n'
         'Tools > Update Symbols from Library.']
y = 112
for i, n in enumerate(notes):
    s.text(126, y, f'{i + 1}.', 1.27, justify='left top')
    s.text(132, y, n, 1.27, justify='left top')
    y += 5 + 2.6 * n.count('\n')
s.write('overview.kicad_sch', 'Cover', 1)

# ================================================================== page 2: block diagram
s = Sheet()
s.text(14, 22, 'BLOCK DIAGRAM', 3.0, bold=True, justify='left bottom')
# MCU
MX, MY, MW, MH = 118, 34, 52, 116
s.rect(MX, MY, MW, MH, 0.6)
s.text(MX + MW / 2, 86, 'ESP32-S3-WROOM-1', 2.0, bold=True, page=7)
s.text(MX + MW / 2, 90.5, 'U1', 1.5)
s.text(MX + MW / 2, 94.5, 'N16R8: 16 MB flash, 8 MB PSRAM', 1.1, italic=True)
L, R = MX + 1.5, MX + MW - 1.5


def left_if(y, name, label, dev_x, both=True, into_mcu=False):
    """MCU left-edge interface: name inside the box, line out to dev_x."""
    s.text(L, y, name, 1.1, justify='left')
    s.line([(MX, y), (dev_x, y)], end=both or not into_mcu, start=both or into_mcu)
    if label:
        s.text(dev_x + 2, y - 0.9, label, 1.0, italic=True, justify='left bottom')


# SPI devices, one link each (shared SCK/MOSI/MISO, own chip select + control)
s.block(40, 34, 56, 15, ['LORA TRANSCEIVER', 'E22P-915M30S (U12)', 'SX1262 + PA, 30 dBm'], page=10)
s.antenna(30, 41.5, 'J13')
s.line([(30, 41.5), (40, 41.5)])
s.text(35, 40.6, 'LPF', 0.9, justify='bottom')
s.block(40, 54, 56, 11, ['E-INK DISPLAY', 'J5'], page=12, fill=GRAY)
s.block(40, 70, 56, 10, ['SPI EXPANSION', 'J1'], page=12, fill=GRAY)
for y, name, sig in [(41.5, 'SPI, GPIO', 'NSS, BUSY, DIO1, NRST'),
                     (59.5, 'SPI, GPIO', 'CS, DC, RST, BUSY'),
                     (75, 'SPI', 'CS')]:
    s.text(L, y, name, 1.1, justify='left')
    s.line([(MX, y), (96, y)], end=True, start=True)
    s.text(98, y - 0.9, sig, 0.9, italic=True, justify='left bottom')
# GNSS
s.block(40, 86, 56, 15, ['GNSS RECEIVER', 'NEO-F10N (U9)', 'L1 + L5'], page=9)
s.antenna(30, 93.5, 'J11 / J12')
s.line([(30, 93.5), (40, 93.5)])
s.text(35, 92.6, 'SAW', 0.9, justify='bottom')
s.text(L, 90, 'UART1', 1.1, justify='left')
s.line([(MX, 90), (96, 90)], end=True, start=True)
s.text(L, 97, 'IO3', 1.1, justify='left')
s.line([(96, 97), (MX, 97)], end=True)
s.text(98, 96.1, 'TIMEPULSE', 0.9, italic=True, justify='left bottom')
# debug UART, USB
s.block(40, 107, 56, 10, ['DEBUG UART', 'J6'], page=12, fill=GRAY)
s.text(L, 112, 'UART0', 1.1, justify='left')
s.line([(MX, 112), (96, 112)], end=True, start=True)
s.block(40, 122, 56, 11, ['USB-C', 'J7, USB 2.0 full speed'], page=4, fill=GRAY)
s.text(L, 127.5, 'USB', 1.1, justify='left')
s.line([(MX, 127.5), (96, 127.5)], end=True, start=True)
s.text(98, 126.6, 'D+, D-', 0.9, italic=True, justify='left bottom')

# I2C_HOST, shared trunk
IX = 180
s.text(R, 60, 'I2C, IRQ', 1.1, justify='right')
s.line([(MX + MW, 60), (IX, 60)])
s.text(175, 59.1, 'I2C_HOST', 0.8, italic=True, justify='bottom')
devs = [(34, ['BATTERY MONITOR', 'MAX17320 (U7)', '0x36, 0x0B'], 6),
        (47, ['USB-PD CONTROLLER', 'TPS25751 (U5)', '0x20'], 4),
        (60, ['GPIO EXPANDER', 'TCAL9539 (U17)', '0x74'], 11),
        (73, ['MAGNETOMETER', 'QMC6309 (U2)', '0x7C'], 11),
        (86, ['IMU', 'LSM6DSV16X (U22)', '0x6A'], 11),
        (99, ['I2C CONNECTORS', 'J2, J3, J4, J16'], 12)]
s.line([(IX, 39.5), (IX, 104.5)])
s.dot(IX, 60)
for y, lines, p in devs:
    s.block(186, y, 46, 11, lines, page=p, fill=GRAY if lines[1].startswith('J') else None, size=1.1)
    s.line([(IX, y + 5.5), (186, y + 5.5)], end=True)
# I2C_PMIC, driven by U5
PX = 237
s.text(PX - 1.2, 39, 'I2C_PMIC', 0.8, italic=True, angle=90)
s.line([(232, 52.5), (PX, 52.5)])
s.line([(PX, 26), (PX, 52.5)])
for y, lines, p, dnp in [(21, ['CHARGER', 'BQ25798 (U8)', '0x6B'], 5, False),
                         (34, ['EEPROM', 'M24512 (U21)', '0x50'], 4, False),
                         (47, ['DEBUG HEADER', 'J8 (DNP)'], 4, True)]:
    s.block(242, y, 40, 11, lines, page=p, size=1.1, dash=dnp, fill=None if not dnp else None)
    s.line([(PX, y + 5.5), (242, y + 5.5)], end=True, dash=dnp)
s.line([(232, 65.5), (242, 65.5)], end=True)
s.block(242, 60, 40, 11, ['GPIO HEADER', 'J14, 16-pin'], page=11, fill=GRAY, size=1.1)
s.text(237, 64.6, 'P0, P1', 0.9, italic=True, justify='bottom')
# LED chain
s.text(R, 135.5, 'IO46', 1.1, justify='right')
s.line([(MX + MW, 135.5), (186, 135.5)], end=True)
s.text(178, 134.6, 'LED DATA', 0.9, italic=True, justify='bottom')
chain = [(186, 30, ['LEVEL SHIFTER', 'SN74LV1T34 (U19)']),
         (222, 30, ['RGB LEDS', '2x WS2816B (U18, U20)']),
         (258, 24, ['LED PORT', 'J15'])]
for i, (x, w, lines) in enumerate(chain):
    s.block(x, 130, w, 11, lines, page=11, size=1.0, fill=GRAY if lines[1] == 'J15' else None)
    if i:
        px, pw, _ = chain[i - 1]
        s.line([(px + pw, 135.5), (x, 135.5)], end=True)
s.write('block_diagram.kicad_sch', 'Block Diagram', 2)

# ================================================================== page 3: power tree
s = Sheet()
s.text(14, 22, 'POWER TREE', 3.0, bold=True, justify='left bottom')
s.oy = 8


def rail(pts, key, label=None, at=None, end=True, start=False, dash=False):
    s.line(pts, RAIL[key], 0.5 if not dash else 0.35, dash=dash, end=end, start=start)
    if label:
        x, y = at
        s.text(x, y, label, 1.0, justify='left bottom')


s.block(14, 26, 26, 12, ['USB-C', 'J7', '5-20 V'], page=4, fill=GRAY, size=1.1)
s.block(14, 48, 26, 12, ['SOLAR', 'J10', 'Voc max 20 V'], page=5, fill=GRAY, size=1.1)
s.block(48, 26, 26, 12, ['PD CONTROLLER', 'TPS25751 (U5)'], page=4, size=1.1)
s.block(82, 26, 26, 12, ['INPUT FETS', 'Q7, Q8'], page=5, size=1.1)
s.block(82, 48, 26, 12, ['INPUT FETS', 'Q10, Q9'], page=5, size=1.1)
s.block(116, 24, 30, 40, ['CHARGER', 'BQ25798 (U8)', 'buck-boost, NVDC', 'IIN max 3.3 A', 'ICHG max 5 A'],
        page=5, size=1.1)
rail([(40, 32), (48, 32)], 'vbus')
s.text(41, 31.1, 'VBUS', 0.9, justify='left bottom')
rail([(74, 32), (82, 32)], 'vbus')
s.text(75, 31.1, 'PPHV', 0.9, justify='left bottom')
rail([(108, 32), (116, 32)], 'vbus')
rail([(40, 54), (82, 54)], 'solar', 'SOLAR', (42, 53.1))
rail([(108, 54), (116, 54)], 'solar')
s.text(109, 31.1, 'VAC1', 0.9, justify='left bottom')
s.text(109, 53.1, 'VAC2', 0.9, justify='left bottom')
# OTG reverse
rail([(131, 24), (131, 20), (27, 20), (27, 26)], 'vbus', dash=True)
s.text(60, 19.1, 'OTG SOURCE, 5-20 V', 0.9, justify='left bottom')

# VSYS and the bucks
VX = 154
rail([(146, 32), (160, 32)], 'vsys', 'VSYS', (147, 31.1))
s.text(147, 35.4, '7-8.4 V', 0.9, justify='left bottom')
rail([(VX, 32), (VX, 96), (160, 96)], 'vsys')
s.dot(VX, 32, RAIL['vsys'])
s.block(160, 26, 28, 12, ['5 V BUCK', 'LM61460 (U14)', '5.0 V @ 6 A'], page=8, size=1.1)
s.block(160, 55, 28, 14, ['SYNC CLOCK', 'Y2, U16', '2 MHz, 180 deg'], page=8, size=1.1)
s.block(160, 90, 28, 12, ['3.3 V BUCK', 'LM61460 (U13)', '3.3 V @ 6 A'], page=8, size=1.1)
s.line([(174, 55), (174, 38)], RAIL['clk'], 0.25, end=True)
s.line([(174, 69), (174, 90)], RAIL['clk'], 0.25, end=True)
s.text(175, 47, 'SYNC', 0.9, justify='left')
s.text(175, 80, 'SYNC', 0.9, justify='left')

# 5 V branch
rail([(188, 32), (194, 32)], '5v')
s.text(188.5, 35.4, '5V_RAW', 0.8, justify='left bottom')
s.block(194, 26, 26, 12, ['EFUSE', 'TPS259474A (U15)', 'ILIM 5.4 A min'], page=8, size=1.0)
FX = 226
rail([(220, 32), (FX, 32)], '5v', end=False)
s.text(220.5, 31.1, '+5V', 0.9, justify='left bottom')
rail([(FX, 22.5), (FX, 67.5)], '5v', end=False)
s.dot(FX, 32, RAIL['5v'])
loads5 = [(18, ['LORA MODULE', 'E22P-915M30S (U12)'], 10),
          (29, ['RGB LEDS, LED PORT', 'U18, U20, J15 (F6 3 A)'], 11),
          (40, ['GNSS ANTENNA BIAS', 'TPS22945 (U6)'], 9),
          (51, ['USB SOURCE (OTG 5 V)', 'TPS25751 PP5V (U5)'], 4)]
for y, lines, p in loads5:
    s.block(232, y, 51, 9, lines, page=p, size=1.0)
    rail([(FX, y + 4.5), (232, y + 4.5)], '5v')
    s.dot(FX, y + 4.5, RAIL['5v'])
# GNSS sub-tree
rail([(FX, 67.5), (232, 67.5)], '5v')
s.block(232, 62, 23, 11, ['LDO', 'TPS7A2033 (U10)', '3.3 V @ 0.3 A'], page=9, size=0.9)
rail([(255, 67.5), (260, 67.5)], 'gnss')
s.block(260, 62, 23, 11, ['EFUSE', 'TPS259474L (U11)', 'ILIM 0.5 A'], page=9, size=0.9)
rail([(271.5, 73), (271.5, 78)], 'gnss')
s.text(270.5, 76, '3V3_GNSS', 0.8, justify='right')
s.block(260, 78, 23, 9, ['GNSS', 'NEO-F10N (U9)'], page=9, size=0.9)

# 3V3 branch
rail([(188, 96), (FX, 96)], '3v3', end=False)
s.text(190, 95.1, '+3V3', 0.9, justify='left bottom')
s.text(190, 99.4, '3.31 V', 0.9, justify='left bottom')
rail([(FX, 96), (FX, 140.5)], '3v3', end=False)
loads3 = [(92, ['MCU', 'ESP32-S3-WROOM-1 (U1)'], 7),
          (103, ['SENSORS, GPIO EXPANDER', 'U2, U22, U17'], 11),
          (114, ['SYNC CLOCK', 'Y2, U16'], 8),
          (125, ['PD CONTROLLER LOGIC', 'TPS25751 VIN_3V3 (U5)'], 4),
          (136, ['EXPANSION CONNECTORS', 'J1-J4, J16 (F1-F3, F7, F8)'], 12)]
for y, lines, p in loads3:
    s.block(232, y, 51, 9, lines, page=p, size=1.0)
    rail([(FX, y + 4.5), (232, y + 4.5)], '3v3')
    if y + 4.5 != 96:
        s.dot(FX, y + 4.5, RAIL['3v3'])

# battery path
rail([(131, 64), (131, 120), (122, 120)], 'bat', start=True)
s.text(132, 92, 'BAT', 0.9, justify='left')
s.block(96, 114, 26, 12, ['SHIP FET', 'CSD17577Q3A (Q11)'], page=5, size=1.0)
rail([(96, 120), (88, 120)], 'bat', start=True)
s.text(88.5, 119.1, '+PACK', 0.8, justify='left bottom')
s.block(62, 114, 26, 12, ['CHG / DSG FETS', 'AON7544 (Q5, Q6)'], page=6, size=1.0)
s.block(62, 134, 26, 12, ['BATTERY MONITOR', 'MAX17320 (U7)'], page=6, size=1.0)
s.line([(75, 134), (75, 126)], RAIL['clk'], 0.25, end=True)
rail([(62, 120), (52, 120)], 'bat', start=True)
s.text(53, 119.1, '+BATT', 0.8, justify='left bottom')
s.text(53, 123.4, 'F5 10 A', 0.8, justify='left bottom')
s.block(14, 112, 38, 16, ['BATTERY', '2S 21700 (BT1, BT2)', '6.0-8.4 V'], page=6, size=1.1)
rail([(33, 128), (33, 134)], 'bat', end=False)
s.block(14, 134, 38, 12, ['REVERSE-POLARITY FET', 'TPN2R203NC (Q16)'], page=6, size=1.0)
s.write('power_tree.kicad_sch', 'Power Tree', 3)
