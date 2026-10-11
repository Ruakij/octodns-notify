import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, urlparse

import pytest

from octodns_technitium import TechnitiumSource


class FakeTechnitium:
    """Serves zones/records/get from `records`."""

    def __init__(self):
        self.records = {}  # zone -> list of Technitium records
        self.requests = []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                url = urlparse(self.path)
                q = {k: v[0] for k, v in parse_qs(url.query).items()}
                fake.requests.append((url.path, q, self.headers.get("Authorization")))
                if self.headers.get("Authorization") != "Bearer secret":
                    body = {"status": "invalid-token", "errorMessage": "Invalid token."}
                elif url.path == "/api/zones/records/get" and q.get("domain") in fake.records:
                    body = {"status": "ok", "response": {"records": fake.records[q["domain"]]}}
                else:
                    body = {"status": "error", "errorMessage": "No such zone was found."}
                raw = json.dumps(body).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def log_message(self, *args):
                pass

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_port}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def fake():
    f = FakeTechnitium()
    yield f
    f.close()


@pytest.fixture
def source(fake):
    return TechnitiumSource("technitium", fake.url, "secret")
