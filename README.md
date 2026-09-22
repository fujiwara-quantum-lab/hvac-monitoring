# hvac-monitoring
hardware and software documentation and code for the HVAC monitoring system


Adafruit has a guide for the chips used in this project. The PDFs have been downloaded to /docs
https://cdn-learn.adafruit.com/downloads/pdf/adafruit-esp32-feather-v2.pdf
https://cdn-learn.adafruit.com/downloads/pdf/adafruit-sht40-temperature-humidity-sensor.pdf

https://learn.adafruit.com/adafruit-sht40-temperature-humidity-sensor/overview

## Programming
Installed the Arduino IDE 2.3.10

Following driver instructions on the adafruit guide. (This is specific to the feather 2.0)

installation of the driverse takes a few minutes

Uploadon 921600 speed threw an error

Sketch uses 308596 bytes (9%) of program storage space. Maximum is 3342336 bytes.
Global variables use 23024 bytes (7%) of dynamic memory, leaving 304656 bytes for local variables. Maximum is 327680 bytes.
esptool v5.2.0
Serial port COM5:
Connecting......
Connected to ESP32 on COM5:
Chip type:          ESP32-PICO-V3-02 (revision v3.1)
Features:           Wi-Fi, BT, Dual Core + LP Core, 240MHz, Embedded Flash, Embedded PSRAM, Vref calibration in eFuse, Coding Scheme None
Crystal frequency:  40MHz
MAC:                14:33:5c:99:26:4c

Uploading stub flasher...
Running stub flasher...
Stub flasher running.
Changing baud rate to 921600...
Changed.

Configuring flash size...
Flash will be erased from 0x00001000 to 0x00006fff...
Compressed 23520 bytes to 15182...

Writing at 0x00001000 [                              ]   0.0% 0/15182 bytes... 

Writing at 0x00006be0 [==============================] 100.0% 15182/15182 bytes... 
Wrote 23520 bytes (15182 compressed) at 0x00001000 in 0.5 seconds (392.0 kbit/s).
Verifying written data...

A fatal error occurred: MD5 of file does not match data in flash!Input MD5: 38cd6b93df1e8b412f6e343894af47c6
Flash MD5: 327abce81da99286c02770ee06764465

Hard resetting via RTS pin...

Failed uploading: uploading error: exit status 2

