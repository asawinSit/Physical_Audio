# Physically Based Collision Audio of Rigid Bodies in Video Games

Bachelor's degree project in Computer Game Development at the Department of Computer and Systems Sciences, Stockholm University.

This project implements a physically based modal sound synthesis pipeline within Unreal Engine 5.7. It includes an offline FEM-based modal analysis tool and a real-time MetaSound synthesis system for impact and sliding audio.

---

## Requirements

- **Unreal Engine 5.7**
- **Python Editor Script Plugin** — enable in Unreal Engine plugins menu

---

## Python Dependencies

The offline pipeline runs inside Unreal Engine's bundled Python interpreter. You must install the packages into **Unreal Engine's Python**, not your system Python.

### Install

Open Command Prompt and run:

```bat
set UE_PYTHON="C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe"

%UE_PYTHON% -m pip install --upgrade pip
%UE_PYTHON% -m pip install numpy
%UE_PYTHON% -m pip install scipy
%UE_PYTHON% -m pip install tetgen --prefer-binary
%UE_PYTHON% -m pip install meshio
%UE_PYTHON% -m pip install pymeshfix
```

Or in PowerShell:

```powershell
$UE_PYTHON = "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe"

& $UE_PYTHON -m pip install --upgrade pip
& $UE_PYTHON -m pip install numpy
& $UE_PYTHON -m pip install scipy
& $UE_PYTHON -m pip install tetgen --prefer-binary
& $UE_PYTHON -m pip install meshio
& $UE_PYTHON -m pip install pymeshfix
```

### Packages

| Package | Version | Purpose |
|---------|---------|---------|
| `numpy` | 2.4.2 | Array operations and linear algebra |
| `scipy` | 1.17.1 | Sparse eigenvalue solver for extracting modal frequencies and mode shapes |
| `tetgen` | 0.8.2 | Converts surface meshes into volumetric tetrahedral meshes |
| `meshio` | 5.3.5 | Mesh format conversion |
| `pymeshfix` | 0.18.0 | Repairs broken or non-manifold meshes |

---

## Usage

### 1. Generate a Modal Data Asset

1. Open the Unreal Engine editor
2. Open the Editor Utility Widget included in the project
3. Select a Static Mesh, a material preset, and a mode count
4. Click **Generate** to run the FEM pipeline
5. A `ModalSoundDataAsset` will be saved to your chosen output folder

### 2. Assign to an Actor

1. Add the **Modal Impact Component** to your actor Blueprint
2. Add the **Modal MetaSound Bridge** to the same actor
3. Assign the generated `ModalSoundDataAsset` to the Modal Impact Component
4. Assign `MS_ModalImpact` and `MS_ModalScrape` to the Modal MetaSound Bridge
5. Ensure the actor has a physics-enabled collision mesh
6. Play — impact and sliding sounds will be generated procedurally on collision

---

## Controls

| Input | Action |
|-------|--------|
| `W A S D` | Move |
| `Space` | Jump |
| `Left Mouse Button` (hold, aim at object) | Grab object |
| `Left Mouse Button` (release) | Release object |
| `Right Mouse Button` (hold, while grabbing) | Rotate held object |
| `Right Mouse Button` (release) | Stop rotating |

---

## Authors

Asawin Sitthi & Chris Pilegård
Supervisor: Thomas Westin
Stockholm University, Spring 2026
