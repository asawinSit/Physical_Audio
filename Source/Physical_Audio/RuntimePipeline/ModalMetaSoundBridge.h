#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "ModalImpactComponent.h"
#include "ModalMetaSoundBridge.generated.h"

UCLASS(ClassGroup=(Audio),
	   meta=(BlueprintSpawnableComponent),
	   DisplayName="Modal MetaSound Bridge")
class PHYSICAL_AUDIO_API UModalMetaSoundBridge : public UActorComponent
{
	GENERATED_BODY()

public:
	UModalMetaSoundBridge();

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
	USoundBase* ModalSoundAsset;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
	USoundAttenuation* AttenuationSettings;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
	USoundBase* ImpactThudSound;

	UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
	int32 MaxModes = 40;

protected:
	virtual void BeginPlay() override;

private:
	UFUNCTION()
	void HandleModalImpact(FVector ImpactPoint,
							float KineticEnergy,
							int32 VertexIndex,
							UModalSoundDataAsset* DataAsset);

	UPROPERTY()
	UModalImpactComponent* ImpactComponent = nullptr;
};