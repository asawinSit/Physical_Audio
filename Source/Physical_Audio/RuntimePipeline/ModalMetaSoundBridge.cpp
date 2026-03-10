// ============================================================================
// ModalMetaSoundBridge.cpp  — v2
// ============================================================================
//
// ARCHITECTURE CHANGE FROM v1:
// v1 used MS_ModalScrape with a Trigger Repeat node that restarted the
// modal synthesizer every 60–100 ms. Each restart produced an attack
// transient, making scraping sound like repeated impacts.
//
// v2 uses ModalResonatorBank (custom C++ MetaSound node) which runs modal
// resonators CONTINUOUSLY at audio rate. MS_ModalScrape_v2 has NO Trigger
// Repeat. The excitation is velocity-pitched bandpass noise. Volume is
// controlled only by ScrapeGain set each tick — no attack transients.
//
// SCRAPE PARAMETERS (each tick):
//   ScrapeFrequencies (Float Array), ScrapeDampings (Float Array),
//   ScrapeAmplitudes (Float Array), NumModes (Int),
//   ScrapeSpeed (Float 0-1), ScrapeGain (Float 0-ScrapeGainScale)
//
// IMPACT PARAMETERS (before Trigger):
//   Frequencies, DampingRatios, Amplitudes, NumModes,
//   MasterGain, ContactGain, then Trigger (LAST)
//
// See MS_ModalSounds_v2_Setup.md for MetaSound graph wiring instructions.
// ============================================================================

#include "ModalMetaSoundBridge.h"
#include "OfflinePipeline/ModalSoundDataAsset.h"
#include "Kismet/GameplayStatics.h"

UModalMetaSoundBridge::UModalMetaSoundBridge()
{
    // Tick needed to advance TimeSinceLastImpact for post-impact scrape suppression.
    PrimaryComponentTick.bCanEverTick = true;
}

void UModalMetaSoundBridge::BeginPlay()
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

    ImpactComp->OnModalImpact.AddDynamic(this, &UModalMetaSoundBridge::HandleImpact);
    ImpactComp->OnModalSlide.AddDynamic(this,  &UModalMetaSoundBridge::HandleSlide);

    UE_LOG(LogTemp, Log,
        TEXT("[ModalBridge] '%s' ready (v2 — continuous resonator bank)"),
        *GetOwner()->GetName());
}

void UModalMetaSoundBridge::EndPlay(const EEndPlayReason::Type EndPlayReason)
{
    if (ScrapeAudio && ScrapeAudio->IsPlaying())
        ScrapeAudio->Stop();
    Super::EndPlay(EndPlayReason);
}

void UModalMetaSoundBridge::TickComponent(float DeltaTime, ELevelTick TickType,
                                           FActorComponentTickFunction* ThisTickFunction)
{
    Super::TickComponent(DeltaTime, TickType, ThisTickFunction);
    TimeSinceLastImpact += DeltaTime;
}

// ─────────────────────────────────────────────────────────────────────────────
// IMPACT
// ─────────────────────────────────────────────────────────────────────────────
void UModalMetaSoundBridge::HandleImpact(
    FVector               ImpactPoint,
    float                 KineticEnergy,
    float                 RelativeSpeed,
    int32                 VertexIndex,
    UModalSoundDataAsset* DataAsset,
    FVector               ImpactNormal)
{
    if (!ModalSoundAsset || !DataAsset || !ImpactComp) return;

    TArray<float> Amplitudes = ImpactComp->ComputeModeAmplitudes(
        VertexIndex, KineticEnergy, RelativeSpeed, ImpactNormal);

    int32 N = FMath::Min(DataAsset->Modes.Num(), MaxModes);
    TArray<float> Freqs, Amps, Damps;
    Freqs.SetNum(N); Amps.SetNum(N); Damps.SetNum(N);

    for (int32 k = 0; k < N; ++k)
    {
        const FModalMode& M = DataAsset->Modes[k];
        Freqs[k] = M.Frequency;
        Damps[k] = M.DampingRatio;
        Amps[k]  = Amplitudes.IsValidIndex(k) ? Amplitudes[k] : 0.f;
    }

    // ── Exponential mode rolloff ────────────────────────────────────────
    // Taper higher modes so the fundamental dominates perceptually while
    // keeping modes 2-6 audible enough to add spectral richness.
    //
    // Rolloff 0.12 (was 0.20, was 0.35):
    //   mode 1=1.00×, mode 2=0.89×, mode 3=0.70×, mode 4=0.62×, mode 6=0.49×
    //
    // WHY reduced from 0.20:
    //   The shell radiation model assigns low GlobalAmplitude to even-numbered
    //   plate bending modes (e.g. (2,1) has two antinodes that partially cancel
    //   in the mean). Combined with a 0.20 rolloff, modes 2-4 become inaudible
    //   → the sound is dominated by the single fundamental → "cork" or "hollow"
    //   quality. At 0.12, modes 2-4 retain 89%/70%/62% amplitude even before
    //   the radiation weighting, ensuring multi-partial character.
    //
    //   Risk of too-flat rolloff: noise-like buzzy attack. At 0.12 the ratio
    //   mode10/mode1 = exp(-0.12×9) = 0.34 — still clearly tapered.
    for (int32 k = 0; k < N; ++k)
        Amps[k] *= FMath::Exp(-0.12f * static_cast<float>(k));

    if (bApplyPerceptualTuning)
    {
        float ObjectVolume = FMath::Max(DataAsset->MeshVolume, 0.001f);
        float RefVolume    = 0.001f;
        float PitchScale   = FMath::Clamp(
            FMath::Pow(RefVolume / ObjectVolume, 0.07f), 0.80f, 1.20f);
        for (float& F : Freqs) F *= PitchScale;
    }

    // ContactGain → drives AudioMixer Gain 1 in MS_ModalImpact (the
    // 3500Hz BPF noise crack layer, AD envelope attack=0.5ms/decay=8ms).
    // Higher = louder, crisper attack transient.
    //
    // Formula: sqrt(speed/4), clamped [0.15, 0.95]
    //
    // WHY speed-only (hardness removed):
    //   The 3500Hz crack encodes impact velocity — fast = loud crack regardless
    //   of material. Material identity is encoded by the modal content (modes,
    //   damping) and by ThudGain (300Hz body layer) below.
    //   Previous formula sqrt(speed/8)*sqrt(hardness): wood at 3m/s → 0.35,
    //   which after Power(0.4) = 0.66. But at 1m/s → 0.15 → Power(0.4) = 0.46
    //   which made gentle wood impacts flat and undifferentiated.
    //   sqrt(speed/4): 1m/s → 0.50 → Power=0.76; 3m/s → 0.87 → Power=0.94.
    //   Every speed produces a meaningfully distinct crack level.
    //
    // The graph raises this to 0.4 power (Power node) at the mixer.
    float ContactGain = FMath::Clamp(
        FMath::Sqrt(RelativeSpeed / 4.0f),
        0.15f, 0.95f);

    UE_LOG(LogTemp, Verbose,
        TEXT("[ModalBridge] Impact KE=%.1f J  speed=%.2f m/s  "
             "vtx=%d  modes=%d  ContactGain=%.3f"),
        KineticEnergy, RelativeSpeed, VertexIndex, N, ContactGain);

    UAudioComponent* Audio = UGameplayStatics::SpawnSoundAtLocation(
        GetWorld(),
        ModalSoundAsset,
        ImpactPoint,
        FRotator::ZeroRotator,
        1.f, 1.f, 0.f,
        AttenuationSettings,
        nullptr,
        true);

    if (!Audio)
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalBridge] SpawnSoundAtLocation returned null."));
        return;
    }

    // Set ALL values before Trigger — MetaSound reads at trigger time
    Audio->SetFloatArrayParameter(FName("Frequencies"),   Freqs);
    Audio->SetFloatArrayParameter(FName("DampingRatios"), Damps);
    Audio->SetFloatArrayParameter(FName("Amplitudes"),    Amps);
    Audio->SetIntParameter       (FName("NumModes"),      N);
    Audio->SetFloatParameter     (FName("MasterGain"),    MasterGain);
    Audio->SetFloatParameter     (FName("ContactGain"),   ContactGain);

    // ThudGain: scales the LPF-noise body layer (Mixer In 2 in MS_ModalImpact).
    // That layer is 300Hz LPF noise, AD envelope decay=100ms — it sounds like
    // the low-frequency body mass of the impact. Fixed at 0.2 default in the
    // graph, which plays at the same level regardless of material or velocity.
    //
    // Fix: scale by ContactGain (velocity) AND ContactHardness (material weight).
    //   ThudGain = ContactGain × hardness × ThudGainBase
    //
    // Effect:
    //   Wood at 3m/s:   0.87 × 0.30 × 0.30 = 0.078  (light, brief body thud)
    //   Stone at 3m/s:  0.87 × 0.70 × 0.30 = 0.183  (medium body)
    //   Metal at 3m/s:  0.87 × 1.00 × 0.30 = 0.261  (heavy body clang)
    //   Wood at 0.5m/s: 0.35 × 0.30 × 0.30 = 0.032  (nearly inaudible body)
    //
    // This separates material weight perception (ThudGain) from attack sharpness
    // (ContactGain), so wood at speed sounds crisp but light, metal sounds
    // both crisp AND heavy.
    //
    // ThudGainBase=0.30: tuned so metal at full speed (0.261) is just below the
    // graph default (0.20). Wood never dominates the modal content.
    constexpr float ThudGainBase = 0.50f;  // raised from 0.30 — adds body to wood
    const float ThudGain = FMath::Clamp(
        ContactGain * DataAsset->ContactHardness * ThudGainBase,
        0.0f, 0.50f);
    Audio->SetFloatParameter(FName("ThudGain"), ThudGain);

    // CrackFc: centre frequency of the body-crack bandpass filter (SVF_3 in
    // MS_ModalImpact). Hardcoded at 3500Hz in the graph, but 3500Hz sits ~12×
    // above the wood plank f1 (280Hz) and sounds completely detached — a sharp
    // synthetic click overlaid on a low wood ring rather than integrated with it.
    //
    // Formula: f1 × 3.0, clamped [800, 4000] Hz
    //   Wood plank  (f1=280Hz):  840 Hz  — sits between f1 and f2, cohesive knock
    //   Wood chair  (f1=596Hz): 1200 Hz  — bright, integrated crack  
    //   Metal chair (f1=1064Hz): 3000 Hz — stays high, metallic click
    //   HeavyMetal  (f1=280Hz):  840 Hz  — matches heavy knock
    //
    // GRAPH CHANGE REQUIRED in MS_ModalImpact:
    //   1. Add Float input "CrackFc" (default 1200)
    //   2. Wire CrackFc → SVF_3 Cutoff Frequency pin
    //   3. Remove the hardcoded 3500.0 default
    //   (SVF_3 is ExternalNode_3, the BPF with Fc=3500Hz in the current graph)
    const float f1 = (DataAsset->Modes.Num() > 0) ? DataAsset->Modes[0].Frequency : 440.f;
    const float CrackFc = FMath::Clamp(f1 * 3.0f, 800.f, 4000.f);
    Audio->SetFloatParameter(FName("CrackFc"), CrackFc);

    Audio->SetTriggerParameter(FName("Trigger"));  // MUST be last

    // Suppress scrape gain for a short window after impact.
    // When an object hits a surface it simultaneously bounces (impact) and may
    // begin sliding. Without suppression, the scrape gain spikes immediately
    // alongside the impact transient — the two sounds have incompatible
    // envelopes and the transition sounds abrupt ("hasty").
    // PostImpactSuppressTime lets the impact ring establish for ~80ms before
    // the scrape fades in, which matches how real sliding collisions sound:
    // a clear impact followed by the object settling into a scrape.
    TimeSinceLastImpact = 0.f;
}

// ─────────────────────────────────────────────────────────────────────────────
// SCRAPE — called each tick while sliding
// ─────────────────────────────────────────────────────────────────────────────
void UModalMetaSoundBridge::HandleSlide(
    float                 TangentialSpeed,
    float                 NormalForce,
    FVector               ContactPoint,
    UModalSoundDataAsset* DataAsset,
    int32                 VertexIndex)
{
    if (!ScrapeSoundAsset || !DataAsset || !ImpactComp) return;

    if (!bScrapeInitialised)
        InitScrapeAudio(DataAsset);

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

    // Bridge-side one-pole gain smoothing.
    // ModalImpactComponent already applies SlideSmoothing on speed, but that
    // does not protect against sudden gain jumps caused by impact/slide
    // classification thrashing — e.g. a long plank sliding on one end while
    // the other end hits a wall: OnHit fires at NormalFraction > threshold
    // (impact), suppresses the scrape via TimeSinceLastImpact, then the next
    // tick resumes the scrape at full gain. The result is a sharp cut/pop.
    // This second smoothing filter in the bridge catches those jumps and
    // ensures gain always changes at a rate controlled by ScrapeGainSmoothing.
    SmoothedScrapeGain = FMath::Lerp(SmoothedScrapeGain, TargetGain, ScrapeGainSmoothing);
    float CurrentScrapeGain = SmoothedScrapeGain;

    TArray<float> ScrapeAmps = ImpactComp->ComputeScrapeAmplitudes(
        VertexIndex, TangentialSpeed, NormalForce);

    int32 N = FMath::Min(DataAsset->Modes.Num(), MaxScrapeModes);
    TArray<float> Freqs, Amps, Damps;
    Freqs.SetNum(N); Amps.SetNum(N); Damps.SetNum(N);

    for (int32 k = 0; k < N; ++k)
    {
        const FModalMode& M = DataAsset->Modes[k];
        Freqs[k] = M.Frequency;
        Damps[k] = M.DampingRatio;
        Amps[k]  = ScrapeAmps.IsValidIndex(k) ? ScrapeAmps[k] : 0.f;
    }

    // Rolloff 0.20: mode 1=1.00x, mode 2=0.82x, mode 4=0.45x, mode 8=0.20x.
    // Keeps modes 1-4 audible — multi-harmonic friction texture.
    for (int32 k = 0; k < N; ++k)
        Amps[k] *= FMath::Exp(-0.20f * static_cast<float>(k));

    // DampingRatios sent as-is (FEM values, no multiplier).
    // ScrapeDampingScale=4.0 was tried and reverted. The scrape "heaviness"
    // was caused by SVF_2 Resonance=4.0 in MS_ModalScrape creating a large
    // resonant spike at ScrapeFilterFc (106-174Hz for slow wood chair slides).
    // Fix: set SVF_2 Resonance to 1.5 in the MetaSound graph (see setup guide).



    // Update the continuously-running MetaSound. NO TRIGGER.
    // The resonator bank picks up parameter changes on the next audio buffer.
    ScrapeAudio->SetFloatArrayParameter(FName("ScrapeFrequencies"), Freqs);
    ScrapeAudio->SetFloatArrayParameter(FName("ScrapeDampings"),    Damps);
    ScrapeAudio->SetFloatArrayParameter(FName("ScrapeAmplitudes"),  Amps);
    ScrapeAudio->SetIntParameter       (FName("NumModes"),          N);
    ScrapeAudio->SetFloatParameter     (FName("ScrapeGain"),        CurrentScrapeGain);
    // ScrapeSpeed no longer sent — Fc is computed in bridge and sent as ScrapeFilterFc.

    // ScrapeFilterFc: computed here so the absolute-Hz floor works correctly
    // regardless of the object's f1. The MetaSound graph wires this directly
    // to the bandpass SVF Cutoff Frequency pin — no math nodes in the graph.
    //
    // Formula: Fc = clamp(f1 * (0.15 + SpeedNorm*0.85), 80, f1)
    //   - floor at 80 Hz: prevents near-DC cutoff on low-f1 objects (planks
    //     at f1=150 Hz had Fc=22 Hz at slow speed → resonators got zero excitation)
    //   - ceiling at f1: prevents exciting high modes at fast speed
    //     (caused screeching on metal ξ=0.002 objects)
    //   - 0.15 offset: ensures some excitation even when SpeedNorm≈0
    //
    // GRAPH CHANGE REQUIRED: In MS_ModalScrape, remove the Multiply×0.85,
    // Add+0.15, Multiply×ScrapeBaseFreq chain and the ScrapeBaseFreq input.
    // Add a new Float input "ScrapeFilterFc" (default 440) and wire it
    // directly to the bandpass SVF's "Cutoff Frequency" pin.
    const float f1 = (N > 0 && DataAsset->Modes.IsValidIndex(0))
        ? DataAsset->Modes[0].Frequency : 440.f;
    const float Fc = FMath::Clamp(
        f1 * (0.15f + SpeedNorm * 0.85f),
        80.f,
        f1);
    ScrapeAudio->SetFloatParameter(FName("ScrapeFilterFc"), Fc);
    // ^^^ No SetTriggerParameter here. Ever. That was the v1 bug.
}


// ─────────────────────────────────────────────────────────────────────────────
// INIT SCRAPE AUDIO COMPONENT
// ─────────────────────────────────────────────────────────────────────────────
void UModalMetaSoundBridge::InitScrapeAudio(UModalSoundDataAsset* DataAsset)
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
        ScrapeAttenuationSettings,
        nullptr,
        true);  // auto activate — graph starts looping immediately

    if (ScrapeAudio)
    {
        // Prime silent until first slide event
        ScrapeAudio->SetFloatParameter(FName("ScrapeGain"),  0.f);
        ScrapeAudio->SetFloatParameter(FName("ScrapeSpeed"), 0.f);

        // Prime resonator bank with object's modal data
        int32 N = FMath::Min(DataAsset->Modes.Num(), MaxScrapeModes);
        TArray<float> Freqs, Amps, Damps;
        Freqs.SetNum(N); Amps.SetNum(N); Damps.SetNum(N);
        for (int32 k = 0; k < N; ++k)
        {
            Freqs[k] = DataAsset->Modes[k].Frequency;
            Damps[k] = DataAsset->Modes[k].DampingRatio;
            Amps[k]  = 0.f;
        }
        ScrapeAudio->SetFloatArrayParameter(FName("ScrapeFrequencies"), Freqs);
        ScrapeAudio->SetFloatArrayParameter(FName("ScrapeDampings"),    Damps);
        ScrapeAudio->SetFloatArrayParameter(FName("ScrapeAmplitudes"),  Amps);
        ScrapeAudio->SetIntParameter       (FName("NumModes"),          N);

        bScrapeInitialised = true;
        UE_LOG(LogTemp, Log,
            TEXT("[ModalBridge] '%s' scrape audio initialised (v2 continuous)"),
            *GetOwner()->GetName());
    }
    else
    {
        UE_LOG(LogTemp, Warning,
            TEXT("[ModalBridge] '%s': Failed to spawn scrape audio. "
                 "Assign MS_ModalScrape_v2 to ScrapeSoundAsset."),
            *GetOwner()->GetName());
    }
}