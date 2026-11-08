--
-- PostgreSQL database dump
--

-- Dumped from database version 16.6 (Ubuntu 16.6-1.pgdg22.04+1)
-- Dumped by pg_dump version 17.2 (Ubuntu 17.2-1.pgdg22.04+1)

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SELECT pg_catalog.set_config('search_path', '', false);
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: public; Type: SCHEMA; Schema: -; Owner: -
--



--
-- Name: SCHEMA public; Type: COMMENT; Schema: -; Owner: -
--

COMMENT ON SCHEMA public IS 'standard public schema';


--
-- Name: log_churn_ticket_change(); Type: FUNCTION; Schema: public; Owner: -
--

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


--
-- Name: set_churn_updated_at(); Type: FUNCTION; Schema: public; Owner: -
--

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


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: churn_customer_snapshots; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: churn_customers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.churn_customers (
    customer_id integer NOT NULL
);


--
-- Name: churn_predictions; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: churn_monthly_coverage; Type: VIEW; Schema: public; Owner: -
--

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


--
-- Name: churn_pipeline_runs; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: churn_pipeline_runs_run_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.churn_pipeline_runs ALTER COLUMN run_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.churn_pipeline_runs_run_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: churn_support_tickets; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.churn_support_tickets (
    ticket_id integer NOT NULL,
    customer_id integer NOT NULL,
    opened_at date NOT NULL,
    updated_at timestamp with time zone
);


--
-- Name: churn_ticket_change_log; Type: TABLE; Schema: public; Owner: -
--

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


--
-- Name: churn_ticket_change_log_change_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

ALTER TABLE public.churn_ticket_change_log ALTER COLUMN change_id ADD GENERATED ALWAYS AS IDENTITY (
    SEQUENCE NAME public.churn_ticket_change_log_change_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1
);


--
-- Name: churn_customer_snapshots churn_customer_snapshots_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_customer_snapshots
    ADD CONSTRAINT churn_customer_snapshots_pkey PRIMARY KEY (customer_id, scoring_month);


--
-- Name: churn_customers churn_customers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_customers
    ADD CONSTRAINT churn_customers_pkey PRIMARY KEY (customer_id);


--
-- Name: churn_pipeline_runs churn_pipeline_runs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_pipeline_runs
    ADD CONSTRAINT churn_pipeline_runs_pkey PRIMARY KEY (run_id);


--
-- Name: churn_predictions churn_predictions_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_predictions
    ADD CONSTRAINT churn_predictions_pkey PRIMARY KEY (customer_id, scoring_month);


--
-- Name: churn_support_tickets churn_support_tickets_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_support_tickets
    ADD CONSTRAINT churn_support_tickets_pkey PRIMARY KEY (ticket_id);


--
-- Name: churn_ticket_change_log churn_ticket_change_log_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_ticket_change_log
    ADD CONSTRAINT churn_ticket_change_log_pkey PRIMARY KEY (change_id);


--
-- Name: churn_customer_snapshots trg_snapshot_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_snapshot_updated_at BEFORE INSERT OR UPDATE ON public.churn_customer_snapshots FOR EACH ROW EXECUTE FUNCTION public.set_churn_updated_at();


--
-- Name: churn_support_tickets trg_ticket_change_log; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_ticket_change_log AFTER INSERT OR DELETE OR UPDATE ON public.churn_support_tickets FOR EACH ROW EXECUTE FUNCTION public.log_churn_ticket_change();


--
-- Name: churn_support_tickets trg_ticket_updated_at; Type: TRIGGER; Schema: public; Owner: -
--

CREATE TRIGGER trg_ticket_updated_at BEFORE INSERT OR UPDATE ON public.churn_support_tickets FOR EACH ROW EXECUTE FUNCTION public.set_churn_updated_at();


--
-- Name: churn_customer_snapshots churn_customer_snapshots_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_customer_snapshots
    ADD CONSTRAINT churn_customer_snapshots_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.churn_customers(customer_id);


--
-- Name: churn_support_tickets churn_support_tickets_customer_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.churn_support_tickets
    ADD CONSTRAINT churn_support_tickets_customer_id_fkey FOREIGN KEY (customer_id) REFERENCES public.churn_customers(customer_id);


--
-- PostgreSQL database dump complete
--

