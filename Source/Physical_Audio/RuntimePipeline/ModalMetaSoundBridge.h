#pragma once
// ============================================================================
// ModalMetaSoundBridge.h
// ============================================================================
//
// PURPOSE
// -------
// Receives FOnModalImpact delegates from UModalImpactComponent and drives
// the MS_ModalImpact MetaSound graph with per-impact parameter arrays.
//
// RESPONSIBILITY BOUNDARY
// -----------------------
// This component is responsible for:
//   - Spawning a spatialized UAudioComponent at the impact location
//   - Setting Frequencies, Amplitudes, DampingRatios, MasterGain
//   - Firing the Trigger to start synthesis
//
// The amplitude computation (including all physical model terms) is done
// in UModalImpactComponent::ComputeModeAmplitudes, not here. This keeps
// the physics model in one place.
//
// METASOUND PARAMETER NAMING
// --------------------------
// Parameter names must exactly match the Graph Input node names in
// MS_ModalImpact. These names are set when you right-click an input pin
// in the MetaSound editor and choose "Promote to Graph Input":
//   "Frequencies"   — float array, Hz
//   "Amplitudes"    — float array, [0,1]
//   "DampingRatios" — float array, [0,0.5]
//   "MasterGain"    — float scalar
//   "Trigger"       — trigger (no value) — must be set LAST
//
// ATTENUATION SETUP
// -----------------
// Without an AttenuationSettings asset, sound plays as 2D (full volume
// everywhere, no panning). Create a SoundAttenuation asset with:
//   Inner Radius:       100 cm  (full volume within 1 m)
//   Falloff Distance:   5000 cm (fade to silence at 50 m)
//   Distance Algorithm: Natural Sound (logarithmic rolloff)
//   Spatialization:     Enabled, Binaural if available
// ============================================================================

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "ModalImpactComponent.h"
#include "ModalMetaSoundBridge.generated.h"

UCLASS(ClassGroup=(Audio), meta=(BlueprintSpawnableComponent),
       DisplayName="Modal MetaSound Bridge")
class PHYSICAL_AUDIO_API UModalMetaSoundBridge : public UActorComponent
{
    GENERATED_BODY()

public:
    UModalMetaSoundBridge();

    // ── CONFIGURATION ──────────────────────────────────────────────────────

    /** The MS_ModalImpact MetaSound Source asset. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
    USoundBase* ModalSoundAsset;

    /**
     * Sound attenuation for 3D spatialization and distance falloff.
     * Create asset SA_ModalImpact. Without this, all impacts are 2D.
     * See header comment above for recommended settings.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
    USoundAttenuation* AttenuationSettings;

    /**
     * Maximum modes passed to MetaSound. Cap at DataAsset mode count.
     * 20–30 is perceptually adequate; diminishing returns above ~30
     * (Cook 2002, van den Doel & Pai 1998).
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound",
              meta=(ClampMin="1", ClampMax="40"))
    int32 MaxModes = 20;

    /**
     * Master gain applied to all amplitudes in MetaSound.
     * Use to adjust overall loudness without touching the DataAsset.
     * Default 1.0. Raise if sound is too quiet, lower if clipping.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound",
              meta=(ClampMin="0.0", ClampMax="8.0"))
    float MasterGain = 1.0f;

protected:
    virtual void BeginPlay() override;

private:
    UFUNCTION()
    void HandleImpact(FVector               ImpactPoint,
                       float                 KineticEnergy,
                       float                 RelativeSpeed,
                       int32                 VertexIndex,
                       UModalSoundDataAsset* DataAsset,
                       FVector               ImpactNormal);

    UPROPERTY()
    UModalImpactComponent* ImpactComp = nullptr;
};