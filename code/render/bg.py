"""Studio backdrop for the spin: soft elliptical vignette, 16-bit with +-0.5 LSB(8-bit) dither.
Usage: bg.py W H OUT [center_hex edge_hex]  (writes OUT.rgb48, raw rgb48le)"""
import sys, numpy as np
W, H, out = int(sys.argv[1]), int(sys.argv[2]), sys.argv[3]
c0, c1 = sys.argv[4:6] if len(sys.argv) > 5 else ('1f242c', '0d1117')   # edge = GitHub dark page
lin = lambda h: (np.array([int(h[i:i + 2], 16) for i in (0, 2, 4)]) / 255.0) ** 2.2   # blend in ~linear light
y, x = np.mgrid[0:H, 0:W]
r = np.sqrt(((x - W / 2) / (0.62 * W)) ** 2 + ((y - 0.47 * H) / (0.58 * H)) ** 2)
t = np.clip(r, 0, 1) ** 1.6
img = (lin(c0)[None, None] * (1 - t[..., None]) + lin(c1)[None, None] * t[..., None]) ** (1 / 2.2)
img = img * 65535 + np.random.default_rng(1).uniform(-128, 128, img.shape)        # +-0.5 LSB at 8 bit
np.clip(img, 0, 65535).astype(np.uint16).tofile(out + '.rgb48')
print(out + '.rgb48', W, H)
