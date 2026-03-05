"""
modal_sound_generator.py

Full pipeline: UE5 StaticMesh → FEM eigenvalue solve → UModalSoundDataAsset

Scientific references:
  - van den Doel & Pai (1998): material damping parameters
  - O'Brien, Cook & Essl (2001): FEM modal synthesis pipeline  
  - Bathe (2006): linear tetrahedral FEM formulation
  - Si (2015): TetGen mesh generation
  - Lehoucq, Sorensen & Yang (1998): ARPACK eigenvalue solver
"""

import unreal
import numpy as np
from scipy.sparse import lil_matrix, csc_matrix
from scipy.sparse.linalg import eigsh
import tetgen
from modal_mesh_utils import (
    extract_static_mesh,
    validate_mesh_for_fem,
    estimate_tet_volume_constraint
)


# ─────────────────────────────────────────────────────────────────
# MATERIAL DATABASE
# Elastic moduli and densities from engineering handbooks.
# Damping ratios (xi) from van den Doel & Pai (1998),
# Table 1: "The sounds of physical shapes", Presence 7(4).
# ─────────────────────────────────────────────────────────────────
MATERIAL_PRESETS = {
    "Metal": {"E": 8e9,    "nu": 0.30, "rho": 7800, "xi": 0.008},
    "Wood":    {"E": 12e9,   "nu": 0.35, "rho": 700,  "xi": 0.04},
    "Glass":   {"E": 70e9,   "nu": 0.22, "rho": 2500, "xi": 0.005},
    "Stone":   {"E": 60e9,   "nu": 0.25, "rho": 2700, "xi": 0.015},
    "Plastic": {"E": 3e9,    "nu": 0.38, "rho": 1200, "xi": 0.05},
    "Ceramic": {"E": 100e9,  "nu": 0.22, "rho": 2400, "xi": 0.008},
}


# ─────────────────────────────────────────────────────────────────
# STAGE 1: TETRAHEDRALIZATION
# ─────────────────────────────────────────────────────────────────
def tetrahedralize(vertices: np.ndarray, faces: np.ndarray,
                   bbox_extent: np.ndarray):
    """
    Converts a closed surface triangle mesh to a volumetric
    tetrahedral mesh using TetGen.

    TetGen reference:
      Si, H. (2015). "TetGen, a Delaunay-Based Quality Tetrahedral
      Mesh Generator." ACM Transactions on Mathematical Software, 41(2).
    """
    n_surface_verts = len(vertices)
    max_vol = estimate_tet_volume_constraint(bbox_extent, target_tets=2000)
    unreal.log(f"Tetrahedralizing — max tet volume: {max_vol:.2e} m³")

    tgen = tetgen.TetGen(vertices, faces)

    # Try with quality constraints first, then fall back progressively
    configs = [
        {"order": 1, "quality": True,  "minratio": 1.414, "maxvolume": max_vol},
        {"order": 1, "quality": True,  "minratio": 1.414},
        {"order": 1, "quality": False},
        {"order": 1},
        {},
    ]

    result = None
    for cfg in configs:
        try:
            result = tgen.tetrahedralize(**cfg)
            unreal.log(f"Tetrahedralization succeeded with config: {cfg}")
            break
        except Exception as e:
            unreal.log_warning(f"Config {cfg} failed: {e}. Trying next...")

    if result is None:
        raise RuntimeError("All tetrahedralization attempts failed.")

    # Handle different return types across tetgen versions
    if isinstance(result, tuple):
        tet_nodes, tets = result[0], result[1]
    else:
        # Newer tetgen returns an object — extract node/tet arrays
        tet_nodes = result.node
        tets      = result.elem

    # Ensure correct dtypes
    tet_nodes = np.array(tet_nodes, dtype=np.float64)
    tets      = np.array(tets,      dtype=np.int32)

    # Strip any extra columns tetgen sometimes appends
    if tet_nodes.shape[1] > 3:
        tet_nodes = tet_nodes[:, :3]
    if tets.shape[1] > 4:
        tets = tets[:, :4]

    unreal.log(
        f"Tetrahedralization complete: "
        f"{len(tet_nodes)} nodes, {len(tets)} tetrahedra"
    )

    return tet_nodes, tets, n_surface_verts


# ─────────────────────────────────────────────────────────────────
# STAGE 2: FEM MATRIX ASSEMBLY
# ─────────────────────────────────────────────────────────────────
def assemble_fem_matrices(nodes: np.ndarray, tetrahedra: np.ndarray,
                           E: float, nu: float, rho: float):
    """
    Assembles global stiffness (K) and consistent mass (M) matrices
    from linear tetrahedral elements (4-node, constant strain tet).

    Formulation reference:
      Bathe, K.J. (2006). Finite Element Procedures. Prentice Hall.
      Chapter 4 (displacement-based FE), Section 5.3 (3D solid elements).

    Also:
      Cook, R.D. et al. (2002). Concepts and Applications of Finite
      Element Analysis, 4th Ed. Wiley. Chapter 17.

    DOF ordering: [u_x0, u_y0, u_z0, u_x1, u_y1, u_z1, ...]
    Each node has 3 DOFs → global matrices are (3N × 3N).

    The constitutive matrix D maps engineering strains to stresses
    for a linear isotropic elastic material:
      σ = D · ε
    where Lamé parameters λ and μ are computed from E and ν.
    """
    n_nodes = len(nodes)
    n_dof   = 3 * n_nodes

    # Use lil_matrix for efficient incremental assembly
    # then convert to csc_matrix for the eigenvalue solver
    K_global = lil_matrix((n_dof, n_dof), dtype=np.float64)
    M_global = lil_matrix((n_dof, n_dof), dtype=np.float64)

    # ── Constitutive matrix D (6×6, Voigt notation) ───────────────
    # Lamé parameters from Young's modulus E and Poisson ratio ν
    lam = (E * nu) / ((1.0 + nu) * (1.0 - 2.0 * nu))  # λ
    mu  = E / (2.0 * (1.0 + nu))                        # μ (shear modulus)

    D = np.array([
        [lam+2*mu, lam,      lam,      0,  0,  0 ],
        [lam,      lam+2*mu, lam,      0,  0,  0 ],
        [lam,      lam,      lam+2*mu, 0,  0,  0 ],
        [0,        0,        0,        mu, 0,  0 ],
        [0,        0,        0,        0,  mu, 0 ],
        [0,        0,        0,        0,  0,  mu],
    ], dtype=np.float64)

    n_tets = len(tetrahedra)
    log_interval = max(1, n_tets // 10)

    unreal.log(f"Assembling FEM matrices ({n_tets} tetrahedra)...")

    skipped = 0
    for tet_idx, tet in enumerate(tetrahedra):

        if tet_idx % log_interval == 0:
            pct = int(100 * tet_idx / n_tets)
            unreal.log(f"  Assembly: {pct}%")

        # ── Node coordinates for this tet ─────────────────────────
        n0, n1, n2, n3 = nodes[tet[0]], nodes[tet[1]], nodes[tet[2]], nodes[tet[3]]

        # ── Jacobian matrix ───────────────────────────────────────
        # Maps reference element coords (ξ,η,ζ) to physical coords (x,y,z)
        # Bathe (2006), eq. 5.70
        J = np.array([
            n1 - n0,
            n2 - n0,
            n3 - n0,
        ], dtype=np.float64)  # shape (3, 3)

        det_J = np.linalg.det(J)

        if abs(det_J) < 1e-20:
            skipped += 1
            continue  # Degenerate tet — skip

        if det_J < 0:
            # Negative determinant means inverted tet (wrong winding)
            # Swap two nodes to fix orientation
            tet[2], tet[3] = tet[3], tet[2]
            n2, n3 = n3, n2
            J = np.array([n1-n0, n2-n0, n3-n0], dtype=np.float64)
            det_J = abs(det_J)

        V_tet = det_J / 6.0   # volume of this tetrahedron

        inv_J = np.linalg.inv(J)

        # ── Shape function derivatives ────────────────────────────
        # For linear tet in reference coords:
        #   N1 = 1-ξ-η-ζ,  N2 = ξ,  N3 = η,  N4 = ζ
        # Derivatives w.r.t. reference coords:
        dN_dxi = np.array([
            [-1., -1., -1.],   # dN1/d(ξ,η,ζ)
            [ 1.,  0.,  0.],   # dN2/d(ξ,η,ζ)
            [ 0.,  1.,  0.],   # dN3/d(ξ,η,ζ)
            [ 0.,  0.,  1.],   # dN4/d(ξ,η,ζ)
        ], dtype=np.float64)   # shape (4, 3)

        # Chain rule: dN/d(x,y,z) = dN/d(ξ,η,ζ) · inv_J
        dN_dx = dN_dxi @ inv_J  # shape (4, 3)

        # ── Strain-displacement matrix B (6×12) ───────────────────
        # Relates strain vector ε to nodal displacement vector u:
        #   ε = B · u
        # Engineering strain convention (Voigt):
        #   ε = [ε_xx, ε_yy, ε_zz, γ_xy, γ_yz, γ_xz]
        B = np.zeros((6, 12), dtype=np.float64)
        for i in range(4):
            c = 3 * i  # column offset for node i
            # Normal strains: ε_xx = dux/dx, etc.
            B[0, c+0] = dN_dx[i, 0]   # dNi/dx
            B[1, c+1] = dN_dx[i, 1]   # dNi/dy
            B[2, c+2] = dN_dx[i, 2]   # dNi/dz
            # Shear strains (engineering shear = 2× tensor shear)
            B[3, c+0] = dN_dx[i, 1]; B[3, c+1] = dN_dx[i, 0]  # γ_xy
            B[4, c+1] = dN_dx[i, 2]; B[4, c+2] = dN_dx[i, 1]  # γ_yz
            B[5, c+0] = dN_dx[i, 2]; B[5, c+2] = dN_dx[i, 0]  # γ_xz

        # ── Local stiffness: K_e = V · B^T · D · B ───────────────
        # For constant-strain tet, one-point integration is exact.
        K_local = V_tet * (B.T @ D @ B)

        # ── Consistent mass matrix ────────────────────────────────
        # M_e = ρ·V/20 · (2·I_block + off_diag_blocks)
        # Cook et al. (2002), eq. 17.4-4
        # For linear tet: diagonal blocks = 2, off-diagonal = 1
        m_scale = rho * V_tet / 20.0
        M_local = np.zeros((12, 12), dtype=np.float64)
        for i in range(4):
            for j in range(4):
                factor = 2.0 if i == j else 1.0
                for d in range(3):
                    M_local[3*i+d, 3*j+d] = m_scale * factor

        # ── Scatter into global matrices ──────────────────────────
        dofs = []
        for node_id in tet:
            dofs += [3*node_id, 3*node_id+1, 3*node_id+2]

        for i, gi in enumerate(dofs):
            for j, gj in enumerate(dofs):
                K_global[gi, gj] += K_local[i, j]
                M_global[gi, gj] += M_local[i, j]

    if skipped > 0:
        unreal.log_warning(f"Skipped {skipped} degenerate tetrahedra.")

    unreal.log("FEM assembly complete. Converting to sparse CSC format...")

    return csc_matrix(K_global), csc_matrix(M_global)


# ─────────────────────────────────────────────────────────────────
# STAGE 3: EIGENVALUE SOLVE
# ─────────────────────────────────────────────────────────────────
def solve_modes(K, M, num_modes: int):
    """
    Solves the generalized eigenvalue problem:
      K · φ_k = ω_k² · M · φ_k

    Uses ARPACK via scipy.sparse.linalg.eigsh with shift-invert
    mode (sigma=0), which efficiently finds the smallest eigenvalues
    of large sparse systems by inverting near zero.

    Reference:
      Lehoucq, R.B., Sorensen, D.C., Yang, C. (1998).
      ARPACK Users' Guide: Solution of Large Scale Eigenvalue
      Problems with Implicitly Restarted Arnoldi Methods. SIAM.

    We request num_modes + 10 to account for rigid body modes
    (6 theoretical zero-frequency modes for a free-floating body)
    and then discard modes below 20 Hz.

    Returns:
        frequencies_hz:  np.ndarray of modal frequencies in Hz
        mode_shapes:     np.ndarray (n_dof, n_modes) of eigenvectors
    """
    n_request = num_modes + 10  # request extra to safely skip rigid body modes

    unreal.log(
        f"Solving eigenvalue problem — "
        f"system size: {K.shape[0]} DOFs, "
        f"requesting {n_request} modes..."
    )
    unreal.log("(This typically takes 30–120 seconds for 2000 tets)")

    eigenvalues, eigenvectors = eigsh(
        K,
        k=n_request,
        M=M,
        sigma=0.0,      # shift-invert: find eigenvalues nearest to 0
        which='LM',     # largest magnitude after shift = smallest original
        tol=1e-6,
        maxiter=10000
    )

    # ω² = eigenvalue → ω = sqrt(|eigenvalue|) → f = ω / (2π)
    eigenvalues = np.abs(eigenvalues)
    frequencies_hz = np.sqrt(eigenvalues) / (2.0 * np.pi)

    # Sort ascending
    sort_idx       = np.argsort(frequencies_hz)
    frequencies_hz = frequencies_hz[sort_idx]
    eigenvectors   = eigenvectors[:, sort_idx]

    # Discard rigid body modes (below 20 Hz threshold)
    # A free body has 6 zero-frequency modes (3 translations, 3 rotations).
    # In practice numerical noise puts these at 0–10 Hz.
    acoustic_mask  = frequencies_hz >= 20.0
    frequencies_hz = frequencies_hz[acoustic_mask]
    eigenvectors   = eigenvectors[:, acoustic_mask]

    # Keep only the requested number of modes
    frequencies_hz = frequencies_hz[:num_modes]
    eigenvectors   = eigenvectors[:, :num_modes]

    unreal.log(
        f"Eigenvalue solve complete. "
        f"{len(frequencies_hz)} acoustic modes found.\n"
        f"Frequency range: "
        f"{frequencies_hz[0]:.1f} Hz – {frequencies_hz[-1]:.1f} Hz"
    )

    return frequencies_hz, eigenvectors


# ─────────────────────────────────────────────────────────────────
# STAGE 4: SURFACE VERTEX PARTICIPATION
# ─────────────────────────────────────────────────────────────────
def compute_vertex_participation(mode_shapes: np.ndarray,
                                  n_surface_verts: int):
    """
    Extracts per-surface-vertex participation factors from the
    full volumetric eigenvectors.

    For mode k and surface vertex v, the participation factor is:
      φ_k(v) = ||[u_x, u_y, u_z]||  (displacement magnitude)

    This represents how much vertex v participates in mode k.
    At runtime, when the mesh is struck at vertex v, mode k is
    excited with amplitude proportional to φ_k(v).

    Physical basis: the excitation amplitude of mode k from an
    impulse at point x is proportional to the mode shape value
    at x. See van den Doel & Pai (1998), Section 3.2, and
    O'Brien et al. (2001), Section 3.

    Because TetGen preserves input surface vertices as the first
    n_surface_verts nodes of the tet mesh, the DOF indices for
    surface vertex v are simply [3v, 3v+1, 3v+2].

    Returns:
        participation: np.ndarray (n_modes, n_surface_verts), float32
                       normalized to [0, 1] per mode
    """
    n_modes = mode_shapes.shape[1]
    participation = np.zeros((n_modes, n_surface_verts), dtype=np.float32)

    for k in range(n_modes):
        for v in range(n_surface_verts):
            # Extract 3D displacement vector for vertex v in mode k
            dof_start = 3 * v
            disp = mode_shapes[dof_start : dof_start + 3, k]
            participation[k, v] = float(np.linalg.norm(disp))

        # Normalize per mode to [0, 1]
        max_p = participation[k].max()
        if max_p > 1e-12:
            participation[k] /= max_p

    return participation


# ─────────────────────────────────────────────────────────────────
# STAGE 5: RAYLEIGH DAMPING
# ─────────────────────────────────────────────────────────────────
def compute_rayleigh_damping(frequencies_hz: np.ndarray,
                              material: str) -> np.ndarray:
    """
    Computes per-mode damping ratios with enforced frequency dependence.
    
    For perceptually convincing impact sounds, high-frequency modes
    must decay significantly faster than low-frequency modes.
    This matches measured behavior of real objects where surface
    waves and air coupling damp high modes more strongly.
    
    Reference: van den Doel & Pai (1998), eq. 4-5.
    Rath & Rocchesso (2005): contact damping increases with frequency.
    """
    mat  = MATERIAL_PRESETS[material]
    xi_0 = mat["xi"]
    
    omega = 2.0 * np.pi * frequencies_hz
    omega_min = omega[0]
    omega_max = omega[-1]
    
    # Enforce meaningful range: high modes damp 5x faster than low modes
    # This is the key perceptual parameter for impact sound character
    xi_min = xi_0
    xi_max = xi_0 * 5.0
    
    # Linear interpolation in log-frequency space
    if omega_max > omega_min:
        t = (np.log(omega) - np.log(omega_min)) / \
            (np.log(omega_max) - np.log(omega_min))
    else:
        t = np.zeros_like(omega)
    
    xi_per_mode = xi_min + t * (xi_max - xi_min)
    xi_per_mode = np.clip(xi_per_mode, 0.0001, 0.5).astype(np.float32)
    
    unreal.log(
        f"Damping range: {xi_per_mode[0]:.4f} (mode 0) "
        f"to {xi_per_mode[-1]:.4f} (mode {len(xi_per_mode)-1})")
    
    return xi_per_mode


# ─────────────────────────────────────────────────────────────────
# STAGE 6: WRITE DATAASSET
# ─────────────────────────────────────────────────────────────────
def write_data_asset(output_path: str,
                     mesh_asset_path: str,
                     frequencies: np.ndarray,
                     damping: np.ndarray,
                     participation: np.ndarray,
                     material: str,
                     meta: dict):
    """
    Creates a UModalSoundDataAsset at output_path and populates it
    with the computed modal data.

    output_path format: "/Game/ModalData/MDA_MyMesh"
    """
    import os

    package_path = os.path.dirname(output_path).replace("\\", "/")
    asset_name   = os.path.basename(output_path)

    asset_tools = unreal.AssetToolsHelpers.get_asset_tools()

    # Create the DataAsset
    data_asset = asset_tools.create_asset(
        asset_name,
        package_path,
        unreal.ModalSoundDataAsset,
        unreal.DataAssetFactory()
    )

    if data_asset is None:
        raise RuntimeError(
            f"Failed to create DataAsset at '{output_path}'. "
            f"Check that the output path exists in the Content Browser."
        )

    # ── Set material parameters ───────────────────────────────────
    mat = MATERIAL_PRESETS[material]
    material_enum = getattr(unreal.ImpactMaterial, material.upper())
    data_asset.set_editor_property("material_type", material_enum)

    data_asset.set_editor_property("material_type",  material_enum)
    data_asset.set_editor_property("young_modulus",  float(mat["E"]))
    data_asset.set_editor_property("poisson_ratio",  float(mat["nu"]))
    data_asset.set_editor_property("density",        float(mat["rho"]))
    mesh_obj = unreal.EditorAssetLibrary.load_asset(mesh_asset_path)
    data_asset.set_editor_property("source_mesh", mesh_obj)
    data_asset.set_editor_property("surface_vertex_count",
        int(meta["n_surface_verts"]))
    data_asset.set_editor_property("mesh_volume",
        float(meta.get("volume", 0.0)))
    data_asset.set_editor_property("bounding_box_extent",
        unreal.Vector(*meta["bbox_extent"].tolist()))

    # ── Build and set modes array ─────────────────────────────────
    n_modes = len(frequencies)
    modes_array = []

    for i in range(n_modes):
        mode = unreal.ModalMode()
        mode.set_editor_property("frequency",       float(frequencies[i]))
        mode.set_editor_property("damping_ratio",   float(damping[i]))
        mode.set_editor_property("global_amplitude", 1.0)
        mode.set_editor_property("vertex_participation",
            participation[i].tolist())
        modes_array.append(mode)

    data_asset.set_editor_property("modes", modes_array)

    # ── Save ──────────────────────────────────────────────────────
    unreal.EditorAssetLibrary.save_asset(output_path)

    unreal.log(
        f"DataAsset saved: '{output_path}'\n"
        f"  {n_modes} modes, "
        f"{meta['n_surface_verts']} surface vertices, "
        f"material: {material}"
    )

    return data_asset


# ─────────────────────────────────────────────────────────────────
# MAIN ENTRY POINT
# ─────────────────────────────────────────────────────────────────
def generate_modal_asset(mesh_asset_path: str,
                          output_asset_path: str,
                          material: str = "Metal",
                          num_modes: int = 30):
    """
    Full pipeline entry point. Call this from the Editor Utility Widget
    or directly from the Python console for testing.

    Args:
        mesh_asset_path:    "/Game/Meshes/SM_MetalBowl"
        output_asset_path:  "/Game/ModalData/MDA_MetalBowl"
        material:           "Metal" | "Wood" | "Glass" | "Stone" |
                            "Plastic" | "Ceramic"
        num_modes:          Number of modes to compute (recommend 20-40)

    Example (from UE5 Python console):
        import modal_sound_generator as msg
        msg.generate_modal_asset(
            "/Game/Meshes/SM_Cube",
            "/Game/ModalData/MDA_Cube_Metal",
            material="Metal",
            num_modes=30
        )
    """
    unreal.log("=" * 60)
    unreal.log("  MODAL SOUND GENERATOR")
    unreal.log(f"  Mesh:     {mesh_asset_path}")
    unreal.log(f"  Material: {material}")
    unreal.log(f"  Modes:    {num_modes}")
    unreal.log("=" * 60)

    # Stage 1: Extract mesh geometry
    vertices, faces, meta = extract_static_mesh(mesh_asset_path)
    if not validate_mesh_for_fem(vertices, faces):
        raise RuntimeError("Mesh validation failed. See warnings above.")

# Adaptive subdivision based on mesh size and vertex density
    # Target: ~300 surface vertices for accurate low-frequency modes
    # Scale target with object volume — smaller objects need fewer verts
    # because their modes are higher frequency and easier to capture
    target_verts = 300
    bbox_vol = meta['bbox_volume']
    
    # Reduce target for small objects (< 0.01 m³ = 10cm cube equivalent)
    if bbox_vol < 0.01:
        target_verts = 150
    elif bbox_vol < 0.1:
        target_verts = 200

    if meta['n_surface_verts'] < target_verts:
        # Calculate how many subdivisions needed
        current = meta['n_surface_verts']
        subs = 0
        while current < target_verts and subs < 4:
            current = current * 4  # each subdivision ~4x triangles
            subs += 1
        
        unreal.log(
            f"Mesh has {meta['n_surface_verts']} vertices "
            f"(target {target_verts}) — subdividing {subs}x...")
        from modal_mesh_utils import subdivide_surface_mesh
        vertices, faces = subdivide_surface_mesh(
            vertices, faces, subdivisions=subs)
        meta['n_surface_verts'] = len(vertices)
        meta['n_triangles']     = len(faces)

    # Stage 2: Tetrahedralize
    tet_nodes, tets, n_surface_verts = tetrahedralize(
        vertices, faces, meta["bbox_extent"]
    )
    meta["n_surface_verts"] = n_surface_verts

    # Compute actual volume from tets
    meta["volume"] = sum(
        abs(np.linalg.det(np.array([
            tet_nodes[t[1]] - tet_nodes[t[0]],
            tet_nodes[t[2]] - tet_nodes[t[0]],
            tet_nodes[t[3]] - tet_nodes[t[0]]
        ]))) / 6.0
        for t in tets
    )

    # Stage 3: Assemble FEM matrices
    mat = MATERIAL_PRESETS[material]
    K, M = assemble_fem_matrices(
        tet_nodes, tets, mat["E"], mat["nu"], mat["rho"]
    )

    # Stage 4: Solve eigenvalue problem
    frequencies, mode_shapes = solve_modes(K, M, num_modes)

    # Stage 5: Extract surface participation factors
    participation = compute_vertex_participation(mode_shapes, n_surface_verts)

    # Stage 6: Compute Rayleigh damping
    damping = compute_rayleigh_damping(frequencies, material)

    # Stage 7: Write DataAsset
    asset = write_data_asset(
        output_asset_path, mesh_asset_path,
        frequencies, damping, participation,
        material, meta
    )

    unreal.log("=" * 60)
    unreal.log("  GENERATION COMPLETE")
    unreal.log("=" * 60)

    return asset


def generate_from_widget(mesh_path: str, output_path: str,
                          material: str, num_modes_str: str):
    """
    Entry point called from the Editor Utility Widget.
    Handles path cleanup and type conversion from Blueprint strings.
    """
    import importlib
    import modal_mesh_utils
    importlib.reload(modal_mesh_utils)

    # Blueprint passes soft reference paths with a suffix like
    # "/Game/Meshes/SM_Cube.SM_Cube" — strip it to get the asset path
    mesh_path   = mesh_path.split('.')[0]
    output_path = output_path.split('.')[0]

    # Blueprint Spin Box values come through as float strings e.g. "30.0"
    num_modes = int(float(num_modes_str))

    # Validate material name — default to Metal if something unexpected arrives
    valid_materials = ["Metal", "Wood", "Glass", "Stone", "Plastic", "Ceramic"]
    if material not in valid_materials:
        unreal.log_warning(f"Unknown material '{material}', defaulting to Metal.")
        material = "Metal"

    unreal.log(f"Widget called: mesh={mesh_path}, output={output_path}, "
               f"material={material}, modes={num_modes}")

    generate_modal_asset(mesh_path, output_path, material, num_modes)