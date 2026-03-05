// Copyright Epic Games, Inc. All Rights Reserved.
#include "Physical_Audio.h"
#include "Modules/ModuleManager.h"
#include "MetasoundFrontendModuleRegistrationMacros.h"

// Force linker to include ModalSynthNode translation unit
#include "MetaSoundNodes/ModalSynthNode.cpp"

class FPhysicalAudioModule : public FDefaultGameModuleImpl
{
public:
	virtual void StartupModule() override
	{
		FDefaultGameModuleImpl::StartupModule();

		// Register all MetaSound nodes and data types defined in this module.
		// Must be called after MetaSound Engine is initialized.
		// Reference: MetasoundFrontendModuleRegistrationMacros.h
		METASOUND_REGISTER_ITEMS_IN_MODULE;

		UE_LOG(LogTemp, Log,
			TEXT("PhysicalAudio: MetaSound nodes registered."));
	}
};

IMPLEMENT_PRIMARY_GAME_MODULE(FPhysicalAudioModule, Physical_Audio, "Physical_Audio");
DEFINE_LOG_CATEGORY(LogPhysical_Audio)