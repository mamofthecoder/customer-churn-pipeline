import os
import secrets
import uuid
from datetime import date
from pathlib import Path

import joblib
import psycopg
from psycopg import sql
import pytest
from score_churn import run_churn_batch, ScoringLockBusy



MONTH = date(2026, 12, 1)


SCHEMA_TEMPLATE = """
CREATE FUNCTION public.log_churn_ticket_change() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        INSERT INTO churn_ticket_change_log (
            ticket_id, operation,
            new_customer_id, new_opened_at
        )
        VALUES (
            NEW.ticket_id, 'INSERT',
            NEW.customer_id, NEW.opened_at
        );
    ELSIF TG_OP = 'DELETE' THEN
        INSERT INTO churn_ticket_change_log (
            ticket_id, operation,
            old_customer_id, old_opened_at
        )
        VALUES (
            OLD.ticket_id, 'DELETE',
            OLD.customer_id, OLD.opened_at
        );
    ELSIF ROW(OLD.customer_id, OLD.opened_at)
          IS DISTINCT FROM
          ROW(NEW.customer_id, NEW.opened_at) THEN
        INSERT INTO churn_ticket_change_log (
            ticket_id, operation,
            old_customer_id, new_customer_id,
            old_opened_at, new_opened_at
        )
        VALUES (
            NEW.ticket_id, 'UPDATE',
            OLD.customer_id, NEW.customer_id,
            OLD.opened_at, NEW.opened_at
        );
    END IF;
    RETURN NULL;
END;
$$;
CREATE FUNCTION public.set_churn_updated_at() RETURNS trigger
    LANGUAGE plpgsql
    AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        NEW.updated_at := CURRENT_TIMESTAMP;
    ELSIF (to_jsonb(NEW) - 'updated_at')
          IS DISTINCT FROM
          (to_jsonb(OLD) - 'updated_at') THEN
        NEW.updated_at := CURRENT_TIMESTAMP;
    ELSE
        NEW.updated_at := OLD.updated_at;
    END IF;
    RETURN NEW;
END;
$$;
CREATE TABLE public.churn_customer_snapshots (
    customer_id integer NOT NULL,
    scoring_month date NOT NULL,
    tenure_months integer NOT NULL,
    monthly_fee numeric(10,2),
    usage_hours double precision,
    contract_type text,
    updated_at timestamp with time zone,
    CONSTRAINT churn_customer_snapshots_contract_type_check CHECK ((contract_type = ANY (ARRAY['Monthly'::text, 'Annual'::text]))),
    CONSTRAINT churn_customer_snapshots_monthly_fee_check CHECK ((monthly_fee >= (0)::numeric)),
    CONSTRAINT churn_customer_snapshots_tenure_months_check CHECK ((tenure_months >= 0)),
    CONSTRAINT churn_customer_snapshots_usage_hours_check CHECK ((usage_hours >= (0)::double precision))
);
CREATE TABLE public.churn_customers (
    customer_id integer NOT NULL
);
CREATE TABLE public.churn_predictions (
    customer_id integer NOT NULL,
    scoring_month text NOT NULL,
    churn_probability real NOT NULL,
    flag_for_review integer NOT NULL,
    scored_at timestamp with time zone,
    model_version text,
    CONSTRAINT churn_predictions_churn_probability_check CHECK (((churn_probability >= (0)::double precision) AND (churn_probability <= (1)::double precision))),
    CONSTRAINT churn_predictions_flag_for_review_check CHECK ((flag_for_review = ANY (ARRAY[0, 1])))
);
CREATE VIEW public.churn_monthly_coverage AS
 SELECT cs.scoring_month,
    cs.contract_type,
    count(*) AS expected_customers,
    count(cp.customer_id) AS scored_customers,
    round(((100.0 * (count(cp.customer_id))::numeric) / (count(*))::numeric), 2) AS coverage_percentage,
    round((100.0 * avg(cp.flag_for_review)), 2) AS flagged_percentage
   FROM (public.churn_customer_snapshots cs
     LEFT JOIN public.churn_predictions cp ON (((cs.customer_id = cp.customer_id) AND (cs.scoring_month = (cp.scoring_month)::date))))
  GROUP BY cs.scoring_month, cs.contract_type;
CREATE TABLE public.churn_pipeline_runs (
    run_id bigint NOT NULL,
    scoring_month date NOT NULL,
    model_version text NOT NULL,
    started_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    finished_at timestamp with time zone,
    status text DEFAULT 'Running'::text NOT NULL,
    customers_scored integer DEFAULT 0 NOT NULL,
    error_message text,
    CONSTRAINT churn_pipeline_runs_customers_scored_check CHECK ((customers_scored >= 0)),
    CONSTRAINT churn_pipeline_runs_status_check CHECK ((status = ANY (ARRAY['Running'::text, 'Succeeded'::text, 'Failed'::text])))
);
ALTER TABLE public.churn_pipeline_runs ALTER COLUMN run_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.churn_pipeline_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
CREATE TABLE public.churn_support_tickets (
    ticket_id integer NOT NULL,
    customer_id integer NOT NULL,
    opened_at date NOT NULL,
    updated_at timestamp with time zone
);
CREATE TABLE public.churn_ticket_change_log (
    change_id bigint NOT NULL,
    ticket_id integer NOT NULL,
    operation text NOT NULL,
    old_customer_id integer,
    new_customer_id integer,
    old_opened_at date,
    new_opened_at date,
    changed_at timestamp with time zone DEFAULT CURRENT_TIMESTAMP NOT NULL,
    CONSTRAINT churn_ticket_change_log_operation_check CHECK ((operation = ANY (ARRAY['INSERT'::text, 'UPDATE'::text, 'DELETE'::text])))
);
ALTER TABLE public.churn_ticket_change_log ALTER COLUMN change_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.churn_ticket_change_log_change_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);
ALTER TABLE ONLY public.churn_customer_snapshots
    ADD CONSTRAINT churn_customer_snapshots_pkey PRIMARY KEY (customer_id, scoring_month);
ALTER TABLE ONLY public.churn_customers
    ADD CONSTRAINT churn_customers_pkey PRIMARY KEY (customer_id);
ALTER TABLE ONLY public.churn_pipeline_runs
    ADD CONSTRAINT churn_pipeline_runs_pkey PRIMARY KEY (run_id);
ALTER TABLE ONLY public.churn_predictions
    ADD CONSTRAINT churn_predictions_pkey PRIMARY KEY (customer_id, scoring_month);
ALTER TABLE ONLY public.churn_support_tickets
    ADD CONSTRAINT churn_support_tickets_pkey PRIMARY KEY (ticket_id);
ALTER TABLE ONLY public.churn_ticket_change_log
    ADD CONSTRAINT churn_ticket_change_log_pkey PRIMARY KEY (change_id);
CREATE TRIGGER trg_snapshot_updated_at BEFORE INSERT OR UPDATE ON public.churn_customer_snapshots FOR EACH ROW EXECUTE FUNCTION public.set_churn_updated_at();
CREATE TRIGGER trg_ticket_change_log AFTER INSERT OR DELETE OR UPDATE ON public.churn_support_tickets FOR EACH ROW EXECUTE FUNCTION public.log_churn_ticket_change();
CREATE TRIGGER trg_ticket_updated_at BEFORE INSERT OR UPDATE ON public.churn_support_tickets FOR EACH ROW EXECUTE FUNCTION public.set_churn_updated_at();
ALTER TABLE ONLY public.churn_customer_snapshots
    ADD CONSTRAINT churn_customer_snapshots_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.churn_customers(customer_id);
ALTER TABLE ONLY public.churn_support_tickets
    ADD CONSTRAINT churn_support_tickets_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.churn_customers(customer_id);
"""

SCHEMA_TEMPLATE += """
ALTER TABLE public.churn_predictions
ADD COLUMN run_id BIGINT
    REFERENCES public.churn_pipeline_runs(run_id);
"""

@pytest.fixture
def test_context():
    """Create a fresh schema per test; never use public pipeline tables."""
    base_settings = {
        "host": os.getenv("PGHOST", "localhost"),
        "port": int(os.getenv("PGPORT", "5432")),
        "dbname": "churn_test",
        "user": os.getenv("PGUSER", "postgres"),
    }
    schema = "churn_it_" + uuid.uuid4().hex
    settings = {
        **base_settings,
        # No public fallback: missing test objects must fail.
        "options": f"-c search_path={schema}",
    }
    customer_id = 9001
    created = False

    try:
        with psycopg.connect(**base_settings) as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT current_database()")
                if cur.fetchone()[0] != "churn_test":
                    raise RuntimeError("Integration tests require churn_test.")

                cur.execute(sql.SQL("CREATE SCHEMA {}").format(
                    sql.Identifier(schema)
                ))
                # uuid.hex ensures the replacement contains identifier-safe text.
                cur.execute(SCHEMA_TEMPLATE.replace("public.", schema + "."))
                cur.execute("SELECT set_config('search_path', %s, false)",
                            (schema,))
                # Pin trigger lookup to the private schema even for other sessions.
                for function_name in (
                    "set_churn_updated_at", "log_churn_ticket_change"
                ):
                    cur.execute(sql.SQL(
                        "ALTER FUNCTION {}.{}() SET search_path TO {}"
                    ).format(sql.Identifier(schema),
                             sql.Identifier(function_name),
                             sql.Identifier(schema)))

                cur.execute("""
                    INSERT INTO churn_customers (customer_id) VALUES (%s)
                """, (customer_id,))
                cur.execute("""
                    INSERT INTO churn_customer_snapshots (
                        customer_id, scoring_month, tenure_months,
                        monthly_fee, usage_hours, contract_type
                    )
                    VALUES (%s, %s, 12, 550, 20, 'Monthly')
                """, (customer_id, MONTH))
        created = True

        bundle = joblib.load(
            Path(__file__).resolve().with_name("customer_churn_model.joblib")
        )
        yield settings, bundle, customer_id

    finally:
        # Failed setup is rolled back; committed test schemas are removed here.
        if created:
            with psycopg.connect(**base_settings) as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT current_database()")
                    if cur.fetchone()[0] != "churn_test":
                        raise RuntimeError("Refusing cleanup outside churn_test.")
                    cur.execute(sql.SQL("DROP SCHEMA {} CASCADE").format(
                        sql.Identifier(schema)
                    ))


def prediction_timestamp(settings, customer_id):
    with psycopg.connect(**settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT scored_at
                FROM churn_predictions
                WHERE customer_id = %s
                  AND CAST(scoring_month AS date) = %s
            """, (customer_id, MONTH))
            rows = cur.fetchall()

    assert len(rows) == 1
    assert rows[0][0] is not None
    return rows[0][0]


def test_score_noop_and_snapshot_change(test_context):
    settings, bundle, customer_id = test_context

    # Initial scoring creates this customer's prediction.
    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored["customer_id"].tolist() == [customer_id]
    first_timestamp = prediction_timestamp(settings, customer_id)

    # An unchanged rerun must leave this prediction untouched.
    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored.empty
    assert prediction_timestamp(settings, customer_id) == first_timestamp

    # A real correction must trigger rescoring.
    with psycopg.connect(**settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE churn_customer_snapshots
                SET usage_hours = 25
                WHERE customer_id = %s AND scoring_month = %s
            """, (customer_id, MONTH))

    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored["customer_id"].tolist() == [customer_id]
    assert prediction_timestamp(settings, customer_id) > first_timestamp

    # The corrected prediction must then remain current.
    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored.empty
    
def test_ticket_insert_and_delete_trigger_rescoring(test_context):
    settings, bundle, customer_id = test_context

    run_churn_batch(MONTH, bundle, settings)

    with psycopg.connect(**settings) as conn:
        with conn.cursor() as cur:
            while True:
                ticket_id = secrets.randbelow(1_000_000_000) + 1_000_000

                cur.execute("""
                    INSERT INTO churn_support_tickets (
                        ticket_id, customer_id, opened_at
                    )
                    VALUES (%s, %s, DATE '2026-11-20')
                    ON CONFLICT (ticket_id) DO NOTHING
                    RETURNING ticket_id
                """, (ticket_id, customer_id))

                if cur.fetchone() is not None:
                    break

    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored["customer_id"].tolist() == [customer_id]
    inserted_timestamp = prediction_timestamp(settings, customer_id)

    with psycopg.connect(**settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                DELETE FROM churn_support_tickets
                WHERE ticket_id = %s AND customer_id = %s
            """, (ticket_id, customer_id))

    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored["customer_id"].tolist() == [customer_id]
    deleted_timestamp = prediction_timestamp(settings, customer_id)
    assert deleted_timestamp > inserted_timestamp

    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored.empty
    assert prediction_timestamp(settings, customer_id) == deleted_timestamp
    
def test_model_version_change_triggers_rescoring(test_context):
    settings, bundle, customer_id = test_context

    run_churn_batch(MONTH, bundle, settings)

    changed_bundle = dict(bundle)
    changed_bundle["model_version"] = (
        f"{bundle['model_version']}_test_{customer_id}"
    )

    scored = run_churn_batch(MONTH, changed_bundle, settings)
    assert scored["customer_id"].tolist() == [customer_id]

    with psycopg.connect(**settings) as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT model_version
                FROM churn_predictions
                WHERE customer_id = %s
                  AND CAST(scoring_month AS date) = %s
            """, (customer_id, MONTH))

            assert cur.fetchone()[0] == changed_bundle["model_version"]

    timestamp = prediction_timestamp(settings, customer_id)

    scored = run_churn_batch(MONTH, changed_bundle, settings)
    assert scored.empty
    assert prediction_timestamp(settings, customer_id) == timestamp
   
def test_prediction_batch_rolls_back_on_invalid_probability(test_context):
    settings, bundle, customer_id = test_context

    with pytest.raises(psycopg.errors.CheckViolation):
        with psycopg.connect(**settings) as conn:
            with conn.cursor() as cur:
                cur.executemany("""
                    INSERT INTO churn_predictions (
                        customer_id, scoring_month,
                        churn_probability, flag_for_review
                    )
                    VALUES (%s, %s, %s, %s)
                """, [
                    (customer_id, MONTH.isoformat(), 0.2, 1),
                    (customer_id, "2027-01-01", 1.5, 1),
                ])

    with psycopg.connect(**settings) as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM churn_predictions")
            assert cur.fetchone()[0] == 0
            
def test_busy_lock_blocks_scoring_then_releases(test_context):
    settings, bundle, customer_id = test_context
    lock_key = MONTH.year * 100 + MONTH.month

    with psycopg.connect(**settings) as lock_conn:
        with lock_conn.cursor() as cur:
            cur.execute(
                "SELECT pg_advisory_xact_lock(%s, %s)",
                (73101, lock_key),
            )

        # A different connection must skip while the lock is held.
        with pytest.raises(ScoringLockBusy):
            run_churn_batch(MONTH, bundle, settings)

        with lock_conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM churn_predictions")
            assert cur.fetchone()[0] == 0

        lock_conn.rollback()

    # Once the lock is released, scoring must proceed.
    scored = run_churn_batch(MONTH, bundle, settings)
    assert scored["customer_id"].tolist() == [customer_id]
    prediction_timestamp(settings, customer_id)
