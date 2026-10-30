#pragma once

/**
 * @file motor_controller.h
 * @brief ESP32 Motor Controller Interface for SCOUT01 Drivetrain (TRD §8, §26).
 */

class MotorController {
public:
    void init();
    void setSpeeds(float leftSpeed, float rightSpeed);
    void emergencyStop();

private:
    float currentLeftSpeed = 0.0f;
    float currentRightSpeed = 0.0f;
};
