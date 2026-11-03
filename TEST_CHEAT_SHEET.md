# ESP32 Physical Test - Quick Cheat Sheet

**Keep this open during testing for quick reference**

---

## 🚦 Before You Start

```bash
# 1. Find ESP32 port
ls /dev/cu.usbmodem*

# 2. Test ESP32 manually
screen /dev/cu.usbmodem14201 115200
# Type: W X A D S (test each)
# Exit: Ctrl+A, K, Y
```

**If manual test fails → Fix ESP32 first!**

---

## ⚙️ Configuration (.env file)

```bash
# Edit
nano .env

# Set these 5 values:
SHADOW_HARDWARE_LOCKOUT=false
SHADOW_DEFAULT_VEHICLE_MODE=real
SHADOW_SCOUT_SERIAL_ENABLED=true
SHADOW_SCOUT_SERIAL_PORT=/dev/cu.usbmodem14201  # ← YOUR port!
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=true      # ← Only if verified!
```

---

## 🚀 Start System

```bash
# Terminal 1: Backend
cd backend && source .venv/bin/activate
export $(cat ../.env | grep -v '^#' | xargs)
PYTHONPATH=fastapi:.:.. uvicorn main:app --reload

# Terminal 2: Frontend
cd frontend && npm run dev

# Terminal 3: Test Script
python3 test_esp32_physical.py
```

---

## ✅ Quick Health Check

```bash
# Backend alive?
curl http://localhost:8000/health

# ESP32 connected?
curl http://localhost:8000/scout/control/status

# Should see: "transmitter_connected": true
```

---

## 🎮 Test Movement

### Method 1: Test Script (Recommended)
```bash
python3 test_esp32_physical.py
# Select: 1 (Diagnostic) → Then 4 (Movement)
```

### Method 2: Browser
```
Open: http://localhost:5173
Find: SCOUT CONTROL panel
Click to focus
Press: W A S D X keys
```

### Method 3: curl
```bash
# Stop (safest first)
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -d '{"command_type": "stop"}'

# Forward 0.5s
curl -X POST http://localhost:8000/vehicle/SCOUT01/command \
  -d '{"command_type": "move", "direction": "forward", "duration_s": 0.5}'
```

---

## 🚨 Emergency Stop

**Keep this ready at all times:**
```bash
curl -X POST http://localhost:8000/emergency-stop -d '{"scope": "ALL"}'
```

**Reset after:**
```bash
curl -X POST http://localhost:8000/emergency-stop/reset
```

---

## 🔧 Common Issues

| Problem | Quick Fix |
|---------|-----------|
| `transmitter_connected: false` | Check USB cable, verify port in `.env`, restart backend |
| "Serial writes remain disabled" | Check all 4 gates in `.env`, restart backend |
| Commands sent but no movement | Test ESP32 with `screen`, check ESP-NOW pairing |
| Scout won't stop | **CUT POWER NOW**, fix firmware |
| WebSocket disconnected | Refresh browser, check backend running |

---

## 🔍 Diagnostic Commands

```bash
# Port exists?
ls /dev/cu.usbmodem*

# Config correct?
cat .env | grep -E "LOCKOUT|MODE|SERIAL"

# Backend healthy?
curl http://localhost:8000/health | python3 -m json.tool

# ESP32 connected?
curl http://localhost:8000/scout/control/status | python3 -m json.tool

# Vehicle status?
curl http://localhost:8000/vehicle/SCOUT01/status | python3 -m json.tool
```

---

## 🎯 Expected Success

```json
// Health check
{
  "status": "operational",
  "hardware_lockout": false,  // ← Should be false
  "vehicle_modes": {
    "SCOUT01": "real"         // ← Should be "real"
  }
}

// Scout control status
{
  "mode": "real",              // ← Should be "real"
  "transmitter_connected": true, // ← Should be true
  "current_command": "X"
}
```

---

## 🔄 After Testing - REVERT!

```bash
# Edit .env
nano .env

# Change back:
SHADOW_HARDWARE_LOCKOUT=true
SHADOW_DEFAULT_VEHICLE_MODE=simulated
SHADOW_SCOUT_SERIAL_ENABLED=false
SHADOW_SCOUT_SERIAL_FAILSAFE_VERIFIED=false

# Restart backend
# Verify: curl http://localhost:8000/health | grep lockout
```

---

## 📍 Port Reference (macOS)

```
ESP32 usually appears as:
/dev/cu.usbmodem14201  ← Most common
/dev/cu.usbmodem14301
/dev/cu.usbmodem*
/dev/cu.usbserial*

If you see /dev/tty.* instead of /dev/cu.*, use the /dev/cu.* version!
```

---

## 🎓 Test Script Menu

```
1. Run Diagnostic          ← Start here!
2. Check Backend Health
3. Check Scout Control Status
4. Test Basic Movement     ← Main test
5. Test Emergency Stop
6. Test Mission Execution
7. Send Single Command
0. Exit
```

---

## ⚠️ Safety Checklist

Before physical testing:
- [ ] Scout in safe area OR wheels lifted
- [ ] Emergency stop tested
- [ ] Motor power switch accessible
- [ ] Supervisor present
- [ ] Emergency stop command ready
- [ ] USB-loss failsafe verified

**Never skip safety checks!**

---

## 🆘 Help

**Detailed guide**: `ESP32_PHYSICAL_MOVEMENT_TEST.md`
**Quick start**: `QUICK_START_PHYSICAL_TEST.md`
**Troubleshooting**: `TROUBLESHOOTING_FLOWCHART.md`
**Overview**: `TESTING_SUMMARY.md`

---

## 💾 Save This Command Block

```bash
# Complete test session in one copy-paste
cd "/Users/khushman/Desktop/Operation Shadow/OperationShadow-new"

# Check ESP32
ls /dev/cu.usbmodem*

# Check config
cat .env | grep -E "LOCKOUT|MODE|SERIAL"

# Run diagnostic
python3 test_esp32_physical.py
# Select: 1

# If all good, test movement
# In script menu: Select 4
```

---

## 🎯 Quick Test Sequence

1. Find ESP32 port → `ls /dev/cu.usbmodem*`
2. Test with screen → Verify W/X work
3. Configure .env → All 4 gates
4. Start backend → Check for serial connection message
5. Run diagnostic → `python3 test_esp32_physical.py` → 1
6. Test movement → Script menu → 4
7. Revert config → Change .env back to safe values

**Total time: 10 minutes**

---

## 📞 Decision: What Should I Do?

| Your Situation | What To Do |
|----------------|------------|
| First time ever | Read `ESP32_PHYSICAL_MOVEMENT_TEST.md` fully |
| Done this before | Follow this cheat sheet |
| Something's broken | Check `TROUBLESHOOTING_FLOWCHART.md` |
| Need automation | Run `python3 test_esp32_physical.py` |
| Need quick reference | Use `QUICK_START_PHYSICAL_TEST.md` |

---

**Print this or keep it on a second screen during testing!**
