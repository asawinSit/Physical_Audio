#pragma once

#include "CoreMinimal.h"
#include "Engine/DataAsset.h"
#include "ModalSoundDataAsset.generated.h"

// ============================================================================
// EImpactMaterial
// ============================================================================
// Must stay in sync with MATERIAL_PRESETS in modal_sound_generator.py.
// ============================================================================
UENUM(BlueprintType)
enum class EImpactMaterial : uint8
{
    Metal       UMETA(DisplayName = "Metal"),
    HeavyMetal  UMETA(DisplayName = "Heavy Metal"),
    Wood        UMETA(DisplayName = "Wood"),
    Glass       UMETA(DisplayName = "Glass"),
    Stone       UMETA(DisplayName = "Stone"),
    Plastic     UMETA(DisplayName = "Plastic"),
    Ceramic     UMETA(DisplayName = "Ceramic"),
};

// ============================================================================
// EFEMSolverType
// ============================================================================
// Records which offline solver produced this DataAsset. Needed so the runtime
// can adapt (e.g. shell objects have outward-facing modes on the surface only).
// Populated in write_data_asset. Ready for Shell FEM when implemented.
// ============================================================================
UENUM(BlueprintType)
enum class EFEMSolverType : uint8
{
    SolidTet    UMETA(DisplayName = "Solid Tetrahedral (default)"),
    Shell       UMETA(DisplayName = "Shell DKT (thin-walled objects)"),
};

// ============================================================================
// FModalMode
// ============================================================================
USTRUCT(BlueprintType)
struct FModalMode
{
    GENERATED_BODY()

    // Natural frequency of this mode in Hz (FEM eigensolve output).
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
    float Frequency = 0.f;

    // Rayleigh damping ratio ξ_k (dimensionless, typically 0.001–0.15).
    // Calibrated per-mode so ξ_high/ξ_low ≥ 7× for material realism.
    // Reference: Sterling et al. (2019) IEEE TVCG §4.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
    float DampingRatio = 0.01f;

    // Radiation efficiency η_k — normalised mean |surface-normal displacement|.
    // Modes where surface patches cancel each other radiate poorly.
    // Proxy for BEM-based FFAT maps. Floor 0.05 so no mode is silent.
    // Reference: NeuralSound §3.2 (Jin et al., SIGGRAPH 2022);
    //            Zheng & James (2011) ACM Trans. Graph.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
    float GlobalAmplitude = 1.f;

    // Per-surface-vertex participation factor φ_k(v), normalised [0,1].
    //
    // CRITICAL — ARRAY INDEX MEANING:
    //   Indexed by FEM surface mesh vertex order, which matches the order
    //   of FEMSurfaceVertexPositions in the parent DataAsset.
    //   NOT the render mesh LOD0 order. FindClosestVertex MUST search
    //   FEMSurfaceVertexPositions, not the render mesh, to get a valid index.
    //
    // Reference: van den Doel & Pai (1998) §3.2; O'Brien et al. (2002) §3.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
    TArray<float> VertexParticipation;

    // Drag / scrape sensitivity for continuous sliding excitation.
    // Amplitude of this mode during scraping ∝ DragSensitivity × TangentialSpeed.
    // Derived from radiation efficiency with a low-pass rolloff by frequency:
    //   DragSensitivity_k = η_k / (1 + (f_k / f_0)²)
    // so low modes dominate scraping (physically correct: scrape excitation
    // is a broadband noise shaped toward lower frequencies by contact compliance).
    // Reference: Rath & Rocchesso (2005) §3.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
    float DragSensitivity = 0.f;
};


// ============================================================================
// UModalSoundDataAsset
// ============================================================================
UCLASS()
class PHYSICAL_AUDIO_API UModalSoundDataAsset : public UDataAsset
{
    GENERATED_BODY()

public:

    // ── Source ───────────────────────────────────────────────────────────

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Source")
    TSoftObjectPtr<UStaticMesh> SourceMesh;

    // ── Modes ────────────────────────────────────────────────────────────

    // All computed modal data. Typically 20 modes.
    // Each mode: Frequency, DampingRatio, GlobalAmplitude (η_k),
    //            VertexParticipation (φ_k per FEM vertex), DragSensitivity.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Modes")
    TArray<FModalMode> Modes;

    // ── FEM Surface Vertex Positions ─────────────────────────────────────
    //
    // Local-space positions (METRES) of the FEM surface mesh vertices,
    // in the same order as VertexParticipation.
    //
    // WHY THIS FIELD EXISTS (critical fix):
    //   Previously, FindClosestVertex searched the render mesh LOD0 (which
    //   has a different vertex count than the FEM mesh after subdivision),
    //   then used the returned render-mesh index to look up VertexParticipation.
    //   For any subdivided mesh the indices were completely mismatched —
    //   a cube's render mesh has 8 verts, its FEM mesh has 386.
    //   The result: VertexParticipation[0..7] was always used regardless
    //   of where the object was struck. Spatial sound variation was broken.
    //
    //   Fix: store FEM vertex positions here. FindClosestVertex searches
    //   this array instead. Index is always valid for VertexParticipation.
    //
    //   Units: metres, local/object space (same coordinate frame as FEM).
    //   At runtime, transform world-space hit point to actor local space
    //   AND convert from cm to m (÷100) before comparing.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
    TArray<FVector> FEMSurfaceVertexPositions;

    // ── Material Parameters ──────────────────────────────────────────────

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
    EImpactMaterial MaterialType = EImpactMaterial::Metal;

    // Young's modulus E in Pascals (stored for reference / re-solve).
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
    float YoungModulus = 200e9f;

    // Poisson ratio ν (dimensionless).
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
    float PoissonRatio = 0.3f;

    // Density ρ in kg/m³.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
    float Density = 7800.f;

    // Hertz contact hardness — relative to steel (1.0).
    // Controls contact pulse duration T_c:  T_c ∝ 1/√(ContactHardness)
    // Harder = shorter pulse = more high-frequency energy in the impact.
    //
    // PREVIOUSLY: This was only on ModalImpactComponent (default 0.8)
    // and had no link to the DataAsset material. A Wood DataAsset actor
    // would use steel-like Hertz shaping unless manually overridden.
    //
    // FIX: Stored here so the correct value is always used automatically.
    // ModalImpactComponent reads DataAsset->ContactHardness at BeginPlay.
    //
    // Values: Metal=1.0, HeavyMetal=0.8, Glass=0.55, Stone=0.65,
    //         Wood=0.25, Plastic=0.18, Ceramic=0.60
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
    float ContactHardness = 1.0f;

    // ── FEM Solver Metadata ──────────────────────────────────────────────

    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "FEM")
    EFEMSolverType SolverType = EFEMSolverType::SolidTet;

    // Shell wall thickness in metres (only used if SolverType == Shell).
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "FEM")
    float ShellThickness = 0.003f;

    // ── Mesh Metadata ────────────────────────────────────────────────────

    // == FEMSurfaceVertexPositions.Num() == VertexParticipation.Num() per mode.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
    int32 SurfaceVertexCount = 0;

    // Sum of tet volumes in m³. Used for perceptual size comparison in bridge.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
    float MeshVolume = 0.f;

    // Object bounding box extent (X,Y,Z) in metres.
    UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
    FVector BoundingBoxExtent = FVector::ZeroVector;
};