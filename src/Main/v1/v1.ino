/* HVAC monitor: deep-sleep test version, based on the supplied v1.ino.
 * Board: Adafruit Feather ESP32 V2; sensor: SHT45 on STEMMA QT.
 * USB serial: 115200 baud, New Line.
 * After power-on/reset: send config within 15 seconds to stay awake.
 * Missing SSID/server: stays awake automatically. Send run when configured.
 * Timer wake: connect (30-second timeout), sample/upload once, sleep 60 sec.
 * Config mode: no automatic samples; test sends one real measurement.
 * Existing hvac-config preferences and JSON fields are preserved.
 * Sleep duration is NOT the exact reporting interval. Failed samples are lost.
 * No automatic USB-power detection. Connect USB, press RESET, then config.
 * This firmware has not been compiled with the ESP32 toolchain here.
 */
#include <WiFi.h>
#include <Wire.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Adafruit_SHT4x.h>
#include <math.h>
#include <esp_sleep.h>
#include <driver/rtc_io.h>

// One SHT45 per board
Adafruit_SHT4x sht4;
bool sensorReady = false;

const uint32_t SLEEP_SECONDS = 60;
const unsigned long CONFIG_WINDOW_MS = 15000;
bool configMode = false;
bool bootWindow = false;
unsigned long windowStarted = 0;

// Explicit declarations also make function order unambiguous.
void sampleAndSend();
void connectWiFi();
void enterDeepSleep();

Preferences prefs;
String ssid, password, serverUrl;
String inputLine;
bool lineTooLong = false;
bool connecting = false;
unsigned long connectStarted = 0;

void showConfig() {
  Serial.println("--- Saved configuration ---");
  Serial.println(configMode ? "Mode: CONFIG (awake)" : "Mode: MONITOR");
  Serial.printf("Sleep duration: %lu seconds\n", (unsigned long)SLEEP_SECONDS);
  Serial.println("SSID: " + ssid);
  Serial.println(password.length() ? "Password: [set]" : "Password: [empty / open network]");
  Serial.println("Server: " + serverUrl);
  Serial.println(WiFi.status() == WL_CONNECTED
                     ? "Wi-Fi: connected"
                     : "Wi-Fi: disconnected");
}

void connectWiFi() {
  if (ssid.isEmpty()) {
    Serial.println("Set SSID first.");
    return;
  }

  WiFi.disconnect();
  WiFi.begin(ssid.c_str(), password.c_str());
  connectStarted = millis();
  connecting = true;
  Serial.println("Connecting... USB commands remain available.");
}

void handleCommand(const String& command) {
  int separator = command.indexOf('=');

  if (separator >= 0) {
    String key = command.substring(0, separator);
    String value = command.substring(separator + 1);
    // Preserve spaces and '=' characters inside values.

    bool valid = false;
    if (key == "ssid")
      valid = value.length() >= 1 && value.length() <= 32;
    else if (key == "password")
      valid = value.isEmpty() ||
          (value.length() >= 8 && value.length() <= 63);
    else if (key == "server") {
      IPAddress serverIP;
      valid = serverIP.fromString(value);

      if (valid) {
        // User enters only IPv4; store the complete HTTP endpoint.
        value = String("http://") + serverIP.toString() + ":8000/data";
      }
    }
    if (!valid) {
      Serial.println("Invalid setting. Use ssid, password, or server.");
      return;
    }

    // Empty strings return zero bytes even when saved; verify by readback.
    prefs.putString(key.c_str(), value);
    if (!prefs.isKey(key.c_str()) ||
        prefs.getString(key.c_str(), "") != value) {
      Serial.println("Save failed.");
      return;
    }

    if (key == "ssid") ssid = value;
    if (key == "password") password = value;
    if (key == "server") serverUrl = value;

    Serial.println("Saved: " + key);
    return;
  }

  if (command == "config") {
    configMode = true;
    bootWindow = false;
    Serial.println("CONFIG mode: staying awake. Type run to resume monitoring.");
  }
  else if (command == "run") {
    if (ssid.isEmpty() || serverUrl.isEmpty()) {
      Serial.println("Set SSID and server IPv4 before run.");
      return;
    }
    configMode = false;
    bootWindow = false;
    connectWiFi();
  }
  else if (command == "show") showConfig();
  else if (command == "connect") connectWiFi();
  else if (command == "test") sampleAndSend();
  else if (command == "help") {
    Serial.println("ssid=YOUR_WIFI_NAME");
    Serial.println("password=YOUR_WIFI_PASSWORD");
    Serial.println("server=COMPUTER_IP");
    Serial.println("show | connect | test | config | run | help");
  } else {
    Serial.println("Unknown command. Type help.");
  }
}

bool initSensor() {
  if (!sht4.begin()) {
    Serial.println("SHT45 not found. Will retry at next sample.");
    return false;
  }

  sht4.setPrecision(SHT4X_HIGH_PRECISION);
  sht4.setHeater(SHT4X_NO_HEATER);
  Serial.println("SHT45 ready.");
  return true;
}

float readBatteryVoltage() {
  // The onboard divider reduces battery voltage by half.
  const int sampleCount = 16;
  uint32_t totalMilliVolts = 0;

  for (int i = 0; i < sampleCount; i++) {
    totalMilliVolts += analogReadMilliVolts(A13);
    delay(2);
  }

  return (totalMilliVolts / float(sampleCount)) * 2.0f / 1000.0f;
}

void sampleAndSend() {
  // Retry initialization if the sensor was previously unavailable.
  if (!sensorReady) {
    sensorReady = initSensor();
    if (!sensorReady) return;
  }

  sensors_event_t humidity, temperature;

  if (!sht4.getEvent(&humidity, &temperature)) {
    Serial.println("Sensor read failed. Sample skipped.");
    sensorReady = false;
    return;
  }

  float tempC = temperature.temperature;
  float rh = humidity.relative_humidity;
  float batteryV = readBatteryVoltage();

  Serial.print("Battery voltage: ");
  Serial.print(batteryV, 3);
  Serial.println(" V");

  if (!isfinite(tempC) || !isfinite(rh)) {
    Serial.println("Invalid sensor values. Sample skipped.");
    return;
  }

  // Print measurements even when Wi-Fi is unavailable.
  Serial.printf("Temperature: %.2f C | Humidity: %.2f %%RH\n",
                tempC, rh);

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Upload skipped: Wi-Fi disconnected.");
    return;
  }

  if (serverUrl.isEmpty()) {
    Serial.println("Upload skipped: set server IPv4 first.");
    return;
  }

  // Automatic source identification; no manual device ID configuration.
  String payload = "{\"device_id\":\"";
  payload += WiFi.macAddress();
  payload += "\",\"temperature_c\":";
  payload += String(tempC, 2);
  payload += ",\"humidity_rh\":";
  payload += String(rh, 2);
  payload += ",\"battery_v\":";
  payload += String(batteryV, 3);
  payload += "}";

  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(5000);

  if (!http.begin(client, serverUrl)) {
    Serial.println("Upload failed: invalid server URL.");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  int status = http.POST(payload);

  if (status == 200) {
    Serial.println("Measurement uploaded: HTTP 200");
  } else if (status > 0) {
    Serial.printf("Upload rejected: HTTP %d\n", status);
    Serial.println(http.getString());
  } else {
    Serial.println("Upload failed: " + HTTPClient::errorToString(status));
  }

  http.end();
}

void enterDeepSleep() {
  esp_err_t result = esp_sleep_enable_timer_wakeup(
      uint64_t(SLEEP_SECONDS) * 1000000ULL);
  if (result != ESP_OK) {
    configMode = true;
    Serial.println("Sleep timer setup failed. Staying awake for configuration.");
    return;
  }

  WiFi.disconnect(true);
  WiFi.mode(WIFI_OFF);
  Wire.end();
  sensorReady = false;

  // GPIO2 controls QT/NeoPixel power. RTC hold keeps it LOW during sleep.
  const gpio_num_t powerPin = (gpio_num_t)NEOPIXEL_I2C_POWER;
  rtc_gpio_init(powerPin);
  rtc_gpio_set_direction(powerPin, RTC_GPIO_MODE_OUTPUT_ONLY);
  rtc_gpio_set_level(powerPin, 0);
  rtc_gpio_hold_en(powerPin);

  prefs.end();
  Serial.printf("Sleeping for %lu seconds.\n", (unsigned long)SLEEP_SECONDS);
  Serial.flush();
  esp_deep_sleep_start();
}

void setup() {
  // Release the RTC hold before restoring normal GPIO control after wake.
  const gpio_num_t powerPin = (gpio_num_t)NEOPIXEL_I2C_POWER;
  rtc_gpio_hold_dis(powerPin);
  rtc_gpio_deinit(powerPin);

  bool timerWake = esp_sleep_get_wakeup_cause() == ESP_SLEEP_WAKEUP_TIMER;
  Serial.begin(115200);
  delay(timerWake ? 100 : 1500);
  Serial.println(timerWake ? "\nWake: TIMER" : "\nWake: POWER/RESET/OTHER");

  pinMode(NEOPIXEL_I2C_POWER, OUTPUT);
  digitalWrite(NEOPIXEL_I2C_POWER, HIGH);
  delay(10);
  sensorReady = initSensor();

  if (!prefs.begin("hvac-config", false)) {
    Serial.println("Cannot open configuration storage. Reset after inspection.");
    // Avoid leaving a battery-powered device indefinitely awake on this fault.
    enterDeepSleep();
    while (true) delay(1000);
  }

  ssid = prefs.getString("ssid", "");
  password = prefs.getString("password", "");
  serverUrl = prefs.getString("server", "");

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  configMode = ssid.isEmpty() || serverUrl.isEmpty();
  bootWindow = !timerWake && !configMode;
  windowStarted = millis();
  showConfig();

  if (configMode) {
    Serial.println("Missing configuration. Staying awake; type help, then run.");
  } else if (bootWindow) {
    Serial.println("Send config within 15 seconds to stay awake, or run now.");
  }

  if (!ssid.isEmpty()) connectWiFi();
}

void loop() {
  while (Serial.available()) {
    char c = Serial.read();

    if (c == '\r') continue;
    if (c == '\n') {
      if (lineTooLong) Serial.println("Command too long; discarded.");
      else if (!inputLine.isEmpty()) handleCommand(inputLine);

      inputLine = "";
      lineTooLong = false;
    } else if (!lineTooLong) {
      if (inputLine.length() < 256) inputLine += c;
      else {
        inputLine = "";
        lineTooLong = true;
      }
    }
  }

  if (connecting) {
    if (WiFi.status() == WL_CONNECTED) {
      connecting = false;
      Serial.print("Wi-Fi connected! ESP32 IP: ");
      Serial.println(WiFi.localIP());
    } else if (millis() - connectStarted >= 30000) {
      connecting = false;
      Serial.println("Connection timed out. Check settings, then type connect.");
    }
  }

  if (bootWindow && millis() - windowStarted >= CONFIG_WINDOW_MS) {
    bootWindow = false;
    Serial.println("Configuration window ended. MONITOR mode.");
  }

  // Once connection succeeds or times out, attempt one sample, then sleep.
  // All sample/upload failure paths return here, so they also lead to sleep.
  if (!configMode && !bootWindow && !connecting) {
    sampleAndSend();
    enterDeepSleep();
  }

  delay(5);
}