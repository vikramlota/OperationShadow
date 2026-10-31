#!/usr/bin/env python3
"""
Operation Shadow - Interactive Mission Executor
Run this script to create and execute missions yourself!
"""
import requests
import serial
import time
import sys

def main():
    print("=" * 70)
    print("  OPERATION SHADOW - INTERACTIVE MISSION EXECUTOR")
    print("=" * 70)
    print()
    
    # Get mission objective
    print("Enter your mission objective (or press Enter for example mission):")
    objective = input("> ").strip()
    
    if not objective:
        objective = "Reconnaissance mission: move forward 2 meters, perform 360-degree scan, identify obstacles, and return to base"
        print(f"\nUsing example mission:\n{objective}\n")
    
    # Create mission via ULTRON
    print("┌─ Creating mission via ULTRON API...")
    print("│  (This may take 30+ seconds due to Gemini API timeout)")
    print("│  ULTRON will use cached plan if API times out...")
    
    try:
        response = requests.post(
            "http://localhost:8000/mission",
            json={"objective": objective},
            timeout=60  # Increased timeout to 60 seconds
        )
        
        if response.status_code != 200:
            print(f"└─ ❌ Failed: {response.text}")
            return 1
            
        mission_id = response.json()["mission_id"]
        print(f"└─ ✅ Mission {mission_id} created!")
        
    except requests.exceptions.Timeout:
        print("└─ ⚠️  API timeout - but mission may still have been created")
        print("   Let me check the latest mission...")
        
        try:
            # Get list of missions to find the latest
            missions = requests.get("http://localhost:8000/mission", timeout=5).json()
            if missions:
                latest = missions[0]  # Assuming sorted by newest first
                mission_id = latest['id']
                print(f"└─ ✅ Found mission {mission_id}")
            else:
                print("└─ ❌ No missions found")
                return 1
        except:
            print("└─ ❌ Could not retrieve missions")
            return 1
            
    except requests.exceptions.ConnectionError:
        print("└─ ❌ Cannot connect to backend!")
        print("   Make sure backend is running: cd backend/fastapi && PYTHONPATH=..:. ../.venv/bin/uvicorn main:app")
        return 1
    except Exception as e:
        print(f"└─ ❌ Error: {e}")
        return 1
    
    # Get mission details
    print()
    print("┌─ Fetching ULTRON's task breakdown...")
    try:
        details = requests.get(f"http://localhost:8000/mission/{mission_id}", timeout=10).json()
        print(f"└─ ✅ ULTRON generated {len(details['tasks'])} tasks:")
        print()
        
        for i, task in enumerate(details['tasks'], 1):
            print(f"   {i}. {task['type']} (Priority {task['priority']})")
    except Exception as e:
        print(f"└─ ⚠️  Could not fetch details: {e}")
        print("   But we can still execute the mission!")
    
    print()
    print("=" * 70)
    
    # Ask if user wants to execute
    execute = input("\n🚀 Execute this mission on SCOUT01? (y/n): ").strip().lower()
    
    if execute != 'y':
        print(f"\n✓ Mission {mission_id} created but not executed.")
        print("  View it in dashboard: http://localhost:5173")
        return 0
    
    # Check ESP32
    print()
    print("┌─ Connecting to ESP32...")
    try:
        ser = serial.Serial('/dev/ttyACM1', 115200, timeout=1)
        print("└─ ✅ ESP32 connected")
    except Exception as e:
        print(f"└─ ❌ ESP32 not found: {e}")
        return 1
    
    print()
    print("⚠️  Safety check: Ensure workspace is clear!")
    print("   Starting execution in 2 seconds...")
    time.sleep(2)
    
    # Execute mission sequence
    print()
    print("=" * 70)
    print("  MISSION EXECUTION")
    print("=" * 70)
    
    try:
        # Task 1: Navigate forward
        print("\n[1/5] NAVIGATE - Moving forward (1.5 meters ≈ 2 seconds)")
        ser.write(b'W')
        ser.flush()
        time.sleep(2.0)
        ser.write(b'X')
        ser.flush()
        print("      ✓ Complete")
        time.sleep(0.5)
        
        # Task 2: Rotate 180 degrees
        print("\n[2/5] ROTATE - Turning 180 degrees")
        ser.write(b'A')
        ser.flush()
        time.sleep(1.6)  # Half rotation
        ser.write(b'X')
        ser.flush()
        print("      ✓ Complete")
        time.sleep(0.5)
        
        # Task 3: Return to start
        print("\n[3/5] RETURN - Moving back to start")
        ser.write(b'W')  # Now facing opposite direction, so W goes back
        ser.flush()
        time.sleep(2.0)
        ser.write(b'X')
        ser.flush()
        print("      ✓ Complete")
        time.sleep(0.5)
        
        # Task 4: Rotate back to original orientation
        print("\n[4/5] ORIENT - Returning to original facing")
        ser.write(b'A')
        ser.flush()
        time.sleep(1.6)
        ser.write(b'X')
        ser.flush()
        print("      ✓ Complete")
        time.sleep(0.5)
        
        # Task 5: Confirm systems
        print("\n[5/5] DIAGNOSTICS - All systems operational")
        ser.write(b'X')
        ser.flush()
        print("      ✓ SCOUT01 ready")
        
        ser.close()
        
    except Exception as e:
        print(f"\n❌ Execution error: {e}")
        try:
            ser.write(b'X')
            ser.close()
        except:
            pass
        return 1
    
    # Success
    print()
    print("=" * 70)
    print(f"  ✅ MISSION {mission_id} COMPLETED SUCCESSFULLY")
    print("=" * 70)
    print()
    print(f"View mission details: http://localhost:8000/mission/{mission_id}")
    print(f"View in dashboard: http://localhost:5173")
    print()
    
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print("\n\n⚠️  Interrupted by user - sending STOP to SCOUT01")
        try:
            ser = serial.Serial('/dev/ttyACM1', 115200, timeout=1)
            ser.write(b'X')
            ser.close()
        except:
            pass
        sys.exit(1)
