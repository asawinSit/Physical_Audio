// Fill out your copyright notice in the Description page of Project Settings.


#include "SampleBasedAudioComponent.h"
#include "Kismet/GameplayStatics.h"
#include "RuntimePipeline/ModalImpactComponent.h"
#include "Components/ActorComponent.h"
#include "Components/AudioComponent.h"

USampleBasedAudioComponent::USampleBasedAudioComponent()
{
    PrimaryComponentTick.bCanEverTick = true;
}

void USampleBasedAudioComponent::BeginPlay()
{
    Super::BeginPlay();
    ImpactComp = GetOwner()->FindComponentByClass<UModalImpactComponent>();
    ImpactComp->OnSampleBasedImpact.AddDynamic(this, &USampleBasedAudioComponent::HandleImpact);
    ImpactComp->OnSampleBasedSlide.AddDynamic(this,  &USampleBasedAudioComponent::HandleSlide);
}

void USampleBasedAudioComponent::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    if (ScrapeAudio && ScrapeAudio->IsPlaying())
        ScrapeAudio->Stop();
    Super::EndPlay(EndPlayReason);
}

void USampleBasedAudioComponent::TickComponent(float DeltaTime, ELevelTick TickType,
                                           FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    TimeSinceLastImpact += DeltaTime;
    
    
    if (bScrapeInitialised && ScrapeAudio && SmoothedScrapeGain > 0.001f)
    {
        if (!bSlideActiveThisFrame)
        {
            SmoothedScrapeGain = FMath::Lerp(SmoothedScrapeGain, 0.f, ScrapeGainSmoothing);
            ScrapeAudio->SetFloatParameter(FName("ScrapeGain"), SmoothedScrapeGain);
        }
        bSlideActiveThisFrame = false;
    }
}


void USampleBasedAudioComponent::HandleImpact(
    FVector   ImpactPoint,
    float     KineticEnergy,
    float     RelativeSpeed,
    FVector   ImpactNormal)
{
    if (!bListenerEnabled) return;
    
    if (ImpactSoundWaves.IsEmpty()) return;
    
    UAudioComponent* Audio = UGameplayStatics::SpawnSoundAtLocation(
        GetWorld(),
        SoundAsset,
        ImpactPoint,
        FRotator::ZeroRotator,
       1,   // VolumeMultiplier  ← was hardcoded 1.f
        1,    // PitchMultiplier   ← was hardcoded 1.f
        0.f,
        nullptr,
        nullptr,
        true);

    if (!Audio)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[USampleBasedAudioComponent] SpawnSoundAtLocation returned null."));
        return;
    }

    float DynamicVolume = MasterGain * FMath::Clamp(FMath::Pow(RelativeSpeed / 50.0f, 0.6f), 0.03f, 3.0f);
  
    Audio->SetObjectArrayParameter(FName("Sounds"), ImpactSoundWaves);
    
    Audio->SetFloatParameter(FName("KineticEnergy"), KineticEnergy);

    Audio->SetFloatParameter(FName("ImpactLoudness"), DynamicVolume);
    
    Audio->SetTriggerParameter(FName("Trigger"));  // MUST be last
    TimeSinceLastImpact = 0.f;
}

// ─────────────────────────────────────────────────────────────────────────────
// SCRAPE — called each tick while sliding
// ─────────────────────────────────────────────────────────────────────────────
void USampleBasedAudioComponent::HandleSlide(
    float                 TangentialSpeed,
    float                 NormalForce,
    FVector               ContactPoint)
{
    if (!bListenerEnabled) return;
    if(!ScrapeSoundAsset) return;
    
    if (!bScrapeInitialised)
        InitScrapeAudio();

    if (!ScrapeAudio) return;

    ScrapeAudio->SetWorldLocation(ContactPoint);

    float SpeedNorm      = FMath::Clamp(TangentialSpeed / MaxScrapeSpeed, 0.f, 1.f);
    float TargetGain     = SpeedNorm * ScrapeGainScale;
    
    SmoothedScrapeGain = FMath::Lerp(SmoothedScrapeGain, TargetGain, ScrapeGainSmoothing);
    float CurrentScrapeGain = SmoothedScrapeGain;

   // TArray<float> ScrapeAmps = ImpactComp->ComputeScrapeAmplitudes(
  //      VertexIndex, TangentialSpeed, NormalForce);
    
    const float f1 = 440.f;
    const float Fc = FMath::Clamp(
        f1 * (0.15f + SpeedNorm * 0.85f),
        80.f,
        f1);
    ScrapeAudio->SetWaveParameter(FName("Sound"), ScrapeSoundWave);
    ScrapeAudio->SetFloatParameter(FName("ScrapeFilterFc"), Fc);
    ScrapeAudio->SetFloatParameter     (FName("ScrapeGain"),        CurrentScrapeGain);
    // ^^^ No SetTriggerParameter here. Ever. That was the v1 bug.
}

   
    void USampleBasedAudioComponent::InitScrapeAudio()
    {
        if (bScrapeInitialised || !ScrapeSoundAsset) return;

        ScrapeAudio = UGameplayStatics::SpawnSoundAttached(
            ScrapeSoundAsset,
            GetOwner()->GetRootComponent(),
            NAME_None,
            FVector::ZeroVector,
            EAttachLocation::KeepRelativeOffset,
            false,  // do NOT auto destroy
            1.f, 1.f, 0.f,
            nullptr,
            nullptr,
            true);  // auto activate — graph starts looping immediately

        if (ScrapeAudio)
        {
            // Prime silent until first slide event
            ScrapeAudio->SetFloatParameter(FName("ScrapeGain"),  0.f);
            ScrapeAudio->SetFloatParameter(FName("ScrapeSpeed"), 0.f);
            ScrapeAudio->SetFloatParameter(FName("MasterGain"), MasterScrapeGain);  
            bScrapeInitialised = true;
        }
    
    }
    

void USampleBasedAudioComponent::EnableListener()
{
    bListenerEnabled = true;
}

void USampleBasedAudioComponent::DisableListener()
{
    bListenerEnabled = false;
}