"""scale-load-balancer-custom - a real reverse-proxy load balancer.

Forwards real HTTP traffic to a pool of backends with:
  * round-robin (default) or least-connections (--algo least-conn)
  * active health checks (GET <backend>/healthz) + passive ejection
  * /stats endpoint: per-backend health, request + connection counters

Usage:
    BACKENDS="127.0.0.1:9001,127.0.0.1:9002" python app.py --port 8000
    python app.py --port 8000 --backends 127.0.0.1:9001,127.0.0.1:9002 \\
        --algo least-conn --health-interval 5 --health-path /healthz
"""
import argparse
import asyncio
import os
import threading

import httpx
from fastapi import FastAPI, Request, Response

from lb import Pool

HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate",
               "proxy-authorization", "te", "trailer", "transfer-encoding",
               "upgrade", "content-length"}

pool: Pool | None = None
_client: httpx.AsyncClient | None = None


def create_app(p: Pool) -> FastAPI:
    global pool
    pool = p
    stop = threading.Event()
    app = FastAPI(title="scale-load-balancer-custom")

    @app.on_event("startup")
    async def _startup():
        global _client
        # NOTE: no_proxy/NO_PROXY must be unset (see README); keep *_proxy.
        _client = httpx.AsyncClient(timeout=15.0, trust_env=False)
        t = threading.Thread(target=pool.health_loop, args=(stop,), daemon=True)
        t.start()

    @app.on_event("shutdown")
    async def _shutdown():
        stop.set()
        if _client:
            await _client.aclose()

    @app.get("/stats")
    async def stats():
        return pool.stats()

    @app.api_route("/{path:path}", methods=["GET", "POST", "PUT", "DELETE",
                                            "PATCH", "HEAD", "OPTIONS"])
    async def proxy(path: str, request: Request):
        backend = pool.pick()
        if backend is None:
            return Response("no healthy backends", status_code=503)
        url = f"http://{backend.addr}/{path}"
        if request.url.query:
            url += f"?{request.url.query}"
        headers = {k: v for k, v in request.headers.items()
                   if k.lower() not in HOP_HEADERS}
        body = await request.body()
        pool.acquire(backend)
        try:
            resp = await _client.request(request.method, url,
                                         headers=headers,
                                         content=body if body else None)
            out_headers = {k: v for k, v in resp.headers.items()
                           if k.lower() not in HOP_HEADERS}
            pool.release(backend, True)
            return Response(content=resp.content, status_code=resp.status_code,
                            headers=out_headers,
                            media_type=resp.headers.get("content-type"))
        except (httpx.HTTPError, asyncio.TimeoutError) as exc:
            pool.release(backend, False, str(exc))
            # try the next backend once (failover), else 502
            nxt = pool.pick()
            if nxt is not None and nxt is not backend:
                return await proxy(path, request)
            return Response(f"bad gateway: {exc}", status_code=502)

    return app


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--backends", default=os.environ.get("BACKENDS", ""),
                    help="comma-separated host:port (or BACKENDS env)")
    ap.add_argument("--algo", default="round-robin",
                    choices=["round-robin", "least-conn"])
    ap.add_argument("--health-interval", type=float, default=5.0)
    ap.add_argument("--health-path", default="/healthz")
    args = ap.parse_args()
    addrs = [a.strip() for a in args.backends.split(",") if a.strip()]
    if not addrs:
        raise SystemExit("give --backends host:port,... or set BACKENDS")
    import uvicorn
    p = Pool(addrs, algo=args.algo, health_path=args.health_path,
             health_interval=args.health_interval)
    print(f"LB on :{args.port} algo={args.algo} backends={addrs}", flush=True)
    uvicorn.run(create_app(p), host="127.0.0.1", port=args.port, log_level="warning")


if __name__ == "__main__":
    main()
