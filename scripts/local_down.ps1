# Tear down the local cluster completely (frees the Docker containers and ports).
$env:Path = [Environment]::GetEnvironmentVariable("Path", "User") + ";" + $env:Path
kind delete cluster --name sqlserve
