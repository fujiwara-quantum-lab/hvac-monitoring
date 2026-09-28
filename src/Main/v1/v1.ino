#include <WiFi.h>
#include <HTTPClient.h>
#include <Preferences.h>
#include <Adafruit_SHT4x.h>
#include <math.h>

// One SHT45 per board
Adafruit_SHT4x sht4;
bool sensorReady = false;

// Use millis() scheduling so USB commands remain available between samples.
const unsigned long SAMPLE_INTERVAL_MS = 10000;
unsigned long lastSampleAt = 0;

Preferences prefs;
String ssid, password, serverUrl;
String inputLine;
bool lineTooLong = false;
bool connecting = false;
unsigned long connectStarted = 0;

void showConfig() {
  Serial.println("--- Saved configuration ---");
  Serial.println("SSID: " + ssid);
  Serial.println(password.length() ? "Password: [set]" : "Password: [not set]");
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

void testServer() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Connect Wi-Fi first.");
    return;
  }
  if (serverUrl.isEmpty()) {
    Serial.println("Set server URL first.");
    return;
  }

  WiFiClient client;
  HTTPClient http;
  http.setConnectTimeout(5000);
  http.setTimeout(5000);

  if (!http.begin(client, serverUrl)) {
    Serial.println("Invalid server URL.");
    return;
  }

  http.addHeader("Content-Type", "application/json");
  int status = http.POST(
      "{\"test\":true,\"message\":\"USB configuration test\"}");

  if (status > 0) {
    Serial.printf("HTTP status: %d\n", status);
    Serial.println("Server response: " + http.getString());
  } else {
    Serial.println("Send failed: " + HTTPClient::errorToString(status));
  }
  http.end();
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
      valid = value.length() >= 8 && value.length() <= 63;
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

    if (prefs.putString(key.c_str(), value) == 0) {
      Serial.println("Save failed.");
      return;
    }

    if (key == "ssid") ssid = value;
    if (key == "password") password = value;
    if (key == "server") serverUrl = value;

    Serial.println("Saved: " + key);
    return;
  }

  if (command == "show") showConfig();
  else if (command == "connect") connectWiFi();
  else if (command == "test") testServer();
  else if (command == "help") {
    Serial.println("ssid=YOUR_WIFI_NAME");
    Serial.println("password=YOUR_WIFI_PASSWORD");
    Serial.println("server=COMPUTER_IP");
    Serial.println("show | connect | test | help");
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
  payload += "\",\"test\":false,\"temperature_c\":";
  payload += String(tempC, 2);
  payload += ",\"humidity_rh\":";
  payload += String(rh, 2);
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

void setup() {
  Serial.begin(115200);
  delay(1500);

  // Enable STEMMA QT power on Feather ESP32 V2.
  pinMode(NEOPIXEL_I2C_POWER, OUTPUT);
  digitalWrite(NEOPIXEL_I2C_POWER, HIGH);
  delay(10);

sensorReady = initSensor();

  if (!prefs.begin("hvac-config", false)) {
    Serial.println("Cannot open configuration storage.");
    while (true) delay(1000);
  }

  ssid = prefs.getString("ssid", "");
  password = prefs.getString("password", "");
  serverUrl = prefs.getString("server", "");

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);

  Serial.println("\nUSB configuration ready. Type help.");
  showConfig();

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
  
  // Take one sample approximately every 10 seconds.
  // Network timeouts may delay the next sample.
  if (millis() - lastSampleAt >= SAMPLE_INTERVAL_MS) {
    lastSampleAt = millis();
    sampleAndSend();
  }

  delay(5);
}