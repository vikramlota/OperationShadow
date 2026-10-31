# Operation Shadow - System Test Results

**Test Date**: $(date)
**Status**: ✅ **OPERATIONAL** (with notes)

---

## Test Results Summary

| Component | Status | Details |
|-----------|--------|---------|
| Backend API | ✅ PASS | Running on port 8000 |
| ULTRON Agent | ✅ PASS | Real LangGraph agent active (not stub) |
| Hardware Control | ✅ PASS | Enabled (real mode, lockout disabled) |
| SCOUT01 Network | ✅ PASS | Reachable at 10.241.171.41 (~18ms latency) |
| Camera Stream | ✅ PASS | MJPEG streaming on port 8081 |
| ROSBridge | ✅ PASS | WebSocket port 9090 open |
| SSH Access | ⚠️ PARTIAL | Port open, credentials: chawlaji@10.241.171.41 |
| Mission Planning | ✅ PASS | ULTRON successfully creates missions |
| API Documentation | ✅ PASS | Available at http://localhost:8000/docs |
| Vehicle Commands | ⚠️ PARTIAL | Commands accepted but vehicle DISCONNECTED |

---

## ✅ What's Working

### 1. Backend & ULTRON
- FastAPI backend running successfully
- ULTRON LangGraph agent integrated and functional
- Mission creation working: `POST /mission`
- API documentation accessible

**Example Mission Creation**:
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Navigate to waypoint alpha and scan for obstacles"}'
# Returns: {"mission_id":"M001"}
```

### 2. Network Connectivity
- SCOUT01 reachable at 10.241.171.41
- Average latency: ~18ms
- All required ports accessible:
  - 8081: Camera stream ✅
  - 9090: ROSBridge ✅
  - 22: SSH ✅

### 3. Camera Stream
- URL: http://10.241.171.41:8081/stream.mjpg
- Format: MJPEG (multipart/x-mixed-replace)
- Status: Actively streaming
- Can be viewed directly in browser or dashboard

### 4. Hardware Configuration
- Hardware lockout: **DISABLED**
- Vehicle mode: **real**
- Commands will be sent to physical SCOUT01
- Serial communication enabled (backup channel)

---

## ⚠️ Issues & Resolutions

### Issue 1: Vehicle Status Shows DISCONNECTED

**Problem**: SCOUT01 shows `connection_state: DISCONNECTED` in backend

**Root Cause**: Backend hasn't received telemetry from SCOUT01 yet

**Resolution**:
1. Ensure ROS 2 nodes are running on SCOUT01
2. Check that topics are being published:
   ```bash
   ssh chawlaji@10.241.171.41
   ros2 topic list
   ros2 topic echo /scout/odom --once
   ```
3. Verify ROSBridge is subscribed to correct topics
4. Once telemetry arrives, vehicle will transition to ONLINE

**Workaround**: System is configured for simulated mode fallback

---

### Issue 2: SSH Requires Password

**Problem**: Passwordless SSH not fully configured

**Current Status**: SSH port 22 open, username `chawlaji` confirmed

**Resolution** (if passwordless access needed):
1. Copy SSH public key to SCOUT01:
   ```bash
   ssh-copy-id chawlaji@10.241.171.41
   ```
2. Or manually add key to `~/.ssh/authorized_keys` on SCOUT01

**Impact**: Low - SSH mainly for debugging, not required for operation

---

## 📋 Command Reference

### Mission Creation (ULTRON Planning)
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Your mission description here","language":"English"}'
```

### Send Movement Command
```bash
SECRET="provisional_secret_change_me"

curl -X POST "http://localhost:8000/vehicle/SCOUT01/command?token=$SECRET" \
  -H "Content-Type: application/json" \
  -d '{
    "command_type":"MOVE_FORWARD",
    "parameters":{
      "speed":0.3,
      "duration_ms":2000
    }
  }'
```

**Available command_type values** (check API docs for full list):
- `MOVE_FORWARD`
- `MOVE_BACKWARD`  
- `TURN_LEFT`
- `TURN_RIGHT`
- `STOP`
- `HOLD_POSITION`

### Check Vehicle Status
```bash
curl http://localhost:8000/vehicle/SCOUT01/status
```

### View Camera Stream
```bash
# In browser:
http://10.241.171.41:8081/stream.mjpg

# Or test with curl:
curl http://10.241.171.41:8081/stream.mjpg | head -c 1000
```

---

## 🚀 Next Steps

### 1. Start SCOUT01 Telemetry Publishing

**On SCOUT01** (via SSH):
```bash
ssh chawlaji@10.241.171.41

# Check what ROS 2 nodes are running
ros2 node list

# Check available topics
ros2 topic list

# Verify telemetry is being published
ros2 topic echo /scout/odom --once
ros2 topic echo /scout/battery --once

# If not running, start required nodes
# (Depends on SCOUT01's launch configuration)
```

### 2. Start Frontend Dashboard

```bash
cd "/home/vikram/Projects/Project CU/frontend"
npm run dev
```

Then open: http://localhost:5173

### 3. Test Movement Commands

Once SCOUT01 is ONLINE (sending telemetry), try:

```bash
SECRET="provisional_secret_change_me"

# Move forward for 1 second
curl -X POST "http://localhost:8000/vehicle/SCOUT01/command?token=$SECRET" \
  -H "Content-Type: application/json" \
  -d '{"command_type":"MOVE_FORWARD","parameters":{"speed":0.2,"duration_ms":1000}}'

# ⚠️ SAFETY: Start with low speed (0.2 m/s) and short duration (1000ms)
# ⚠️ Ensure clear workspace and physical E-Stop is functional
```

### 4. Create and Execute Mission

```bash
# 1. Create mission via ULTRON
MISSION=$(curl -s -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Navigate to coordinate (5,5) and return"}')

MISSION_ID=$(echo "$MISSION" | python3 -c "import sys, json; print(json.load(sys.stdin)['mission_id'])")

# 2. Check mission details
curl "http://localhost:8000/mission/$MISSION_ID"

# 3. Watch execution in dashboard or via WebSocket
# (Backend automatically dispatches tasks to SCOUT01)
```

---

## 🔗 Important URLs

- **Backend API**: http://localhost:8000
- **API Documentation**: http://localhost:8000/docs
- **Health Check**: http://localhost:8000/health
- **Frontend Dashboard**: http://localhost:5173 (after starting)
- **SCOUT01 Camera**: http://10.241.171.41:8081/stream.mjpg
- **SCOUT01 SSH**: `ssh chawlaji@10.241.171.41`

---

## ⚠️ Safety Reminders

1. **Physical E-Stop**: Ensure SCOUT01's physical emergency stop button is functional
2. **Clear Workspace**: Verify area is clear before sending movement commands
3. **Start Slow**: Use low speeds (0.1-0.3 m/s) for initial tests
4. **Monitor Constantly**: Watch SCOUT01 during autonomous operation
5. **Software E-Stop**: Available at `POST /emergency-stop` (broadcasts STOP to all vehicles)
6. **Network Dependency**: Control requires stable WiFi connection to 10.241.171.41

---

## 📊 System Configuration

**Backend** (`/home/vikram/Projects/Project CU/.env`):
```
SHADOW_HARDWARE_LOCKOUT=false
SHADOW_DEFAULT_VEHICLE_MODE=real
SHADOW_ROSBRIDGE_ENABLED=true
SCOUT01_IP=10.241.171.41
SCOUT01_CAMERA_URL=http://10.241.171.41:8081/stream.mjpg
SCOUT01_ROSBRIDGE_WS=ws://10.241.171.41:9090
```

**ULTRON**:
- LLM Model: `gemini-3.5-flash-lite`
- Planning Timeout: 30s
- Replanning Timeout: 20s
- Mode: Real LangGraph agent (not stub)

---

## ✅ Overall Status: READY FOR OPERATION

**System is functional and ready for integration testing.**

Main requirement: Start ROS 2 telemetry publishing on SCOUT01 to establish vehicle connection.

---

Generated: $(date)
