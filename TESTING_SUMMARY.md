# Physical Scout Movement Testing - Documentation Summary

This document provides an overview of the testing resources created for verifying physical Scout movement controlled by the ESP32 device through the backend and frontend.

---

## Testing Documentation Files

### 1. **ESP32_PHYSICAL_MOVEMENT_TEST.md** (Comprehensive Guide)

**Purpose**: Complete, detailed guide for physical movement testing

**Contents**:
- System architecture overview
- Pre-test verification steps
- Safety gate configuration (all 4 gates explained)
- Step-by-step test sequence (8 tests total)
- Troubleshooting for common issues
- Safety checklist
- Quick command reference

**When to use**: First-time setup or when you need detailed explanations

**Key sections**:
- Pre-Test Verification → Ensure ESP32 is working before backend testing
- Configuration for Physical Testing → How to safely enable hardware control
- Test Sequence → Tests 1-8 covering all aspects
- Troubleshooting → Solutions for common problems

---

### 2. **QUICK_START_PHYSICAL_TEST.md** (5-Minute Guide)

**Purpose**: Fast-track guide for experienced users

**Contents**:
- Prerequisites check (2 min)
- Configuration (1 min)
- System startup (1 min)
- Quick test methods
- Common fixes

**When to use**: After initial setup, or for quick verification

**Steps**:
1. Find ESP32 port
2. Test ESP32 directly with `screen`
3. Configure 4 safety gates in `.env`
4. Start backend and frontend
5. Run interactive test script

---

### 3. **test_esp32_physical.py** (Interactive Test Script)

**Purpose**: Python script with interactive menu for all tests

**Features**:
- System diagnostic (checks configuration)
- Backend health check
- Scout control status
- Basic movement test (automated W/A/S/D/X sequence)
- Emergency stop test
- Mission execution test
- Single command mode

**Usage**:
```bash
python3 test_esp32_physical.py
```

**Menu Options**:
1. Run Diagnostic ← **Start here to verify system readiness**
2. Check Backend Health
3. Check Scout Control Status
4. Test Basic Movement
5. Test Emergency Stop
6. Test Mission Execution
7. Send Single Command

**Benefits**:
- Color-coded output (success/warning/error)
- Automated test sequences
- Safety prompts before dangerous operations
- Formatted status displays

---

## Testing Workflow

### For First-Time Setup:

1. **Read**: `ESP32_PHYSICAL_MOVEMENT_TEST.md` (sections 1-3)
2. **Verify**: ESP32 connection and firmware
3. **Configure**: Follow safety gate setup
4. **Test**: Run `test_esp32_physical.py` → Option 1 (Diagnostic)
5. **Validate**: Run Option 4 (Basic Movement) in safe environment

### For Quick Testing:

1. **Reference**: `QUICK_START_PHYSICAL_TEST.md`
2. **Check**: Prerequisites and configuration
3. **Run**: `python3 test_esp32_physical.py`
4. **Test**: Option 1 (Diagnostic) then Option 4 (Movement)

### For Debugging:

1. **Consult**: `ESP32_PHYSICAL_MOVEMENT_TEST.md` → Troubleshooting section
2. **Check**: Backend logs for errors
3. **Verify**: Each component in isolation:
   - ESP32 with `screen` command
   - Backend health endpoint
   - Scout control status endpoint
4. **Test**: Manual curl commands from troubleshooting guide

---

## Safety Gate Configuration

All 4 gates must be cleared for physical movement:

| Gate | Variable | Safe Value | Test Value | Purpose |
|------|----------|------------|------------|---------|
| 1 | `SHADOW_HARDWARE_LOCKOUT` | `true` | `false` | Master hardware safety lock |
| 2 | `SHADOW_DEFAULT_VEHICLE_MODE` | `simulated` | `real` | Vehicle operation mode |
| 3 | `SHADOW_SCOUT_SERIAL_ENABLED` | `false` | `true` | Enable serial output |
| 4 | `SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED` | `false` | `true` | Confirm hardware failsafe |

**Configuration Location**: `.env` file in project root

**After Testing**: Always revert to safe values!

---

## Testing Levels

### Level 1: Connection Verification
- ESP32 port detection
- Serial port open test
- Backend health check
- Scout control status

**Tools**: 
- `ls /dev/cu.usbmodem*`
- `curl http://localhost:8000/health`
- `curl http://localhost:8000/scout/control/status`

### Level 2: Direct ESP32 Testing
- Manual serial commands with `screen`
- Verify W/A/S/D/X commands work
- Confirm ESP-NOW communication to Scout

**Tools**:
- `screen /dev/cu.usbmodem14201 115200`

### Level 3: Backend API Testing
- Single command via REST API
- Emergency stop verification
- Status monitoring

**Tools**:
- `curl` commands
- `test_esp32_physical.py` Option 7

### Level 4: Frontend Integration Testing
- WebSocket keyboard control
- Real-time status display
- Browser-based emergency stop

**Tools**:
- Browser at http://localhost:5173
- Scout Control panel

### Level 5: Mission Execution Testing
- Full mission planning via ULTRON
- Task execution on physical hardware
- Multi-step movement sequences

**Tools**:
- `test_esp32_physical.py` Option 6
- Frontend mission panel

---

## Critical Safety Information

### Before ANY Physical Testing:

✅ Read safety sections in `ESP32_PHYSICAL_MOVEMENT_TEST.md`
✅ Verify emergency stop works (software + hardware)
✅ Clear area or lift wheels
✅ Someone supervising
✅ Know where motor power switch is
✅ USB-loss failsafe verified (Gate 4) OR manual cable control

### Emergency Stop Methods:

**Method 1: API (fastest)**
```bash
curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'
```

**Method 2: Browser**
- Click "Emergency Stop" button in dashboard

**Method 3: Hardware (ultimate safety)**
- Cut motor power physically
- Unplug ESP32 USB cable

### Stop Command Verification

The system will NOT allow testing until stop ('X') command is reliable:
1. Test with `screen`: Type X, motors must stop
2. Test with backend: Send stop command via API
3. Test emergency stop: Verify immediate halt
4. Never proceed if stop is unreliable

---

## Common Issues and Solutions

### Issue: "transmitter_connected: false"
**Cause**: Backend cannot open ESP32 serial port
**Solution**: Check USB connection, verify port in `.env`, restart backend

### Issue: "Serial writes remain disabled"
**Cause**: One or more safety gates still active
**Solution**: Review all 4 gates in `.env`, restart backend

### Issue: Commands sent but Scout doesn't move
**Cause**: ESP32 receives but doesn't forward commands
**Solution**: Test ESP32 with `screen`, verify ESP-NOW pairing, check motor power

### Issue: Scout moves but doesn't stop
**Cause**: Stop command not reaching ESP32 or firmware issue
**Solution**: ⚠️ CRITICAL - Stop testing immediately, verify stop works with `screen`

**Full troubleshooting**: See `ESP32_PHYSICAL_MOVEMENT_TEST.md` → Troubleshooting section

---

## Quick Reference Commands

```bash
# Find ESP32 port
ls /dev/cu.usbmodem* /dev/ttyUSB* /dev/ttyACM*

# Test ESP32 directly
screen /dev/cu.usbmodem14201 115200  # Exit: Ctrl+A, K, Y

# Start backend
cd backend && source .venv/bin/activate
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload --host 127.0.0.1 --port 8000

# Start frontend
cd frontend && npm run dev

# Run test script
python3 test_esp32_physical.py

# Check health
curl http://localhost:8000/health | python3 -m json.tool

# Check Scout status
curl http://localhost:8000/scout/control/status | python3 -m json.tool

# Emergency stop
curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'

# Reset emergency stop
curl -X POST http://localhost:8000/emergency-stop/reset

# Send movement command (0.5s forward)
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -H "Content-Type: application/json" \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}'
```

---

## Testing Checklist

Use this checklist before each testing session:

### Pre-Test Setup
- [ ] ESP32 connected via USB
- [ ] ESP32 port identified: `/dev/cu.usbmodem______`
- [ ] Manual `screen` test successful (W and X work)
- [ ] `.env` configured with correct port
- [ ] All 4 safety gates configured for testing
- [ ] Backend `.env` matches settings (check for `backend/.env` override)

### Safety Verification
- [ ] Scout in safe environment (clear area OR wheels lifted)
- [ ] Emergency stop tested and working
- [ ] Motor power switch accessible
- [ ] USB-loss failsafe verified OR manual cable control
- [ ] Supervisor present
- [ ] Emergency stop curl command ready in terminal

### System Checks
- [ ] Backend started and shows serial connection
- [ ] Frontend accessible at http://localhost:5173
- [ ] Health check shows `hardware_lockout: false`
- [ ] Scout control status shows `transmitter_connected: true`
- [ ] Scout control status shows `mode: "real"`

### Post-Test Cleanup
- [ ] Emergency stop triggered and tested
- [ ] `.env` reverted to safe values
- [ ] Backend restarted with safe config
- [ ] Health check confirms `hardware_lockout: true`
- [ ] Test notes documented

---

## File Locations

```
/Users/khushman/Desktop/Operation Shadow/OperationShadow-new/
├── ESP32_PHYSICAL_MOVEMENT_TEST.md       ← Comprehensive guide
├── QUICK_START_PHYSICAL_TEST.md          ← 5-minute quickstart
├── TESTING_SUMMARY.md                    ← This file
├── test_esp32_physical.py                ← Interactive test script
├── .env                                  ← Configuration file
├── .env.backup                           ← Backup (create before testing)
└── backend/
    └── .env                              ← May override root .env
```

---

## Getting Help

**For detailed explanations**: Read `ESP32_PHYSICAL_MOVEMENT_TEST.md`

**For quick setup**: Follow `QUICK_START_PHYSICAL_TEST.md`

**For automated testing**: Run `python3 test_esp32_physical.py`

**For troubleshooting**: Check Troubleshooting section in comprehensive guide

**For safety questions**: Review Safety Checklist sections in all guides

---

## Next Steps

1. **First Time?** 
   - Read `ESP32_PHYSICAL_MOVEMENT_TEST.md` fully
   - Follow Pre-Test Verification section
   - Configure safety gates carefully

2. **Ready to Test?**
   - Follow `QUICK_START_PHYSICAL_TEST.md`
   - Run `test_esp32_physical.py` → Option 1 (Diagnostic)
   - Proceed with Option 4 (Basic Movement) if diagnostic passes

3. **Encountered Issues?**
   - Check Troubleshooting in `ESP32_PHYSICAL_MOVEMENT_TEST.md`
   - Review backend logs
   - Test ESP32 in isolation with `screen`

4. **After Testing**
   - Revert `.env` to safe values
   - Restart backend
   - Verify safety gates restored

---

**Remember**: Safety first, always! Never skip safety checks or rush the testing process.
