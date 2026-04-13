// Fill out your copyright notice in the Description page of Project Settings.

#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "RuntimePipeline/ModalMetaSoundBridge.h"
#include "SampleBasedAudioComponent.generated.h"


UCLASS(ClassGroup=(Audio), meta=(BlueprintSpawnableComponent),
	   DisplayName="SampleBased Audio")
class PHYSICAL_AUDIO_API USampleBasedAudioComponent : public UActorComponent
{
	
	GENERATED_BODY()
public:
	
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Impact")
    USoundBase* SoundAsset;
	
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Impact",
              meta=(ClampMin="0.0", ClampMax="8.0"))
    float MasterGain = 1.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Scrape",
			meta=(ClampMin="0.0", ClampMax="10.0"))
	float MasterScrapeGain = 1.0f;
   
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Scrape",
              meta=(ClampMin="0.01", ClampMax="1.0"))
    float ScrapeGainSmoothing = 0.20f;
	
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Scrape")
    USoundBase* ScrapeSoundAsset;

	
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Scrape",
              meta=(ClampMin="0.0", ClampMax="4.0"))
    float ScrapeGainScale = 0.5f;
	
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Scrape",
              meta=(ClampMin="0.1"))
    float MaxScrapeSpeed = 3.0f;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Impact")
	TArray<UObject*> ImpactSoundWaves;
	
	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="SampleBased Sound|Scrape")
	USoundWave* ScrapeSoundWave;
	
	USampleBasedAudioComponent();
	
	void EnableListener();
	void DisableListener();
	

	bool bListenerEnabled = false;
protected:
    virtual void BeginPlay() override;
    virtual void EndPlay(const EEndPlayReason::Type EndPlayReason) override;
    virtual void TickComponent(float DeltaTime, ELevelTick TickType,
                               FActorComponentTickFunction* ThisTickFunction) override;

private:
    UFUNCTION()
    void HandleImpact(FVector               ImpactPoint,
                       float                 KineticEnergy,
                       float                 RelativeSpeed,
                       FVector               ImpactNormal);

    UFUNCTION()
    void HandleSlide(float                 TangentialSpeed,
                      float                 NormalForce,
                      FVector               ContactPoint);

    void InitScrapeAudio();

    UPROPERTY()
    class UModalImpactComponent* ImpactComp = nullptr;
	
	
    UPROPERTY()
    class UAudioComponent* ScrapeAudio = nullptr;

    bool bScrapeInitialised = false;
	
    float TimeSinceLastImpact = 9999.f;
	
    float SmoothedScrapeGain = 0.f;
	
	bool bSlideActiveThisFrame = false;
	
	
};
