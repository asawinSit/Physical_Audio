// ============================================================================
// ModalMetaSoundBridge.cpp
// ============================================================================

#include "ModalMetaSoundBridge.h"
#include "OfflinePipeline/ModalSoundDataAsset.h"
#include "Components/AudioComponent.h"
#include "Kismet/GameplayStatics.h"

UModalMetaSoundBridge::UModalMetaSoundBridge()
{
    PrimaryComponentTick.bCanEverTick = false;
}

void UModalMetaSoundBridge::BeginPlay()
{
    Super::BeginPlay();

    ImpactComp = GetOwner()->FindComponentByClass<UModalImpactComponent>();
    if (!ImpactComp)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalBridge] '%s': No ModalImpactComponent found. "
                 "Add one to the same actor."),
            *GetOwner()->GetName());
        return;
    }

    ImpactComp->OnModalImpact.AddDynamic(
        this, &UModalMetaSoundBridge::HandleImpact);

    UE_LOG(LogTemp, Log, TEXT("[ModalBridge] '%s' ready"),
           *GetOwner()->GetName());
}

void UModalMetaSoundBridge::HandleImpact(
    FVector               ImpactPoint,
    float                 KineticEnergy,
    float                 RelativeSpeed,
    int32                 VertexIndex,
    UModalSoundDataAsset* DataAsset,
    FVector               ImpactNormal)
{
    if (!ModalSoundAsset || !DataAsset || !ImpactComp) return;

    // ── Compute per-mode amplitudes ───────────────────────────────────────
    // ComputeModeAmplitudes applies the full physical amplitude model:
    //   φ_k(vertex) × √(KE/KE_max) × η_k × F_k(speed) × jitter
    // where F_k is the Hertz contact spectral weight computed from the
    // actual impact speed RelativeSpeed (Chadwick et al. 2012).
    TArray<float> Amplitudes = ImpactComp->ComputeModeAmplitudes(
        VertexIndex, KineticEnergy, RelativeSpeed, ImpactNormal);

    // ── Build parameter arrays ────────────────────────────────────────────
    int32 N = FMath::Min(DataAsset->Modes.Num(), MaxModes);

    TArray<float> Freqs, Amps, Damps;
    Freqs.SetNum(N); Amps.SetNum(N); Damps.SetNum(N);

    for (int32 k = 0; k < N; ++k)
    {
        const FModalMode& M = DataAsset->Modes[k];
        Freqs[k] = M.Frequency;
        Damps[k] = M.DampingRatio;
        Amps[k]  = Amplitudes.IsValidIndex(k) ? Amplitudes[k] : 0.f;
    }

    UE_LOG(LogTemp, Log,
        TEXT("[ModalBridge] KE=%.1f J speed=%.2f m/s vtx=%d "
             "modes=%d Amp[0]=%.4f"),
        KineticEnergy, RelativeSpeed, VertexIndex, N,
        Amps.Num() > 0 ? Amps[0] : 0.f);

    // ── Spawn spatialized audio at impact location ────────────────────────
    // SpawnSoundAtLocation creates a UAudioComponent at ImpactPoint in
    // world space, using AttenuationSettings for 3D audio.
    // bAutoDestroy=true ensures cleanup after the MetaSound finishes.
    UAudioComponent* Audio = UGameplayStatics::SpawnSoundAtLocation(
        GetWorld(),
        ModalSoundAsset,
        ImpactPoint,
        FRotator::ZeroRotator,
        1.f,   // VolumeMultiplier — use MasterGain parameter instead
        1.f,   // PitchMultiplier
        0.f,   // StartTime
        AttenuationSettings,
        nullptr,
        true); // bAutoDestroy

    if (!Audio)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalBridge] SpawnSoundAtLocation returned null. "
                 "Check ModalSoundAsset is assigned and valid."));
        return;
    }

    // ── Set MetaSound parameters ──────────────────────────────────────────
    // IMPORTANT: All value parameters must be set BEFORE Trigger.
    // The MetaSound node reads parameter values at the trigger event.
    // Setting Trigger first means the node reads the previous frame's values.
    Audio->SetFloatArrayParameter(FName("Frequencies"),   Freqs);
    Audio->SetFloatArrayParameter(FName("Amplitudes"),    Amps);
    Audio->SetFloatArrayParameter(FName("DampingRatios"), Damps);
    Audio->SetFloatParameter     (FName("MasterGain"),    MasterGain);
    Audio->SetTriggerParameter   (FName("Trigger"));      // MUST be last
}