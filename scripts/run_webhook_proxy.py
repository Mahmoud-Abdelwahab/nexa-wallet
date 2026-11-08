"""Local development gateway: expose only POST /webhooks/moyasar to a tunnel.

Run: .venv/bin/python scripts/run_webhook_proxy.py
Then: cloudflared tunnel --url http://127.0.0.1:8001
Keep the real API running on port 8000. This gateway does not bypass its secret check.
"""

from http.client import HTTPConnection, HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


WEBHOOK_PATH = "/webhooks/moyasar"
MAX_BODY_BYTES = 1024 * 1024


class WebhookProxy(BaseHTTPRequestHandler):
    def setup(self):
        super().setup()
        self.connection.settimeout(15)

    def reply(self, status, body):
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        # Exact match only: no other API path can reach the upstream server.
        if self.path != WEBHOOK_PATH:
            self.reply(404, b'{"detail":"Not found"}')
            return
        lengths = self.headers.get_all("Content-Length", [])
        if self.headers.get("Transfer-Encoding") or len(lengths) != 1:
            self.reply(400, b'{"detail":"One Content-Length header is required"}')
            return
        try:
            size = int(lengths[0])
        except ValueError:
            self.reply(400, b'{"detail":"Invalid Content-Length"}')
            return
        if not 0 < size <= MAX_BODY_BYTES:
            self.reply(413, b'{"detail":"Invalid body size"}')
            return

        upstream = HTTPConnection("127.0.0.1", 8000, timeout=15)
        try:
            body = self.rfile.read(size)
            if len(body) != size:
                self.reply(400, b'{"detail":"Incomplete body"}')
                return
            # Fixed destination; never forward user-supplied hosts or redirects.
            upstream.request("POST", WEBHOOK_PATH, body=body,
                             headers={"Content-Type": "application/json"})
            response = upstream.getresponse()
            self.reply(response.status, response.read())
        except (OSError, HTTPException):
            self.reply(502, b'{"detail":"Local webhook unavailable"}')
        finally:
            upstream.close()

    def log_message(self, format, *args):
        # Do not log request bodies, secrets, or arbitrary client-supplied paths.
        pass


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", 8001), WebhookProxy)
    print("Webhook-only gateway: http://127.0.0.1:8001/webhooks/moyasar", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
