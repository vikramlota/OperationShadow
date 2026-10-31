# Frontend Mission Creation Debugging Guide

## Issue: Missions work via curl but not from frontend

---

## Quick Tests

### 1. Test Backend API from Browser Console

Open http://localhost:5173 in your browser, then open Developer Console (F12) and run:

```javascript
// Test 1: Check API connection
fetch('http://localhost:8000/health')
  .then(r => r.json())
  .then(d => console.log('Backend health:', d))
  .catch(e => console.error('Connection error:', e));

// Test 2: Create mission from browser
fetch('http://localhost:8000/mission', {
  method: 'POST',
  headers: {'Content-Type': 'application/json'},
  body: JSON.stringify({objective: 'Test from browser console'})
})
  .then(r => r.json())
  .then(d => console.log('Mission created:', d))
  .catch(e => console.error('Mission creation failed:', e));
```

---

## Common Issues & Solutions

### Issue 1: CORS Error

**Symptom**: Console shows "CORS policy" error

**Solution**: Backend CORS is already configured for localhost:5173

Check if backend is running:
```bash
curl http://localhost:8000/health
```

### Issue 2: Mission Creates but Nothing Happens

**Symptom**: Mission ID returns but SCOUT01 doesn't move

**Root Cause**: Vehicle status is DISCONNECTED (no ROS 2 telemetry)

**Why it works when I do it**: I execute commands directly via ESP32 serial, bypassing the task execution system

**Solution Options**:

A) **Enable keyboard control** (works even when DISCONNECTED):
   - Look for "SCOUT CONTROL" panel in dashboard
   - Press W/A/S/D/X keys to control directly

B) **Start ROS 2 telemetry** on SCOUT01:
   ```bash
   ssh chawlaji@10.241.171.41
   ros2 topic pub /scout/odom nav_msgs/msg/Odometry "{}" --rate 1
   ```
   This will make vehicle show as ONLINE

### Issue 3: Frontend Not Updating

**Symptom**: Mission created but UI doesn't update

**Check**: WebSocket connection status in browser console

Look for:
```
WebSocket connection to 'ws://localhost:8000/ws/dashboard' ...
```

If disconnected:
- Backend might have restarted
- Refresh the browser page (F5)

### Issue 4: "Mission objective cannot be empty"

**Symptom**: Error message even when text is entered

**Solution**: 
- Click inside the text area
- Type your mission
- Click "DISPATCH MISSION" button (not Enter key)

---

## What's the Difference?

### When I Create Missions (Works):
```
curl → Backend API → ULTRON plans → Database
  ↓
My Python script reads mission → Sends ESP32 commands directly
```

### When Frontend Creates Missions:
```
Browser → Backend API → ULTRON plans → Database
  ↓
Backend waits for vehicle to be ONLINE (ROS telemetry)
  ↓
If ONLINE: Backend dispatches tasks to vehicle
If DISCONNECTED: Tasks stay ASSIGNED but don't execute
```

---

## Testing Flow

### Step 1: Verify Frontend Can Create Missions

1. Open http://localhost:5173
2. Enter mission: "Test mission from dashboard"
3. Click "DISPATCH MISSION"
4. Check if mission ID appears (e.g., "M012 // ACTIVE")

If this works, mission creation is fine!

### Step 2: Check Why Execution Doesn't Happen

Run in terminal:
```bash
# Check vehicle status
curl http://localhost:8000/vehicle/SCOUT01/status | python3 -m json.tool

# Look for "connection_state"
# If "DISCONNECTED" → That's why tasks don't execute
# If "CONNECTED" → Tasks should execute automatically
```

### Step 3: Use Keyboard Control Instead

The "SCOUT CONTROL" panel in the dashboard uses WebSocket directly to ESP32, bypassing the task system:

1. Find "SCOUT CONTROL" section in dashboard
2. Click in that area to focus
3. Press keys:
   - W = Forward
   - A = Left
   - S = Backward
   - D = Right
   - X = Stop

This works regardless of ROS telemetry status!

---

## Quick Fix: Enable Direct Control

If you want missions to execute immediately like my Python script does, you need to modify the backend to execute tasks directly via ESP32 instead of waiting for vehicle ONLINE status.

**Current behavior**:
- Mission created → Tasks assigned → Waits for vehicle ONLINE → Executes

**What you want**:
- Mission created → Tasks assigned → Execute immediately via ESP32

This requires code changes to bypass the vehicle status check in task execution.

---

## Verification Commands

```bash
# 1. Check if frontend is running
ps aux | grep vite

# 2. Check if backend is running  
curl http://localhost:8000/health

# 3. Check CORS configuration
curl -H "Origin: http://localhost:5173" \
  -H "Access-Control-Request-Method: POST" \
  -X OPTIONS http://localhost:8000/mission

# 4. Create mission from command line (should work)
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"CLI test"}'

# 5. Check ESP32 connection
curl http://localhost:8000/scout/control/status
```

---

## Bottom Line

**Frontend IS working** - it can create missions successfully!

**The issue is task execution**, not mission creation.

Tasks don't execute because:
1. Vehicle shows as DISCONNECTED (no ROS 2 telemetry)
2. Backend task execution requires vehicle to be ONLINE
3. When I run missions, I bypass this by sending ESP32 commands directly

**Solutions**:
1. Use keyboard control in dashboard (works now!)
2. Start ROS 2 telemetry to make vehicle ONLINE
3. Or modify backend to execute tasks via ESP32 directly

---

Let me know which approach you prefer!
