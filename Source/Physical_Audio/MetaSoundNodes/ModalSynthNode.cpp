// Modal Synthesis MetaSound Node
// s(t) = sum_k A_k * e^(-xi_k * omega_k * t) * sin(omega_d,k * t)
// Reference: van den Doel & Pai (1998). Presence 7(4).

#include "MetasoundFacade.h"
#include "MetasoundAudioBuffer.h"
#include "MetasoundExecutableOperator.h"
#include "MetasoundNodeRegistrationMacro.h"
#include "MetasoundParamHelper.h"
#include "MetasoundPrimitives.h"
#include "MetasoundTrigger.h"
#include "MetasoundBuilderInterface.h"

#define LOCTEXT_NAMESPACE "PhysicalAudio_ModalSynthNode"

namespace Metasound
{
    // ── Pin names ──────────────────────────────────────────────────
    namespace ModalSynthVertexNames
    {
        METASOUND_PARAM(InputTrigger,
            "Trigger",
            "Trigger a new impact. Restarts all modal oscillators.");
        METASOUND_PARAM(InputFrequencies,
            "Frequencies",
            "Array of modal frequencies in Hz.");
        METASOUND_PARAM(InputAmplitudes,
            "Amplitudes",
            "Per-mode excitation amplitudes (vertex participation * kinetic energy).");
        METASOUND_PARAM(InputDampingRatios,
            "DampingRatios",
            "Per-mode Rayleigh damping ratios xi.");
        METASOUND_PARAM(InputMasterGain,
            "MasterGain",
            "Overall output gain multiplier.");
        METASOUND_PARAM(OutputAudio,
            "AudioOut",
            "Synthesized modal impact sound (mono).");
        METASOUND_PARAM(OutputOnImpact,
            "OnImpact",
            "Fires once when a new impact is triggered.");
        METASOUND_PARAM(OutputOnSilence,
            "OnSilence",
            "Fires once when all modes have fully decayed.");
    }

    // ── Per-mode oscillator state ──────────────────────────────────
    struct FModalOscillator
    {
        float Phase        = 0.f;
        float Envelope     = 0.f;
        float PeakAmp      = 0.f;
        float PhaseInc     = 0.f;
        float DecayPerSamp = 1.f;
        bool  bActive      = false;

        void Trigger(float FreqHz, float DampingRatio,
                     float Amplitude, float SampleRate)
        {
            if (Amplitude < 1e-7f) { bActive = false; return; }

            const float Xi     = FMath::Clamp(DampingRatio, 1e-4f, 0.99f);
            const float OmegaN = FreqHz * 2.f * UE_PI;
            const float OmegaD = OmegaN *
                FMath::Sqrt(FMath::Max(0.f, 1.f - Xi * Xi));

            PhaseInc     = OmegaD / SampleRate;
            DecayPerSamp = FMath::Exp(-(Xi * OmegaN) / SampleRate);
            Phase        = 0.f;
            Envelope     = Amplitude;
            PeakAmp      = Amplitude;
            bActive      = true;
        }

        bool IsAudible() const
        {
            return bActive && (Envelope > PeakAmp * 0.001f);
        }
    };

    // ── Operator ───────────────────────────────────────────────────
    class FModalSynthOperator
        : public TExecutableOperator<FModalSynthOperator>
    {
    public:
        using FFloatArrayReadRef = TDataReadReference<TArray<float>>;

        // ── GetVertexInterface ─────────────────────────────────────
        static const FVertexInterface& GetVertexInterface()
        {
            using namespace ModalSynthVertexNames;

            static const FVertexInterface Interface(
                FInputVertexInterface(
                    TInputDataVertex<FTrigger>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputTrigger)),
                    TInputDataVertex<TArray<float>>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputFrequencies)),
                    TInputDataVertex<TArray<float>>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputAmplitudes)),
                    TInputDataVertex<TArray<float>>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputDampingRatios)),
                    TInputDataVertex<float>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(InputMasterGain),
                        1.0f)
                ),
                FOutputVertexInterface(
                    TOutputDataVertex<FAudioBuffer>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(OutputAudio)),
                    TOutputDataVertex<FTrigger>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(OutputOnImpact)),
                    TOutputDataVertex<FTrigger>(
                        METASOUND_GET_PARAM_NAME_AND_METADATA(OutputOnSilence))
                )
            );

            return Interface;
        }

        // ── GetNodeInfo ────────────────────────────────────────────
        static const FNodeClassMetadata& GetNodeInfo()
        {
            auto InitNodeInfo = []() -> FNodeClassMetadata
            {
                FNodeClassMetadata Info;
                Info.ClassName        = { TEXT("PhysicalAudio"),
                                          TEXT("ModalSynthesizer"),
                                          FName() };
                Info.MajorVersion     = 1;
                Info.MinorVersion     = 0;
                Info.DisplayName      = METASOUND_LOCTEXT(
                    "ModalSynth_DisplayName", "Modal Synthesizer");
                Info.Description      = METASOUND_LOCTEXT(
                    "ModalSynth_Desc",
                    "Real-time modal impact synthesis. Sums exponentially "
                    "decaying sinusoids weighted by impact location and "
                    "kinetic energy. van den Doel & Pai (1998).");
                Info.Author           = PluginAuthor;
                Info.PromptIfMissing  = PluginNodeMissingPrompt;
                Info.DefaultInterface = GetVertexInterface();
                Info.CategoryHierarchy.Emplace(
                    METASOUND_LOCTEXT("ModalSynth_Category",
                                      "Physical Audio"));
                return Info;
            };

            static const FNodeClassMetadata Info = InitNodeInfo();
            return Info;
        }

        // ── CreateOperator ─────────────────────────────────────────
        static TUniquePtr<IOperator> CreateOperator(
            const FBuildOperatorParams& InParams,
            FBuildResults&              OutResults)
        {
            using namespace ModalSynthVertexNames;
            const FInputVertexInterfaceData& InputData = InParams.InputData;

            FTriggerReadRef Trigger =
                InputData.GetOrCreateDefaultDataReadReference<FTrigger>(
                    METASOUND_GET_PARAM_NAME(InputTrigger),
                    InParams.OperatorSettings);

            FFloatArrayReadRef Frequencies =
                InputData.GetOrCreateDefaultDataReadReference<TArray<float>>(
                    METASOUND_GET_PARAM_NAME(InputFrequencies),
                    InParams.OperatorSettings);

            FFloatArrayReadRef Amplitudes =
                InputData.GetOrCreateDefaultDataReadReference<TArray<float>>(
                    METASOUND_GET_PARAM_NAME(InputAmplitudes),
                    InParams.OperatorSettings);

            FFloatArrayReadRef DampingRatios =
                InputData.GetOrCreateDefaultDataReadReference<TArray<float>>(
                    METASOUND_GET_PARAM_NAME(InputDampingRatios),
                    InParams.OperatorSettings);

            FFloatReadRef MasterGain =
                InputData.GetOrCreateDefaultDataReadReference<float>(
                    METASOUND_GET_PARAM_NAME(InputMasterGain),
                    InParams.OperatorSettings);

            return MakeUnique<FModalSynthOperator>(
                InParams.OperatorSettings,
                Trigger, Frequencies, Amplitudes, DampingRatios, MasterGain);
        }

        // ── Constructor ────────────────────────────────────────────
        FModalSynthOperator(
            const FOperatorSettings& InSettings,
            FTriggerReadRef          InTrigger,
            FFloatArrayReadRef       InFrequencies,
            FFloatArrayReadRef       InAmplitudes,
            FFloatArrayReadRef       InDampingRatios,
            FFloatReadRef            InMasterGain)
            : TriggerIn      (InTrigger)
            , FrequenciesIn  (InFrequencies)
            , AmplitudesIn   (InAmplitudes)
            , DampingRatiosIn(InDampingRatios)
            , MasterGainIn   (InMasterGain)
            , AudioOut       (FAudioBufferWriteRef::CreateNew(InSettings))
            , OnImpactOut    (FTriggerWriteRef::CreateNew(InSettings))
            , OnSilenceOut   (FTriggerWriteRef::CreateNew(InSettings))
            , SampleRate     (InSettings.GetSampleRate())
        {
            Oscillators.SetNum(40);
        }

        // ── BindInputs ─────────────────────────────────────────────
        virtual void BindInputs(FInputVertexInterfaceData& InData) override
        {
            using namespace ModalSynthVertexNames;
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(InputTrigger),       TriggerIn);
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(InputFrequencies),   FrequenciesIn);
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(InputAmplitudes),    AmplitudesIn);
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(InputDampingRatios), DampingRatiosIn);
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(InputMasterGain),    MasterGainIn);
        }

        // ── BindOutputs ────────────────────────────────────────────
        virtual void BindOutputs(FOutputVertexInterfaceData& InData) override
        {
            using namespace ModalSynthVertexNames;
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(OutputAudio),     AudioOut);
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(OutputOnImpact),  OnImpactOut);
            InData.BindReadVertex(
                METASOUND_GET_PARAM_NAME(OutputOnSilence), OnSilenceOut);
        }

        // ── Reset ──────────────────────────────────────────────────
        void Reset(const IOperator::FResetParams& InParams)
        {
            AudioOut->Zero();
            OnImpactOut->Reset();
            OnSilenceOut->Reset();
            for (FModalOscillator& Osc : Oscillators)
            {
                Osc.bActive  = false;
                Osc.Envelope = 0.f;
                Osc.Phase    = 0.f;
            }
            bWasActive = false;
        }

        // ── Execute ────────────────────────────────────────────────
        void Execute()
        {
            OnImpactOut->AdvanceBlock();
            OnSilenceOut->AdvanceBlock();

            float* OutBuffer = AudioOut->GetData();
            int32  NumFrames = AudioOut->Num();
            float  Gain      = *MasterGainIn;

            FMemory::Memzero(OutBuffer, NumFrames * sizeof(float));

            // Handle trigger
            if (TriggerIn->Num() > 0)
            {
                const TArray<float>& Freqs = *FrequenciesIn;
                const TArray<float>& Amps  = *AmplitudesIn;
                const TArray<float>& Damps = *DampingRatiosIn;

                int32 NumModes = FMath::Min3(
                    Freqs.Num(), Amps.Num(), Damps.Num());
                NumModes = FMath::Min(NumModes, Oscillators.Num());

                for (int32 k = 0; k < NumModes; ++k)
                    Oscillators[k].Trigger(
                        Freqs[k], Damps[k], Amps[k], SampleRate);

                for (int32 k = NumModes; k < Oscillators.Num(); ++k)
                    Oscillators[k].bActive = false;

                OnImpactOut->TriggerFrame(0);
                bWasActive = true;
            }

            // Synthesize: s(t) = sum_k A_k * e^(-xi_k*w_k*t) * sin(wd_k*t)
            bool bAnyActive = false;
            for (FModalOscillator& Osc : Oscillators)
            {
                if (!Osc.bActive) continue;
                bAnyActive = true;

                for (int32 n = 0; n < NumFrames; ++n)
                {
                    OutBuffer[n] += Osc.Envelope * FMath::Sin(Osc.Phase);
                    Osc.Phase    += Osc.PhaseInc;
                    Osc.Envelope *= Osc.DecayPerSamp;
                }

                Osc.Phase = FMath::Fmod(Osc.Phase, 2.f * UE_PI);

                if (!Osc.IsAudible())
                {
                    Osc.bActive  = false;
                    Osc.Envelope = 0.f;
                }
            }

            if (!FMath::IsNearlyEqual(Gain, 1.f))
                for (int32 n = 0; n < NumFrames; ++n)
                    OutBuffer[n] *= Gain;

            if (bWasActive && !bAnyActive)
            {
                OnSilenceOut->TriggerFrame(NumFrames - 1);
                bWasActive = false;
            }
        }

    private:
        FTriggerReadRef    TriggerIn;
        FFloatArrayReadRef FrequenciesIn;
        FFloatArrayReadRef AmplitudesIn;
        FFloatArrayReadRef DampingRatiosIn;
        FFloatReadRef      MasterGainIn;
        FAudioBufferWriteRef AudioOut;
        FTriggerWriteRef     OnImpactOut;
        FTriggerWriteRef     OnSilenceOut;
        TArray<FModalOscillator> Oscillators;
        float SampleRate = 48000.f;
        bool  bWasActive = false;
    };

    // TNodeFacade automatically provides CreateNodeClassMetadata()
    // and the correct constructor — recommended pattern in UE 5.7
    using FModalSynthNode = TNodeFacade<FModalSynthOperator>;
    METASOUND_REGISTER_NODE(FModalSynthNode)

} // namespace Metasound

#undef LOCTEXT_NAMESPACE