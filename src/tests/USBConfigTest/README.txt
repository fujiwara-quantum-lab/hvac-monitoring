This code aims to test:
1. User configs WIFI connection (including WIFI name, password and computer's IP address) on Serial Monitor when the board is wired.
2. The WIFI information is stored in the board even if the board is power off.

Guide to config:
1. Open Arduino IDE and connect the board. Open Serial Monitor and set the baud rate to 115200 and message type to "New line". Message to config the board.
2. Go to repository -> receiver -> server and open it. This will test if your computer receives the WIFI message from the board.
3. To find your computer's IP address, Search "PowerShell" and open it. Type "ipconfig" and you can see your IP address under "Wireless LAN adapter Wi-Fi:" -> "IPv4 Address"

User Messages:
ssid="WIFI name"			----set WIFI name
password="WIFI password"		----set WIFI password
server="computer's IPv4"		----set computer's IP address
show					----show setting information, password would not show
connect					----connect WIFI
help					----show available messages and descriptions
test					----test if the connection is successful

Common successful procedure:
1.use commands: ssid, password, server
2.Type "connect" and see "Connecting... USB commands remain available." "Wi-Fi connected! ESP32 IP: (board's IP address)"
3.Type "test" and see "HTTP status: 200" "Server response: OK".
4.Check server.py file opened in "repository -> receiver", there's a message "{'test': True, 'message': 'USB configuration test'}"
5.The test is successful.

Errors and possible solutions:
Cannot open configuration					----Press reset button on the board
Invalid setting. Use ssid, password, or server			----Check spelling and format, make sure there's no space between characters
Save failed							----Press reset button on the board
Unknown command. Type help					----Type help to see available commands
Command too long; discarded					----Make sure the message is set as "new line" in serial monitor
set SSID first							----set WIFI name using "ssid" command
Connection timed out. Check settings, then type again		----use "show" command to check setting and type information again
connect Wi-Fi first						----type "connect" before "test" command
Set server URL first						----set computer's IP address using "server" command
Invalid server URL						----type IP address again, make sure there's no space between characters