#pragma once
// ============================================================================
// ModalMetaSoundBridge.h
// ============================================================================
//
// PURPOSE
// -------
// Receives FOnModalImpact and FOnModalSlide delegates from
// UModalImpactComponent and drives MetaSound graphs for both impact and
// continuous scraping sounds.
//
// WHAT CHANGED FROM PREVIOUS VERSION
// ------------------------------------
// 1. REMOVED DOUBLE PITCH/DECAY CORRECTION:
//    The old bridge applied PitchScale and DecayScale based on MeshVolume,
//    on top of FEM frequencies that already correctly encode the object's
//    geometry. This caused large objects to have all their physically-correct
//    FEM frequencies shifted down by an additional factor, often by 4–10×.
//    FEM already accounts for geometry. These runtime corrections are removed.
//    (If you want to add a subtle perceptual bias, use bApplyPerceptualTuning
//    which applies only a very mild ±20% correction, off by default.)
//
// 2. CONTACT GAIN FROM DATAASSET:
//    ContactGain was computed as RelativeSpeed/8 with no material awareness.
//    Hard stone and soft rubber produced identical transient shapes at the
//    same speed. Now ContactGain incorporates DataAsset->ContactHardness:
//      ContactGain = Clamp(speed × hardness / 8, 0.05, 0.8)
//    Harder materials produce a sharper, brighter transient at the same speed.
//
// 3. SCRAPE AUDIO (new):
//    A persistent UAudioComponent (ScrapeAudio) is created once at BeginPlay
//    for the scrape MetaSound. OnModalSlide updates parameters each tick.
//    When TangentialSpeed drops to zero, MasterGain is set to 0 (silence)
//    rather than destroying the component — avoids the pop-and-spawn cycle.
//
// METASOUND PARAMETERS — IMPACT (MS_ModalImpact):
//   Same as before: Frequencies, Amplitudes, DampingRatios, MasterGain,
//   ContactGain, Trigger.
//
// METASOUND PARAMETERS — SCRAPE (MS_ModalScrape — new):
//   "ScrapeFrequencies"  — float array, Hz  (same as impact)
//   "ScrapeAmplitudes"   — float array, [0,1]  (from DragSensitivity)
//   "ScrapeDampings"     — float array  (same damping ratios)
//   "ScrapeSpeed"        — float, normalised [0,1] tangential speed
//   "ScrapeGain"         — float, overall scrape volume
//   No Trigger — the scrape MetaSound loops continuously; ScrapeGain controls
//   audibility. See MS_ModalScrape wiring notes in the .cpp file.
// ============================================================================

#include "CoreMinimal.h"
#include "Components/ActorComponent.h"
#include "Components/AudioComponent.h"
#include "ModalImpactComponent.h"
#include "ModalMetaSoundBridge.generated.h"

UCLASS(ClassGroup=(Audio), meta=(BlueprintSpawnableComponent),
       DisplayName="Modal MetaSound Bridge")
class PHYSICAL_AUDIO_API UModalMetaSoundBridge : public UActorComponent
{
    GENERATED_BODY()

public:
    UModalMetaSoundBridge();

    // ── IMPACT CONFIGURATION ───────────────────────────────────────────────

    /** The MS_ModalImpact MetaSound Source asset. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Impact")
    USoundBase* ModalSoundAsset;

    /**
     * Sound attenuation for 3D spatialization.
     * Create SA_ModalImpact:  Inner=100 cm, Falloff=5000 cm, LogRolloff.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Impact")
    USoundAttenuation* AttenuationSettings;

    /**
     * Maximum modes sent to the impact MetaSound. Diminishing perceptual
     * returns above ~30. Must not exceed DataAsset mode count.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Impact",
              meta=(ClampMin="1", ClampMax="40"))
    int32 MaxModes = 20;

    /** Overall impact volume. Default 1.0. */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Impact",
              meta=(ClampMin="0.0", ClampMax="8.0"))
    float MasterGain = 1.0f;


    // ── SCRAPE CONFIGURATION ───────────────────────────────────────────────

    /**
     * Maximum modes sent to the scrape MetaSound.
     * SEPARATE from MaxModes (which is impact-only).
     * For scraping 6-10 modes sound more natural than 20 — higher modes add
     * noise texture rather than character, and many equal-amplitude resonators
     * produce a harmonic buzz rather than friction texture.
     * Default 10. Use 6-8 for smooth hard surfaces, 10-12 for rough ones.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Scrape",
              meta=(ClampMin="1", ClampMax="40"))
    int32 MaxScrapeModes = 10;

    /**
     * One-pole smoothing on scrape gain applied in the bridge.
     * Alpha per tick: SmoothedGain = lerp(SmoothedGain, TargetGain, alpha).
     * Range 0.01–1.0. Lower = slower fade; higher = faster response.
     *
     * IMPORTANT — compounding with SlideSmoothing:
     * ModalImpactComponent already applies SlideSmoothing (default 0.15) to
     * tangential speed. This bridge smoother runs on top of that. Two smoothers
     * in series compound: at 60fps, alpha=0.08 + upstream alpha=0.15 takes ~600ms
     * to reach 90% gain after a speed change — the scrape sounds muffled and
     * slow to respond to fast movement.
     *
     * Default 0.20: combined with SlideSmoothing=0.15 this gives ~250ms to 90%
     * gain, which is fast enough for energetic throws while still preventing
     * the pop at impact→slide transitions.
     * Lower to 0.08–0.12 only for very slow, heavy objects (thick stone slabs).
     * Raise to 0.30–0.50 if you want near-instant scrape response (no fade).
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Scrape",
              meta=(ClampMin="0.01", ClampMax="1.0"))
    float ScrapeGainSmoothing = 0.20f;

    /**
     * The MS_ModalScrape MetaSound Source asset.
     * This MetaSound should loop continuously and read ScrapeGain each tick
     * to fade in/out. See wiring notes in ModalMetaSoundBridge.cpp.
     * Leave null to disable scrape sounds entirely.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Scrape")
    USoundBase* ScrapeSoundAsset;

    /**
     * Attenuation for scrape sound. Can share with impact or use separate asset
     * with shorter falloff (scrape is less loud than impact at distance).
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Scrape")
    USoundAttenuation* ScrapeAttenuationSettings;

    /** Overall scrape volume multiplier. Default 0.5 (quieter than impact). */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Scrape",
              meta=(ClampMin="0.0", ClampMax="4.0"))
    float ScrapeGainScale = 0.2f;

    /**
     * Maximum speed (m/s) that maps to full scrape amplitude.
     * At this speed ScrapeGain reaches ScrapeGainScale.
     * Default 3.0 m/s (fast slide on hard floor).
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Scrape",
              meta=(ClampMin="0.1"))
    float MaxScrapeSpeed = 300.0f;

    // ── PERCEPTUAL TUNING (optional, default OFF) ─────────────────────────

    /**
     * Apply a mild perceptual pitch bias based on object volume.
     * OFF by default — FEM already computes physically-correct frequencies
     * for the actual geometry. Enabling this applies an additional ±20%
     * correction, which may help if comparing objects of very different sizes.
     *
     * When ON: f_adjusted = f_FEM × Clamp((V_ref/V)^0.07, 0.80, 1.20)
     * The exponent 0.07 is much smaller than the previous 0.2, which was
     * causing up to 4× frequency shifts and fighting the FEM physics.
     *
     * Reference intent: Grassi (2005), but only as a small perceptual nudge,
     * not a dominant correction.
     */
    UPROPERTY(EditAnywhere, BlueprintReadWrite, Category="Modal Sound|Tuning")
    bool bApplyPerceptualTuning = false;
	
	
	bool bListenerEnabled = false;
	
	void EnableListener();
	void DisableListener();


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
                       int32                 VertexIndex,
                       UModalSoundDataAsset* DataAsset,
                       FVector               ImpactNormal);

    UFUNCTION()
    void HandleSlide(float                 TangentialSpeed,
                      float                 NormalForce,
                      FVector               ContactPoint,
                      UModalSoundDataAsset* DataAsset,
                      int32                 VertexIndex);

    void InitScrapeAudio(UModalSoundDataAsset* DataAsset);

    UPROPERTY()
    UModalImpactComponent* ImpactComp = nullptr;

    // Persistent scrape audio component — created once, never destroyed until EndPlay.
    // Volume is controlled by setting "ScrapeGain" parameter each tick.
    UPROPERTY()
    UAudioComponent* ScrapeAudio = nullptr;

    bool bScrapeInitialised = false;

    // Elapsed time since last impact trigger. Reset to 0 in HandleImpact.
    // Used to ramp scrape gain from 0→full over PostImpactSuppressTime.
    float TimeSinceLastImpact = 9999.f;

    // Bridge-side one-pole smoothed scrape gain.
    // Separate from SmoothedSlideSpeed in ModalImpactComponent — this one
    // smooths the final gain value sent to MetaSound, absorbing sudden
    // speed jumps from impact/slide classification thrashing on long objects.
    float SmoothedScrapeGain = 0.f;

	bool bSlideActiveThisFrame = false;
};