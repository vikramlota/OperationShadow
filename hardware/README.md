# Hardware Layer — Operation Shadow

This directory isolates all physical robotics compute, vehicle control software, and embedded microcontroller firmware from the web backend and operator frontend.

## Directory Structure

```text
hardware/
├── scout/                    # SCOUT01 Ground Rover System (Raspberry Pi, TRD §8, §15)
│   ├── navigation/           # Waypoint following + reactive obstacle avoidance
│   ├── sensors/              # Sensor fusion (IMU, ToF, ultrasonic, temp, gas, camera)
│   ├── communication/        # Authenticated WebSocket client & ESP32 UART bridge
│   └── main.py               # SCOUT Pi entry point
└── firmware/                 # SCOUT01 Low-Level Hardware Firmware (TRD §8, §26)
    └── esp32/
        ├── motors/           # PWM motor controller & emergency cutoff
        ├── sensors/          # Ultrasonic, ToF, temperature & gas ADC reader
        ├── safety/           # Hardware kill switch & watchdog safety layer
        └── main.cpp          # ESP32 main setup/loop entry
```

## Safety Invariant
Real hardware (`mode = "real"`) remains locked out under `SHADOW_HARDWARE_LOCKOUT=true` until TRD v2.0 §7 command schemas and hardware failsafes are validated. All standard benchmarks run using simulated vehicles against the FastAPI C2 backend.
