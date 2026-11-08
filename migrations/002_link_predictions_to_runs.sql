BEGIN;

ALTER TABLE churn_predictions
ADD COLUMN run_id BIGINT
    REFERENCES churn_pipeline_runs(run_id);

INSERT INTO schema_migrations (version, name)
VALUES (2, 'link_predictions_to_runs');

COMMIT;
