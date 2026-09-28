#include <WiFi.h>
#include "arduino_secrets.h"

void setup() {
  Serial.begin(115200);
  delay(1500);

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASSWORD);

  Serial.print("Connecting");

  unsigned long start = millis();

  // Try for up to 30 seconds
  while (WiFi.status() != WL_CONNECTED &&
         millis() - start < 30000) {
    delay(500);
    Serial.print(".");
  }

  if (WiFi.status() == WL_CONNECTED) {
    Serial.println("\nWi-Fi connected!");
    Serial.print("ESP32 IP: ");
    Serial.println(WiFi.localIP());
    Serial.print("Signal strength (dBm): ");
    Serial.println(WiFi.RSSI());
  } else {
    Serial.println("\nConnection timed out.");
    Serial.println("Check credentials, then press RESET to retry.");
  }
}

void loop() {
  delay(1000);
}
