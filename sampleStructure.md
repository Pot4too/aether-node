my-iot-device/
├── firmware/
│   ├── src/
│   │   ├── main.c
│   │   ├── sensors/
│   │   ├── wifi/
│   │   └── mqtt/
│   ├── include/
│   ├── platformio.ini        ← or CMakeLists.txt for ESP-IDF
│   └── partitions.csv
│
├── hardware/
│   ├── pcb/
│   │   ├── my-device.kicad_pro
│   │   ├── my-device.kicad_sch
│   │   ├── my-device.kicad_pcb
│   │   ├── symbols/          ← custom schematic symbols
│   │   ├── footprints/       ← custom footprints
│   │   └── fabrication/
│   │       ├── gerbers/
│   │       ├── bom.csv
│   │       └── cpl.csv       ← component placement for PCBA
│   └── docs/
│       ├── schematic.pdf
│       └── ibom.html         ← interactive BOM
│
├── enclosure/
│   ├── cad/
│   │   ├── case.f3d          ← Fusion360 source
│   │   └── case.step         ← neutral export
│   └── stl/
│       ├── top.stl
│       ├── bottom.stl
│       └── print-settings.md
│
├── docs/
│   ├── architecture.md
│   ├── pinout.md
│   └── flashing.md
│
├── tools/                    ← scripts: flash, OTA, test
│   ├── flash.sh
│   └── monitor.py
│
├── .gitignore
├── LICENSE
└── README.md