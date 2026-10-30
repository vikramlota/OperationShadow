from typing import Dict, Any


class ESP32SerialBridge:
    """
    Serial UART bridge from Raspberry Pi to ESP32 motor controller (TRD §1, §8, §19).
    Dispatches low-level motor commands and reads hardware sensor ADC values.
    """

    def __init__(self, port: str = "/dev/ttyUSB0", baudrate: int = 115200) -> None:
        self.port = port
        self.baudrate = baudrate
        self.connected = False

    def send_motor_command(self, left_speed: float, right_speed: float) -> str:
        """Formats deterministic motor frame for ESP32 safety layer."""
        return f"M,{left_speed:.2f},{right_speed:.2f}\n"

    def parse_sensor_frame(self, line: str) -> Dict[str, Any]:
        """Parses CSV line from ESP32: distance,temp,gas."""
        parts = line.strip().split(",")
        if len(parts) >= 3:
            return {
                "distance_cm": float(parts[0]),
                "temp_c": float(parts[1]),
                "gas_raw": float(parts[2])
            }
        return {"distance_cm": 999.0, "temp_c": 25.0, "gas_raw": 0.0}
