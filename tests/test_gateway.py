import concurrent.futures as cf
import re
import time

import httpx
import pytest
from conftest import fake_vllm, wait_ready

from app.main import Settings, create_app, extract_sql, mock_sql

SCHEMA = "CREATE TABLE singer (id INT, name TEXT); CREATE TABLE concert (id INT);"


def mock_app(**kw):
    base = dict(backend="mock", mock_cpu_ms=5, warmup_seconds=0.0, max_inflight=32)
    base.update(kw)
    return create_app(Settings(**base))


def test_extract_sql_handles_fences_and_plain_text():
    assert extract_sql("```sql\nSELECT 1;\n```") == "SELECT 1;"
    assert extract_sql("blah ```SELECT 2```") == "SELECT 2"
    assert extract_sql("  SELECT 3;  ") == "SELECT 3;"


def test_mock_sql_is_deterministic_and_uses_the_schema():
    assert mock_sql("How many singers are there?", SCHEMA) == "SELECT COUNT(*) FROM singer;"
    assert mock_sql("List the concert rows", SCHEMA) == "SELECT * FROM concert LIMIT 10;"
    assert mock_sql("anything", "") == "SELECT * FROM t LIMIT 10;"
    assert mock_sql("How many singers are there?", SCHEMA) == mock_sql("How many singers are there?", SCHEMA)


def test_generate_returns_sql_and_validates_input(serve):
    srv = serve(mock_app())
    wait_ready(srv.url)
    r = httpx.post(srv.url + "/v1/sql", json={"question": "How many singers are there?", "schema": SCHEMA})
    assert r.status_code == 200
    body = r.json()
    assert body["sql"] == "SELECT COUNT(*) FROM singer;" and body["backend"] == "mock" and body["latency_ms"] >= 5
    assert httpx.post(srv.url + "/v1/sql", json={"schema": SCHEMA}).status_code == 422       # missing question
    assert httpx.post(srv.url + "/v1/sql", json={"question": ""}).status_code == 422          # empty question
    assert httpx.post(srv.url + "/v1/sql", json={"question": "x" * 5000}).status_code == 422  # too long


def test_readiness_waits_for_warmup_but_liveness_does_not(serve):
    srv = serve(mock_app(warmup_seconds=0.8))
    assert httpx.get(srv.url + "/healthz").status_code == 200          # alive immediately
    assert httpx.get(srv.url + "/readyz").status_code == 503           # but not ready while "loading the model"
    time.sleep(1.0)
    assert httpx.get(srv.url + "/readyz").status_code == 200


def test_overload_is_shed_with_503_instead_of_queueing(serve):
    srv = serve(mock_app(mock_cpu_ms=250, max_inflight=2))
    wait_ready(srv.url)

    def call(_):
        r = httpx.post(srv.url + "/v1/sql", json={"question": "q"}, timeout=10)
        return r.status_code, r.headers.get("retry-after")

    with cf.ThreadPoolExecutor(12) as ex:
        res = list(ex.map(call, range(12)))
    codes = [c for c, _ in res]
    # exactly how many are shed depends on how fast the client threads connect, so assert the behaviour, not a count
    assert codes.count(503) >= 3 and codes.count(200) >= 2, codes
    assert set(codes) <= {200, 503}, "overload must produce 503, never timeouts or other 5xx"
    assert all(ra == "1" for c, ra in res if c == 503)
    m = httpx.get(srv.url + "/metrics").text
    shed = float(re.search(r"^gateway_shed_total ([\d.e+]+)", m, re.M).group(1))
    assert shed == codes.count(503)


def test_metrics_expose_counts_latency_histogram_and_readiness(serve):
    srv = serve(mock_app())
    wait_ready(srv.url)
    for _ in range(5):
        httpx.post(srv.url + "/v1/sql", json={"question": "hello"})
    text = httpx.get(srv.url + "/metrics").text
    assert 'gateway_requests_total{code="200"} 5.0' in text
    assert "gateway_request_seconds_bucket" in text and "gateway_request_seconds_count 5.0" in text
    assert re.search(r"^gateway_ready 1.0", text, re.M)
    assert re.search(r"^gateway_inflight 0.0", text, re.M)


# ---------------------------------------------------------------- vLLM backend against a fake OpenAI-compatible server
@pytest.fixture
def vllm_pair(serve):
    box = {"mode": "ok"}
    backend = serve(fake_vllm(box))
    gw = serve(create_app(Settings(backend="vllm", vllm_url=backend.url, warmup_seconds=0.0, request_timeout=1.0,
                                   model_name="sql-lora")))
    wait_ready(gw.url)
    return gw, box


def test_vllm_backend_proxies_and_extracts_sql(vllm_pair):
    gw, box = vllm_pair
    r = httpx.post(gw.url + "/v1/sql", json={"question": "How many singers?", "schema": SCHEMA})
    assert r.status_code == 200 and r.json()["sql"] == "SELECT COUNT(*) FROM singer;"
    body = box["last_body"]
    assert body["model"] == "sql-lora" and body["temperature"] == 0 and SCHEMA in body["messages"][1]["content"]


@pytest.mark.parametrize("mode,code", [("error", 502), ("garbage", 502), ("slow", 504)])
def test_vllm_failures_map_to_gateway_errors(vllm_pair, mode, code):
    gw, box = vllm_pair
    box["mode"] = mode
    assert httpx.post(gw.url + "/v1/sql", json={"question": "q"}, timeout=10).status_code == code
    text = httpx.get(gw.url + "/metrics").text
    assert f'gateway_requests_total{{code="{code}"}} 1.0' in text


def test_readiness_follows_backend_health(vllm_pair):
    gw, box = vllm_pair
    assert httpx.get(gw.url + "/readyz").status_code == 200
    box["mode"] = "unhealthy"
    assert httpx.get(gw.url + "/readyz").status_code == 503  # pod would be pulled from the Service
    box["mode"] = "ok"
    assert httpx.get(gw.url + "/readyz").status_code == 200
    assert httpx.get(gw.url + "/healthz").status_code == 200  # liveness must NOT depend on the backend (no restart storms)


def test_readiness_handler_honors_the_shutdown_flag(serve):
    srv = serve(mock_app())
    wait_ready(srv.url)
    assert httpx.get(srv.url + "/readyz").status_code == 200
    srv.server.config.app.state.state["shutting_down"] = True
    assert httpx.get(srv.url + "/readyz").status_code == 503


def test_real_lifespan_shutdown_marks_the_app_not_ready():
    """Starts and stops the app through its real lifespan: after shutdown the readiness gauge is 0 and the flag is set,
    so a pod receiving SIGTERM stops being advertised as ready before it stops serving."""
    from starlette.testclient import TestClient

    app = mock_app(warmup_seconds=0.0)
    with TestClient(app) as c:
        time.sleep(0.2)
        assert c.get("/readyz").status_code == 200
        assert 'gateway_ready 1.0' in c.get("/metrics").text
    assert app.state.state["shutting_down"] is True
    assert app.state.metrics.ready._value.get() == 0.0
