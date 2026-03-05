"""
modal_mesh_utils.py

Mesh extraction utilities for the Modal Sound Generator.
Extracts vertex positions and triangle indices from UE5 StaticMesh assets
and provides helper functions for geometry processing.

The mesh data pipeline:
  UStaticMesh (UE5) 
    → MeshDescription (UE5 internal format)
    → numpy arrays (vertices, faces)
    → TetGen input format

Unit convention: UE5 uses centimeters internally.
All output from this module is converted to meters (SI units)
so that FEM matrix assembly produces correct frequencies in Hz.
"""

import unreal
import numpy as np


def extract_static_mesh(mesh_asset_path: str):
    """
    Extracts surface geometry from a UE5 StaticMesh asset.
    Uses StaticMeshDescription API (UE 5.7).

    Returns:
        vertices:  np.ndarray shape (N, 3), float64, in METERS
        faces:     np.ndarray shape (M, 3), int32, triangle indices
        meta:      dict with bounding box, vertex count, etc.
    """
    # ── Load asset ────────────────────────────────────────────────
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_asset_path)
    if not isinstance(mesh, unreal.StaticMesh):
        raise ValueError(
            f"Asset '{mesh_asset_path}' is not a StaticMesh. "
            f"Got: {type(mesh)}"
        )

    desc = mesh.get_static_mesh_description(0)
    if desc is None:
        raise RuntimeError(
            f"Could not get StaticMeshDescription for '{mesh_asset_path}'."
        )

    # ── Extract vertex positions ──────────────────────────────────
    n_verts = desc.get_vertex_count()
    if n_verts == 0:
        raise RuntimeError(f"Mesh '{mesh_asset_path}' has no vertices.")

    vertices = np.zeros((n_verts, 3), dtype=np.float64)
    for v_id in range(n_verts):
        pos = desc.get_vertex_position(unreal.VertexID(v_id))
        # UE5 uses centimeters — convert to meters for SI FEM units
        vertices[v_id, 0] = pos.x * 0.01
        vertices[v_id, 1] = pos.y * 0.01
        vertices[v_id, 2] = pos.z * 0.01

    # ── Extract faces via polygons ────────────────────────────────
    # We use get_polygon_vertices rather than get_triangle_vertices
    # because the triangle API returns malformed arrays in UE 5.7.
    # Polygons can be tris or quads — we triangulate manually.
    n_polys = desc.get_polygon_count()
    if n_polys == 0:
        raise RuntimeError(f"Mesh '{mesh_asset_path}' has no polygons.")

    triangles = []
    for p_id in range(n_polys):
        poly_verts = desc.get_polygon_vertices(unreal.PolygonID(p_id))
        # Extract integer id_value from each VertexID struct
        ids = [v.id_value for v in poly_verts]

        if len(ids) == 3:
            triangles.append(ids)
        elif len(ids) == 4:
            # Fan triangulation: quad ABCD → ABC + ACD
            triangles.append([ids[0], ids[1], ids[2]])
            triangles.append([ids[0], ids[2], ids[3]])
        elif len(ids) > 4:
            # General fan for n-gons
            for i in range(1, len(ids) - 1):
                triangles.append([ids[0], ids[i], ids[i+1]])
        # Skip degenerate polygons with < 3 verts

    if len(triangles) == 0:
        raise RuntimeError(f"Mesh '{mesh_asset_path}' produced no triangles.")

    faces = np.array(triangles, dtype=np.int32)

    # ── Clamp indices to valid range ──────────────────────────────
    max_idx = len(vertices) - 1
    if faces.max() > max_idx or faces.min() < 0:
        unreal.log_warning(
            f"Face indices out of range [{faces.min()}, {faces.max()}] "
            f"for {len(vertices)} vertices. Clamping."
        )
        faces = np.clip(faces, 0, max_idx)

    # ── Remove degenerate triangles ───────────────────────────────
    valid_mask = (
        (faces[:, 0] != faces[:, 1]) &
        (faces[:, 1] != faces[:, 2]) &
        (faces[:, 0] != faces[:, 2])
    )
    n_degen = int(np.sum(~valid_mask))
    if n_degen > 0:
        unreal.log_warning(f"Removed {n_degen} degenerate triangles.")
        faces = faces[valid_mask]

    # ── Metadata ──────────────────────────────────────────────────
    bbox_min    = vertices.min(axis=0)
    bbox_max    = vertices.max(axis=0)
    bbox_extent = bbox_max - bbox_min

    meta = {
        "n_surface_verts": n_verts,
        "n_triangles":     len(faces),
        "bbox_min":        bbox_min,
        "bbox_max":        bbox_max,
        "bbox_extent":     bbox_extent,
        "bbox_volume":     float(np.prod(bbox_extent)),
    }

    unreal.log(
        f"Mesh extracted: {n_verts} vertices, {len(faces)} triangles, "
        f"bbox {bbox_extent.round(3)} m"
    )

    return vertices, faces, meta


def validate_mesh_for_fem(vertices: np.ndarray, faces: np.ndarray) -> bool:
    """
    Checks that the mesh is suitable for tetrahedralization.
    Logs warnings for common issues.

    Returns True if mesh looks OK, False if likely to fail.
    """
    ok = True

    # Check for NaN or Inf in vertex positions
    if not np.all(np.isfinite(vertices)):
        unreal.log_error("Mesh contains NaN or Inf vertex positions.")
        ok = False

    # Check minimum size — meshes smaller than 1mm bounding box
    # suggest a unit error (e.g. forgot to scale from cm to m)
    bbox = vertices.max(axis=0) - vertices.min(axis=0)
    if bbox.max() < 0.001:
        unreal.log_error(
            f"Mesh bounding box is extremely small: {bbox} m. "
            f"Check that the mesh is not zero-scaled."
        )
        ok = False

    # Check vertex count — very low poly meshes may not tetrahedralize well
    if len(vertices) < 8:
        unreal.log_warning(
            f"Only {len(vertices)} vertices. "
            f"Very low-poly meshes produce few modes. Consider a denser mesh."
        )

    # Check face count
    if len(faces) < 4:
        unreal.log_error("Mesh has fewer than 4 triangles — cannot tetrahedralize.")
        ok = False

    return ok


def estimate_tet_volume_constraint(bbox_extent: np.ndarray,target_tets: int = 2000) -> float:
    """
    Estimates a reasonable maximum tetrahedron volume for TetGen.

    We target ~2000 tets as a balance between:
    - Accuracy: more tets = better mode shapes
    - Speed: assembly time scales with n_tets
    - Memory: eigenvectors scale with 3 * n_nodes

    For a thesis prototype, 1000-3000 tets gives good results
    for objects up to ~30cm. Larger objects may need more.

    Reference: convergence study in Ren et al. (2013) shows
    modal frequencies converge within 2% at ~1500 tets for
    simple shapes.
    """
    bbox_volume = float(np.prod(bbox_extent))
    # Each tet occupies roughly bbox_volume / target_tets
    max_vol = bbox_volume / target_tets
    # Clamp to avoid extremes
    max_vol = np.clip(max_vol, 1e-8, 1e-2)
    return float(max_vol)


def subdivide_surface_mesh(vertices: np.ndarray,
                            faces: np.ndarray,
                            subdivisions: int = 3) -> tuple:
    """
    Midpoint subdivision of a triangle mesh.
    Each pass splits every triangle into 4 smaller triangles.
    
    A cube with 8 verts/12 tris becomes:
      subdivisions=1 →  26 verts /  48 tris
      subdivisions=2 →  98 verts / 192 tris  
      subdivisions=3 → 386 verts / 768 tris  ← use this for FEM
    
    More vertices = finer tet mesh = accurate low-frequency modes.
    Reference: mesh refinement convergence in Ren et al. (2013).
    """
    for _ in range(subdivisions):
        edge_midpoints = {}
        new_vertices   = list(vertices)
        new_faces      = []

        def get_midpoint(i, j):
            key = (min(i, j), max(i, j))
            if key not in edge_midpoints:
                edge_midpoints[key] = len(new_vertices)
                new_vertices.append((vertices[i] + vertices[j]) / 2.0)
            return edge_midpoints[key]

        for tri in faces:
            a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
            ab = get_midpoint(a, b)
            bc = get_midpoint(b, c)
            ca = get_midpoint(c, a)
            new_faces += [
                [a,  ab, ca],
                [b,  bc, ab],
                [c,  ca, bc],
                [ab, bc, ca],
            ]

        vertices = np.array(new_vertices, dtype=np.float64)
        faces    = np.array(new_faces,    dtype=np.int32)

    unreal.log(
        f"Subdivision complete: "
        f"{len(vertices)} verts, {len(faces)} tris")
    return vertices, faces