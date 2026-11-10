#!/usr/bin/env bash
# Same experiments as the local kind runs, against the GKE LoadBalancer. Usage: bash scripts/run_cloud_experiments.sh <LB_IP>
cd "$(dirname "$0")/.."
export PYTHONPATH=. PATH="/c/Users/rohil/gcloud-sdk/google-cloud-sdk/bin:$PATH"
URL="http://$1/v1/sql"
PY=.venv/Scripts/python.exe
run() { name=$1; shift; $PY -m loadtest.run_experiment --name "$name" --url "$URL" "$@" | tee results/cloud/$name.txt; mv -f results/$name.json results/cloud/$name.json 2>/dev/null; }
run cloud_hpa_step_perrequest --stages 20:5,120:40,40:5 --wait-baseline --no-keepalive
run cloud_pod_kill --stages 60:30 --kill-at 25 --no-keepalive --wait-baseline
run cloud_rolling_restart --stages 60:30 --rollout-at 10 --no-keepalive --wait-baseline
run cloud_hpa_step_keepalive --stages 20:5,120:40,40:5 --wait-baseline
echo done > results/cloud/DONE
