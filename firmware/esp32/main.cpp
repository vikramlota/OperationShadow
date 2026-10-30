/**
 * @file main.cpp
 * @brief Operation Shadow — ESP32 SCOUT01 Drivetrain & Sensor Firmware Entry
 * Architecture: TRD v2.0 §1, §8, §26
 */

#include "motors/motor_controller.h"
#include "sensors/sensor_reader.h"
#include "safety/safety_layer.h"

MotorController motors;
SensorReader sensors;
FirmwareSafetyLayer safety;

void setup() {
    motors.init();
    sensors.init();
    safety.init();
}

void loop() {
    safety.tickWatchdog();
    SensorReadings readings = sensors.readAll();

    if (safety.isEmergencyStopTriggered() || !safety.validateMotion(readings.distanceCm)) {
        motors.emergencyStop();
    }
}
