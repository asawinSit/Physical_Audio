#include "ModalImpactComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "StaticMeshResources.h"
#include "OfflinePipeline/ModalSoundDataAsset.h"

UModalImpactComponent::UModalImpactComponent(): ModalDataAsset(nullptr)
{
    PrimaryComponentTick.bCanEverTick = false;
}

void UModalImpactComponent::BeginPlay()
{
    Super::BeginPlay();

    if (!ModalDataAsset)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("ModalImpactComponent on '%s': No DataAsset assigned."),
            *GetOwner()->GetName());
        return;
    }

    OwnerMeshComponent = Cast<UPrimitiveComponent>(
        GetOwner()->GetComponentByClass(
            UStaticMeshComponent::StaticClass()));

    if (!OwnerMeshComponent)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("ModalImpactComponent: No StaticMeshComponent on '%s'."),
            *GetOwner()->GetName());
        return;
    }

    OwnerMeshComponent->SetNotifyRigidBodyCollision(true);
    OwnerMeshComponent->OnComponentHit.AddDynamic(
        this, &UModalImpactComponent::OnComponentHit);

    BuildVertexCache();

    UE_LOG(LogTemp, Log,
        TEXT("ModalImpactComponent ready on '%s': %d modes, %d vertices."),
        *GetOwner()->GetName(),
        ModalDataAsset->Modes.Num(),
        CachedVertexPositions.Num());
}

void UModalImpactComponent::BuildVertexCache()
{
    if (!ModalDataAsset || !ModalDataAsset->SourceMesh.IsValid())
        return;

    UStaticMesh* Mesh = ModalDataAsset->SourceMesh.LoadSynchronous();
    if (!Mesh || !Mesh->HasValidRenderData()) return;

    const FStaticMeshLODResources& LOD =
        Mesh->GetRenderData()->LODResources[0];
    const FPositionVertexBuffer& PosBuffer =
        LOD.VertexBuffers.PositionVertexBuffer;

    int32 NumVerts = PosBuffer.GetNumVertices();
    CachedVertexPositions.SetNum(NumVerts);

    for (int32 i = 0; i < NumVerts; ++i)
    {
        FVector3f P = PosBuffer.VertexPosition(i);
        CachedVertexPositions[i] = FVector(P.X, P.Y, P.Z);
    }
}

int32 UModalImpactComponent::FindClosestVertex(FVector WorldPos) const
{
    if (CachedVertexPositions.Num() == 0) return 0;

    FVector LocalPos = GetOwner()->GetActorTransform()
        .InverseTransformPosition(WorldPos);

    int32 Closest = 0;
    float BestDist = FLT_MAX;

    for (int32 i = 0; i < CachedVertexPositions.Num(); ++i)
    {
        float D = FVector::DistSquared(LocalPos, CachedVertexPositions[i]);
        if (D < BestDist) { BestDist = D; Closest = i; }
    }

    return Closest;
}

TArray<float> UModalImpactComponent::ComputeModeAmplitudes(
    int32 VertexIndex, float KineticEnergy) const
{
    TArray<float> Amplitudes;
    if (!ModalDataAsset) return Amplitudes;

    float Energy = FMath::Clamp(KineticEnergy, 0.f, MaxImpactEnergy);

    // Sqrt scaling: loudness perception scales sublinearly with energy
    // Reference: Klatzky et al. (2000)
    float EnergyScale = FMath::Sqrt(Energy / MaxImpactEnergy);

    int32 NumModes = ModalDataAsset->Modes.Num();
    Amplitudes.SetNum(NumModes);

    for (int32 k = 0; k < NumModes; ++k)
    {
        const FModalMode& Mode = ModalDataAsset->Modes[k];

        if (!Mode.VertexParticipation.IsValidIndex(VertexIndex))
        {
            Amplitudes[k] = 0.f;
            continue;
        }

        // Core excitation formula:
        // amplitude_k = phi_k(impact_point) * energy_scale
        // Reference: van den Doel & Pai (1998), Section 3.2
        Amplitudes[k] = Mode.VertexParticipation[VertexIndex]
                      * EnergyScale
                      * Mode.GlobalAmplitude
                      * VolumeScale;
    }

    return Amplitudes;
}

void UModalImpactComponent::OnComponentHit(
    UPrimitiveComponent* HitComponent,
    AActor*              OtherActor,
    UPrimitiveComponent* OtherComp,
    FVector              NormalImpulse,
    const FHitResult&    Hit)
{
    if (!ModalDataAsset) return;

    float Now = GetWorld()->GetTimeSeconds();
    if (Now - LastImpactTime < ImpactCooldown) return;

    // UE NormalImpulse is in kg*cm/s — convert to kg*m/s
    float ImpulseMag = NormalImpulse.Size() * 0.01f; // cm/s → m/s
    float KineticEnergy = 0.f;

    if (HitComponent && HitComponent->IsSimulatingPhysics())
    {
        FBodyInstance* Body = HitComponent->GetBodyInstance();
        if (Body)
        {
            float Mass = Body->GetBodyMass();
            if (Mass > 0.f)
                KineticEnergy = (ImpulseMag * ImpulseMag) / (2.f * Mass);
        }
    }

    if (KineticEnergy <= 0.f)
        KineticEnergy = ImpulseMag * ImpulseMag * 0.5f;

    if (KineticEnergy < MinImpactEnergy) return;

    LastImpactTime = Now;

    int32 Vertex = FindClosestVertex(Hit.ImpactPoint);

    UE_LOG(LogTemp, Verbose,
        TEXT("ModalImpact: energy=%.3f vertex=%d"), KineticEnergy, Vertex);

    OnModalImpact.Broadcast(Hit.ImpactPoint, KineticEnergy,
                             Vertex, ModalDataAsset);
}