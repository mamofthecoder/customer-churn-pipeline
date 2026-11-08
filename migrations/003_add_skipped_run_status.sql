BEGIN;

ALTER TABLE churn_pipeline_runs
DROP CONSTRAINT churn_pipeline_runs_status_check;

ALTER TABLE churn_pipeline_runs
ADD CONSTRAINT churn_pipeline_runs_status_check
CHECK (status IN ('Running', 'Succeeded', 'Failed', 'Skipped'));

INSERT INTO schema_migrations (version, name)
VALUES (3, 'add_skipped_run_status');

COMMIT;
