"""
modal_mesh_utils.py  —  Mesh Extraction and Subdivision Utilities
=================================================================

PURPOSE
-------
Extracts surface geometry from UE5 StaticMesh assets and provides
adaptive subdivision to ensure sufficient vertex density for FEM.

UNIT CONVENTION
---------------
UE5 stores geometry in centimetres internally.
All output is converted to METRES (SI units) so that FEM produces
correct frequencies in Hz. This conversion (× 0.01) is applied in
extract_static_mesh. Forgetting it produces frequencies 100× too high
because modal frequencies scale as f ∝ 1/L (smaller object → higher freq).

SUBDIVISION RATIONALE
---------------------
The standard UE5 cube has 8 vertices and 12 triangles. This produces
only ~100–200 tets — too coarse for accurate modes. Midpoint subdivision
densifies the surface mesh without altering geometry (unlike Catmull-Clark
or Loop subdivision, which smooth the surface). After 3 passes a cube has
386 vertices and TetGen generates ~2000 tets, converging to within ~5% of
true frequencies for simple convex shapes (Ren et al. 2013).
"""

import unreal
import numpy as np


# ═══════════════════════════════════════════════════════════════════════════
# MESH EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════
def extract_static_mesh(mesh_asset_path: str):
    """
    Extracts surface geometry from a UE5 StaticMesh using the
    StaticMeshDescription API (UE 5.7).

    POLYGON API NOTE:
      We use get_polygon_vertices instead of get_triangle_vertices because
      the triangle API returns malformed arrays in UE 5.7. Polygons with
      more than 3 vertices are fan-triangulated: polygon ABCD...N produces
      triangles [A,B,C], [A,C,D], ..., [A,N-1,N].

    UNIT CONVERSION:
      pos.x/y/z (centimetres) × 0.01 = metres
      This is critical — the FEM stiffness matrix K scales as E·L and the
      mass matrix M scales as ρ·L³. In SI units these produce eigenvalues
      in rad²/s² → frequencies in Hz. In centimetres the frequencies would
      be 100× too high (f ∝ √(K/M) ∝ 1/L → 100× wrong for cm input).

    Returns:
        vertices:  (N, 3) float64 array, METRES
        faces:     (M, 3) int32 array, triangle vertex indices
        meta:      dict — bounding box info in metres, vertex/triangle counts
    """
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_asset_path)
    if not isinstance(mesh, unreal.StaticMesh):
        raise ValueError(f"'{mesh_asset_path}' is not a StaticMesh "
                         f"(got {type(mesh)})")

    desc = mesh.get_static_mesh_description(0)
    if desc is None:
        raise RuntimeError(
            f"No StaticMeshDescription for '{mesh_asset_path}'. "
            f"Ensure LOD0 exists and the mesh is not a procedural mesh.")

    n_verts = desc.get_vertex_count()
    if n_verts == 0:
        raise RuntimeError(f"'{mesh_asset_path}' has no vertices.")

    # ── Vertex positions ──────────────────────────────────────────
    vertices = np.zeros((n_verts, 3), dtype=np.float64)
    for v in range(n_verts):
        pos = desc.get_vertex_position(unreal.VertexID(v))
        vertices[v, 0] = pos.x * 0.01   # cm → m
        vertices[v, 1] = pos.y * 0.01
        vertices[v, 2] = pos.z * 0.01

    # ── Polygon/triangle extraction ───────────────────────────────
    n_polys = desc.get_polygon_count()
    if n_polys == 0:
        raise RuntimeError(f"'{mesh_asset_path}' has no polygons.")

    tris = []
    for p in range(n_polys):
        ids = [v.id_value for v in
               desc.get_polygon_vertices(unreal.PolygonID(p))]
        if len(ids) == 3:
            tris.append(ids)
        elif len(ids) == 4:
            tris += [[ids[0], ids[1], ids[2]],
                     [ids[0], ids[2], ids[3]]]
        elif len(ids) > 4:
            for i in range(1, len(ids)-1):
                tris.append([ids[0], ids[i], ids[i+1]])

    if not tris:
        raise RuntimeError(f"'{mesh_asset_path}' produced no triangles.")

    faces = np.array(tris, dtype=np.int32)

    # Clamp out-of-range indices (can occur with LOD or instanced meshes)
    max_idx = len(vertices) - 1
    if faces.max() > max_idx or faces.min() < 0:
        unreal.log_warning(
            f"[Mesh] Clamping face indices [{faces.min()},{faces.max()}] "
            f"to [0,{max_idx}]")
        faces = np.clip(faces, 0, max_idx)

    # Remove degenerate triangles (two or more identical vertex indices)
    valid = ((faces[:,0] != faces[:,1]) &
             (faces[:,1] != faces[:,2]) &
             (faces[:,0] != faces[:,2]))
    nd = int(np.sum(~valid))
    if nd: unreal.log_warning(f"[Mesh] Removed {nd} degenerate tris")
    faces = faces[valid]

    # ── Metadata ──────────────────────────────────────────────────
    bb_min = vertices.min(axis=0)
    bb_max = vertices.max(axis=0)
    ext    = bb_max - bb_min

    meta = {
        "n_surface_verts": n_verts,
        "n_triangles":     len(faces),
        "bbox_min":        bb_min,
        "bbox_max":        bb_max,
        "bbox_extent":     ext,
        "bbox_volume":     float(np.prod(ext)),
    }

    unreal.log(f"[Mesh] {n_verts} verts, {len(faces)} tris, "
               f"bbox {ext.round(3)} m")
    return vertices, faces, meta


# ═══════════════════════════════════════════════════════════════════════════
# MESH VALIDATION
# ═══════════════════════════════════════════════════════════════════════════
def validate_mesh_for_fem(vertices: np.ndarray, faces: np.ndarray) -> bool:
    """
    Checks that the mesh is suitable for tetrahedralization and FEM.

    Common failure modes and their causes:
      NaN/Inf positions       — corrupted mesh data or import error
      Tiny bounding box       — forgot cm→m conversion (bbox < 1 mm)
      Too few faces           — mesh is essentially a point or line
      Very few vertices       — low-poly mesh will produce few/inaccurate modes

    Returns True if mesh looks OK, False if likely to fail.
    """
    ok = True

    if not np.all(np.isfinite(vertices)):
        unreal.log_error("[Validate] NaN or Inf in vertex positions.")
        ok = False

    ext = vertices.max(axis=0) - vertices.min(axis=0)
    if ext.max() < 0.001:
        unreal.log_error(
            f"[Validate] Bounding box tiny: {ext} m. "
            f"Likely a unit error — check cm→m conversion in extract_static_mesh.")
        ok = False

    if len(faces) < 4:
        unreal.log_error("[Validate] Fewer than 4 triangles — cannot tetrahedralize.")
        ok = False

    if len(vertices) < 8:
        unreal.log_warning(
            f"[Validate] Only {len(vertices)} vertices — very coarse mesh, "
            f"subdivision will be applied.")

    return ok


# ═══════════════════════════════════════════════════════════════════════════
# TET VOLUME CONSTRAINT
# ═══════════════════════════════════════════════════════════════════════════
def estimate_tet_volume_constraint(bbox_extent: np.ndarray,
                                    target_tets: int = 2000) -> float:
    """
    Estimates a TetGen maxvolume constraint to produce approximately
    target_tets tetrahedra.

    Logic: if we divide the bounding box volume uniformly into target_tets
    tets, each tet has volume = bbox_vol / target_tets. Clamped to avoid
    extremes that cause TetGen to run for hours or produce degenerate meshes.

    Convergence reference: Ren et al. (2013) — modal frequencies converge
    within ~2% at 1500 tets for simple convex shapes.
    """
    vol = float(np.prod(bbox_extent))
    return float(np.clip(vol / target_tets, 1e-8, 1e-2))


# ═══════════════════════════════════════════════════════════════════════════
# MIDPOINT SUBDIVISION
# ═══════════════════════════════════════════════════════════════════════════
def subdivide_surface_mesh(vertices: np.ndarray,
                            faces:    np.ndarray,
                            subdivisions: int = 3) -> tuple:
    """
    Midpoint (1-to-4) triangle subdivision.

    Each pass inserts one midpoint on each edge and splits every triangle
    into 4 smaller triangles. This is geometry-preserving — new vertices
    lie exactly on the original mesh edges, so sharp features (corners,
    flat faces) are maintained. This is NOT Loop or Catmull-Clark subdivision
    (which smooth the mesh).

    Starting from the UE5 primitive cube (8 verts / 12 tris):
      1 pass:   26 verts /   48 tris
      2 passes: 98 verts /  192 tris
      3 passes: 386 verts / 768 tris  ← recommended for ~1 m object
      4 passes: 1538 verts / 3072 tris ← large objects only

    WHY THIS MATTERS:
      TetGen uses the surface mesh density to determine internal node
      spacing. A cube with only 8 surface nodes produces ~100–200 tets —
      too coarse for accurate low-frequency modes. After 3 subdivisions
      (386 nodes), TetGen generates ~2000 tets, reaching ~2–5% frequency
      accuracy for simple shapes (Ren et al. 2013).

      For complex shapes (stool, chair), the legs already have high
      curvature/thin cross-section that forces fine tet elements without
      needing as many subdivision passes. The adaptive logic in
      generate_modal_asset reduces target_verts for larger bbox volumes
      to avoid over-subdividing thin-featured meshes.

    Each pass:
      1. For each edge (i,j), compute midpoint (v_i + v_j) / 2
         and cache it by edge key (min(i,j), max(i,j)).
      2. Replace triangle [A,B,C] with [A,ab,ca], [B,bc,ab],
         [C,ca,bc], [ab,bc,ca] where ab,bc,ca are edge midpoints.
    """
    for pass_idx in range(subdivisions):
        edge_mid    = {}
        new_verts   = list(vertices)
        new_faces   = []

        def midpoint(i, j):
            key = (min(i,j), max(i,j))
            if key not in edge_mid:
                edge_mid[key] = len(new_verts)
                new_verts.append((vertices[i] + vertices[j]) * 0.5)
            return edge_mid[key]

        for tri in faces:
            a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
            ab = midpoint(a, b)
            bc = midpoint(b, c)
            ca = midpoint(c, a)
            new_faces += [[a, ab, ca],
                           [b, bc, ab],
                           [c, ca, bc],
                           [ab, bc, ca]]

        vertices = np.array(new_verts, dtype=np.float64)
        faces    = np.array(new_faces, dtype=np.int32)

        unreal.log(f"[Subdiv] Pass {pass_idx+1}/{subdivisions}: "
                   f"{len(vertices)} verts, {len(faces)} tris")

    return vertices, faces