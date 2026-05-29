from __future__ import annotations

import json
import hashlib
import sqlite3
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .schema import MemoryCandidate
from .security import redact_secrets
from .taxonomy import enrich_candidate, near_duplicate_text


_WIPE_TABLES = (
    "runtime_trace_links",
    "runtime_trace_events",
    "runtime_trace_spans",
    "runtime_traces",
    "runtime_state_transitions",
    "cognitive_edges",
    "cognitive_records",
    "governance_state",
    "governance_policies",
    "governance_reports",
    "memory_edges",
    "recall_events",
    "events",
    "memories",
)

RUNTIME_SKILL_GOVERNANCE_MIGRATION_VERSION = 2
RUNTIME_SKILL_GOVERNANCE_MIGRATION_NAME = "runtime_skill_governance_shape"
RUNTIME_TRACE_MIGRATION_VERSION = 3
RUNTIME_TRACE_MIGRATION_NAME = "runtime_trace_flow_monitor"


class Ledger:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(path), timeout=60)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout=60000")
        self._transaction_depth = 0
        self._init_with_retry()

    def close(self) -> None:
        self.conn.close()

    def _init_with_retry(self) -> None:
        for attempt in range(5):
            try:
                self._init()
                return
            except sqlite3.OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 4:
                    raise
                time.sleep(0.2 * (attempt + 1))

    def _init(self) -> None:
        self.conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS memories (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                content TEXT NOT NULL,
                memory_type TEXT NOT NULL,
                scope TEXT NOT NULL,
                ttl TEXT NOT NULL,
                confidence REAL NOT NULL,
                importance REAL NOT NULL,
                domain TEXT,
                category TEXT,
                subcategory TEXT,
                abstraction_level TEXT,
                triggers_json TEXT NOT NULL DEFAULT '[]',
                project_key TEXT,
                source_session_id TEXT,
                recall_count INTEGER NOT NULL DEFAULT 0,
                positive_recall_count INTEGER NOT NULL DEFAULT 0,
                negative_recall_count INTEGER NOT NULL DEFAULT 0,
                last_recalled_at TEXT,
                strength REAL NOT NULL DEFAULT 1.0,
                evidence_json TEXT NOT NULL,
                reason TEXT,
                review_json TEXT NOT NULL DEFAULT '{}',
                supersedes_id TEXT,
                expires_at TEXT,
                review_feedback_json TEXT NOT NULL DEFAULT '[]',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS recall_events (
                id TEXT PRIMARY KEY,
                prompt TEXT NOT NULL,
                route_json TEXT NOT NULL,
                memory_ids_json TEXT NOT NULL,
                cwd TEXT,
                project_key TEXT,
                session_id TEXT,
                turn_id TEXT,
                outcome_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS memory_edges (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id, target_id, relation)
            );

            CREATE TABLE IF NOT EXISTS governance_reports (
                id TEXT PRIMARY KEY,
                report_json TEXT NOT NULL,
                actions_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS governance_policies (
                id TEXT PRIMARY KEY,
                policy_type TEXT NOT NULL,
                matcher_json TEXT NOT NULL,
                action TEXT NOT NULL,
                reason TEXT NOT NULL,
                source_memory_id TEXT,
                active INTEGER NOT NULL DEFAULT 1,
                hit_count INTEGER NOT NULL DEFAULT 0,
                last_hit_at TEXT,
                expires_at TEXT,
                supersedes_id TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS governance_state (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                created_at TEXT NOT NULL,
                processed_at TEXT,
                process_error TEXT
            );

            CREATE TABLE IF NOT EXISTS cognitive_records (
                id TEXT PRIMARY KEY,
                layer TEXT NOT NULL,
                record_type TEXT NOT NULL,
                source_id TEXT,
                source_kind TEXT,
                status TEXT NOT NULL,
                scope TEXT NOT NULL,
                content TEXT NOT NULL,
                domain TEXT,
                category TEXT,
                subcategory TEXT,
                confidence REAL NOT NULL DEFAULT 0,
                importance REAL NOT NULL DEFAULT 0,
                strength REAL NOT NULL DEFAULT 1,
                project_key TEXT,
                session_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(layer, record_type, source_id)
            );

            CREATE TABLE IF NOT EXISTS cognitive_edges (
                id TEXT PRIMARY KEY,
                source_id TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                weight REAL NOT NULL,
                evidence_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                UNIQUE(source_id,target_id,relation)
            );

            CREATE TABLE IF NOT EXISTS runtime_state_transitions (
                id TEXT PRIMARY KEY,
                subject_type TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                state TEXT NOT NULL,
                previous_state TEXT,
                event_id TEXT,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS runtime_traces (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                turn_id TEXT,
                cwd TEXT,
                project_key TEXT,
                prompt_sha256 TEXT,
                prompt_preview TEXT,
                prompt_chars INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                updated_at TEXT NOT NULL,
                root_event_id TEXT,
                runtime_skill_injection_id TEXT,
                workflow_id TEXT,
                final_outcome TEXT,
                error_count INTEGER NOT NULL DEFAULT 0,
                warning_count INTEGER NOT NULL DEFAULT 0,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runtime_trace_spans (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                parent_span_id TEXT,
                name TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                ended_at TEXT,
                duration_ms INTEGER,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runtime_trace_events (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                span_id TEXT,
                name TEXT NOT NULL,
                severity TEXT NOT NULL DEFAULT 'info',
                status TEXT,
                message TEXT,
                subject_type TEXT,
                subject_id TEXT,
                session_id TEXT,
                turn_id TEXT,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}'
            );

            CREATE TABLE IF NOT EXISTS runtime_trace_links (
                id TEXT PRIMARY KEY,
                trace_id TEXT NOT NULL,
                target_type TEXT NOT NULL,
                target_id TEXT NOT NULL,
                relation TEXT NOT NULL,
                created_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL DEFAULT '{}',
                UNIQUE(trace_id, target_type, target_id, relation)
            );

            CREATE TABLE IF NOT EXISTS schema_migrations (
                version INTEGER PRIMARY KEY,
                name TEXT NOT NULL,
                applied_at TEXT NOT NULL
            );
            """
        )
        self._record_schema_baseline()
        self._ensure_event_columns()
        self._ensure_memory_columns()
        self._ensure_recall_event_columns()
        self._ensure_cognitive_record_columns()
        self._ensure_indexes()
        self._ensure_runtime_trace_columns()
        self.run_runtime_skill_governance_migration()
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (RUNTIME_TRACE_MIGRATION_VERSION, RUNTIME_TRACE_MIGRATION_NAME, _now()),
        )
        self._commit()

    @contextmanager
    def transaction(self):
        outermost = self._transaction_depth == 0
        self._transaction_depth += 1
        try:
            yield
            self._transaction_depth -= 1
            if outermost:
                self.conn.commit()
        except Exception:
            self._transaction_depth -= 1
            if outermost:
                self.conn.rollback()
            raise

    def _commit(self) -> None:
        if self._transaction_depth == 0:
            self.conn.commit()

    def _record_schema_baseline(self) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
            (1, "baseline_ledger_cognitive_runtime", _now()),
        )

    def run_runtime_skill_governance_migration(self) -> dict[str, Any]:
        counts = {
            "legacy_runtime_skill_records": 0,
            "runtime_skill_shape_updates": 0,
            "seed_status_normalized": 0,
        }
        rows = self.conn.execute(
            """
            SELECT id,layer,record_type,status,metadata_json
            FROM cognitive_records
            WHERE (layer='audit' AND record_type IN ('runtime_skill_injection','runtime_skill_feedback'))
               OR (layer='runtime_skill' AND record_type IN ('injection','feedback'))
               OR (layer='skill' AND record_type='seed_skill')
            """
        ).fetchall()
        with self.transaction():
            for row in rows:
                record_id = str(row["id"])
                layer = str(row["layer"] or "")
                record_type = str(row["record_type"] or "")
                status = str(row["status"] or "")
                metadata = _json_obj(row["metadata_json"])
                if layer == "audit" and record_type in {"runtime_skill_injection", "runtime_skill_feedback"}:
                    new_type = "injection" if record_type == "runtime_skill_injection" else "feedback"
                    metadata.setdefault("shape_version", 2)
                    self.conn.execute(
                        "UPDATE cognitive_records SET layer='runtime_skill', record_type=?, metadata_json=?, updated_at=? WHERE id=?",
                        (new_type, json.dumps(metadata, ensure_ascii=False), _now(), record_id),
                    )
                    counts["legacy_runtime_skill_records"] += 1
                    continue
                if layer == "runtime_skill" and record_type in {"injection", "feedback"} and metadata.get("shape_version") != 2:
                    metadata["shape_version"] = 2
                    self.conn.execute(
                        "UPDATE cognitive_records SET metadata_json=?, updated_at=? WHERE id=?",
                        (json.dumps(metadata, ensure_ascii=False), _now(), record_id),
                    )
                    counts["runtime_skill_shape_updates"] += 1
                    continue
                if layer == "skill" and record_type == "seed_skill":
                    normalized_status, normalized_metadata = _normalized_seed_skill_state(status, metadata)
                    if normalized_status != status or normalized_metadata != metadata:
                        self.conn.execute(
                            "UPDATE cognitive_records SET status=?, metadata_json=?, updated_at=? WHERE id=?",
                            (normalized_status, json.dumps(normalized_metadata, ensure_ascii=False), _now(), record_id),
                        )
                        counts["seed_status_normalized"] += 1
            self.conn.execute(
                "INSERT OR IGNORE INTO schema_migrations(version,name,applied_at) VALUES(?,?,?)",
                (RUNTIME_SKILL_GOVERNANCE_MIGRATION_VERSION, RUNTIME_SKILL_GOVERNANCE_MIGRATION_NAME, _now()),
            )
        return {"version": RUNTIME_SKILL_GOVERNANCE_MIGRATION_VERSION, "name": RUNTIME_SKILL_GOVERNANCE_MIGRATION_NAME, "counts": counts}

    def runtime_skill_governance_migration_status(self) -> dict[str, Any]:
        migration = self.conn.execute(
            "SELECT version,name,applied_at FROM schema_migrations WHERE version=?",
            (RUNTIME_SKILL_GOVERNANCE_MIGRATION_VERSION,),
        ).fetchone()
        legacy_count = self.conn.execute(
            "SELECT COUNT(*) FROM cognitive_records WHERE layer='audit' AND record_type IN ('runtime_skill_injection','runtime_skill_feedback')"
        ).fetchone()[0]
        conflict_count = 0
        rows = self.conn.execute(
            "SELECT status,metadata_json FROM cognitive_records WHERE layer='skill' AND record_type='seed_skill'"
        ).fetchall()
        for row in rows:
            status = str(row["status"] or "")
            trust_state = str(_json_obj(row["metadata_json"]).get("trust_state") or "")
            if (status == "active" and trust_state in {"suppressed", "disabled"}) or (status == "suppressed" and trust_state != "suppressed") or (status == "deprecated" and trust_state not in {"disabled", "deprecated"}):
                conflict_count += 1
        return {
            "version": RUNTIME_SKILL_GOVERNANCE_MIGRATION_VERSION,
            "name": RUNTIME_SKILL_GOVERNANCE_MIGRATION_NAME,
            "applied": migration is not None,
            "applied_at": migration["applied_at"] if migration else None,
            "legacy_runtime_skill_records": int(legacy_count),
            "seed_status_trust_conflicts": conflict_count,
            "ok": migration is not None and int(legacy_count) == 0 and conflict_count == 0,
        }

    def runtime_trace_migration_status(self) -> dict[str, Any]:
        migration = self.conn.execute(
            "SELECT version,name,applied_at FROM schema_migrations WHERE version=?",
            (RUNTIME_TRACE_MIGRATION_VERSION,),
        ).fetchone()
        tables = {
            row["name"]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        required = {"runtime_traces", "runtime_trace_spans", "runtime_trace_events", "runtime_trace_links"}
        missing = sorted(required - tables)
        return {
            "version": RUNTIME_TRACE_MIGRATION_VERSION,
            "name": RUNTIME_TRACE_MIGRATION_NAME,
            "applied": migration is not None,
            "applied_at": migration["applied_at"] if migration else None,
            "missing_tables": missing,
            "ok": migration is not None and not missing,
        }

    def _ensure_memory_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(memories)").fetchall()}
        if "supersedes_id" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN supersedes_id TEXT")
        if "expires_at" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN expires_at TEXT")
        if "review_feedback_json" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN review_feedback_json TEXT NOT NULL DEFAULT '[]'")
        if "domain" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN domain TEXT")
        if "category" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN category TEXT")
        if "subcategory" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN subcategory TEXT")
        if "abstraction_level" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN abstraction_level TEXT")
        if "triggers_json" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN triggers_json TEXT NOT NULL DEFAULT '[]'")
        if "project_key" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN project_key TEXT")
        if "source_session_id" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN source_session_id TEXT")
        if "recall_count" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN recall_count INTEGER NOT NULL DEFAULT 0")
        if "positive_recall_count" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN positive_recall_count INTEGER NOT NULL DEFAULT 0")
        if "negative_recall_count" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN negative_recall_count INTEGER NOT NULL DEFAULT 0")
        if "last_recalled_at" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN last_recalled_at TEXT")
        if "strength" not in columns:
            self.conn.execute("ALTER TABLE memories ADD COLUMN strength REAL NOT NULL DEFAULT 1.0")
        self._ensure_governance_policy_columns()

    def _ensure_runtime_trace_columns(self) -> None:
        tables = {
            row["name"]
            for row in self.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"runtime_traces", "runtime_trace_spans", "runtime_trace_events", "runtime_trace_links"}.issubset(tables):
            return

    def _ensure_governance_policy_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(governance_policies)").fetchall()}
        if not columns:
            return
        if "hit_count" not in columns:
            self.conn.execute("ALTER TABLE governance_policies ADD COLUMN hit_count INTEGER NOT NULL DEFAULT 0")
        if "last_hit_at" not in columns:
            self.conn.execute("ALTER TABLE governance_policies ADD COLUMN last_hit_at TEXT")
        if "expires_at" not in columns:
            self.conn.execute("ALTER TABLE governance_policies ADD COLUMN expires_at TEXT")
        if "supersedes_id" not in columns:
            self.conn.execute("ALTER TABLE governance_policies ADD COLUMN supersedes_id TEXT")

    def _ensure_event_columns(self) -> None:
        columns = {
            row["name"]
            for row in self.conn.execute("PRAGMA table_info(events)").fetchall()
        }
        added_processing_columns = False
        if "processed_at" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN processed_at TEXT")
            added_processing_columns = True
        if "process_error" not in columns:
            self.conn.execute("ALTER TABLE events ADD COLUMN process_error TEXT")
            added_processing_columns = True
        if added_processing_columns:
            self.conn.execute("UPDATE events SET processed_at=created_at WHERE processed_at IS NULL")

    def _ensure_recall_event_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(recall_events)").fetchall()}
        if not columns:
            return
        if "cwd" not in columns:
            self.conn.execute("ALTER TABLE recall_events ADD COLUMN cwd TEXT")
        if "project_key" not in columns:
            self.conn.execute("ALTER TABLE recall_events ADD COLUMN project_key TEXT")
        if "session_id" not in columns:
            self.conn.execute("ALTER TABLE recall_events ADD COLUMN session_id TEXT")
        if "turn_id" not in columns:
            self.conn.execute("ALTER TABLE recall_events ADD COLUMN turn_id TEXT")
        if "outcome_json" not in columns:
            self.conn.execute("ALTER TABLE recall_events ADD COLUMN outcome_json TEXT NOT NULL DEFAULT '{}'")
        if "updated_at" not in columns:
            self.conn.execute("ALTER TABLE recall_events ADD COLUMN updated_at TEXT")
            self.conn.execute("UPDATE recall_events SET updated_at=created_at WHERE updated_at IS NULL")

    def _ensure_cognitive_record_columns(self) -> None:
        columns = {row["name"] for row in self.conn.execute("PRAGMA table_info(cognitive_records)").fetchall()}
        if not columns:
            return
        if "source_kind" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN source_kind TEXT")
        if "domain" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN domain TEXT")
        if "category" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN category TEXT")
        if "subcategory" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN subcategory TEXT")
        if "confidence" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN confidence REAL NOT NULL DEFAULT 0")
        if "importance" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN importance REAL NOT NULL DEFAULT 0")
        if "strength" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN strength REAL NOT NULL DEFAULT 1")
        if "project_key" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN project_key TEXT")
        if "session_id" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN session_id TEXT")
        if "metadata_json" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        if "updated_at" not in columns:
            self.conn.execute("ALTER TABLE cognitive_records ADD COLUMN updated_at TEXT")
            self.conn.execute("UPDATE cognitive_records SET updated_at=created_at WHERE updated_at IS NULL")

    def _ensure_indexes(self) -> None:
        self.conn.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_memories_status ON memories(status);
            CREATE INDEX IF NOT EXISTS idx_memories_type ON memories(memory_type);
            CREATE INDEX IF NOT EXISTS idx_memories_content ON memories(content);
            CREATE INDEX IF NOT EXISTS idx_memories_scope_project ON memories(status, scope, project_key);
            CREATE INDEX IF NOT EXISTS idx_recall_events_turn ON recall_events(session_id, turn_id);
            CREATE INDEX IF NOT EXISTS idx_memory_edges_source ON memory_edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_memory_edges_target ON memory_edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_governance_policies_type ON governance_policies(policy_type, active);
            CREATE INDEX IF NOT EXISTS idx_cognitive_records_layer ON cognitive_records(layer,status);
            CREATE INDEX IF NOT EXISTS idx_cognitive_records_scope ON cognitive_records(scope,project_key,session_id);
            CREATE INDEX IF NOT EXISTS idx_cognitive_edges_source ON cognitive_edges(source_id);
            CREATE INDEX IF NOT EXISTS idx_cognitive_edges_target ON cognitive_edges(target_id);
            CREATE INDEX IF NOT EXISTS idx_runtime_state_subject ON runtime_state_transitions(subject_type,subject_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_traces_session_turn ON runtime_traces(session_id,turn_id,updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_traces_status ON runtime_traces(status,updated_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_trace_spans_trace ON runtime_trace_spans(trace_id,started_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_trace_events_trace ON runtime_trace_events(trace_id,created_at);
            CREATE INDEX IF NOT EXISTS idx_runtime_trace_links_trace ON runtime_trace_links(trace_id,target_type,target_id);
            """
        )

    def record_trace(
        self,
        trace_id: str,
        session_id: str | None,
        turn_id: str | None,
        cwd: str | None,
        project_key: str | None,
        prompt_sha256: str | None,
        prompt_preview: str | None,
        prompt_chars: int,
        status: str = "started",
        root_event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _now()
        self.conn.execute(
            """
            INSERT OR IGNORE INTO runtime_traces(
                id,session_id,turn_id,cwd,project_key,prompt_sha256,prompt_preview,prompt_chars,
                status,started_at,updated_at,root_event_id,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                trace_id,
                session_id,
                turn_id,
                cwd,
                project_key,
                prompt_sha256,
                prompt_preview,
                int(prompt_chars or 0),
                status,
                now,
                now,
                root_event_id,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        self._commit()
        return self.get_trace(trace_id) or {}

    def latest_trace(self, session_id: str | None = None, turn_id: str | None = None) -> dict[str, Any] | None:
        where = []
        params: list[Any] = []
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        if turn_id:
            where.append("turn_id=?")
            params.append(turn_id)
        sql = "SELECT * FROM runtime_traces"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, started_at DESC LIMIT 1"
        row = self.conn.execute(sql, params).fetchone()
        return _trace_row_to_dict(row) if row else None

    def get_trace(self, trace_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runtime_traces WHERE id=?", (trace_id,)).fetchone()
        return _trace_row_to_dict(row) if row else None

    def list_traces(self, session_id: str | None = None, turn_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        where = []
        params: list[Any] = []
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        if turn_id:
            where.append("turn_id=?")
            params.append(turn_id)
        sql = "SELECT * FROM runtime_traces"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY updated_at DESC, started_at DESC LIMIT ?"
        params.append(max(1, min(int(limit), 500)))
        return [_trace_row_to_dict(row) for row in self.conn.execute(sql, params).fetchall()]

    def update_trace(self, trace_id: str, **fields: Any) -> dict[str, Any] | None:
        trace = self.get_trace(trace_id)
        if not trace:
            return None
        allowed = {
            "status",
            "completed_at",
            "root_event_id",
            "runtime_skill_injection_id",
            "workflow_id",
            "final_outcome",
            "error_count",
            "warning_count",
        }
        updates = []
        params: list[Any] = []
        metadata_patch = fields.pop("metadata", None)
        for key, value in fields.items():
            if key in allowed:
                updates.append(f"{key}=?")
                params.append(value)
        if isinstance(metadata_patch, dict):
            metadata = dict(trace.get("metadata_json") or {})
            metadata.update(metadata_patch)
            updates.append("metadata_json=?")
            params.append(json.dumps(metadata, ensure_ascii=False))
        updates.append("updated_at=?")
        params.append(_now())
        params.append(trace_id)
        self.conn.execute(f"UPDATE runtime_traces SET {', '.join(updates)} WHERE id=?", params)
        self._commit()
        return self.get_trace(trace_id)

    def complete_trace(self, trace_id: str, final_outcome: str | None = None, metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        return self.update_trace(trace_id, status="completed", completed_at=_now(), final_outcome=final_outcome, metadata=metadata or {})

    def fail_trace(self, trace_id: str, final_outcome: str = "failure", metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        trace = self.get_trace(trace_id)
        error_count = int((trace or {}).get("error_count") or 0) + 1
        return self.update_trace(trace_id, status="failed", completed_at=_now(), final_outcome=final_outcome, error_count=error_count, metadata=metadata or {})

    def record_trace_span(
        self,
        trace_id: str,
        name: str,
        status: str = "started",
        parent_span_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        span_id = _id("span")
        now = _now()
        self.conn.execute(
            "INSERT INTO runtime_trace_spans(id,trace_id,parent_span_id,name,status,started_at,metadata_json) VALUES(?,?,?,?,?,?,?)",
            (span_id, trace_id, parent_span_id, name, status, now, json.dumps(metadata or {}, ensure_ascii=False)),
        )
        self._commit()
        return self.get_trace_span(span_id) or {}

    def get_trace_span(self, span_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runtime_trace_spans WHERE id=?", (span_id,)).fetchone()
        return _trace_span_row_to_dict(row) if row else None

    def end_trace_span(self, span_id: str, status: str = "completed", metadata: dict[str, Any] | None = None) -> dict[str, Any] | None:
        span = self.get_trace_span(span_id)
        if not span:
            return None
        ended_at = _now()
        duration_ms = _duration_ms(str(span.get("started_at") or ""), ended_at)
        merged = dict(span.get("metadata_json") or {})
        merged.update(metadata or {})
        self.conn.execute(
            "UPDATE runtime_trace_spans SET status=?, ended_at=?, duration_ms=?, metadata_json=? WHERE id=?",
            (status, ended_at, duration_ms, json.dumps(merged, ensure_ascii=False), span_id),
        )
        self._commit()
        return self.get_trace_span(span_id)

    def list_trace_spans(self, trace_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runtime_trace_spans WHERE trace_id=? ORDER BY started_at ASC", (trace_id,)).fetchall()
        return [_trace_span_row_to_dict(row) for row in rows]

    def record_trace_event(
        self,
        trace_id: str,
        name: str,
        span_id: str | None = None,
        severity: str = "info",
        status: str | None = None,
        message: str | None = None,
        subject_type: str | None = None,
        subject_id: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        event_id = _id("tev")
        now = _now()
        self.conn.execute(
            """
            INSERT INTO runtime_trace_events(
                id,trace_id,span_id,name,severity,status,message,subject_type,subject_id,
                session_id,turn_id,created_at,metadata_json
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                event_id,
                trace_id,
                span_id,
                name,
                severity,
                status,
                message,
                subject_type,
                subject_id,
                session_id,
                turn_id,
                now,
                json.dumps(metadata or {}, ensure_ascii=False),
            ),
        )
        if severity == "error":
            self.conn.execute("UPDATE runtime_traces SET error_count=error_count+1 WHERE id=?", (trace_id,))
        elif severity == "warn":
            self.conn.execute("UPDATE runtime_traces SET warning_count=warning_count+1 WHERE id=?", (trace_id,))
        self.update_trace(trace_id)
        return self.get_trace_event(event_id) or {}

    def get_trace_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM runtime_trace_events WHERE id=?", (event_id,)).fetchone()
        return _trace_event_row_to_dict(row) if row else None

    def list_trace_events(self, trace_id: str, limit: int = 500) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runtime_trace_events WHERE trace_id=? ORDER BY created_at ASC LIMIT ?",
            (trace_id, max(1, min(int(limit), 5000))),
        ).fetchall()
        return [_trace_event_row_to_dict(row) for row in rows]

    def link_trace(self, trace_id: str, target_type: str, target_id: str, relation: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        link_id = _id("tlink")
        self.conn.execute(
            """
            INSERT OR IGNORE INTO runtime_trace_links(id,trace_id,target_type,target_id,relation,created_at,metadata_json)
            VALUES(?,?,?,?,?,?,?)
            """,
            (link_id, trace_id, target_type, target_id, relation, _now(), json.dumps(metadata or {}, ensure_ascii=False)),
        )
        self._commit()
        return {"id": link_id, "trace_id": trace_id, "target_type": target_type, "target_id": target_id, "relation": relation, "metadata_json": metadata or {}}

    def list_trace_links(self, trace_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute("SELECT * FROM runtime_trace_links WHERE trace_id=? ORDER BY created_at ASC", (trace_id,)).fetchall()
        return [_trace_link_row_to_dict(row) for row in rows]

    def export_trace(self, trace_id: str) -> dict[str, Any] | None:
        trace = self.get_trace(trace_id)
        if not trace:
            return None
        return {
            "trace": trace,
            "spans": self.list_trace_spans(trace_id),
            "events": self.list_trace_events(trace_id, limit=5000),
            "links": self.list_trace_links(trace_id),
        }

    def prune_traces(self, older_than_days: int | None = None) -> dict[str, Any]:
        cutoff = _cutoff(older_than_days)
        if cutoff:
            rows = self.conn.execute("SELECT id FROM runtime_traces WHERE updated_at < ?", (cutoff,)).fetchall()
        else:
            rows = self.conn.execute("SELECT id FROM runtime_traces").fetchall()
        trace_ids = [str(row["id"]) for row in rows]
        with self.transaction():
            for trace_id in trace_ids:
                self.conn.execute("DELETE FROM runtime_trace_links WHERE trace_id=?", (trace_id,))
                self.conn.execute("DELETE FROM runtime_trace_events WHERE trace_id=?", (trace_id,))
                self.conn.execute("DELETE FROM runtime_trace_spans WHERE trace_id=?", (trace_id,))
                self.conn.execute("DELETE FROM runtime_traces WHERE id=?", (trace_id,))
        return {"deleted_traces": len(trace_ids)}

    def add_event(self, event_type: str, payload: dict[str, Any]) -> str:
        event_id = _id("evt")
        self.conn.execute(
            "INSERT INTO events(id,event_type,payload_json,created_at) VALUES(?,?,?,?)",
            (event_id, event_type, json.dumps(payload, ensure_ascii=False), _now()),
        )
        self._commit()
        return event_id

    def record_cognitive_record(
        self,
        layer: str,
        record_type: str,
        source_id: str | None,
        content: str,
        status: str,
        scope: str,
        domain: str | None = None,
        category: str | None = None,
        subcategory: str | None = None,
        confidence: float = 0.0,
        importance: float = 0.0,
        strength: float = 1.0,
        project_key: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        source_kind: str | None = None,
    ) -> dict[str, Any]:
        record_id = source_id if source_id and layer in {"memory", "knowledge", "skill", "runtime_state", "audit"} else _id("cog")
        now = _now()
        self.conn.execute(
            """
            INSERT INTO cognitive_records(
                id,layer,record_type,source_id,source_kind,status,scope,content,
                domain,category,subcategory,confidence,importance,strength,
                project_key,session_id,metadata_json,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            ON CONFLICT(layer,record_type,source_id)
            DO UPDATE SET
                status=excluded.status,
                scope=excluded.scope,
                content=excluded.content,
                domain=excluded.domain,
                category=excluded.category,
                subcategory=excluded.subcategory,
                confidence=excluded.confidence,
                importance=excluded.importance,
                strength=excluded.strength,
                project_key=excluded.project_key,
                session_id=excluded.session_id,
                metadata_json=excluded.metadata_json,
                updated_at=excluded.updated_at
            """,
            (
                record_id,
                layer,
                record_type,
                source_id,
                source_kind,
                status,
                scope,
                content,
                domain,
                category,
                subcategory,
                max(0.0, min(1.0, float(confidence))),
                max(0.0, min(1.0, float(importance))),
                max(0.1, min(3.0, float(strength))),
                project_key,
                session_id,
                json.dumps(metadata or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._commit()
        row = self.conn.execute("SELECT * FROM cognitive_records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            row = self.conn.execute(
                """
                SELECT * FROM cognitive_records
                WHERE layer=? AND record_type=? AND source_id IS ?
                ORDER BY updated_at DESC LIMIT 1
                """,
                (layer, record_type, source_id),
            ).fetchone()
        return _cognitive_record_row_to_dict(row) if row else {}

    def upsert_cognitive_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO cognitive_edges(id,source_id,target_id,relation,weight,evidence_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id,target_id,relation)
            DO UPDATE SET weight=max(cognitive_edges.weight, excluded.weight),
                          evidence_json=excluded.evidence_json,
                          updated_at=excluded.updated_at
            """,
            (
                _id("cedge"),
                source_id,
                target_id,
                relation,
                max(0.0, min(1.0, float(weight))),
                json.dumps(evidence or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._commit()

    def list_cognitive_edges(self, relation: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        if relation:
            rows = self.conn.execute(
                "SELECT * FROM cognitive_edges WHERE relation=? ORDER BY weight DESC, updated_at DESC LIMIT ?",
                (relation, limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM cognitive_edges ORDER BY weight DESC, updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_cognitive_edge_row_to_dict(row) for row in rows]

    def list_cognitive_records(
        self,
        layer: str | None = None,
        status: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        where = []
        params: list[Any] = []
        if layer:
            where.append("layer=?")
            params.append(layer)
        if status:
            where.append("status=?")
            params.append(status)
        sql = "SELECT * FROM cognitive_records"
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY importance DESC, strength DESC, updated_at DESC LIMIT ?"
        params.append(limit)
        rows = self.conn.execute(sql, params).fetchall()
        return [_cognitive_record_row_to_dict(row) for row in rows]

    def get_cognitive_record(self, record_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM cognitive_records WHERE id=?", (record_id,)).fetchone()
        return _cognitive_record_row_to_dict(row) if row else None

    def set_cognitive_record_status(self, record_id: str, status: str, metadata_patch: dict[str, Any] | None = None) -> dict[str, Any] | None:
        record = self.get_cognitive_record(record_id)
        if not record:
            return None
        metadata = dict(record.get("metadata_json") or {})
        metadata.update(metadata_patch or {})
        self.conn.execute(
            "UPDATE cognitive_records SET status=?, metadata_json=?, updated_at=? WHERE id=?",
            (status, json.dumps(metadata, ensure_ascii=False), _now(), record_id),
        )
        self._commit()
        return self.get_cognitive_record(record_id)

    def patch_cognitive_record_metadata(self, record_id: str, metadata_patch: dict[str, Any]) -> dict[str, Any] | None:
        record = self.get_cognitive_record(record_id)
        if not record:
            return None
        metadata = dict(record.get("metadata_json") or {})
        metadata.update(metadata_patch)
        self.conn.execute(
            "UPDATE cognitive_records SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), _now(), record_id),
        )
        self._commit()
        return self.get_cognitive_record(record_id)

    def add_workflow_violation(
        self,
        workflow_id: str,
        violation_type: str,
        severity: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        existing = self.list_open_workflow_violations(workflow_id=workflow_id)
        for item in existing:
            metadata = item.get("metadata_json") or {}
            if metadata.get("violation_type") == violation_type:
                return item
        return self.record_cognitive_record(
            "audit",
            "workflow_violation",
            f"violation:{workflow_id}:{violation_type}",
            f"{severity}: {violation_type}",
            "active",
            "session",
            importance=0.95 if severity == "high" else 0.75,
            metadata={
                "workflow_id": workflow_id,
                "violation_type": violation_type,
                "severity": severity,
                "evidence": evidence,
                "resolved_at": None,
            },
            source_kind="workflow_violation",
        )

    def list_open_workflow_violations(self, workflow_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        records = self.list_cognitive_records(layer="audit", status="active", limit=max(limit, 200))
        violations = []
        for record in records:
            if record.get("record_type") != "workflow_violation":
                continue
            metadata = record.get("metadata_json") or {}
            if metadata.get("resolved_at"):
                continue
            if workflow_id and metadata.get("workflow_id") != workflow_id:
                continue
            violations.append(record)
            if len(violations) >= limit:
                break
        return violations

    def resolve_workflow_violation(self, violation_id: str) -> dict[str, Any] | None:
        return self.set_cognitive_record_status(violation_id, "resolved", {"resolved_at": _now()})

    def record_runtime_observation(
        self,
        workflow_id: str,
        observation: dict[str, Any],
        soft_evidence: bool = False,
    ) -> dict[str, Any]:
        summary = observation.get("summary") or {}
        tool_kind = str(summary.get("tool_kind") or observation.get("tool_kind") or "unknown")
        step_id = str(observation.get("matched_step_id") or "unmatched")
        return self.record_cognitive_record(
            "audit",
            "workflow_observation",
            None,
            f"{tool_kind}: {step_id}",
            "active",
            "session",
            importance=0.55 if soft_evidence else 0.7,
            metadata={
                "workflow_id": workflow_id,
                "matched_step_id": step_id,
                "soft_evidence": bool(soft_evidence),
                "observation": observation,
            },
            source_kind="runtime_observation",
        )

    def record_runtime_skill_injection(
        self,
        prompt: str,
        skill: dict[str, Any],
        session_id: str | None = None,
        turn_id: str | None = None,
        cwd: str | None = None,
        project_key: str | None = None,
        strict_privacy: bool = False,
    ) -> dict[str, Any]:
        skill_name = str(skill.get("name") or "runtime_skill")
        prompt_text = str(redact_secrets(prompt or ""))
        prompt_metadata = {
            "prompt_sha256": hashlib.sha256(prompt_text.encode("utf-8", errors="replace")).hexdigest(),
            "prompt_chars": len(prompt_text),
        }
        location_metadata: dict[str, Any] = {"cwd": cwd, "project_key": project_key}
        if not strict_privacy:
            prompt_metadata["prompt_preview"] = prompt_text[:500]
        else:
            location_metadata = {
                "cwd_sha256": hashlib.sha256(str(cwd or "").encode("utf-8", errors="replace")).hexdigest() if cwd else None,
                "project_key_sha256": hashlib.sha256(str(project_key or "").encode("utf-8", errors="replace")).hexdigest() if project_key else None,
            }
        stored_skill = skill
        if strict_privacy:
            stored_skill = {
                "skill_type": skill.get("skill_type"),
                "name": skill.get("name"),
                "intent": skill.get("intent"),
                "domain": skill.get("domain"),
                "confidence": skill.get("confidence"),
                "memory_basis_ids": [str(item) for item in skill.get("memory_basis_ids") or []],
                "durable_skill_ids": [str(item) for item in skill.get("durable_skill_ids") or []],
                "seed_skill_ids": [str(item) for item in skill.get("seed_skill_ids") or []],
            }
        return self.record_cognitive_record(
            "runtime_skill",
            "injection",
            None,
            f"Runtime skill injection: {skill_name}",
            "active",
            "session",
            domain=str(skill.get("domain") or "") or None,
            category="runtime_skill",
            subcategory=str(skill.get("intent") or "") or None,
            confidence=float(skill.get("confidence") or 0.0),
            importance=0.62,
            project_key=project_key,
            session_id=session_id,
            metadata={
                **prompt_metadata,
                "skill": stored_skill,
                "memory_basis_ids": [str(item) for item in skill.get("memory_basis_ids") or []],
                "durable_skill_ids": [str(item) for item in skill.get("durable_skill_ids") or []],
                "seed_skill_ids": [str(item) for item in skill.get("seed_skill_ids") or []],
                "session_id": session_id,
                "turn_id": turn_id,
                **location_metadata,
                "shape_version": 2,
            },
            source_kind="runtime_skill_injection",
        )

    def latest_runtime_skill_injection(
        self,
        session_id: str | None = None,
        turn_id: str | None = None,
        max_age_minutes: int | None = None,
    ) -> dict[str, Any] | None:
        where = [
            "((layer='runtime_skill' AND record_type='injection') OR (layer='audit' AND record_type='runtime_skill_injection'))",
            "status='active'",
        ]
        params: list[Any] = []
        if session_id:
            where.append("session_id=?")
            params.append(session_id)
        rows = self.conn.execute(
            "SELECT * FROM cognitive_records WHERE " + " AND ".join(where) + " ORDER BY created_at DESC LIMIT 50",
            params,
        ).fetchall()
        for row in rows:
            record = _cognitive_record_row_to_dict(row)
            metadata = record.get("metadata_json") or {}
            if turn_id and metadata.get("turn_id") != turn_id:
                continue
            if max_age_minutes is not None and _record_age_minutes(record) > max_age_minutes:
                continue
            return record
        return None

    def record_runtime_skill_feedback(
        self,
        injection_id: str,
        outcome: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any] | None:
        injection = self.get_cognitive_record(injection_id)
        if not injection:
            return None
        metadata = dict(injection.get("metadata_json") or {})
        feedback_target = str(evidence.get("feedback_target") or _runtime_skill_feedback_target(outcome, evidence))
        evidence = {**evidence, "feedback_target": feedback_target}
        classifier_dimensions = evidence.get("classifier_dimensions") if isinstance(evidence.get("classifier_dimensions"), dict) else {}
        dimensions = dict(classifier_dimensions) if classifier_dimensions else _runtime_skill_feedback_dimensions(outcome, evidence)
        metadata.update(
            {
                "feedback_status": outcome,
                "feedback_at": _now(),
                "feedback_source": evidence.get("source"),
                "feedback_dimensions": dimensions,
            }
        )
        self.conn.execute(
            "UPDATE cognitive_records SET metadata_json=?, updated_at=? WHERE id=?",
            (json.dumps(metadata, ensure_ascii=False), _now(), injection_id),
        )
        feedback = self.record_cognitive_record(
            "runtime_skill",
            "feedback",
            None,
            f"Runtime skill feedback: {outcome}",
            "active",
            "session",
            domain=injection.get("domain"),
            category="runtime_skill",
            subcategory=outcome,
            confidence=0.7,
            importance=0.58,
            project_key=injection.get("project_key"),
            session_id=injection.get("session_id"),
            metadata={
                "injection_id": injection_id,
                "trace_id": metadata.get("trace_id"),
                "outcome": outcome,
                "dimensions": dimensions,
                "evidence": evidence,
                "seed_skill_ids": metadata.get("seed_skill_ids") or [],
                "durable_skill_ids": metadata.get("durable_skill_ids") or [],
                "shape_version": 2,
            },
            source_kind="runtime_skill_feedback",
        )
        should_adjust_seed_strength = bool(evidence.get("adjust_seed_skill_strength", True))
        trace_id = str(metadata.get("trace_id") or "")
        if should_adjust_seed_strength and outcome in {"positive", "success", "negative", "failure"}:
            success = outcome in {"positive", "success"}
            for seed_id in metadata.get("seed_skill_ids") or []:
                self._record_seed_skill_feedback(str(seed_id), success)
                if trace_id:
                    seed = self.get_cognitive_record(str(seed_id)) or {}
                    seed_metadata = seed.get("metadata_json") or {}
                    self.record_trace_event(
                        trace_id,
                        "seed_skill_adjusted",
                        severity="info",
                        subject_type="seed_skill",
                        subject_id=str(seed_id),
                        metadata={
                            "skill_id": str(seed_id),
                            "success": success,
                            "reuse_count": seed_metadata.get("reuse_count"),
                            "success_count": seed_metadata.get("success_count"),
                            "failure_count": seed_metadata.get("failure_count"),
                            "new_trust_state": seed_metadata.get("trust_state"),
                            "new_status": seed.get("status"),
                        },
                    )
                    self.link_trace(trace_id, "seed_skill", str(seed_id), "adjusted")
        should_adjust_durable_strength = bool(evidence.get("adjust_durable_skill_strength", False))
        if should_adjust_durable_strength and outcome in {"positive", "success", "negative", "failure"}:
            success = outcome in {"positive", "success"}
            for skill_id in metadata.get("durable_skill_ids") or []:
                self._record_durable_skill_feedback(str(skill_id), success)
                if trace_id:
                    skill = self.get_cognitive_record(str(skill_id)) or {}
                    skill_metadata = skill.get("metadata_json") or {}
                    self.record_trace_event(
                        trace_id,
                        "durable_skill_adjusted",
                        severity="warn" if skill.get("status") == "suppressed" else "info",
                        subject_type="durable_skill",
                        subject_id=str(skill_id),
                        metadata={
                            "skill_id": str(skill_id),
                            "success": success,
                            "reuse_count": skill_metadata.get("reuse_count"),
                            "success_count": skill_metadata.get("success_count"),
                            "failure_count": skill_metadata.get("failure_count"),
                            "new_trust_state": skill_metadata.get("trust_state"),
                            "new_status": skill.get("status"),
                        },
                    )
                    self.link_trace(trace_id, "durable_skill", str(skill_id), "adjusted")
        return feedback

    def _record_seed_skill_feedback(self, seed_id: str, success: bool) -> None:
        seed = self.get_cognitive_record(seed_id)
        if not seed or seed.get("record_type") != "seed_skill":
            return
        metadata = dict(seed.get("metadata_json") or {})
        metadata["reuse_count"] = int(metadata.get("reuse_count") or 0) + 1
        metadata["success_count"] = int(metadata.get("success_count") or 0) + (1 if success else 0)
        metadata["failure_count"] = int(metadata.get("failure_count") or 0) + (0 if success else 1)
        metadata["last_feedback_at"] = _now()
        if metadata["failure_count"] >= 3 and metadata["failure_count"] > metadata["success_count"]:
            metadata["trust_state"] = "suppressed"
            self.set_cognitive_record_status(seed_id, "suppressed", metadata)
            return
        elif metadata.get("trust_state") == "suppressed" and metadata["success_count"] >= metadata["failure_count"]:
            metadata["trust_state"] = "trusted" if metadata.get("source_verified") else "unverified"
            self.set_cognitive_record_status(seed_id, "active", metadata)
            return
        self.adjust_cognitive_record_strength(seed_id, 0.05 if success else -0.1, metadata)

    def _record_durable_skill_feedback(self, skill_id: str, success: bool) -> None:
        skill = self.get_cognitive_record(skill_id)
        if not skill or skill.get("record_type") != "dynamic_skill":
            return
        metadata = dict(skill.get("metadata_json") or {})
        metadata["reuse_count"] = int(metadata.get("reuse_count") or 0) + 1
        metadata["success_count"] = int(metadata.get("success_count") or 0) + (1 if success else 0)
        metadata["failure_count"] = int(metadata.get("failure_count") or 0) + (0 if success else 1)
        metadata["last_feedback_at"] = _now()
        if metadata["failure_count"] >= 3 and metadata["failure_count"] > metadata["success_count"]:
            metadata["trust_state"] = "suppressed"
            self.set_cognitive_record_status(skill_id, "suppressed", metadata)
            return
        self.adjust_cognitive_record_strength(skill_id, 0.08 if success else -0.16, metadata)

    def record_runtime_violation(
        self,
        workflow_id: str,
        violation_type: str,
        severity: str,
        evidence: dict[str, Any],
    ) -> dict[str, Any]:
        return self.add_workflow_violation(workflow_id, violation_type, severity, evidence)

    def resolve_runtime_violation(self, violation_id: str) -> dict[str, Any] | None:
        return self.resolve_workflow_violation(violation_id)

    def record_recipe_recommendation(self, workflow_id: str, recipe_ids: list[str]) -> dict[str, Any] | None:
        self.record_cognitive_record(
            "audit",
            "recipe_recommendation",
            None,
            "Recommended verification recipes",
            "active",
            "session",
            importance=0.58,
            metadata={"workflow_id": workflow_id, "recipe_ids": recipe_ids},
            source_kind="recipe_recommendation",
        )
        return self.patch_cognitive_record_metadata(workflow_id, {"recommended_recipe_ids": recipe_ids})

    def record_recipe_reuse(
        self,
        recipe_id: str,
        workflow_id: str,
        observation: dict[str, Any],
        succeeded: bool,
        metadata_patch: dict[str, Any],
        strength_delta: float,
    ) -> dict[str, Any] | None:
        self.record_cognitive_record(
            "audit",
            "recipe_reuse",
            None,
            "Verification recipe reuse: " + ("success" if succeeded else "failure"),
            "active",
            "session",
            importance=0.72 if succeeded else 0.84,
            metadata={
                "recipe_id": recipe_id,
                "workflow_id": workflow_id,
                "succeeded": bool(succeeded),
                "strength_delta": strength_delta,
                "observation": observation,
            },
            source_kind="recipe_reuse",
        )
        return self.adjust_cognitive_record_strength(recipe_id, strength_delta, metadata_patch)

    def adjust_cognitive_record_strength(self, record_id: str, delta: float, metadata_patch: dict[str, Any] | None = None) -> dict[str, Any] | None:
        record = self.get_cognitive_record(record_id)
        if not record:
            return None
        metadata = dict(record.get("metadata_json") or {})
        metadata.update(metadata_patch or {})
        self.conn.execute(
            """
            UPDATE cognitive_records
            SET strength=max(0.1, min(3.0, strength+?)),
                metadata_json=?,
                updated_at=?
            WHERE id=?
            """,
            (float(delta), json.dumps(metadata, ensure_ascii=False), _now(), record_id),
        )
        self._commit()
        return self.get_cognitive_record(record_id)

    def record_state_transition(
        self,
        subject_type: str,
        subject_id: str,
        state: str,
        previous_state: str | None = None,
        event_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        transition_id = _id("state")
        self.conn.execute(
            """
            INSERT INTO runtime_state_transitions(
                id,subject_type,subject_id,state,previous_state,event_id,metadata_json,created_at
            ) VALUES(?,?,?,?,?,?,?,?)
            """,
            (
                transition_id,
                subject_type,
                subject_id,
                state,
                previous_state,
                event_id,
                json.dumps(metadata or {}, ensure_ascii=False),
                _now(),
            ),
        )
        self._commit()
        return transition_id

    def latest_state_for(self, subject_type: str, subject_id: str) -> str | None:
        row = self.conn.execute(
            """
            SELECT state FROM runtime_state_transitions
            WHERE subject_type=? AND subject_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (subject_type, subject_id),
        ).fetchone()
        return str(row["state"]) if row else None

    def latest_state_transitions(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM runtime_state_transitions ORDER BY created_at DESC LIMIT ?",
            (max(1, min(limit, 500)),),
        ).fetchall()
        return [_state_row_to_dict(row) for row in rows]

    def get_event(self, event_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM events WHERE id=?", (event_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
        try:
            data["payload_json"] = json.loads(data["payload_json"])
        except (TypeError, json.JSONDecodeError):
            data["payload_json"] = {}
        return data

    def list_events(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        rows = self.conn.execute("SELECT * FROM events ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_event_row_to_dict(row) for row in rows]

    def mark_event_processed(self, event_id: str) -> None:
        self.conn.execute(
            "UPDATE events SET processed_at=?, process_error=NULL WHERE id=?",
            (_now(), event_id),
        )
        self._commit()

    def mark_event_failed(self, event_id: str, error: str) -> None:
        self.conn.execute(
            "UPDATE events SET process_error=? WHERE id=?",
            (error[:1000], event_id),
        )
        self._commit()

    def add_candidate(
        self,
        candidate: MemoryCandidate,
        status: str,
        review: dict[str, Any],
        project_key: str | None = None,
        session_id: str | None = None,
    ) -> str:
        candidate = enrich_candidate(candidate)
        memory_id = _id("mem")
        now = _now()
        self.conn.execute(
            """
            INSERT INTO memories(
                id,status,content,memory_type,scope,ttl,confidence,importance,
                domain,category,subcategory,abstraction_level,triggers_json,
                project_key,source_session_id,
                evidence_json,reason,review_json,expires_at,created_at,updated_at
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                memory_id,
                status,
                candidate.content,
                candidate.memory_type,
                candidate.scope,
                candidate.ttl,
                candidate.confidence,
                candidate.importance,
                candidate.domain,
                candidate.category,
                candidate.subcategory,
                candidate.abstraction_level,
                json.dumps(candidate.triggers, ensure_ascii=False),
                project_key if candidate.scope == "project" else None,
                session_id if candidate.scope == "session" else None,
                json.dumps([e.__dict__ for e in candidate.evidence], ensure_ascii=False),
                candidate.reason,
                json.dumps(review, ensure_ascii=False),
                _expiry_for_ttl(candidate.ttl),
                now,
                now,
            ),
        )
        self._commit()
        return memory_id

    def get_memory(self, memory_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM memories WHERE id=?", (memory_id,)).fetchone()
        return _row_to_dict(row) if row else None

    def find_active_duplicates(
        self,
        content: str,
        memory_type: str,
        scope: str,
        project_key: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        normalized = _normalize(content)
        memory_types = _duplicate_memory_types(memory_type)
        placeholders = ",".join("?" for _ in memory_types)
        scope_filter, scope_params = _duplicate_scope_filter(scope, project_key, session_id)
        rows = self.conn.execute(
            f"""
            SELECT * FROM memories
            WHERE status='active' AND memory_type IN ({placeholders}) AND {scope_filter}
            ORDER BY created_at DESC
            """,
            (*memory_types, *scope_params),
        ).fetchall()
        matches = []
        for row in rows:
            item = _row_to_dict(row)
            existing_content = str(item.get("content") or "")
            if _normalize(existing_content) == normalized or (
                _polarity(existing_content) == _polarity(content)
                and near_duplicate_text(existing_content, content)
            ):
                matches.append(item)
        return matches

    def find_active_conflicts(
        self,
        content: str,
        memory_type: str,
        scope: str,
        project_key: str | None = None,
        session_id: str | None = None,
    ) -> list[dict[str, Any]]:
        if memory_type not in {"user_preference", "project_context", "fact"}:
            return []
        rows = self.conn.execute(
            f"""
            SELECT * FROM memories
            WHERE status='active' AND memory_type=? AND scope=? AND {_scope_filter(scope)}
            ORDER BY created_at DESC LIMIT 50
            """,
            (memory_type, scope, *_scope_params(scope, project_key, session_id)),
        ).fetchall()
        content_tokens = _tokens(content)
        polarity = _polarity(content)
        if polarity == 0:
            return []
        conflicts = []
        for row in rows:
            item = _row_to_dict(row)
            old_content = str(item.get("content") or "")
            if _polarity(old_content) == -polarity and len(content_tokens & _tokens(old_content)) >= 2:
                conflicts.append(item)
        return conflicts

    def supersede(self, old_id: str, new_id: str, reason: str) -> None:
        old = self.get_memory(old_id)
        review = dict(old.get("review_json") or {}) if old else {}
        review["superseded_by"] = new_id
        review["supersede_reason"] = reason
        self.set_status(old_id, "superseded", review)
        self.conn.execute(
            "UPDATE memories SET supersedes_id=?, updated_at=? WHERE id=?",
            (old_id, _now(), new_id),
        )
        self._commit()

    def add_review_feedback(self, memory_id: str, action: str, note: str = "") -> dict[str, Any]:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise ValueError(f"memory not found: {memory_id}")
        feedback = list(memory.get("review_feedback_json") or [])
        feedback.append({"action": action, "note": note, "at": _now()})
        self.conn.execute(
            "UPDATE memories SET review_feedback_json=?, updated_at=? WHERE id=?",
            (json.dumps(feedback, ensure_ascii=False), _now(), memory_id),
        )
        self._commit()
        return self.get_memory(memory_id) or {}

    def promote(self, memory_id: str, note: str = "") -> dict[str, Any]:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise ValueError(f"memory not found: {memory_id}")
        review = dict(memory.get("review_json") or {})
        review["manual_review"] = {"action": "promote", "note": note, "at": _now()}
        self.set_status(memory_id, "active", review)
        return self.add_review_feedback(memory_id, "promote", note)

    def reject(self, memory_id: str, note: str = "") -> dict[str, Any]:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise ValueError(f"memory not found: {memory_id}")
        review = dict(memory.get("review_json") or {})
        review["manual_review"] = {"action": "reject", "note": note, "at": _now()}
        self.set_status(memory_id, "rejected", review)
        return self.add_review_feedback(memory_id, "reject", note)

    def delete(self, memory_id: str, note: str = "") -> dict[str, Any]:
        memory = self.get_memory(memory_id)
        if memory is None:
            raise ValueError(f"memory not found: {memory_id}")
        review = dict(memory.get("review_json") or {})
        review["manual_review"] = {"action": "delete", "note": note, "at": _now()}
        self.set_status(memory_id, "deleted", review)
        return self.add_review_feedback(memory_id, "delete", note)

    def expire_due(self) -> list[dict[str, Any]]:
        now = _now()
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE status='active' AND expires_at IS NOT NULL AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (now,),
        ).fetchall()
        expired = []
        for row in rows:
            item = _row_to_dict(row)
            review = dict(item.get("review_json") or {})
            review["expired_at"] = now
            self.set_status(str(item["id"]), "superseded", review)
            expired.append(self.get_memory(str(item["id"])) or item)
        return expired

    def reconcile_audit_events(self) -> int:
        cur = self.conn.execute(
            """
            UPDATE events
            SET processed_at=created_at
            WHERE processed_at IS NULL
              AND process_error IS NULL
              AND event_type IN ('session_start','session_end','precompact','after_tool_call')
            """
        )
        self._commit()
        return int(cur.rowcount or 0)

    def link_related_active_memories(self, memory_id: str) -> int:
        memory = self.get_memory(memory_id)
        if not memory or memory.get("status") != "active":
            return 0
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE status='active' AND id<>?
              AND (scope='global' OR project_key IS ? OR project_key=?)
            ORDER BY created_at DESC LIMIT 200
            """,
            (memory_id, memory.get("project_key"), memory.get("project_key")),
        ).fetchall()
        count = 0
        for row in rows:
            other = _row_to_dict(row)
            relation, weight, evidence = _edge_for(memory, other)
            if not relation:
                continue
            self.upsert_edge(str(memory["id"]), str(other["id"]), relation, weight, evidence)
            self.upsert_edge(str(other["id"]), str(memory["id"]), relation, weight, evidence)
            count += 2
        return count

    def upsert_edge(
        self,
        source_id: str,
        target_id: str,
        relation: str,
        weight: float,
        evidence: dict[str, Any] | None = None,
    ) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO memory_edges(id,source_id,target_id,relation,weight,evidence_json,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?)
            ON CONFLICT(source_id,target_id,relation)
            DO UPDATE SET weight=max(memory_edges.weight, excluded.weight),
                          evidence_json=excluded.evidence_json,
                          updated_at=excluded.updated_at
            """,
            (
                _id("edge"),
                source_id,
                target_id,
                relation,
                max(0.0, min(1.0, float(weight))),
                json.dumps(evidence or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        self._commit()

    def list_edges(self, memory_ids: list[str] | None = None, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 2000))
        if memory_ids:
            placeholders = ",".join("?" for _ in memory_ids)
            rows = self.conn.execute(
                f"""
                SELECT * FROM memory_edges
                WHERE source_id IN ({placeholders}) OR target_id IN ({placeholders})
                ORDER BY weight DESC, updated_at DESC LIMIT ?
                """,
                (*memory_ids, *memory_ids, limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM memory_edges ORDER BY weight DESC, updated_at DESC LIMIT ?", (limit,)).fetchall()
        return [_edge_row_to_dict(row) for row in rows]

    def list_recallable_memories(
        self,
        cwd: str | None = None,
        session_id: str | None = None,
        limit: int = 200,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 500))
        project_key = project_key_for_cwd(cwd)
        rows = self.conn.execute(
            """
            SELECT * FROM memories
            WHERE status='active'
              AND (
                scope='global'
                OR (scope='project' AND (project_key=? OR project_key IS NULL))
                OR (scope='session' AND source_session_id=?)
              )
            ORDER BY strength DESC, importance DESC, updated_at DESC
            LIMIT ?
            """,
            (project_key, session_id, limit),
        ).fetchall()
        memories = [_row_to_dict(row) for row in rows]
        filtered = []
        for memory in memories:
            decision = self.memory_policy_decision(memory, "recall_gate")
            if decision and decision.get("action") == "suppress":
                continue
            filtered.append(memory)
        return filtered

    def record_recall(
        self,
        prompt: str,
        route: dict[str, Any],
        memories: list[dict[str, Any]],
        cwd: str | None = None,
        session_id: str | None = None,
        turn_id: str | None = None,
    ) -> str | None:
        if not memories:
            return None
        now = _now()
        recall_id = _id("recall")
        memory_ids = [str(item["id"]) for item in memories if item.get("id")]
        self.conn.execute(
            """
            INSERT INTO recall_events(id,prompt,route_json,memory_ids_json,cwd,project_key,session_id,turn_id,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?,?)
            """,
            (
                recall_id,
                prompt,
                json.dumps(route, ensure_ascii=False),
                json.dumps(memory_ids, ensure_ascii=False),
                cwd,
                project_key_for_cwd(cwd),
                session_id,
                turn_id,
                now,
                now,
            ),
        )
        for memory_id in memory_ids:
            self.conn.execute(
                """
                UPDATE memories
                SET recall_count=recall_count+1,
                    last_recalled_at=?,
                    strength=min(3.0, strength+0.05),
                    updated_at=?
                WHERE id=?
                """,
                (now, now, memory_id),
            )
        for index, source_id in enumerate(memory_ids):
            for target_id in memory_ids[index + 1 :]:
                self.upsert_edge(source_id, target_id, "co_recalled", 0.55, {"recall_id": recall_id})
                self.upsert_edge(target_id, source_id, "co_recalled", 0.55, {"recall_id": recall_id})
        self._commit()
        return recall_id

    def record_recall_outcome(
        self,
        session_id: str | None,
        turn_id: str | None,
        assistant_message: str,
    ) -> dict[str, Any]:
        if not session_id or not turn_id:
            return {"updated": 0, "reason": "missing_session_or_turn"}
        row = self.conn.execute(
            """
            SELECT * FROM recall_events
            WHERE session_id=? AND turn_id=?
            ORDER BY created_at DESC LIMIT 1
            """,
            (session_id, turn_id),
        ).fetchone()
        if row is None:
            return {"updated": 0, "reason": "no_recall_event"}
        event = _recall_row_to_dict(row)
        memory_ids = [str(item) for item in event.get("memory_ids_json") or []]
        memories = [self.get_memory(memory_id) for memory_id in memory_ids]
        adopted = [
            str(memory["id"])
            for memory in memories
            if memory and _assistant_uses_memory(assistant_message, memory)
        ]
        now = _now()
        outcome = {"assistant_used_memory_ids": adopted, "assistant_message_chars": len(assistant_message), "at": now}
        self.conn.execute(
            "UPDATE recall_events SET outcome_json=?, updated_at=? WHERE id=?",
            (json.dumps(outcome, ensure_ascii=False), now, event["id"]),
        )
        for memory_id in adopted:
            self.conn.execute(
                """
                UPDATE memories
                SET positive_recall_count=positive_recall_count+1,
                    strength=min(3.0, strength+0.15),
                    updated_at=?
                WHERE id=?
                """,
                (now, memory_id),
            )
        self._commit()
        return {"updated": len(adopted), "recall_id": event["id"], "adopted": adopted}

    def register_recall_feedback(self, memory_id: str, outcome: str, note: str = "") -> dict[str, Any]:
        if outcome not in {"positive", "negative"}:
            raise ValueError("outcome must be positive or negative")
        column = "positive_recall_count" if outcome == "positive" else "negative_recall_count"
        delta = 0.2 if outcome == "positive" else -0.35
        self.conn.execute(
            f"""
            UPDATE memories
            SET {column}={column}+1,
                strength=max(0.1, min(3.0, strength+?)),
                updated_at=?
            WHERE id=?
            """,
            (delta, _now(), memory_id),
        )
        self._commit()
        return self.add_review_feedback(memory_id, f"recall_{outcome}", note)

    def adjust_strength(self, memory_id: str, delta: float, note: str) -> dict[str, Any]:
        self.conn.execute(
            """
            UPDATE memories
            SET strength=max(0.1, min(3.0, strength+?)),
                updated_at=?
            WHERE id=?
            """,
            (float(delta), _now(), memory_id),
        )
        self._commit()
        return self.add_review_feedback(memory_id, "governance_strength_adjust", note)

    def record_governance_report(self, report: dict[str, Any], actions: list[dict[str, Any]]) -> str:
        report_id = _id("gov")
        self.conn.execute(
            """
            INSERT INTO governance_reports(id,report_json,actions_json,created_at)
            VALUES(?,?,?,?)
            """,
            (
                report_id,
                json.dumps(report, ensure_ascii=False),
                json.dumps(actions, ensure_ascii=False),
                _now(),
            ),
        )
        self._commit()
        return report_id

    def add_governance_policy(
        self,
        policy_type: str,
        matcher: dict[str, Any],
        action: str,
        reason: str,
        source_memory_id: str | None = None,
        ttl_days: int | None = 90,
    ) -> str:
        existing = self.conn.execute(
            """
            SELECT * FROM governance_policies
            WHERE policy_type=? AND matcher_json=? AND action=? AND active=1
            ORDER BY created_at DESC LIMIT 1
            """,
            (policy_type, json.dumps(matcher, ensure_ascii=False, sort_keys=True), action),
        ).fetchone()
        if existing:
            return str(existing["id"])
        policy_id = _id("policy")
        now = _now()
        self.conn.execute(
            """
            INSERT INTO governance_policies(id,policy_type,matcher_json,action,reason,source_memory_id,expires_at,created_at,updated_at)
            VALUES(?,?,?,?,?,?,?,?,?)
            """,
            (
                policy_id,
                policy_type,
                json.dumps(matcher, ensure_ascii=False, sort_keys=True),
                action,
                reason,
                source_memory_id,
                _days_from_now(ttl_days) if ttl_days else None,
                now,
                now,
            ),
        )
        self._commit()
        return policy_id

    def list_governance_policies(self, policy_type: str | None = None, active: bool = True) -> list[dict[str, Any]]:
        self.expire_governance_policies()
        if policy_type:
            rows = self.conn.execute(
                "SELECT * FROM governance_policies WHERE policy_type=? AND active=? ORDER BY created_at DESC",
                (policy_type, 1 if active else 0),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM governance_policies WHERE active=? ORDER BY created_at DESC",
                (1 if active else 0,),
            ).fetchall()
        return [_policy_row_to_dict(row) for row in rows]

    def candidate_policy_decision(self, candidate: MemoryCandidate) -> dict[str, Any] | None:
        candidate = enrich_candidate(candidate)
        policies = self.list_governance_policies(policy_type="candidate_gate", active=True)
        for policy in policies:
            matcher = policy.get("matcher_json") or {}
            if _matches_policy(candidate.to_dict(), matcher):
                now = _now()
                self.conn.execute(
                    "UPDATE governance_policies SET hit_count=hit_count+1,last_hit_at=?,updated_at=? WHERE id=?",
                    (now, now, policy["id"]),
                )
                self._commit()
                return {
                    "policy_id": policy["id"],
                    "action": policy["action"],
                    "reason": policy["reason"],
                    "matcher": matcher,
                    "source_memory_id": policy.get("source_memory_id"),
                }
        return None

    def memory_policy_decision(self, memory: dict[str, Any], policy_type: str) -> dict[str, Any] | None:
        policies = self.list_governance_policies(policy_type=policy_type, active=True)
        for policy in policies:
            matcher = policy.get("matcher_json") or {}
            if _matches_policy(memory, matcher):
                now = _now()
                self.conn.execute(
                    "UPDATE governance_policies SET hit_count=hit_count+1,last_hit_at=?,updated_at=? WHERE id=?",
                    (now, now, policy["id"]),
                )
                self._commit()
                return {
                    "policy_id": policy["id"],
                    "action": policy["action"],
                    "reason": policy["reason"],
                    "matcher": matcher,
                    "source_memory_id": policy.get("source_memory_id"),
                }
        return None

    def expire_governance_policies(self) -> int:
        now = _now()
        cur = self.conn.execute(
            """
            UPDATE governance_policies
            SET active=0, updated_at=?
            WHERE active=1 AND expires_at IS NOT NULL AND expires_at <= ?
            """,
            (now, now),
        )
        self._commit()
        return int(cur.rowcount or 0)

    def set_governance_state(self, key: str, value: Any) -> None:
        now = _now()
        self.conn.execute(
            """
            INSERT INTO governance_state(key,value,updated_at)
            VALUES(?,?,?)
            ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at
            """,
            (key, json.dumps(value, ensure_ascii=False), now),
        )
        self._commit()

    def get_governance_state(self, key: str) -> Any:
        row = self.conn.execute("SELECT value FROM governance_state WHERE key=?", (key,)).fetchone()
        if not row:
            return None
        try:
            return json.loads(row["value"])
        except (TypeError, json.JSONDecodeError):
            return None

    def latest_recall_event(self, session_id: str | None = None) -> dict[str, Any] | None:
        if session_id:
            row = self.conn.execute(
                "SELECT * FROM recall_events WHERE session_id=? ORDER BY created_at DESC LIMIT 1",
                (session_id,),
            ).fetchone()
        else:
            row = self.conn.execute("SELECT * FROM recall_events ORDER BY created_at DESC LIMIT 1").fetchone()
        return _recall_row_to_dict(row) if row else None

    def list_recall_events(self, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 5000))
        rows = self.conn.execute(
            "SELECT * FROM recall_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [_recall_row_to_dict(row) for row in rows]

    def export_data(self, limit: int = 5000) -> dict[str, Any]:
        limit = max(1, min(limit, 20000))
        return {
            "version": 1,
            "ledger_path": str(self.path),
            "exported_at": _now(),
            "stats": self.stats(),
            "memories": self.list_memories(limit=min(limit, 5000)),
            "events": self.list_events(limit=limit),
            "recall_events": self.list_recall_events(limit=limit),
            "memory_edges": self.list_edges([], limit=limit),
            "governance_policies": self.list_governance_policies(active=True) + self.list_governance_policies(active=False),
            "cognitive_records": self.list_cognitive_records(limit=limit),
            "cognitive_edges": self.list_cognitive_edges(limit=limit),
            "runtime_state_transitions": self.latest_state_transitions(limit=limit),
            "runtime_traces": self.list_traces(limit=limit),
            "runtime_trace_spans": _select_json_rows(self.conn, "runtime_trace_spans", limit),
            "runtime_trace_events": _select_json_rows(self.conn, "runtime_trace_events", limit),
            "runtime_trace_links": _select_json_rows(self.conn, "runtime_trace_links", limit),
        }

    def wipe_all(self) -> dict[str, Any]:
        counts = {name: self.conn.execute(f"SELECT COUNT(*) AS count FROM {name}").fetchone()["count"] for name in _WIPE_TABLES}
        with self.transaction():
            for table in _WIPE_TABLES:
                self.conn.execute(f"DELETE FROM {table}")
        return {"wiped": counts, "ledger_path": str(self.path)}

    def prune_events(self, older_than_days: int | None = None) -> dict[str, Any]:
        if older_than_days is not None and older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        if older_than_days is None:
            where = "processed_at IS NOT NULL"
            params: tuple[Any, ...] = ()
        else:
            cutoff = (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=older_than_days)).isoformat().replace("+00:00", "Z")
            where = "processed_at IS NOT NULL AND created_at < ?"
            params = (cutoff,)
        count = self.conn.execute(f"SELECT COUNT(*) AS count FROM events WHERE {where}", params).fetchone()["count"]
        self.conn.execute(f"DELETE FROM events WHERE {where}", params)
        self._commit()
        return {"pruned_events": count, "older_than_days": older_than_days}

    def prune_runtime_records(self, older_than_days: int | None = None, include_recipes: bool = False, include_skills: bool = False) -> dict[str, Any]:
        if older_than_days is not None and older_than_days < 0:
            raise ValueError("older_than_days must be non-negative")
        cutoff = None
        if older_than_days is not None:
            cutoff = (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=older_than_days)).isoformat().replace("+00:00", "Z")
        audit_record_types = {"workflow_observation", "recipe_recommendation", "recipe_reuse", "runtime_skill_injection", "runtime_skill_feedback"}
        runtime_skill_record_types = {"injection", "feedback"}
        ids: list[str] = []
        workflow_metadata_updates: list[tuple[str, dict[str, Any]]] = []
        counts = {
            "workflow_observation": 0,
            "recipe_recommendation": 0,
            "recipe_reuse": 0,
            "runtime_skill_injection": 0,
            "runtime_skill_feedback": 0,
            "runtime_skill_layer_injection": 0,
            "runtime_skill_layer_feedback": 0,
            "resolved_workflow_violation": 0,
            "workflow_metadata_observations": 0,
            "verification_recipe": 0,
            "dynamic_skill": 0,
        }
        rows = self.conn.execute(
            """
            SELECT id,layer,record_type,status,created_at,metadata_json
            FROM cognitive_records
            ORDER BY created_at DESC
            """
        ).fetchall()
        for row in rows:
            if cutoff and str(row["created_at"] or "") >= cutoff:
                continue
            record_type = str(row["record_type"] or "")
            layer = str(row["layer"] or "")
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            should_delete = False
            count_key = record_type
            if layer == "audit" and record_type in audit_record_types:
                should_delete = True
            elif layer == "runtime_skill" and record_type in runtime_skill_record_types:
                should_delete = True
                count_key = f"runtime_skill_layer_{record_type}"
            elif layer == "audit" and record_type == "workflow_violation" and (row["status"] == "resolved" or metadata.get("resolved_at")):
                should_delete = True
                count_key = "resolved_workflow_violation"
            elif layer == "workflow" and record_type == "observed_workflow":
                patch: dict[str, Any] = {}
                if metadata.get("observations"):
                    patch["observations"] = []
                if metadata.get("recommended_recipe_ids"):
                    patch["recommended_recipe_ids"] = []
                if metadata.get("recommended_dynamic_skill_ids"):
                    patch["recommended_dynamic_skill_ids"] = []
                if patch:
                    patch["pruned_runtime_metadata_at"] = _now()
                    workflow_metadata_updates.append((str(row["id"]), patch))
                    counts["workflow_metadata_observations"] += 1
            elif (include_recipes or include_skills) and layer == "skill" and record_type == "verification_recipe":
                should_delete = True
                count_key = "verification_recipe"
            elif include_skills and layer == "skill" and record_type == "dynamic_skill":
                should_delete = True
                count_key = "dynamic_skill"
            if should_delete:
                ids.append(str(row["id"]))
                counts[count_key] = counts.get(count_key, 0) + 1
        with self.transaction():
            for record_id in ids:
                self.conn.execute("DELETE FROM cognitive_records WHERE id=?", (record_id,))
                self.conn.execute("DELETE FROM cognitive_edges WHERE source_id=? OR target_id=?", (record_id, record_id))
            for record_id, patch in workflow_metadata_updates:
                record = self.get_cognitive_record(record_id)
                if not record:
                    continue
                metadata = dict(record.get("metadata_json") or {})
                metadata.update(patch)
                self.conn.execute(
                    "UPDATE cognitive_records SET metadata_json=?, updated_at=? WHERE id=?",
                    (json.dumps(metadata, ensure_ascii=False), _now(), record_id),
                )
        return {
            "pruned_runtime_records": len(ids),
            "counts": counts,
            "older_than_days": older_than_days,
            "include_recipes": include_recipes,
        }

    def set_status(self, memory_id: str, status: str, review: dict[str, Any] | None = None) -> None:
        if review is None:
            self.conn.execute("UPDATE memories SET status=?, updated_at=? WHERE id=?", (status, _now(), memory_id))
        else:
            self.conn.execute(
                "UPDATE memories SET status=?, review_json=?, updated_at=? WHERE id=?",
                (status, json.dumps(review, ensure_ascii=False), _now(), memory_id),
            )
        self._commit()

    def list_memories(self, status: str | None = None, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 200))
        if status:
            rows = self.conn.execute(
                "SELECT * FROM memories WHERE status=? ORDER BY created_at DESC LIMIT ?",
                (status, limit),
            ).fetchall()
        else:
            rows = self.conn.execute("SELECT * FROM memories ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
        return [_row_to_dict(row) for row in rows]

    def stats(self) -> dict[str, Any]:
        rows = self.conn.execute("SELECT status, COUNT(*) AS count FROM memories GROUP BY status").fetchall()
        pending = self.conn.execute(
            "SELECT COUNT(*) AS count FROM events WHERE processed_at IS NULL AND process_error IS NULL"
        ).fetchone()["count"]
        failed = self.conn.execute(
            "SELECT COUNT(*) AS count FROM events WHERE processed_at IS NULL AND process_error IS NOT NULL"
        ).fetchone()["count"]
        return {
            "ledger_path": str(self.path),
            "by_status": {row["status"]: row["count"] for row in rows},
            "events": {"pending": pending, "failed": failed},
        }


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    legacy_drawer_key = "mem" + "palace_drawer_id"
    for legacy_key in ("wing", "room", legacy_drawer_key, "kg_triple_ids_json"):
        data.pop(legacy_key, None)
    for key in ("evidence_json", "review_json", "review_feedback_json", "triggers_json"):
        try:
            data[key] = json.loads(data[key])
        except (TypeError, json.JSONDecodeError):
            data[key] = None
    return data


def _event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["payload_json"] = json.loads(data["payload_json"])
    except (TypeError, json.JSONDecodeError):
        data["payload_json"] = {}
    return data


def _edge_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["evidence_json"] = json.loads(data["evidence_json"])
    except (TypeError, json.JSONDecodeError):
        data["evidence_json"] = {}
    return data


def _recall_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    for key in ("route_json", "memory_ids_json", "outcome_json"):
        try:
            data[key] = json.loads(data[key])
        except (TypeError, json.JSONDecodeError):
            data[key] = [] if key == "memory_ids_json" else {}
    return data


def _policy_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["matcher_json"] = json.loads(data["matcher_json"])
    except (TypeError, json.JSONDecodeError):
        data["matcher_json"] = {}
    return data


def _cognitive_record_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata_json"] = json.loads(data["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        data["metadata_json"] = {}
    return data


def _cognitive_edge_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["evidence_json"] = json.loads(data["evidence_json"])
    except (TypeError, json.JSONDecodeError):
        data["evidence_json"] = {}
    return data


def _state_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata_json"] = json.loads(data["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        data["metadata_json"] = {}
    return data


def _trace_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata_json"] = json.loads(data["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        data["metadata_json"] = {}
    return data


def _trace_span_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata_json"] = json.loads(data["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        data["metadata_json"] = {}
    return data


def _trace_event_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata_json"] = json.loads(data["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        data["metadata_json"] = {}
    return data


def _trace_link_row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    data = dict(row)
    try:
        data["metadata_json"] = json.loads(data["metadata_json"])
    except (TypeError, json.JSONDecodeError):
        data["metadata_json"] = {}
    return data


def _id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:24]}"


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _duration_ms(started_at: str, ended_at: str) -> int | None:
    try:
        started = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        ended = datetime.fromisoformat(ended_at.replace("Z", "+00:00"))
    except ValueError:
        return None
    return int((ended - started).total_seconds() * 1000)


def _cutoff(older_than_days: int | None) -> str | None:
    if older_than_days is None:
        return None
    return (datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=older_than_days)).isoformat().replace("+00:00", "Z")


def _record_age_minutes(record: dict[str, Any]) -> float:
    created_at = str(record.get("created_at") or "")
    if not created_at:
        return float("inf")
    try:
        created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    except ValueError:
        return float("inf")
    return (datetime.now(timezone.utc) - created).total_seconds() / 60


def _runtime_skill_feedback_dimensions(outcome: str, evidence: dict[str, Any]) -> dict[str, str]:
    source = str(evidence.get("source") or "")
    target = str(evidence.get("feedback_target") or "")
    unknown = "unknown"
    dimensions = {
        "skill_relevance": unknown,
        "first_action_quality": unknown,
        "memory_basis_quality": unknown,
        "seed_skill_quality": unknown,
        "durable_skill_quality": unknown,
        "execution_compliance": unknown,
        "final_result_quality": unknown,
    }
    if source == "workflow_stop":
        if outcome == "success":
            dimensions["execution_compliance"] = "passed"
            dimensions["final_result_quality"] = "positive"
        elif outcome == "failure":
            dimensions["execution_compliance"] = "failed"
            dimensions["final_result_quality"] = "negative"
    elif source in {"natural_feedback", "manual_cli"}:
        value = "positive" if outcome == "positive" else "negative" if outcome == "negative" else "mixed" if outcome == "mixed" else unknown
        if target == "first_action":
            dimensions["first_action_quality"] = value
        elif target == "skill_strategy":
            dimensions["skill_relevance"] = value
            dimensions["seed_skill_quality"] = value
        elif target == "seed_skill":
            dimensions["seed_skill_quality"] = value
        elif target == "durable_skill":
            dimensions["durable_skill_quality"] = value
        elif target == "memory_basis":
            dimensions["memory_basis_quality"] = value
        elif target == "final_result":
            dimensions["final_result_quality"] = value
        if outcome == "positive":
            if target == "skill_strategy" and dimensions["skill_relevance"] == unknown:
                dimensions["skill_relevance"] = "positive"
            if target == "final_result":
                dimensions["final_result_quality"] = "positive"
        elif outcome == "negative":
            if target == "skill_strategy" and dimensions["skill_relevance"] == unknown:
                dimensions["skill_relevance"] = "negative"
            if target == "final_result":
                dimensions["final_result_quality"] = "negative"
    return dimensions


def _runtime_skill_feedback_target(outcome: str, evidence: dict[str, Any]) -> str:
    source = str(evidence.get("source") or "")
    if source == "workflow_stop":
        return "workflow_execution"
    return "unknown"


def _json_obj(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return dict(raw)
    try:
        parsed = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _select_json_rows(conn: sqlite3.Connection, table: str, limit: int) -> list[dict[str, Any]]:
    rows = conn.execute(f"SELECT * FROM {table} ORDER BY rowid DESC LIMIT ?", (max(1, min(int(limit), 20000)),)).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        item = dict(row)
        for key, value in list(item.items()):
            if key.endswith("_json"):
                try:
                    item[key] = json.loads(value or "{}")
                except (TypeError, json.JSONDecodeError):
                    item[key] = {}
        result.append(item)
    return result


def _normalized_seed_skill_state(status: str, metadata: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    normalized = dict(metadata)
    trust_state = str(normalized.get("trust_state") or "").strip()
    if not trust_state:
        trust_state = "trusted" if normalized.get("source_verified") else "unverified"
        normalized["trust_state"] = trust_state
    if trust_state == "disabled":
        return "deprecated", normalized
    if trust_state == "suppressed":
        return "suppressed", normalized
    if status == "deprecated":
        normalized["trust_state"] = "disabled"
        return "deprecated", normalized
    if status == "suppressed":
        normalized["trust_state"] = "suppressed"
        return "suppressed", normalized
    if trust_state == "trusted" and status != "active":
        return "active", normalized
    if trust_state == "unverified" and status not in {"active", "candidate"}:
        return "candidate", normalized
    return status or "active", normalized


def _days_from_now(days: int | None) -> str | None:
    if days is None:
        return None
    return (datetime.now(timezone.utc).replace(microsecond=0) + timedelta(days=days)).isoformat().replace("+00:00", "Z")


def project_key_for_cwd(cwd: str | None) -> str | None:
    if not cwd:
        return None
    try:
        return str(Path(cwd).expanduser().resolve()).lower()
    except OSError:
        return str(Path(cwd).expanduser()).lower()


def _expiry_for_ttl(ttl: str) -> str | None:
    now = datetime.now(timezone.utc).replace(microsecond=0)
    if ttl == "short":
        return (now + timedelta(days=7)).isoformat().replace("+00:00", "Z")
    if ttl == "session":
        return (now + timedelta(hours=12)).isoformat().replace("+00:00", "Z")
    return None


def _normalize(text: str) -> str:
    return "".join(text.lower().split()).strip("。.!！?")


def _tokens(text: str) -> set[str]:
    return {part for part in re_split(text.lower()) if len(part) >= 2}


def _matches_policy(candidate: dict[str, Any], matcher: dict[str, Any]) -> bool:
    expected_id = matcher.get("memory_id")
    if expected_id and candidate.get("id") != expected_id:
        return False
    for key in ("memory_type", "scope", "domain", "category", "subcategory"):
        expected = matcher.get(key)
        actual = candidate.get("type") or candidate.get("memory_type") if key == "memory_type" else candidate.get(key)
        if expected and actual != expected:
            return False
    candidate_terms = set(str(item).lower() for item in candidate.get("triggers") or [])
    candidate_terms.update(_tokens(str(candidate.get("content") or "")))
    required_terms = set(str(item).lower() for item in matcher.get("terms") or [])
    if required_terms and len(candidate_terms & required_terms) < min(2, len(required_terms)):
        return False
    return True


def _scope_filter(scope: str) -> str:
    if scope == "project":
        return "(project_key=? OR project_key IS NULL)"
    if scope == "session":
        return "source_session_id=?"
    return "1=1"


def _scope_params(scope: str, project_key: str | None, session_id: str | None) -> tuple[Any, ...]:
    if scope == "project":
        return (project_key,)
    if scope == "session":
        return (session_id,)
    return ()


def _duplicate_scope_filter(scope: str, project_key: str | None, session_id: str | None) -> tuple[str, tuple[Any, ...]]:
    if scope == "session":
        return "scope='session' AND source_session_id=?", (session_id,)
    if scope == "project":
        return "((scope='project' AND (project_key=? OR project_key IS NULL)) OR scope='global')", (project_key,)
    if project_key:
        return "((scope='global') OR (scope='project' AND (project_key=? OR project_key IS NULL)))", (project_key,)
    return "scope='global'", ()


def _edge_for(left: dict[str, Any], right: dict[str, Any]) -> tuple[str | None, float, dict[str, Any]]:
    if left.get("scope") == "project" and right.get("scope") == "project":
        left_key = left.get("project_key")
        right_key = right.get("project_key")
        if left_key and right_key and left_key != right_key:
            return None, 0.0, {}
    triggers = set(str(item).lower() for item in left.get("triggers_json") or [])
    other_triggers = set(str(item).lower() for item in right.get("triggers_json") or [])
    shared = sorted(triggers & other_triggers)
    if left.get("domain") == right.get("domain") and left.get("subcategory") == right.get("subcategory"):
        return "same_subcategory", 0.8, {"domain": left.get("domain"), "subcategory": left.get("subcategory")}
    if len(shared) >= 2:
        return "shared_triggers", 0.7, {"shared_triggers": shared[:8]}
    if left.get("domain") == right.get("domain") and left.get("category") == right.get("category"):
        return "same_category", 0.6, {"domain": left.get("domain"), "category": left.get("category")}
    return None, 0.0, {}


def _assistant_uses_memory(assistant_message: str, memory: dict[str, Any]) -> bool:
    text = assistant_message.lower()
    if not text:
        return False
    triggers = [str(item).lower() for item in memory.get("triggers_json") or [] if len(str(item).strip()) >= 2]
    if any(trigger in text for trigger in triggers[:8]):
        return True
    content_tokens = _tokens(str(memory.get("content") or ""))
    assistant_tokens = _tokens(text)
    return len(content_tokens & assistant_tokens) >= 2


def re_split(text: str) -> list[str]:
    import re

    return re.findall(r"[\w\u4e00-\u9fff]+", text)


def _duplicate_memory_types(memory_type: str) -> tuple[str, ...]:
    if memory_type in {"project_context", "experience", "fact"}:
        return ("project_context", "experience", "fact")
    return (memory_type,)


def _polarity(text: str) -> int:
    lowered = text.lower()
    architecture_polarity = _architecture_relation_polarity(lowered)
    if architecture_polarity:
        return architecture_polarity
    negative = (
        "不是",
        "不能",
        "不要",
        "不允许",
        "不希望",
        "不喜欢",
        "不推荐",
        "不应",
        "不得",
        "禁用",
        "关闭",
        "disable",
        "not ",
        "never",
    )
    positive = ("要", "需", "需要", "应该", "应把", "应将", "应当", "应分", "应作为", "应支持", "应采用", "必须", "启用", "打开", "enable", "always")
    if any(item in lowered for item in negative):
        return -1
    if any(item in lowered for item in positive):
        return 1
    return 0


def _architecture_relation_polarity(lowered: str) -> int:
    if not ("mcp" in lowered and "hook" in lowered):
        return 0
    separated = (
        "不重叠",
        "不互相调用",
        "不能互相调用",
        "不得互相调用",
        "不允许互相调用",
        "不能调用",
        "不得调用",
        "不允许调用",
        "路径隔离",
        "路径分离",
        "两条路径",
        "两条不重叠",
        "不能互调",
        "不得互调",
        "不允许互调",
    )
    if any(item in lowered for item in separated):
        return -1
    integrated = (
        "互相调用",
        "互调",
        "统一链路",
        "形成统一链路",
        "合并链路",
        "单一链路",
        "同一链路",
        "链路统一",
    )
    if any(item in lowered for item in integrated):
        return 1
    return 0
