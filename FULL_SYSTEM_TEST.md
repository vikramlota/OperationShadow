# Full System Test - Copy & Paste Commands

## Quick Setup

Open 3 terminals and follow the exact sequence below.

---

## Terminal 1: Start Backend

Copy and paste this ENTIRE block:

```bash
#!/bin/bash
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new/backend"
source .venv/bin/activate
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

**Wait for:**
```
✓ Connected to SCOUT01 ESP32 serial on /dev/cu.usbserial-5C3D0844351
```

---

## Terminal 2: Run All Tests

Copy and paste each section one at a time:

### Test 2A: ESP32 Serial Test
```bash
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new"
python3 test_scout_simple.py
```
**Wait for:** "TEST COMPLETE - Scout executed all commands!"

### Test 2B: Health Check
```bash
echo "=== BACKEND HEALTH CHECK ==="
curl http://localhost:8000/health | python3 -m json.tool | head -15
```

**Expected:** `status: operational`, `hardware_lockout: false`

### Test 2C: Scout Status
```bash
echo "=== SCOUT CONTROL STATUS ==="
curl http://localhost:8000/scout/control/status | python3 -m json.tool
```

**Expected:** `transmitter_connected: true`, `mode: real`

### Test 2D: Movement Sequence
```bash
echo "=== BACKEND MOVEMENT TEST ==="
echo "Forward..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}' > /dev/null
sleep 1.5

echo "Left..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "left", "duration_s": 0.5}' > /dev/null
sleep 1.5

echo "Backward..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "backward", "duration_s": 0.5}' > /dev/null
sleep 1.5

echo "Right..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "right", "duration_s": 0.5}' > /dev/null
sleep 1.5

echo "Stop"
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "stop"}' > /dev/null

echo "✓ Movement test complete - Scout should have moved!"
```

### Test 2E: Find Scout's IP (For Camera)
```bash
echo "=== FINDING SCOUT ON NETWORK ==="
echo "Looking for Scout IP..."
arp-scan -l 2>/dev/null | grep -i scout

# If Scout found, note the IP
# If not found, check:
# 1. Scout is powered on
# 2. Scout is on SAME WiFi as laptop
# 3. Check your router's connected devices
```

### Test 2F: Test Camera (Replace SCOUT_IP with actual IP)
```bash
# Replace 10.68.44.41 with your Scout's actual IP
SCOUT_IP="10.68.44.41"
echo "=== CAMERA TEST ==="
echo "Testing camera at $SCOUT_IP:8081"
curl -I http://$SCOUT_IP:8081/stream.mjpg

# Expected: HTTP/1.1 200 OK
# If error: Scout not accessible, check IP and WiFi
```

---

## Terminal 3: Start Frontend

```bash
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new/frontend"
npm run dev
```

**Wait for:**
```
VITE v4.0.0 ready in XXX ms
Local: http://localhost:5173/
```

---

## Browser: Frontend Tests

### Test 3A: Open Frontend
```
http://localhost:5173
```

**Should see:**
- Scout Control panel on the left
- Mission panel in center
- Camera feed (if Scout on WiFi)

### Test 3B: Check Connection Status
Look for text that says:
```
✓ REAL MODE
✓ TRANSMITTER CONNECTED
```

If shows "DISCONNECTED":
- Hard refresh: Cmd+Shift+R
- Check Terminal 1 for "Connected to ESP32"

### Test 3C: Keyboard Control Test
1. Click **Scout Control panel** to focus
2. Press and hold **W** key
3. Scout should move **immediately** (not 2-3 second delay)
4. Release **W** key
5. Scout should stop **immediately**

**Success:** No delay, instant response

### Test 3D: Test All Keys
Press each key and verify movement:
- **W** → Forward ✓
- **A** → Left ✓
- **S** → Backward ✓
- **D** → Right ✓
- **X** → Stop ✓

### Test 3E: Camera Feed
Look for **Camera Feed** panel:
- Should show live video from Scout
- Video should update in real-time (not frozen)
- Scout should be visible in frame

### Test 3F: Mission Test
1. Find **Mission Command & Tasking** panel
2. In text area, type:
   ```
   Move forward 1 meter, rotate 180 degrees, then return to start
   ```
3. Click **Create Mission**
4. Wait 2-5 seconds for ULTRON to plan
5. Should see:
   ```
   Mission ID: M001
   Tasks: 3 (or more)
   ```
6. Click **Execute Mission**
7. Confirm: "Yes"
8. **Watch Scout move!**
   - Should move forward ~1 second
   - Turn ~2 seconds
   - Move backward ~1 second
   - Stop

### Test 3G: Emergency Stop
While Scout is idle or moving:
1. Find **Emergency Stop** button (red button)
2. Click it
3. Confirm dialog
4. Scout should stop
5. Dashboard should show: "EMERGENCY STOP ACTIVE"
6. All controls should be disabled
7. Click **Reset Emergency Stop**
8. Should return to normal

---

## Verification Summary

### Copy this to check everything:

```bash
echo "================================"
echo "FULL SYSTEM VERIFICATION"
echo "================================"

echo ""
echo "1. ESP32 Serial:"
ls -la /dev/cu.usbserial-5C3D0844351 2>/dev/null && echo "✓ Connected" || echo "✗ Not found"

echo ""
echo "2. Backend Health:"
curl -s http://localhost:8000/health | python3 -c "import sys, json; d=json.load(sys.stdin); print('✓ Operational' if d.get('status')=='operational' else '✗ Failed')"

echo ""
echo "3. Scout Connection:"
curl -s http://localhost:8000/scout/control/status | python3 -c "import sys, json; d=json.load(sys.stdin); print('✓ Connected' if d.get('transmitter_connected') else '✗ Disconnected')"

echo ""
echo "4. Frontend:"
curl -s http://localhost:5173 > /dev/null && echo "✓ Running on port 5173" || echo "✗ Not running"

echo ""
echo "5. Scout IP (for camera):"
arp-scan -l 2>/dev/null | grep -i scout | head -1 | awk '{print "✓ Found: " $1}' || echo "✗ Not on network"

echo ""
echo "================================"
echo "Check all 5 above for full pass"
echo "================================"
```

---

## Expected Results

### Level 1: ESP32 ✅
- Serial test completes
- Scout executes W/A/S/D/X
- No errors

### Level 2: Backend ✅
- Health shows operational
- transmitter_connected: true
- Movement commands execute
- Scout moves via API

### Level 3: Camera ✅
- Scout found on network
- Camera port 8081 responds
- MJPEG stream accessible

### Level 4: Frontend ✅
- Frontend loads <2 seconds
- Shows "CONNECTED"
- Keyboard controls instant (<100ms)
- Camera visible in feed
- Mission creates and executes
- Scout follows mission
- Emergency stop works

---

## Troubleshooting Quick Links

**ESP32 not found:**
- Check USB cable
- Try: `ls /dev/cu.usb*`

**Backend won't connect:**
- Kill with Ctrl+C
- Restart backend
- Check Terminal 1 logs

**Scout disconnected:**
- Hard refresh browser: Cmd+Shift+R
- Check ESP32 connected to laptop
- Restart backend

**Camera not visible:**
- Find Scout IP: `arp-scan -l`
- Check Scout on same WiFi
- Update Scout IP in `.env` if changed

**Keyboard controls slow:**
- Hard refresh: Cmd+Shift+R
- Restart backend
- Check .env: ROS_ENABLED=false

---

## Final Success Indicator

If you see this in browser:
```
✓ REAL MODE
✓ TRANSMITTER CONNECTED
✓ Camera feed live
✓ Keyboard responds instantly
✓ Mission executes smoothly
```

**THEN: FULL SYSTEM WORKING!** 🎉

---

## What to Report After Testing

Please tell me:
1. ✓ All 4 levels passed or which failed?
2. ✓ Scout connects on first try?
3. ✓ Camera visible in frontend?
4. ✓ Keyboard response time (should be instant)?
5. ✓ Mission executed successfully?
6. ✓ Any errors in Terminal 1?

