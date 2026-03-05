#include "ModalMetaSoundBridge.h"
#include "Components/AudioComponent.h"
#include "Kismet/GameplayStatics.h"
#include "OfflinePipeline/ModalSoundDataAsset.h"

UModalMetaSoundBridge::UModalMetaSoundBridge(): ModalSoundAsset(nullptr), AttenuationSettings(nullptr)
{
    PrimaryComponentTick.bCanEverTick = false;
}

void UModalMetaSoundBridge::BeginPlay()
{
    Super::BeginPlay();

    ImpactComponent =
        GetOwner()->FindComponentByClass<UModalImpactComponent>();

    if (!ImpactComponent)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("ModalMetaSoundBridge: No ModalImpactComponent on '%s'"),
            *GetOwner()->GetName());
        return;
    }

    ImpactComponent->OnModalImpact.AddDynamic(
        this, &UModalMetaSoundBridge::HandleModalImpact);

    UE_LOG(LogTemp, Log,
        TEXT("ModalMetaSoundBridge ready on '%s'."),
        *GetOwner()->GetName());
}

void UModalMetaSoundBridge::HandleModalImpact(
    FVector               ImpactPoint,
    float                 KineticEnergy,
    int32                 VertexIndex,
    UModalSoundDataAsset* DataAsset)
{
    if (!ModalSoundAsset || !DataAsset || !ImpactComponent) return;

    UE_LOG(LogTemp, Log,
        TEXT("ModalBridge: Spawning sound. energy=%.3f vertex=%d"),
        KineticEnergy, VertexIndex);

    // Compute per-mode amplitudes from vertex participation * kinetic energy
    TArray<float> Amplitudes =
        ImpactComponent->ComputeModeAmplitudes(VertexIndex, KineticEnergy);

    // Spawn a short broadband thud at the impact point
    // This provides the low-frequency transient that modal synthesis lacks
    if (ImpactThudSound)
    {
        UGameplayStatics::SpawnSoundAtLocation(
            GetWorld(), ImpactThudSound, ImpactPoint,
            FRotator::ZeroRotator, 
            FMath::Clamp(KineticEnergy / 5000, 0.1f, 1.0f),
            1.0f, 0.f, AttenuationSettings);
    }

    // Spawn the MetaSound at the impact point (spatialized)
    UAudioComponent* AudioComp =
        UGameplayStatics::SpawnSoundAtLocation(
            GetWorld(), ModalSoundAsset, ImpactPoint,
            FRotator::ZeroRotator, 1.f, 1.f, 0.f,
            AttenuationSettings,
            nullptr, true);

    if (!AudioComp) return;

    // Build the three parameter arrays
    int32 NumModes = FMath::Min(DataAsset->Modes.Num(), MaxModes);

    TArray<float> Frequencies, Amps, Dampings;
    Frequencies.SetNum(NumModes);
    Amps.SetNum(NumModes);
    Dampings.SetNum(NumModes);

    for (int32 k = 0; k < NumModes; ++k)
    {
        const FModalMode& Mode = DataAsset->Modes[k];
        Frequencies[k] = Mode.Frequency;
        Dampings[k]    = Mode.DampingRatio;
        Amps[k]        = k < Amplitudes.Num() ? Amplitudes[k] : 0.f;
    }

    // Set parameters on the MetaSound — names must match
    // the Graph Input names you promoted in MS_ModalImpact
    AudioComp->SetFloatArrayParameter(FName("Frequencies"), Frequencies);
    AudioComp->SetFloatArrayParameter(FName("Amplitudes"),  Amps);
    AudioComp->SetFloatArrayParameter(FName("DampingRatios"), Dampings);
    AudioComp->SetFloatParameter     (FName("MasterGain"),  1.0f);

    // Fire the trigger to start synthesis
    AudioComp->SetTriggerParameter(FName("Trigger"));

    UE_LOG(LogTemp, Log, 
    TEXT("Bridge: %d modes, Freq[0]=%.1f Amp[0]=%.6f Damp[0]=%.6f"),
    NumModes, 
    Frequencies.Num() > 0 ? Frequencies[0] : 0.f,
    Amps.Num() > 0 ? Amps[0] : 0.f,
    Dampings.Num() > 0 ? Dampings[0] : 0.f);
}