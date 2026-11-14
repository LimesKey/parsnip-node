"""Mock Meshtastic status screen for the WeAct 3.7" e-paper (240x416 layout from the old render),
drawn antialiased at 4x and placed on the full paper area of the panel. Usage: screen.py OUT.png"""
import sys
from PIL import Image, ImageDraw, ImageFont
S = 4                                    # supersample of the 240x416 layout
W, H = 240 * S, 416 * S
PAPER, INK = 208, 20                     # grey levels (texture is sRGB)
img = Image.new('L', (W, H), PAPER)
d = ImageDraw.Draw(img)
mono = lambda s: ImageFont.truetype('/usr/share/fonts/TTF/DejaVuSansMono-Bold.ttf', s * S)
sans = lambda s: ImageFont.truetype('/usr/share/fonts/TTF/DejaVuSans-Bold.ttf', s * S)
R = lambda *v: tuple(x * S for x in v)
d.rectangle(R(0, 0, 240, 34), fill=INK)
d.text(R(8, 6), 'parsnip', font=sans(20), fill=PAPER)
d.rectangle(R(168, 10, 200, 24), outline=PAPER, width=2 * S); d.rectangle(R(201, 14, 204, 20), fill=PAPER)
d.rectangle(R(171, 13, 194, 21), fill=PAPER)
d.text(R(208, 9), '87', font=mono(14), fill=PAPER)
d.text(R(120, 78), '14:32', font=sans(62), fill=INK, anchor='mm')
d.text(R(120, 120), 'Sat 14 Nov', font=mono(16), fill=INK, anchor='mm')
d.line(R(8, 138, 232, 138), fill=INK, width=2 * S)
y = 146
for a, b in (('LongFast', '915 MHz'), ('Nodes', '12 / 31'), ('GPS', '3D  14 sat'), ('Batt', '7.9 V')):
    d.text(R(10, y), a, font=mono(17), fill=INK); d.text(R(230, y), b, font=mono(17), fill=INK, anchor='ra')
    y += 25
d.line(R(8, y + 4, 232, y + 4), fill=INK, width=2 * S)
y += 8
for name, hops, sig in (('ridge-relay', '2 hop', '-92 dBm  7.5 dB'), ('trail-cam-07', '1 hop', '-104 dBm -3.2 dB'),
                        ('base-camp', '3 hop', '-88 dBm  9.0 dB')):
    d.text(R(10, y), name, font=mono(16), fill=INK); d.text(R(230, y), hops, font=mono(14), fill=INK, anchor='ra')
    d.text(R(18, y + 20), sig, font=mono(13), fill=INK)
    y += 40
d.rectangle(R(0, 416 - 28, 240, 416), fill=INK)
d.text(R(120, 416 - 14), 'TX 30 dBm   up 3d 04h', font=mono(14), fill=PAPER, anchor='mm')
# paper area of the panel (51.4 x 91.5 minus 0.3 mm border) with the 47.0 x 81.4 active area centred
PX = 20                                  # px per mm
PW, PH, AW, AH = 50.8, 90.9, 47.0, 81.4
paper = Image.new('L', (round(PW * PX), round(PH * PX)), PAPER)
act = img.resize((round(AW * PX), round(AH * PX)), Image.LANCZOS)
paper.paste(act, ((paper.width - act.width) // 2, (paper.height - act.height) // 2))
paper.convert('RGB').save(sys.argv[1])
print(paper.size)
