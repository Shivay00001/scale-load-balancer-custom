"""Real verification with two live dummy backends (separate HTTP servers).

  * round-robin distributes requests A,B,A,B...
  * killing B -> health check ejects it -> all traffic goes to A
  * /stats reports real counters
  * least-conn picker unit-tested + concurrent slow-request integration test
"""
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest
from fastapi.testclient import TestClient

from app import create_app
from lb import Backend, Pool

PORT_A = 18701
PORT_B = 18702


class Quiet(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass


def make_backend(tag, slow=False):
    class B(Quiet):
        def _ok(self, payload: bytes, ctype="application/json"):
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("X-Backend-Id", tag)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            if self.path == "/healthz":
                return self._ok(b'{"ok":true}')
            if slow and self.path == "/slow":
                time.sleep(0.8)
            body = json.dumps({"backend": tag, "path": self.path}).encode()
            self._ok(body)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0) or 0)
            data = self.rfile.read(n) if n else b""
            body = json.dumps({"backend": tag, "echo": data.decode()}).encode()
            self._ok(body)
    return B


def serve(port, handler):
    srv = ThreadingHTTPServer(("127.0.0.1", port), handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture()
def two_backends():
    a = serve(PORT_A, make_backend("A"))
    b = serve(PORT_B, make_backend("B"))
    time.sleep(0.3)
    yield a, b
    for s in (a, b):
        s.shutdown()
        s.server_close()


def test_round_robin_distributes(two_backends):
    with TestClient(create_app(Pool([f"127.0.0.1:{PORT_A}",
                                     f"127.0.0.1:{PORT_B}"],
                                    health_interval=60))) as c:
        seen = [c.get("/ping").json()["backend"] for _ in range(6)]
    assert seen == ["A", "B", "A", "B", "A", "B"], seen


def test_post_body_and_query_forwarded(two_backends):
    with TestClient(create_app(Pool([f"127.0.0.1:{PORT_A}"],
                                    health_interval=60))) as c:
        r = c.post("/echo?x=1", content=b"hello-backend")
    assert r.status_code == 200
    assert r.json() == {"backend": "A", "echo": "hello-backend"}
    assert r.headers["x-backend-id"] == "A"


def test_dead_backend_ejected(two_backends):
    a, b = two_backends
    with TestClient(create_app(Pool([f"127.0.0.1:{PORT_A}",
                                     f"127.0.0.1:{PORT_B}"],
                                    health_interval=0.5))) as c:
        assert c.get("/ping").json()["backend"] in ("A", "B")
        b.shutdown()
        b.server_close()  # really kill B's socket
        time.sleep(1.5)   # let the 0.5s health check notice
        stats = c.get("/stats").json()
        health = {x["addr"]: x["healthy"] for x in stats["backends"]}
        assert health[f"127.0.0.1:{PORT_B}"] is False
        seen = {c.get("/ping").json()["backend"] for _ in range(4)}
        assert seen == {"A"}, seen


def test_stats_counters(two_backends):
    with TestClient(create_app(Pool([f"127.0.0.1:{PORT_A}",
                                     f"127.0.0.1:{PORT_B}"],
                                    health_interval=60))) as c:
        for _ in range(4):
            c.get("/ping")
        s = c.get("/stats").json()
    assert s["algorithm"] == "round-robin"
    counts = {x["addr"]: x["requests"] for x in s["backends"]}
    assert counts == {f"127.0.0.1:{PORT_A}": 2, f"127.0.0.1:{PORT_B}": 2}


def test_least_conn_picker_unit():
    p = Pool(["h1:1", "h2:2"], algo="least-conn")
    a, b = p.backends
    with a._lock:
        a.active = 5
    with b._lock:
        b.active = 1
    assert p.pick() is b
    with a._lock:
        a.active = 0
    with b._lock:
        b.active = 0
        b.healthy = False
    assert p.pick() is a  # unhealthy B skipped


def test_least_conn_spreads_concurrent_slow():
    a = serve(PORT_A, make_backend("A", slow=True))
    b = serve(PORT_B, make_backend("B", slow=True))
    time.sleep(0.3)
    try:
        with TestClient(create_app(Pool([f"127.0.0.1:{PORT_A}",
                                         f"127.0.0.1:{PORT_B}"],
                                        algo="least-conn",
                                        health_interval=60))) as c:
            results = []
            def hit():
                results.append(c.get("/slow").json()["backend"])
            ts = [threading.Thread(target=hit) for _ in range(4)]
            [t.start() for t in ts]
            [t.join() for t in ts]
            s = c.get("/stats").json()
        counts = sorted(x["requests"] for x in s["backends"])
        assert counts == [2, 2], counts  # spread, not piled on one
        assert sorted(results) == ["A", "A", "B", "B"]
    finally:
        for s in (a, b):
            s.shutdown()
            s.server_close()
