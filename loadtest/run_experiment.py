"""Run a load experiment against the cluster and record BOTH the client-side latency and the cluster's reaction
(HPA replica counts over time).

    python -m loadtest.run_experiment --name hpa_step --stages 30:10,90:60,60:10
    python -m loadtest.run_experiment --name pod_kill --stages 20:30,60:30 --kill-at 30

Writes results/<name>.json (summary, per-window stats, replica timeline). Needs kubectl pointing at the cluster.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import subprocess
import threading
import time
from pathlib import Path

from .openloop import overall, parse_stages, print_table, run, summarize


def kubectl(*args: str, timeout: float = 20) -> str:
    return subprocess.run(["kubectl", "-n", "sql", *args], capture_output=True, text=True, timeout=timeout).stdout


def hpa_state() -> dict:
    try:
        h = json.loads(kubectl("get", "hpa", "gateway", "-o", "json"))
        cpu = None
        for m in h.get("status", {}).get("currentMetrics", []) or []:
            if m.get("type") == "Resource":
                cpu = m["resource"]["current"].get("averageUtilization")
        d = json.loads(kubectl("get", "deploy", "gateway", "-o", "json"))["status"]
        return {"current": h["status"].get("currentReplicas"), "desired": h["status"].get("desiredReplicas"),
                "ready": d.get("readyReplicas", 0), "cpu_pct": cpu}
    except Exception:  # noqa: BLE001  (a failed poll is just a missing sample)
        return {}


def sampler(stop: threading.Event, t0: float, out: list, every: float = 2.0):
    while not stop.is_set():
        s = hpa_state()
        if s:
            out.append({"t": round(time.time() - t0, 1), **s})
        stop.wait(every)


def wait_for_baseline(replicas: int = 1, timeout: float = 400):
    end = time.time() + timeout
    while time.time() < end:
        if hpa_state().get("current") == replicas and hpa_state().get("ready") == replicas:
            return True
        time.sleep(5)
    return False


def kill_one_pod():
    pods = kubectl("get", "pods", "-l", "app=gateway", "-o", "jsonpath={.items[*].metadata.name}").split()
    if pods:
        subprocess.run(["kubectl", "-n", "sql", "delete", "pod", pods[0], "--wait=false"], capture_output=True)
        return pods[0]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--name", required=True)
    ap.add_argument("--stages", required=True)
    ap.add_argument("--url", default="http://127.0.0.1:30080/v1/sql")
    ap.add_argument("--kill-at", type=float, help="seconds into the run at which to delete one gateway pod")
    ap.add_argument("--rollout-at", type=float, help="seconds into the run at which to `kubectl rollout restart` the gateway")
    ap.add_argument("--wait-baseline", action="store_true", help="wait for the HPA to be back at 1 replica first")
    ap.add_argument("--window", type=float, default=5.0)
    ap.add_argument("--constant", action="store_true")
    ap.add_argument("--no-keepalive", action="store_true", help="fresh TCP connection per request (see openloop.run)")
    a = ap.parse_args()

    if a.wait_baseline:
        print("waiting for 1 ready replica...", flush=True)
        print("baseline reached" if wait_for_baseline() else "WARNING: baseline not reached", flush=True)

    timeline: list[dict] = []
    stop, t0 = threading.Event(), time.time()
    threading.Thread(target=sampler, args=(stop, t0, timeline), daemon=True).start()
    killed = {}
    if a.kill_at is not None:
        def later():
            time.sleep(a.kill_at)
            killed["pod"], killed["t"] = kill_one_pod(), round(time.time() - t0, 1)
        threading.Thread(target=later, daemon=True).start()

    if a.rollout_at is not None:
        def roll():
            time.sleep(a.rollout_at)
            subprocess.run(["kubectl", "-n", "sql", "rollout", "restart", "deployment/gateway"], capture_output=True)
            killed["rollout_restart_at"] = round(time.time() - t0, 1)
        threading.Thread(target=roll, daemon=True).start()

    recs = asyncio.run(run(a.url, parse_stages(a.stages), poisson=not a.constant, keepalive=not a.no_keepalive))
    time.sleep(2)
    stop.set()

    rows = summarize(recs, a.window)
    for r in rows:  # attach the cluster's replica count at the START of each window
        s = [x for x in timeline if x["t"] <= r["t"] + 0.5]
        r["replicas_ready"] = s[-1]["ready"] if s else None
        r["cpu_pct"] = s[-1]["cpu_pct"] if s else None
    print("|   t(s) | offered | ready pods | cpu%  |    ok | 503 | err |  p50ms |  p95ms |  p99ms |")
    print("|-------:|--------:|-----------:|------:|------:|----:|----:|-------:|-------:|-------:|")
    for r in rows:
        print(f"| {r['t']:6.0f} | {r['rps']:7.1f} | {str(r['replicas_ready']):>10} | {str(r['cpu_pct']):>5} | {r['ok']:5d} | "
              f"{r['shed_503']:3d} | {r['errors']:3d} | {r['p50_ms']:6.1f} | {r['p95_ms']:6.1f} | {r['p99_ms']:6.1f} |")
    summ = overall(recs)
    print(summ)
    if killed:
        print("killed pod:", killed)
    Path("results").mkdir(exist_ok=True)
    Path(f"results/{a.name}.json").write_text(json.dumps(
        {"stages": a.stages, "summary": summ, "windows": rows, "replica_timeline": timeline, "killed": killed}, indent=1))


if __name__ == "__main__":
    main()
