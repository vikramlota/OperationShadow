# Quick Test Commands - Copy & Paste

## Terminal Setup (Do This First)

### Terminal 1: Start Backend
```bash
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new/backend"
source .venv/bin/activate
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000
```

**Wait for:** `Connected to SCOUT01 ESP32 serial on /dev/cu.usbserial-5C3D0844351`

### Terminal 2: Start Frontend
```bash
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new/frontend"
npm run dev
```

**Wait for:** `Local: http://localhost:5173/`

### Terminal 3: Test Commands (Copy-Paste Below)

---

## LEVEL 2: Backend API Tests

### Test 1: Health Check
```bash
curl http://localhost:8000/health | python3 -m json.tool
```

Expected: `hardware_lockout: false`, `vehicle_modes.SCOUT01: real`

### Test 2: Scout Status
```bash
curl http://localhost:8000/scout/control/status | python3 -m json.tool
```

Expected: `transmitter_connected: true`, `mode: real`

### Test 3: Forward Movement
```bash
echo "Sending FORWARD command..."
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}'
echo -e "\nWait 1 second..."
sleep 1
```

**Watch Scout**: Should move forward for 0.5 seconds, then stop

### Test 4: Complete Sequence (Forward, Left, Backward, Right, Stop)
```bash
echo "=== TESTING SCOUT MOVEMENT SEQUENCE ==="

echo -e "\n1. Forward"
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}'
sleep 1.5

echo -e "\n2. Left"
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "left", "duration_s": 0.5}'
sleep 1.5

echo -e "\n3. Backward"
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "backward", "duration_s": 0.5}'
sleep 1.5

echo -e "\n4. Right"
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "right", "duration_s": 0.5}'
sleep 1.5

echo -e "\n5. Stop"
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "stop"}'

echo -e "\n\n=== BACKEND TEST COMPLETE ==="
```

---

## LEVEL 3: Frontend + Mission Tests

### Open Browser
```
http://localhost:5173
```

### Create Mission (via curl)
```bash
echo "Creating mission..."
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{
    "objective": "Move forward 1 meter, rotate 180 degrees, then return to start position"
  }' | python3 -m json.tool
```

**Note the mission_id** from output (e.g., M001)

### Check Mission Details
```bash
# Replace M001 with your mission ID
curl http://localhost:8000/mission/M001 | python3 -m json.tool
```

Should show tasks planned by ULTRON

### Execute Mission (via curl)
```bash
# Replace M001 with your mission ID
echo "Executing mission..."
curl -X POST http://localhost:8000/mission/M001/execute

echo -e "\nWatch Scout move! Mission should take ~5 seconds"
sleep 6

echo -e "\nChecking mission status..."
curl http://localhost:8000/mission/M001 | python3 -m json.tool
```

### Check Events
```bash
# Replace M001 with your mission ID
curl http://localhost:8000/events?mission_id=M001 | python3 -m json.tool
```

---

## LEVEL 3: Frontend Mission (In Browser)

### Step 1: Open Frontend
```
http://localhost:5173
```

### Step 2: Find Mission Panel
Look for: **"MISSION COMMAND & TASKING"**

### Step 3: Enter Mission
In the text area, type:
```
Move forward 1 meter and return to start
```

### Step 4: Create Mission
Click: **"CREATE MISSION"** button

### Step 5: Wait for ULTRON
Wait 2-5 seconds for ULTRON to plan tasks

Should see:
```
Mission ID: M001
Tasks: 3
- move_forward
- rotate_cw
- move_backward
```

### Step 6: Execute Mission
Click: **"EXECUTE MISSION"** button

Confirm: **"Yes"**

### Step 7: Watch Scout
Scout should move for ~5 seconds

### Step 8: Check Results
Panel should show: **"MISSION COMPLETED"**

View report: Click **"VIEW REPORT"**

---

## All-In-One Test Command

Copy this entire block and paste into Terminal 3:

```bash
#!/bin/bash

echo "INTEGRATION TEST: BACKEND + FRONTEND + MISSION"
echo "=============================================="
echo ""

# Test 1: Health
echo "Test 1: Backend Health"
curl -s http://localhost:8000/health | python3 -m json.tool | head -10
echo ""

# Test 2: Scout Status
echo "Test 2: Scout Control Status"
curl -s http://localhost:8000/scout/control/status | python3 -m json.tool
echo ""

# Test 3: Movement Sequence
echo "Test 3: Movement Sequence"
echo "  Forward..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}' > /dev/null
sleep 1

echo "  Left..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "left", "duration_s": 0.5}' > /dev/null
sleep 1

echo "  Backward..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "backward", "duration_s": 0.5}' > /dev/null
sleep 1

echo "  Right..."
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "right", "duration_s": 0.5}' > /dev/null
sleep 1

echo "  Stop"
curl -s -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "stop"}' > /dev/null

echo "✓ Movement test complete"
echo ""

# Test 4: Create Mission
echo "Test 4: Create Mission via ULTRON"
MISSION_RESPONSE=$(curl -s -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective": "Move forward and return"}')

MISSION_ID=$(echo $MISSION_RESPONSE | python3 -c "import sys, json; print(json.load(sys.stdin).get('mission_id', 'ERROR'))")

echo "Mission Created: $MISSION_ID"
echo ""

# Test 5: Get Mission Details
echo "Test 5: Mission Tasks"
curl -s http://localhost:8000/mission/$MISSION_ID | python3 -m json.tool | head -20
echo ""

# Test 6: Execute Mission
echo "Test 6: Execute Mission (this will move Scout)"
curl -s -X POST http://localhost:8000/mission/$MISSION_ID/execute > /dev/null

echo "Mission executing... wait 7 seconds"
sleep 7

echo ""
echo "Test 7: Mission Status"
curl -s http://localhost:8000/mission/$MISSION_ID | python3 -m json.tool | grep -E "mission_id|status|tasks" | head -5

echo ""
echo "=============================================="
echo "INTEGRATION TEST COMPLETE!"
echo "If you see all tests above without errors:"
echo "  ✓ Backend working"
echo "  ✓ Serial working"
echo "  ✓ Mission execution working"
echo "=============================================="
```

---

## Troubleshooting Quick Reference

### Backend won't connect to ESP32
```bash
# Check port
ls /dev/cu.usb*

# Check config
cat .env | grep SERIAL_PORT

# Restart backend
# (Kill with Ctrl+C, run again)
```

### transmitter_connected: false
```bash
# Check if ESP32 connected
ls -la /dev/cu.usbserial-5C3D0844351

# Check backend logs for:
# "Could not open serial port"

# Try different port (edit .env, restart)
```

### Mission executes but Scout doesn't move
```bash
# Test direct serial first
python3 test_scout_simple.py

# Check backend logs for:
# "✓ Sent 'W'"

# If logs show sends but Scout doesn't move:
# - Check ESP-NOW pairing
# - Check motor power
# - Test motors directly
```

### Frontend not loading
```bash
# Check frontend running on port 5173
# Is it visible in Terminal 2?

# Try refreshing browser
# Hard refresh: Cmd+Shift+R

# Check firewall
# Try: http://localhost:5173
```

---

## Success Criteria

✓ **Backend Level**
- Health check returns operational
- transmitter_connected: true
- Movement commands execute (W/A/S/D/X)

✓ **Mission Level**
- Mission creates successfully
- ULTRON plans tasks
- Mission executes on Scout
- Scout moves as planned

✓ **Frontend Level**
- Frontend loads
- Mission panel visible
- Can create missions
- Dashboard shows progress
- Can view mission results

---

## Next Steps After Success

1. Test different mission objectives
2. Test emergency stop (click button while moving)
3. Test keyboard controls (W/A/S/D/X in browser)
4. Test multiple missions in sequence
5. Document any issues
6. Ready for production testing!

