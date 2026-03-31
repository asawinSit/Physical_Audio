// ============================================================================
// ModalImpactComponent.cpp
// ============================================================================
//
// KEY CHANGES FROM PREVIOUS VERSION:
//
// 1. VERTEX INDEX FIX (critical correctness fix):
//    FindClosestVertex now searches DataAsset->FEMSurfaceVertexPositions
//    instead of a cached copy of the render mesh LOD0.
//
//    Root cause of old bug: after mesh subdivision the FEM surface mesh has
//    many more vertices than the render mesh (e.g. 386 vs 8 for a cube).
//    The old code searched 8 render verts, returned an index in [0,7], then
//    used it to index VertexParticipation which has 386 entries. Every impact
//    on every part of the cube sounded identical because it always read from
//    the first 8 participation values. Spatial impact variation was silent.
//
//    Fix: BuildVertexCache() is removed. At BeginPlay we just verify the
//    DataAsset has FEMSurfaceVertexPositions. FindClosestVertex searches that
//    array, which is indexed identically to VertexParticipation.
//
// 2. CONTACT HARDNESS FROM DATAASSET:
//    BeginPlay reads DataAsset->ContactHardness into the component property.
//    The Blueprint override is still respected — setting ContactHardness
//    in the Details panel after BeginPlay overrides the DataAsset value.
//
// 3. SLIDING DELEGATE:
//    OnHit now additionally tracks sliding contacts (NormalFraction < threshold
//    but TangentialSpeed is meaningful). TickComponent broadcasts OnModalSlide
//    each tick while sliding is active, using a one-pole smoothed speed.
//    The scrape sound fades in and out naturally as the object moves.
//
// 4. SCRAPE AMPLITUDES:
//    ComputeScrapeAmplitudes uses DragSensitivity from the DataAsset, which
//    encodes low-frequency emphasis appropriate for friction excitation.
// ============================================================================

#include "ModalImpactComponent.h"
#include "Components/StaticMeshComponent.h"
#include "Engine/StaticMesh.h"
#include "OfflinePipeline/ModalSoundDataAsset.h"

UModalImpactComponent::UModalImpactComponent(): ModalDataAsset(nullptr)
{
    PrimaryComponentTick.bCanEverTick = true;
    PrimaryComponentTick.bStartWithTickEnabled = true;
}

void UModalImpactComponent::BeginPlay()
{
    Super::BeginPlay();

    if (!ModalDataAsset)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalImpact] '%s': No ModalDataAsset assigned."),
            *GetOwner()->GetName());
        return;
    }

    // Read material contact hardness from the DataAsset.
    // This ensures the Hertz shaping always matches the material that was
    // used to generate the asset, without requiring manual override.
    // Blueprint override still works — set ContactHardness in Details panel
    // after the game has started if you want to deviate from the asset value.
    ContactHardness = ModalDataAsset->ContactHardness;

    OwnerMesh = Cast<UPrimitiveComponent>(
        GetOwner()->GetComponentByClass(UStaticMeshComponent::StaticClass()));

    if (!OwnerMesh)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalImpact] '%s': No StaticMeshComponent found."),
            *GetOwner()->GetName());
        return;
    }

    OwnerMesh->SetNotifyRigidBodyCollision(true);
    OwnerMesh->OnComponentHit.AddDynamic(this, &UModalImpactComponent::OnHit);

    // Validate that FEM vertex positions were stored by the pipeline.
    int32 FEMVerts = ModalDataAsset->FEMSurfaceVertexPositions.Num();
    if (FEMVerts == 0)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalImpact] '%s': DataAsset has no FEMSurfaceVertexPositions. "
                 "Regenerate the DataAsset with the updated pipeline. "
                 "FindClosestVertex will always return vertex 0."),
            *GetOwner()->GetName());
    }

    UE_LOG(LogTemp, Log,
        TEXT("[ModalImpact] '%s' ready: %d modes, %d FEM verts, "
             "ContactHardness=%.2f, ContactDurationScale=%.2f, MinKE=%.1f J"),
        *GetOwner()->GetName(),
        ModalDataAsset->Modes.Num(),
        FEMVerts,
        ContactHardness,
        ContactDurationScale,
        MinImpactEnergy);
}

void UModalImpactComponent::TickComponent(
    float DeltaTime, ELevelTick TickType,
    FActorComponentTickFunction* Fn)
{
    Super::TickComponent(DeltaTime, TickType, Fn);

    if (!OwnerMesh || !OwnerMesh->IsSimulatingPhysics()) return;

    // Cache pre-impact velocity for the next OnHit event.
    FVector CurrentVel = OwnerMesh->GetPhysicsLinearVelocity() * 0.01f; // cm→m

    FBodyInstance* Body = OwnerMesh->GetBodyInstance();
    bool bAwake = Body && Body->IsInstanceAwake()
                       && CurrentVel.SizeSquared() > 0.0001f;

    PreImpactVelocity = bAwake ? CurrentVel : FVector::ZeroVector;

    // ── Slide tick ────────────────────────────────────────────────────────
    // If OnHit detected a sliding contact recently, maintain the slide state
    // and broadcast OnModalSlide each tick with smoothed speed.
    // We consider sliding "active" if OnHit has fired within the last 0.1 s
    // with a sliding classification (tracked via LastSlideTime).
    float Now = GetWorld()->GetTimeSeconds();
    bool bSlidingRecently = (Now - LastSlideTime) < 0.10f;

    if (bSlidingRecently && ModalDataAsset && bAwake)
    {
        // Use current tangential speed (velocity projected onto last contact plane)
        FVector Vel     = PreImpactVelocity;
        float Normal    = FVector::DotProduct(Vel, LastContactNormal);
        FVector TanVel  = Vel - LastContactNormal * Normal;
        float TanSpeed  = TanVel.Size();

        // One-pole smoothing to avoid crackling from frame-to-frame variation.
        SmoothedSlideSpeed = FMath::Lerp(SmoothedSlideSpeed, TanSpeed, SlideSmoothing);

        if (SmoothedSlideSpeed > MinSlideSpeed)
        {
            // Normal force proxy: |normal velocity component| × mass.
            // Heavier pressing = louder scrape.
            float Mass = 1.f;
            if (Body) Mass = FMath::Max(Body->GetBodyMass(), 0.001f);
            float NormalForce = FMath::Clamp(
                FMath::Abs(Normal) * Mass / FMath::Max(MaxImpactEnergy, 1.f),
                0.f, 1.f);

            if (!bIsSliding)
            {
                bIsSliding = true;
                UE_LOG(LogTemp, Verbose,
                    TEXT("[ModalImpact] '%s' slide start, speed=%.2f m/s"),
                    *GetOwner()->GetName(), SmoothedSlideSpeed);
            }

            OnModalSlide.Broadcast(
                SmoothedSlideSpeed, NormalForce,
                LastContactPoint, ModalDataAsset, LastSlideVertex);
            
            OnSampleBasedSlide.Broadcast(SmoothedSlideSpeed, NormalForce, LastContactPoint);
        }
        else
        {
            // Speed has dropped below threshold — end slide
            if (bIsSliding)
            {
                bIsSliding = false;
                SmoothedSlideSpeed = 0.f;
                // Broadcast one final tick at zero speed so the bridge can fade out
                OnModalSlide.Broadcast(
                    0.f, 0.f, LastContactPoint, ModalDataAsset, LastSlideVertex);
                
                OnSampleBasedSlide.Broadcast(0.f, 0.f, LastContactPoint);
            }
        }
    }
    else
    {
        // No recent contact — end slide if active
        if (bIsSliding)
        {
            bIsSliding = false;
            SmoothedSlideSpeed = 0.f;
            OnModalSlide.Broadcast(
                0.f, 0.f, LastContactPoint, ModalDataAsset, LastSlideVertex);
            OnSampleBasedSlide.Broadcast(0.f, 0.f, LastContactPoint);
        }
    }
}

int32 UModalImpactComponent::FindClosestVertex(FVector WorldPos) const
{
    if (!ModalDataAsset) return 0;
    const TArray<FVector>& FEMVerts = ModalDataAsset->FEMSurfaceVertexPositions;
    if (FEMVerts.Num() == 0) return 0;

    // Transform world-space hit point into actor local space.
    // FEM vertices are stored in local space in METRES.
    // UE5 actor local space is in centimetres, so we also divide by 100.
    FVector LocalPos =
        GetOwner()->GetActorTransform().InverseTransformPosition(WorldPos)
        * 0.01f;  // cm → m to match FEM vertex scale

    int32 Best  = 0;
    float BestD = FLT_MAX;
    for (int32 i = 0; i < FEMVerts.Num(); ++i)
    {
        float D = FVector::DistSquared(LocalPos, FEMVerts[i]);
        if (D < BestD) { BestD = D; Best = i; }
    }
    return Best;
}

TArray<float> UModalImpactComponent::ComputeModeAmplitudes(
    int32   VertexIndex,
    float   KineticEnergy,
    float   RelativeSpeed,
    FVector ImpactNormal) const
{
    TArray<float> Amps;
    if (!ModalDataAsset) return Amps;

    // ── Energy scale: √(KE/KE_max) ──────────────────────────────────────
    // Perceived loudness ∝ √(contact force). Klatzky et al. (2000).
    float Energy      = FMath::Clamp(KineticEnergy, 0.f, MaxImpactEnergy);
    float EnergyScale = FMath::Sqrt(Energy / FMath::Max(MaxImpactEnergy, 1.f));

    // ── Hertz contact duration T_c ───────────────────────────────────────
    // T_c = C_mat / v^0.2   (Johnson 1985, §3.4; Chadwick et al. 2012, eq.4–5)
    // C_mat encodes material stiffness via ContactHardness (from DataAsset).
    //
    // ContactDurationScale multiplies C_mat to model contact GEOMETRY:
    //   - Broad surface contact (face):    scale ≈ 1.0  (long T_c, more sinc rolloff)
    //   - Edge or point contact (plank):   scale ≈ 0.35 (short T_c, more HF content)
    //
    // Perceptually: shorter T_c → sinc zero at higher frequency → more high-
    // frequency modes are excited → impact sounds harder and crisper.
    // ContactHardness alone is insufficient because its effect on C_mat is
    // dampened by the sqrt, whereas ContactDurationScale scales C_mat directly.
    float SpeedSafe = FMath::Max(RelativeSpeed, 0.1f);
    float C_mat     = (0.0025f / FMath::Sqrt(FMath::Max(ContactHardness, 0.01f)))
                      * FMath::Max(ContactDurationScale, 0.05f);
    float T_c       = C_mat / FMath::Pow(SpeedSafe, 0.2f);

    // ── Per-impact random seed ───────────────────────────────────────────
    // Models micro-scale texture variation. Golden ratio spread for low
    // inter-mode correlation. Rath & Rocchesso (2005).
    float Seed = FMath::FRand();

    int32 N = ModalDataAsset->Modes.Num();
    Amps.SetNum(N);

    for (int32 k = 0; k < N; ++k)
    {
        const FModalMode& Mode = ModalDataAsset->Modes[k];

        // φ_k(v) — participation factor. Valid because VertexIndex came from
        // FindClosestVertex which now searches FEMSurfaceVertexPositions.
        float Phi = Mode.VertexParticipation.IsValidIndex(VertexIndex)
                  ? Mode.VertexParticipation[VertexIndex]
                  : 0.f;

        float Eta = Mode.GlobalAmplitude;  // radiation efficiency η_k

        // Hertz sinc filter: F_k = |sin(π·f·T_c) / (π·f·T_c)|
        float fT   = Mode.Frequency * T_c;
        float Sinc = (fT < 1e-6f)
                   ? 1.0f
                   : FMath::Abs(FMath::Sin(PI * fT) / (PI * fT));

        // Jitter ±15%, golden-ratio phase to decorrelate adjacent modes.
        float Phase  = FMath::Fractional(Seed + k * 0.6180339f);
        float Jitter = 0.85f + 0.30f * Phase;

        Amps[k] = Phi * EnergyScale * Eta * Sinc * VolumeScale * Jitter;
    }

    return Amps;
}

TArray<float> UModalImpactComponent::ComputeScrapeAmplitudes(
    int32 VertexIndex,
    float TangentialSpeed,
    float NormalForce) const
{
    TArray<float> Amps;
    if (!ModalDataAsset) return Amps;

    // Scrape amplitude model (Rath & Rocchesso 2005 §3):
    //   A_k = DragSensitivity_k × SpeedScale × NormalScale × jitter
    //
    // KEY CHANGE: NormalForce is no longer a hard multiplicative gate.
    // Previously: Amps *= NormalForce — but NormalForce is estimated from
    // |normal_velocity| × mass / MaxImpactEnergy, which is near zero for
    // light objects sliding on flat surfaces. This caused near-silence.
    // Fix: NormalForce modulates amplitude by ±30% (additive mix), not ×.
    // The base amplitude is set by DragSensitivity × SpeedScale alone.
    //
    // Also: Phi (vertex participation) is NOT multiplied here for scraping.
    // For impacts, Phi selects which modes are excited at the hit point.
    // For scraping, the object resonates globally — all modes contribute
    // regardless of where contact happens. Using Phi here would make scrape
    // near-silent when contact is at a low-participation vertex.
    float SpeedClamped  = FMath::Clamp(TangentialSpeed / 3.0f, 0.f, 1.f);
    float NormalModAmt  = FMath::Clamp(NormalForce, 0.f, 1.f);  // 0–1
    float NormalMod     = 0.70f + 0.30f * NormalModAmt;          // 0.70–1.00×
    float Seed          = FMath::FRand();

    int32 N = ModalDataAsset->Modes.Num();
    Amps.SetNum(N);

    for (int32 k = 0; k < N; ++k)
    {
        const FModalMode& Mode = ModalDataAsset->Modes[k];

        float Drag = Mode.DragSensitivity;  // pre-shaped low-pass weight

        // Micro-jitter: ±8%, different seed offset from impacts
        float Phase  = FMath::Fractional(Seed + k * 0.6180339f + 0.5f);
        float Jitter = 0.92f + 0.16f * Phase;

        Amps[k] = Drag * SpeedClamped * NormalMod * VolumeScale * Jitter;
    }

    return Amps;
}

void UModalImpactComponent::OnHit(
    UPrimitiveComponent* HitComp,
    AActor*              OtherActor,
    UPrimitiveComponent* OtherComp,
    FVector              NormalImpulse,
    const FHitResult&    Hit)
{
    if (!ModalDataAsset) return;
    if (OtherActor && OtherActor->IsA(APawn::StaticClass())) return;

    if (HitComp)
    {
        FBodyInstance* Body = HitComp->GetBodyInstance();
        if (Body && !Body->IsInstanceAwake()) return;
    }

    FVector OtherVel = FVector::ZeroVector;
    if (OtherComp && OtherComp->IsSimulatingPhysics())
        OtherVel = OtherComp->GetPhysicsLinearVelocity() * 0.01f;

    FVector RelVel      = PreImpactVelocity - OtherVel;
    FVector ContactNorm = Hit.ImpactNormal;

    float NormalSpeed     = FMath::Abs(FVector::DotProduct(RelVel, ContactNorm));
    FVector TanVelVec     = RelVel - ContactNorm * NormalSpeed;
    float TangentialSpeed = TanVelVec.Size();
    float TotalSpeed      = RelVel.Size();

    if (TotalSpeed < 0.05f) return;  // too slow to matter at all

    float NormalFraction = NormalSpeed / FMath::Max(TotalSpeed, 0.001f);

    // Always record contact state for the slide ticker regardless of what we do next.
    LastContactPoint  = Hit.ImpactPoint;
    LastContactNormal = ContactNorm;

    if (NormalFraction < MinNormalFraction)
    {
        // Predominantly sliding — record time/vertex for the slide ticker.
        // Only update if we're NOT in a recent impact cooldown. This prevents
        // a fast-moving plank hitting a wall from immediately re-entering slide
        // state while the impact ring is still sounding — which would cause
        // the scrape sound to snap back on within one PostImpactSuppressTime.
        float Now2 = GetWorld()->GetTimeSeconds();
        if (Now2 - LastImpactTime > ImpactCooldown)
        {
            LastSlideTime   = Now2;
            LastSlideVertex = FindClosestVertex(Hit.ImpactPoint);
        }
        return;
    }

    // ── Impact path ───────────────────────────────────────────────────────
    float Now = GetWorld()->GetTimeSeconds();
    if (Now - LastImpactTime < ImpactCooldown) return;

    // Suppress the slide ticker for a brief window around the impact moment.
    // Set LastSlideTime to (Now - 0.10f + PostImpactSuppressWindow) so that the
    // tick's bSlidingRecently check (< 0.10s) remains false for exactly
    // PostImpactSuppressWindow seconds, then allows slide to resume.
    //
    // IMPORTANT: we do NOT block slide for the full ImpactCooldown (200ms).
    // At high velocity the object is still moving fast after the impact.
    // If we suppress slide for 200ms, there is an audible dead gap — no impact
    // (cooldown blocks re-trigger), no scrape (slide blocked) — for the full
    // cooldown window. At 10 m/s this sounds like a sharp cutoff in the middle
    // of a loud collision.
    //
    // The bridge already applies PostImpactSuppressTime as a gain ramp
    // (SmoothedScrapeGain fades from 0 on the bridge side). So we only need
    // to block the slide ticker long enough for the bridge to start smoothing.
    // 60ms is enough: the bridge ramps in over ScrapeGainSmoothing (default ~120ms),
    // so the scrape is already at near-zero when slide resumes here.
    // The result is a smooth crossfade: impact ring → scrape fade-in, with no gap.
    static constexpr float SlideSuppressWindow = 0.06f;   // s — matches bridge ramp
    LastSlideTime = Now - (0.10f - SlideSuppressWindow);

    float Mass = 1.f;
    if (HitComp && HitComp->IsSimulatingPhysics())
    {
        FBodyInstance* Body = HitComp->GetBodyInstance();
        if (Body) Mass = FMath::Max(Body->GetBodyMass(), 0.001f);
    }

    float KE = 0.5f * Mass * NormalSpeed * NormalSpeed;
    if (KE < MinImpactEnergy) return;

    LastImpactTime = Now;
    int32 Vertex   = FindClosestVertex(Hit.ImpactPoint);

    OnModalImpact.Broadcast(
        Hit.ImpactPoint, KE, NormalSpeed, Vertex, ModalDataAsset, Hit.ImpactNormal);
    
    OnSampleBasedImpact.Broadcast(Hit.ImpactPoint, KE, NormalSpeed, Hit.ImpactNormal);
}