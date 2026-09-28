import json
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class Receiver(BaseHTTPRequestHandler):
    def reply(self, status, text):
        body = text.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self.reply(200, "HVAC receiver is running")

    def do_POST(self):
        if self.path != "/data":
            self.reply(404, "Not found")
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 4096:
                self.reply(400, "Invalid message size")
                return

            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Expected a JSON object")
        except (ValueError, UnicodeDecodeError):
            self.reply(400, "Invalid JSON")
            return

        timestamp = datetime.now().isoformat(timespec="seconds")
        print(f"[{timestamp}] {self.client_address[0]}: {data}", flush=True)
        self.reply(200, "OK")


if __name__ == "__main__":
    server = ThreadingHTTPServer(("0.0.0.0", 8000), Receiver)
    print("Receiver started. Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()