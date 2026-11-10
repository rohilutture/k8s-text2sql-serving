# Text-to-SQL serving on Kubernetes: autoscaling, load testing, and a GPU deployment spec

Status: **the Kubernetes behaviour (probes, HPA, rollouts, load shedding) was run for real on a local `kind`
cluster with a CPU "mock" backend, and measured. The GPU/vLLM deployment and the Terraform for GKE were only
validated offline: nothing was ever applied to a cloud account, no GPU served a request, and no cloud
latency, cost, or uptime was measured.**

## What exists

| Piece | Where | Verified how |
|---|---|---|
| Gateway service (FastAPI): `/v1/sql`, `/healthz`, `/readyz`, `/metrics`, load shedding, mock and vLLM backends | `app/main.py`, `Dockerfile` | 19 pytest tests; ran in the cluster |
| Kubernetes manifests (kustomize): Deployment, Service, HPA, PDB, hardened pod spec | `k8s/base`, `k8s/overlays/local-kind` | **ran on kind**, load-tested |
| GPU overlay: vLLM Deployment (1x L4, PVC model cache, long startup probe), gateway pointed at it | `k8s/overlays/cloud-gke` | server-side dry run against a real API server (schema + admission); never ran |
| Terraform for GKE (VPC, zonal cluster, CPU pool, GPU pool scaling from 0, Artifact Registry, optional budget) | `terraform/gke` | `terraform fmt` + `validate` against the real google provider; **never planned or applied** |
| Open-loop load generator and experiment runner | `loadtest/` | 6 tests; used for every number below |

## Experiments on the local cluster

Setup: `kind` v0.33 (Kubernetes 1.37) with 1 control-plane and 3 worker nodes in Docker on one 24-core machine, so
"nodes" share the same CPUs. The mock backend burns 20 ms of real CPU per request (`thread_time`), so a pod limited
to 0.5 CPU serves roughly 25 requests/s. Pod: CPU request 250m, limit 500m; HPA target 60 percent of the request,
min 1, max 12; `MAX_INFLIGHT` 16 (beyond that the pod answers 503 immediately). Client: open-loop, Poisson arrivals,
latency measured from the scheduled send time. Single run of each; raw output is in `results/`.

**1. Steady load below capacity** (5 rps for 40 s, one pod): 190/190 succeeded, p50 36 ms, p99 62 ms.

**2. Load step 5 to 40 rps, with keep-alive connections: scale-out did not help.**
One pod served all 3,309 successful requests and issued all 1,916 rejections, while the three pods the HPA added
served **zero** requests (read from each pod's own counter). Successful throughput stayed near 24 rps with 1, 2
and 4 pods, p50 stayed near 1.1 s for the whole 120 s step, and 62 percent of requests succeeded overall. Why:
a Kubernetes Service balances per TCP connection, keep-alive clients stay on the pods they first connected to, and
the HPA's *average* CPU (about 50 percent across 4 pods, one saturated and three idle) looked healthy.

**3. Same step, fresh connection per request (behaves like an L7 balancer): scale-out worked.**

| time into the step | ready pods | p50 | p99 | rejected (503) |
|---|---|---|---|---|
| first 25 s | 1 | about 1.2 s | about 1.4 s | about 40-60 percent |
| 25-45 s | 2 | 120-450 ms | 330 ms-1.3 s | 41 then 0 |
| 45-60 s | 2 to 4 | 33-42 ms | 130-240 ms | 0 |
| 60 s onward | 4 to 6 | 28-29 ms | 95-250 ms | 0 |

The first new pod became ready about 25 s after the step (metrics-server's 15 s resolution, the HPA sync period,
and a 3 s warm-up), so a burst costs roughly half a minute of degraded service; the 455 rejected requests (9 percent
of the run) all fall in that window. After scale-out p50 returned to about 29 ms. Scale-down is deliberately slow
(60 s stabilization, one pod per 30 s).

**4. Pod killed under 30 rps (6 pods):** 1,779/1,779 succeeded, 0 rejected, p99 113 ms; ready pods dipped from 5 to
4 and recovered. (The HPA was also scaling down from the previous run during this test; that is visible in the
replica column and is not caused by the kill.) The `preStop` sleep keeps a terminating pod serving while endpoint
removal propagates.

**5. Rolling restart under 30 rps (5 pods):** 2,058/2,058 succeeded, 0 rejected, p99 119 ms. Confirmed from the
cluster: all 5 pods were replaced (a new ReplicaSet) and the ready count never dropped below 5
(`maxUnavailable: 0`, `maxSurge: 1`).

## What these results do and do not show

- They show that the manifests and probes behave as designed, **and that a default Service plus keep-alive clients
  defeats horizontal scaling** (experiment 2 vs 3), the most useful finding here. The fix in a real deployment is
  L7 load balancing (an Ingress or gateway, a service mesh) or bounding connection lifetime; this repo only
  demonstrates the problem and the per-request-connection control, it does not implement the L7 fix.
- They are **not** vLLM, GPU, or cloud numbers. The mock backend is deterministic CPU burn, not a model; real
  inference latency, batching, prefix-cache behaviour, GPU autoscaling and cold-start are all unmeasured. Scaling a
  GPU deployment on CPU utilization would be wrong anyway: use the queue depth (`vllm:num_requests_waiting`) via
  KEDA or a Prometheus adapter; that is documented here and not built.
- One machine, shared CPUs, kind networking, single runs: the absolute figures (25 s reaction, 25 rps per pod) are
  not portable; the qualitative behaviour is.

## GKE deployment (not run)

`terraform/gke` creates a zonal cluster, a CPU node pool (1 to 3 nodes), a GPU node pool that scales from 0 to
`max_gpu_nodes` (default 1) so no GPU exists until a vLLM pod is pending, an Artifact Registry repository, a
least-privilege node service account, and, if you give it a billing account, a budget with alerts at 50/90/100
percent. Defaults use Spot VMs (cheaper, can be reclaimed).

Approximate cost, **from memory, not checked against current GCP pricing**: a g2-standard-4 with one L4 is on the
order of $0.7 per hour on demand and well under half that on Spot, plus a CPU node and disk; a one-to-two-hour demo
is single-digit dollars if torn down, and a forgotten GPU node is tens of dollars per day. Verify in the GCP
pricing calculator first.

Before running anything in a cloud account, **you** must decide to spend the money and authenticate
(`gcloud auth application-default login`). Then:

```bash
cd terraform/gke
terraform init
terraform plan  -var project_id=YOUR_PROJECT -var billing_account=XXXXXX-XXXXXX-XXXXXX   # read the plan
terraform apply -var project_id=YOUR_PROJECT ...
# build and push the gateway image to the registry output, set PROJECT_ID and the tag in
# k8s/overlays/cloud-gke/kustomization.yaml (and pin the vLLM image tag), then:
gcloud container clusters get-credentials ...   # printed by terraform output
kubectl apply -k k8s/overlays/cloud-gke
# ... measure ...
terraform destroy -var project_id=YOUR_PROJECT ...    # always tear down
```

Known gaps in the GCP side: the vLLM image tag is a placeholder to verify, the model is a public base model (swap
in your merged fine-tuned one), no Ingress/TLS or authentication, no Prometheus/KEDA, state is local by default.

## Run it yourself (local, no cloud, no admin)

```powershell
.venv\Scripts\python.exe -m pytest -q                       # 19 tests
powershell -File scripts\local_up.ps1                       # image, kind cluster, metrics-server, deploy (~3-5 min)
.venv\Scripts\python.exe scripts\smoke.py
$env:PYTHONPATH="."
.venv\Scripts\python.exe -m loadtest.run_experiment --name hpa_step --stages 20:5,120:40,40:5 --wait-baseline --no-keepalive
.venv\Scripts\python.exe -m loadtest.run_experiment --name pod_kill --stages 60:30 --kill-at 25 --no-keepalive
powershell -File scripts\local_down.ps1                     # tear the cluster down
```

Gotchas hit while building this, kept as comments in the scripts: PowerShell strips quotes from inline JSON passed
to native commands (use `--patch-file`); metrics-server needs `--kubelet-insecure-tls` on kind; `time.thread_time()`
has ~15 ms resolution on Windows (the mock uses it only on Linux).

## Phrasing rules

- Safe: "deployed a containerized inference gateway to Kubernetes (local kind) with HPA, PDB, probes and load
  shedding; load-tested with an open-loop generator; found and demonstrated that keep-alive clients pin traffic to
  one pod and defeat scale-out; validated Terraform and a GPU vLLM overlay offline."
- Never claim a cloud deployment, GPU serving, real model latency, or uptime. Name the backend ("CPU mock") and
  the environment ("local kind, one machine") with every number.
