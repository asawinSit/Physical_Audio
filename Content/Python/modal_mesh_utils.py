"""
modal_mesh_utils.py  —  Mesh Extraction, Subdivision, and Geometry Analysis
============================================================================

WHAT CHANGED IN THIS VERSION
------------------------------
Added:
  classify_geometry()        — auto-detects solid vs thin-shell geometry
  estimate_shell_thickness() — ray-cast from centroid to estimate wall thickness

These functions let generate_modal_asset automatically route thin/hollow
objects to the Kirchhoff plate FEM solver, giving physically correct
frequencies for plates, planks, cups, tubes, and bowls instead of
incorrect solid-tet results.

GEOMETRY CLASSIFIER LOGIC
--------------------------
  1. Sort bbox extents [thin, mid, thick].
     If thin/thick < 0.12 → flat/thin object → shell FEM.
     (catches plates: 0.02/0.30 = 0.07, planks: 0.02/2.0 = 0.01)
  2. Else: ray-cast from centroid to estimate wall thickness.
     If wall < 0.20 × thick → hollow object → shell FEM.
     (catches cups, bowls, tubes, vases)
  3. Otherwise → solid FEM (cubes, chairs, thick furniture, etc.)

UNIT CONVENTION (unchanged)
----------------------------
UE5 stores geometry in centimetres. All output is in METRES (× 0.01).
"""

import unreal
import numpy as np


# ═══════════════════════════════════════════════════════════════════════════
# MESH EXTRACTION
# ═══════════════════════════════════════════════════════════════════════════
def extract_static_mesh(mesh_asset_path: str):
    """
    Extracts surface geometry from a UE5 StaticMesh (LOD0).

    Uses get_polygon_vertices (not triangle API) — the triangle API returns
    malformed arrays in UE 5.7. Polygons > 3 verts are fan-triangulated.

    Returns:
        vertices:  (N, 3) float64, METRES
        faces:     (M, 3) int32
        meta:      dict with bbox, vertex and triangle counts
    """
    mesh = unreal.EditorAssetLibrary.load_asset(mesh_asset_path)
    if not isinstance(mesh, unreal.StaticMesh):
        raise ValueError(f"'{mesh_asset_path}' is not a StaticMesh "
                         f"(got {type(mesh)})")

    desc = mesh.get_static_mesh_description(0)
    if desc is None:
        raise RuntimeError(
            f"No StaticMeshDescription for '{mesh_asset_path}'. "
            f"Ensure LOD0 exists and mesh is not procedural.")

    n_verts = desc.get_vertex_count()
    if n_verts == 0:
        raise RuntimeError(f"'{mesh_asset_path}' has no vertices.")

    vertices = np.zeros((n_verts, 3), dtype=np.float64)
    for v in range(n_verts):
        pos = desc.get_vertex_position(unreal.VertexID(v))
        vertices[v, 0] = pos.x * 0.01   # cm → m
        vertices[v, 1] = pos.y * 0.01
        vertices[v, 2] = pos.z * 0.01

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
            tris += [[ids[0], ids[1], ids[2]], [ids[0], ids[2], ids[3]]]
        elif len(ids) > 4:
            for i in range(1, len(ids) - 1):
                tris.append([ids[0], ids[i], ids[i + 1]])

    if not tris:
        raise RuntimeError(f"'{mesh_asset_path}' produced no triangles.")

    faces = np.array(tris, dtype=np.int32)

    max_idx = len(vertices) - 1
    if faces.max() > max_idx or faces.min() < 0:
        unreal.log_warning(f"[Mesh] Clamping face indices to [0,{max_idx}]")
        faces = np.clip(faces, 0, max_idx)

    valid = ((faces[:, 0] != faces[:, 1]) &
             (faces[:, 1] != faces[:, 2]) &
             (faces[:, 0] != faces[:, 2]))
    nd = int(np.sum(~valid))
    if nd:
        unreal.log_warning(f"[Mesh] Removed {nd} degenerate tris")
    faces = faces[valid]

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
    ok = True

    if not np.all(np.isfinite(vertices)):
        unreal.log_error("[Validate] NaN or Inf in vertex positions.")
        ok = False

    ext = vertices.max(axis=0) - vertices.min(axis=0)
    if ext.max() < 0.001:
        unreal.log_error(
            f"[Validate] Bounding box tiny: {ext} m. "
            f"Check cm→m conversion in extract_static_mesh.")
        ok = False

    if len(faces) < 4:
        unreal.log_error("[Validate] Fewer than 4 triangles.")
        ok = False

    if len(vertices) < 8:
        unreal.log_warning(
            f"[Validate] Only {len(vertices)} vertices — subdivision needed.")

    return ok


# ═══════════════════════════════════════════════════════════════════════════
# GEOMETRY CLASSIFICATION
# ═══════════════════════════════════════════════════════════════════════════

# Aspect ratio below which object is definitely a thin flat shell
THIN_ASPECT_RATIO = 0.12

# Wall thickness / max_extent below which object is treated as hollow shell.
# Tightened from 0.20 to 0.08: a true hollow vessel (cup, bowl, tube) has
# walls that are < 8% of its max extent. A chair leg is ~3 cm on a 46 cm
# object = 6.5% — dangerously close — so the absolute cap below is critical.
HOLLOW_THICKNESS_RATIO = 0.08

# Absolute upper bound on shell thickness for the FLAT PLATE branch only
# (only reached when aspect < THIN_ASPECT_RATIO=0.12).
# Raised from 25mm to 70mm:
#   A 60mm structural plank (aspect=0.027) is a flat plate — shell FEM is
#   correct. Solid-tet CST shear-locks severely at aspect=0.027, producing
#   frequencies ~10× too high and wrong mode shapes.
#   Shell FEM (Kirchhoff-Love) is the right model for any flat plate regardless
#   of absolute thickness, as long as it is geometrically thin (aspect < 0.12).
#   Objects with aspect > 0.12 (chairs, beams, blocks) never reach this branch.
#   Hollow vessels are independently gated by HOLLOW_MAX_WALL_M=10mm, so
#   raising this limit does not affect cup/bowl/cylinder classification.
MAX_SHELL_THICKNESS_M = 0.070   # 70 mm — covers structural planks up to 70mm thick

# Absolute upper bound on wall thickness to be considered hollow.
# The ray-caster fires rays from the mesh centroid and measures the distance
# to the nearest surface hit. For thin-walled objects (cups, bowls, tubes,
# bent-panel chairs) this correctly measures the wall. For open-frame
# furniture where the centroid is mid-air, rays hit the nearest structural
# member face — which is the member's half-width, not a thin wall.
#
# Setting this at 25mm:
#   Cup/bowl wall (2–5mm):            ✓ shell (well under 25mm)
#   Thin-tube metal chair (14mm):     ✓ shell — correct, it IS thin-walled
#   Solid wood chair leg (~25mm rad): ✗ solid — just at/above limit ✓
#   Brick / stone block (>>25mm):     ✗ solid ✓
#
# Old value of 10mm was too conservative — it rejected the metal chair's
# 14mm measurement even though that IS the tube wall, not a leg diameter.
# The relative guard (wall < 0.08 × max_extent) is the primary defence
# against thick solid objects; this absolute cap catches only gross errors.
HOLLOW_MAX_WALL_M = 0.025   # 25 mm


def strip_isolated_vertices(vertices: np.ndarray,
                              faces:    np.ndarray) -> tuple:
    """
    Remove vertices not referenced by any face and remap face indices.

    pymeshfix sometimes returns a compacted vertex array that still contains
    unreferenced vertices. These contribute zero rows to the FEM mass matrix,
    making it exactly singular and causing eigsh to crash with splu failure.

    Returns compact (vertices, faces) with a contiguous index space.
    """
    used = np.unique(faces)
    if len(used) == len(vertices):
        return vertices, faces   # nothing to do

    n_removed = len(vertices) - len(used)
    remap = np.full(len(vertices), -1, dtype=np.int32)
    remap[used] = np.arange(len(used), dtype=np.int32)

    vertices_clean = vertices[used]
    faces_clean    = remap[faces]

    unreal.log(f"[Strip] Removed {n_removed} isolated vertices "
               f"({len(vertices)} → {len(vertices_clean)})")
    return vertices_clean, faces_clean


def classify_geometry(vertices: np.ndarray,
                       faces:    np.ndarray,
                       meta:     dict) -> dict:
    """
    Determines whether an object should use solid-tet or Kirchhoff shell FEM.

    Returns dict:
        "solver"    : "solid" | "shell"
        "thickness" : estimated shell/wall thickness in metres
        "reason"    : human-readable string (logged to console)

    WHY THIS MATTERS:
      SOLID FEM on thin objects fails in two ways:
        (a) TetGen produces extremely thin, ill-conditioned tets at high
            aspect ratios (e.g. a plate 2 m × 0.5 m × 0.02 m).
        (b) Even if TetGen succeeds, CST solid elements are too stiff in
            transverse bending — they give frequencies that are several times
            too high for thin plates and shells (Bathe 2006 §4.3.6).
      SHELL FEM solves both problems: it operates on the surface mesh directly
      (no TetGen needed) and uses Kirchhoff plate elements that natively capture
      bending as the dominant deformation mode.

    OBJECT GUIDE:
      Shell solver: plates, planks, sheets, cups, bowls, mugs, vases,
                    tubes/pipes, pots, pans, trays, bells, cymbals.
      Solid solver: cubes, spheres, books, bricks, solid chairs/stools,
                    thick furniture, cannonballs, stones.
    """
    ext        = meta["bbox_extent"]
    sorted_ext = np.sort(ext)           # [thinnest, mid, thickest]
    thin       = float(sorted_ext[0])
    thick      = float(sorted_ext[2])
    aspect     = thin / max(thick, 1e-6)

    # ── Test 1: flat/thin bounding box ───────────────────────────────────
    # Three conditions must ALL be true to classify as a thin flat plate:
    #   (a) aspect ratio < THIN_ASPECT_RATIO  (object is geometrically flat)
    #   (b) absolute thickness < MAX_SHELL_THICKNESS_M  (flat plate, not a brick)
    #   (c) mid/thin ratio (W/T) > 3.0  (plate-shaped, not a rod or square beam)
    #
    # Condition (c) prevents square cross-section rods (chair legs, bars) from
    # being misclassified. A chair leg (0.46m × 0.04m × 0.04m) has W/T=1.0 →
    # solid. A plank (2.2m × 0.216m × 0.06m) has W/T=3.6 → shell (correct).
    mid   = float(sorted_ext[1])
    W_T   = mid / max(thin, 1e-9)
    if aspect < THIN_ASPECT_RATIO and thin < MAX_SHELL_THICKNESS_M and W_T > 3.0:
        t = thin
        reason = (f"Thin flat object (aspect={aspect:.3f} < {THIN_ASPECT_RATIO}, "
                  f"t={t*100:.1f} cm < {MAX_SHELL_THICKNESS_M*100:.0f} cm cap, "
                  f"W/T={W_T:.1f} > 3): "
                  f"Shell FEM.")
        unreal.log(f"[Classify] {reason}")
        return {"solver": "shell", "thickness": t, "reason": reason}

    # ── Test 2: hollow object (cup, bowl, tube) ──────────────────────────
    # Both conditions must be true:
    #   (a) wall < HOLLOW_THICKNESS_RATIO × max_extent  (relative)
    #   (b) wall < HOLLOW_MAX_WALL_M = 10 mm            (absolute cap)
    # The absolute cap stops open-frame furniture (chairs, stools, tables)
    # being misclassified. A chair centroid sits in empty air between legs;
    # rays hit the legs at ~14 mm — a solid member, not a vessel wall.
    # True hollow objects (cups, bowls) have 2-5 mm walls, well under cap.
    wall_t = estimate_shell_thickness(vertices, faces, n_rays=64)
    hollow_threshold = HOLLOW_THICKNESS_RATIO * thick

    if (wall_t is not None
            and wall_t < hollow_threshold
            and wall_t < HOLLOW_MAX_WALL_M):
        reason = (f"Hollow object (wall={wall_t*100:.1f} cm < "
                  f"threshold={hollow_threshold*100:.1f} cm, "
                  f"< {HOLLOW_MAX_WALL_M*1000:.0f} mm cap). Shell FEM.")
        unreal.log(f"[Classify] {reason}")
        return {"solver": "shell", "thickness": wall_t, "reason": reason}

    # ── Default: solid ───────────────────────────────────────────────────
    wall_str = f"{wall_t*100:.1f} cm" if wall_t is not None else "n/a"
    reason = (f"Solid object (aspect={aspect:.3f}, wall={wall_str}). "
              f"Solid-tet FEM.")
    unreal.log(f"[Classify] {reason}")
    return {"solver": "solid", "thickness": thick, "reason": reason}


def estimate_shell_thickness(vertices: np.ndarray,
                              faces:    np.ndarray,
                              n_rays:   int = 64) -> float | None:
    """
    Estimates mean wall thickness by casting rays from centroid to surface.

    Uses Möller–Trumbore ray-triangle intersection vectorised over all faces.
    Takes median of nearest-hit distances across n_rays directions.

    For SOLID objects: rays hit the far outer surface → large t → classified solid.
    For HOLLOW objects (cups, bowls, tubes): rays hit thin walls → small t.
    For OPEN surfaces (open-top cup): many rays miss → treated as shell.

    Returns wall thickness in metres, or None if ray-casting is inconclusive.
    """
    centroid = vertices.mean(axis=0)

    rng  = np.random.default_rng(42)
    dirs = rng.standard_normal((n_rays, 3))
    nrms = np.linalg.norm(dirs, axis=1, keepdims=True)
    dirs = dirs / np.where(nrms < 1e-12, 1.0, nrms)

    v0 = vertices[faces[:, 0]]
    v1 = vertices[faces[:, 1]]
    v2 = vertices[faces[:, 2]]
    e1 = v1 - v0
    e2 = v2 - v0

    thicknesses = []
    for d in dirs:
        h    = np.cross(d, e2)
        det  = np.einsum('ij,ij->i', e1, h)
        mask = np.abs(det) > 1e-9

        if not np.any(mask):
            continue

        inv_det = np.where(mask, 1.0 / np.where(mask, det, 1.0), 0.0)
        s   = centroid - v0
        u   = np.einsum('ij,ij->i', s, h) * inv_det
        mask = mask & (u >= 0.0) & (u <= 1.0)

        q    = np.cross(s, e1)          # (M, 3)
        # d is (3,), q is (M,3): dot each row of q with d → (M,)
        v_b  = np.einsum('j,ij->i', d, q) * inv_det
        mask = mask & (v_b >= 0.0) & (u + v_b <= 1.0)

        # e2 is (M,3), q is (M,3): row-wise dot product → (M,)
        t    = np.einsum('ij,ij->i', e2, q) * inv_det
        t    = np.where(mask & (t > 1e-6), t, np.inf)
        t_min = float(np.min(t))

        if np.isfinite(t_min):
            thicknesses.append(t_min)

    if len(thicknesses) < n_rays // 4:
        unreal.log_warning(
            f"[Classify] Only {len(thicknesses)}/{n_rays} ray hits "
            f"(open surface?) — treating as shell.")
        return 0.01 if not thicknesses else float(np.median(thicknesses))

    return float(np.median(thicknesses))


# ═══════════════════════════════════════════════════════════════════════════
# TET VOLUME CONSTRAINT
# ═══════════════════════════════════════════════════════════════════════════
def estimate_tet_volume_constraint(bbox_extent: np.ndarray,
                                    target_tets: int = 2000) -> float:
    """
    Estimates TetGen maxvolume to produce ~target_tets tets.
    Reference: Ren et al. (2013) — ~2% frequency accuracy at 1500 tets.
    """
    vol = float(np.prod(bbox_extent))
    return float(np.clip(vol / target_tets, 1e-8, 1e-2))


# ═══════════════════════════════════════════════════════════════════════════
# MIDPOINT SUBDIVISION
# ═══════════════════════════════════════════════════════════════════════════
def subdivide_surface_mesh(vertices:     np.ndarray,
                            faces:        np.ndarray,
                            subdivisions: int = 3) -> tuple:
    """
    Geometry-preserving midpoint (1-to-4) subdivision. Does NOT smooth surface.

    Cube vertex progression:
      0 passes:   8 verts /  12 tris
      1 pass:    26 verts /  48 tris
      2 passes:  98 verts / 192 tris
      3 passes: 386 verts / 768 tris  ← recommended for ~1 m solid objects
    """
    for pass_idx in range(subdivisions):
        edge_mid  = {}
        new_verts = list(vertices)
        new_faces = []

        def midpoint(i, j):
            key = (min(i, j), max(i, j))
            if key not in edge_mid:
                edge_mid[key] = len(new_verts)
                new_verts.append((vertices[i] + vertices[j]) * 0.5)
            return edge_mid[key]

        for tri in faces:
            a, b, c = int(tri[0]), int(tri[1]), int(tri[2])
            ab = midpoint(a, b)
            bc = midpoint(b, c)
            ca = midpoint(c, a)
            new_faces += [[a, ab, ca], [b, bc, ab],
                          [c, ca, bc], [ab, bc, ca]]

        vertices = np.array(new_verts, dtype=np.float64)
        faces    = np.array(new_faces, dtype=np.int32)
        unreal.log(f"[Subdiv] Pass {pass_idx + 1}/{subdivisions}: "
                   f"{len(vertices)} verts, {len(faces)} tris")

    return vertices, faces


# ═══════════════════════════════════════════════════════════════════════════
# MESH REPAIR
# ═══════════════════════════════════════════════════════════════════════════
def repair_mesh_for_fem(vertices: np.ndarray, faces: np.ndarray) -> tuple:
    """
    Repairs self-intersections and holes using pymeshfix (Attene 2010).
    Install: pip install pymeshfix --break-system-packages
    """
    try:
        import pymeshfix
    except ImportError:
        unreal.log_warning(
            "[Repair] pymeshfix not installed. "
            "Install: pip install pymeshfix --break-system-packages")
        return vertices, faces

    unreal.log(f"[Repair] {len(vertices)} verts, {len(faces)} faces …")
    v_c, f_c = pymeshfix.clean_from_arrays(
        vertices.astype(np.float64), faces.astype(np.int32))
    v_c = np.array(v_c, dtype=np.float64)
    f_c = np.array(f_c, dtype=np.int32)
    unreal.log(f"[Repair] Done: {len(v_c)} verts, {len(f_c)} faces")
    return v_c, f_c