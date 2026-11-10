"""Text-to-SQL inference gateway.

    POST /v1/sql     {"question": "...", "schema": "CREATE TABLE ..."}  ->  {"sql": "...", ...}
    GET  /healthz    liveness  (the process is up)
    GET  /readyz     readiness (warmed up, backend reachable, not shutting down)
    GET  /metrics    Prometheus text format

Backends (env BACKEND):
  mock  deterministic template "model" that burns MOCK_CPU_MS of real CPU per request. It lets the Kubernetes
        behaviour (probes, HPA, rollout, load shedding) be tested on any machine without a GPU. It is NOT a model.
  vllm  proxies to an OpenAI-compatible vLLM server at VLLM_URL (the real deployment; needs a GPU node).

Overload behaviour: at most MAX_INFLIGHT requests are processed at once; beyond that the gateway answers 503 with
Retry-After immediately instead of queueing without bound, so latency for admitted requests stays bounded and the
autoscaler / client retries can react.
"""
from __future__ import annotations

import asyncio
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

import httpx
from fastapi import FastAPI, HTTPException, Response
from prometheus_client import CONTENT_TYPE_LATEST, CollectorRegistry, Counter, Gauge, Histogram, generate_latest
from pydantic import BaseModel, Field


@dataclass
class Settings:
    backend: str = field(default_factory=lambda: os.getenv("BACKEND", "mock"))
    mock_cpu_ms: float = field(default_factory=lambda: float(os.getenv("MOCK_CPU_MS", "20")))
    warmup_seconds: float = field(default_factory=lambda: float(os.getenv("WARMUP_SECONDS", "2")))
    max_inflight: int = field(default_factory=lambda: int(os.getenv("MAX_INFLIGHT", "32")))
    vllm_url: str = field(default_factory=lambda: os.getenv("VLLM_URL", "http://vllm:8000"))
    model_name: str = field(default_factory=lambda: os.getenv("MODEL_NAME", "text-to-sql"))
    request_timeout: float = field(default_factory=lambda: float(os.getenv("REQUEST_TIMEOUT", "30")))
    max_tokens: int = field(default_factory=lambda: int(os.getenv("MAX_TOKENS", "256")))


class SqlRequest(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    schema_: str = Field(default="", alias="schema", max_length=20000)

    model_config = {"populate_by_name": True}


class SqlResponse(BaseModel):
    sql: str
    backend: str
    latency_ms: float


_SQL_FENCE = re.compile(r"```(?:sql)?\s*(.*?)```", re.S | re.I)


def extract_sql(text: str) -> str:
    m = _SQL_FENCE.search(text)
    return (m.group(1) if m else text).strip()


_cpu_clock = time.thread_time if sys.platform.startswith("linux") else time.perf_counter


def burn_cpu(ms: float) -> None:
    """Consume `ms` of CPU time on this thread. Using thread CPU time (not a wall-clock deadline) matters: with a
    wall-clock spin, concurrent requests would share a core yet each still 'finish' in `ms` of wall time, so total CPU
    use per request would fall as load rose and capacity would not be CPU-bound. This way every request costs exactly
    `ms` of CPU, so a pod's capacity is (CPU it is allowed) / ms and the HPA's CPU signal means something.

    Thread CPU time is only precise on Linux (where the container runs). On Windows its resolution is ~15.6 ms, so
    a 1 ms burn would spin ~15 ms; there (developer machines, unit tests) fall back to a wall-clock spin."""
    clock = _cpu_clock
    end = clock() + ms / 1000.0
    while clock() < end:
        pass


def mock_sql(question: str, schema: str) -> str:
    """Deterministic stand-in 'model': picks a table from the schema (or the question) and writes a plausible query."""
    tables = re.findall(r"create\s+table\s+(\w+)", schema, re.I)
    words = re.findall(r"[a-z_]+", question.lower())
    table = next((t for t in tables if t.lower() in words), tables[0] if tables else "t")
    if "count" in words or ("how" in words and "many" in words):
        return f"SELECT COUNT(*) FROM {table};"
    return f"SELECT * FROM {table} LIMIT 10;"


class Metrics:
    def __init__(self):
        self.registry = CollectorRegistry()
        self.requests = Counter("gateway_requests_total", "Requests by status code", ["code"], registry=self.registry)
        self.latency = Histogram("gateway_request_seconds", "End-to-end request latency", registry=self.registry,
                                 buckets=(.005, .01, .025, .05, .1, .25, .5, 1, 2.5, 5, 10, 30))
        self.inflight = Gauge("gateway_inflight", "Requests being processed", registry=self.registry)
        self.shed = Counter("gateway_shed_total", "Requests rejected because the gateway was at capacity", registry=self.registry)
        self.ready = Gauge("gateway_ready", "1 when ready to serve", registry=self.registry)


def create_app(settings: Settings | None = None) -> FastAPI:
    s = settings or Settings()
    m = Metrics()
    state = {"ready": False, "started": 0.0, "shutting_down": False, "inflight": 0}
    pool = ThreadPoolExecutor(max_workers=8)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        state["started"] = time.monotonic()

        async def warm():
            await asyncio.sleep(s.warmup_seconds)  # stands in for model load time
            state["ready"] = True
            m.ready.set(1)

        task = asyncio.create_task(warm())
        client = httpx.AsyncClient(timeout=s.request_timeout) if s.backend == "vllm" else None
        app.state.client = client
        yield
        state["shutting_down"] = True  # readiness flips first so the load balancer stops sending traffic
        m.ready.set(0)
        task.cancel()
        if client:
            await client.aclose()
        pool.shutdown(wait=False)

    app = FastAPI(title="text-to-sql gateway", lifespan=lifespan)
    app.state.settings, app.state.metrics, app.state.state = s, m, state

    async def backend_sql(req: SqlRequest) -> str:
        if s.backend == "mock":
            await asyncio.get_running_loop().run_in_executor(pool, burn_cpu, s.mock_cpu_ms)
            return mock_sql(req.question, req.schema_)
        payload = {"model": s.model_name, "temperature": 0, "max_tokens": s.max_tokens,
                   "messages": [{"role": "system", "content": "Translate the question into one SQLite query. Reply with SQL only."},
                                {"role": "user", "content": f"Schema:\n{req.schema_}\n\nQuestion: {req.question}"}]}
        try:
            r = await app.state.client.post(f"{s.vllm_url}/v1/chat/completions", json=payload)
        except httpx.TimeoutException as e:
            raise HTTPException(504, "backend timeout") from e
        except httpx.HTTPError as e:
            raise HTTPException(502, f"backend unreachable: {type(e).__name__}") from e
        if r.status_code != 200:
            raise HTTPException(502, f"backend returned {r.status_code}")
        try:
            return extract_sql(r.json()["choices"][0]["message"]["content"])
        except (KeyError, IndexError, ValueError) as e:
            raise HTTPException(502, "backend returned an unexpected body") from e

    @app.post("/v1/sql", response_model=SqlResponse)
    async def generate(req: SqlRequest, response: Response):
        t0 = time.perf_counter()
        if state["inflight"] >= s.max_inflight:
            m.shed.inc()
            m.requests.labels("503").inc()
            raise HTTPException(503, "overloaded", headers={"Retry-After": "1"})
        state["inflight"] += 1
        m.inflight.set(state["inflight"])
        code = "200"
        try:
            sql = await backend_sql(req)
            return SqlResponse(sql=sql, backend=s.backend, latency_ms=(time.perf_counter() - t0) * 1000)
        except HTTPException as e:
            code = str(e.status_code)
            raise
        finally:
            state["inflight"] -= 1
            m.inflight.set(state["inflight"])
            m.requests.labels(code).inc()
            m.latency.observe(time.perf_counter() - t0)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/readyz")
    async def readyz():
        if state["shutting_down"] or not state["ready"]:
            raise HTTPException(503, "not ready")
        if s.backend == "vllm":
            try:
                r = await app.state.client.get(f"{s.vllm_url}/health", timeout=2.0)
                if r.status_code != 200:
                    raise HTTPException(503, "backend not healthy")
            except httpx.HTTPError as e:
                raise HTTPException(503, "backend unreachable") from e
        return {"status": "ready"}

    @app.get("/metrics")
    async def metrics():
        return Response(generate_latest(m.registry), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()  # what uvicorn serves: `uvicorn app.main:app` (tests build their own via create_app())
