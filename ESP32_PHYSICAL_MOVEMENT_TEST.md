# ESP32 Physical Movement Test Guide

**Purpose**: Test SCOUT01 physical movement through backend and frontend with ESP32 device connected to laptop.

**⚠️ SAFETY FIRST**: Before any physical testing, ensure the Scout is in a safe environment with:
- No obstacles nearby
- Wheels lifted off the ground OR sufficient clearance
- Emergency stop button easily accessible
- Motors can be safely powered off immediately

---

## System Architecture Overview

```
Frontend (Browser) → Backend API/WebSocket → ScoutSerialWriter → ESP32 (USB Serial) → ESP-NOW → Scout Motors
```

- **Frontend**: React dashboard with keyboard control panel
- **Backend**: FastAPI with safety-gated serial writer
- **ESP32**: Connected via USB, receives single-byte commands (W/A/S/D/X)
- **Protocol**: 115200 baud, ASCII commands, no newline

---

## Pre-Test Verification

### 1. Check ESP32 Connection

```bash
# List available serial ports (macOS)
ls -la /dev/tty.* /dev/cu.*

# Common ESP32 ports on macOS:
# /dev/cu.usbmodem* (most ESP32 boards)
# /dev/cu.usbserial* (some ESP32 with USB-to-serial chips)

# Check which port is the ESP32
ls -la /dev/cu.usbmodem* 2>/dev/null || ls -la /dev/cu.usbserial* 2>/dev/null
```

**Expected Output**: You should see something like:
```
/dev/cu.usbmodem14201
/dev/cu.usbmodem14301
```

### 2. Verify ESP32 Firmware

The ESP32 should be running firmware that:
- Listens on serial at 115200 baud
- Accepts single uppercase ASCII characters: `W`, `A`, `S`, `D`, `X`
- Does NOT require newline characters
- Forwards commands via ESP-NOW to Scout's motor controller

### 3. Test Serial Communication (Manual)

Before using backend/frontend, verify basic serial communication:

```bash
# Install screen if not available
# brew install screen  # macOS

# Connect to ESP32 (replace with your port)
screen /dev/cu.usbmodem14201 115200

# Type these keys (you won't see echo):
# W - Should trigger forward movement
# X - Should stop
# A - Should turn left
# D - Should turn right
# S - Should move backward

# Exit screen: Ctrl+A, then K, then Y
```

**⚠️ Important**: If this manual test doesn't work, the backend won't work either. Fix ESP32 firmware first.

---

## Configuration for Physical Testing

### Safety Gates Overview

The system has **4 independent safety gates** that ALL must be cleared for physical movement:

1. **SHADOW_HARDWARE_LOCKOUT** = `false` (enables physical control)
2. **SHADOW_DEFAULT_VEHICLE_MODE** = `real` (sets Scout to real mode)
3. **SHADOW_SCOUT_SERIAL_ENABLED** = `true` (enables serial output)
4. **SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED** = `true` (confirms hardware failsafe exists)

### Step 1: Backup Current Configuration

```bash
cd /Users/khushman/Desktop/Operation\ Shadow/OperationShadow-new
cp .env .env.backup
```

### Step 2: Configure for Physical Testing

Edit `.env` file (or `backend/.env` if it exists):

```bash
# Open in editor
nano .env
# OR
code .env
```

**Change these values** (⚠️ ONLY after verifying hardware failsafes):

```bash
# === SAFETY GATES (ALL MUST BE CLEARED FOR PHYSICAL MOVEMENT) ===

# Gate 1: Disable hardware lockout (⚠️ DANGEROUS - only for supervised testing)
SHADOW_HARDWARE_LOCKOUT=false

# Gate 2: Set vehicle to real mode
SHADOW_DEFAULT_VEHICLE_MODE=real

# Gate 3: Enable serial output to ESP32
SHADOW_SCOUT_SERIAL_ENABLED=true

# Gate 4: Confirm USB-loss failsafe (⚠️ CRITICAL - see below)
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true

# Set the correct ESP32 serial port
SHADOW_SCOUT_SERIAL_PORT=/dev/cu.usbmodem14201  # ⬅️ Change to YOUR port

# Serial baudrate (should match ESP32 firmware)
SHADOW_SCOUT_SERIAL_BAUDRATE=115200
```

**⚠️ CRITICAL: Gate 4 (SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED)**

This gate should ONLY be set to `true` if:
- Your ESP32/Scout firmware has an **independent motor stop** when USB serial disconnects
- You have physically tested that motors STOP when USB cable is unplugged
- The firmware has a watchdog timer that stops motors if no commands received for >500ms

If you haven't verified this, keep it `false` and **manually hold the USB cable** during testing, or implement a hardware emergency stop button.

### Step 3: Verify Configuration

```bash
cd backend
cat .env | grep -E "HARDWARE_LOCKOUT|VEHICLE_MODE|SERIAL"
```

**Expected Output**:
```
SHADOW_HARDWARE_LOCKOUT=false
SHADOW_DEFAULT_VEHICLE_MODE=real
SHADOW_SCOUT_SERIAL_ENABLED=true
SHADOW_SCOUT_SERIAL_PORT=/dev/cu.usbmodem14201
SHADOW_SCOUT_SERIAL_BAUDRATE=115200
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true
```

---

## Test Sequence

### Test 1: Backend Serial Writer Verification

**Purpose**: Verify backend can open serial port and send commands.

```bash
# Terminal 1: Start backend with explicit config
cd backend
source .venv/bin/activate

# Load environment and start backend
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

**Watch backend logs for**:
```
INFO:     SCOUT01 serial writes remain disabled by configuration or safety gates.
# OR (if all gates cleared):
INFO:     Connected to SCOUT01 ESP32 serial on /dev/cu.usbmodem14201
```

If you see "serial writes remain disabled", check which gate is blocking:
- Review `.env` file
- Ensure backend is loading correct `.env` (check `backend/.env` vs root `.env`)
- Restart backend after changes

### Test 2: Health Check (Verify Configuration Loaded)

```bash
# Terminal 2: Check backend health
curl -s http://localhost:8000/health | python3 -m json.tool
```

**Expected Response**:
```json
{
  "status": "operational",
  "hardware_lockout": false,
  "vehicles": ["SCOUT01"],
  "vehicle_modes": {
    "SCOUT01": "real"
  },
  "ros_enabled": false,
  "ros_connection_state": "not_configured"
}
```

**Verify**:
- ✅ `hardware_lockout: false`
- ✅ `vehicle_modes.SCOUT01: "real"`

### Test 3: Scout Control Status (Check Serial Connection)

```bash
curl -s http://localhost:8000/scout/control/status | python3 -m json.tool
```

**Expected Response (if successful)**:
```json
{
  "mode": "real",
  "simulation_available": true,
  "transmitter_connected": true,
  "lease_owner": null,
  "current_command": "X",
  "lease_expires_at": null
}
```

**Verify**:
- ✅ `mode: "real"`
- ✅ `transmitter_connected: true` ← **CRITICAL**: If false, check serial port

**If transmitter_connected is false**:
1. Check ESP32 is connected: `ls /dev/cu.usbmodem*`
2. Verify port in `.env` matches actual port
3. Check backend logs for serial errors
4. Verify ESP32 is powered on and USB cable is good

### Test 4: Frontend Dashboard Test

```bash
# Terminal 3: Start frontend
cd ../frontend
npm run dev
```

**Open browser**: http://localhost:5173

**Locate Scout Control Panel**:
1. Look for "SCOUT CONTROL" panel on the dashboard
2. Should show control status
3. Check that it shows "REAL MODE" (not "SIMULATION ONLY")

**Verify Status Display**:
- Mode: REAL
- Transmitter: CONNECTED ✅ (or OFFLINE ❌)
- Current: X (stopped)

### Test 5: Manual Command Test (Backend API)

**⚠️ SAFETY**: Ensure Scout wheels are lifted or area is clear.

```bash
# Test stop command first (safest)
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "stop"}'

# Wait 2 seconds, then test forward
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}'

# Scout should move forward for 0.5 seconds then stop
```

**Watch for**:
- Backend logs showing serial write: `Sending 'W' to ESP32`
- ESP32 receiving command (LED might flash)
- Scout motors activating
- Automatic stop after 0.5 seconds

**If nothing happens**:
1. Check backend logs for errors
2. Verify `transmitter_connected: true` in status
3. Test manual serial with `screen` (see Pre-Test section)

### Test 6: WebSocket Keyboard Control (Frontend)

**⚠️ SAFETY**: Keep hand near emergency stop button.

**In Browser** (http://localhost:5173):

1. Click inside the "SCOUT CONTROL" panel to focus it
2. Press and hold `W` key
   - Scout should move forward
   - Status should show "Current: W"
   - Release `W`, should auto-send `X` (stop)

3. Test each control:
   - `W` - Forward (watch for movement)
   - `X` - Stop (verify motors stop)
   - `A` - Turn left
   - `D` - Turn right
   - `S` - Backward

**Expected Behavior**:
- Pressing key: Scout moves immediately
- Releasing key: Scout stops within 100ms
- Status updates in real-time
- WebSocket shows connected

**If controls don't work**:
1. Check browser console (F12) for errors
2. Verify WebSocket connection: Look for "WebSocket connected" in console
3. Check backend logs for WebSocket errors
4. Verify Scout Control panel shows "REAL MODE"

### Test 7: Emergency Stop Test

**Purpose**: Verify emergency stop works during movement.

**Test A: Browser Emergency Stop**

1. Press and hold `W` (Scout moves forward)
2. Click **Emergency Stop** button in dashboard
3. Verify:
   - Scout stops immediately
   - All controls become disabled
   - Dashboard shows "EMERGENCY STOP ACTIVE"
   - Backend logs: "Emergency Stop latched"

**Test B: Backend Emergency Stop**

```bash
# While Scout is moving, trigger emergency stop
curl -X POST http://localhost:8000/emergency-stop \
  -H "Content-Type: application/json" \
  -d '{"scope": "ALL"}'
```

**Verify**:
- Scout stops immediately
- All movement commands blocked
- Status shows emergency stop latched

**Reset Emergency Stop**:
```bash
curl -X POST http://localhost:8000/emergency-stop/reset
```

### Test 8: Mission Execution Test (Full Integration)

**Purpose**: Test complete mission flow with physical movement.

**Step 1: Create Test Mission**

```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{
    "objective": "Move forward 1 meter, rotate 360 degrees, then return to start position"
  }' | python3 -m json.tool
```

**Save the mission ID** from response (e.g., `M001`).

**Step 2: Review Generated Tasks**

```bash
# Replace M001 with your mission ID
curl -s http://localhost:8000/mission/M001 | python3 -m json.tool
```

**Verify tasks look reasonable** (should be move/rotate/move commands).

**Step 3: Execute Mission**

**⚠️ SAFETY**: Clear area, Scout on ground or wheels lifted.

```bash
curl -X POST http://localhost:8000/mission/M001/execute
```

**Watch for**:
- Backend logs showing each task
- Scout executing movement commands
- Mission status updates in dashboard (if open)
- Automatic stops between tasks

**Step 4: Monitor Mission Progress**

```bash
# Check mission status
curl -s http://localhost:8000/mission/M001 | python3 -m json.tool | grep -E "status|progress"

# View events
curl -s http://localhost:8000/events?mission_id=M001 | python3 -m json.tool
```

**Step 5: Emergency Stop During Mission** (if needed)

```bash
curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'
```

---

## Troubleshooting

### Issue: "transmitter_connected: false"

**Cause**: Backend cannot open serial port.

**Solutions**:
1. Check ESP32 connected: `ls /dev/cu.usbmodem*`
2. Verify correct port in `.env`: `SHADOW_SCOUT_SERIAL_PORT=/dev/cu.usbmodem14201`
3. Check port permissions (shouldn't be issue on macOS)
4. Try different USB port/cable
5. Check backend logs: `grep -i serial backend.log`
6. Restart backend after `.env` changes

### Issue: "Serial writes remain disabled"

**Cause**: One or more safety gates still active.

**Solutions**:
1. Check all 4 gates in `.env`:
   ```bash
   cat .env | grep -E "HARDWARE_LOCKOUT|VEHICLE_MODE|SERIAL_ENABLED|FAILSAFE_VERIFIED"
   ```
2. Verify backend loaded `.env`: Check startup logs
3. Restart backend after changes
4. Check if `backend/.env` overrides root `.env`

### Issue: Commands sent but Scout doesn't move

**Cause**: ESP32 receives commands but doesn't forward to motors.

**Solutions**:
1. Test ESP32 directly with `screen`:
   ```bash
   screen /dev/cu.usbmodem14201 115200
   # Type W, A, S, D, X manually
   ```
2. Check ESP32 firmware is correct version
3. Verify ESP-NOW pairing with Scout receiver
4. Check motor power supply to Scout
5. Test with Scout wheels lifted (less load)
6. Check ESP32 serial monitor for error messages

### Issue: Scout moves but doesn't stop

**Cause**: Stop command ('X') not reaching ESP32 or firmware issue.

**⚠️ IMMEDIATELY**:
1. Disconnect motor power
2. Unplug ESP32 USB cable
3. Implement hardware emergency stop button

**Debug**:
1. Check backend logs: Should show `Sending 'X'` after movement
2. Test stop command directly:
   ```bash
   screen /dev/cu.usbmodem14201 115200
   X  # Type X, motors should stop
   ```
3. **DO NOT proceed** until reliable stop is verified
4. Consider adding hardware watchdog timer to firmware

### Issue: WebSocket disconnects frequently

**Cause**: Network issues or backend restart.

**Solutions**:
1. Check browser console for WebSocket errors
2. Verify backend is running: `curl http://localhost:8000/health`
3. Check network firewall rules
4. Frontend auto-reconnects, but refresh page if needed

### Issue: Emergency stop doesn't work

**⚠️ CRITICAL SAFETY ISSUE**

**Immediate Actions**:
1. Cut motor power physically
2. Unplug ESP32
3. DO NOT continue testing

**Debug**:
1. Check emergency stop endpoint:
   ```bash
   curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'
   ```
2. Review backend logs: Should show "Emergency Stop latched"
3. Verify serial 'X' command sent
4. Test stop command isolation with `screen`

---

## Safety Checklist

Before ANY physical movement testing:

- [ ] Scout is in safe environment (clear area OR wheels lifted)
- [ ] Emergency stop is tested and working
- [ ] Motor power can be cut immediately (know where the switch is)
- [ ] USB-loss failsafe is verified OR you're holding the cable
- [ ] Someone is supervising the test
- [ ] You understand how to trigger emergency stop (curl command ready)
- [ ] Backend logs are visible (watching for errors)
- [ ] ESP32 is reliably receiving commands (tested with `screen`)
- [ ] You've read this entire guide

**If ANY checkbox is unchecked, DO NOT proceed with physical testing.**

---

## Quick Command Reference

```bash
# Check ESP32 port
ls /dev/cu.usbmodem*

# Check configuration
cat .env | grep -E "HARDWARE_LOCKOUT|VEHICLE_MODE|SERIAL"

# Start backend
cd backend && source .venv/bin/activate
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000

# Start frontend
cd frontend && npm run dev

# Check health
curl http://localhost:8000/health | python3 -m json.tool

# Check Scout control status
curl http://localhost:8000/scout/control/status | python3 -m json.tool

# Emergency stop
curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'

# Reset emergency stop
curl -X POST http://localhost:8000/emergency-stop/reset

# Test manual serial (disconnect with Ctrl+A, K, Y)
screen /dev/cu.usbmodem14201 115200

# View backend logs (if logging to file)
tail -f backend/logs/operation_shadow.log
```

---

## Success Criteria

A successful physical movement test means:

✅ Backend connects to ESP32 serial port (`transmitter_connected: true`)
✅ Frontend shows "REAL MODE" and "TRANSMITTER CONNECTED"
✅ Keyboard controls (W/A/S/D/X) cause physical Scout movement
✅ Scout stops immediately when key released (auto-sends X)
✅ Emergency stop button works and halts all movement
✅ Mission execution causes Scout to follow planned tasks
✅ Backend logs show clean serial communication (no errors)
✅ Scout returns to safe state on WebSocket disconnect

---

## Reverting to Simulation Mode

After testing, restore safe defaults:

```bash
# Edit .env
nano .env

# Change back to safe values:
SHADOW_HARDWARE_LOCKOUT=true
SHADOW_DEFAULT_VEHICLE_MODE=simulated
SHADOW_SCOUT_SERIAL_ENABLED=false
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false

# Restart backend
# (Kill with Ctrl+C and restart)
cd backend
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

Verify simulation mode restored:
```bash
curl http://localhost:8000/health | python3 -m json.tool | grep hardware_lockout
# Should show: "hardware_lockout": true
```

---

## Need Help?

If you encounter issues:

1. **Check backend logs** (most errors appear here)
2. **Test ESP32 in isolation** with `screen` command
3. **Verify all 4 safety gates** are properly configured
4. **Review this guide's Troubleshooting section**
5. **Check that USB cable and ESP32 are working** (try different port)

**For emergency stop issues**: STOP ALL TESTING and verify hardware safety mechanisms first.
