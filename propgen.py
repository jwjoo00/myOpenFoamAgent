"""
propgen.py — CAD-free parametric marine-propeller geometry generator.

Builds a propeller STL from a published radial offset table (r/R, c/D, P/D, camber,
thickness) by constructing each blade section, rotating it to the local pitch angle,
wrapping it onto the cylinder of that radius, lofting the sections into a blade, then
patterning n_blades around the axis and adding a hub. Also emits a rotating-zone
cylinder STL for the MRF cellZone. Sections use a parabolic camber line + NACA-style
thickness (1st-order approx of the DTMB NACA66/a=0.8 sections).

Triangle-soup output (blades + hub need not be a clean boolean union — snappyHexMesh
ray-casts and tolerates the blade/hub intersection).
"""
from __future__ import annotations

from pathlib import Path

# DTMB 4119: 3 blades, D=0.3048 m, hub/D=0.2, no skew/rake, NACA66 + a=0.8 mean line.
# Canonical published radial distributions (design P/D(0.7R)=1.084).
#         r/R     c/D      P/D     camber f/c  thickness t/c
DTMB_4119 = [
    (0.20, 0.3200, 1.105, 0.01429, 0.2055),
    (0.30, 0.3625, 1.102, 0.02318, 0.1553),
    (0.40, 0.4048, 1.098, 0.02303, 0.11800),
    (0.50, 0.4392, 1.093, 0.02182, 0.09016),
    (0.60, 0.4610, 1.088, 0.02072, 0.06960),
    (0.70, 0.4622, 1.084, 0.02003, 0.05418),
    (0.80, 0.4347, 1.081, 0.01967, 0.04206),
    (0.90, 0.3613, 1.079, 0.01817, 0.03321),
    (0.95, 0.2775, 1.077, 0.01631, 0.03228),
    (1.00, 0.0150, 1.075, 0.01175, 0.03160),   # tiny tip chord (true 0 → degenerate)
]


def _meanline_a08(u, a=0.8):
    """NACA a=0.8 mean line ordinate y_c/c for design Cl_i=1 (Abbott & von Doenhoff,
    NACA Rep. 824). DTMB 4119의 실제 meanline — uniform loading 0..a then linear to TE.
    parabolic 근사보다 정확한 camber 분포(앞전 loading) → 정확한 유효 받음각·Kt."""
    import numpy as np
    x = np.clip(u, 1e-6, 1 - 1e-6)
    om = 1.0 - a
    g = -(1.0 / om) * (a * a * (0.5 * np.log(a) - 0.25) + 0.25)
    h = (1.0 / om) * (0.5 * om * om * np.log(om) - 0.25 * om * om) + g
    ax = a - x
    oneX = 1.0 - x
    c1 = (1.0 / om) * (0.5 * ax * ax * np.log(np.abs(ax) + 1e-12) - 0.5 * oneX * oneX * np.log(oneX)
                       + 0.25 * oneX * oneX - 0.25 * ax * ax)
    return (1.0 / (2 * np.pi * (a + 1.0))) * (c1 - x * np.log(x) + g - h * x)


# Wageningen B-series — parametric marine-propeller family. Standard blade-outline (chord) and
# thickness distributions; pick Z (blades), AE/A0 (expanded-area ratio), P/D (pitch ratio).
# Kc = c·Z/(D·AE/A0) (normalized chord) and t/D per r/R are the series' fixed distributions.
_B_RR = [0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9, 1.0]
_B_KC = [1.662, 1.882, 2.050, 2.152, 2.187, 2.144, 1.970, 1.582, 0.100]
_B_TD = [0.0366, 0.0324, 0.0282, 0.0240, 0.0198, 0.0156, 0.0114, 0.0072, 0.0030]
_B_FC = [0.0200, 0.0200, 0.0190, 0.0180, 0.0170, 0.0160, 0.0150, 0.0140, 0.0120]


def wageningen_b_series(Z=4, AE_A0=0.70, PD=1.0):
    """Wageningen B-series 프로펠러의 offset table(r/R, c/D, P/D, camber, thickness) 생성.
    Z=블레이드수(2~7), AE_A0=전개면적비(0.3~1.05), PD=피치비(0.5~1.4). 일정 피치.
    generate_propeller(table=...)에 넘기면 STL이 나온다. 예: B4-70 = Z=4, AE_A0=0.70."""
    tbl = []
    for rr, kc, td, fc in zip(_B_RR, _B_KC, _B_TD, _B_FC):
        cD = max(kc * float(AE_A0) / max(int(Z), 1), 0.003)   # c/D
        tc = td / cD                                          # t/c
        tbl.append((rr, cD, float(PD), fc, min(tc, 0.30)))
    return tbl


# Oosterveld & van Oossanen (1975) open-water regression for the Wageningen B-series (Re=2e6 base):
#   KT = Σ C·J^s·(P/D)^t·(AE/A0)^u·Z^v   (39 terms),  KQ = Σ ... (47 terms). Reference for CFD compare.
_KT_COEF = [
    (0.00880496, 0, 0, 0, 0), (-0.204554, 1, 0, 0, 0), (0.166351, 0, 1, 0, 0), (0.158114, 0, 2, 0, 0),
    (-0.147581, 2, 0, 1, 0), (-0.481497, 1, 1, 1, 0), (0.415437, 0, 2, 1, 0), (0.0144043, 0, 0, 0, 1),
    (-0.0530054, 2, 0, 0, 1), (0.0143481, 0, 1, 0, 1), (0.0606826, 1, 1, 0, 1), (-0.0125894, 0, 0, 1, 1),
    (0.0109689, 1, 0, 1, 1), (-0.133698, 0, 3, 0, 0), (0.00638407, 0, 6, 0, 0), (-0.00132718, 2, 6, 0, 0),
    (0.168496, 3, 0, 1, 0), (-0.0507214, 0, 0, 2, 0), (0.0854559, 2, 0, 2, 0), (-0.0504475, 3, 0, 2, 0),
    (0.010465, 1, 6, 2, 0), (-0.00648272, 2, 6, 2, 0), (-0.00841728, 0, 3, 0, 1), (0.0168424, 1, 3, 0, 1),
    (-0.00102296, 3, 3, 0, 1), (-0.0317791, 0, 3, 1, 1), (0.018604, 1, 0, 2, 1), (-0.00410798, 0, 2, 2, 1),
    (-0.000606848, 0, 0, 0, 2), (-0.0049819, 1, 0, 0, 2), (0.0025983, 2, 0, 0, 2), (-0.000560528, 3, 0, 0, 2),
    (-0.00163652, 1, 2, 0, 2), (-0.000328787, 1, 6, 0, 2), (0.000116502, 2, 6, 0, 2), (0.000690904, 0, 0, 1, 2),
    (0.00421749, 0, 3, 1, 2), (0.0000565229, 3, 6, 1, 2), (-0.00146564, 0, 3, 2, 2),
]
_KQ_COEF = [
    (0.00379368, 0, 0, 0, 0), (0.00886523, 2, 0, 0, 0), (-0.032241, 1, 1, 0, 0), (0.00344778, 0, 2, 0, 0),
    (-0.0408811, 0, 1, 1, 0), (-0.108009, 1, 1, 1, 0), (-0.0885381, 2, 1, 1, 0), (0.188561, 0, 2, 1, 0),
    (-0.00370871, 1, 0, 0, 1), (0.00513696, 0, 1, 0, 1), (0.0209449, 1, 1, 0, 1), (0.00474319, 2, 1, 0, 1),
    (-0.00723408, 2, 0, 1, 1), (0.00438388, 1, 1, 1, 1), (-0.0269403, 0, 2, 1, 1), (0.0558082, 3, 0, 1, 0),
    (0.0161886, 0, 3, 1, 0), (0.00318086, 1, 3, 1, 0), (0.015896, 0, 0, 2, 0), (0.0471729, 1, 0, 2, 0),
    (0.0196283, 3, 0, 2, 0), (-0.0502782, 0, 1, 2, 0), (-0.030055, 3, 1, 2, 0), (0.0417122, 2, 2, 2, 0),
    (-0.0397722, 0, 3, 2, 0), (-0.00350024, 0, 6, 2, 0), (-0.0106854, 3, 0, 0, 1), (0.00110903, 3, 3, 0, 1),
    (-0.000313912, 0, 6, 0, 1), (0.0035985, 3, 0, 1, 1), (-0.00142121, 0, 6, 1, 1), (-0.00383637, 1, 0, 2, 1),
    (0.0126803, 0, 2, 2, 1), (-0.00318278, 2, 3, 2, 1), (0.00334268, 0, 6, 2, 1), (-0.00183491, 1, 1, 0, 2),
    (0.000112451, 3, 2, 0, 2), (-0.0000297228, 3, 6, 0, 2), (0.000269551, 1, 0, 1, 2), (0.00083265, 2, 0, 1, 2),
    (0.00155334, 0, 2, 1, 2), (0.000302683, 0, 6, 1, 2), (-0.0001843, 0, 0, 2, 2), (-0.000425399, 0, 3, 2, 2),
    (0.0000869243, 3, 3, 2, 2), (-0.0004659, 0, 6, 2, 2), (0.0000554194, 1, 6, 2, 2),
]


def bseries_KT_KQ(J, PD, AE_A0, Z):
    """Wageningen B-series open-water KT, KQ(=10·Kq/10) from the Oosterveld-van Oossanen polynomial.
    반환 (KT, KQ). CFD MRF 결과 검증의 reference. (Re=2e6 기준; 우리 ~1e6은 약간 차이)."""
    KT = sum(C * J**s * PD**t * AE_A0**u * Z**v for C, s, t, u, v in _KT_COEF)
    KQ = sum(C * J**s * PD**t * AE_A0**u * Z**v for C, s, t, u, v in _KQ_COEF)
    return KT, KQ


# NACA 66 (DTMB-modified) thickness form — max thickness at ~45% chord (vs NACA-4 at 30%), the
# actual DTMB 4119 section. Normalized half-thickness y/ymax vs x/c (round LE, sharp TE).
_N66_X = [0.0, 0.025, 0.05, 0.10, 0.15, 0.20, 0.30, 0.40, 0.45, 0.50, 0.60, 0.70, 0.80, 0.90, 0.95, 1.0]
_N66_Y = [0.0, 0.345, 0.470, 0.645, 0.770, 0.865, 0.960, 0.998, 1.000, 0.988, 0.920, 0.790, 0.595, 0.345, 0.215, 0.0]


def _section(rR, cD, PD, camber, thickness, D, n_chord, handed, meanline="a08", thickness_form="naca4"):
    import numpy as np
    R = D / 2.0
    r = rR * R
    c = cD * D
    P = PD * D
    phi = np.arctan2(P, 2.0 * np.pi * r)                     # local pitch angle
    u = (1 - np.cos(np.linspace(0.0, np.pi, n_chord))) / 2.0  # 0(LE)..1(TE), cosine spacing
    if meanline == "a08":                                   # exact DTMB camber line
        prof = _meanline_a08(u)
        yc = prof * (camber * c / max(prof.max(), 1e-12))   # scale so max camber = (f/c)*c
    else:
        yc = 4.0 * camber * c * u * (1 - u)                 # parabolic fallback
    if thickness_form == "naca66":                          # exact DTMB thickness form (peak ~45%)
        yt = 0.5 * thickness * c * np.interp(u, _N66_X, _N66_Y)
    else:
        yt = 5.0 * thickness * c * (0.2969 * np.sqrt(u) - 0.1260 * u - 0.3516 * u**2
                                    + 0.2843 * u**3 - 0.1036 * u**4)   # NACA-4 thickness
    # round LE(u=0) must face the relative inflow (+axial side) → place LE at +c/2, TE at -c/2.
    xc = (0.5 - u) * c                                      # chordwise, LE at +c/2 (faces inflow)
    surf = {}
    for name, sgn in (("back", +1.0), ("face", -1.0)):
        yn = yc + sgn * yt
        axial = xc * np.sin(phi) + yn * np.cos(phi)
        tang = handed * (xc * np.cos(phi) - yn * np.sin(phi))
        ang = tang / r
        surf[name] = np.column_stack([r * np.cos(ang), axial, r * np.sin(ang)])
    return surf


def _blade(table, D, n_chord, handed, meanline="a08", thickness_form="naca4"):
    import numpy as np
    secs = [_section(*row, D, n_chord, handed, meanline, thickness_form) for row in table]
    nr, nc = len(secs), n_chord
    backs = np.array([s["back"] for s in secs])
    faces = np.array([s["face"] for s in secs])
    verts = []
    bi = [[0] * nc for _ in range(nr)]
    fi = [[0] * nc for _ in range(nr)]
    for i in range(nr):
        for j in range(nc):
            verts.append(backs[i, j]); bi[i][j] = len(verts) - 1
            if j == 0 or j == nc - 1:           # LE/TE: face coincides with back (yt=0)
                fi[i][j] = bi[i][j]
            else:
                verts.append(faces[i, j]); fi[i][j] = len(verts) - 1
    tris = []

    def quad(a, b, c, d):
        tris.append([a, b, c]); tris.append([a, c, d])
    for i in range(nr - 1):
        for j in range(nc - 1):
            quad(bi[i][j], bi[i][j + 1], bi[i + 1][j + 1], bi[i + 1][j])
            quad(fi[i][j], fi[i + 1][j], fi[i + 1][j + 1], fi[i][j + 1])
    for i in (0, nr - 1):                        # cap root & tip cross-sections
        for j in range(nc - 1):
            quad(bi[i][j], fi[i][j], fi[i][j + 1], bi[i][j + 1])
    return np.array(verts), np.array(tris)


def generate_propeller(out_dir, table=None, n_blades=3, diameter=0.3048, hub_ratio=0.2,
                       handed=1, n_chord=60, zone_scale=1.25, meanline="a08",
                       thickness_form="naca4") -> dict:
    """propeller.stl(블레이드+허브) + rotatingZone.stl(MRF cellZone 실린더)을 out_dir에 쓴다.
    table: (r/R, c/D, P/D, camber, thickness) 행 목록(기본 DTMB 4119). handed=±1 회전손잡이.
    meanline='a08'(정확한 NACA a=0.8) 또는 'parabolic'. thickness_form='naca66'(DTMB 정확, peak ~45%)
    또는 'naca4'(기본)."""
    import numpy as np
    import trimesh
    tbl = table or DTMB_4119
    out = Path(out_dir); out.mkdir(parents=True, exist_ok=True)
    bv, bt = _blade(tbl, diameter, n_chord, int(handed), meanline, thickness_form)
    blade = trimesh.Trimesh(vertices=bv, faces=bt, process=True)
    R = diameter / 2.0
    yext = float(max(abs(bv[:, 1].min()), abs(bv[:, 1].max())))
    rotX = trimesh.transformations.rotation_matrix(np.pi / 2, [1, 0, 0])
    parts = []
    for k in range(n_blades):
        a = 2.0 * np.pi * k / n_blades
        cs, sn = np.cos(a), np.sin(a)
        T = np.array([[cs, 0, sn, 0], [0, 1, 0, 0], [-sn, 0, cs, 0], [0, 0, 0, 1]])
        bl = blade.copy(); bl.apply_transform(T); parts.append(bl)
    parts.append(trimesh.creation.cylinder(radius=hub_ratio * R, height=2.5 * yext + 0.05,
                                           sections=48, transform=rotX))
    prop = trimesh.util.concatenate(parts)
    prop_path = out / "propeller.stl"; prop.export(prop_path)
    zone = trimesh.creation.cylinder(radius=zone_scale * R, height=3.0 * yext + 0.06,
                                     sections=64, transform=rotX)
    zone_path = out / "rotatingZone.stl"; zone.export(zone_path)
    return {"ok": True, "propeller_stl": str(prop_path), "rotatingZone_stl": str(zone_path),
            "diameter": diameter, "n_blades": n_blades, "hub_ratio": hub_ratio,
            "handed": int(handed), "axial_extent_m": round(yext, 4),
            "bbox": [[round(x, 4) for x in row] for row in prop.bounds.tolist()],
            "watertight": bool(prop.is_watertight), "n_faces": int(len(prop.faces))}
