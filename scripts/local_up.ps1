# Bring up the LOCAL cluster (kind, in Docker) and deploy the gateway with the mock backend. No cloud, no cost, no admin.
#   powershell -File scripts\local_up.ps1
$ErrorActionPreference = "Stop"
$env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
Set-Location (Split-Path -Parent $PSScriptRoot)
$cluster = "sqlserve"
$metricsServer = "https://github.com/kubernetes-sigs/metrics-server/releases/download/v0.7.2/components.yaml"

docker build -t sql-gateway:dev . | Select-Object -Last 2
if (-not (kind get clusters | Select-String -SimpleMatch $cluster)) {
    kind create cluster --name $cluster --config k8s/kind-cluster.yaml
}
kind load docker-image sql-gateway:dev --name $cluster

# metrics-server is what feeds CPU usage to the HPA; kind's kubelets use self-signed certs, hence --kubelet-insecure-tls
kubectl apply -f $metricsServer | Out-Null
# (the patch lives in a file: PowerShell strips the double quotes out of inline JSON passed to native commands)
kubectl -n kube-system patch deployment metrics-server --type=json --patch-file k8s/kind-metrics-server-patch.json | Out-Null
kubectl -n kube-system rollout status deployment/metrics-server --timeout=180s

kubectl apply -k k8s/overlays/local-kind
kubectl -n sql rollout status deployment/gateway --timeout=180s
kubectl -n sql get pods,hpa -o wide
