# ULTRON Mission Examples

## How to Create a Mission

```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"YOUR MISSION DESCRIPTION HERE"}'
```

---

## 🎯 Mission Ideas

### 1. Simple Patrol Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Patrol the perimeter of the room. Move forward 2 meters, turn right 90 degrees, repeat for all four sides, then return to starting position."}'
```

### 2. Search and Identify Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Search the area for a red object. Scan left and right while moving forward. If target is found, approach and capture image. Report findings and return."}'
```

### 3. Obstacle Avoidance Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Navigate to the far corner of the room while avoiding any obstacles detected by sensors. Map obstacle locations and find the safest path."}'
```

### 4. Inspection Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Inspect the designated area for anomalies. Move slowly in a grid pattern, scanning continuously. Stop and analyze any unusual readings from temperature or gas sensors."}'
```

### 5. Reconnaissance Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Perform reconnaissance of the unknown corridor ahead. Advance cautiously, mapping the environment with camera and sensors. Report any hazards or points of interest encountered."}'
```

### 6. Delivery Simulation Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Navigate to waypoint Alpha located 5 meters ahead. Verify arrival at destination. Wait 3 seconds to simulate package delivery. Return to base via shortest safe route."}'
```

### 7. Emergency Response Simulation
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Respond to simulated emergency at coordinate (3, 4). Navigate quickly but safely. Upon arrival, perform 360-degree scan. Identify and report all visible hazards. Maintain position and await further instructions."}'
```

### 8. Systematic Area Mapping
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Create a detailed map of the operational area. Execute systematic grid search pattern covering 4x4 meter zone. Record all obstacles, boundaries, and notable features. Generate complete occupancy map."}'
```

### 9. Target Tracking Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Locate and track the moving target. Maintain visual contact while keeping safe following distance of 1 meter. Adapt speed to match target movement. Continue tracking until target stops or mission timeout."}'
```

### 10. Multi-Point Waypoint Mission
```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Visit waypoints A, B, and C in sequence. A: move forward 2m. B: turn left, advance 1.5m. C: turn right, advance 2m. At each waypoint, stop for 2 seconds and capture image. Return to start position after completing route."}'
```

---

## 🚀 Recommended First Mission (Safe & Simple)

```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Test navigation capabilities. Move forward 1 meter slowly, stop and scan surroundings, turn 180 degrees, return to starting position. Report mission status."}'
```

**Why this one?**
- Short duration (~30 seconds)
- Simple movements (forward, turn, return)
- Safe (small distance)
- Demonstrates ULTRON planning
- Easy to verify success

---

## 📋 After Creating Mission

### Check Mission Status
```bash
# Replace M007 with your mission ID
curl http://localhost:8000/mission/M007
```

### List All Missions
```bash
curl http://localhost:8000/mission
```

### View Mission Tasks
```bash
curl http://localhost:8000/mission/M007 | python3 -m json.tool
```

---

## 🎮 Advanced: Missions with Language

ULTRON supports multiple languages for rationale:

```bash
curl -X POST http://localhost:8000/mission \
  -H "Content-Type: application/json" \
  -d '{"objective":"Explore the designated sector and report findings","language":"Spanish"}'
```

ULTRON will generate rationale in Spanish!

---

## ⚠️ Important Notes

- **ULTRON generates the plan automatically** - you just provide the objective
- **Tasks are assigned to SCOUT01** by ULTRON's task assignment logic
- **Current limitation**: Vehicle must be ONLINE (sending telemetry) for task execution
- **Simulated mode**: Tasks show as COMPLETED but don't control real hardware
- **Real mode**: Tasks dispatch actual movement commands to ESP32

---

## 🔍 What ULTRON Does

When you create a mission, ULTRON:
1. **Analyzes** your objective
2. **Plans** the optimal task sequence
3. **Validates** the plan against safety constraints
4. **Assigns** tasks to SCOUT01
5. **Monitors** execution (when vehicle is ONLINE)
6. **Replans** if hazards or targets are detected

---

Generated for Operation Shadow - ULTRON Testing
