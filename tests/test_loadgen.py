import asyncio

import numpy as np
from conftest import wait_ready

from app.main import Settings, create_app
from loadtest.openloop import overall, parse_stages, percentile, run, schedule, summarize


def test_parse_stages():
    assert parse_stages("20:10,40:80.5") == [(20.0, 10.0), (40.0, 80.5)]


def test_schedule_rate_and_stage_boundaries():
    s = schedule([(10, 50), (10, 0), (10, 100)], poisson=True, seed=1)
    ts = [t for t, _ in s]
    assert ts == sorted(ts)
    n1 = sum(1 for t in ts if t < 10)
    n_gap = sum(1 for t in ts if 10 <= t < 20)
    n2 = sum(1 for t in ts if t >= 20)
    assert n_gap == 0 and abs(n1 - 500) < 90 and abs(n2 - 1000) < 120       # Poisson counts, ~4 sigma bounds
    c = schedule([(5, 20)], poisson=False)
    assert len(c) == 99 or len(c) == 100
    assert np.allclose(np.diff([t for t, _ in c]), 0.05)


def test_summarize_and_percentiles_on_synthetic_records():
    recs = [{"t": i * 0.1, "stage_rps": 10, "code": 200, "err": "", "latency": 0.001 * (i + 1)} for i in range(100)]
    recs += [{"t": 1.0, "stage_rps": 10, "code": 503, "err": "", "latency": 0.001},
             {"t": 1.1, "stage_rps": 10, "code": 0, "err": "timeout", "latency": 10.0}]
    o = overall(recs)
    assert o["requests"] == 102 and o["ok"] == 100 and o["shed_503"] == 1 and o["errors"] == 1
    assert abs(o["p50_ms"] - 50.5) < 1 and abs(o["p99_ms"] - 99.01) < 1.5   # 503s and timeouts must not pollute success latency
    w = summarize(recs, window=5.0)
    # the 503 (t=1.0) and the timeout (t=1.1) both fall in the first 5 s window, on top of its 50 successes
    assert [x["sent"] for x in w] == [52, 50] and w[0]["errors"] == 1 and w[0]["shed_503"] == 1 and w[1]["errors"] == 0
    assert np.isnan(percentile([], 50))


def test_generator_hits_the_target_rate_against_a_real_server(serve):
    # this test is about the generator's rate accuracy, so shedding is disabled (it would otherwise make the result
    # depend on how busy the machine happens to be)
    srv = serve(create_app(Settings(backend="mock", mock_cpu_ms=1, warmup_seconds=0.0, max_inflight=10_000)))
    wait_ready(srv.url)
    recs = asyncio.run(run(srv.url + "/v1/sql", [(3, 100)], poisson=False))
    assert 295 <= len(recs) <= 300
    o = overall(recs)
    assert o["ok_frac"] > 0.99 and o["p99_ms"] < 500


def _count_connections(keepalive: bool, n_requests: int = 20) -> int:
    """Tiny raw HTTP server that counts accepted TCP connections."""
    async def main():
        conns = {"n": 0}

        async def handle(reader, writer):
            conns["n"] += 1
            try:
                while True:
                    head = await reader.readuntil(b"\r\n\r\n")
                    length = 0
                    for line in head.split(b"\r\n"):
                        if line.lower().startswith(b"content-length:"):
                            length = int(line.split(b":")[1])
                    await reader.readexactly(length)
                    writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\n{}")
                    await writer.drain()
                    if b"connection: close" in head.lower():
                        break
            except (asyncio.IncompleteReadError, ConnectionError):
                pass
            finally:
                writer.close()

        server = await asyncio.start_server(handle, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        async with server:
            recs = await run(f"http://127.0.0.1:{port}/x", [(1, n_requests)], poisson=False, keepalive=keepalive)
        assert all(r["code"] == 200 for r in recs), recs[:2]
        return conns["n"], len(recs)

    return asyncio.run(main())


def test_keepalive_flag_controls_connection_reuse():
    """Matters on Kubernetes: per-connection balancing pins keep-alive clients to the pods that existed when they connected."""
    conns_keep, n1 = _count_connections(True)
    conns_fresh, n2 = _count_connections(False)
    assert conns_keep <= 3, conns_keep                     # sequential requests reuse one (or very few) connections
    assert conns_fresh == n2 >= 19, (conns_fresh, n2)      # one connection per request


def test_latency_is_measured_from_the_intended_send_time_so_stalls_are_not_omitted(serve):
    """Server capacity ~ 1/0.1s = 10 rps but we offer 40 rps: an open-loop client must see latency GROW as the queue
    builds. A closed-loop client would just slow down and report a flat, flattering latency."""
    srv = serve(create_app(Settings(backend="mock", mock_cpu_ms=100, warmup_seconds=0.0, max_inflight=10_000)))
    wait_ready(srv.url)
    recs = asyncio.run(run(srv.url + "/v1/sql", [(4, 40)], poisson=False, timeout=30))
    # all of the first second's requests queue behind each other, so compare the EARLIEST arrivals with the latest
    first = [r["latency"] for r in recs if r["t"] < 0.25]
    last = [r["latency"] for r in recs if r["t"] > 3]
    assert np.median(last) > 3 * np.median(first), (np.median(first), np.median(last))
    assert np.median(last) > 2.0
