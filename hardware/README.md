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

## ESP32 Transmitter Integration Status
The working ESP-NOW transmitter sketch provided by the hardware team reads individual serial characters at 115200 baud. It accepts `W`, `A`, `S`, `D`, and `X` (case-insensitive) with no newline, and `X` sets the motor packet to zero. The backend serial writer follows this exact one-byte contract. The separate `scout/communication/serial_bridge.py` remains a provisional companion-to-drivetrain frame (`M,left,right\n`) and must not be used for this transmitter.

USB serial writes are disabled by default. Configuration requires all of `SHADOW_SCOUT_SERIAL_ENABLED=true`, `SHADOW_DEFAULT_VEHICLE_MODE=real`, `SHADOW_HARDWARE_LOCKOUT=false`, and `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true` before the backend opens the configured port. Keep these disabled until a separate transmitter/receiver failsafe on USB loss is verified. On browser disconnect, backend shutdown, or control lease expiry, the backend sends `X` when the serial connection is still available. If USB has already failed, it cannot deliver `X`.

Bench-test with wheels lifted and motor power disabled before any controlled motion test. Keep `SHADOW_HARDWARE_LOCKOUT=true` and serial disabled until the project safety gate is formally cleared.
