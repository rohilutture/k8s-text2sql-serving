"""One request through the cluster's NodePort. python scripts/smoke.py [url]"""
import sys

import httpx

url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:30080"
r = httpx.post(url + "/v1/sql", json={"question": "How many singers are there?",
                                      "schema": "CREATE TABLE singer (id INT, name TEXT);"}, timeout=10)
print(r.status_code, r.json())
print("readyz", httpx.get(url + "/readyz").status_code, "| metrics lines:", len(httpx.get(url + "/metrics").text.splitlines()))
