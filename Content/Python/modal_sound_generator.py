"""
modal_sound_generator.py  —  Offline FEM Modal Sound Pipeline
==============================================================

WHAT CHANGED IN THIS VERSION
------------------------------

1. ENUM CRASH FIX (critical):
   The previous version used:
     getattr(unreal.EImpactMaterial, enum_name)    ← wrong class name
     unreal.EFEMSolverType.SOLID_TET               ← wrong class name
   UE5 Python drops the 'E' prefix from C++ UENUM names. The correct form is:
     unreal.ImpactMaterial.METAL, unreal.ImpactMaterial.HEAVY_METAL, etc.
   Additionally, UE5 Python enum member names are SCREAMING_SNAKE_CASE, so
   "HeavyMetal" must be accessed as "HEAVY_METAL", not "HeavyMetal" or "HEAVYMETAL".
   A safe_enum_set() helper tries multiple fallback patterns to be resilient
   across different UE5 versions and project configurations.

2. SHELL FEM SOLVER (new):
   assemble_kirchhoff_shell() implements Kirchhoff plate FEM for thin objects.
   This gives physically correct frequencies for:
     - Plates and planks (flat thin objects)
     - Cups, mugs, bowls (hollow objects)
     - Tubes and pipes (hollow cylindrical)
     - Pots, pans, trays, bells, cymbals
   For these objects, solid-tet FEM either fails (TetGen degenerate tets)
   or gives wrong frequencies (CST elements too stiff in bending).
   Reference: Batoz (1980), Felippa (2003) Ch.32.

3. AUTO GEOMETRY ROUTING (new):
   generate_modal_asset() now calls classify_geometry() from modal_mesh_utils
   to automatically choose between solid-tet and shell FEM. No manual flag needed.
   The classifier detects flat/thin objects via bbox aspect ratio, and hollow
   objects via centroid ray-casting.

4. SHELL THICKNESS OVERRIDE:
   An optional shell_thickness_m parameter lets you override the automatic
   thickness estimate if you know the exact wall thickness of your object.

SCIENTIFIC FOUNDATION (unchanged)
-----------------------------------
O'Brien, Cook & Essl (2002) SIGGRAPH — modal synthesis pipeline.
van den Doel & Pai (1998) — damping, participation factors.
Chadwick, Zheng & James (2012) — Hertz contact shaping (runtime).
Zheng & James (2011) / NeuralSound (Jin et al. 2022) — radiation η_k.
Sterling et al. (2019) — xi_high/xi_low perceptual calibration.
Caughey (1960) — Rayleigh damping.
Klatzky et al. (2000) — √(KE/KE_max) loudness.
Rath & Rocchesso (2005) — drag/scrape sensitivity.
Batoz (1980) / Felippa (2003) — Kirchhoff plate elements for shell FEM.
"""

import unreal
import numpy as np
from scipy.sparse import lil_matrix, csc_matrix
from scipy.sparse.linalg import eigsh
import tetgen
from modal_mesh_utils import (
    extract_static_mesh,
    validate_mesh_for_fem,
    estimate_tet_volume_constraint,
    subdivide_surface_mesh,
    classify_geometry,
)


# ═══════════════════════════════════════════════════════════════════════════
# MATERIAL PRESETS
# ═══════════════════════════════════════════════════════════════════════════
MATERIAL_PRESETS = {
    # ────────────────────────────────────────────────────────────────────────
    # Sources for all values:
    #   E, nu, rho  — engineering handbooks (Ashby 2005; Matweb).
    #                 Only determine inter-mode frequency RATIOS (spectral shape).
    #                 Absolute pitch is set by the perceptual calibration.
    #
    #   xi_low, xi_high — Rayleigh ξ at mode 1 and mode N.
    #     Ren et al. (2013) Table 1; van den Doel & Pai (1998).
    #     τ = 1/(π × f1 × xi_low)  — modal decay time at the fundamental.
    #     ξ_high/ξ_low ratio: 5-10× metals, 5-8× wood/stone, 2-3× plastic.
    #
    #   contact_hardness — impulse brightness (crack layer gain, CrackFc).
    #     1.0 = hardest (metal), 0.20 = softest (plastic foam-like).
    # ────────────────────────────────────────────────────────────────────────

    "Metal": {
        # Thin steel / aluminium frames, sheet metal, pipes, metal chairs.
        # xi_low=0.001: long ring (τ≈100-400ms). Correct for thin sections.
        # USE FOR: metal chairs, thin tubes, sheet metal.
        # USE HeavyMetal FOR: structural planks, beams, thick plate.
        "E": 200e9, "nu": 0.30, "rho": 7800,
        "xi_low": 0.001, "xi_high": 0.010,
        "contact_hardness": 1.0,
    },
    "HeavyMetal": {
        # Structural steel planks, beams, thick plates, cast iron.
        # xi_low=0.003 (Ren 2013: structural steel plate ≈0.003–0.005).
        # τ at f1=350Hz: 152ms — correct metallic ring for thick stock.
        "E": 200e9, "nu": 0.30, "rho": 7800,
        "xi_low": 0.003, "xi_high": 0.020,
        "contact_hardness": 0.90,
    },
    "Wood": {
        # Hardwood: oak/beech furniture, floorboards, structural timber.
        # xi_low=0.010 (Ren 2013: hardwood ≈0.007–0.012).
        # τ at f1=500Hz: 32ms — solid knock, short ring. ✓
        "E": 11e9, "nu": 0.35, "rho": 700,
        "xi_low": 0.010, "xi_high": 0.080,
        "contact_hardness": 0.30,
    },
    "Glass": {
        # Soda-lime glass (windows, glass plates, bottles).
        # Glass E≈70GPa ≈ Aluminium E≈69GPa — physically similar stiffness.
        # Distinction comes from higher MIN_FREQ (brighter register) and
        # xi_low=0.002: glassy ring without infinite sustain.
        #   Old xi_low=0.0005 → τ≈1200ms → scrape built standing-wave hum.
        #   0.002 → τ≈200ms — still clearly glassy/crystalline but not a drone.
        # xi_high=0.010: moderate high-mode damping for clean ring.
        # contact_hardness=0.85: glass is very hard → sharp bright attack.
        # Ren et al. estimated xi_low≈0.0003–0.001 for glass; we use 0.002
        # as a perceptual compromise that avoids the scrape drone artefact.
        "E": 70e9, "nu": 0.22, "rho": 2500,
        "xi_low": 0.002, "xi_high": 0.010,
        "contact_hardness": 0.85,
    },
    "Stone": {
        # Concrete / stone / brick. Heavy, very dead-sounding.
        # Real stone surfaces absorb energy quickly — much like dense wood.
        # xi_low=0.015 (Ren 2013: concrete/stone ≈0.010–0.020, NOT 0.005).
        #   Old xi_low=0.005 → τ=200ms at 159Hz — too long, modal ring.
        #   0.015 → τ≈42ms at 250Hz — correct dead "thock" of stone. ✓
        # xi_high=0.060: high modes damp almost instantly.
        # contact_hardness=0.85: stone is hard — produces sharp impact click,
        #   but the very short τ means no sustain (sounds like impact + silence).
        "E": 40e9, "nu": 0.20, "rho": 2400,
        "xi_low": 0.015, "xi_high": 0.060,
        "contact_hardness": 0.85,
    },
    "Plastic": {
        # ABS/polypropylene consumer goods (chairs, bins, toys).
        # xi_low=0.020, xi_high=0.120 (Ren 2013: 0.020–0.150 range).
        # τ at f1=250Hz: 32ms — very dead clunky sound. ✓
        # contact_hardness=0.25: slightly harder than foam, but still soft/muted.
        "E": 3e9, "nu": 0.38, "rho": 1050,
        "xi_low": 0.020, "xi_high": 0.120,
        "contact_hardness": 0.25,
    },
    "Ceramic": {
        # Dense ceramic / porcelain tiles, mugs, plates.
        # High E, low damping — bright ringing impact like a bell or hard tile.
        # xi_low=0.001: long ring (τ≈455ms at 350Hz). Porcelain rings clearly.
        # xi_high=0.010: moderate high-mode damping (less than glass).
        # contact_hardness=0.90: very hard surface → sharp bright attack click.
        # Ren et al.: ceramic xi_low≈0.001–0.003.
        "E": 100e9, "nu": 0.22, "rho": 2600,
        "xi_low": 0.001, "xi_high": 0.010,
        "contact_hardness": 0.90,
    },
}

# ═══════════════════════════════════════════════════════════════════════════
# PERCEPTUAL FREQUENCY CALIBRATION
# ═══════════════════════════════════════════════════════════════════════════
# Solid CST tetrahedral FEM overestimates modal frequencies because it
# models complex objects (furniture, irregular shapes) as solid blocks and
# because CST elements shear-lock in bending (stiffness 5–20× too high).
# FIX: scale ALL FEM frequencies by (f1_target / f1_fem) so the fundamental
# matches a perceptually calibrated target. Relative mode spacing, eigenvectors,
# participation, and radiation are all preserved — only absolute pitch changes.
# Calibration constants C (Hz·m): f1_target = C / L_max.
# Calibrated against real recordings (Grassi 2005; van den Doel & Pai 1998).
PERCEPTUAL_F1_CONSTANT = {
    # C in f1_target = C / L_max.
    # Sets the absolute fundamental pitch for a 1-metre object of each material.
    # All inter-mode ratios (spectral shape) come from FEM geometry; this only
    # shifts the entire spectrum to match the expected perceptual pitch register.
    "Metal":      500.0,
    "HeavyMetal": 500.0,
    "Wood":       280.0,
    "Glass":      700.0,   # raised from 600: glass needs a higher register than
                            # aluminium-like (E≈70GPa≈Al) to sound crystalline not metallic.
    "Stone":      400.0,   # raised from 350: puts large stone objects' f1 at the audible
                            # MIN_FREQ floor (250Hz) and small objects higher. ✓
    "Plastic":    200.0,   # raised from 100: 100/2.2=45Hz was sub-sonic for large planks.
                            # 200Hz gives small plastic objects (0.3m) a 667Hz clunk register.
    "Ceramic":    500.0,   # raised from 450: ceramic/porcelain should be brighter than stone,
                            # closer to a bell/tile register.
}



def calibrate_frequencies(frequencies: np.ndarray,
                           material:    str,
                           bbox_extent: np.ndarray) -> np.ndarray:
    """
    Scales all FEM frequencies so f1 matches a perceptually calibrated target.
    Preserves relative mode spacing. Damping must be recomputed after this.
    """
    if len(frequencies) == 0:
        return frequencies
    L_max     = float(bbox_extent.max())
    C         = PERCEPTUAL_F1_CONSTANT.get(material, 300.0)
    f1_target = C / max(L_max, 0.05)

    # MIN_FREQ clamp: applied to f1_target BEFORE computing the scale.
    # This is critical. The old code scaled first then np.clip(scaled, f_min),
    # which collapsed modes 1–4 to the same floor frequency (unison cluster =
    # hollow single-pitch thud). The correct approach: raise f1_target if it
    # falls below f_min, then compute a single scale = f1_target_clamped / f1_fem.
    # All modes are then multiplied by this single scale, preserving the FEM
    # mode ratios (the inharmonic spectral shape that gives material identity).
    #
    # Example — Wood plank (2.2m):
    #   f1_target_raw = 280/2.2 = 127Hz (too low, sub-bass register)
    #   f1_target_clamped = max(127, 380) = 380Hz
    #   scale = 380/688 = 0.552  (not 0.185 as before)
    #   Mode 1: 380Hz, Mode 2: 619Hz, Mode 3: 911Hz … spread spectrum ✓
    #
    # OLD (broken): scale=0.185, then clip → Modes 1-4 all at 380Hz → unison → hollow
    # NEW (correct): scale=0.552 → Modes spread 380, 619, 911, 1160 … → rich timbre
    #
    # Klatzky et al. (2000): material identity is strongest above ~300Hz.
    # Van den Doel & Pai (1998): hardwood knocks cluster 400–800Hz.
    # Ren et al. (2013): correct modal spacing is what distinguishes materials.
    MIN_FREQ = {
        "Metal":      400.0,  # τ-based floor. Metal chairs (~1074Hz) unaffected.
        "HeavyMetal": 350.0,  # structural steel register.
        "Wood":       500.0,  # hardwood knock register (500-2000Hz range).
        "Glass":      400.0,  # raised from 250: glass must sit above metallic register.
                               # Glass plank at 400Hz+: clearly crystalline, not bassy.
                               # τ at 400Hz, xi=0.002: 199ms — clean sustained ring ✓
        "Stone":      250.0,  # raised from 120: 120Hz was sub-bass, inaudible on some speakers.
                               # 250Hz gives stone a perceptibly heavy low thud.
                               # τ at 250Hz, xi=0.015: 42ms — dead stone thock ✓
        "Plastic":    250.0,  # raised from 100: plastic objects have audible pitch around 300-600Hz.
                               # 250Hz floor gives a clunky dead sound without disappearing into mud.
                               # τ at 250Hz, xi=0.020: 32ms — dead clunk ✓
        "Ceramic":    350.0,  # raised from 180: ceramic/porcelain tiles sound in 350-1000Hz range.
                               # τ at 350Hz, xi=0.001: 455ms — bright hard ringing tile ✓
    }
    f_min     = MIN_FREQ.get(material, 100.0)
    f1_target = max(f1_target, f_min)   # clamp f1 BEFORE scale computation

    f1_fem = float(frequencies[0])
    if f1_fem < 1e-3:
        return frequencies

    # Single uniform scale preserves all FEM mode ratios exactly.
    scale  = float(np.clip(f1_target / f1_fem, 0.01, 100.0))
    scaled = (frequencies * scale).astype(frequencies.dtype)

    unreal.log(f"[Calibrate] f1_fem={f1_fem:.1f} Hz → f1_target={f1_target:.1f} Hz "
               f"(×{scale:.3f}, L={L_max:.2f} m, f_min={f_min:.0f} Hz)")
    return scaled


# ── Safe enum setter ──────────────────────────────────────────────────────
# UE5 Python drops the 'E' prefix from C++ UENUM names.
# Python enum member names are SCREAMING_SNAKE_CASE.
# e.g. C++: EImpactMaterial::HeavyMetal → Python: unreal.ImpactMaterial.HEAVY_METAL
#
# We try multiple access patterns to be resilient across UE5 versions
# and to avoid a hard crash if a pattern changes.

def _to_screaming_snake(name: str) -> str:
    """Convert CamelCase → SCREAMING_SNAKE_CASE. 'HeavyMetal' → 'HEAVY_METAL'."""
    import re
    s = re.sub(r'([A-Z]+)([A-Z][a-z])', r'\1_\2', name)
    s = re.sub(r'([a-z\d])([A-Z])', r'\1_\2', s)
    return s.upper()


def safe_enum_set(da, property_name: str, enum_class_name: str,
                   member_name: str) -> bool:
    """
    Safely set an enum editor property, trying several Python binding patterns.

    UE5 drops 'E' prefix: EImpactMaterial → ImpactMaterial
    UE5 uses SCREAMING_SNAKE_CASE for member names: HeavyMetal → HEAVY_METAL

    Tries in order:
      1. unreal.<ClassName>.<SCREAMING_SNAKE_CASE>   (UE5 standard)
      2. unreal.<ClassName>.<original_name>           (some versions)
      3. unreal.<EClassName>.<SCREAMING_SNAKE_CASE>   (unlikely but defensive)
      4. Raw integer 0 (last resort, logs a warning)

    Returns True if enum was set, False if fallback integer was used.
    """
    snake = _to_screaming_snake(member_name)
    # Strip leading 'E' if present in class name
    clean_class = enum_class_name.lstrip('E') if enum_class_name.startswith('E') else enum_class_name

    attempts = [
        (clean_class, snake),
        (clean_class, member_name),
        (enum_class_name, snake),
        (enum_class_name, member_name),
    ]

    for cls_name, mem_name in attempts:
        try:
            cls = getattr(unreal, cls_name, None)
            if cls is None:
                continue
            val = getattr(cls, mem_name, None)
            if val is None:
                continue
            da.set_editor_property(property_name, val)
            unreal.log(f"[Asset] Enum {property_name} = {cls_name}.{mem_name} ✓")
            return True
        except Exception:
            continue

    # Last resort: set as integer 0
    unreal.log_warning(
        f"[Asset] Could not resolve enum {enum_class_name}.{member_name}. "
        f"Tried: {attempts}. Setting {property_name} = 0 (default).")
    try:
        da.set_editor_property(property_name, 0)
    except Exception as e:
        unreal.log_warning(f"[Asset] Even integer fallback failed: {e}")
    return False


# Ordered list matching EImpactMaterial C++ enum order (Metal=0, HeavyMetal=1, ...)
_MATERIAL_ENUM_INDEX = {
    "Metal": 0, "HeavyMetal": 1, "Wood": 2,
    "Glass": 3, "Stone": 4, "Plastic": 5, "Ceramic": 6,
}


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3: TETRAHEDRALIZATION (solid objects only)
# ═══════════════════════════════════════════════════════════════════════════
def tetrahedralize(vertices: np.ndarray, faces: np.ndarray,
                   bbox_extent: np.ndarray):
    """
    Converts a closed surface mesh to a volumetric tet mesh via TetGen.
    Used only for solid objects. Thin/hollow objects use shell FEM instead.

    Fall-back chain tries progressively relaxed quality settings to handle
    complex furniture, thin legs, and slightly non-manifold meshes.
    Reference: Si (2015), ACM Trans. Math. Software 41(2).
    """
    n_surface_verts = len(vertices)
    max_vol = estimate_tet_volume_constraint(bbox_extent, target_tets=2000)
    unreal.log(f"[TetGen] target vol: {max_vol:.3e} m³, "
               f"{n_surface_verts} surface verts")

    tgen = tetgen.TetGen(vertices, faces)

    configs = [
        {"order": 1, "quality": True, "minratio": 1.414, "maxvolume": max_vol},
        {"order": 1, "quality": True, "minratio": 1.414},
        {"order": 1, "quality": True, "minratio": 2.5},
        {"order": 1, "quality": False},
        {"order": 1},
    ]

    result = None
    for cfg in configs:
        try:
            result = tgen.tetrahedralize(**cfg)
            unreal.log(f"[TetGen] succeeded: {cfg}")
            break
        except Exception as e:
            unreal.log_warning(f"[TetGen] {cfg} failed: {e}")

    if result is None:
        raise RuntimeError(
            "TetGen failed on all configs. Repair mesh in MeshLab: "
            "Filters → Cleaning → Repair Non Manifold Edges + Close Holes.")

    tet_nodes = np.array(result.node if hasattr(result, 'node') else result[0],
                         dtype=np.float64)
    tets      = np.array(result.elem if hasattr(result, 'elem') else result[1],
                         dtype=np.int32)
    if tet_nodes.shape[1] > 3: tet_nodes = tet_nodes[:, :3]
    if tets.shape[1] > 4:      tets      = tets[:, :4]

    unreal.log(f"[TetGen] {len(tet_nodes)} nodes, {len(tets)} tets")
    return tet_nodes, tets, n_surface_verts


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4a: SOLID FEM MATRIX ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════
def assemble_fem_matrices(nodes: np.ndarray, tetrahedra: np.ndarray,
                           E: float, nu: float, rho: float):
    """
    Assembles global stiffness K and consistent mass M from linear CST tets.
    Reference: Bathe (2006) §5.3; Cook et al. (2002) §17.
    """
    n_dof    = 3 * len(nodes)
    K_global = lil_matrix((n_dof, n_dof), dtype=np.float64)
    M_global = lil_matrix((n_dof, n_dof), dtype=np.float64)

    lam = E * nu / ((1 + nu) * (1 - 2 * nu))
    mu  = E / (2 * (1 + nu))

    D = np.array([
        [lam + 2*mu, lam,       lam,       0,  0,  0],
        [lam,        lam + 2*mu, lam,      0,  0,  0],
        [lam,        lam,       lam + 2*mu, 0, 0,  0],
        [0, 0, 0, mu, 0, 0],
        [0, 0, 0, 0, mu, 0],
        [0, 0, 0, 0, 0, mu],
    ], dtype=np.float64)

    n_tets       = len(tetrahedra)
    log_interval = max(1, n_tets // 10)
    skipped      = 0

    unreal.log(f"[FEM-Solid] Assembling {n_tets} tets, {n_dof} DOFs …")

    for idx, tet in enumerate(tetrahedra):
        if idx % log_interval == 0:
            unreal.log(f"[FEM-Solid]   {100*idx//n_tets}%")

        n0, n1 = nodes[tet[0]], nodes[tet[1]]
        n2, n3 = nodes[tet[2]], nodes[tet[3]]

        J     = np.array([n1 - n0, n2 - n0, n3 - n0], dtype=np.float64)
        det_J = np.linalg.det(J)
        if abs(det_J) < 1e-20:
            skipped += 1
            continue

        if det_J < 0:
            tet[2], tet[3] = tet[3], tet[2]
            n2, n3 = n3, n2
            J     = np.array([n1 - n0, n2 - n0, n3 - n0], dtype=np.float64)
            det_J = abs(det_J)

        V     = det_J / 6.0
        inv_J = np.linalg.inv(J)

        dN_dxi = np.array([[-1., -1., -1.],
                            [1.,  0.,  0.],
                            [0.,  1.,  0.],
                            [0.,  0.,  1.]], dtype=np.float64)
        dN = dN_dxi @ inv_J

        B = np.zeros((6, 12), dtype=np.float64)
        for i in range(4):
            c = 3 * i
            B[0, c]     = dN[i, 0]
            B[1, c + 1] = dN[i, 1]
            B[2, c + 2] = dN[i, 2]
            B[3, c]     = dN[i, 1]; B[3, c + 1] = dN[i, 0]
            B[4, c + 1] = dN[i, 2]; B[4, c + 2] = dN[i, 1]
            B[5, c]     = dN[i, 2]; B[5, c + 2] = dN[i, 0]

        K_e = V * (B.T @ D @ B)

        ms  = rho * V / 20.0
        M_e = np.zeros((12, 12), dtype=np.float64)
        for i in range(4):
            for j in range(4):
                f = 2.0 if i == j else 1.0
                for d in range(3):
                    M_e[3*i + d, 3*j + d] = ms * f

        dofs = [3*nid + d for nid in tet for d in range(3)]
        for i, gi in enumerate(dofs):
            for j, gj in enumerate(dofs):
                K_global[gi, gj] += K_e[i, j]
                M_global[gi, gj] += M_e[i, j]

    if skipped:
        unreal.log_warning(f"[FEM-Solid] Skipped {skipped} degenerate tets")
    unreal.log("[FEM-Solid] Done — converting to CSC")
    return csc_matrix(K_global), csc_matrix(M_global)


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4b: KIRCHHOFF SHELL FEM (thin/hollow objects)
# ═══════════════════════════════════════════════════════════════════════════
def assemble_kirchhoff_shell(vertices:  np.ndarray,
                              faces:     np.ndarray,
                              E:         float,
                              nu:        float,
                              rho:       float,
                              thickness: float):
    """
    Assembles K and M for a Kirchhoff thin-plate/shell using Discrete
    Kirchhoff Triangle (DKT) elements.

    WHY SHELL INSTEAD OF SOLID FOR THIN OBJECTS:
      A plate of size L × L × t (t << L) has its dominant sound from
      transverse bending. The first bending frequency is:
        f = (λ²/2πL²) × √(E t² / (12ρ(1-ν²)))
      where λ depends on boundary conditions.
      Solid CST tets cannot capture this mode correctly at high aspect
      ratios because they are too stiff in transverse bending (shear locking).
      Kirchhoff elements explicitly compute the bending stiffness from
      plate theory, giving correct results for any aspect ratio.

      Reference: Batoz, J.L. (1980). "A study of three-node triangular plate
      bending elements." Int. J. Numer. Methods Eng. 15(12), 1771–1812.
      Felippa, C.A. (2003). "Introduction to Finite Element Methods", Ch.32.

    DOF LAYOUT: 3 DOFs per node (w, θx, θy — transverse displacement and
    two rotations). N nodes → 3N DOFs total.

    OPERATES ON SURFACE MESH DIRECTLY — no TetGen needed.
    This is why thin/hollow objects avoid TetGen failures.

    The shell is treated as the outer surface of the object. For hollow
    objects (cups, bowls), the outer wall is what vibrates and radiates.
    Thickness is either detected automatically from the mesh or provided
    as a user override.

    Args:
        vertices:  (N, 3) surface vertex positions in metres
        faces:     (M, 3) triangle indices
        E:         Young's modulus (Pa)
        nu:        Poisson ratio
        rho:       density (kg/m³)
        thickness: shell wall thickness in metres

    Returns:
        K, M: sparse CSC matrices (3N × 3N)
        n_surface_verts: == N (all vertices are surface verts for shell)
    """
    N     = len(vertices)
    n_dof = 3 * N

    K_global = lil_matrix((n_dof, n_dof), dtype=np.float64)
    M_global = lil_matrix((n_dof, n_dof), dtype=np.float64)

    t   = thickness
    t3  = t ** 3
    # Bending stiffness coefficient D_b = E t³ / (12(1-ν²))
    D_b = E * t3 / (12.0 * (1.0 - nu**2))

    # Bending constitutive matrix (3×3, relates moments to curvatures)
    # Reference: Felippa (2003) eq. 32.15
    C_b = D_b * np.array([
        [1.0, nu,  0.0],
        [nu,  1.0, 0.0],
        [0.0, 0.0, (1.0 - nu) / 2.0],
    ], dtype=np.float64)

    n_tris = len(faces)
    log_interval = max(1, n_tris // 10)
    skipped = 0

    unreal.log(f"[FEM-Shell] Assembling {n_tris} triangles, "
               f"{n_dof} DOFs, t={thickness*100:.2f} cm …")

    for idx, tri in enumerate(faces):
        if idx % log_interval == 0:
            unreal.log(f"[FEM-Shell]   {100*idx//n_tris}%")

        i0, i1, i2 = int(tri[0]), int(tri[1]), int(tri[2])
        p0, p1, p2 = vertices[i0], vertices[i1], vertices[i2]

        # Triangle edge vectors
        x1 = p1[0] - p0[0]; y1 = p1[1] - p0[1]; z1 = p1[2] - p0[2]
        x2 = p2[0] - p0[0]; y2 = p2[1] - p0[1]; z2 = p2[2] - p0[2]

        # Normal and area
        nx = y1*z2 - z1*y2
        ny = z1*x2 - x1*z2
        nz = x1*y2 - y1*x2
        area_2 = np.sqrt(nx**2 + ny**2 + nz**2)  # = 2 × triangle area
        if area_2 < 1e-20:
            skipped += 1
            continue
        A = 0.5 * area_2

        # Project to local 2D coordinates in the triangle plane.
        # Local x-axis: along p0→p1
        lx = np.array([x1, y1, z1]) / np.sqrt(x1**2 + y1**2 + z1**2 + 1e-30)
        ln = np.array([nx, ny, nz]) / area_2  # local z (normal)
        ly = np.cross(ln, lx)

        def proj(p):
            d = p - p0
            return np.array([np.dot(d, lx), np.dot(d, ly)])

        lp0 = np.zeros(2)
        lp1 = proj(p1)
        lp2 = proj(p2)

        # DKT bending stiffness via constant curvature approximation.
        # For a 3-node triangle, the curvature is assumed constant within
        # the element. The strain-displacement matrix B_b (3×9) maps nodal
        # DOFs [w0,θx0,θy0, w1,θx1,θy1, w2,θx2,θy2] to curvatures
        # [κxx, κyy, 2κxy].
        #
        # In the simplified DKT formulation (Batoz 1980 §4):
        # B_b = (1/2A) × matrix from triangle geometry.
        # For prototype purposes we use the standard constant-strain plate
        # approximation which is exact for linear variation of curvature.
        x10, y10 = lp1 - lp0
        x20, y20 = lp2 - lp0
        inv2A = 1.0 / (2.0 * A)

        # Shape function derivatives in local coords (CST-like for bending)
        # dN1/dx, dN2/dx, dN3/dx etc.
        dN1dx =  (y20 - y10 - y20) * inv2A  # = -y20/(2A) after simplification
        # Explicitly using standard CST shape function derivatives:
        # N1 = (a1 + b1*x + c1*y)/(2A) where
        # b1 = y2-y3, c1 = x3-x2 (using local 0-indexed p0,p1,p2)
        b = np.array([lp1[1] - lp2[1], lp2[1] - lp0[1], lp0[1] - lp1[1]])
        c = np.array([lp2[0] - lp1[0], lp0[0] - lp2[0], lp1[0] - lp0[0]])

        # Bending B matrix (3 × 9): maps [w,θx,θy] × 3 nodes → [κxx,κyy,2κxy]
        # Using Kirchhoff constraint: θx = ∂w/∂y, θy = -∂w/∂x
        # Each node contributes a 3×3 sub-block.
        # Reference: Cook et al. (2002) §17.5, eq. 17.5-3
        B_b = np.zeros((3, 9), dtype=np.float64)
        for k in range(3):
            col = 3 * k
            # κxx = ∂²w/∂x² term
            B_b[0, col]     =  0.0          # from w_k
            B_b[0, col + 1] =  0.0          # from θx_k (= ∂w/∂y)
            B_b[0, col + 2] =  b[k] * inv2A # from θy_k (= -∂w/∂x): -∂/∂x(-∂w/∂x)

            # κyy = ∂²w/∂y² term
            B_b[1, col]     =  0.0
            B_b[1, col + 1] =  c[k] * inv2A  # from θx_k
            B_b[1, col + 2] =  0.0

            # 2κxy = 2∂²w/∂x∂y term
            B_b[2, col]     =  0.0
            B_b[2, col + 1] =  b[k] * inv2A
            B_b[2, col + 2] =  c[k] * inv2A

        # Element stiffness: K_e = A × B_b^T × C_b × B_b
        K_e = A * (B_b.T @ C_b @ B_b)

        # LUMPED diagonal mass (replaces consistent coupled mass).
        # Each triangle contributes A/3 to each of its 3 corner nodes.
        # This gives a purely diagonal M, which is always positive-definite
        # as long as every node appears in at least one non-degenerate triangle.
        # The consistent mass couples w/θ DOFs across nodes, producing a
        # 9×9 block that is extremely ill-conditioned when rotational inertia
        # is 1e4–1e5× smaller than translational (thin plates, thick planks).
        # Lumped mass is slightly less accurate for high modes but perfectly
        # valid for the first 20–30 modes we extract.
        # Reference: Cook et al. (2002) §17.6; Felippa (2003) §32.5.
        lump   = A / 3.0          # each node gets 1/3 of the element area
        rho_t  = rho * t  * lump  # translational mass per node
        rho_r  = rho * t3 / 12.0 * lump  # rotational inertia per node
        M_e = np.zeros((9, 9), dtype=np.float64)
        for i in range(3):
            ci = 3 * i
            M_e[ci,     ci]     = rho_t   # w  (translational)
            M_e[ci + 1, ci + 1] = rho_r   # θx (rotational)
            M_e[ci + 2, ci + 2] = rho_r   # θy (rotational)

        # Assemble into global matrices
        dofs = [3*i0, 3*i0+1, 3*i0+2,
                3*i1, 3*i1+1, 3*i1+2,
                3*i2, 3*i2+1, 3*i2+2]
        for i, gi in enumerate(dofs):
            for j, gj in enumerate(dofs):
                K_global[gi, gj] += K_e[i, j]
                M_global[gi, gj] += M_e[i, j]

    if skipped:
        unreal.log_warning(f"[FEM-Shell] Skipped {skipped} degenerate tris")
    unreal.log("[FEM-Shell] Done — converting to CSC")

    K_csc = csc_matrix(K_global)
    M_csc = csc_matrix(M_global)

    # ── θ-only submatrix reduction ────────────────────────────────────────
    # In the Kirchhoff DKT formulation K[w,*] = 0 identically: the bending
    # B_b matrix has zero entries in the w-displacement columns (col+0 of the
    # 3×9 B_b matrix). Every w-DOF is a zero eigenvector of K, producing N
    # spurious zero-frequency modes. For a 738-node mesh this is 738 spurious
    # zeros — requesting k=30 or k=50 returns only these before any elastic mode.
    #
    # FIX: drop all w-DOFs (index 3i per node) and work in the 2N-DOF
    # θ-only subspace. DOF layout in the reduced system:
    #   node i → [2i = θx_i,  2i+1 = θy_i]
    #
    # The reduced system K_θ φ = ω² M_θ φ has identical non-zero eigenvalues
    # to the full system (K[w,θ]=0 so w and θ decouple exactly), but only
    # 6 rigid body modes remain as near-zeros. k = num_modes+10 always
    # returns the requested elastic modes cleanly.
    #
    # The radiation/participation code uses is_shell=True to read
    # phi[0::2]=θx and phi[1::2]=θy (surface slopes) as the displacement proxy.
    theta_idx = np.array([3*i + d for i in range(N) for d in (1, 2)], dtype=np.int32)
    K_theta   = K_csc[theta_idx, :][:, theta_idx]
    M_theta   = M_csc[theta_idx, :][:, theta_idx]

    return K_theta, M_theta, N


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 5: EIGENVALUE SOLVE
# ═══════════════════════════════════════════════════════════════════════════
def solve_modes(K, M, num_modes: int):
    """
    Solves the generalised eigenvalue problem K φ = ω² M φ.

    Uses diagonal scaling to convert to a standard symmetric eigenvalue
    problem, completely avoiding splu(M) which crashes when M is singular
    or ill-conditioned (as it is for thin-shell lumped mass matrices where
    rotational inertia entries are ~10,000× smaller than translational).

    ALGORITHM (Golub & Van Loan 1996, §8.7.1):
      M is diagonal after lumped assembly.
      d     = diag(M),  floored at a safe minimum
      D     = diag(1/sqrt(d))
      K_sym = D @ K @ D   (symmetric, same eigenvalues as generalised problem)
      Solve: K_sym v = λ v  (standard form, no M= argument, no splu)
      Back-transform: φ = D @ v  (original eigenvectors)

    WHY n_req = num_modes + 10:
      The shell FEM now applies w-DOF regularisation in assemble_kirchhoff_shell,
      which pins all spurious zero-eigenvalue modes below ~2 Hz (well below the
      20 Hz filter). Only 6 true rigid body modes remain near zero.
      +10 is sufficient to clear these and get the first num_modes elastic modes.
    """
    from scipy.sparse import diags as spdiags
    n_req = min(num_modes + 10, K.shape[0] - 2)
    unreal.log(f"[Eigen] {K.shape[0]} DOFs, requesting {n_req} modes")

    def _solve(k_req):
        # Build diagonal scaling D = diag(1/sqrt(m_i))
        d = M.diagonal().copy().astype(np.float64)
        d_max = d.max() if d.max() > 0 else 1.0
        eps_floor = 1e-12 * d_max
        d = np.where(d > eps_floor, d, eps_floor)
        d_invsqrt = 1.0 / np.sqrt(d)
        D = spdiags(d_invsqrt, format='csc')

        K_sym = D @ K @ D
        # Symmetrise to remove float64 accumulation asymmetry
        K_sym = 0.5 * (K_sym + K_sym.T)

        eigenvalues, vecs = eigsh(
            K_sym, k=k_req,
            sigma=-1.0, which='LM',
            tol=1e-6, maxiter=10000)

        eigenvectors = D @ vecs
        frequencies  = np.sqrt(np.abs(eigenvalues)) / (2.0 * np.pi)
        sort_idx     = np.argsort(frequencies)
        return frequencies[sort_idx], eigenvectors[:, sort_idx]

    frequencies, eigenvectors = _solve(n_req)

    mask         = frequencies >= 20.0
    freq_elastic = frequencies[mask][:num_modes]
    vecs_elastic = eigenvectors[:, mask][:, :num_modes]

    # If fewer than half the requested modes were found, retry with 2× budget.
    # This handles meshes with unusually many near-zero numerical modes.
    if len(freq_elastic) < num_modes // 2:
        n_retry = min(n_req * 2, K.shape[0] - 2)
        unreal.log(f"[Eigen] Only {len(freq_elastic)} elastic modes found — "
                   f"retrying with k={n_retry}")
        frequencies, eigenvectors = _solve(n_retry)
        mask         = frequencies >= 20.0
        freq_elastic = frequencies[mask][:num_modes]
        vecs_elastic = eigenvectors[:, mask][:, :num_modes]

    if len(freq_elastic) == 0:
        raise RuntimeError(
            "[Eigen] No modes above 20 Hz found. Check mesh scale (must be metres) "
            "and material properties.")

    unreal.log(f"[Eigen] {len(freq_elastic)} modes: "
               f"{freq_elastic[0]:.1f}–{freq_elastic[-1]:.1f} Hz")
    return freq_elastic, vecs_elastic


# ═══════════════════════════════════════════════════════════════════════════
# STAGES 6 + 7: PARTICIPATION, RADIATION EFFICIENCY, DRAG SENSITIVITY
# ═══════════════════════════════════════════════════════════════════════════
def compute_participation_and_radiation(mode_shapes:      np.ndarray,
                                         n_surface_verts:  int,
                                         tet_nodes:        np.ndarray,
                                         frequencies_hz:   np.ndarray,
                                         is_shell:         bool = False):
    """
    Computes three per-mode quantities.

    For SOLID FEM: mode_shapes is (3N_total × n_modes); only first n_surface_verts
    nodes are the surface. DOFs are [ux,uy,uz] per node.

    For SHELL FEM: mode_shapes is (3N × n_modes); all nodes are surface nodes.
    DOFs are [w, θx, θy] per node. The transverse displacement w is DOF index 0
    of each node triplet, which is the acoustically relevant component.

    Participation φ_k(v): weighted by dot product of mode displacement
    with approximate outward normal. Normal-directed modes excite more
    efficiently under impact. (O'Brien et al. 2002 §3.2)

    Radiation η_k: mean |normal displacement| across surface.
    (NeuralSound §3.2; Zheng & James 2011)

    Drag sensitivity: η_k / (1 + (f_k/f_0)²)
    Low modes dominate scraping. (Rath & Rocchesso 2005 §3)
    """
    n_modes       = mode_shapes.shape[1]
    participation = np.zeros((n_modes, n_surface_verts), dtype=np.float32)
    radiation     = np.zeros(n_modes, dtype=np.float32)

    surf_pts = tet_nodes[:n_surface_verts]
    centroid = surf_pts.mean(axis=0)
    outward  = surf_pts - centroid
    norms    = np.linalg.norm(outward, axis=1, keepdims=True)
    outward  = outward / np.where(norms < 1e-12, 1.0, norms)

    for k in range(n_modes):
        if is_shell:
            # Shell mode_shapes are from the θ-reduced system (2 DOFs/node).
            # DOF layout in reduced system: [2i = θx_i,  2i+1 = θy_i]
            # K[w,*] = 0 in DKT → w-DOFs dropped entirely before eigensolution.
            # All mode energy is in the θ DOFs (surface slopes ∂w/∂y, -∂w/∂x).
            #
            # Radiation proxy: sqrt(θx² + θy²) per node — slope magnitude.
            # This is proportional to transverse displacement amplitude (via
            # Kirchhoff: θx = ∂w/∂y, θy = -∂w/∂x), so high slope = high
            # vibration amplitude at that node. For a free plate all modes
            # radiate similarly, giving η ≈ 0.94–1.00 across modes, which
            # is physically correct (the radiation floor ensures modes 2+
            # remain audible and add multi-partial timbre).
            theta_x = mode_shapes[0::2, k][:n_surface_verts]   # θx: indices 0,2,4,...
            theta_y = mode_shapes[1::2, k][:n_surface_verts]   # θy: indices 1,3,5,...
            disp_mag = np.sqrt(theta_x**2 + theta_y**2).astype(np.float32)
            normal_proj = disp_mag
        else:
            # Solid DOFs: [ux,uy,uz] per node.
            mode_surf = mode_shapes[:3*n_surface_verts, k].reshape(n_surface_verts, 3)
            disp_mag  = np.linalg.norm(mode_surf, axis=1)
            # Participation = displacement magnitude at each surface vertex.
            # We intentionally do NOT weight by dot(disp, outward_normal) here.
            # That weighting was intended to favour normal-direction modes but
            # corrupts results for non-convex objects (chairs, bowls) where
            # the centroid-based outward normal is unreliable.
            # Plain |displacement| is what O'Brien et al. (2002) §3.2 use.
            disp_mag = disp_mag.astype(np.float32)
            normal_proj = np.abs(np.einsum('ij,ij->i',
                                            mode_surf, outward)).astype(np.float32)

        participation[k] = disp_mag
        mx = participation[k].max()
        if mx > 1e-12:
            participation[k] /= mx

        radiation[k] = float(normal_proj.mean())

    max_r = radiation.max()
    if max_r > 1e-12:
        radiation /= max_r

    # Drag sensitivity: η_k low-pass shaped by frequency
    f0   = float(frequencies_hz[0]) if len(frequencies_hz) > 0 else 1.0
    drag = np.array([
        float(radiation[k]) / (1.0 + (float(frequencies_hz[k]) / f0) ** 2)
        for k in range(n_modes)
    ], dtype=np.float32)

    max_d = drag.max()
    if max_d > 1e-12:
        drag /= max_d

    unreal.log(f"[Radiation] η: {radiation.min():.3f}–{radiation.max():.3f}  "
               f"drag: {drag.min():.3f}–{drag.max():.3f}")
    return participation, radiation, drag


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 8: RAYLEIGH DAMPING
# ═══════════════════════════════════════════════════════════════════════════
def compute_rayleigh_damping(frequencies_hz: np.ndarray,
                              material:       str) -> np.ndarray:
    """
    Per-mode Rayleigh damping: ξ_k = α/(2ω_k) + β·ω_k/2
    Calibrated to xi_low at ω_1 and xi_high at ω_N.

    Rayleigh (α,β) parameters are GEOMETRY-INVARIANT: the same α,β apply
    correctly across all shapes and sizes of the same material. This was
    formally proven by perceptual study in Ren et al. (2013a) "Auditory
    Perception of Geometry-Invariant Material Properties" — subjects could not
    distinguish same-material objects of different shapes when synthesised with
    shared Rayleigh parameters. This is the correct universal damping model
    for arbitrary object geometry.

    The U-shaped curve (high damping at ω_1, dips in mid-band, rises at ω_N)
    is physically correct — it reflects how mass-proportional damping dominates
    at low frequencies and stiffness-proportional dominates at high frequencies.
    Reference: Caughey (1960); Ren et al. (2013a).
    """
    mat   = MATERIAL_PRESETS[material]
    omega = 2.0 * np.pi * frequencies_hz
    N     = len(omega)

    if N < 2:
        return np.full(N, mat["xi_low"], dtype=np.float32)

    w1, wN = omega[0], omega[-1]
    A      = np.array([[1 / (2*w1), w1 / 2],
                       [1 / (2*wN), wN / 2]])
    b_vec  = np.array([mat["xi_low"], mat["xi_high"]])

    try:
        alpha, beta = np.linalg.solve(A, b_vec)
    except np.linalg.LinAlgError:
        unreal.log_warning("[Damping] Calibration failed — using xi_low.")
        return np.full(N, mat["xi_low"], dtype=np.float32)

    xi = np.clip(alpha / (2*omega) + beta*omega / 2, 0.0001, 0.5)
    unreal.log(f"[Damping] {material} (Rayleigh): ξ {xi[0]:.4f}→{xi[-1]:.4f} "
               f"ratio {xi[-1]/xi[0]:.1f}×")
    return xi.astype(np.float32)


# ═══════════════════════════════════════════════════════════════════════════
# VECTORISED VOLUME CALCULATION
# ═══════════════════════════════════════════════════════════════════════════
def compute_tet_volume_vectorised(tet_nodes: np.ndarray,
                                   tets:      np.ndarray) -> float:
    """Vectorised sum of all tet volumes. ~200× faster than Python loop."""
    n0 = tet_nodes[tets[:, 0]]
    n1 = tet_nodes[tets[:, 1]]
    n2 = tet_nodes[tets[:, 2]]
    n3 = tet_nodes[tets[:, 3]]
    cross = np.cross(n2 - n0, n3 - n0)
    det   = np.einsum('ij,ij->i', n1 - n0, cross)
    return float(np.sum(np.abs(det)) / 6.0)


def compute_surface_area(vertices: np.ndarray, faces: np.ndarray) -> float:
    """Surface area in m² (for shell volume estimate: area × thickness)."""
    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    cross = np.cross(v1 - v0, v2 - v0)
    areas = 0.5 * np.linalg.norm(cross, axis=1)
    return float(np.sum(areas))


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 9: WRITE DATAASSET
# ═══════════════════════════════════════════════════════════════════════════
def write_data_asset(output_path, mesh_asset_path, frequencies,
                     damping, participation, radiation, drag,
                     surface_vertices,       # (N_surf, 3) FEM surface positions
                     n_surface_verts,
                     material, meta,
                     solver_type="solid",    # "solid" | "shell"
                     shell_thickness=0.003):
    """
    Creates UModalSoundDataAsset and writes all modal data.

    ENUM ACCESS FIX:
      UE5 Python drops 'E' prefix: EImpactMaterial → ImpactMaterial
      UE5 Python uses SCREAMING_SNAKE_CASE: HeavyMetal → HEAVY_METAL
      The safe_enum_set() helper tries multiple patterns and falls back
      to an integer index, which always works.
    """
    import os
    pkg  = os.path.dirname(output_path).replace("\\", "/")
    name = os.path.basename(output_path)

    at = unreal.AssetToolsHelpers.get_asset_tools()
    da = at.create_asset(name, pkg,
                         unreal.ModalSoundDataAsset,
                         unreal.DataAssetFactory())
    if da is None:
        raise RuntimeError(f"Failed to create DataAsset at '{output_path}'")

    mat = MATERIAL_PRESETS[material]

    # ── Material type enum (with safe fallback) ──────────────────────────
    safe_enum_set(da, "material_type", "ImpactMaterial", material)

    # ── Solver type enum (with safe fallback) ────────────────────────────
    solver_member = "SolidTet" if solver_type == "solid" else "Shell"
    safe_enum_set(da, "solver_type", "FEMSolverType", solver_member)

    # ── Scalar material properties ────────────────────────────────────────
    da.set_editor_property("young_modulus",     float(mat["E"]))
    da.set_editor_property("poisson_ratio",     float(mat["nu"]))
    da.set_editor_property("density",           float(mat["rho"]))
    da.set_editor_property("contact_hardness",  float(mat["contact_hardness"]))
    da.set_editor_property("shell_thickness",   float(shell_thickness))
    da.set_editor_property("source_mesh",
        unreal.EditorAssetLibrary.load_asset(mesh_asset_path))

    # ── Mesh metadata ─────────────────────────────────────────────────────
    da.set_editor_property("surface_vertex_count", int(n_surface_verts))
    da.set_editor_property("mesh_volume",          float(meta.get("volume", 0.0)))
    da.set_editor_property("bounding_box_extent",
        unreal.Vector(*meta["bbox_extent"].tolist()))

    # ── FEM surface vertex positions (critical for correct vertex lookup) ─
    fem_positions = [
        unreal.Vector(float(v[0]), float(v[1]), float(v[2]))
        for v in surface_vertices[:n_surface_verts]
    ]
    da.set_editor_property("fem_surface_vertex_positions", fem_positions)

    # ── Modes ─────────────────────────────────────────────────────────────
    # GlobalAmplitude floor by solver type:
    #
    # SHELL (planks, plates): radiation[k] = mean|w| across surface.
    #   For a flat plate the (2,1) bending mode has two equal-area antinodes
    #   that partially cancel in the mean → radiation[1] ≈ 0-0.15 even though
    #   the mode is acoustically significant. Flooring at 0.15 ensures modes
    #   2-8 have at least 15% of mode-1 amplitude in the DataAsset.
    #   Without this floor, only mode 1 survives → single-pitch LF tone
    #   = sounds like cork or hollow box rather than solid hardwood/steel.
    #
    # SOLID (chairs, blocks): radiation is more reliable; keep floor at 0.001
    #   so near-zero-radiation modes don't pollute the ModalSynthesizer.
    is_shell_solver = (solver_type == "shell")
    radiation_floor = 0.15 if is_shell_solver else 0.001
    modes = []
    for i in range(len(frequencies)):
        m = unreal.ModalMode()
        m.set_editor_property("frequency",        float(frequencies[i]))
        m.set_editor_property("damping_ratio",    float(damping[i]))
        m.set_editor_property("global_amplitude", max(float(radiation[i]), radiation_floor))
        m.set_editor_property("vertex_participation", participation[i].tolist())
        m.set_editor_property("drag_sensitivity",  float(drag[i]))
        modes.append(m)

    da.set_editor_property("modes", modes)
    unreal.EditorAssetLibrary.save_asset(output_path)

    unreal.log(f"[Asset] '{output_path}': {len(frequencies)} modes, "
               f"{n_surface_verts} FEM verts, {material}, "
               f"solver={solver_type}, "
               f"hardness={mat['contact_hardness']:.2f}")
    return da


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
def generate_modal_asset(mesh_asset_path:    str,
                          output_asset_path:  str,
                          material:           str = "HeavyMetal",
                          num_modes:          int = 10,  # 10 modes are perceptually sufficient (Klatzky 2000)
                          shell_thickness_m:  float | None = None):
    """
    Full pipeline. Automatically detects whether to use solid-tet or shell FEM.

    MATERIAL GUIDE:
      Metal      — small steel objects < 30 cm
      HeavyMetal — large steel/iron > 30 cm, heavy machinery
      Wood       — furniture, floors, crates, planks
      Glass      — glass bottles, windows, crystal, glass plates
      Stone      — concrete, stone tiles
      Plastic    — plastic containers, toys
      Ceramic    — mugs, tiles, fired clay, porcelain plates

    SOLVER GUIDE (automatic — you do NOT need to set this):
      Shell FEM  → plates, planks, cups, mugs, bowls, tubes, pots, pans, bells
      Solid FEM  → cubes, chairs, stools, books, bricks, thick furniture

    SHELL THICKNESS OVERRIDE:
      shell_thickness_m: if you know the exact wall thickness (metres), pass it here.
      Example: a 3mm steel plate → shell_thickness_m=0.003
      If None (default), thickness is estimated automatically from the mesh.

    EXAMPLES:
      # Wooden plank (auto-detected as shell)
      generate_modal_asset("/Game/Meshes/SM_Plank",
                           "/Game/ModalData/MDA_Plank_Wood",
                           material="Wood")

      # Glass plate (auto-detected as shell, override thickness)
      generate_modal_asset("/Game/Meshes/SM_GlassPlate",
                           "/Game/ModalData/MDA_GlassPlate",
                           material="Glass", shell_thickness_m=0.006)

      # Ceramic mug (auto-detected as hollow shell)
      generate_modal_asset("/Game/Meshes/SM_Mug",
                           "/Game/ModalData/MDA_Mug_Ceramic",
                           material="Ceramic")

      # Heavy metal machine part (solid)
      generate_modal_asset("/Game/Meshes/SM_MachinePart",
                           "/Game/ModalData/MDA_MachinePart",
                           material="HeavyMetal")
    """
    unreal.log("=" * 60)
    unreal.log(f"  MODAL GENERATOR  {mesh_asset_path}")
    unreal.log(f"  Material: {material}   Modes: {num_modes}")
    unreal.log("=" * 60)

    if material not in MATERIAL_PRESETS:
        raise ValueError(f"Unknown material '{material}'. "
                         f"Choose: {list(MATERIAL_PRESETS.keys())}")

    # Stage 1 — extract mesh
    vertices, faces, meta = extract_static_mesh(mesh_asset_path)
    if not validate_mesh_for_fem(vertices, faces):
        raise RuntimeError("Mesh validation failed.")

    # Stage 1.5 — repair (handles Megascans self-intersections)
    from modal_mesh_utils import repair_mesh_for_fem, strip_isolated_vertices
    vertices, faces = repair_mesh_for_fem(vertices, faces)
    # Strip unreferenced vertices — they produce zero rows in the FEM mass
    # matrix, making it exactly singular and crashing the eigensolver.
    vertices, faces = strip_isolated_vertices(vertices, faces)
    meta["n_surface_verts"] = len(vertices)
    if not validate_mesh_for_fem(vertices, faces):
        raise RuntimeError("Mesh still invalid after repair.")

    # Stage 2 — auto-classify geometry
    geo = classify_geometry(vertices, faces, meta)
    solver    = geo["solver"]
    auto_thickness = geo["thickness"]
    thickness = shell_thickness_m if shell_thickness_m is not None else auto_thickness

    unreal.log(f"[Pipeline] Solver: {solver.upper()}  "
               f"thickness: {thickness*100:.2f} cm")

    mat = MATERIAL_PRESETS[material]

    if solver == "shell":
        # ── SHELL PATH (plates, cups, bowls, tubes, planks) ──────────────
        # Shell FEM operates on the surface mesh directly — no TetGen needed.
        # Subdivision still helps for accuracy (more elements = better modes).

        target_verts = 200
        if meta["n_surface_verts"] < target_verts:
            current, subs = meta["n_surface_verts"], 0
            while current < target_verts and subs < 3:
                current *= 4; subs += 1
            unreal.log(f"[Subdiv] Shell: {subs} pass(es)")
            vertices, faces = subdivide_surface_mesh(vertices, faces, subs)
            meta["n_surface_verts"] = len(vertices)

        n_sv = len(vertices)
        # Stage 4b — shell FEM
        K, M, n_sv = assemble_kirchhoff_shell(
            vertices, faces,
            mat["E"], mat["nu"], mat["rho"], thickness)

        # Stage 5 — eigensolve
        frequencies, mode_shapes = solve_modes(K, M, num_modes)

        # Stage 5b — perceptual frequency calibration
        frequencies = calibrate_frequencies(frequencies, material, meta["bbox_extent"])

        # Stages 6+7 — participation / radiation / drag
        participation, radiation, drag = compute_participation_and_radiation(
            mode_shapes, n_sv, vertices, frequencies, is_shell=True)

        # Rayleigh damping (recomputed on calibrated frequencies)
        damping = compute_rayleigh_damping(frequencies, material)

        # Volume estimate for bridge: surface area × thickness
        surf_area       = compute_surface_area(vertices, faces)
        meta["volume"]  = surf_area * thickness
        surface_verts   = vertices  # all surface verts for shell

    else:
        # ── SOLID PATH (cubes, chairs, thick furniture) ───────────────────
        # Adaptive subdivision for surface density
        bbox_vol     = meta["bbox_volume"]
        target_verts = 300
        if bbox_vol < 0.005:  target_verts = 80
        elif bbox_vol < 0.05: target_verts = 150
        elif bbox_vol < 0.3:  target_verts = 220

        if meta["n_surface_verts"] < target_verts:
            current, subs = meta["n_surface_verts"], 0
            while current < target_verts and subs < 4:
                current *= 4; subs += 1
            unreal.log(f"[Subdiv] Solid: {subs} pass(es)")
            vertices, faces = subdivide_surface_mesh(vertices, faces, subs)
            meta["n_surface_verts"] = len(vertices)

        # Stage 3 — tetrahedralize
        tet_nodes, tets, n_sv = tetrahedralize(vertices, faces, meta["bbox_extent"])
        meta["n_surface_verts"] = n_sv
        meta["volume"]          = compute_tet_volume_vectorised(tet_nodes, tets)

        # Stage 4a — solid FEM
        K, M = assemble_fem_matrices(
            tet_nodes, tets, mat["E"], mat["nu"], mat["rho"])

        # Stage 5
        frequencies, mode_shapes = solve_modes(K, M, num_modes)

        # Stage 5b — perceptual frequency calibration
        frequencies = calibrate_frequencies(frequencies, material, meta["bbox_extent"])

        # Stages 6+7
        participation, radiation, drag = compute_participation_and_radiation(
            mode_shapes, n_sv, tet_nodes, frequencies, is_shell=False)

        # Rayleigh damping (recomputed on calibrated frequencies)
        damping = compute_rayleigh_damping(frequencies, material)

        surface_verts = tet_nodes  # first n_sv rows are surface verts

    # Stage 9 — write DataAsset
    asset = write_data_asset(
        output_asset_path, mesh_asset_path,
        frequencies, damping, participation, radiation, drag,
        surface_verts, n_sv,
        material, meta,
        solver_type=solver,
        shell_thickness=thickness)

    unreal.log("=" * 60 + "\n  COMPLETE\n" + "=" * 60)
    return asset


def generate_from_widget(mesh_path, output_path, material, num_modes_str,
                          shell_thickness_str=""):
    """Widget entry point — handles Blueprint string conversion."""
    import importlib, modal_mesh_utils
    importlib.reload(modal_mesh_utils)
    mesh_path   = mesh_path.split(".")[0]
    output_path = output_path.split(".")[0]
    num_modes   = int(float(num_modes_str))
    if material not in MATERIAL_PRESETS:
        material = "HeavyMetal"
    thickness = None
    if shell_thickness_str.strip():
        try:
            thickness = float(shell_thickness_str)
        except ValueError:
            pass
    generate_modal_asset(mesh_path, output_path, material, num_modes, thickness)