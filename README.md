# scale-load-balancer-custom

Built by [VisionQuantech](https://visionquantech.com) — AI agents, apps & tools for small businesses.

A real reverse-proxy load balancer: forwards live HTTP traffic to a backend
pool with round-robin or least-connections scheduling, active health checks,
passive failure ejection, and a `/stats` endpoint.

## Run

```bash
pip install -r requirements.txt
BACKENDS="127.0.0.1:9001,127.0.0.1:9002" python app.py --port 8000
python app.py --port 8000 --backends 127.0.0.1:9001,127.0.0.1:9002 \
    --algo least-conn --health-interval 5 --health-path /healthz
```

Every path except `/stats` is proxied to the chosen backend (method, query
string, headers, and body are all forwarded; responses stream back with
their real status codes).

## Behavior

- **Algorithms**: `round-robin` (default) or `least-conn`.
- **Health checks**: `GET <backend><health-path>` every `--health-interval`
  seconds; failures eject the backend, recovery re-admits it. A failed live
  request also ejects that backend immediately (with one failover retry).
- **`GET /stats`**: algorithm plus per-backend health, request count,
  active connections, failures, last error.

## Environment note

`no_proxy`/`NO_PROXY` must be unset (or must not contain broken patterns)
because the bundled `httpx` crashes on the `*[::1]`-style entries some
sandboxes ship; outbound traffic keeps working via the regular `*_proxy`
variables. The LB itself never uses a proxy for backends or health checks.

## Test

```bash
pytest -q
```

Spins up two real backend servers and verifies round-robin distribution,
body/query forwarding, dead-backend ejection by the health checker,
`/stats` counters, and least-connections spreading under concurrency.