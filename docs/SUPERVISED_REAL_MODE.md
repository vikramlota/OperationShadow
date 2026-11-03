# Supervised Real-Hardware Mode Checklist (`docs/SUPERVISED_REAL_MODE.md`)

> **CRITICAL FIRMWARE FAILSAFE WARNING (READ FIRST)**
>
> The ESP-NOW transmitter firmware **continues broadcasting its last received motor command (`W`, `A`, `S`, `D`) indefinitely if USB serial input stops** (for example, if the USB cable is unplugged, the serial adapter resets, or the backend host crashes before it can transmit `X`).
>
> The backend `ScoutSerialWriter` does **not** modify transmitter or receiver firmware and **cannot** guarantee a motor stop after USB loss. Therefore:
> 1. An independent hardware/firmware USB-loss motor-stop failsafe **must** be verified on a bench with wheels lifted before `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED` is ever enabled.
> 2. **A human operator — never an AI agent or automated script — makes the decision** to verify the failsafe and unlock real hardware mode.

---

## 1. Mandatory Pre-Flight Preconditions

Before changing any safety flag away from the repository defaults (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`), a human operator must verify and sign off on all four physical preconditions:

1. **Physical Clearance or Wheels Lifted**:
   - For initial bench/serial checks: `SCOUT01` must be elevated on a stable test stand with all four wheels lifted clear of the surface.
   - For floor trials: an open, flat operating area must be cleared of people, cables, and fragile obstacles.
2. **Named Safety Officer Holding the Physical E-Stop**:
   - A specific, named human safety officer must be designated before power-on.
   - That person must hold the physical hardware E-Stop (or stay within immediate reach of the rover main battery cutoff switch) continuously while the rover is powered.
   - Software E-Stop (`POST /emergency-stop`) is a secondary control only and must never substitute for the physical E-Stop.
3. **Defined Stop Condition and Venue Boundary**:
   - Explicit physical venue boundaries (matching or stricter than `configs/geofence.json`, `0 <= x <= 20m`, `0 <= y <= 15m`) must be marked or agreed upon.
   - A clear stop condition must be defined prior to dispatch (e.g., immediately cut power if the rover approaches within 1.0 m of any boundary, wall, or person, or if telemetry/heartbeat lags by > 1.5 s).
4. **Rover Battery Known and Healthy**:
   - Both drive battery and logic/companion power on `SCOUT01` must be measured and confirmed known prior to enabling real mode.
   - Battery state of charge must be strictly above the 20% safety cutoff (recommended >= 50% for floor tests).

---

## 2. Environment Variable Changes Required for Supervised Real Mode

Repository code (`backend/fastapi/core/config.py`) and `.env.example` enforce safe defaults at startup. A human operator who has completed Section 1 above may configure a local, uncommitted `.env` according to the table below:

| Environment Variable | Repository Safe Default | Supervised Real-Mode Value | Gate Purpose |
| --- | --- | --- | --- |
| `SHADOW_HARDWARE_LOCKOUT` | `true` | Set to `false` (human-only) | Primary software interlock; `core/config.py` raises `ValueError` at startup if mode is `real` while lockout is `true`. |
| `SHADOW_DEFAULT_VEHICLE_MODE` | `simulated` | Set to `real` (human-only) | Switches default vehicle adapter and mission execution target from simulated to physical `SCOUT01`. |
| `SHADOW_SCOUT_SERIAL_ENABLED` | `false` | Set to `true` only if using USB serial transmitter | Enables `ScoutSerialWriter` on `SHADOW_SCOUT_SERIAL_PORT` (`115200` baud). |
| `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED` | `false` | Set to `true` **only after human verification** of USB-loss motor cutoff | Confirms that an independent transmitter/receiver failsafe stops the motors if serial input ceases. |
| `SHADOW_VEHICLE_SHARED_SECRET` | `provisional_secret_change_me` | Non-default random secret | Required whenever `SHADOW_HOST` is bound to a non-loopback interface (`!= 127.0.0.1`). |

> **Never commit real-mode settings:** Keep `.env` untracked (`.gitignore` excludes `.env`) and restore all four safety flags to their safe defaults (`SHADOW_HARDWARE_LOCKOUT=true`, `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, `SHADOW_SCOUT_SERIAL_ENABLED=false`, `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false`) immediately after concluding a supervised hardware session.

---

## 3. Startup Verification & Shutdown Procedure

1. **Startup Warning Check**:
   - When the FastAPI backend starts with hardware lockout disabled or default mode set to `real`, `operation_shadow.main` logs a structured `WARNING` message (`SAFETY WARNING: Hardware lockout is disabled or default vehicle mode is 'real'...`). Confirm this warning matches your intended supervised session.
   - If `SHADOW_VEHICLE_SHARED_SECRET` is still a placeholder (`provisional_secret_change_me` or `change-me-local-demo-secret`) while `SHADOW_HOST` is not `127.0.0.1`, a second `WARNING` (`SECURITY WARNING`) is emitted—rotate the secret before binding to a LAN interface.
2. **Post-Session Return to Safe State**:
   - Send `X` (stop) and engage `POST /emergency-stop`.
   - Power off `SCOUT01` motor stage first, then companion computer/transmitter.
   - Revert `.env` to `SHADOW_HARDWARE_LOCKOUT=true` and `SHADOW_DEFAULT_VEHICLE_MODE=simulated`, and verify `GET /health` reports `"hardware_lockout": true` and `"default_vehicle_mode": "simulated"`.
