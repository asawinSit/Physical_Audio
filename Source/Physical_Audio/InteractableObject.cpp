// Fill out your copyright notice in the Description page of Project Settings.


#include "InteractableObject.h"
#include "RuntimePipeline/ModalImpactComponent.h"
#include "RuntimePipeline/ModalMetaSoundBridge.h"
#include "SampleBased/SampleBasedAudioComponent.h"
#include "MetaSoundSource.h"

// Sets default values
AInteractableObject::AInteractableObject()
{
	// Set this actor to call Tick() every frame.  You can turn this off to improve performance if you don't need it.
	PrimaryActorTick.bCanEverTick = true;
	
	Root = CreateDefaultSubobject<USceneComponent>(TEXT("Root"));
	Mesh = CreateDefaultSubobject<UStaticMeshComponent>(TEXT("Mesh"));
	SetRootComponent(Root);
	Mesh->SetupAttachment(Root);
	Mesh->SetUseCCD(true);
	Mesh->SetSimulatePhysics(true);
	Mesh->SetNotifyRigidBodyCollision(true);
	Mesh->SetCollisionEnabled(ECollisionEnabled::QueryAndPhysics);
	Mesh->SetCollisionObjectType(ECC_PhysicsBody);
	
	ImpactComp = CreateDefaultSubobject<UModalImpactComponent>(TEXT("Modal Impact Component"));
	ModalAudioComp = CreateDefaultSubobject<UModalMetaSoundBridge>(TEXT("Modal MetaSound Bridge"));
	SampleBasedAudioComp = CreateDefaultSubobject<USampleBasedAudioComponent>(TEXT("SampleBased Audio Component"));
	
	SetupDefaultComponents();
}

// Called when the game starts or when spawned
void AInteractableObject::BeginPlay()
{
	Super::BeginPlay();
	
	
	ToggleSoundImplementation(false);
}

// Called every frame
void AInteractableObject::Tick(float DeltaTime)
{
	Super::Tick(DeltaTime);
}

void AInteractableObject::ToggleSoundImplementation(bool bUseSampleBasedAudio)
{
	bIsSampleBased = bUseSampleBasedAudio;

	GetWorld()->GetTimerManager().SetTimerForNextTick([this]()
	{
		if (bIsSampleBased)
		{
			ModalAudioComp->DisableListener();
			SampleBasedAudioComp->EnableListener();
		}
		else
		{
			ModalAudioComp->EnableListener();
			SampleBasedAudioComp->DisableListener();
		}
	});
}

void AInteractableObject::SetupDefaultComponents()
{
	// Load MetaSound assets
	UMetaSoundSource* DefaultModalSoundAsset = LoadObject<UMetaSoundSource>(
		nullptr, TEXT("/Game/Audio/MS_ModalImpact.MS_ModalImpact"));
	UMetaSoundSource* DefaultScrapeSoundAsset = LoadObject<UMetaSoundSource>(
		nullptr, TEXT("/Game/Audio/MS_ModalScrape.MS_ModalScrape"));
	// Load attenuation assets
	USoundAttenuation* DefaultModalAttenuation = LoadObject<USoundAttenuation>(
		nullptr, TEXT("/Game/Audio/SA_ModalImpact.SA_ModalImpact"));
	USoundAttenuation* DefaultScrapeAttenuation = LoadObject<USoundAttenuation>(
		nullptr, TEXT("/Game/Audio/SA_ModalScrape.SA_ModalScrape"));

	// Assign defaults to the audio component if it exists
	if (ModalAudioComp)
	{
		ModalAudioComp->ModalSoundAsset = DefaultModalSoundAsset;
		ModalAudioComp->AttenuationSettings = DefaultModalAttenuation;
		ModalAudioComp->ScrapeSoundAsset = DefaultScrapeSoundAsset;
		ModalAudioComp->ScrapeAttenuationSettings = DefaultScrapeAttenuation;
	}
	// Load MetaSound assets
	UMetaSoundSource* DefaultSampleBasedImpactSoundAsset = LoadObject<UMetaSoundSource>(
		nullptr, TEXT("/Game/Audio/MS_SampleBasedImpact.MS_SampleBasedImpact"));
	UMetaSoundSource* DefaultSampleBasedScrapeSoundAsset = LoadObject<UMetaSoundSource>(
		nullptr, TEXT("/Game/Audio/MS_SampleBasedScrape.MS_SampleBasedScrape"));

	// Assign defaults to the audio component if it exists
	if (SampleBasedAudioComp)
	{
		SampleBasedAudioComp->SoundAsset = DefaultSampleBasedImpactSoundAsset;
		SampleBasedAudioComp->ScrapeSoundAsset = DefaultSampleBasedScrapeSoundAsset;
	
	}
}
