#!/usr/bin/env python3
"""
ESP32 Physical Movement Test Script
====================================

This script provides an interactive test menu for verifying physical Scout movement
through the backend with ESP32 device connected via USB serial.

SAFETY WARNING: Only run this script in a safe environment with:
- Clear area or wheels lifted
- Emergency stop button accessible
- Motors can be powered off immediately

Usage:
    python3 test_esp32_physical.py
"""

import json
import sys
import time
from typing import Any

try:
    import requests
except ImportError:
    print("❌ ERROR: 'requests' library not installed.")
    print("Install with: pip install requests")
    sys.exit(1)

# Configuration
BACKEND_URL = "http://localhost:8000"
TIMEOUT = 5


class Colors:
    """ANSI color codes for terminal output."""
    HEADER = '\033[95m'
    OKBLUE = '\033[94m'
    OKCYAN = '\033[96m'
    OKGREEN = '\033[92m'
    WARNING = '\033[93m'
    FAIL = '\033[91m'
    ENDC = '\033[0m'
    BOLD = '\033[1m'


def print_header(text: str) -> None:
    """Print formatted header."""
    print(f"\n{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{text.center(70)}{Colors.ENDC}")
    print(f"{Colors.HEADER}{Colors.BOLD}{'=' * 70}{Colors.ENDC}\n")


def print_success(text: str) -> None:
    """Print success message."""
    print(f"{Colors.OKGREEN}✅ {text}{Colors.ENDC}")


def print_error(text: str) -> None:
    """Print error message."""
    print(f"{Colors.FAIL}❌ {text}{Colors.ENDC}")


def print_warning(text: str) -> None:
    """Print warning message."""
    print(f"{Colors.WARNING}⚠️  {text}{Colors.ENDC}")


def print_info(text: str) -> None:
    """Print info message."""
    print(f"{Colors.OKCYAN}ℹ️  {text}{Colors.ENDC}")


def check_backend_health() -> dict[str, Any] | None:
    """Check if backend is running and get health status."""
    print_header("Backend Health Check")
    try:
        response = requests.get(f"{BACKEND_URL}/health", timeout=TIMEOUT)
        response.raise_for_status()
        health = response.json()
        
        print_success(f"Backend is {health.get('status', 'unknown')}")
        print(f"   Hardware Lockout: {health.get('hardware_lockout', 'unknown')}")
        print(f"   Vehicles: {', '.join(health.get('vehicles', []))}")
        
        vehicle_modes = health.get('vehicle_modes', {})
        for vehicle, mode in vehicle_modes.items():
            print(f"   {vehicle} Mode: {mode}")
        
        # Check for safety issues
        if health.get('hardware_lockout') is True:
            print_warning("Hardware lockout is ENABLED - physical movement is blocked")
            print_info("Set SHADOW_HARDWARE_LOCKOUT=false in .env to enable physical control")
        
        if vehicle_modes.get('SCOUT01') == 'simulated':
            print_warning("SCOUT01 is in SIMULATED mode")
            print_info("Set SHADOW_DEFAULT_VEHICLE_MODE=real in .env for physical control")
        
        return health
    
    except requests.exceptions.ConnectionError:
        print_error("Cannot connect to backend at " + BACKEND_URL)
        print_info("Start backend with: cd backend && PYTHONPATH=fastapi:.:.. uvicorn main:app --reload")
        return None
    except requests.exceptions.Timeout:
        print_error("Backend request timed out")
        return None
    except Exception as e:
        print_error(f"Unexpected error: {e}")
        return None


def check_scout_control_status() -> dict[str, Any] | None:
    """Check Scout control status including serial connection."""
    print_header("Scout Control Status")
    try:
        response = requests.get(f"{BACKEND_URL}/scout/control/status", timeout=TIMEOUT)
        response.raise_for_status()
        status = response.json()
        
        print(f"   Mode: {status.get('mode', 'unknown')}")
        print(f"   Transmitter Connected: {status.get('transmitter_connected', False)}")
        print(f"   Current Command: {status.get('current_command', 'unknown')}")
        print(f"   Lease Owner: {status.get('lease_owner', 'None')}")
        
        # Interpret status
        if status.get('transmitter_connected'):
            print_success("ESP32 serial connection is active")
        else:
            print_error("ESP32 transmitter is NOT connected")
            print_info("Check: ls /dev/cu.usbmodem* or /dev/ttyUSB* or /dev/ttyACM*")
            print_info("Verify SHADOW_SCOUT_SERIAL_PORT in .env matches your ESP32 port")
        
        if status.get('mode') == 'real':
            print_success("SCOUT01 is in REAL mode")
        else:
            print_warning("SCOUT01 is in simulation mode")
        
        return status
    
    except requests.exceptions.RequestException as e:
        print_error(f"Failed to get Scout control status: {e}")
        return None


def send_single_command(command: str, duration: float = 0.0) -> bool:
    """Send a single movement command to Scout."""
    print(f"\n   Sending command: {command} (duration: {duration}s)")
    
    try:
        payload = {
            "command_type": "stop" if command == "X" else "move",
        }
        
        if command != "X":
            direction_map = {
                "W": "forward",
                "S": "backward",
                "A": "left",
                "D": "right"
            }
            payload["direction"] = direction_map.get(command, "forward")
            if duration > 0:
                payload["duration_s"] = duration
        
        response = requests.post(
            f"{BACKEND_URL}/vehicle/SCOUT01/command",
            json=payload,
            timeout=TIMEOUT + duration
        )
        response.raise_for_status()
        
        print_success(f"Command '{command}' sent successfully")
        return True
    
    except requests.exceptions.RequestException as e:
        print_error(f"Failed to send command: {e}")
        return False


def test_basic_movement() -> None:
    """Test basic movement commands."""
    print_header("Basic Movement Test")
    print_warning("⚠️  SAFETY: Ensure Scout is in safe environment!")
    
    confirm = input("   Ready to test movement? (yes/no): ").strip().lower()
    if confirm not in ['yes', 'y']:
        print_info("Test cancelled")
        return
    
    commands = [
        ("X", 0.0, "Stop (safe start)"),
        ("W", 0.5, "Forward 0.5s"),
        ("X", 0.0, "Stop"),
        ("S", 0.5, "Backward 0.5s"),
        ("X", 0.0, "Stop"),
        ("A", 0.5, "Turn left 0.5s"),
        ("X", 0.0, "Stop"),
        ("D", 0.5, "Turn right 0.5s"),
        ("X", 0.0, "Stop"),
    ]
    
    print("\n   Test sequence:")
    for cmd, dur, desc in commands:
        print(f"      {cmd}: {desc}")
    
    input("\n   Press Enter to start sequence...")
    
    for cmd, dur, desc in commands:
        print(f"\n   Step: {desc}")
        if not send_single_command(cmd, dur):
            print_error("Test failed, stopping sequence")
            send_single_command("X", 0.0)  # Try to stop
            return
        if dur > 0:
            time.sleep(dur + 0.5)  # Wait for movement plus settle time
    
    print_success("Basic movement test completed!")


def test_emergency_stop() -> None:
    """Test emergency stop functionality."""
    print_header("Emergency Stop Test")
    print_warning("This will trigger the emergency stop system")
    
    confirm = input("   Trigger emergency stop? (yes/no): ").strip().lower()
    if confirm not in ['yes', 'y']:
        print_info("Test cancelled")
        return
    
    try:
        response = requests.post(
            f"{BACKEND_URL}/emergency-stop",
            json={"scope": "ALL"},
            timeout=TIMEOUT
        )
        response.raise_for_status()
        result = response.json()
        
        print_success("Emergency stop triggered!")
        print(f"   Scope: {result.get('scope', 'unknown')}")
        print(f"   Vehicles Stopped: {', '.join(result.get('vehicles_stopped', []))}")
        
        print_warning("Emergency stop is now LATCHED")
        print_info("All movement commands will be blocked until reset")
        
        # Ask if user wants to reset
        reset = input("\n   Reset emergency stop? (yes/no): ").strip().lower()
        if reset in ['yes', 'y']:
            response = requests.post(f"{BACKEND_URL}/emergency-stop/reset", timeout=TIMEOUT)
            response.raise_for_status()
            print_success("Emergency stop reset")
    
    except requests.exceptions.RequestException as e:
        print_error(f"Emergency stop failed: {e}")


def test_mission_execution() -> None:
    """Test full mission execution with physical movement."""
    print_header("Mission Execution Test")
    print_warning("This will create and execute a mission with real Scout movement")
    
    default_objective = "Move forward 1 meter, rotate 180 degrees, then return to start"
    print(f"\n   Default objective: {default_objective}")
    custom = input("   Use custom objective? (yes/no): ").strip().lower()
    
    if custom in ['yes', 'y']:
        objective = input("   Enter mission objective: ").strip()
        if not objective:
            print_error("Objective cannot be empty")
            return
    else:
        objective = default_objective
    
    print(f"\n   Creating mission with objective:")
    print(f"   '{objective}'")
    
    try:
        # Create mission
        response = requests.post(
            f"{BACKEND_URL}/mission",
            json={"objective": objective},
            timeout=TIMEOUT
        )
        response.raise_for_status()
        mission_data = response.json()
        mission_id = mission_data.get("mission_id")
        
        print_success(f"Mission created: {mission_id}")
        
        # Get mission details
        response = requests.get(f"{BACKEND_URL}/mission/{mission_id}", timeout=TIMEOUT)
        response.raise_for_status()
        mission = response.json()
        
        tasks = mission.get("tasks", [])
        print(f"\n   ULTRON generated {len(tasks)} tasks:")
        for i, task in enumerate(tasks, 1):
            print(f"      {i}. {task.get('type', 'unknown')}: {task.get('id', 'unknown')}")
        
        # Ask to execute
        confirm = input(f"\n   Execute mission {mission_id} on SCOUT01? (yes/no): ").strip().lower()
        if confirm not in ['yes', 'y']:
            print_info("Mission created but not executed")
            print_info(f"Execute later with: curl -X POST {BACKEND_URL}/mission/{mission_id}/execute")
            return
        
        print_warning("⚠️  SAFETY: Last chance to cancel!")
        final_confirm = input("   Proceed with execution? (yes/no): ").strip().lower()
        if final_confirm not in ['yes', 'y']:
            print_info("Execution cancelled")
            return
        
        # Execute mission
        print("\n   Executing mission...")
        response = requests.post(
            f"{BACKEND_URL}/mission/{mission_id}/execute",
            timeout=60  # Longer timeout for mission execution
        )
        response.raise_for_status()
        
        print_success(f"Mission {mission_id} execution started!")
        print_info("Watch Scout for physical movement")
        print_info(f"Monitor status: curl {BACKEND_URL}/mission/{mission_id}")
        
    except requests.exceptions.RequestException as e:
        print_error(f"Mission test failed: {e}")


def run_diagnostic() -> None:
    """Run comprehensive diagnostic checks."""
    print_header("System Diagnostic")
    
    # 1. Backend health
    health = check_backend_health()
    if not health:
        print_error("Backend is not accessible - cannot continue diagnostic")
        return
    
    # 2. Scout control status
    time.sleep(0.5)
    status = check_scout_control_status()
    
    # 3. Summary
    print_header("Diagnostic Summary")
    
    ready_for_physical = True
    issues = []
    
    if health.get('hardware_lockout') is True:
        ready_for_physical = False
        issues.append("Hardware lockout is enabled")
    
    if health.get('vehicle_modes', {}).get('SCOUT01') != 'real':
        ready_for_physical = False
        issues.append("SCOUT01 is not in real mode")
    
    if status and not status.get('transmitter_connected'):
        ready_for_physical = False
        issues.append("ESP32 transmitter is not connected")
    
    if ready_for_physical:
        print_success("System is ready for physical movement testing!")
    else:
        print_warning("System is NOT ready for physical movement:")
        for issue in issues:
            print(f"   - {issue}")
        
        print("\n   To enable physical movement:")
        print("   1. Edit .env file")
        print("   2. Set SHADOW_HARDWARE_LOCKOUT=false")
        print("   3. Set SHADOW_DEFAULT_VEHICLE_MODE=real")
        print("   4. Set SHADOW_SCOUT_SERIAL_ENABLED=true")
        print("   5. Connect ESP32 via USB")
        print("   6. Restart backend")


def main() -> None:
    """Main interactive menu."""
    print_header("ESP32 Physical Movement Test")
    print_warning("⚠️  SAFETY FIRST: Only use in safe, supervised environment")
    
    menu = """
    Test Menu:
    
    1. Run Diagnostic (Check system readiness)
    2. Check Backend Health
    3. Check Scout Control Status
    4. Test Basic Movement (W/A/S/D/X sequence)
    5. Test Emergency Stop
    6. Test Mission Execution
    7. Send Single Command
    
    0. Exit
    """
    
    while True:
        print(menu)
        choice = input("   Select option (0-7): ").strip()
        
        if choice == "0":
            print_info("Exiting...")
            sys.exit(0)
        
        elif choice == "1":
            run_diagnostic()
        
        elif choice == "2":
            check_backend_health()
        
        elif choice == "3":
            check_scout_control_status()
        
        elif choice == "4":
            test_basic_movement()
        
        elif choice == "5":
            test_emergency_stop()
        
        elif choice == "6":
            test_mission_execution()
        
        elif choice == "7":
            print("\n   Commands: W (forward), A (left), S (backward), D (right), X (stop)")
            cmd = input("   Enter command: ").strip().upper()
            if cmd in ['W', 'A', 'S', 'D', 'X']:
                dur = 0.0
                if cmd != 'X':
                    dur_str = input("   Duration (seconds, 0 for continuous): ").strip()
                    try:
                        dur = float(dur_str)
                    except ValueError:
                        print_error("Invalid duration, using 0")
                send_single_command(cmd, dur)
            else:
                print_error("Invalid command")
        
        else:
            print_error("Invalid option")
        
        input("\n   Press Enter to continue...")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print(f"\n\n{Colors.WARNING}Test interrupted by user{Colors.ENDC}")
        sys.exit(0)
