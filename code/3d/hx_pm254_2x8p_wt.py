"""3D model of the Hanxia HX PM2.54-2x8P WT (LCSC C46635853), 2x8 2.54 mm right-angle SMD
female header. No vendor model exists; dims are from its drawing
(docs/datasheets/peripherals/HX_PM2.54-2X8P_WT.pdf).

Frame = KiCad footprint HDR-SMD_16P-P2.54_HXPM2.54-2X8PWT with model y = -footprint y, so the
footprint's model entry needs no offset or rotation. Housing lies flat on the board, mating face
at footprint y = 13.60 (board edge), rear at 5.10. Odd pins = row nearest the PCB (3.5 mm lead,
pads at fp y +2.25), even pins = upper row (8.0 mm lead, pads at fp y -2.25).

    uv run --python 3.12 --with build123d python code/3d/hx_pm254_2x8p_wt.py
"""
from pathlib import Path
from build123d import (Box, Color, Compound, FilletPolyline, Plane, Pos, Rectangle, export_step,
                       loft, sweep)

COLS = 8
PITCH = 2.54
W = COLS * PITCH + 0.5        # drawing: (B+0.5) +/-0.3
H, D = 5.00, 8.50             # face height (2 rows), insertion depth
REAR = -5.10                  # model y of the rear face; the mating face is at REAR - D
ROW_Z = (H / 2 - PITCH / 2, H / 2 + PITCH / 2)   # 1.23 / 3.77, lower / upper row centres
LEAD_W, LEAD_T = 0.64, 0.40   # drawing: PIN 0.64x0.4 (0.64 along the pin row)
BEND_R = 0.4                  # centreline bend radius, eyeballed from the drawing
HOLE_OUT, HOLE_IN, FUNNEL, HOLE_DEPTH = 1.6, 0.8, 0.43, 2.2   # entry: 1.6 sq chamfered to 0.8 sq

# lead centrelines as (distance behind the rear face, z); drawing: 3.5 / 8.0 to the foot end,
# 1.3 mm flat foot, ~45 deg jog. Starts 1.8 mm behind the face so its tip shows in the hole.
TIP = -(D - 1.8)
PATHS = (
    [(TIP, ROW_Z[0]), (1.0, ROW_Z[0]), (2.2, LEAD_T / 2), (3.5, LEAD_T / 2)],
    [(TIP, ROW_Z[1]), (3.3, ROW_Z[1]), (6.7, LEAD_T / 2), (8.0, LEAD_T / 2)],
)


def build():
    face_y = REAR - D
    housing = Pos(0, REAR - D / 2, H / 2) * Box(W, D, H)
    p_face = Plane(origin=(0, face_y, 0), x_dir=(1, 0, 0), z_dir=(0, 1, 0))  # z_dir points into the body
    funnel = loft([p_face * Rectangle(HOLE_OUT, HOLE_OUT),
                   p_face.offset(FUNNEL) * Rectangle(HOLE_IN, HOLE_IN)])
    shaft = p_face.offset(FUNNEL) * Pos(0, 0, (HOLE_DEPTH - FUNNEL) / 2) * Box(HOLE_IN, HOLE_IN, HOLE_DEPTH - FUNNEL)
    hole = funnel + shaft

    leads = []
    for c in range(COLS):
        x = -PITCH * (COLS - 1) / 2 + c * PITCH
        for row, z in enumerate(ROW_Z):
            housing -= Pos(x, 0, z) * hole
            pts = [(x, REAR + d, zz) for d, zz in PATHS[row]]
            path = FilletPolyline(*pts, radius=BEND_R)
            prof = Plane(origin=pts[0], x_dir=(1, 0, 0), z_dir=(0, 1, 0)) * Rectangle(LEAD_W, LEAD_T)
            lead = sweep(prof, path)
            lead.label, lead.color = f"pin{2 * c + 1 + row}", Color(0.86, 0.74, 0.50)
            leads.append(lead)

    housing.label, housing.color = "housing", Color(0.12, 0.12, 0.12)
    return Compound(label="HX_PM2.54-2x8P_WT", children=[housing, *leads])


if __name__ == "__main__":
    model = build()
    bb = model.bounding_box()
    print(f"bbox x {bb.min.X:.2f}..{bb.max.X:.2f}  y {bb.min.Y:.2f}..{bb.max.Y:.2f}  z {bb.min.Z:.2f}..{bb.max.Z:.2f}")
    # self-check against the drawing and footprint pads (fp y = -model y)
    assert abs(bb.size.X - 20.82) < 0.01 and abs(bb.max.Z - H) < 1e-6 and abs(bb.min.Z) < 1e-6
    assert abs(bb.min.Y - (REAR - D)) < 1e-6 and abs(bb.max.Y - (REAR + 8.0)) < 1e-3
    for lead in model.children[1:]:
        lb = lead.bounding_box()
        n = int(lead.label[3:])
        foot_y = -2.25 if n % 2 else 2.25          # model y of the pad centre
        assert abs(lb.center().X - (-8.89 + 2.54 * ((n - 1) // 2))) < 1e-3, lead.label
        assert abs(lb.max.Y - (foot_y + 0.65)) < 1e-3 and abs(lb.min.Z) < 1e-6, lead.label
    out = Path(__file__).resolve().parents[2] / "lib/3d-models/HDR-SMD_16P-P2.54_HXPM2.54-2X8PWT.step"
    export_step(model, str(out))
    print("wrote", out)
