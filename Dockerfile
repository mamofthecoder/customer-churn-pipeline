FROM python:3.8-slim-bookworm

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-lock.txt .
RUN python -m pip install --no-cache-dir -r requirements-lock.txt \
    && python -m pip check

COPY score_churn.py customer_churn_model.joblib ./

ENTRYPOINT ["python", "score_churn.py"]

