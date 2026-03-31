// Fill out your copyright notice in the Description page of Project Settings.


#include "SampleBasedAudioComponent.h"
#include "Kismet/GameplayStatics.h"
#include "RuntimePipeline/ModalImpactComponent.h"
#include "Components/ActorComponent.h"
#include "Components/AudioComponent.h"

USampleBasedAudioComponent::USampleBasedAudioComponent()
{
    // Tick needed to advance TimeSinceLastImpact for post-impact scrape suppression.
    PrimaryComponentTick.bCanEverTick = true;
}

void USampleBasedAudioComponent::BeginPlay()
{
    Super::BeginPlay();

    ImpactComp = GetOwner()->FindComponentByClass<UModalImpactComponent>();
    if (!ImpactComp)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalBridge] '%s': No ModalImpactComponent found."),
            *GetOwner()->GetName());
        return;
    }

   // ImpactComp->OnModalImpact.AddDynamic(this, &USampleBasedAudioComponent::HandleImpact);
    //ImpactComp->OnModalSlide.AddDynamic(this,  &USampleBasedAudioComponent::HandleSlide);

    UE_LOG(LogTemp, Log,
        TEXT("[ModalBridge] '%s' ready (v2 — continuous resonator bank)"),
        *GetOwner()->GetName());
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
}

// ─────────────────────────────────────────────────────────────────────────────
// IMPACT
// ─────────────────────────────────────────────────────────────────────────────
void USampleBasedAudioComponent::HandleImpact(
    FVector               ImpactPoint,
    float                 KineticEnergy,
    float                 RelativeSpeed,
    FVector               ImpactNormal)
{
    float ContactGain = FMath::Clamp(
        FMath::Sqrt(RelativeSpeed / 4.0f),
        0.15f, 0.95f);

    UAudioComponent* Audio = UGameplayStatics::SpawnSoundAtLocation(
        GetWorld(),
        SoundAsset,
       ImpactPoint,
        FRotator::ZeroRotator,
        1.f, 1.f, 0.f,
        nullptr,
        nullptr,
        true);

    if (!Audio)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[USampleBasedAudioComponent] SpawnSoundAtLocation returned null."));
        return;
    }
  
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
    if (!bScrapeInitialised)
        InitScrapeAudio();

    if (!ScrapeAudio) return;

    ScrapeAudio->SetWorldLocation(ContactPoint);

    float SpeedNorm      = FMath::Clamp(TangentialSpeed / MaxScrapeSpeed, 0.f, 1.f);
    float TargetGain     = SpeedNorm * ScrapeGainScale;

    // Post-impact scrape suppression: ramp from 0→full over PostImpactSuppressTime.
    if (TimeSinceLastImpact < PostImpactSuppressTime)
    {
        float Ramp = FMath::Clamp(TimeSinceLastImpact / PostImpactSuppressTime, 0.f, 1.f);
        TargetGain *= Ramp;
    }

  
    SmoothedScrapeGain = FMath::Lerp(SmoothedScrapeGain, TargetGain, ScrapeGainSmoothing);
    float CurrentScrapeGain = SmoothedScrapeGain;

   // TArray<float> ScrapeAmps = ImpactComp->ComputeScrapeAmplitudes(
  //      VertexIndex, TangentialSpeed, NormalForce);
    
    const float f1 = 44.f;
    const float Fc = FMath::Clamp(
        f1 * (0.15f + SpeedNorm * 0.85f),
        80.f,
        f1);
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
         
            bScrapeInitialised = true;
        }
    
    }
    

void USampleBasedAudioComponent::EnableListener()
{
    ImpactComp->OnSampleBasedImpact.AddDynamic(this, &USampleBasedAudioComponent::HandleImpact);
    ImpactComp->OnSampleBasedSlide.AddDynamic(this,  &USampleBasedAudioComponent::HandleSlide);
}

void USampleBasedAudioComponent::DisableListener()
{
    ImpactComp->OnSampleBasedImpact.RemoveDynamic(this, &USampleBasedAudioComponent::HandleImpact);
    ImpactComp->OnSampleBasedSlide.RemoveDynamic(this,  &USampleBasedAudioComponent::HandleSlide);
}