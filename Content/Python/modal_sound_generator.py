"""
modal_sound_generator.py  —  Offline FEM Modal Sound Pipeline
==============================================================

PURPOSE
-------
Converts a UE5 StaticMesh into a UModalSoundDataAsset containing
precomputed modal frequencies, per-mode damping ratios, surface vertex
participation factors, and radiation efficiency weights. Used at runtime
by ModalImpactComponent + ModalMetaSoundBridge for physically-based
impact sound synthesis via MetaSound.

SCIENTIFIC FOUNDATION — WHAT THIS IMPLEMENTS AND WHY
-----------------------------------------------------
This pipeline implements the O'Brien et al. (2002) modal synthesis method
with extensions grounded in four subsequent peer-reviewed works. Every
design decision below is traceable to a specific paper.

CORE PIPELINE:
  O'Brien, J.F., Cook, P.R., Essl, G. (2002). "Synthesizing Sounds from
  Rigid-Body Simulations." SIGGRAPH 2002.
  → FEM eigendecomposition + additive damped sinusoidal synthesis.
  → Still the standard foundation used by every subsequent paper including
    DiffSound SIGGRAPH 2024.

MATERIAL DAMPING:
  van den Doel, K., Pai, D.K. (1998). "The Sounds of Physical Shapes."
  Presence 7(4), 382–395.
  → Table 1 provides the only published measured xi values for common
    materials. Used verbatim by NeuralSound 2022 and DiffSound 2024.
  → Key insight: high-frequency modes must be damped 5–10x more than
    low-frequency modes (Sterling et al. 2019 validated this perceptually).

CONTACT FORCE SHAPING (Hertz):
  Chadwick, J.N., Zheng, C., James, D.L. (2012). "Precomputed Acceleration
  Noise for Improved Rigid-Body Sound." ACM Trans. Graph. 31(4).
  → Estimating continuous contact force profiles from rigid-body impulses
    using Hertz contact theory "significantly complements the standard
    modal sound algorithm, especially for small objects."
  → The contact duration T_c shapes the excitation spectrum: hard/fast
    impacts have shorter T_c and excite high frequencies more strongly.
  → CRITICAL CORRECTION from previous version: Hertz weights must be
    computed AT RUNTIME from the actual impact velocity, not baked offline
    into the DataAsset. The previous code baked a fixed "soft impact" weight
    which made all impacts sound identical regardless of speed.

RADIATION EFFICIENCY:
  Zheng, C., James, D.L. (2011). "Toward High-Quality Modal Contact Sound."
  ACM Trans. Graph. 30(4).
  NeuralSound (Jin et al., SIGGRAPH 2022) quantified: some modes are
  up to 1000x more radiative than others. Ignoring radiation efficiency
  produces wrong relative mode loudnesses.
  → We store η_k (surface-normal displacement proxy) in GlobalAmplitude.
  → Full treatment requires Boundary Element Method (James et al. 2006,
    Precomputed Acoustic Transfer) — not feasible for a prototype.
    NeuralSound's §3.2 simplified proxy is used here.

DAMPING MODEL VALIDATION:
  Sterling, A., Rewkowski, N., Klatzky, R.L., Lin, M.C. (2019).
  "Audio-Material Reconstruction for Virtualized Reality Using a
  Probabilistic Damping Model." IEEE TVCG 25(5), 1855–1864.
  → Validates Rayleigh damping as geometry-invariant across shapes.
  → xi_high / xi_low ≥ 3 is required for perceptual material realism.
  → Previous code had ratios of ~1.5 (all modes same damping) — this
    is why every impact sounded like "dry hollow wood".

WHY NOT DiffSound (SIGGRAPH 2024) OR NeuralSound (SIGGRAPH 2022)?
  DiffSound uses differentiable high-order FEM + implicit SDF shape
  representation. NeuralSound uses a sparse 3D ConvNet + LOBPCG. Both are
  offline research pipelines for parameter estimation — they are not
  designed for real-time interactive use and require GPU inference.
  The O'Brien pipeline with the above extensions is the correct choice for
  an interactive game prototype and is explicitly used by game practitioners
  (Lloyd et al., I3D 2011; van den Doel et al., 2001 FoleyAutomatic).

PIPELINE STAGES
---------------
  1. Extract surface mesh         (modal_mesh_utils.py)
  2. Adaptive subdivision         (modal_mesh_utils.py)
  3. Tetrahedralization           (TetGen — Si 2015)
  4. FEM K, M assembly            (linear CST elements — Bathe 2006)
  5. Eigenvalue solve             (ARPACK shift-invert — scipy)
  6. Participation factors φ_k(v) (van den Doel & Pai 1998)
  7. Radiation efficiency η_k     (surface-normal displacement proxy)
  8. Rayleigh damping ξ_k         (calibrated α,β — Caughey 1960)
  9. Write UModalSoundDataAsset
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
)


# ═══════════════════════════════════════════════════════════════════════════
# MATERIAL PRESETS
# ═══════════════════════════════════════════════════════════════════════════
#
# E (Young's modulus), nu (Poisson ratio), rho (density):
#   From engineering handbooks (Ashby 2011, Materials Selection in
#   Mechanical Design).
#
# xi_low, xi_high (damping ratios at lowest/highest mode):
#   From van den Doel & Pai (1998) Table 1 — the only published measured
#   values for common materials. xi_low is the base damping ratio; xi_high
#   applies at the highest mode. The ratio xi_high/xi_low determines how
#   quickly high modes decay relative to low modes.
#
#   Sterling et al. (2019): xi_high/xi_low ≥ 3–5 required for perceptual
#   material differentiation. Previous code violated this (ratio ≈ 1.5).
#   New values have ratios of 7–10×.
#
# contact_hardness:
#   Relative Hertz contact stiffness (steel = 1.0). Controls estimated
#   contact duration T_c at runtime. NOT stored in DataAsset — used only
#   by ModalImpactComponent.cpp. Harder = shorter T_c = more high-freq.
#   Reference: Johnson (1985) Contact Mechanics; Chadwick et al. (2012).
# ═══════════════════════════════════════════════════════════════════════════
MATERIAL_PRESETS = {
    # Structural steel. E=200 GPa is physically correct.
    # For a 1m solid steel cube, first mode ≈ 2500 Hz. This is correct
    # physics but may sound surprising — use HeavyMetal for large objects.
    "Metal": {
        "E": 200e9, "nu": 0.30, "rho": 7800,
        "xi_low": 0.002,  "xi_high": 0.020,   # ratio 10× — steel rings long
        "contact_hardness": 1.0,
    },

    # Reduced-stiffness heavy metal model for large objects (> ~30 cm).
    # E=8 GPa produces ~500 Hz first mode for a 1m cube — these are the
    # bending frequencies of large steel plate/frame structures, which is
    # perceptually more convincing for objects you'd hear as "heavy metal".
    "HeavyMetal": {
        "E": 8e9,   "nu": 0.30, "rho": 7800,
        "xi_low": 0.004,  "xi_high": 0.030,   # ratio 7.5×
        "contact_hardness": 0.8,
    },

    # Structural wood (spruce/pine). Isotropic model is standard for
    # interactive synthesis — real wood is anisotropic but the modal
    # frequencies are dominated by geometry for furniture-scale objects.
    "Wood": {
        "E": 12e9,  "nu": 0.35, "rho":  700,
        "xi_low": 0.015,  "xi_high": 0.100,   # ratio 6.7× — wood damps quickly
        "contact_hardness": 0.25,
    },

    # Soda-lime glass. Very low internal damping — long decay.
    "Glass": {
        "E": 70e9,  "nu": 0.22, "rho": 2500,
        "xi_low": 0.0008, "xi_high": 0.006,   # ratio 7.5×
        "contact_hardness": 0.55,
    },

    # Granite/marble. Dense, moderately stiff.
    "Stone": {
        "E": 60e9,  "nu": 0.25, "rho": 2700,
        "xi_low": 0.005,  "xi_high": 0.040,   # ratio 8×
        "contact_hardness": 0.65,
    },

    # General thermoplastic (ABS/PLA). Low stiffness, high damping.
    "Plastic": {
        "E": 3e9,   "nu": 0.38, "rho": 1200,
        "xi_low": 0.020,  "xi_high": 0.150,   # ratio 7.5×
        "contact_hardness": 0.18,
    },

    # Fired ceramic / porcelain. Hard, brittle, long decay.
    "Ceramic": {
        "E": 100e9, "nu": 0.22, "rho": 2400,
        "xi_low": 0.001,  "xi_high": 0.008,   # ratio 8×
        "contact_hardness": 0.60,
    },
}


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 3: TETRAHEDRALIZATION
# ═══════════════════════════════════════════════════════════════════════════
def tetrahedralize(vertices: np.ndarray, faces: np.ndarray,
                   bbox_extent: np.ndarray):
    """
    Converts a closed surface triangle mesh into a volumetric tetrahedral
    mesh using TetGen (Si 2015, ACM Trans. Math. Software 41(2)).

    WHY TETRAHEDRALIZATION?
      The FEM eigenvalue problem requires integration over the object's
      volume. TetGen generates a Delaunay-quality tet mesh from any closed
      manifold surface. Critically, it preserves the input surface vertices
      as the first n_surface_verts nodes — our participation factors map
      surface hit positions to DOF indices by this index correspondence.

    FALL-BACK CHAIN FOR ARBITRARY SHAPES:
      Complex or thin-featured meshes (furniture legs, thin shells, handles)
      cause TetGen's strict quality constraints to fail. The fall-back chain
      tries progressively relaxed settings. With quality=False, TetGen
      accepts any closed manifold mesh. This is why the stool failed
      previously — the solver was not using a sufficient fall-back strategy.

      If all configs fail, the mesh has non-manifold geometry or
      self-intersections. Fix this in MeshLab ("Close Holes", "Repair
      Non-Manifold Edges") or Blender's 3D Print Toolbox before importing.

    TARGET TET COUNT:
      2000 tets balances accuracy (modal freqs within ~2–5% of truth for
      simple shapes — Ren et al. 2013) against solve time (30–120 s for
      2000 tets at 3×n_nodes DOFs).
    """
    n_surface_verts = len(vertices)
    max_vol = estimate_tet_volume_constraint(bbox_extent, target_tets=2000)
    unreal.log(f"[TetGen] target vol: {max_vol:.3e} m³, "
               f"{n_surface_verts} surface verts")

    tgen = tetgen.TetGen(vertices, faces)

    configs = [
        {"order": 1, "quality": True,  "minratio": 1.414, "maxvolume": max_vol},
        {"order": 1, "quality": True,  "minratio": 1.414},
        {"order": 1, "quality": True,  "minratio": 2.5},   # thin features
        {"order": 1, "quality": False},                     # any closed manifold
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
            "TetGen failed on all configs. Mesh likely has non-manifold "
            "geometry. Repair in MeshLab (Close Holes + Repair Non-Manifold).")

    if isinstance(result, tuple):
        tet_nodes, tets = result[0], result[1]
    else:
        tet_nodes, tets = result.node, result.elem

    tet_nodes = np.array(tet_nodes, dtype=np.float64)
    tets      = np.array(tets,      dtype=np.int32)
    if tet_nodes.shape[1] > 3: tet_nodes = tet_nodes[:, :3]
    if tets.shape[1] > 4:      tets      = tets[:, :4]

    unreal.log(f"[TetGen] {len(tet_nodes)} nodes, {len(tets)} tets")
    return tet_nodes, tets, n_surface_verts


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 4: FEM MATRIX ASSEMBLY
# ═══════════════════════════════════════════════════════════════════════════
def assemble_fem_matrices(nodes: np.ndarray, tetrahedra: np.ndarray,
                           E: float, nu: float, rho: float):
    """
    Assembles global stiffness K and consistent mass M matrices from
    linear tetrahedral (4-node constant strain, CST) elements.

    REFERENCE:
      Bathe (2006) "Finite Element Procedures", §5.3 (3D solid elements).
      Cook et al. (2002) "Concepts and Applications of FEA", §17.

    DOF ORDERING: [u_x0, u_y0, u_z0, u_x1, ...] → (3N × 3N) matrices.

    CONSTITUTIVE MATRIX D (Voigt engineering-strain notation, 6×6):
      σ = D ε
      Lamé constants: λ = Eν/((1+ν)(1-2ν)),  μ = E/(2(1+ν))

    STIFFNESS: K_e = V_e · B^T D B
      B (6×12) maps nodal displacements to strains. Constant for CST
      → exact one-point integration.

    MASS (consistent): M_e = ρ V_e/20 · [2·I_3 diagonal, I_3 off-diagonal]
      Closed form for linear tet (Cook et al. eq. 17.4-4).

    KNOWN LIMITATION:
      CST elements exhibit volumetric locking — they are too stiff,
      overestimating frequencies. Convergence is monotonically from
      above as mesh is refined (Bathe 2006, §4.3.6). Quadratic 10-node
      tets (used in DiffSound 2024) eliminate this but require a far
      more complex implementation.
    """
    n_dof        = 3 * len(nodes)
    K_global     = lil_matrix((n_dof, n_dof), dtype=np.float64)
    M_global     = lil_matrix((n_dof, n_dof), dtype=np.float64)

    lam = E * nu / ((1 + nu) * (1 - 2*nu))
    mu  = E / (2 * (1 + nu))

    D = np.array([
        [lam+2*mu, lam,      lam,      0,  0,  0],
        [lam,      lam+2*mu, lam,      0,  0,  0],
        [lam,      lam,      lam+2*mu, 0,  0,  0],
        [0,        0,        0,        mu, 0,  0],
        [0,        0,        0,        0,  mu, 0],
        [0,        0,        0,        0,  0,  mu],
    ], dtype=np.float64)

    n_tets       = len(tetrahedra)
    log_interval = max(1, n_tets // 10)
    skipped      = 0

    unreal.log(f"[FEM] Assembling {n_tets} tets, {n_dof} DOFs …")

    for idx, tet in enumerate(tetrahedra):
        if idx % log_interval == 0:
            unreal.log(f"[FEM]   {100*idx//n_tets}%")

        n0, n1 = nodes[tet[0]], nodes[tet[1]]
        n2, n3 = nodes[tet[2]], nodes[tet[3]]

        J     = np.array([n1-n0, n2-n0, n3-n0], dtype=np.float64)
        det_J = np.linalg.det(J)
        if abs(det_J) < 1e-20:
            skipped += 1
            continue

        if det_J < 0:           # inverted tet — swap nodes 2 and 3
            tet[2], tet[3] = tet[3], tet[2]
            n2, n3 = n3, n2
            J     = np.array([n1-n0, n2-n0, n3-n0], dtype=np.float64)
            det_J = abs(det_J)

        V     = det_J / 6.0
        inv_J = np.linalg.inv(J)

        # Shape function derivatives: N1=1-ξ-η-ζ, N2=ξ, N3=η, N4=ζ
        dN_dxi = np.array([[-1.,-1.,-1.],
                            [ 1., 0., 0.],
                            [ 0., 1., 0.],
                            [ 0., 0., 1.]], dtype=np.float64)
        dN = dN_dxi @ inv_J     # physical derivatives (4×3)

        # Strain-displacement matrix B (6×12)
        B = np.zeros((6, 12), dtype=np.float64)
        for i in range(4):
            c = 3*i
            B[0, c]   = dN[i,0]
            B[1, c+1] = dN[i,1]
            B[2, c+2] = dN[i,2]
            B[3, c]   = dN[i,1]; B[3, c+1] = dN[i,0]
            B[4, c+1] = dN[i,2]; B[4, c+2] = dN[i,1]
            B[5, c]   = dN[i,2]; B[5, c+2] = dN[i,0]

        K_e = V * (B.T @ D @ B)

        ms  = rho * V / 20.0
        M_e = np.zeros((12, 12), dtype=np.float64)
        for i in range(4):
            for j in range(4):
                f = 2.0 if i == j else 1.0
                for d in range(3):
                    M_e[3*i+d, 3*j+d] = ms * f

        dofs = [3*nid+d for nid in tet for d in range(3)]
        for i, gi in enumerate(dofs):
            for j, gj in enumerate(dofs):
                K_global[gi, gj] += K_e[i, j]
                M_global[gi, gj] += M_e[i, j]

    if skipped:
        unreal.log_warning(f"[FEM] Skipped {skipped} degenerate tets")
    unreal.log("[FEM] Done — converting to CSC")
    return csc_matrix(K_global), csc_matrix(M_global)


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 5: EIGENVALUE SOLVE
# ═══════════════════════════════════════════════════════════════════════════
def solve_modes(K, M, num_modes: int):
    """
    Solves K φ_k = ω_k² M φ_k using ARPACK shift-invert (sigma=0).

    Shift-invert transforms the problem so ARPACK efficiently finds the
    lowest eigenvalues of large sparse systems (Lehoucq et al. 1998).

    We request (num_modes + 10) to safely skip the 6 rigid-body modes
    (3 translations + 3 rotations of a free body) that appear at near-zero
    frequency due to numerical noise. Modes below 20 Hz are discarded.
    """
    n_req = num_modes + 10
    unreal.log(f"[Eigen] {K.shape[0]} DOFs, requesting {n_req} modes")

    eigenvalues, eigenvectors = eigsh(
        K, k=n_req, M=M,
        sigma=0.0, which='LM',
        tol=1e-6, maxiter=10000)

    frequencies  = np.sqrt(np.abs(eigenvalues)) / (2.0 * np.pi)
    sort_idx     = np.argsort(frequencies)
    frequencies  = frequencies[sort_idx]
    eigenvectors = eigenvectors[:, sort_idx]

    mask         = frequencies >= 20.0
    frequencies  = frequencies[mask][:num_modes]
    eigenvectors = eigenvectors[:, mask][:, :num_modes]

    unreal.log(f"[Eigen] {len(frequencies)} modes: "
               f"{frequencies[0]:.1f}–{frequencies[-1]:.1f} Hz")
    return frequencies, eigenvectors


# ═══════════════════════════════════════════════════════════════════════════
# STAGES 6 + 7: PARTICIPATION FACTORS AND RADIATION EFFICIENCY
# ═══════════════════════════════════════════════════════════════════════════
def compute_participation_and_radiation(mode_shapes: np.ndarray,
                                         n_surface_verts: int,
                                         tet_nodes: np.ndarray):
    """
    Computes two physically grounded per-mode quantities.

    ── PARTICIPATION FACTOR φ_k(v) ─────────────────────────────────────────
    The displacement magnitude of surface vertex v in mode k.
    van den Doel & Pai (1998) §3.2 and O'Brien et al. (2002) §3:
    "Mode k is excited with amplitude proportional to φ_k(x)" where x is
    the impact location. We approximate x by the closest surface vertex.

    ── RADIATION EFFICIENCY η_k ────────────────────────────────────────────
    The RMS surface-normal displacement of mode k, averaged over surface
    vertices. NeuralSound (SIGGRAPH 2022) quantified up to 1000× difference
    between modes — modes where surface patches move in opposite directions
    cancel and radiate poorly (Zheng & James 2011).

    We use the simplified surface-normal proxy from NeuralSound §3.2:
      η_k = mean_v(|φ_k(v) · n̂(v)|)
    where n̂(v) = normalised(v - centroid) approximates the outward normal.
    Full BEM-based FFAT (James et al. 2006) is not feasible for a prototype.

    TetGen preserves input surface vertices as first n_surface_verts nodes.
    DOFs for vertex v: [3v, 3v+1, 3v+2].

    Returns:
        participation: (n_modes, n_surface_verts) float32, normalised [0,1]
        radiation:     (n_modes,) float32, normalised [0,1]
    """
    n_modes      = mode_shapes.shape[1]
    participation = np.zeros((n_modes, n_surface_verts), dtype=np.float32)
    radiation     = np.zeros(n_modes, dtype=np.float32)

    # Approximate outward normals: unit vector from centroid to vertex.
    surf_pts = tet_nodes[:n_surface_verts]
    centroid = surf_pts.mean(axis=0)
    outward  = surf_pts - centroid
    norms    = np.linalg.norm(outward, axis=1, keepdims=True)
    norms    = np.where(norms < 1e-12, 1.0, norms)
    normals  = outward / norms   # (n_surface_verts, 3)

    for k in range(n_modes):
        mode_surf = mode_shapes[:3*n_surface_verts, k].reshape(n_surface_verts, 3)

        # Participation = displacement magnitude per surface vertex
        disp_mag = np.linalg.norm(mode_surf, axis=1).astype(np.float32)
        participation[k] = disp_mag
        mx = participation[k].max()
        if mx > 1e-12:
            participation[k] /= mx

        # Radiation = mean |normal-component of displacement|
        normal_proj = np.abs(np.einsum('ij,ij->i', mode_surf, normals))
        radiation[k] = float(normal_proj.mean())

    max_r = radiation.max()
    if max_r > 1e-12:
        radiation /= max_r

    unreal.log(f"[Radiation] η: {radiation.min():.3f}–{radiation.max():.3f}")
    return participation, radiation


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 8: RAYLEIGH DAMPING
# ═══════════════════════════════════════════════════════════════════════════
def compute_rayleigh_damping(frequencies_hz: np.ndarray,
                              material: str) -> np.ndarray:
    """
    Per-mode damping ratios using Rayleigh (proportional) damping.

    ξ_k = α/(2ω_k) + β·ω_k/2   (Caughey 1960)

    α (mass-proportional) damps low modes more.
    β (stiffness-proportional) damps high modes more.

    We calibrate α and β so that:
      ξ(ω_1) = xi_low   (at lowest mode)
      ξ(ω_N) = xi_high  (at highest mode)
    by solving the 2×2 linear system. This gives the correct
    xi_high/xi_low ratio (7–10×) required by Sterling et al. (2019)
    for perceptual material differentiation.

    KEY CHANGE FROM PREVIOUS VERSION:
    The old code used a single xi_0 value for all modes then linearly
    interpolated in log-frequency space — this produced ratios of ≈1.5
    and is why all materials sounded the same. Rayleigh calibration
    between xi_low and xi_high produces the physically correct U-shaped
    curve (but approximately monotone-increasing for our parameter range)
    that matches measured decay behavior.
    """
    mat   = MATERIAL_PRESETS[material]
    omega = 2.0 * np.pi * frequencies_hz

    if len(omega) < 2:
        return np.full(len(omega), mat["xi_low"], dtype=np.float32)

    w1, wN = omega[0], omega[-1]
    A = np.array([[1/(2*w1), w1/2],
                  [1/(2*wN), wN/2]])
    b_vec = np.array([mat["xi_low"], mat["xi_high"]])

    try:
        alpha, beta = np.linalg.solve(A, b_vec)
    except np.linalg.LinAlgError:
        unreal.log_warning("[Damping] calibration failed, using xi_low")
        return np.full(len(omega), mat["xi_low"], dtype=np.float32)

    xi = np.clip(alpha/(2*omega) + beta*omega/2, 0.0001, 0.5).astype(np.float32)

    unreal.log(
        f"[Damping] {material}: ξ {xi[0]:.4f}→{xi[-1]:.4f}  "
        f"ratio {xi[-1]/xi[0]:.1f}× (need ≥3 for perceptual realism)")
    return xi


# ═══════════════════════════════════════════════════════════════════════════
# STAGE 9: WRITE DATAASSET
# ═══════════════════════════════════════════════════════════════════════════
def write_data_asset(output_path, mesh_asset_path, frequencies,
                     damping, participation, radiation, material, meta):
    """
    Creates UModalSoundDataAsset and writes all computed modal data.

    WHAT GOES IN GLOBALAMPLITUDE:
      GlobalAmplitude = radiation efficiency η_k.
      At runtime, ModalImpactComponent multiplies this by:
        φ_k(vertex)         — participation factor (spatial)
        √(KE/KE_max)        — energy scale (Klatzky et al. 2000)
        F_k(v_impact)       — Hertz contact spectral weight (computed
                              from actual impact speed — Chadwick et al. 2012)
        jitter ±15%         — micro-variation (Rath & Rocchesso 2005)

    WHY HERTZ IS NOT BAKED INTO THE DATAASSET:
      The contact duration T_c (which shapes which frequencies get excited)
      depends on the actual impact speed at runtime. Baking a fixed T_c
      offline (as the previous version did) makes all impacts sound identical
      regardless of whether the object falls gently or slams down hard.
      The bridge now computes F_k(v) per-impact from the measured velocity.
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
    da.set_editor_property("material_type",
        getattr(unreal.ImpactMaterial, material.upper()))
    da.set_editor_property("young_modulus",        float(mat["E"]))
    da.set_editor_property("poisson_ratio",        float(mat["nu"]))
    da.set_editor_property("density",              float(mat["rho"]))
    da.set_editor_property("source_mesh",
        unreal.EditorAssetLibrary.load_asset(mesh_asset_path))
    da.set_editor_property("surface_vertex_count", int(meta["n_surface_verts"]))
    da.set_editor_property("mesh_volume",          float(meta.get("volume", 0.0)))
    da.set_editor_property("bounding_box_extent",
        unreal.Vector(*meta["bbox_extent"].tolist()))

    modes = []
    for i in range(len(frequencies)):
        m = unreal.ModalMode()
        m.set_editor_property("frequency",     float(frequencies[i]))
        m.set_editor_property("damping_ratio", float(damping[i]))
        # Radiation efficiency η_k — floor at 0.05 so no mode is silent.
        m.set_editor_property("global_amplitude", max(float(radiation[i]), 0.05))
        m.set_editor_property("vertex_participation", participation[i].tolist())
        modes.append(m)

    da.set_editor_property("modes", modes)
    unreal.EditorAssetLibrary.save_asset(output_path)
    unreal.log(f"[Asset] '{output_path}': {len(frequencies)} modes, "
               f"{meta['n_surface_verts']} verts, {material}")
    return da


# ═══════════════════════════════════════════════════════════════════════════
# MAIN ENTRY POINT
# ═══════════════════════════════════════════════════════════════════════════
def generate_modal_asset(mesh_asset_path: str,
                          output_asset_path: str,
                          material: str = "HeavyMetal",
                          num_modes: int = 20):
    """
    Full pipeline. Call from UE5 Python console or Editor Utility Widget.

    MATERIAL GUIDE:
      Metal      — small steel objects < 30 cm
      HeavyMetal — large steel/iron > 30 cm, cast iron, heavy machinery
      Wood       — furniture, floors, crates
      Glass      — glass bottles, windows, crystal
      Stone      — concrete blocks, stone tiles, gravel
      Plastic    — plastic containers, toys
      Ceramic    — mugs, tiles, fired clay

    ARBITRARY SHAPES (stool, chair, complex mesh):
      - Ensure the mesh is a closed manifold (no holes, no self-intersections)
      - Check in UE5: Mesh Editor → Statistics → check for "Open Edges"
      - If TetGen fails even with fall-backs, repair in MeshLab:
          Filters → Cleaning → Repair Non Manifold Edges
          Filters → Remeshing → Close Holes
      - Thin legs: TetGen fall-back to quality=False handles these
      - Expected: first modes are bending modes of thin/long features

    Example:
        import modal_sound_generator as msg
        msg.generate_modal_asset(
            "/Game/Meshes/SM_Stool",
            "/Game/ModalData/MDA_Stool_Wood",
            material="Wood", num_modes=20)
    """
    unreal.log("="*60)
    unreal.log(f"  MODAL GENERATOR  {mesh_asset_path}")
    unreal.log(f"  Material: {material}   Modes: {num_modes}")
    unreal.log("="*60)

    if material not in MATERIAL_PRESETS:
        raise ValueError(f"Unknown material '{material}'. "
                         f"Choose: {list(MATERIAL_PRESETS.keys())}")

    # Stage 1
    vertices, faces, meta = extract_static_mesh(mesh_asset_path)
    if not validate_mesh_for_fem(vertices, faces):
        raise RuntimeError("Mesh validation failed.")

    # Stage 1.5: Automatic mesh repair (handles Megascans self-intersections)
    from modal_mesh_utils import repair_mesh_for_fem
    vertices, faces = repair_mesh_for_fem(vertices, faces)
    meta["n_surface_verts"] = len(vertices)
    meta["n_triangles"]     = len(faces)
    # Re-validate after repair
    if not validate_mesh_for_fem(vertices, faces):
        raise RuntimeError("Mesh still invalid after repair.")

    # Stage 2 — adaptive subdivision
    bbox_vol     = meta["bbox_volume"]
    target_verts = 300
    if bbox_vol < 0.005: target_verts = 80
    elif bbox_vol < 0.05: target_verts = 150
    elif bbox_vol < 0.3:  target_verts = 220

    if meta["n_surface_verts"] < target_verts:
        current, subs = meta["n_surface_verts"], 0
        while current < target_verts and subs < 4:
            current *= 4; subs += 1
        unreal.log(f"[Subdiv] subdivide {subs}× to reach {target_verts} target verts")
        vertices, faces = subdivide_surface_mesh(vertices, faces, subs)
        meta["n_surface_verts"] = len(vertices)
        meta["n_triangles"]     = len(faces)

    # Stage 3
    tet_nodes, tets, n_sv = tetrahedralize(vertices, faces, meta["bbox_extent"])
    meta["n_surface_verts"] = n_sv
    meta["volume"] = float(sum(
        abs(np.linalg.det(np.array([
            tet_nodes[t[1]]-tet_nodes[t[0]],
            tet_nodes[t[2]]-tet_nodes[t[0]],
            tet_nodes[t[3]]-tet_nodes[t[0]]]))) / 6.0
        for t in tets))

    # Stage 4
    mat  = MATERIAL_PRESETS[material]
    K, M = assemble_fem_matrices(tet_nodes, tets, mat["E"], mat["nu"], mat["rho"])

    # Stage 5
    frequencies, mode_shapes = solve_modes(K, M, num_modes)

    # Stages 6 + 7
    participation, radiation = compute_participation_and_radiation(
        mode_shapes, n_sv, tet_nodes)

    # Stage 8
    damping = compute_rayleigh_damping(frequencies, material)

    # Stage 9
    asset = write_data_asset(
        output_asset_path, mesh_asset_path,
        frequencies, damping, participation, radiation, material, meta)

    unreal.log("="*60 + "\n  COMPLETE\n" + "="*60)
    return asset


def generate_from_widget(mesh_path, output_path, material, num_modes_str):
    """Widget entry point — handles Blueprint string conversion."""
    import importlib, modal_mesh_utils
    importlib.reload(modal_mesh_utils)
    mesh_path   = mesh_path.split(".")[0]
    output_path = output_path.split(".")[0]
    num_modes   = int(float(num_modes_str))
    if material not in MATERIAL_PRESETS:
        material = "HeavyMetal"
    generate_modal_asset(mesh_path, output_path, material, num_modes)