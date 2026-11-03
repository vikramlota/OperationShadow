# Troubleshooting Flowchart for Physical Testing

Quick decision tree to diagnose and fix common issues.

---

## Problem: Backend Won't Start

```
Backend fails to start or crashes immediately
│
├─ Check: Can you see "Uvicorn running on..." message?
│  │
│  ├─ NO → Python/dependency issue
│  │  └─ Solution:
│  │     1. cd backend && source .venv/bin/activate
│  │     2. pip install -r requirements.txt
│  │     3. Check Python version: python3 --version (need 3.10+)
│  │
│  └─ YES → Backend started but...
│
└─ Check: Backend logs show errors?
   │
   ├─ "Could not open serial port" → ESP32 connection issue (see below)
   │
   ├─ "Database error" → SQLite issue
   │  └─ Solution:
   │     1. PYTHONPATH=fastapi:.:.. python3 -c "import asyncio; from db.session import init_db; asyncio.run(init_db())"
   │     2. Restart backend
   │
   └─ Other errors → Check backend logs for details
```

---

## Problem: "transmitter_connected: false"

```
curl http://localhost:8000/scout/control/status shows transmitter_connected: false
│
├─ Step 1: Is ESP32 physically connected?
│  │
│  ├─ NO → Plug in ESP32 via USB
│  │  └─ Check cable is data cable (not charge-only)
│  │
│  └─ YES → Continue to Step 2
│
├─ Step 2: Can system see ESP32?
│  │
│  ├─ Run: ls /dev/cu.usbmodem* /dev/ttyUSB* /dev/ttyACM*
│  │
│  ├─ NO OUTPUT → Driver issue
│  │  └─ Solution:
│  │     - macOS: Should work automatically
│  │     - Try different USB port
│  │     - Check USB-C adapter (if using)
│  │     - Verify ESP32 has power (LED on?)
│  │
│  └─ SHOWS PORT (e.g., /dev/cu.usbmodem14201) → Continue to Step 3
│
├─ Step 3: Does .env have correct port?
│  │
│  ├─ Run: cat .env | grep SERIAL_PORT
│  │
│  ├─ DIFFERENT PORT or MISSING → Fix .env
│  │  └─ Solution:
│  │     1. nano .env
│  │     2. Set: SHADOW_SCOUT_SERIAL_PORT=/dev/cu.usbmodem14201 (use YOUR port)
│  │     3. Save and restart backend
│  │
│  └─ MATCHES → Continue to Step 4
│
├─ Step 4: Does ESP32 actually work?
│  │
│  ├─ Run: screen /dev/cu.usbmodem14201 115200
│  │  └─ Type: W (Scout should move)
│  │  └─ Type: X (Scout should stop)
│  │  └─ Exit: Ctrl+A, K, Y
│  │
│  ├─ DOESN'T WORK → ESP32 firmware issue
│  │  └─ Solution:
│  │     - Verify ESP32 running correct firmware
│  │     - Check baudrate is 115200
│  │     - Flash firmware again
│  │     - Test with Arduino Serial Monitor
│  │
│  └─ WORKS → Continue to Step 5
│
└─ Step 5: Are safety gates configured?
   │
   ├─ Run: cat .env | grep -E "HARDWARE_LOCKOUT|SERIAL_ENABLED"
   │
   ├─ HARDWARE_LOCKOUT=true OR SERIAL_ENABLED=false
   │  └─ Solution:
   │     1. Edit .env
   │     2. Set both to correct test values
   │     3. Restart backend
   │
   └─ ALL CORRECT → Check backend logs
      └─ Look for serial connection errors
      └─ May need port permissions (unlikely on macOS)
```

---

## Problem: "Serial writes remain disabled"

```
Backend logs show "Serial writes remain disabled by configuration"
│
├─ This means one or more safety gates are active
│
├─ Check all 4 gates:
│  │
│  ├─ Run: cat .env | grep -E "HARDWARE_LOCKOUT|VEHICLE_MODE|SERIAL_ENABLED|FAILSAFE"
│  │
│  └─ Required for physical testing:
│     - SHADOW_HARDWARE_LOCKOUT=false
│     - SHADOW_DEFAULT_VEHICLE_MODE=real
│     - SHADOW_SCOUT_SERIAL_ENABLED=true
│     - SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true
│
├─ IMPORTANT: Check for duplicate .env files!
│  │
│  ├─ Run: ls -la .env backend/.env 2>/dev/null
│  │
│  ├─ If backend/.env exists → It overrides root .env
│  │  └─ Solution: Edit backend/.env instead
│  │
│  └─ Only root .env exists → Edit that one
│
└─ After fixing .env:
   1. Export variables: export $(cat .env | grep -v '^#' | xargs)
   2. Restart backend
   3. Verify: curl http://localhost:8000/health | grep hardware_lockout
```

---

## Problem: Commands Sent But Scout Doesn't Move

```
Backend sends commands successfully, but Scout doesn't move
│
├─ Step 1: Does ESP32 receive commands?
│  │
│  ├─ Test: screen /dev/cu.usbmodem14201 115200
│  │  └─ Type W, A, S, D manually
│  │
│  ├─ SCOUT MOVES → Backend serial write issue
│  │  └─ Check backend logs for serial errors
│  │  └─ Verify curl commands use correct endpoints
│  │
│  └─ SCOUT DOESN'T MOVE → Continue to Step 2
│
├─ Step 2: ESP-NOW communication issue
│  │
│  └─ Solution:
│     - Check ESP-NOW pairing between transmitter and Scout receiver
│     - Verify both ESP32s have correct MAC addresses
│     - Check transmitter is sending RF signals (check firmware)
│     - Test Scout receiver independently
│
├─ Step 3: Motor power issue
│  │
│  └─ Check:
│     - Is Scout powered on?
│     - Are batteries charged?
│     - Is motor power switch ON?
│     - Are motor connections secure?
│     - Test motors directly (bypass ESP32)
│
└─ Step 4: Load/mechanical issue
   │
   └─ Solution:
      - Lift wheels off ground (reduce load)
      - Check for mechanical binding
      - Verify motors are not damaged
      - Test each motor individually
```

---

## Problem: Scout Won't Stop

```
⚠️ CRITICAL SAFETY ISSUE ⚠️
Scout moves but doesn't stop when commanded
│
├─ IMMEDIATELY:
│  1. Cut motor power physically
│  2. Unplug USB cable
│  3. DO NOT continue testing
│
├─ Diagnosis:
│  │
│  ├─ Test stop in isolation:
│  │  1. screen /dev/cu.usbmodem14201 115200
│  │  2. Type: W (start moving)
│  │  3. Type: X (should stop)
│  │
│  ├─ DOESN'T STOP in screen → Firmware problem
│  │  └─ Solution:
│  │     - Fix ESP32 firmware stop command handling
│  │     - Verify 'X' command stops motors
│  │     - Add hardware watchdog timer
│  │     - DO NOT proceed until fixed
│  │
│  └─ STOPS in screen → Backend command issue
│     └─ Solution:
│        - Check backend always sends 'X' after movement
│        - Verify execute_step() includes stop
│        - Check emergency stop functionality
│        - Review ScoutSerialWriter logs
│
└─ Prevention:
   - Implement USB-loss failsafe in firmware
   - Add hardware emergency stop button
   - Test stop reliability thoroughly
   - Never skip safety verification
```

---

## Problem: Frontend Controls Don't Work

```
Browser keyboard controls (W/A/S/D/X) don't move Scout
│
├─ Step 1: Is WebSocket connected?
│  │
│  ├─ Check browser console (F12)
│  │
│  ├─ Shows "WebSocket disconnected" → Connection issue
│  │  └─ Solution:
│  │     1. Verify backend is running: curl http://localhost:8000/health
│  │     2. Check firewall not blocking WebSocket
│  │     3. Refresh browser page
│  │     4. Check backend WebSocket logs
│  │
│  └─ Shows "WebSocket connected" → Continue to Step 2
│
├─ Step 2: Is Scout in correct mode?
│  │
│  ├─ Check Scout Control panel status display
│  │
│  ├─ Shows "SIMULATION ONLY" → Wrong mode
│  │  └─ Solution:
│  │     1. Check .env: SHADOW_DEFAULT_VEHICLE_MODE=real
│  │     2. Restart backend
│  │     3. Refresh browser
│  │
│  └─ Shows "REAL MODE" → Continue to Step 3
│
├─ Step 3: Is transmitter connected?
│  │
│  ├─ Check status display
│  │
│  ├─ Shows "OFFLINE" or "transmitter_connected: false"
│  │  └─ See "transmitter_connected: false" section above
│  │
│  └─ Shows "CONNECTED" → Continue to Step 4
│
└─ Step 4: Are controls focused?
   │
   ├─ You must CLICK inside Scout Control panel first
   │  └─ Solution:
   │     1. Click anywhere in Scout Control panel
   │     2. Panel should show focus border
   │     3. Then press W/A/S/D/X keys
   │
   └─ Still not working?
      └─ Check browser console for JavaScript errors
      └─ Try curl commands to verify backend works
      └─ Test with test_esp32_physical.py script
```

---

## Problem: Mission Execution Fails

```
Mission created successfully but execution fails
│
├─ Step 1: Check mission status
│  │
│  ├─ Run: curl http://localhost:8000/mission/{MISSION_ID}
│  │
│  └─ Look at "status" and "tasks" fields
│
├─ Step 2: Check hardware readiness
│  │
│  ├─ Run: curl http://localhost:8000/scout/control/status
│  │
│  ├─ transmitter_connected: false → See transmitter troubleshooting above
│  │
│  └─ transmitter_connected: true → Continue to Step 3
│
├─ Step 3: Check for emergency stop
│  │
│  ├─ Run: curl http://localhost:8000/health
│  │
│  ├─ Shows emergency_stop_active: true → Reset required
│  │  └─ Solution:
│  │     curl -X POST http://localhost:8000/emergency-stop/reset
│  │
│  └─ No emergency stop → Continue to Step 4
│
├─ Step 4: Check backend logs
│  │
│  └─ Look for errors during mission execution:
│     - Serial write errors → Check USB connection
│     - Task validation errors → Mission plan issue
│     - Timeout errors → Commands taking too long
│
└─ Step 5: Test with simpler mission
   │
   └─ Try:
      curl -X POST http://localhost:8000/mission \
      -d '{"objective": "Move forward 0.5 meters and stop"}'
   │
   └─ If simple mission works → Original mission too complex
   └─ If simple mission fails → Hardware issue (go back to basic tests)
```

---

## Problem: Emergency Stop Doesn't Work

```
⚠️ CRITICAL SAFETY FAILURE ⚠️
Emergency stop command doesn't halt Scout
│
├─ IMMEDIATELY:
│  1. Cut motor power at source
│  2. Unplug ESP32
│  3. STOP ALL TESTING
│
├─ Test emergency stop in isolation:
│  │
│  ├─ Test 1: API emergency stop
│  │  curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'
│  │  └─ Check response and backend logs
│  │
│  ├─ Test 2: Frontend button
│  │  Click emergency stop button in dashboard
│  │  └─ Check browser console and backend logs
│  │
│  └─ Test 3: Serial stop command
│     screen /dev/cu.usbmodem14201 115200
│     Type: X (must stop motors immediately)
│
├─ Diagnosis:
│  │
│  ├─ Serial 'X' doesn't stop → CRITICAL firmware bug
│  │  └─ Fix firmware first, DO NOT proceed
│  │
│  ├─ API call fails → Backend emergency stop broken
│  │  └─ Check ScoutSerialWriter emergency stop methods
│  │  └─ Review latch_global_emergency_stop() function
│  │
│  └─ Delayed response → Timing issue
│     └─ Emergency stop must be immediate
│     └─ Review async handling in ScoutSerialWriter
│
└─ DO NOT RESUME TESTING until:
   ✓ Emergency stop works reliably every time
   ✓ Stop happens within 100ms of command
   ✓ Hardware emergency stop also implemented
   ✓ USB-loss causes immediate motor stop
```

---

## Quick Diagnostic Commands

Run these in order when troubleshooting:

```bash
# 1. Check ESP32 port exists
ls -la /dev/cu.usbmodem* /dev/ttyUSB* /dev/ttyACM* 2>/dev/null

# 2. Check configuration
cat .env | grep -E "HARDWARE_LOCKOUT|VEHICLE_MODE|SERIAL_ENABLED|SERIAL_PORT|FAILSAFE"

# 3. Check backend is running
curl -s http://localhost:8000/health | python3 -m json.tool

# 4. Check Scout control status
curl -s http://localhost:8000/scout/control/status | python3 -m json.tool

# 5. Check vehicle status
curl -s http://localhost:8000/vehicle/SCOUT01/status | python3 -m json.tool

# 6. Test ESP32 directly
screen /dev/cu.usbmodem14201 115200
# Type W, X, A, D, S
# Exit: Ctrl+A, K, Y

# 7. Check backend logs (if logging to file)
tail -50 backend/logs/*.log

# 8. Run automated diagnostic
python3 test_esp32_physical.py
# Select option 1: Run Diagnostic
```

---

## When to Escalate

Stop troubleshooting and seek help if:

❌ Emergency stop fails to work reliably
❌ Scout moves but cannot be stopped
❌ USB disconnection doesn't stop motors (no failsafe)
❌ Firmware appears to have critical bugs
❌ Physical damage to Scout or ESP32
❌ Safety systems are bypassed or compromised

**Never compromise on safety to "just make it work"**

---

## Getting More Help

1. **Review full documentation**:
   - `ESP32_PHYSICAL_MOVEMENT_TEST.md` - Comprehensive guide
   - `QUICK_START_PHYSICAL_TEST.md` - Quick reference
   - `TESTING_SUMMARY.md` - Overview

2. **Check backend logs**:
   - Look in terminal where backend is running
   - Search for ERROR or WARNING messages
   - Check serial-related log messages

3. **Test components in isolation**:
   - ESP32 with `screen`
   - Backend with `curl`
   - Frontend in browser console

4. **Use diagnostic tools**:
   - `python3 test_esp32_physical.py` (automated tests)
   - Browser DevTools (F12) for frontend issues
   - Backend health endpoint for system status

---

## Prevention Tips

Avoid issues before they happen:

✅ Always test ESP32 with `screen` before backend testing
✅ Verify all 4 safety gates before each session
✅ Check backend logs for warnings on startup
✅ Test emergency stop at beginning of each session
✅ Keep USB cable secure during testing
✅ Have emergency stop command ready in terminal
✅ Never test alone - always have a supervisor
✅ Start with wheels lifted for initial tests
✅ Document any issues encountered for next time
