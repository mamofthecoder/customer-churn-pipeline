"""Score one month of PostgreSQL snapshots with the saved churn model."""
import argparse
import os
from datetime import date
from pathlib import Path
import logging

batch_sql = """
WITH scoring_params AS (
    SELECT CAST(%s AS date) AS scoring_date
),
affected_ticket_events AS (
    SELECT
        old_customer_id AS customer_id,
        old_opened_at AS opened_at,
        changed_at
    FROM churn_ticket_change_log
    WHERE old_customer_id IS NOT NULL

    UNION ALL

    SELECT
        new_customer_id,
        new_opened_at,
        changed_at
    FROM churn_ticket_change_log
    WHERE new_customer_id IS NOT NULL
),
ticket_changes AS (
    SELECT
        e.customer_id,
        MAX(e.changed_at) AS latest_ticket_change
    FROM affected_ticket_events AS e
    CROSS JOIN scoring_params AS p
    WHERE e.opened_at >= p.scoring_date - 30
      AND e.opened_at < p.scoring_date
    GROUP BY e.customer_id
),
ticket_counts AS (
    SELECT
        st.customer_id,
        COUNT(*) AS support_tickets
    FROM churn_support_tickets AS st
    CROSS JOIN scoring_params AS p
    WHERE st.opened_at >= p.scoring_date - 30
      AND st.opened_at < p.scoring_date
    GROUP BY st.customer_id
)
SELECT
    cs.customer_id,
    cs.scoring_month,
    cs.tenure_months AS "Tenure_Months",
    cs.monthly_fee::double precision AS "Monthly_Fee",
    COALESCE(t.support_tickets, 0) AS "Support_Tickets",
    cs.usage_hours AS "Usage_Hours",
    cs.contract_type AS "Contract_Type"
FROM churn_customer_snapshots AS cs
JOIN scoring_params AS p
    ON cs.scoring_month = p.scoring_date
LEFT JOIN ticket_counts AS t
    ON t.customer_id = cs.customer_id
LEFT JOIN ticket_changes AS tc
    ON tc.customer_id = cs.customer_id
LEFT JOIN churn_predictions AS cp
    ON cp.customer_id = cs.customer_id
   AND CAST(cp.scoring_month AS date) = cs.scoring_month
WHERE cp.customer_id IS NULL
   OR cp.model_version IS DISTINCT FROM %s
   OR cp.scored_at IS NULL
   OR COALESCE(
       GREATEST(cs.updated_at, tc.latest_ticket_change) > cp.scored_at,
       FALSE
   )
ORDER BY cs.customer_id;
"""

snapshot_count_sql = """
SELECT COUNT(*)
FROM churn_customer_snapshots
WHERE scoring_month = CAST(%s AS date);
"""

upsert_sql = """
INSERT INTO churn_predictions (
    customer_id, scoring_month, churn_probability,
    flag_for_review, scored_at, model_version, run_id
)
VALUES (%s, %s, %s, %s, CURRENT_TIMESTAMP, %s, %s)
ON CONFLICT (customer_id, scoring_month) DO UPDATE SET
    churn_probability = EXCLUDED.churn_probability,
    flag_for_review = EXCLUDED.flag_for_review,
    scored_at = EXCLUDED.scored_at,
    model_version = EXCLUDED.model_version,
    run_id = EXCLUDED.run_id;
"""
def validate_batch(batch):
    import numpy as np
    import pandas as pd

    batch = batch.copy()

    numeric_columns = [
        "Tenure_Months",
        "Monthly_Fee",
        "Support_Tickets",
        "Usage_Hours",
    ]
    required_columns = ["Tenure_Months", "Support_Tickets"]
    keys = ["customer_id", "scoring_month"]
    expected_columns = keys + numeric_columns + ["Contract_Type"]

    missing_columns = [
        column for column in expected_columns
        if column not in batch.columns
    ]
    if missing_columns:
        raise ValueError(f"Missing columns: {missing_columns}")

    if batch[keys].isna().any().any():
        raise ValueError("Customer ID and scoring month cannot be missing.")

    if batch.duplicated(keys).any():
        raise ValueError("Expected one row per customer per scoring month.")

    for column in numeric_columns:
        values = pd.to_numeric(batch[column], errors="raise")
        invalid = values.notna() & (
            (values < 0) | ~np.isfinite(values)
        )

        if invalid.any():
            ids = batch.loc[invalid, "customer_id"].tolist()
            raise ValueError(f"Invalid {column} for customer IDs: {ids}")

        batch[column] = values

    for column in required_columns:
        missing = batch[column].isna()
        if missing.any():
            ids = batch.loc[missing, "customer_id"].tolist()
            raise ValueError(
                f"Missing required {column} for customer IDs: {ids}"
            )

        fractional = batch[column].mod(1).ne(0)
        if fractional.any():
            ids = batch.loc[fractional, "customer_id"].tolist()
            raise ValueError(
                f"{column} must contain whole numbers; "
                f"invalid customer IDs: {ids}"
            )

    invalid_contract = (
        batch["Contract_Type"].notna()
        & ~batch["Contract_Type"].isin({"Monthly", "Annual"})
    )
    if invalid_contract.any():
        ids = batch.loc[invalid_contract, "customer_id"].tolist()
        raise ValueError(
            f"Invalid Contract_Type for customer IDs: {ids}"
        )

    return batch

class ScoringLockBusy(Exception):
    """Another execution is already scoring this month."""


def run_churn_batch(scoring_date, loaded_bundle, connection_settings, run_id=None):
    import pandas as pd
    import psycopg

    if scoring_date.day != 1:
        raise ValueError("Use the first day of the scoring month.")

    with psycopg.connect(**connection_settings) as conn:
        with conn.cursor() as cur:
            lock_key = scoring_date.year * 100 + scoring_date.month

            cur.execute(
                "SELECT pg_try_advisory_xact_lock(%s, %s)",
                (73101, lock_key),
            )
            lock_acquired = cur.fetchone()[0]

            if not lock_acquired:
                raise ScoringLockBusy(
                    f"Another job is already scoring {scoring_date}"
                )

            cur.execute(snapshot_count_sql, (scoring_date,))
            snapshot_count = cur.fetchone()[0]

            if snapshot_count == 0:
                raise ValueError(
                    f"No customer snapshots found for {scoring_date}."
                )

            model_version = loaded_bundle["model_version"]

            cur.execute(batch_sql, (scoring_date, model_version))
            batch = pd.DataFrame(
                cur.fetchall(),
                columns=[column.name for column in cur.description],
            )

            keys = ["customer_id", "scoring_month"]

            if batch.empty:
                logging.getLogger(__name__).info(
                    "No customers need scoring for %s", scoring_date
                )
                return pd.DataFrame(columns=[
                    "customer_id",
                    "scoring_month",
                    "churn_probability",
                    "flag_for_review",
                ])

            batch = validate_batch(batch)

            keys = ["customer_id", "scoring_month"]
            features = batch[loaded_bundle["feature_columns"]]
            probabilities = loaded_bundle["pipeline"].predict_proba(
                features
            )[:, 1]
            threshold = loaded_bundle["threshold"]

            scored = batch[keys].copy()
            scored["churn_probability"] = probabilities
            scored["flag_for_review"] = (
                probabilities >= threshold
            ).astype(int)
            rows = [
                (
                    int(customer_id),
                    month,
                    float(probability),
                    int(flag),
                    model_version,
                    run_id,
                )
                for customer_id, month, probability, flag
                in scored.itertuples(index=False, name=None)
            ]
            cur.executemany(upsert_sql, rows)

    return scored

def start_run(scoring_date, model_version, connection_settings):
    import psycopg

    with psycopg.connect(**connection_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO churn_pipeline_runs (
                    scoring_month, model_version
                )
                VALUES (%s, %s)
                RETURNING run_id;
            """, (scoring_date, model_version))

            run_id = cur.fetchone()[0]

    return run_id


def finish_run(
    run_id, status, customers_scored, error_message, connection_settings
):
    import psycopg

    with psycopg.connect(**connection_settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE churn_pipeline_runs
                SET status = %s,
                    finished_at = clock_timestamp(),
                    customers_scored = %s,
                    error_message = %s
                WHERE run_id = %s;
            """, (status, customers_scored, error_message, run_id))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scoring-month", required=True, type=date.fromisoformat)
    parser.add_argument("--model-path", type=Path,
                        default=Path(__file__).resolve().with_name("customer_churn_model.joblib"))
    parser.add_argument("--host", default=os.getenv("PGHOST", "localhost"))
    parser.add_argument("--port", type=int, default=os.getenv("PGPORT", "5432"))
    parser.add_argument("--dbname", default=os.getenv("PGDATABASE"))
    parser.add_argument("--user", default=os.getenv("PGUSER"))
    args = parser.parse_args()
    if args.scoring_month.day != 1:
        parser.error("--scoring-month must be the first day of the month")
    if not args.dbname or not args.user:
        parser.error("provide --dbname and --user, or set PGDATABASE and PGUSER")
    if not args.model_path.is_file():
        parser.error(f"model bundle not found: {args.model_path}")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )
    logger = logging.getLogger(__name__)

    logger.info("Starting churn batch for %s", args.scoring_month)

    connection_settings = {
        "host": args.host,
        "port": args.port,
        "dbname": args.dbname,
        "user": args.user,
    }
    run_id = None

    try:
        import joblib

        bundle = joblib.load(args.model_path)

        run_id = start_run(
            args.scoring_month,
            bundle["model_version"],
            connection_settings,
        )
        logger.info("Started run %s", run_id)

        scored = run_churn_batch(
            args.scoring_month,
            bundle,
            connection_settings,
            run_id=run_id,
        )

    except ScoringLockBusy as exc:
        logger.info("Skipping run %s: %s", run_id, exc)

        try:
            finish_run(
                run_id, "Skipped", 0, None, connection_settings
            )
        except Exception:
            logger.exception(
                "Could not record skipped status for run %s", run_id
            )
            raise SystemExit(1)

        return

    except Exception as exc:
        logger.exception("Churn batch failed for %s", args.scoring_month)

        if run_id is not None:
            try:
                finish_run(
                    run_id, "Failed", 0, str(exc), connection_settings
                )
            except Exception:
                logger.exception(
                    "Could not record failure for run %s", run_id
                )

        raise SystemExit(1)

    # Scoring has committed. Record completion separately.
    try:
        finish_run(
            run_id, "Succeeded", len(scored), None, connection_settings
        )
    except Exception:
        logger.exception(
            "Scoring completed, but completion could not be recorded "
            "for run %s",
            run_id,
        )
        raise SystemExit(1)

    logger.info(
        "Run %s succeeded: %d customers scored, %d flagged for review",
        run_id,
        len(scored),
        scored["flag_for_review"].sum(),
    )

if __name__ == "__main__":
    main()
