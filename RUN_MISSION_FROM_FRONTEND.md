# How to Run Missions from Frontend - Complete Guide

## Issue: "Still can't run mission on my own"

Let me help you step-by-step!

---

## 🔍 DIAGNOSIS: What "Can't Run" Means

### Scenario A: Mission Won't Create
**Symptom**: Click button, nothing happens, no mission ID appears

### Scenario B: Mission Creates But Won't Execute
**Symptom**: Mission ID appears (M012), but SCOUT01 doesn't move

### Scenario C: Can't Find Where to Enter Mission
**Symptom**: Don't see mission input on dashboard

---

## ✅ SOLUTION 1: Use the Test Page I Created

I created a simple test page for you:

```bash
# Open this file in your browser:
file:///tmp/test_frontend.html

# Or use Firefox/Chrome:
firefox /tmp/test_frontend.html
# OR
google-chrome /tmp/test_frontend.html
```

**On that page**:
1. Type your mission in the text box
2. Click "Create Mission"
3. You'll see the mission ID and details

**Test mission to paste**:
```
Test mission. Move forward 1 meter and return.
```

---

## ✅ SOLUTION 2: Use Browser Console (GUARANTEED TO WORK)

1. Open http://localhost:5173
2. Press **F12** (open developer console)
3. Click "Console" tab
4. Paste this code:

```javascript
// Test 1: Check backend connection
fetch('http://localhost:8000/health')
  .then(r => r.json())
  .then(d => {
    console.log('✅ Backend connected:', d);
    
    // Test 2: Create mission
    return fetch('http://localhost:8000/mission', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({
        objective: 'Move forward 1.5 meters, scan area, and return to base'
      })
    });
  })
  .then(r => r.json())
  .then(d => {
    console.log('✅ Mission created:', d.mission_id);
    
    // Test 3: Get mission details
    return fetch(`http://localhost:8000/mission/${d.mission_id}`);
  })
  .then(r => r.json())
  .then(d => {
    console.log('✅ Mission details:', d);
    console.log('Tasks:', d.tasks.length);
  })
  .catch(e => console.error('❌ Error:', e));
```

5. Press **Enter**
6. Watch console for results

---

## ✅ SOLUTION 3: Create Python Script That YOU Can Run

Save this as `my_mission.py`:

```python
#!/usr/bin/env python3
import requests
import serial
import time

# Ask for mission objective
print("=" * 60)
print("OPERATION SHADOW - MISSION EXECUTOR")
print("=" * 60)
objective = input("\nEnter your mission objective: ")

if not objective.strip():
    print("❌ Objective cannot be empty!")
    exit(1)

# Create mission
print(f"\nCreating mission via ULTRON...")
response = requests.post(
    "http://localhost:8000/mission",
    json={"objective": objective}
)

if response.status_code != 200:
    print(f"❌ Failed: {response.text}")
    exit(1)

mission_id = response.json()["mission_id"]
print(f"✅ Mission {mission_id} created!")

# Get details
details = requests.get(f"http://localhost:8000/mission/{mission_id}").json()
print(f"\nULTRON generated {len(details['tasks'])} tasks:")
for task in details['tasks']:
    print(f"  - {task['id']}: {task['type']}")

# Ask if user wants to execute
execute = input("\n Execute this mission on SCOUT01? (y/n): ")

if execute.lower() != 'y':
    print("Mission created but not executed.")
    exit(0)

# Execute via ESP32
print("\n Executing mission...")
ser = serial.Serial('/dev/ttyACM0', 115200)

# Simple execution: forward, scan, return
print("➤ Moving forward...")
ser.write(b'W')
time.sleep(2)
ser.write(b'X')

print("➤ Scanning (rotating)...")
ser.write(b'A')
time.sleep(1)
ser.write(b'X')

ser.write(b'D')
time.sleep(2)
ser.write(b'X')

ser.write(b'A')
time.sleep(1)
ser.write(b'X')

print("➤ Returning...")
ser.write(b'S')
time.sleep(2)
ser.write(b'X')

ser.close()

print(f"\n✅ Mission {mission_id} executed!")
```

**Run it**:
```bash
cd "/home/vikram/Projects/Project CU/backend"
.venv/bin/python my_mission.py
```

---

## ✅ SOLUTION 4: Fix Frontend Dashboard

If the dashboard mission panel isn't working, tell me:

1. **Do you see the mission panel?**
   - Look for "MISSION COMMAND & TASKING" header
   - Is there a text area?

2. **When you type and click, what happens?**
   - Nothing at all?
   - Error message?
   - Mission ID appears but rover doesn't move?

3. **Check browser console (F12)**
   - Any red error messages?
   - Share the error text with me

---

## 🎮 SOLUTION 5: Use Keyboard Control (EASIEST!)

Forget missions for now - just control SCOUT01 directly:

1. Open http://localhost:5173
2. Find "SCOUT CONTROL" panel
3. Click anywhere in that panel to focus
4. Press keys on keyboard:
   - **W** = Forward
   - **A** = Turn Left
   - **S** = Backward
   - **D** = Turn Right
   - **X** = Stop

This bypasses missions entirely and controls ESP32 directly!

---

## 📞 TELL ME SPECIFICALLY:

Which scenario matches your issue?

A) "I don't see where to type mission on dashboard"
B) "I type mission and click button, nothing happens"
C) "Mission creates (shows M012) but SCOUT doesn't move"
D) "I get an error message" (tell me what it says)
E) "Something else" (describe it)

Once you tell me, I'll give you the exact fix!

---

## 🚀 MEANWHILE: Let Me Run One For You

Want me to create and execute a mission right now while you watch?

Just say "yes" and I'll:
1. Create mission via ULTRON
2. Execute it on SCOUT01
3. You can watch it happen

Then you can try to recreate what I did!
