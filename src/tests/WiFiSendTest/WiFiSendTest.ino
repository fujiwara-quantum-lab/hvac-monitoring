#include <WiFi.h>
#include <HTTPClient.h>
#include "arduino_secrets.h"

// Computer's address, not the ESP32's address
const char* SERVER_URL = "http://172.16.29.19:8000/data";

void setup() {
  Serial.begin(115200);
  delay(1500);

  WiFi.mode(WIFI_STA);
  WiFi.setAutoReconnect(true);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("Connecting");
  unsigned long start = millis();

  while (WiFi.status() != WL_CONNECTED &&
         millis() - start < 30000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWi-Fi connected!");
    Serial.print("ESP32 IP: ");
    Serial.println(WiFi.localIP());
  } else {
    Serial.println("\nConnection timed out. Press RESET to retry.");
  }
}

void loop() {
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("Wi-Fi disconnected. Waiting...");
    delay(5000);
    return;
  }

  WiFiClient client;
  HTTPClient http;

  http.setConnectTimeout(5000);
  http.setTimeout(5000);

  if (!http.begin(client, SERVER_URL)) {
    Serial.println("Could not initialize HTTP request.");
    delay(10000);
    return;
  }

  http.addHeader("Content-Type", "application/json");

  // Fixed test values, not actual sensor readings
  String payload =
      "{\"node_id\":\"sensor_01\",\"test\":true,"
      "\"temperature_c\":23.45,\"humidity_rh\":42.10}";

  Serial.print("Sending: ");
  Serial.println(payload);

  int status = http.POST(payload);

  if (status > 0) {
    Serial.print("HTTP status: ");
    Serial.println(status);
    Serial.print("Server response: ");
    Serial.println(http.getString());
  } else {
    Serial.print("Send failed: ");
    Serial.println(HTTPClient::errorToString(status));
  }

  http.end();
  delay(10000);
}