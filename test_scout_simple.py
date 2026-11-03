#!/usr/bin/env python3
import serial
import time
import sys

def test_scout():
    port = "/dev/cu.usbserial-5C3D0844351"
    baud = 115200
    
    print(f"Connecting to ESP32 on {port} at {baud} baud...")
    
    try:
        ser = serial.Serial(port, baud, timeout=1)
        time.sleep(2)
        print("Connected!\n")
    except serial.SerialException as e:
        print(f"Failed to connect: {e}")
        return False
    
    # Test sequence
    commands = [
        ("W", 1.0, "FORWARD for 1 second"),
        ("X", 0.5, "STOP"),
        ("A", 0.5, "TURN LEFT for 0.5 seconds"),
        ("X", 0.5, "STOP"),
        ("S", 1.0, "BACKWARD for 1 second"),
        ("X", 0.5, "STOP"),
        ("D", 0.5, "TURN RIGHT for 0.5 seconds"),
        ("X", 0.5, "STOP"),
    ]
    
    try:
        for cmd, duration, description in commands:
            print(f"\n{description}")
            print(f"  Sending: '{cmd}' (0x{ord(cmd):02x})")
            
            ser.write(cmd.encode())
            print(f"  Sent to ESP32")
            
            for i in range(int(duration * 10)):
                time.sleep(0.1)
                print(".", end="", flush=True)
            print()
        
        print("\n" + "="*50)
        print("TEST COMPLETE - Scout executed all commands!")
        print("="*50)
        return True
    
    except Exception as e:
        print(f"\nError during test: {e}")
        return False
    
    finally:
        ser.close()
        print("Connection closed")

if __name__ == "__main__":
    success = test_scout()
    sys.exit(0 if success else 1)
