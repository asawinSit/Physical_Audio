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
  
}

// ─────────────────────────────────────────────────────────────────────────────
// SCRAPE — called each tick while sliding
// ─────────────────────────────────────────────────────────────────────────────
void USampleBasedAudioComponent::HandleSlide(
    float                 TangentialSpeed,
    float                 NormalForce,
    FVector               ContactPoint)
{
   
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