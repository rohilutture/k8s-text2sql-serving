import socket
import threading
import time

import httpx
import pytest
import uvicorn
from fastapi import FastAPI, HTTPException


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Server:
    """Run an ASGI app on a real socket in a background thread (so concurrency and lifespan behave as in production)."""

    def __init__(self, app):
        self.port = free_port()
        self.server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread = threading.Thread(target=self.server.run, daemon=True)

    @property
    def url(self):
        return f"http://127.0.0.1:{self.port}"

    def __enter__(self):
        self.thread.start()
        for _ in range(200):
            if self.server.started:
                return self
            time.sleep(0.05)
        raise RuntimeError("server did not start")

    def __exit__(self, *a):
        self.server.should_exit = True
        self.thread.join(timeout=5)


@pytest.fixture
def serve():
    started = []

    def _serve(app):
        srv = Server(app).__enter__()
        started.append(srv)
        return srv

    yield _serve
    for s in started:
        s.__exit__()


def wait_ready(url, timeout=10):
    end = time.time() + timeout
    while time.time() < end:
        try:
            if httpx.get(url + "/readyz", timeout=1).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.05)
    raise RuntimeError("never became ready")


def fake_vllm(mode_box: dict) -> FastAPI:
    """A stand-in OpenAI-compatible server. mode_box['mode'] in ok | error | garbage | slow | unhealthy."""
    app = FastAPI()

    @app.get("/health")
    async def health():
        if mode_box["mode"] == "unhealthy":
            raise HTTPException(503)
        return {}

    @app.post("/v1/chat/completions")
    async def chat(body: dict):
        mode_box["last_body"] = body
        mode = mode_box["mode"]
        if mode == "error":
            raise HTTPException(500)
        if mode == "garbage":
            return {"nope": 1}
        if mode == "slow":
            import asyncio

            await asyncio.sleep(3)
        return {"choices": [{"message": {"content": "Here you go:\n```sql\nSELECT COUNT(*) FROM singer;\n```"}}]}

    return app
