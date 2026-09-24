"""Core load-balancer logic: backend pool, pickers, health checking.

Kept framework-free so it can be unit-tested directly; app.py wraps it
in a FastAPI reverse proxy.
"""
import asyncio
import threading
import time
import urllib.request

# Health checks must never go through an egress proxy: localhost backends.
_NO_PROXY_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


class Backend:
    def __init__(self, addr: str):
        self.addr = addr  # "host:port"
        self.healthy = True
        self.requests = 0
        self.failures = 0
        self.active = 0
        self.last_error = ""
        self._lock = threading.Lock()

    def snapshot(self):
        with self._lock:
            return {"addr": self.addr, "healthy": self.healthy,
                    "requests": self.requests, "failures": self.failures,
                    "active_connections": self.active,
                    "last_error": self.last_error}


class Pool:
    """Thread-safe backend pool with round-robin and least-connections pickers."""

    def __init__(self, addrs, algo="round-robin", health_path="/healthz",
                 health_interval=5.0):
        if algo not in ("round-robin", "least-conn"):
            raise ValueError("algo must be round-robin or least-conn")
        if not addrs:
            raise ValueError("need at least one backend")
        self.backends = [Backend(a) for a in addrs]
        self.algo = algo
        self.health_path = health_path
        self.health_interval = health_interval
        self._rr = 0
        self._lock = threading.Lock()

    def healthy_backends(self):
        return [b for b in self.backends if b.healthy]

    def pick(self) -> Backend | None:
        with self._lock:
            cands = self.healthy_backends()
            if not cands:
                return None
            if self.algo == "least-conn":
                return min(cands, key=lambda b: (b.active, b.requests))
            b = cands[self._rr % len(cands)]
            self._rr += 1
            return b

    def acquire(self, b: Backend):
        with b._lock:
            b.active += 1
            b.requests += 1

    def release(self, b: Backend, ok: bool, err: str = ""):
        with b._lock:
            b.active = max(0, b.active - 1)
            if not ok:
                b.failures += 1
                b.last_error = err
                b.healthy = False  # passive ejection on real failure

    def check_once(self):
        """One active health-check pass (also run in a background loop)."""
        for b in self.backends:
            try:
                req = urllib.request.Request(
                    f"http://{b.addr}{self.health_path}", method="GET")
                with _NO_PROXY_OPENER.open(req, timeout=3) as r:
                    ok, err = (200 <= r.status < 300), ""
            except Exception as exc:  # noqa: BLE001 - any failure = unhealthy
                ok, err = False, f"health-check: {exc}"
            with b._lock:
                b.healthy = ok
                b.last_error = err

    def health_loop(self, stop: threading.Event):
        while not stop.wait(self.health_interval):
            self.check_once()

    def stats(self):
        return {"algorithm": self.algo,
                "backends": [b.snapshot() for b in self.backends]}
