// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "GameFramework/Actor.h"
#include "InteractableObject.generated.h"

class USampleBasedAudioComponent;
class UModalMetaSoundBridge;
class UModalImpactComponent;

UCLASS()
class PHYSICAL_AUDIO_API AInteractableObject : public AActor
{
	GENERATED_BODY()

public:
	// Sets default values for this actor's properties
	AInteractableObject();

protected:
	// Called when the game starts or when spawned
	virtual void BeginPlay() override;

public:
	// Called every frame
	virtual void Tick(float DeltaTime) override;
	
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Components", meta = (AllowPrivateAccess = "true"))
	USceneComponent* Root = nullptr;
	
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Components", meta = (AllowPrivateAccess = "true"))
	UMeshComponent* Mesh = nullptr;
	
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Components", meta = (AllowPrivateAccess = "true"))
	UModalImpactComponent* ImpactComp = nullptr;
	
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Components", meta = (AllowPrivateAccess = "true"))
	UModalMetaSoundBridge* ModalAudioComp = nullptr;
	
	UPROPERTY(VisibleAnywhere, BlueprintReadOnly, Category="Components", meta = (AllowPrivateAccess = "true"))
	USampleBasedAudioComponent* SampleBasedAudioComp = nullptr;
	
	UFUNCTION(BlueprintCallable)
	void ToggleSoundImplementation(bool bUseSampleBasedAudio);
	
	void SetupDefaultComponents();
	
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Sound")
	bool bIsSampleBased;
};
