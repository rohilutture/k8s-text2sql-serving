"""Closed-loop Locust scenario, for interactive exploration with the Locust UI:

    locust -f loadtest/locustfile.py --host http://127.0.0.1:30080

CAVEAT: Locust users each wait for their reply before sending the next request, so when the server slows down the
offered load silently drops and tail latency looks better than it is (coordinated omission). For measurements that
are meant to be reported, use loadtest/openloop.py (fixed arrival rate, latency from the intended send time).
"""
from locust import HttpUser, constant_throughput, task

SCHEMA = "CREATE TABLE singer (singer_id INT, name TEXT, age INT); CREATE TABLE concert (concert_id INT);"


class SqlUser(HttpUser):
    wait_time = constant_throughput(2)  # each simulated user sends ~2 requests per second

    @task(3)
    def count_query(self):
        self.client.post("/v1/sql", json={"question": "How many singers are there?", "schema": SCHEMA}, name="/v1/sql")

    @task(1)
    def list_query(self):
        self.client.post("/v1/sql", json={"question": "List all concerts", "schema": SCHEMA}, name="/v1/sql")

    def on_start(self):
        # readiness check so a not-yet-ready pod does not pollute the first seconds of a run
        self.client.get("/readyz", name="/readyz")
