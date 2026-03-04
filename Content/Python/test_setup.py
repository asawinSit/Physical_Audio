import unreal

def run_all_checks():

    checks = []

    try:
        import numpy as np
        checks.append(("numpy",   True,  f"version {np.__version__}"))
    except Exception as e:
        checks.append(("numpy",   False, str(e)))

    try:
        import scipy
        from scipy.sparse.linalg import eigsh
        checks.append(("scipy",   True,  f"version {scipy.__version__}"))
    except Exception as e:
        checks.append(("scipy",   False, str(e)))

    try:
        import tetgen
        # Actually instantiate it to confirm C extension loads
        tg = tetgen.TetGen(
            __import__('numpy').array([[0,0,0],[1,0,0],[0,1,0],[0,0,1]], dtype=float),
            __import__('numpy').array([[0,1,2],[0,1,3],[0,2,3],[1,2,3]])
        )
        checks.append(("tetgen",  True,  "C extension loaded OK"))
    except Exception as e:
        checks.append(("tetgen",  False, str(e)))

    try:
        import meshio
        checks.append(("meshio",  True,  f"version {meshio.__version__}"))
    except Exception as e:
        checks.append(("meshio",  False, str(e)))

    try:
        _ = unreal.EditorAssetLibrary
        checks.append(("EditorAssetLibrary",  True,  "accessible"))
    except Exception as e:
        checks.append(("EditorAssetLibrary",  False, str(e)))

    try:
        _ = unreal.AssetToolsHelpers.get_asset_tools()
        checks.append(("AssetToolsHelpers",   True,  "accessible"))
    except Exception as e:
        checks.append(("AssetToolsHelpers",   False, str(e)))

    try:
        _ = unreal.StaticMeshEditorSubsystem
        checks.append(("StaticMeshEditorSubsystem", True, "accessible"))
    except Exception as e:
        checks.append(("StaticMeshEditorSubsystem", False, str(e)))

    try:
        import sys
        content_python_on_path = any("Content" in p for p in sys.path)
        checks.append(("Content/Python on sys.path", content_python_on_path,
                        "found" if content_python_on_path else "NOT FOUND — check Editor Preferences → Python"))
    except Exception as e:
        checks.append(("sys.path check", False, str(e)))

    # ── Print results ──────────────────────────────────────────
    unreal.log("=" * 55)
    unreal.log("  MODAL SOUND GENERATOR — ENVIRONMENT CHECK")
    unreal.log("=" * 55)
    all_ok = True
    for name, ok, detail in checks:
        tag = " OK " if ok else "FAIL"
        unreal.log(f"  [{tag}]  {name}: {detail}")
        if not ok:
            all_ok = False
    unreal.log("=" * 55)
    if all_ok:
        unreal.log("  All checks passed. Proceed to Step 1 (C++ DataAsset).")
    else:
        unreal.log_warning("  One or more checks failed. Fix before continuing.")
    unreal.log("=" * 55)

run_all_checks()