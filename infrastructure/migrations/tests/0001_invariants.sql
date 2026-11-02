-- Schema invariants for 0001_init.sql. Every block raises if the invariant does NOT hold.
-- Runs inside a transaction that is rolled back. Usage: psql -v ON_ERROR_STOP=1 -f this_file
BEGIN;

INSERT INTO organizations (id, name) VALUES ('org_a', 'A'), ('org_b', 'B');
INSERT INTO projects (id, organization_id, name) VALUES ('prj_a', 'org_a', 'p'), ('prj_b', 'org_b', 'p');
INSERT INTO agents (id, organization_id, project_id, name) VALUES ('agt_a', 'org_a', 'prj_a', 'support');
INSERT INTO agent_versions (id, organization_id, agent_id, version, spec, spec_digest,
                            artifact_digest, runtime_compat, integration_level)
VALUES ('agtv_a', 'org_a', 'agt_a', '1.0.0', '{}', 'sha256:s', 'sha256:a', '>=0.1', 3);
INSERT INTO executions (id, organization_id, agent_id, agent_version_id, status, integration_level, input_ref, budget)
VALUES ('run_a', 'org_a', 'agt_a', 'agtv_a', 'QUEUED', 3, 'blob://in', '{}');
INSERT INTO execution_events (organization_id, execution_id, sequence, event_id, type, is_transition, lease_generation)
VALUES ('org_a', 'run_a', 1, 'evt_1', 'run.accepted', true, 0);
INSERT INTO execution_tasks (id, organization_id, execution_id, kind, state)
VALUES ('tsk_1', 'org_a', 'run_a', 'run', 'ready');

CREATE FUNCTION pg_temp.expect_failure(stmt text, label text) RETURNS void LANGUAGE plpgsql AS $$
BEGIN
  BEGIN
    EXECUTE stmt;
  EXCEPTION WHEN OTHERS THEN
    RAISE NOTICE 'ok: %', label;
    RETURN;
  END;
  RAISE EXCEPTION 'INVARIANT VIOLATED: % (statement succeeded: %)', label, stmt;
END $$;

SELECT pg_temp.expect_failure(
  $q$INSERT INTO agents (id, organization_id, project_id, name) VALUES ('agt_x', 'org_b', 'prj_a', 'x')$q$,
  'cross-tenant parent reference rejected');
SELECT pg_temp.expect_failure($q$UPDATE agent_versions SET version = '2' WHERE id = 'agtv_a'$q$,
  'agent versions immutable');
SELECT pg_temp.expect_failure($q$UPDATE execution_events SET type = 'x'$q$, 'journal not updatable');
SELECT pg_temp.expect_failure($q$DELETE FROM execution_events$q$, 'journal not deletable');
SELECT pg_temp.expect_failure(
  $q$INSERT INTO execution_tasks (id, organization_id, execution_id, kind, state) VALUES ('tsk_2', 'org_a', 'run_a', 'run', 'ready')$q$,
  'at most one live task per execution');
SELECT pg_temp.expect_failure($q$UPDATE executions SET status = 'COMPLETED' WHERE id = 'run_a'$q$,
  'terminal status requires finished_at');
SELECT pg_temp.expect_failure(
  $q$INSERT INTO execution_events (organization_id, execution_id, sequence, event_id, type, is_transition, lease_generation) VALUES ('org_a', 'run_a', 1, 'evt_2', 'x', false, 0)$q$,
  'duplicate sequence rejected');
SELECT pg_temp.expect_failure(
  $q$INSERT INTO tool_definitions (id, organization_id, project_id, name, version, effect, input_schema, digest, credentials_ref) VALUES ('tool_1', 'org_a', 'prj_a', 't', '1', 'read', '{}', 'd', 'sk-raw-secret')$q$,
  'raw credentials rejected');

-- RLS: tenant B sees nothing of tenant A; missing tenant setting sees nothing.
SET LOCAL ROLE mait_app;
SET LOCAL mait.organization_id = 'org_b';
DO $$ BEGIN
  IF (SELECT count(*) FROM executions) <> 0 THEN RAISE EXCEPTION 'INVARIANT VIOLATED: RLS leak'; END IF;
END $$;
SELECT pg_temp.expect_failure(
  $q$INSERT INTO agents (id, organization_id, project_id, name) VALUES ('agt_y', 'org_a', 'prj_a', 'y')$q$,
  'RLS blocks writes into another tenant');
SET LOCAL mait.organization_id = '';
DO $$ BEGIN
  IF (SELECT count(*) FROM agents) <> 0 THEN RAISE EXCEPTION 'INVARIANT VIOLATED: RLS without tenant'; END IF;
END $$;
SET LOCAL mait.organization_id = 'org_a';
DO $$ BEGIN
  IF (SELECT count(*) FROM executions) <> 1 THEN RAISE EXCEPTION 'INVARIANT VIOLATED: owner cannot see own rows'; END IF;
END $$;
RESET ROLE;

ROLLBACK;
