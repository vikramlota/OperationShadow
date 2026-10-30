#pragma once

/**
 * @file safety_layer.h
 * @brief ESP32 Low-Level Safety Controller (TRD §2, §8, §22, §26).
 * Enforces emergency stop, collision cutoff, and communication loss watchdog.
 */

class FirmwareSafetyLayer {
public:
    void init();
    bool isEmergencyStopTriggered();
    bool validateMotion(float forwardClearanceCm);
    void tickWatchdog();
    bool isCommsTimedOut(unsigned long timeoutMs);

private:
    unsigned long lastHeartbeatMs = 0;
};
