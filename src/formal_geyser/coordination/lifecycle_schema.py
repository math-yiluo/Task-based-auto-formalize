"""Additive, restart-safe coordination metadata for temporary task membership."""
from __future__ import annotations

LIFECYCLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS project_lifecycle(
 id INTEGER PRIMARY KEY CHECK(id=1),root_task_id TEXT,state TEXT NOT NULL DEFAULT 'running',
 shutdown_reason TEXT);
INSERT OR IGNORE INTO project_lifecycle(id) VALUES(1);
CREATE TABLE IF NOT EXISTS task_lifecycle(
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),initial_worker_id TEXT,
 recruitment_decision TEXT NOT NULL DEFAULT 'pending',opportunity_state TEXT NOT NULL DEFAULT 'closed',
 invitation TEXT NOT NULL DEFAULT '',opportunity_opened_at INTEGER,first_turn_id TEXT,
 decision_helper_id TEXT,suspended INTEGER NOT NULL DEFAULT 0,closed_reason TEXT);
-- The operator's place for a task in admission, apart from its contract (task.set_priority,
-- task.set_pinned): a priority in place of the contract's, and since when it is pinned first.
CREATE TABLE IF NOT EXISTS task_admission(
 task_id TEXT PRIMARY KEY REFERENCES tasks(id),priority INTEGER CHECK(priority BETWEEN 0 AND 3),
 pinned_at INTEGER,updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS helper_intents(
 id TEXT PRIMARY KEY,kind TEXT NOT NULL,source_worker_id TEXT NOT NULL REFERENCES workers(id),
 source_task_id TEXT,run_id TEXT NOT NULL REFERENCES runs(id),source_turn_id TEXT,
 candidates_json TEXT NOT NULL DEFAULT '[]',state TEXT NOT NULL,
 thread_id TEXT,remote_turn_id TEXT,raw_result_json TEXT,result_json TEXT,reason TEXT,usage_json TEXT,
 created_at INTEGER NOT NULL,ended_at INTEGER,inference_uncertain INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS helper_usage_samples(
 helper_id TEXT NOT NULL REFERENCES helper_intents(id),sample_id TEXT NOT NULL,delta_tokens INTEGER NOT NULL,
 sample_json TEXT NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(helper_id,sample_id));
CREATE TABLE IF NOT EXISTS native_usage_samples(
 worker_id TEXT NOT NULL REFERENCES workers(id),epoch INTEGER NOT NULL,sha256 TEXT NOT NULL,
 sample_json TEXT NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(worker_id,epoch));
CREATE UNIQUE INDEX IF NOT EXISTS one_live_assignment_per_task ON helper_intents(source_task_id)
 WHERE kind='assignment' AND state IN ('prepared','running','uncertain');
-- A task's decisions by state and time: admission finds each task's latest one on every pass,
-- which without it read every decision again for each one (1.3 s for 1,733 in the Linux pilot).
CREATE INDEX IF NOT EXISTS helper_intent_task_idx ON helper_intents(kind,source_task_id,state,created_at);
CREATE UNIQUE INDEX IF NOT EXISTS one_dispatcher ON workers(kind) WHERE kind='dispatcher';
CREATE TABLE IF NOT EXISTS decision_preferences(
 id INTEGER PRIMARY KEY CHECK(id=1),model TEXT NOT NULL,effort TEXT NOT NULL,updated_at INTEGER NOT NULL);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_recruitment_choice ON helper_intents(source_task_id)
 WHERE kind='recruitment' AND state<>'cancelled';
CREATE TABLE IF NOT EXISTS worker_wake_messages(
 worker_id TEXT NOT NULL REFERENCES workers(id),message_id TEXT NOT NULL,created_at INTEGER NOT NULL,
 PRIMARY KEY(worker_id,message_id));
CREATE TABLE IF NOT EXISTS task_revision_history(
 task_id TEXT NOT NULL,revision INTEGER NOT NULL,previous_revision INTEGER NOT NULL,
 author_id TEXT NOT NULL,reason TEXT NOT NULL,material INTEGER NOT NULL,created_at INTEGER NOT NULL,
 PRIMARY KEY(task_id,revision));
CREATE TABLE IF NOT EXISTS task_completion_declarations(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),task_revision INTEGER NOT NULL,
 original_contract_hash TEXT NOT NULL,declarer_id TEXT NOT NULL,
 acceptance_json TEXT NOT NULL,evidence_json TEXT NOT NULL,
 feedback_worker_id TEXT,feedback_state TEXT NOT NULL,feedback_json TEXT,
 state TEXT NOT NULL,decision_reason TEXT,resolution_json TEXT,created_at INTEGER NOT NULL,decided_at INTEGER);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending_completion ON task_completion_declarations(task_id)
 WHERE state='pending';
CREATE TABLE IF NOT EXISTS completion_drains(
 declaration_id TEXT PRIMARY KEY REFERENCES task_completion_declarations(id),
 snapshot_json TEXT NOT NULL,state TEXT NOT NULL DEFAULT 'draining');
CREATE TABLE IF NOT EXISTS completion_evidence_invalidations(
 id TEXT PRIMARY KEY,task_id TEXT NOT NULL REFERENCES tasks(id),task_revision INTEGER NOT NULL,
 declaration_id TEXT NOT NULL REFERENCES task_completion_declarations(id),
 artifact_ids_json TEXT NOT NULL,observations_json TEXT NOT NULL,
 repair_task_id TEXT NOT NULL UNIQUE REFERENCES tasks(id),created_at INTEGER NOT NULL);
CREATE INDEX IF NOT EXISTS completion_invalidation_declaration ON completion_evidence_invalidations(declaration_id);
CREATE TABLE IF NOT EXISTS artifact_obligations(
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),task_id TEXT NOT NULL,
 predecessor_id TEXT REFERENCES artifacts(id),required INTEGER NOT NULL DEFAULT 1,
 withdrawn_reason TEXT,withdrawn_at INTEGER);
-- The correction of a candidate: requirements follow it to an artifact's newest version.
CREATE INDEX IF NOT EXISTS artifact_obligation_predecessor ON artifact_obligations(predecessor_id);
CREATE TABLE IF NOT EXISTS candidate_provenance(
 artifact_id TEXT PRIMARY KEY REFERENCES artifacts(id),worker_ids_json TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS review_recovery_history(
 id TEXT PRIMARY KEY,review_task_id TEXT NOT NULL,worker_id TEXT NOT NULL,reason TEXT NOT NULL,
 snapshot TEXT,prior_acceptance_json TEXT,created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS worker_imported_tasks(
 worker_id TEXT NOT NULL REFERENCES workers(id),position INTEGER NOT NULL,title TEXT NOT NULL,
 PRIMARY KEY(worker_id,position));
"""

