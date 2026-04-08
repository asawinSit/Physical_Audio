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

### Install on Windows (PowerShell)
 
```powershell
& "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install numpy
& "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install scipy
& "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install tetgen --prefer-binary
& "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install meshio
& "C:\Program Files\Epic Games\UE_5.7\Engine\Binaries\ThirdParty\Python3\Win64\python.exe" -m pip install pymeshfix
```
 
### Install on Mac (Terminal)
 
```bash
/Users/Shared/Epic\ Games/UE_5.7/Engine/Binaries/ThirdParty/Python3/Mac/bin/python3 -m pip install numpy
/Users/Shared/Epic\ Games/UE_5.7/Engine/Binaries/ThirdParty/Python3/Mac/bin/python3 -m pip install scipy
/Users/Shared/Epic\ Games/UE_5.7/Engine/Binaries/ThirdParty/Python3/Mac/bin/python3 -m pip install tetgen --prefer-binary
/Users/Shared/Epic\ Games/UE_5.7/Engine/Binaries/ThirdParty/Python3/Mac/bin/python3 -m pip install meshio
/Users/Shared/Epic\ Games/UE_5.7/Engine/Binaries/ThirdParty/Python3/Mac/bin/python3 -m pip install pymeshfix
```
 
> **Note for Mac users:** The path above assumes a standard Unreal Engine installation. If you installed UE elsewhere, adjust the path accordingly. You can find the correct path by right-clicking Unreal Engine in the Epic Games Launcher, selecting **Show in Finder**, then navigating to `Engine/Binaries/ThirdParty/Python3/Mac/bin/python3`.

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

1. Make your blueprint actor inherit from **InteractableObject**
2. Assign the generated `ModalSoundDataAsset` to the Modal Impact Component
3. Adjust the parameters in Modal Impact Component for the desired sound
4. Play — impact and sliding sounds will be generated procedurally on collision
5. 
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
| `E` (press, while grabbing) | Throw held object |

---

## Authors

Asawin Sitthi & Chris Pilegård
Supervisor: Thomas Westin
Stockholm University, Spring 2026
