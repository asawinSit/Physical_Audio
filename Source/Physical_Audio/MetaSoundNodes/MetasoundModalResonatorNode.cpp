// ============================================================================
// MetasoundModalResonatorBankNode.cpp
// ============================================================================
//
// Custom MetaSound node for CONTINUOUS friction/scraping sound.
// Implements a bank of biquad (IIR) resonators driven by an audio-rate
// excitation signal. Unlike the ModalSynthesizer (which triggers on impact),
// this node runs permanently and produces sound only while excitation is
// non-zero and ScrapeGain > 0. No trigger input, no attack transient.
//
// PHYSICS (van den Doel, Kry & Pai — FoleyAutomatic, SIGGRAPH 2001 §4.2):
//   Friction produces a continuous "audio force" f(t) at the contact point.
//   f(t) = bandpass-filtered white noise, centre frequency ∝ contact speed.
//   Each mode k is a damped resonator driven by f(t):
//     y_k[n] = a1_k*y[n-1] + a2_k*y[n-2] + b0_k*f[n]
//   The biquad coefficients are derived by impulse-invariant mapping of the
//   continuous-time pole  s_k = -ξ_k ω_k ± i ω_k √(1-ξ_k²).
//   Output = ScrapeGain × Σ_k A_k × y_k[n].
//
// USAGE IN MS_ModalScrape_v2:
//   White Noise → State Variable Filter (bandpass, Fc = ScrapeSpeed×1200 Hz)
//              → [Modal Resonator Bank] → Audio Gain (ScrapeGain) → Out Mono
//
// INTEGRATION (no plugin needed — add directly to Physical_Audio module):
//   1. Drop this file into Source/Physical_Audio/Private/
//   2. Build.cs already has all required dependencies.
//   3. In Physical_Audio.cpp (module StartupModule), add:
//        #include "MetasoundFrontend/Public/MetasoundFrontendRegistries.h"
//        FMetasoundFrontendRegistryContainer::Get()->RegisterPendingNodes();
//      (or call this once from anywhere before the MetaSound editor is opened)
//   4. The node appears in the MetaSound editor under "Physical Audio".
//
// API NOTE: This file uses the EXACT same MetaSound API patterns as the
//   existing ModalSynthesizer node (TNodeFacade, BindReadVertex,
//   TDataReadReference, GetOrCreateDefaultDataReadReference).
//   Do NOT use TArrayDataReadReference or BindReadReference — those are
//   from a different API version and will not compile in UE 5.7.
// ============================================================================

#include "MetasoundFacade.h"
#include "MetasoundAudioBuffer.h"
#include "MetasoundExecutableOperator.h"
#include "MetasoundNodeRegistrationMacro.h"
#include "MetasoundParamHelper.h"
#include "MetasoundPrimitives.h"
#include "MetasoundBuilderInterface.h"

#define LOCTEXT_NAMESPACE "PhysicalAudio_ModalResonatorBankNode"

namespace Metasound
{
    // ─────────────────────────────────────────────────────────────────────────
    // Pin names
    // ─────────────────────────────────────────────────────────────────────────
    namespace ModalResonatorBankVertexNames
    {
        METASOUND_PARAM(InputExcitation,
            "Excitation",
            "Audio-rate excitation signal. Use bandpass-filtered white noise "
            "with centre frequency proportional to ScrapeSpeed. "
            "van den Doel et al. (2001).");

        METASOUND_PARAM(InputFrequencies,
            "Frequencies",
            "Array of modal resonance frequencies in Hz (up to 20 modes).");

        METASOUND_PARAM(InputDampingRatios,
            "DampingRatios",
            "Per-mode damping ratio xi. Determines ring-down time of each mode.");

        METASOUND_PARAM(InputAmplitudes,
            "Amplitudes",
            "Per-mode weight A_k. Use DragSensitivity from DataAsset "
            "(low-pass shaped by frequency for friction excitation).");

        METASOUND_PARAM(InputScrapeGain,
            "ScrapeGain",
            "Master output gain. Set to 0 when not sliding (silent but running). "
            "Proportional to tangential speed × ScrapeGainScale.");

        METASOUND_PARAM(InputNumModes,
            "NumModes",
            "Number of active modes to process (1..20). "
            "Set equal to the length of the Frequencies array.");

        METASOUND_PARAM(OutputAudio,
            "AudioOut",
            "Summed modal resonator output (mono).");
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Per-mode IIR resonator state
    // ─────────────────────────────────────────────────────────────────────────
    struct FModalResonatorState
    {
        float y1 = 0.f;   // output sample y[n-1]
        float y2 = 0.f;   // output sample y[n-2]
    };

    // Biquad coefficients for one resonator mode
    struct FModalResonatorCoeffs
    {
        float a1 = 0.f;
        float a2 = 0.f;
        float b0 = 0.f;
    };

    // ─────────────────────────────────────────────────────────────────────────
    // Impulse-invariant biquad coefficients for a damped resonator
    //
    // Continuous-time transfer function:
    //   H(s) = b / (s² + 2ξω_n s + ω_n²)
    // Poles: s = -ξω_n ± i·ω_d,  ω_d = ω_n√(1-ξ²)
    //
    // Impulse-invariant discrete mapping (z = e^{s/Fs}):
    //   a1 = 2·exp(-ξω_n/Fs)·cos(ω_d/Fs)
    //   a2 = -exp(-2ξω_n/Fs)
    //   b0 = exp(-ξω_n/Fs)·sin(ω_d/Fs)   [matched to unit peak at resonance]
    //
    // We normalise b0 by 2ξ so that at resonance the steady-state gain ≈ 1.
    // Reference: Cook & Scavone, "The Synthesis ToolKit" (1999).
    // ─────────────────────────────────────────────────────────────────────────
    static FModalResonatorCoeffs ComputeResonatorCoeffs(float FreqHz,
                                                         float DampingRatio,
                                                         float SampleRate)
    {
        FModalResonatorCoeffs C;

        const float Xi      = FMath::Clamp(DampingRatio, 0.0001f, 0.9999f);
        const float F       = FMath::Clamp(FreqHz, 10.f, SampleRate * 0.499f);
        const float OmegaN  = F * 2.f * UE_PI;
        const float Xi_W    = Xi * OmegaN;
        const float OmegaD  = OmegaN * FMath::Sqrt(FMath::Max(1.f - Xi*Xi, 1e-8f));
        const float Decay   = FMath::Exp(-Xi_W / SampleRate);
        const float Theta   = OmegaD / SampleRate;
        const float SinT    = FMath::Sin(Theta);
        const float CosT    = FMath::Cos(Theta);

        C.a1 = 2.f * Decay * CosT;
        C.a2 = -(Decay * Decay);

        // Normalise so resonance peak ≈ 1/(2ξ) is cancelled out:
        //   b0 = Decay * sin(θ) * (2ξ)
        // This makes A_k directly control perceived loudness of each mode.
        // b0 WITHOUT 2ξ normalisation.
        // The "cancel resonance peak" normalisation (×2ξ) suppresses
        // narrow high-Q modes. For friction-driven sound, we WANT narrow
        // modes to ring strongly — that is the modal character of the scrape.
        // The peak gain at resonance is 1/(2ξ), which is correct physics:
        // a low-damping resonator rings more under the same excitation.
        // Reference: Cook & Scavone STK (1999); Steiglitz DSP Primer (1996).
        C.b0 = Decay * SinT;

        return C;
    }

    // ─────────────────────────────────────────────────────────────────────────
    // Operator
    // ─────────────────────────────────────────────────────────────────────────
    class FModalResonatorBankOperator
        : public TExecutableOperator<FModalResonatorBankOperator>
    {
    public:
        using FFloatArrayReadRef = TDataReadReference<TArray<float>>;

        static constexpr int32 MaxModes = 20;

        // ── GetVertexInterface ────────────────────────────────────────────────
        static const FVertexInterface& GetVertexInterface()
        {
            using namespace ModalResonatorBankVertexNames;

            static const FVertexInterface Interface(
                FInputVertexInterface(
                    TInputDataVertex<FAudioBuffer>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputExcitation)),
                    TInputDataVertex<TArray<float>>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputFrequencies)),
                    TInputDataVertex<TArray<float>>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputDampingRatios)),
                    TInputDataVertex<TArray<float>>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputAmplitudes)),
                    TInputDataVertex<float>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputScrapeGain),
                        0.0f),
                    TInputDataVertex<int32>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputNumModes),
                        10)
                ),
                FOutputVertexInterface(
                    TOutputDataVertex<FAudioBuffer>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(OutputAudio))
                )
            );
            return Interface;
        }

        // ── GetNodeInfo ───────────────────────────────────────────────────────
        static const FNodeClassMetadata& GetNodeInfo()
        {
            auto InitNodeInfo = []() -> FNodeClassMetadata
            {
                FNodeClassMetadata Info;
                Info.ClassName        = { TEXT("PhysicalAudio"),
                                          TEXT("ModalResonatorBank"),
                                          FName() };
                Info.MajorVersion     = 1;
                Info.MinorVersion     = 0;
                Info.DisplayName      = METASOUND_LOCTEXT(
                    "ModalResonatorBank_DisplayName", "Modal Resonator Bank");
                Info.Description      = METASOUND_LOCTEXT(
                    "ModalResonatorBank_Desc",
                    "Continuous bank of biquad resonators driven by an audio-rate "
                    "excitation signal. For scraping: connect bandpass-filtered white "
                    "noise (Fc proportional to ScrapeSpeed). No trigger — fades in/out "
                    "smoothly via ScrapeGain. van den Doel, Kry & Pai (2001).");
                Info.Author           = PluginAuthor;
                Info.PromptIfMissing  = PluginNodeMissingPrompt;
                Info.DefaultInterface = GetVertexInterface();
                Info.CategoryHierarchy.Emplace(
                    METASOUND_LOCTEXT("ModalResonatorBank_Category",
                                      "Physical Audio"));
                return Info;
            };

            static const FNodeClassMetadata Info = InitNodeInfo();
            return Info;
        }

        // ── CreateOperator ────────────────────────────────────────────────────
        static TUniquePtr<IOperator> CreateOperator(
            const FBuildOperatorParams& InParams,
            FBuildResults&              OutResults)
        {
            using namespace ModalResonatorBankVertexNames;
            const FInputVertexInterfaceData& InputData = InParams.InputData;

            FAudioBufferReadRef Excitation =
                InputData.GetOrCreateDefaultDataReadReference<FAudioBuffer>(
                    METASOUND_GET_PARAM_NAME(InputExcitation),
                    InParams.OperatorSettings);

            FFloatArrayReadRef Frequencies =
                InputData.GetOrCreateDefaultDataReadReference<TArray<float>>(
                    METASOUND_GET_PARAM_NAME(InputFrequencies),
                    InParams.OperatorSettings);

            FFloatArrayReadRef DampingRatios =
                InputData.GetOrCreateDefaultDataReadReference<TArray<float>>(
                    METASOUND_GET_PARAM_NAME(InputDampingRatios),
                    InParams.OperatorSettings);

            FFloatArrayReadRef Amplitudes =
                InputData.GetOrCreateDefaultDataReadReference<TArray<float>>(
                    METASOUND_GET_PARAM_NAME(InputAmplitudes),
                    InParams.OperatorSettings);

            FFloatReadRef ScrapeGain =
                InputData.GetOrCreateDefaultDataReadReference<float>(
                    METASOUND_GET_PARAM_NAME(InputScrapeGain),
                    InParams.OperatorSettings);

            FInt32ReadRef NumModes =
                InputData.GetOrCreateDefaultDataReadReference<int32>(
                    METASOUND_GET_PARAM_NAME(InputNumModes),
                    InParams.OperatorSettings);

            return MakeUnique<FModalResonatorBankOperator>(
                InParams.OperatorSettings,
                Excitation, Frequencies, DampingRatios,
                Amplitudes, ScrapeGain, NumModes);
        }

        // ── Constructor ───────────────────────────────────────────────────────
        FModalResonatorBankOperator(
            const FOperatorSettings& InSettings,
            FAudioBufferReadRef      InExcitation,
            FFloatArrayReadRef       InFrequencies,
            FFloatArrayReadRef       InDampingRatios,
            FFloatArrayReadRef       InAmplitudes,
            FFloatReadRef            InScrapeGain,
            FInt32ReadRef            InNumModes)
            : ExcitationIn   (MoveTemp(InExcitation))
            , FrequenciesIn  (MoveTemp(InFrequencies))
            , DampingRatiosIn(MoveTemp(InDampingRatios))
            , AmplitudesIn   (MoveTemp(InAmplitudes))
            , ScrapeGainIn   (MoveTemp(InScrapeGain))
            , NumModesIn     (MoveTemp(InNumModes))
            , AudioOut       (FAudioBufferWriteRef::CreateNew(InSettings))
            , SampleRate     (InSettings.GetSampleRate())
        {
            States.SetNum(MaxModes);
            Coeffs.SetNum(MaxModes);
            CachedFreqs.Init(0.f, MaxModes);
            CachedDamps.Init(0.f, MaxModes);
        }

        // ── BindInputs ────────────────────────────────────────────────────────
        virtual void BindInputs(FInputVertexInterfaceData& InData) override
        {
            using namespace ModalResonatorBankVertexNames;
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(InputExcitation),    ExcitationIn);
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(InputFrequencies),   FrequenciesIn);
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(InputDampingRatios), DampingRatiosIn);
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(InputAmplitudes),    AmplitudesIn);
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(InputScrapeGain),    ScrapeGainIn);
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(InputNumModes),      NumModesIn);
        }

        // ── BindOutputs ───────────────────────────────────────────────────────
        virtual void BindOutputs(FOutputVertexInterfaceData& InData) override
        {
            using namespace ModalResonatorBankVertexNames;
            InData.BindReadVertex(METASOUND_GET_PARAM_NAME(OutputAudio), AudioOut);
        }

        // ── Reset ─────────────────────────────────────────────────────────────
        void Reset(const IOperator::FResetParams& InParams)
        {
            AudioOut->Zero();
            for (FModalResonatorState& S : States)
            {
                S.y1 = 0.f;
                S.y2 = 0.f;
            }
        }

        // ── Execute ───────────────────────────────────────────────────────────
        // Called every audio buffer (~10 ms at 48 kHz, 512 frames).
        // Implements: y_k[n] = a1*y[n-1] + a2*y[n-2] + b0*x[n]
        // Output = ScrapeGain * Σ_k A_k * y_k[n]
        void Execute()
        {
            const int32 N = FMath::Clamp(*NumModesIn, 1, MaxModes);
            const float Gain = *ScrapeGainIn;

            float* OutData      = AudioOut->GetData();
            const int32 NumFrames = AudioOut->Num();

            FMemory::Memzero(OutData, NumFrames * sizeof(float));

            // Early-out: gain is zero — still drain resonator state to zero
            // so there's no transient when it comes back up.
            // We still process the DSP loop but skip adding to output.
            const bool bAudible = (Gain > 1e-6f);

            // ── Refresh IIR coefficients if frequencies/dampings changed ──────
            const TArray<float>& Freqs = *FrequenciesIn;
            const TArray<float>& Damps = *DampingRatiosIn;

            for (int32 k = 0; k < N; ++k)
            {
                const float F = Freqs.IsValidIndex(k) ? Freqs[k] : 440.f;
                const float D = Damps.IsValidIndex(k) ? Damps[k] : 0.01f;

                // Only recompute if parameters changed (saves CPU on steady slides)
                if (!FMath::IsNearlyEqual(F, CachedFreqs[k], 0.5f) ||
                    !FMath::IsNearlyEqual(D, CachedDamps[k], 1e-5f))
                {
                    Coeffs[k]      = ComputeResonatorCoeffs(F, D, SampleRate);
                    CachedFreqs[k] = F;
                    CachedDamps[k] = D;
                }
            }

            // ── DSP loop: sample-by-sample resonator bank ─────────────────────
            const float* ExcitData = ExcitationIn->GetData();
            const TArray<float>& Amps = *AmplitudesIn;

            for (int32 k = 0; k < N; ++k)
            {
                const float Ak = Amps.IsValidIndex(k) ? Amps[k] : 0.f;
                if (Ak < 1e-9f) continue;   // skip inaudible modes

                const FModalResonatorCoeffs& C = Coeffs[k];
                FModalResonatorState& S         = States[k];
                const float AkGain = Ak * Gain;

                for (int32 i = 0; i < NumFrames; ++i)
                {
                    // Core biquad: direct-form II transposed
                    const float yn = C.a1 * S.y1 + C.a2 * S.y2 + C.b0 * ExcitData[i];

                    // Protect against numerical blow-up (should not occur with
                    // stable poles, but guards against edge cases at Nyquist)
                    S.y2 = FMath::IsFinite(S.y1) ? S.y1 : 0.f;
                    S.y1 = FMath::IsFinite(yn)   ? yn   : 0.f;

                    if (bAudible)
                        OutData[i] += AkGain * S.y1;
                }
            }

            // ── Soft-clip output to ±1 (tanh) ─────────────────────────────────
            // Prevents clipping if many modes sum to large amplitude.
            // Tanh is smooth — no harsh clipping artifacts.
            if (bAudible)
            {
                for (int32 i = 0; i < NumFrames; ++i)
                    OutData[i] = FMath::Tanh(OutData[i]);
            }
        }

    private:
        FAudioBufferReadRef  ExcitationIn;
        FFloatArrayReadRef   FrequenciesIn;
        FFloatArrayReadRef   DampingRatiosIn;
        FFloatArrayReadRef   AmplitudesIn;
        FFloatReadRef        ScrapeGainIn;
        FInt32ReadRef        NumModesIn;

        FAudioBufferWriteRef AudioOut;

        float SampleRate;

        TArray<FModalResonatorCoeffs> Coeffs;
        TArray<FModalResonatorState>  States;
        TArray<float>                 CachedFreqs;
        TArray<float>                 CachedDamps;
    };

    // ─────────────────────────────────────────────────────────────────────────
    // Node registration — same TNodeFacade pattern as ModalSynthesizer
    // ─────────────────────────────────────────────────────────────────────────
    using FModalResonatorBankNode = TNodeFacade<FModalResonatorBankOperator>;
    METASOUND_REGISTER_NODE(FModalResonatorBankNode)

} // namespace Metasound

#undef LOCTEXT_NAMESPACE