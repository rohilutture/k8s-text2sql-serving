FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PIP_NO_CACHE_DIR=1

# non-root, fixed uid so Kubernetes runAsNonRoot can verify it
RUN useradd --uid 10001 --no-create-home --shell /usr/sbin/nologin app
WORKDIR /srv

COPY requirements.txt .
RUN pip install -r requirements.txt

COPY app ./app
USER 10001

EXPOSE 8000
# One worker per pod: scale by adding pods (the HPA's job), not processes, so CPU requests/limits and the
# autoscaler's utilisation signal stay meaningful. Graceful shutdown must be shorter than terminationGracePeriodSeconds.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--timeout-graceful-shutdown", "20"]
