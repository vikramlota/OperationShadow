# Quick Start: Physical Movement Test

**5-Minute Guide to Testing Scout Physical Movement with ESP32**

---

## Prerequisites Check (2 minutes)

```bash
# 1. Find your ESP32 port
ls /dev/cu.usbmodem* 2>/dev/null || ls /dev/ttyUSB* 2>/dev/null || ls /dev/ttyACM* 2>/dev/null

# You should see something like: /dev/cu.usbmodem14201
# If nothing appears, check USB connection and try again
```

```bash
# 2. Test ESP32 directly (IMPORTANT!)
screen /dev/cu.usbmodem14201 115200

# Type: W (Scout should move forward if connected)
# Type: X (Scout should stop)
# Exit: Ctrl+A, then K, then Y

# ⚠️ If this doesn't work, fix ESP32/firmware first before continuing!
```

---

## Configure for Physical Testing (1 minute)

```bash
# Navigate to project
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new"

# Backup current config
cp .env .env.backup

# Edit configuration
nano .env
# OR
code .env
```

**Change these 4 lines** (adjust port to match YOUR ESP32):

```bash
SHADOW_HARDWARE_LOCKOUT=false
SHADOW_DEFAULT_VEHICLE_MODE=real
SHADOW_SCOUT_SERIAL_ENABLED=true
SHADOW_SCOUT_SERIAL_PORT=/dev/cu.usbmodem14201  # ⬅️ Use YOUR port from step 1
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true      # ⚠️ Only if USB-loss failsafe verified!
```

Save and close (Ctrl+X, Y, Enter in nano).

---

## Start System (1 minute)

**Terminal 1 - Backend:**
```bash
cd backend
source .venv/bin/activate
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

**Wait for**: `INFO:     Connected to SCOUT01 ESP32 serial on /dev/cu.usbmodem14201`

**Terminal 2 - Frontend:**
```bash
cd frontend
npm run dev
```

---

## Test with Interactive Script (Recommended)

**Terminal 3:**
```bash
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new"
python3 test_esp32_physical.py
```

**Menu Options:**
1. **Run Diagnostic** ← Start here! Checks everything
2. Check Backend Health
3. Check Scout Control Status
4. **Test Basic Movement** ← Tests W/A/S/D/X sequence
5. Test Emergency Stop
6. Test Mission Execution

---

## Manual Testing (Alternative)

### Test 1: Verify Connection
```bash
curl http://localhost:8000/scout/control/status | python3 -m json.tool
```

**Expected**: `"transmitter_connected": true`

### Test 2: Browser Controls

1. Open http://localhost:5173
2. Find "SCOUT CONTROL" panel
3. Click to focus
4. Press `W` key → Scout moves forward
5. Release → Scout stops (auto X)
6. Test `A`, `S`, `D` keys

### Test 3: Send Commands via API

```bash
# Stop (safest first command)
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "stop"}'

# Forward for 0.5 seconds
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}'
```

### Test 4: Emergency Stop

```bash
curl -X POST http://localhost:8000/emergency-stop \
  -H "Content-Type: application/json" \
  -d '{"scope": "ALL"}'

# Reset
curl -X POST http://localhost:8000/emergency-stop/reset
```

---

## Troubleshooting

### ❌ "transmitter_connected: false"

**Fix:**
1. Check USB cable plugged in
2. Verify port: `ls /dev/cu.usbmodem*`
3. Check `.env` has correct port
4. Restart backend

### ❌ "Serial writes remain disabled"

**Fix:**
1. Verify all 4 gates in `.env`:
   ```bash
   cat .env | grep -E "HARDWARE_LOCKOUT|VEHICLE_MODE|SERIAL_ENABLED|FAILSAFE"
   ```
2. All should be: `false`, `real`, `true`, `true`
3. Restart backend

### ❌ Commands sent but Scout doesn't move

**Fix:**
1. Test ESP32 directly with `screen` (see Prerequisites)
2. Check ESP-NOW pairing with Scout receiver
3. Verify motor power supply
4. Lift wheels and try again (less load)

### ❌ Scout won't stop

**⚠️ CRITICAL - IMMEDIATELY:**
1. Cut motor power
2. Unplug USB
3. DO NOT continue until stop is reliable

---

## Safety Reminders

Before testing:
- ✅ Clear area or lift wheels
- ✅ Know where motor power switch is
- ✅ Emergency stop tested and working
- ✅ Someone supervising
- ✅ USB-loss failsafe verified (or manual control of cable)

**Emergency stop command** (keep ready):
```bash
curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'
```

---

## Revert to Simulation

After testing:

```bash
nano .env

# Change back to:
SHADOW_HARDWARE_LOCKOUT=true
SHADOW_DEFAULT_VEHICLE_MODE=simulated
SHADOW_SCOUT_SERIAL_ENABLED=false
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false

# Restart backend (Ctrl+C and restart)
```

---

## Success Criteria

You're successful when:

✅ `curl http://localhost:8000/scout/control/status` shows `transmitter_connected: true`
✅ Browser controls (W/A/S/D/X) cause real Scout movement
✅ Releasing key stops Scout immediately
✅ Emergency stop works reliably
✅ Backend logs show no serial errors

---

## Need More Detail?

See full guide: `ESP32_PHYSICAL_MOVEMENT_TEST.md`

For mission testing: `RUN_MISSION_FROM_FRONTEND.md`
