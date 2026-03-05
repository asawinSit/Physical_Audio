#pragma once

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "ModalImpactComponent.generated.h"

class UModalSoundDataAsset;

DECLARE_DYNAMIC_MULTICAST_DELEGATE_FourParams(
    FOnModalImpact,
    FVector, ImpactPoint,
    float, KineticEnergy,
    int32, ClosestVertexIndex,
    UModalSoundDataAsset*, DataAsset
);

UCLASS(ClassGroup=(Audio),
       meta=(BlueprintSpawnableComponent),
       DisplayName="Modal Impact Component")
class PHYSICAL_AUDIO_API UModalImpactComponent : public UActorComponent
{
    GENERATED_BODY()

public:
    UModalImpactComponent();

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
    UModalSoundDataAsset* ModalDataAsset;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
    float MinImpactEnergy = 0.01f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
    float MaxImpactEnergy = 500.f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound")
    float ImpactCooldown = 0.05f;

    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound",
              meta=(ClampMin="0.0", ClampMax="2.0"))
    float VolumeScale = 1.0f;

    UPROPERTY(BlueprintAssignable, Category="Modal Sound")
    FOnModalImpact OnModalImpact;

    UFUNCTION(BlueprintCallable, Category="Modal Sound")
    int32 FindClosestVertex(FVector WorldPosition) const;

    UFUNCTION(BlueprintCallable, Category="Modal Sound")
    TArray<float> ComputeModeAmplitudes(int32 VertexIndex,
                                        float KineticEnergy) const;

protected:
    virtual void BeginPlay() override;

private:
    UFUNCTION()
    void OnComponentHit(UPrimitiveComponent* HitComponent,
                        AActor* OtherActor,
                        UPrimitiveComponent* OtherComp,
                        FVector NormalImpulse,
                        const FHitResult& Hit);

    UPROPERTY()
    UPrimitiveComponent* OwnerMeshComponent = nullptr;

    TArray<FVector> CachedVertexPositions;
    float LastImpactTime = -999.f;

    void BuildVertexCache();
};