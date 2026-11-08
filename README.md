# Customer Churn Prediction and PostgreSQL Scoring Pipeline

A learning and portfolio project that connects customer churn modeling to a repeatable PostgreSQL-to-Python batch scoring workflow. The project uses **synthetic data** and demonstrates cost-based model selection, incremental scoring, database change tracking, and automated testing.

## Project overview

The model estimates whether a customer will churn during the following month. Features represent information available before that outcome period:

| Feature | Meaning |
|---|---|
| `Tenure_Months` | Customer tenure in whole months |
| `Monthly_Fee` | Monthly subscription fee |
| `Support_Tickets` | Ticket count; the pipeline constructs this over the previous 30 days |
| `Usage_Hours` | Customer usage hours |
| `Contract_Type` | Monthly or Annual contract |

Customer IDs identify records but are excluded from model training. Numeric features use median imputation and scaling; contract types use most-frequent imputation and one-hot encoding. Preprocessing is fitted within training folds through a scikit-learn pipeline.

## Model development and results

The modeling dataset contains 2,000 synthetic customers. Development uses 1,600 rows, including 240 churners (15%). Logistic regression and a constrained random forest were compared using stratified cross-validation and a threshold grid of 0.05–0.30.

The illustrative cost function assigns **R100 to a false negative** and **R10 to a false positive**. These are assumptions for the exercise, not measured business costs.

The final development-selected configuration is **logistic regression with a 0.10 decision threshold**. Five-fold outer validation, with model and threshold selection inside each outer training fold, estimated **R7.20 per customer** for the selection process. Logistic regression was selected in four outer folds and random forest in one.

The original 400-row holdout was accidentally included in an earlier development loop. It was therefore no longer treated as an untouched final test. After freezing the model and threshold, evaluation used **400 newly generated independent synthetic customers**, produced with the same generating process and a different seed.

| Fresh evaluation metric | Result |
|---|---:|
| True negatives | 144 |
| False positives | 193 |
| False negatives | 15 |
| True positives | 48 |
| Precision | 19.92% |
| Recall | 76.19% |
| Illustrative model cost | R3,430 |
| Always-predict-no-churn baseline cost | R6,300 |
| Model cost per customer | R8.575 |
| Cost reduction against baseline | 45.56% |

The low threshold catches more churners while generating many false positives. The cost reduction applies to the assumed error costs; it does **not** establish real-world retention savings or the effectiveness of an intervention.

## Batch scoring workflow

1. Read customer snapshots for a requested scoring month from PostgreSQL.
2. Acquire a transaction-level advisory lock for that month; skip if another scoring execution holds it.
3. Select missing predictions, predictions with stale inputs, or predictions from a different model version.
4. Validate inputs before passing them to the saved model pipeline.
5. Apply the threshold stored in the model bundle.
6. Upsert predictions using `(customer_id, scoring_month)` as the key.
7. Record the execution outcome in a separate run-history transaction.

An unchanged rerun is a successful no-op: existing predictions, scoring timestamps, and prediction run IDs remain unchanged. A missing source snapshot batch is an error. A busy lock records a `Skipped` execution.

### Database objects

| Object | Purpose |
|---|---|
| `churn_customers` | Customer identifiers |
| `churn_customer_snapshots` | Customer attributes per scoring month |
| `churn_support_tickets` | Ticket event dates and customer references |
| `churn_ticket_change_log` | Old/new ticket customer IDs and event dates, including deletions |
| `churn_predictions` | Latest prediction per customer-month, model version, scoring time, and originating run ID |
| `churn_pipeline_runs` | One row per execution: Running, Succeeded, Failed, or Skipped |
| `churn_monthly_coverage` | Coverage and flagged percentage by month and contract type |

Snapshot and ticket triggers maintain change timestamps. A ticket change log preserves the information needed to invalidate predictions after deletions or corrections that move a ticket between customers or feature windows. Ticket counts are calculated from the current ticket table, not from the log.

## Repository files

Keep these files together in the project directory:

| File | Purpose |
|---|---|
| `customer_churn_project.ipynb` | Synthetic data generation, exploration, model development, and evaluation |
| `customer_churn_model.joblib` | Fitted pipeline, feature names, threshold, costs, and model version |
| `score_churn.py` | Command-line incremental batch scoring job |
| `test_score_churn.py` | Input-validation unit tests |
| `test_churn_integration.py` | Isolated PostgreSQL integration tests |
| `migrations/001_baseline.sql` | Initial schema baseline |
| `migrations/002_link_predictions_to_runs.sql` | Prediction-to-run foreign key |
| `migrations/003_add_skipped_run_status.sql` | Adds Skipped to allowed run statuses |
| `requirements.txt` | Direct dependency pins |
| `requirements-lock.txt` | Full dependency snapshot, when generated from the verified environment |

## Environment and installation

The development environment uses **Python 3.8.19**, **PostgreSQL 16.6**, and:

```text
numpy==1.24.4
pandas==2.0.3
scipy==1.10.1
scikit-learn==1.3.0
joblib==1.4.0
psycopg==3.2.13
pytest==7.4.0
```

Create a virtual environment with the compatible Python interpreter:

```bash
python3.8 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -m pip check
```

The pure-Python psycopg installation also requires the system PostgreSQL client library (`libpq`). Integration tests require a running PostgreSQL server. Package pins capture the project's working environment; they are not a recommendation to use this older Python version for new deployments.

Keep the saved model beside `score_churn.py`, or pass `--model-path`. Load only trusted joblib files, and retain compatible Python and library versions when loading the model.

## Database setup and migrations

Create a project database, for example `Churn_Predictions`, using pgAdmin or PostgreSQL tools. For a **new empty database**, apply the create_migration_history,baseline, record version 1, then apply migrations 2 and 3:

```bash
psql -h localhost -U postgres -d Churn_Predictions \
  --set=ON_ERROR_STOP=1 \
  --file=migrations/000_create_migration_history.sql

psql -h localhost -U postgres -d Churn_Predictions \
  --set=ON_ERROR_STOP=1 --single-transaction \
  --file=migrations/001_baseline.sql

psql -h localhost -U postgres -d Churn_Predictions \
  --set=ON_ERROR_STOP=1 \
  -c "INSERT INTO schema_migrations (version, name) VALUES (1, 'baseline');"

psql -h localhost -U postgres -d Churn_Predictions \
  --set=ON_ERROR_STOP=1 \
  --file=migrations/002_link_predictions_to_runs.sql

psql -h localhost -U postgres -d Churn_Predictions \
  --set=ON_ERROR_STOP=1 \
  --file=migrations/003_add_skipped_run_status.sql
```

These migration files are applied manually; there is no automatic migration runner. Each later migration groups its schema change and history insert in one transaction. Do not reapply a recorded migration. For an existing populated database, verify schema equivalence before marking a baseline as applied; do not replay its CREATE TABLE statements.

The baseline is schema-only: it does not populate customer snapshots. Load the synthetic source data before running a batch. The notebook contains the worked three-customer pipeline exercise.

Configure authentication locally, for example through `~/.pgpass`:

```text
localhost:5432:Churn_Predictions:postgres:YOUR_LOCAL_PASSWORD
localhost:5432:churn_test:postgres:YOUR_LOCAL_PASSWORD
```

Restrict the password file to its owner (`chmod 600 ~/.pgpass`). Never commit credentials. Connection options also accept `PGHOST`, `PGPORT`, `PGDATABASE`, and `PGUSER` environment variables.

## Run a scoring batch or backfill

```bash
.venv/bin/python score_churn.py \
  --scoring-month 2026-12-01 \
  --dbname Churn_Predictions \
  --user postgres
```

Use the first day of the scoring month. Add `--host`, `--port`, or `--model-path` when needed. The same command can backfill a past month after its snapshots arrive.

The current-month schedule was configured manually with Linux cron for 07:00 on the second day of the month, after source loading. The scheduled command needs absolute interpreter, script, and log paths, plus noninteractive authentication. Manual execution has been verified; an unattended scheduled execution still requires checking its log after the scheduled time. Cron does not create missing monthly snapshots.

## Automated tests

Create a separate database named `churn_test`. The integration fixture checks this database name and creates a unique `churn_it_*` schema per test. It installs an embedded schema template, directs connections and trigger functions to that private schema, and drops the schema at teardown. The test user needs permission to create schemas and their objects.

```bash
.venv/bin/python -m pytest -q \
  test_score_churn.py test_churn_integration.py
```

The suite contains **11 tests**: six validation tests and five integration tests. Coverage includes initial scoring, unchanged reruns, snapshot corrections, ticket insertions/deletions, model-version changes, transactional rollback, and advisory-lock contention. The lock test exercises the scoring function; command-line run-history outcomes were checked manually. Run the integration tests sequentially because monthly advisory locks are shared across schemas within the test database.

Keep the embedded test schema aligned with deployed migrations. Passing these tests does not establish performance or correctness for every possible concurrent workload.

## Monitoring and performance work

SQL exercises monitor failed attempts, unfinished runs, status proportions, recorded durations, latest attempts per month, and snapshot-to-prediction coverage. A requested-month calendar makes missing batches visible. Overdue checks use the scheduled start plus a ten-minute grace period in `Africa/Johannesburg` time.

Coverage measures whether predictions exist; it does not establish freshness or model accuracy. Statuses likewise describe executions rather than guaranteeing complete, current predictions. Monitoring queries provide checks; automatic alert delivery is not implemented.

A separate temporary-table lab explored sequential scans, bitmap scans, selective filters, covering indexes, index-only scans, and partial indexes with `EXPLAIN (ANALYZE, BUFFERS)`. Candidate project indexes remain measurement-driven proposals rather than deployed optimizations.

## Limitations and future improvements

- All model and scoring examples are synthetic; the project is not a production churn system.
- The generator supplies illustrative feature relationships, and fresh evaluation follows that same generating process. Generalization to real customers is untested.
- Training support-ticket and usage semantics must match operational feature definitions before real deployment. The SQL exercise does not establish that alignment with an external source system.
- Late-data rescoring revises historical outputs; it does not recreate what was knowable at the original scoring time. Only the latest prediction per customer-month is retained.
- Timestamp-based invalidation is a learning implementation. Concurrent source transactions and commit ordering require stronger input-version or change-capture guarantees for production use. The monthly advisory lock coordinates scoring jobs, not all source writers.
- Run-history completion is committed separately from predictions. An interruption can leave a Running record even when scoring has committed; reconciliation is necessary.
- A deployed model-version label must change when the model, preprocessing, or threshold changes; the script does not calculate a model fingerprint automatically.

## Skills demonstrated

Python, pandas, scikit-learn pipelines, cost-sensitive classification, nested validation, PostgreSQL, parameterized SQL, upserts, triggers, change logs, incremental processing, backfills, schema migrations, transaction handling, advisory locks, pytest, monitoring queries, and query-plan interpretation.
