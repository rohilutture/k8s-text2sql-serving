"""Open-loop load generator.

A closed-loop tool (a fixed pool of users that each wait for their reply, like Locust by default) slows down when the
server slows down, so overload quietly turns into "fewer requests" and the tail latency looks fine. This generator
instead fires requests on a fixed schedule whatever the server does, and measures latency from the SCHEDULED send time,
so a stall (in the server or in this client) shows up as latency instead of being omitted ("coordinated omission").

    python -m loadtest.openloop --url http://localhost:30080/v1/sql --stages 20:20,40:80,20:20 --out results/run.jsonl

--stages is a comma-separated list of duration_seconds:requests_per_second. Arrivals are Poisson by default
(--constant for evenly spaced). Every request is written to --out as one JSON line.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import random
import time
from collections import defaultdict
from pathlib import Path

import aiohttp
import numpy as np

PAYLOAD = {"question": "How many singers are there?",
           "schema": "CREATE TABLE singer (singer_id INT, name TEXT, age INT); CREATE TABLE concert (concert_id INT);"}


def parse_stages(s: str) -> list[tuple[float, float]]:
    out = []
    for part in s.split(","):
        d, r = part.split(":")
        out.append((float(d), float(r)))
    return out


def schedule(stages: list[tuple[float, float]], poisson: bool = True, seed: int = 0) -> list[tuple[float, float]]:
    """[(send_time_offset, stage_rps)] for every request."""
    rng = random.Random(seed)
    out, t0 = [], 0.0
    for dur, rps in stages:
        if rps <= 0:
            t0 += dur
            continue
        t = t0
        while True:
            t += rng.expovariate(rps) if poisson else 1.0 / rps
            if t >= t0 + dur:
                break
            out.append((t, rps))
        t0 += dur
    return out


async def run(url: str, stages, poisson: bool = True, timeout: float = 10.0, payload=None, seed: int = 0,
              max_connections: int = 4000, keepalive: bool = True) -> list[dict]:
    """keepalive=False opens a fresh TCP connection per request. That matters on Kubernetes: a plain Service balances
    per CONNECTION (kube-proxy), so keep-alive clients stay pinned to whichever pods existed when they connected and
    scale-out never receives their traffic; a fresh connection per request behaves like an L7 balancer."""
    sched = schedule(stages, poisson, seed)
    records: list[dict] = []
    conn = aiohttp.TCPConnector(limit=max_connections, ttl_dns_cache=300, force_close=not keepalive)
    tmo = aiohttp.ClientTimeout(total=timeout)
    async with aiohttp.ClientSession(connector=conn, timeout=tmo) as sess:
        t_start = time.perf_counter()

        async def one(offset: float, stage_rps: float):
            intended = t_start + offset
            code, err = 0, ""
            try:
                async with sess.post(url, json=payload or PAYLOAD) as r:
                    await r.read()
                    code = r.status
            except asyncio.TimeoutError:
                err = "timeout"
            except aiohttp.ClientError as e:
                err = type(e).__name__
            records.append({"t": offset, "stage_rps": stage_rps, "code": code, "err": err,
                            "latency": time.perf_counter() - intended})  # from the INTENDED time

        tasks = []
        for offset, rps in sched:
            delay = t_start + offset - time.perf_counter()
            if delay > 0:
                await asyncio.sleep(delay)
            tasks.append(asyncio.create_task(one(offset, rps)))
        await asyncio.gather(*tasks)
    return records


def percentile(xs: list[float], q: float) -> float:
    return float(np.percentile(xs, q)) if xs else float("nan")


def summarize(records: list[dict], window: float = 5.0) -> list[dict]:
    """Per-time-window stats. Latency percentiles are over SUCCESSFUL (200) requests; errors are counted separately."""
    wins = defaultdict(list)
    for r in records:
        wins[int(r["t"] // window)].append(r)
    out = []
    for w in sorted(wins):
        rs = wins[w]
        ok = [r["latency"] * 1000 for r in rs if r["code"] == 200]
        out.append({"t": w * window, "sent": len(rs), "rps": len(rs) / window, "ok": len(ok),
                    "shed_503": sum(r["code"] == 503 for r in rs),
                    "errors": sum(1 for r in rs if r["code"] not in (200, 503)),
                    "p50_ms": percentile(ok, 50), "p95_ms": percentile(ok, 95), "p99_ms": percentile(ok, 99)})
    return out


def overall(records: list[dict]) -> dict:
    ok = [r["latency"] * 1000 for r in records if r["code"] == 200]
    n = len(records)
    return {"requests": n, "ok": len(ok), "ok_frac": len(ok) / n if n else float("nan"),
            "shed_503": sum(r["code"] == 503 for r in records),
            "errors": sum(1 for r in records if r["code"] not in (200, 503)),
            "p50_ms": percentile(ok, 50), "p95_ms": percentile(ok, 95), "p99_ms": percentile(ok, 99)}


def print_table(rows: list[dict]):
    print("|   t(s) |   rps |    ok | 503 | err |  p50ms |  p95ms |  p99ms |")
    print("|-------:|------:|------:|----:|----:|-------:|-------:|-------:|")
    for r in rows:
        print(f"| {r['t']:6.0f} | {r['rps']:5.1f} | {r['ok']:5d} | {r['shed_503']:3d} | {r['errors']:3d} | "
              f"{r['p50_ms']:6.1f} | {r['p95_ms']:6.1f} | {r['p99_ms']:6.1f} |")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", required=True)
    ap.add_argument("--stages", required=True)
    ap.add_argument("--constant", action="store_true")
    ap.add_argument("--no-keepalive", action="store_true", help="fresh TCP connection per request")
    ap.add_argument("--timeout", type=float, default=10.0)
    ap.add_argument("--out")
    ap.add_argument("--window", type=float, default=5.0)
    a = ap.parse_args()
    recs = asyncio.run(run(a.url, parse_stages(a.stages), poisson=not a.constant, timeout=a.timeout,
                           keepalive=not a.no_keepalive))
    if a.out:
        Path(a.out).parent.mkdir(parents=True, exist_ok=True)
        Path(a.out).write_text("\n".join(json.dumps(r) for r in recs))
    print_table(summarize(recs, a.window))
    print(overall(recs))


if __name__ == "__main__":
    main()
