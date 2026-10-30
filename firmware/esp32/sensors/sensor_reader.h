#pragma once

/**
 * @file sensor_reader.h
 * @brief ESP32 Sensor Layer Reader for SCOUT01 (TRD §8, §11, §26).
 */

struct SensorReadings {
    float distanceCm;
    float temperatureC;
    float gasRaw;
};

class SensorReader {
public:
    void init();
    SensorReadings readAll();
};
