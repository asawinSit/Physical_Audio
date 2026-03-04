#pragma once

#include "CoreMinimal.h"
#include "Engine/DataAsset.h"
#include "ModalSoundDataAsset.generated.h"

UENUM(BlueprintType)
enum class EImpactMaterial : uint8
{
	Metal       UMETA(DisplayName = "Metal"),
	Wood        UMETA(DisplayName = "Wood"),
	Glass       UMETA(DisplayName = "Glass"),
	Stone       UMETA(DisplayName = "Stone"),
	Plastic     UMETA(DisplayName = "Plastic"),
	Ceramic     UMETA(DisplayName = "Ceramic")
};

USTRUCT(BlueprintType)
struct FModalMode
{
	GENERATED_BODY()

	// Natural frequency of this mode in Hz
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
	float Frequency = 0.f;

	// Rayleigh damping ratio xi_k (dimensionless, 0.001 – 0.05)
	// Controls how quickly this mode decays after impact
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
	float DampingRatio = 0.01f;

	// Global amplitude weight for this mode (normalized 0-1)
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
	float GlobalAmplitude = 1.f;

	// Per-surface-vertex participation factor phi_k(v).
	// Index matches the StaticMesh LOD0 vertex buffer order.
	// At runtime: when struck at vertex v, this mode is excited
	// with amplitude = VertexParticipation[v] * ImpactForce.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mode")
	TArray<float> VertexParticipation;
};


UCLASS()
class PHYSICAL_AUDIO_API UModalSoundDataAsset : public UDataAsset
{
	GENERATED_BODY()

public:

	// ── Source ───────────────────────────────────────────────────
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Source")
	TSoftObjectPtr<UStaticMesh> SourceMesh;

	// ── Modes ────────────────────────────────────────────────────
	// Computed by FEM eigenvalue solve. Typically 20-40 modes.
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Modes")
	TArray<FModalMode> Modes;

	// ── Material Parameters ──────────────────────────────────────
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
	EImpactMaterial MaterialType = EImpactMaterial::Metal;

	// Young's modulus E in Pascals
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
	float YoungModulus = 200e9f;

	// Poisson ratio nu (dimensionless)
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
	float PoissonRatio = 0.3f;

	// Density rho in kg/m³
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Material")
	float Density = 7800.f;

	// ── Mesh Metadata ────────────────────────────────────────────
	// Stored so the runtime system can sanity-check vertex indices
	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
	int32 SurfaceVertexCount = 0;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
	float MeshVolume = 0.f;

	UPROPERTY(EditAnywhere, BlueprintReadOnly, Category = "Mesh Info")
	FVector BoundingBoxExtent = FVector::ZeroVector;
};
