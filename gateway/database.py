from __future__ import annotations

import json
import sqlite3
import time
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator


class ConflictError(Exception):
    pass


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 10000")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS users (
                    id TEXT PRIMARY KEY,
                    email TEXT NOT NULL COLLATE NOCASE UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );

                CREATE TABLE IF NOT EXISTS web_sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_web_sessions_user ON web_sessions(user_id);

                CREATE TABLE IF NOT EXISTS mcp_keys (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    scope TEXT NOT NULL DEFAULT 'legacy' CHECK (scope IN ('legacy', 'plan')),
                    created_at INTEGER NOT NULL,
                    last_used_at INTEGER,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_mcp_keys_user ON mcp_keys(user_id);

                CREATE TABLE IF NOT EXISTS enrollment_tokens (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    prefix TEXT NOT NULL,
                    token_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL,
                    used_at INTEGER,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_enrollment_user ON enrollment_tokens(user_id);

                CREATE TABLE IF NOT EXISTS agents (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    profiles_json TEXT NOT NULL,
                    version_json TEXT NOT NULL DEFAULT '{}',
                    features_json TEXT NOT NULL DEFAULT '{}',
                    runtimes_json TEXT NOT NULL DEFAULT '[]',
                    device_json TEXT NOT NULL DEFAULT '{}',
                    environment TEXT NOT NULL DEFAULT '',
                    tags_json TEXT NOT NULL DEFAULT '[]',
                    shell_policy_mode TEXT NOT NULL DEFAULT 'off'
                        CHECK (shell_policy_mode IN ('off', 'standard')),
                    plan_l1_paths_json TEXT NOT NULL DEFAULT '[]',
                    plan_policy_revision INTEGER NOT NULL DEFAULT 0,
                    credential_prefix TEXT NOT NULL,
                    credential_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);

                CREATE TABLE IF NOT EXISTS execution_stops (
                    scope TEXT NOT NULL CHECK (scope IN ('global', 'account', 'device', 'runtime', 'operation')),
                    user_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    subject TEXT NOT NULL,
                    stopped INTEGER NOT NULL CHECK (stopped IN (0, 1)),
                    revision INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (scope, user_id, agent_id, subject)
                );
                CREATE INDEX IF NOT EXISTS idx_execution_stops_revision ON execution_stops(revision);
                CREATE INDEX IF NOT EXISTS idx_execution_stops_user ON execution_stops(user_id);

                CREATE TABLE IF NOT EXISTS agent_bundles (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    enrollment_token_id TEXT NOT NULL REFERENCES enrollment_tokens(id) ON DELETE CASCADE,
                    name TEXT NOT NULL,
                    description TEXT NOT NULL DEFAULT '',
                    runtime TEXT NOT NULL,
                    profiles_json TEXT NOT NULL,
                    filename TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_agent_bundles_user ON agent_bundles(user_id);

                CREATE TABLE IF NOT EXISTS tasks (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    objective TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_selector_json TEXT NOT NULL DEFAULT '{}',
                    requested_runtime TEXT,
                    risk_policy_json TEXT NOT NULL DEFAULT '{}',
                    approval_policy_json TEXT NOT NULL DEFAULT '{}',
                    resource_budget_json TEXT NOT NULL DEFAULT '{}',
                    deadline INTEGER,
                    result_json TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_tasks_user_updated
                    ON tasks(user_id, updated_at DESC);

                CREATE TABLE IF NOT EXISTS health_monitors (
                    agent_id TEXT PRIMARY KEY REFERENCES agents(id) ON DELETE CASCADE,
                    lease_id TEXT NOT NULL,
                    interval_minutes INTEGER NOT NULL CHECK (interval_minutes IN (15, 30, 60)),
                    expires_at INTEGER NOT NULL,
                    next_due_at INTEGER NOT NULL,
                    execution_revision INTEGER NOT NULL,
                    last_task_id TEXT REFERENCES tasks(id) ON DELETE SET NULL,
                    sample_json TEXT,
                    consecutive_failures INTEGER NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_health_monitors_due ON health_monitors(next_due_at);

                CREATE TABLE IF NOT EXISTS task_plans (
                    task_id TEXT PRIMARY KEY REFERENCES tasks(id) ON DELETE CASCADE,
                    plan_hash TEXT NOT NULL,
                    plan_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS task_steps (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    action_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    target_agent_id TEXT,
                    depends_on_json TEXT NOT NULL DEFAULT '[]',
                    required_capabilities_json TEXT NOT NULL DEFAULT '[]',
                    inputs_json TEXT NOT NULL DEFAULT '{}',
                    expected_result_json TEXT NOT NULL DEFAULT '{}',
                    retry_policy_json TEXT NOT NULL DEFAULT '{}',
                    idempotency_key TEXT NOT NULL UNIQUE,
                    started_at INTEGER,
                    completed_at INTEGER,
                    result_json TEXT,
                    error TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_task_steps_task
                    ON task_steps(task_id);
                CREATE INDEX IF NOT EXISTS idx_task_steps_agent
                    ON task_steps(target_agent_id);

                CREATE TABLE IF NOT EXISTS task_actions (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    step_id TEXT NOT NULL REFERENCES task_steps(id) ON DELETE CASCADE,
                    agent_id TEXT NOT NULL,
                    profile TEXT NOT NULL,
                    action TEXT NOT NULL,
                    params_json TEXT NOT NULL DEFAULT '{}',
                    scope_json TEXT NOT NULL DEFAULT '{}',
                    risk_level TEXT NOT NULL,
                    policy_context_json TEXT NOT NULL DEFAULT '{}',
                    approval_requirement TEXT NOT NULL,
                    expires_at INTEGER,
                    idempotency_key TEXT NOT NULL UNIQUE,
                    status TEXT NOT NULL DEFAULT 'created',
                    result_json TEXT,
                    error TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    completed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_task_actions_task
                    ON task_actions(task_id);
                CREATE INDEX IF NOT EXISTS idx_task_actions_agent
                    ON task_actions(agent_id);

                CREATE TABLE IF NOT EXISTS task_approvals (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    plan_hash TEXT NOT NULL,
                    approved_scope_json TEXT NOT NULL DEFAULT '{}',
                    decision TEXT NOT NULL,
                    approver_user_id TEXT REFERENCES users(id) ON DELETE SET NULL,
                    reason TEXT NOT NULL DEFAULT '',
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_task_approvals_task
                    ON task_approvals(task_id, created_at DESC);

                CREATE TABLE IF NOT EXISTS task_events (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    event_type TEXT NOT NULL,
                    payload_json TEXT NOT NULL DEFAULT '{}',
                    sequence INTEGER NOT NULL,
                    step_id TEXT,
                    agent_id TEXT,
                    created_at INTEGER NOT NULL,
                    UNIQUE(task_id, sequence)
                );
                CREATE INDEX IF NOT EXISTS idx_task_events_task
                    ON task_events(task_id, sequence);

                CREATE TABLE IF NOT EXISTS task_evidence (
                    id TEXT PRIMARY KEY,
                    task_id TEXT NOT NULL REFERENCES tasks(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    summary_json TEXT NOT NULL DEFAULT '{}',
                    content_hash TEXT,
                    storage_ref TEXT,
                    step_id TEXT,
                    agent_id TEXT,
                    created_at INTEGER NOT NULL,
                    expires_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_task_evidence_task
                    ON task_evidence(task_id, created_at);

                CREATE TABLE IF NOT EXISTS risk_alerts (
                    id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                    task_id TEXT REFERENCES tasks(id) ON DELETE CASCADE,
                    step_id TEXT,
                    agent_id TEXT,
                    action TEXT,
                    basis_json TEXT NOT NULL DEFAULT '{}',
                    severity TEXT NOT NULL,
                    category TEXT NOT NULL,
                    trigger TEXT NOT NULL,
                    recommendation TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL CHECK (status IN
                        ('open', 'acknowledged', 'approved', 'blocked', 'resolved', 'ignored')),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    resolved_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_risk_alerts_owner ON risk_alerts(user_id, updated_at DESC);
                CREATE INDEX IF NOT EXISTS idx_risk_alerts_task ON risk_alerts(task_id, created_at);
                CREATE INDEX IF NOT EXISTS idx_risk_alerts_device ON risk_alerts(agent_id, category, status);

                CREATE TABLE IF NOT EXISTS risk_alert_transitions (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    alert_id TEXT NOT NULL REFERENCES risk_alerts(id) ON DELETE CASCADE,
                    previous_status TEXT,
                    status TEXT NOT NULL,
                    source TEXT NOT NULL,
                    actor_user_id TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_risk_alert_transitions ON risk_alert_transitions(alert_id, sequence);

                CREATE TABLE IF NOT EXISTS runtime_sessions (
                    id TEXT PRIMARY KEY,
                    agent_id TEXT NOT NULL REFERENCES agents(id) ON DELETE CASCADE,
                    provider_id TEXT NOT NULL,
                    runtime_version TEXT NOT NULL,
                    adapter_version TEXT NOT NULL,
                    mode TEXT NOT NULL,
                    status TEXT NOT NULL,
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    started_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    ended_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_runtime_sessions_agent
                    ON runtime_sessions(agent_id, last_seen_at DESC);
                """
            )
            agent_columns = {
                row["name"] for row in connection.execute("PRAGMA table_info(agents)").fetchall()
            }
            if "version_json" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN version_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "shell_policy_mode" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN shell_policy_mode TEXT NOT NULL DEFAULT 'off'"
                )
            if "features_json" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN features_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "runtimes_json" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN runtimes_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "device_json" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN device_json TEXT NOT NULL DEFAULT '{}'"
                )
            if "environment" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN environment TEXT NOT NULL DEFAULT ''")
            if "tags_json" not in agent_columns:
                connection.execute("ALTER TABLE agents ADD COLUMN tags_json TEXT NOT NULL DEFAULT '[]'")
            if "plan_l1_paths_json" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN plan_l1_paths_json TEXT NOT NULL DEFAULT '[]'"
                )
            if "plan_policy_revision" not in agent_columns:
                connection.execute(
                    "ALTER TABLE agents ADD COLUMN plan_policy_revision INTEGER NOT NULL DEFAULT 0"
                )
            key_columns = {row["name"] for row in connection.execute("PRAGMA table_info(mcp_keys)")}
            if "scope" not in key_columns:
                connection.execute(
                    "ALTER TABLE mcp_keys ADD COLUMN scope TEXT NOT NULL DEFAULT 'legacy' "
                    "CHECK (scope IN ('legacy', 'plan'))"
                )
            if connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'task_alerts'"
            ).fetchone():
                connection.execute(
                    """
                    INSERT OR IGNORE INTO risk_alerts (
                        id, user_id, task_id, agent_id, severity, category, trigger, recommendation,
                        status, created_at, updated_at, resolved_at
                    )
                    SELECT alerts.id, tasks.user_id, alerts.task_id, alerts.agent_id, alerts.severity,
                           alerts.category, alerts.trigger, alerts.recommendation, alerts.status,
                           alerts.created_at, COALESCE(alerts.resolved_at, alerts.created_at), alerts.resolved_at
                    FROM task_alerts AS alerts JOIN tasks ON tasks.id = alerts.task_id
                    """
                )

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def create_user(self, email: str, password_hash: str) -> dict[str, Any]:
        now = int(time.time())
        user_id = str(uuid.uuid4())
        try:
            with self.connect() as connection:
                connection.execute(
                    "INSERT INTO users (id, email, password_hash, created_at) VALUES (?, ?, ?, ?)",
                    (user_id, email.strip().lower(), password_hash, now),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该邮箱已注册") from exc
        return {"id": user_id, "email": email.strip().lower(), "created_at": now}

    def get_user_by_email(self, email: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM users WHERE email = ? COLLATE NOCASE", (email.strip(),)
            ).fetchone()
        return self._row(row)

    def get_user(self, user_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id, email, created_at FROM users WHERE id = ?", (user_id,)
            ).fetchone()
        return self._row(row)

    def create_session(self, token_hash: str, user_id: str, expires_at: int) -> None:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO web_sessions VALUES (?, ?, ?, ?, ?)",
                (token_hash, user_id, expires_at, now, now),
            )

    def get_session_user(self, token_hash: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT users.id, users.email, users.created_at
                FROM web_sessions
                JOIN users ON users.id = web_sessions.user_id
                WHERE web_sessions.token_hash = ? AND web_sessions.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE web_sessions SET last_seen_at = ? WHERE token_hash = ?",
                    (now, token_hash),
                )
        return self._row(row)

    def delete_session(self, token_hash: str) -> None:
        with self.connect() as connection:
            connection.execute("DELETE FROM web_sessions WHERE token_hash = ?", (token_hash,))

    def create_mcp_key(
        self, user_id: str, name: str, prefix: str, token_hash: str, *, scope: str = "legacy"
    ) -> dict[str, Any]:
        key_id = str(uuid.uuid4())
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_keys
                    (id, user_id, name, prefix, token_hash, scope, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (key_id, user_id, name, prefix, token_hash, scope, now),
            )
        return {"id": key_id, "name": name, "prefix": prefix, "scope": scope, "created_at": now}

    def list_mcp_keys(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, prefix, scope, created_at, last_used_at, revoked_at
                FROM mcp_keys WHERE user_id = ? ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_mcp_key(self, user_id: str, key_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE mcp_keys SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ? AND user_id = ?
                """,
                (int(time.time()), key_id, user_id),
            )
        return cursor.rowcount > 0

    def verify_mcp_key(self, token_hash: str) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT mcp_keys.id AS key_id, mcp_keys.user_id, mcp_keys.scope, users.email
                FROM mcp_keys
                JOIN users ON users.id = mcp_keys.user_id
                WHERE mcp_keys.token_hash = ? AND mcp_keys.revoked_at IS NULL
                """,
                (token_hash,),
            ).fetchone()
            if row is not None:
                connection.execute(
                    "UPDATE mcp_keys SET last_used_at = ? WHERE id = ?", (now, row["key_id"])
                )
        return self._row(row)

    def is_active_plan_key(self, user_id: str, key_id: str) -> bool:
        with self.connect() as connection:
            return connection.execute(
                "SELECT 1 FROM mcp_keys WHERE id = ? AND user_id = ? AND scope = 'plan' AND revoked_at IS NULL",
                (key_id, user_id),
            ).fetchone() is not None

    def create_enrollment_token(
        self,
        user_id: str,
        name: str,
        prefix: str,
        token_hash: str,
        expires_at: int,
    ) -> dict[str, Any]:
        token_id = str(uuid.uuid4())
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO enrollment_tokens
                    (id, user_id, name, prefix, token_hash, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (token_id, user_id, name, prefix, token_hash, now, expires_at),
            )
        return {
            "id": token_id,
            "name": name,
            "prefix": prefix,
            "created_at": now,
            "expires_at": expires_at,
        }

    def list_enrollment_tokens(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, prefix, created_at, expires_at, used_at, revoked_at
                FROM enrollment_tokens WHERE user_id = ? ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [dict(row) for row in rows]

    def revoke_enrollment_token(self, user_id: str, token_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE enrollment_tokens SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ? AND user_id = ? AND used_at IS NULL
                """,
                (int(time.time()), token_id, user_id),
            )
        return cursor.rowcount > 0

    def register_agent(
        self,
        enrollment_hash: str,
        agent_id: str,
        credential_prefix: str,
        credential_hash: str,
        name: str,
        description: str,
        profile_declarations: list[dict[str, Any]],
        version: dict[str, Any],
        features: dict[str, Any] | None = None,
        runtimes: list[dict[str, Any]] | None = None,
        *,
        device: dict[str, Any] | None = None,
    ) -> dict[str, Any] | None:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            enrollment = connection.execute(
                """
                SELECT id, user_id FROM enrollment_tokens
                WHERE token_hash = ? AND used_at IS NULL AND revoked_at IS NULL AND expires_at > ?
                """,
                (enrollment_hash, now),
            ).fetchone()
            if enrollment is None:
                return None
            connection.execute(
                "UPDATE enrollment_tokens SET used_at = ? WHERE id = ?",
                (now, enrollment["id"]),
            )
            connection.execute(
                """
                INSERT INTO agents
                    (id, user_id, name, description, profiles_json, version_json,
                     features_json, runtimes_json, device_json, credential_prefix, credential_hash,
                     created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    enrollment["user_id"],
                    name,
                    description,
                    json.dumps(profile_declarations, separators=(",", ":")),
                    json.dumps(version, separators=(",", ":")),
                    json.dumps(features or {}, separators=(",", ":")),
                    json.dumps(runtimes or [], separators=(",", ":")),
                    json.dumps(device or {}, separators=(",", ":")),
                    credential_prefix,
                    credential_hash,
                    now,
                    now,
                ),
            )
        return {"id": agent_id, "user_id": enrollment["user_id"]}

    def authenticate_agent(self, agent_id: str, credential_hash: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, name, description, profiles_json, version_json,
                       features_json, runtimes_json, shell_policy_mode,
                       device_json, environment, tags_json,
                       plan_l1_paths_json, plan_policy_revision,
                       created_at, last_seen_at
                FROM agents
                WHERE id = ? AND credential_hash = ? AND revoked_at IS NULL
                """,
                (agent_id, credential_hash),
            ).fetchone()
        return self._agent_row(row)

    @staticmethod
    def _agent_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        raw_declarations = json.loads(result.pop("profiles_json"))
        declarations = (
            [
                declaration
                for declaration in raw_declarations
                if isinstance(declaration, dict)
                and isinstance(declaration.get("id"), str)
                and isinstance(declaration.get("runtime"), dict)
            ]
            if isinstance(raw_declarations, list)
            else []
        )
        result["profile_declarations"] = declarations
        result["profiles"] = [declaration["id"] for declaration in declarations]
        raw_version = json.loads(result.pop("version_json"))
        result["version"] = raw_version if isinstance(raw_version, dict) else {}
        raw_features = json.loads(result.pop("features_json", "{}"))
        result["features"] = raw_features if isinstance(raw_features, dict) else {}
        raw_runtimes = json.loads(result.pop("runtimes_json", "[]"))
        result["runtimes"] = raw_runtimes if isinstance(raw_runtimes, list) else []
        result["device"] = json.loads(result.pop("device_json"))
        result["environment"] = result["environment"] or None
        result["tags"] = json.loads(result.pop("tags_json"))
        mode = result.get("shell_policy_mode")
        result["shell_policy_mode"] = mode if mode in {"off", "standard"} else "off"
        result["plan_policy"] = {
            "l1_paths": json.loads(result.pop("plan_l1_paths_json")),
            "revision": result.pop("plan_policy_revision"),
        }
        return result

    def update_agent_runtime(
        self,
        agent_id: str,
        declarations: list[dict[str, Any]],
        version: dict[str, Any],
        features: dict[str, Any] | None = None,
        runtimes: list[dict[str, Any]] | None = None,
        *,
        device: dict[str, Any] | None = None,
    ) -> None:
        with self.connect() as connection:
            connection.execute(
                """
                UPDATE agents SET profiles_json = ?, version_json = ?,
                                   features_json = ?, runtimes_json = ?, device_json = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (
                    json.dumps(declarations, separators=(",", ":")),
                    json.dumps(version, separators=(",", ":")),
                    json.dumps(features or {}, separators=(",", ":")),
                    json.dumps(runtimes or [], separators=(",", ":")),
                    json.dumps(device or {}, separators=(",", ":")),
                    agent_id,
                ),
            )

    def touch_agent(self, agent_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE agents SET last_seen_at = ? WHERE id = ?", (int(time.time()), agent_id)
            )

    def list_agents(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, name, description, profiles_json, version_json,
                       features_json, runtimes_json, shell_policy_mode,
                       device_json, environment, tags_json,
                       plan_l1_paths_json, plan_policy_revision,
                       created_at, last_seen_at, revoked_at
                FROM agents
                WHERE user_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC, id
                """,
                (user_id,),
            ).fetchall()
        return [self._agent_row(row) for row in rows if row is not None]

    def get_owned_agent(self, user_id: str, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, name, description, profiles_json, version_json,
                       features_json, runtimes_json, shell_policy_mode,
                       device_json, environment, tags_json,
                       plan_l1_paths_json, plan_policy_revision,
                       created_at, last_seen_at, revoked_at
                FROM agents WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (agent_id, user_id),
            ).fetchone()
        return self._agent_row(row)

    def update_agent(
        self, user_id: str, agent_id: str, name: str, description: str,
        *, environment: str | None = None, tags: list[str] | None = None,
    ) -> bool:
        tags_json = json.dumps(sorted(set(tags)), separators=(",", ":")) if tags is not None else None
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agents SET name = ?, description = ?,
                    environment = COALESCE(?, environment), tags_json = COALESCE(?, tags_json)
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (name, description, environment, tags_json, agent_id, user_id),
            )
        return cursor.rowcount > 0

    def update_agent_shell_policy(
        self, user_id: str, agent_id: str, mode: str
    ) -> bool:
        if mode not in {"off", "standard"}:
            raise ValueError("shell_policy_mode 无效")
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agents SET shell_policy_mode = ?
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (mode, agent_id, user_id),
            )
        return cursor.rowcount > 0

    def update_agent_plan_policy(self, user_id: str, agent_id: str, l1_paths: list[str]) -> bool:
        paths_json = json.dumps(sorted(set(l1_paths)), separators=(",", ":"))
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agents SET plan_l1_paths_json = ?,
                    plan_policy_revision = plan_policy_revision + (plan_l1_paths_json != ?)
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (paths_json, paths_json, agent_id, user_id),
            )
        return cursor.rowcount > 0

    def revoke_agent(self, user_id: str, agent_id: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agents SET revoked_at = COALESCE(revoked_at, ?)
                WHERE id = ? AND user_id = ?
                """,
                (int(time.time()), agent_id, user_id),
            )
        return cursor.rowcount > 0

    def execution_revision(self) -> int:
        with self.connect() as connection:
            return connection.execute("SELECT COALESCE(MAX(revision), 0) FROM execution_stops").fetchone()[0]

    def set_execution_stop(
        self, scope: str, *, stopped: bool, user_id: str = "", agent_id: str = "", subject: str = "",
    ) -> None:
        key = (scope, user_id, agent_id, subject)
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT stopped, revision FROM execution_stops "
                "WHERE scope = ? AND user_id = ? AND agent_id = ? AND subject = ?", key,
            ).fetchone()
            if (row is None and not stopped) or (row is not None and bool(row["stopped"]) == stopped):
                return
            revision = (
                connection.execute("SELECT COALESCE(MAX(revision), 0) + 1 FROM execution_stops").fetchone()[0]
                if stopped else row["revision"]
            )
            connection.execute(
                """
                INSERT INTO execution_stops (scope, user_id, agent_id, subject, stopped, revision, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (scope, user_id, agent_id, subject) DO UPDATE SET
                    stopped = excluded.stopped, revision = excluded.revision, updated_at = excluded.updated_at
                """,
                (*key, int(stopped), revision, int(time.time())),
            )

    def execution_stops(self, user_id: str = "") -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [
                {**dict(row), "stopped": bool(row["stopped"])}
                for row in connection.execute(
                    """
                    SELECT scope, agent_id, subject, stopped, updated_at
                    FROM execution_stops WHERE (scope = 'global' OR user_id = ?) AND stopped = 1
                    ORDER BY scope, agent_id, subject
                    """,
                    (user_id,),
                )
            ]

    def execution_blocked(
        self, user_id: str, agent_id: str, *, runtime: str = "", operation: str = "",
        revision: int | None = None,
    ) -> bool:
        with self.connect() as connection:
            return connection.execute(
                """
                SELECT 1 FROM execution_stops
                WHERE (stopped = 1 OR revision > ?)
                  AND (scope = 'global' OR (user_id = ? AND (
                      scope = 'account'
                      OR (scope = 'device' AND agent_id = ?)
                      OR (scope = 'runtime' AND agent_id = ? AND subject = ?)
                      OR (scope = 'operation' AND subject = ?)
                  )))
                LIMIT 1
                """,
                (revision, user_id, agent_id, agent_id, runtime, operation),
            ).fetchone() is not None

    def list_health_monitors(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                """
                SELECT monitor.agent_id, monitor.interval_minutes, monitor.expires_at, monitor.next_due_at,
                       monitor.last_task_id, monitor.execution_revision
                FROM health_monitors AS monitor JOIN agents ON agents.id = monitor.agent_id
                WHERE agents.user_id = ? AND agents.revoked_at IS NULL ORDER BY monitor.agent_id
                """,
                (user_id,),
            )]

    def set_health_monitor(
        self, user_id: str, agent_id: str, interval_minutes: int | None, *, now: int,
        expires_at: int, max_per_owner: int, execution_revision: int,
    ) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if connection.execute(
                "SELECT 1 FROM agents WHERE user_id = ? AND id = ? AND revoked_at IS NULL",
                (user_id, agent_id),
            ).fetchone() is None:
                return False
            if interval_minutes is None:
                connection.execute("DELETE FROM health_monitors WHERE agent_id = ?", (agent_id,))
                return True
            active = connection.execute(
                """
                SELECT COUNT(*) FROM health_monitors AS monitor JOIN agents ON agents.id = monitor.agent_id
                WHERE agents.user_id = ? AND agents.revoked_at IS NULL AND monitor.expires_at > ?
                  AND monitor.agent_id != ?
                """,
                (user_id, now, agent_id),
            ).fetchone()[0]
            if active >= max_per_owner:
                raise ConflictError(f"每个账号最多同时启用 {max_per_owner} 台设备的巡检")
            connection.execute(
                """
                INSERT INTO health_monitors (agent_id, lease_id, interval_minutes, expires_at, next_due_at,
                                             execution_revision)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT (agent_id) DO UPDATE SET
                    lease_id = excluded.lease_id, interval_minutes = excluded.interval_minutes,
                    expires_at = excluded.expires_at, next_due_at = excluded.next_due_at,
                    execution_revision = excluded.execution_revision, last_task_id = NULL,
                    sample_json = NULL, consecutive_failures = 0
                """,
                (agent_id, uuid.uuid4().hex, interval_minutes, expires_at, now, execution_revision),
            )
            return True

    def defer_overdue_health_monitors(self, now: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE health_monitors SET next_due_at = ? + interval_minutes * 60 WHERE next_due_at <= ?",
                (now, now),
            )

    def claim_due_health_monitors(self, now: int, limit: int) -> list[dict[str, Any]]:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            monitors = connection.execute(
                """
                SELECT monitor.*, agents.user_id
                FROM health_monitors AS monitor JOIN agents ON agents.id = monitor.agent_id
                WHERE monitor.next_due_at <= ? AND monitor.expires_at > ? AND agents.revoked_at IS NULL
                ORDER BY monitor.next_due_at, monitor.agent_id LIMIT ?
                """,
                (now, now, limit),
            ).fetchall()
            connection.executemany(
                "UPDATE health_monitors SET next_due_at = ? WHERE agent_id = ?",
                [(now + monitor["interval_minutes"] * 60, monitor["agent_id"]) for monitor in monitors],
            )
            return [{**dict(monitor), "sample": self._json_load(monitor["sample_json"], None)} for monitor in monitors]

    def record_health_monitor_task(self, agent_id: str, lease_id: str, task_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE health_monitors SET last_task_id = ? WHERE agent_id = ? AND lease_id = ?",
                (task_id, agent_id, lease_id),
            )

    def record_health_monitor_sample(
        self, agent_id: str, lease_id: str, sample: dict[str, Any] | None, failures: int,
    ) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE health_monitors
                SET sample_json = COALESCE(?, sample_json), consecutive_failures = ?
                WHERE agent_id = ? AND lease_id = ?
                """,
                (json.dumps(sample) if sample is not None else None, failures, agent_id, lease_id),
            )
            return cursor.rowcount == 1

    @staticmethod
    def _json_load(value: object, default: Any) -> Any:
        if not isinstance(value, str):
            return default
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return default

    @classmethod
    def _task_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field_name, default in (
            ("target_selector_json", {}),
            ("risk_policy_json", {}),
            ("approval_policy_json", {}),
            ("resource_budget_json", {}),
            ("result_json", None),
        ):
            raw = result.pop(field_name, None)
            result[field_name.removesuffix("_json")] = cls._json_load(raw, default)
        return result

    @classmethod
    def _task_step_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field_name, default in (
            ("depends_on_json", []),
            ("required_capabilities_json", []),
            ("inputs_json", {}),
            ("expected_result_json", {}),
            ("retry_policy_json", {}),
            ("result_json", None),
        ):
            raw = result.pop(field_name, None)
            result[field_name.removesuffix("_json")] = cls._json_load(raw, default)
        return result

    @classmethod
    def _task_action_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        for field_name, default in (
            ("params_json", {}),
            ("scope_json", {}),
            ("policy_context_json", {}),
            ("result_json", None),
        ):
            raw = result.pop(field_name, None)
            result[field_name.removesuffix("_json")] = cls._json_load(raw, default)
        return result

    @classmethod
    def _task_approval_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("approved_scope_json", None)
        result["approved_scope"] = cls._json_load(raw, {})
        return result

    @classmethod
    def _task_event_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("payload_json", None)
        result["payload"] = cls._json_load(raw, {})
        return result

    @classmethod
    def _task_evidence_row(cls, row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("summary_json", None)
        result["summary"] = cls._json_load(raw, {})
        return result

    def create_task(
        self,
        user_id: str,
        objective: str,
        *,
        task_id: str | None = None,
        status: str = "created",
        target_selector: dict[str, Any] | None = None,
        requested_runtime: str | None = None,
        risk_policy: dict[str, Any] | None = None,
        approval_policy: dict[str, Any] | None = None,
        resource_budget: dict[str, Any] | None = None,
        deadline: int | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        task_id = task_id or str(uuid.uuid4())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO tasks
                    (id, user_id, objective, status, target_selector_json,
                     requested_runtime, risk_policy_json, approval_policy_json,
                     resource_budget_json, deadline, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    task_id,
                    user_id,
                    objective,
                    status,
                    json.dumps(target_selector or {}, separators=(",", ":")),
                    requested_runtime,
                    json.dumps(risk_policy or {}, separators=(",", ":")),
                    json.dumps(approval_policy or {}, separators=(",", ":")),
                    json.dumps(resource_budget or {}, separators=(",", ":")),
                    deadline,
                    now,
                    now,
                ),
            )
        return self.get_task(user_id, task_id)  # type: ignore[return-value]

    def get_task(self, user_id: str, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM tasks WHERE id = ? AND user_id = ?",
                (task_id, user_id),
            ).fetchone()
        return self._task_row(row)

    @staticmethod
    def _select_task_plan(
        connection: sqlite3.Connection, user_id: str, task_id: str,
    ) -> sqlite3.Row | None:
        return connection.execute(
            """
            SELECT task_plans.*, tasks.status
            FROM task_plans JOIN tasks ON tasks.id = task_plans.task_id
            WHERE task_plans.task_id = ? AND tasks.user_id = ?
            """,
            (task_id, user_id),
        ).fetchone()

    def get_task_plan(self, user_id: str, task_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = self._select_task_plan(connection, user_id, task_id)
        if row is None:
            return None
        return {"hash": row["plan_hash"], **json.loads(row["plan_json"])}

    @staticmethod
    def _plan_event(
        connection: sqlite3.Connection, task_id: str, event_type: str, payload: dict[str, Any],
    ) -> None:
        connection.execute(
            """
            INSERT INTO task_events (id, task_id, event_type, payload_json, sequence, created_at)
            SELECT ?, ?, ?, ?, COALESCE(MAX(sequence), 0) + 1, ?
            FROM task_events WHERE task_id = ?
            """,
            (uuid.uuid4().hex, task_id, event_type, json.dumps(payload), int(time.time()), task_id),
        )

    def save_task_plan(
        self, user_id: str, task_id: str, plan_hash: str, plan: dict[str, Any],
        *, previous_hash: str | None = None,
    ) -> None:
        now = int(time.time())
        status = "waiting_approval" if plan["requires_approval"] else "approved"
        targets = json.dumps({"agent_ids": sorted(plan["targets"])})
        policy = json.dumps({"mode": "operation_plan", "max_level": plan["max_level"]})
        approval_policy = json.dumps({"required": plan["requires_approval"], "plan_hash": plan_hash, "single_use": True})
        budget = json.dumps({"max_actions": len(plan["steps"]), "timeout_seconds": plan["timeout_seconds"]})
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if previous_hash is None:
                connection.execute(
                    """
                    INSERT INTO tasks (id, user_id, objective, status, target_selector_json, risk_policy_json,
                                       approval_policy_json, resource_budget_json, deadline, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (task_id, user_id, plan["objective"], status, targets, policy,
                     approval_policy, budget, plan["valid_until"], now, now),
                )
                self._plan_event(connection, task_id, "task.created", {"kind": "operation_plan"})
            else:
                existing = self._select_task_plan(connection, user_id, task_id)
                if not existing or existing["plan_hash"] != previous_hash or existing["status"] not in {
                    "waiting_approval", "approved",
                }:
                    raise ConflictError("计划已更新或已开始执行，请刷新")
                connection.execute(
                    """
                    UPDATE tasks SET objective = ?, status = ?, target_selector_json = ?, risk_policy_json = ?,
                                     approval_policy_json = ?, resource_budget_json = ?, deadline = ?, updated_at = ?
                    WHERE id = ? AND user_id = ?
                    """,
                    (plan["objective"], status, targets, policy, approval_policy, budget, plan["valid_until"],
                     now, task_id, user_id),
                )
                connection.execute("DELETE FROM task_steps WHERE task_id = ?", (task_id,))
                self._plan_event(connection, task_id, "approval.invalidated", {"plan_hash": previous_hash})
            connection.execute(
                """
                INSERT INTO task_plans (task_id, plan_hash, plan_json) VALUES (?, ?, ?)
                ON CONFLICT(task_id) DO UPDATE SET plan_hash = excluded.plan_hash, plan_json = excluded.plan_json
                """,
                (task_id, plan_hash, json.dumps(plan)),
            )
            step_ids = {step["key"]: step["id"] for step in plan["steps"]}
            connection.executemany(
                """
                INSERT INTO task_steps (id, task_id, action_type, status, target_agent_id, depends_on_json,
                                        required_capabilities_json, inputs_json, expected_result_json, idempotency_key)
                VALUES (?, ?, ?, 'created', ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        step["id"], task_id, step["operation"], step["agent_id"],
                        json.dumps([step_ids[key] for key in step["depends_on"]]),
                        json.dumps([step["profile"]]), json.dumps(step["scope"]),
                        json.dumps({"verified": True}), step["id"],
                    )
                    for step in plan["steps"]
                ],
            )
            self._plan_event(connection, task_id, "task.planned", {
                "plan_hash": plan_hash, "revision": plan["revision"], "status": status, "risk_level": plan["max_level"],
            })

    def decide_task_plan(self, user_id: str, task_id: str, plan_hash: str, *, approve: bool) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_task_plan(connection, user_id, task_id)
            if not row or row["plan_hash"] != plan_hash or row["status"] != "waiting_approval":
                raise ConflictError("当前版本不在等待审批状态")
            plan = json.loads(row["plan_json"])
            maintenance = plan.get("maintenance")
            expires_at = plan["valid_until"] if maintenance else min(plan["valid_until"], now + 600)
            if expires_at <= now:
                raise ConflictError("计划已经过期，请重新提交")
            approval_id = uuid.uuid4().hex
            decision = "allow" if approve else "deny"
            scopes = {
                "single_use": True,
                "steps": [
                    {"id": step["id"], "agent_id": step["agent_id"], **step["scope"]}
                    for step in plan["steps"]
                ],
            }
            if maintenance:
                scopes["maintenance"] = maintenance
            connection.execute(
                """
                INSERT INTO task_approvals (id, task_id, plan_hash, approved_scope_json, decision,
                                            approver_user_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (approval_id, task_id, plan_hash, json.dumps(scopes), decision, user_id, now, expires_at),
            )
            connection.execute(
                "UPDATE tasks SET status = ?, updated_at = ? WHERE id = ?",
                ("approved" if approve else "cancelled", now, task_id),
            )
            if not approve:
                connection.execute(
                    "UPDATE task_steps SET status = 'cancelled', completed_at = ? WHERE task_id = ?", (now, task_id),
                )
            self._plan_event(connection, task_id, "approval.decided", {
                "approval_id": approval_id, "plan_hash": plan_hash, "decision": decision, "expires_at": expires_at,
            })
            record = connection.execute("SELECT * FROM task_approvals WHERE id = ?", (approval_id,)).fetchone()
        return self._task_approval_row(record)  # type: ignore[return-value]

    def claim_task_plan(self, user_id: str, task_id: str, plan_hash: str) -> tuple[str | None, int]:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._select_task_plan(connection, user_id, task_id)
            if not row or row["plan_hash"] != plan_hash or row["status"] != "approved":
                raise ConflictError("当前版本未批准、已消耗或状态已变化")
            plan = json.loads(row["plan_json"])
            maintenance = plan.get("maintenance")
            if maintenance and not maintenance["starts_at"] <= now < maintenance["ends_at"]:
                raise ConflictError("不在批准的维护窗口内")
            lease_end = min(plan["valid_until"], now + plan["timeout_seconds"])
            approval_id = None
            if plan["requires_approval"]:
                approval = connection.execute(
                    """
                    SELECT id, expires_at FROM task_approvals
                    WHERE task_id = ? AND plan_hash = ? AND approver_user_id = ?
                      AND decision = 'allow' AND expires_at > ?
                    """,
                    (task_id, plan_hash, user_id, now),
                ).fetchone()
                if not approval:
                    raise ConflictError("审批已过期或不匹配，请重新提交")
                approval_id = approval["id"]
                lease_end = min(lease_end, approval["expires_at"])
            if lease_end <= now:
                raise ConflictError("计划已过期")
            connection.execute(
                "UPDATE tasks SET status = 'queued', updated_at = ? WHERE id = ?", (now, task_id),
            )
            self._plan_event(connection, task_id, "task.queued", {
                "plan_hash": plan_hash, "approval_id": approval_id, "expires_at": lease_end,
            })
        return approval_id, lease_end

    def recover_interrupted_tasks(self) -> None:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """
                SELECT id FROM tasks
                WHERE status IN ('queued', 'running')
                   OR (status IN ('created', 'planned', 'approved')
                       AND json_extract(risk_policy_json, '$.mode') = 'read_only')
                """,
            ).fetchall()
            for row in rows:
                task_id = row["id"]
                completed = connection.execute(
                    "SELECT 1 FROM task_evidence WHERE task_id = ? AND kind != 'operation_plan_attempt' LIMIT 1", (task_id,),
                ).fetchone()
                status = "partially_completed" if completed else "failed"
                connection.execute(
                    "UPDATE tasks SET status = ?, error = 'gateway_interrupted', updated_at = ? WHERE id = ?",
                    (status, now, task_id),
                )
                for table in ("task_steps", "task_actions"):
                    connection.execute(
                        f"UPDATE {table} SET status = 'failed', error = 'gateway_interrupted', completed_at = ? "
                        "WHERE task_id = ? AND status IN ('created', 'running', 'allow')",
                        (now, task_id),
                    )
                self._plan_event(connection, task_id, "task.interrupted", {
                    "status": status, "automatic_retry": False,
                })
            connection.execute(
                "UPDATE runtime_sessions SET status = 'failed', ended_at = ? WHERE status = 'running' AND ended_at IS NULL",
                (now,),
            )

    def list_tasks(self, user_id: str, limit: int = 50) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 200))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM tasks
                WHERE user_id = ?
                ORDER BY updated_at DESC
                LIMIT ?
                """,
                (user_id, bounded_limit),
            ).fetchall()
        return [task for row in rows if (task := self._task_row(row)) is not None]

    def update_task(
        self,
        user_id: str,
        task_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
    ) -> bool:
        updates: list[str] = ["updated_at = ?"]
        values: list[Any] = [int(time.time())]
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if result is not None:
            updates.append("result_json = ?")
            values.append(json.dumps(result, separators=(",", ":")))
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        values.extend([task_id, user_id])
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE tasks SET {', '.join(updates)} WHERE id = ? AND user_id = ?",
                values,
            )
        return cursor.rowcount > 0

    def create_task_step(
        self,
        *,
        step_id: str,
        task_id: str,
        action_type: str,
        idempotency_key: str,
        status: str = "created",
        target_agent_id: str | None = None,
        depends_on: list[str] | None = None,
        required_capabilities: list[str] | None = None,
        inputs: dict[str, Any] | None = None,
        expected_result: dict[str, Any] | None = None,
        retry_policy: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO task_steps
                        (id, task_id, action_type, status, target_agent_id,
                         depends_on_json, required_capabilities_json, inputs_json,
                         expected_result_json, retry_policy_json, idempotency_key)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        step_id,
                        task_id,
                        action_type,
                        status,
                        target_agent_id,
                        json.dumps(depends_on or [], separators=(",", ":")),
                        json.dumps(required_capabilities or [], separators=(",", ":")),
                        json.dumps(inputs or {}, separators=(",", ":")),
                        json.dumps(expected_result or {}, separators=(",", ":")),
                        json.dumps(retry_policy or {}, separators=(",", ":")),
                        idempotency_key,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Task Step 或幂等键已存在") from exc
        return self.get_task_step(step_id)  # type: ignore[return-value]

    def get_task_step(self, step_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute("SELECT * FROM task_steps WHERE id = ?", (step_id,)).fetchone()
        return self._task_step_row(row)

    def list_task_steps(self, user_id: str, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT task_steps.*
                FROM task_steps
                JOIN tasks ON tasks.id = task_steps.task_id
                WHERE task_steps.task_id = ? AND tasks.user_id = ?
                ORDER BY task_steps.rowid
                """,
                (task_id, user_id),
            ).fetchall()
        return [step for row in rows if (step := self._task_step_row(row)) is not None]

    def update_task_step(
        self,
        step_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        started_at: int | None = None,
        completed_at: int | None = None,
    ) -> bool:
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if result is not None:
            updates.append("result_json = ?")
            values.append(json.dumps(result, separators=(",", ":")))
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        if started_at is not None:
            updates.append("started_at = ?")
            values.append(started_at)
        if completed_at is not None:
            updates.append("completed_at = ?")
            values.append(completed_at)
        if not updates:
            return False
        values.append(step_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE task_steps SET {', '.join(updates)} WHERE id = ?",
                values,
            )
        return cursor.rowcount > 0

    def create_task_action(
        self,
        *,
        action_id: str,
        task_id: str,
        step_id: str,
        agent_id: str,
        profile: str,
        action: str,
        params: dict[str, Any],
        idempotency_key: str,
        scope: dict[str, Any] | None = None,
        risk_level: str = "L0",
        policy_context: dict[str, Any] | None = None,
        approval_requirement: str = "allow",
        expires_at: int | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        try:
            with self.connect() as connection:
                connection.execute(
                    """
                    INSERT INTO task_actions
                        (id, task_id, step_id, agent_id, profile, action, params_json,
                         scope_json, risk_level, policy_context_json, approval_requirement,
                         expires_at, idempotency_key, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        action_id,
                        task_id,
                        step_id,
                        agent_id,
                        profile,
                        action,
                        json.dumps(params, separators=(",", ":")),
                        json.dumps(scope or {}, separators=(",", ":")),
                        risk_level,
                        json.dumps(policy_context or {}, separators=(",", ":")),
                        approval_requirement,
                        expires_at,
                        idempotency_key,
                        now,
                    ),
                )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("Action 幂等键已存在") from exc
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_actions WHERE id = ?", (action_id,)
            ).fetchone()
        return self._task_action_row(row)  # type: ignore[return-value]

    def get_task_action_by_idempotency(self, idempotency_key: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_actions WHERE idempotency_key = ?",
                (idempotency_key,),
            ).fetchone()
        return self._task_action_row(row)

    def update_task_action(
        self,
        action_id: str,
        *,
        status: str | None = None,
        result: dict[str, Any] | None = None,
        error: str | None = None,
        started_at: int | None = None,
        completed_at: int | None = None,
    ) -> bool:
        updates: list[str] = []
        values: list[Any] = []
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if result is not None:
            updates.append("result_json = ?")
            values.append(json.dumps(result, separators=(",", ":")))
        if error is not None:
            updates.append("error = ?")
            values.append(error)
        if started_at is not None:
            updates.append("started_at = ?")
            values.append(started_at)
        if completed_at is not None:
            updates.append("completed_at = ?")
            values.append(completed_at)
        if not updates:
            return False
        values.append(action_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE task_actions SET {', '.join(updates)} WHERE id = ?",
                values,
            )
        return cursor.rowcount > 0

    def create_task_approval(
        self,
        *,
        approval_id: str,
        task_id: str,
        plan_hash: str,
        approved_scope: dict[str, Any],
        decision: str,
        approver_user_id: str | None = None,
        reason: str = "",
        expires_at: int | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO task_approvals
                    (id, task_id, plan_hash, approved_scope_json, decision,
                     approver_user_id, reason, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    approval_id,
                    task_id,
                    plan_hash,
                    json.dumps(approved_scope, separators=(",", ":")),
                    decision,
                    approver_user_id,
                    reason,
                    now,
                    expires_at,
                ),
            )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_approvals WHERE id = ?", (approval_id,)
            ).fetchone()
        return self._task_approval_row(row)  # type: ignore[return-value]

    def get_task_approval(
        self, user_id: str, task_id: str, *, plan_hash: str | None = None,
    ) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT task_approvals.*
                FROM task_approvals
                JOIN tasks ON tasks.id = task_approvals.task_id
                WHERE task_approvals.task_id = ? AND tasks.user_id = ?
                  AND (? IS NULL OR task_approvals.plan_hash = ?)
                ORDER BY task_approvals.created_at DESC, task_approvals.rowid DESC
                LIMIT 1
                """,
                (task_id, user_id, plan_hash, plan_hash),
            ).fetchone()
        return self._task_approval_row(row)

    def append_task_event(
        self,
        task_id: str,
        event_type: str,
        payload: dict[str, Any],
        *,
        event_id: str | None = None,
        step_id: str | None = None,
        agent_id: str | None = None,
        sequence: int | None = None,
    ) -> dict[str, Any]:
        event_id = event_id or str(uuid.uuid4())
        now = int(time.time())
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM task_events WHERE id = ?", (event_id,)
            ).fetchone()
            if existing is not None:
                return self._task_event_row(existing)  # type: ignore[return-value]
            if sequence is None:
                row = connection.execute(
                    "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence "
                    "FROM task_events WHERE task_id = ?",
                    (task_id,),
                ).fetchone()
                sequence = int(row["next_sequence"])
            try:
                connection.execute(
                    """
                    INSERT INTO task_events
                        (id, task_id, event_type, payload_json, sequence,
                         step_id, agent_id, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_id,
                        task_id,
                        event_type,
                        json.dumps(payload, separators=(",", ":")),
                        sequence,
                        step_id,
                        agent_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise ConflictError("Task Event 序列或 ID 已存在") from exc
            row = connection.execute(
                "SELECT * FROM task_events WHERE id = ?", (event_id,)
            ).fetchone()
            return self._task_event_row(row)  # type: ignore[return-value]

    def list_task_events(
        self,
        user_id: str,
        task_id: str,
        *,
        after_sequence: int = 0,
        limit: int = 500,
    ) -> list[dict[str, Any]]:
        bounded_limit = max(1, min(limit, 2000))
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT task_events.*
                FROM task_events
                JOIN tasks ON tasks.id = task_events.task_id
                WHERE task_events.task_id = ?
                  AND tasks.user_id = ?
                  AND task_events.sequence > ?
                ORDER BY task_events.sequence
                LIMIT ?
                """,
                (task_id, user_id, after_sequence, bounded_limit),
            ).fetchall()
        return [event for row in rows if (event := self._task_event_row(row)) is not None]

    def create_task_evidence(
        self,
        *,
        evidence_id: str,
        task_id: str,
        kind: str,
        summary: dict[str, Any],
        content_hash: str | None = None,
        storage_ref: str | None = None,
        step_id: str | None = None,
        agent_id: str | None = None,
        expires_at: int | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO task_evidence
                    (id, task_id, kind, summary_json, content_hash, storage_ref,
                     step_id, agent_id, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    evidence_id,
                    task_id,
                    kind,
                    json.dumps(summary, separators=(",", ":")),
                    content_hash,
                    storage_ref,
                    step_id,
                    agent_id,
                    now,
                    expires_at,
                ),
            )
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM task_evidence WHERE id = ?", (evidence_id,)
            ).fetchone()
        return self._task_evidence_row(row)  # type: ignore[return-value]

    def list_task_evidence(self, user_id: str, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT task_evidence.*
                FROM task_evidence
                JOIN tasks ON tasks.id = task_evidence.task_id
                WHERE task_evidence.task_id = ? AND tasks.user_id = ?
                ORDER BY task_evidence.created_at
                """,
                (task_id, user_id),
            ).fetchall()
        return [item for row in rows if (item := self._task_evidence_row(row)) is not None]

    def create_task_alert(
        self,
        *,
        alert_id: str,
        task_id: str,
        severity: str,
        category: str,
        trigger: str,
        recommendation: str = "",
        agent_id: str | None = None,
        step_id: str | None = None,
        action: str | None = None,
        basis: dict[str, Any] | None = None,
        blocked: bool = False,
    ) -> dict[str, Any]:
        with self.connect() as connection:
            task = connection.execute("SELECT user_id FROM tasks WHERE id = ?", (task_id,)).fetchone()
            if task is None:
                return {}
            self._insert_risk_alert(
                connection, alert_id, task["user_id"], agent_id, category, trigger, recommendation,
                severity=severity, task_id=task_id, step_id=step_id, action=action, basis=basis,
                status="blocked" if blocked else "open",
            )
            return self._risk_alert_row(
                connection.execute("SELECT * FROM risk_alerts WHERE id = ?", (alert_id,)).fetchone()
            )

    def record_device_alert(
        self, user_id: str, agent_id: str, category: str, trigger: str, recommendation: str,
        *, basis: dict[str, Any], severity: str = "warning", action: str | None = None, blocked: bool = False,
    ) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            owned = connection.execute(
                "SELECT 1 FROM agents WHERE user_id = ? AND id = ? AND revoked_at IS NULL", (user_id, agent_id),
            ).fetchone()
            if owned is None:
                return
            pending = connection.execute(
                """
                SELECT 1 FROM risk_alerts WHERE user_id = ? AND agent_id = ? AND task_id IS NULL
                  AND category = ? AND status IN ('open', 'acknowledged', 'blocked') LIMIT 1
                """,
                (user_id, agent_id, category),
            ).fetchone()
            if pending is None:
                self._insert_risk_alert(
                    connection, f"alert_{uuid.uuid4().hex}", user_id, agent_id, category, trigger, recommendation,
                    severity=severity, basis=basis, action=action, status="blocked" if blocked else "open",
                )

    @staticmethod
    def _insert_risk_alert(
        connection: sqlite3.Connection, alert_id: str, user_id: str, agent_id: str | None,
        category: str, trigger: str, recommendation: str,
        *, severity: str, task_id: str | None = None, step_id: str | None = None, action: str | None = None,
        basis: dict[str, Any] | None = None, status: str = "open",
    ) -> None:
        now = int(time.time())
        connection.execute(
            """
            INSERT INTO risk_alerts (id, user_id, task_id, step_id, agent_id, action, basis_json, severity, category,
                                     trigger, recommendation, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (alert_id, user_id, task_id, step_id, agent_id, action, json.dumps(basis or {}),
             severity, category, trigger, recommendation, status, now, now),
        )
        connection.execute(
            "INSERT INTO risk_alert_transitions (alert_id, status, source, created_at) VALUES (?, ?, 'detected', ?)",
            (alert_id, status, now),
        )

    @classmethod
    def _risk_alert_row(cls, row: sqlite3.Row) -> dict[str, Any]:
        alert = dict(row)
        alert.pop("user_id")
        alert["basis"] = cls._json_load(alert.pop("basis_json"), {})
        return alert

    def list_alerts(self, user_id: str, *, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM risk_alerts WHERE user_id = ? AND (? IS NULL OR status = ?)
                ORDER BY updated_at DESC, created_at DESC, id LIMIT ?
                """,
                (user_id, status, status, max(1, min(limit, 200))),
            ).fetchall()
        return [self._risk_alert_row(row) for row in rows]

    def list_task_alerts(self, user_id: str, task_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM risk_alerts WHERE task_id = ? AND user_id = ? ORDER BY created_at, id",
                (task_id, user_id),
            ).fetchall()
        return [self._risk_alert_row(row) for row in rows]

    def alert_detail(self, user_id: str, alert_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM risk_alerts WHERE user_id = ? AND id = ?", (user_id, alert_id),
            ).fetchone()
            if row is None:
                return None
            history = connection.execute(
                "SELECT previous_status, status, source, actor_user_id, created_at FROM risk_alert_transitions "
                "WHERE alert_id = ? ORDER BY sequence",
                (alert_id,),
            ).fetchall()
            return {**self._risk_alert_row(row), "history": [dict(event) for event in history]}

    @staticmethod
    def _transition_risk_alert(
        connection: sqlite3.Connection, alert_id: str, previous: str, status: str,
        *, source: str, actor_user_id: str | None = None,
    ) -> None:
        now = int(time.time())
        connection.execute(
            "UPDATE risk_alerts SET status = ?, updated_at = ?, resolved_at = ? WHERE id = ?",
            (status, now, now if status == "resolved" else None, alert_id),
        )
        connection.execute(
            "INSERT INTO risk_alert_transitions "
            "(alert_id, previous_status, status, source, actor_user_id, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (alert_id, previous, status, source, actor_user_id, now),
        )

    def update_alert(self, user_id: str, alert_id: str, *, previous: str, status: str) -> bool:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT status FROM risk_alerts WHERE id = ? AND user_id = ?", (alert_id, user_id),
            ).fetchone()
            if row is None:
                return False
            current = row["status"]
            allowed = (
                current in {"open", "blocked", "approved"} and status == "acknowledged"
                or current in {"open", "blocked", "approved", "acknowledged"} and status in {"resolved", "ignored"}
            )
            if current != previous or not allowed:
                raise ConflictError("告警状态已变化或不允许此操作，请刷新")
            self._transition_risk_alert(connection, alert_id, current, status, source="owner", actor_user_id=user_id)
            return True

    def resolve_device_offline_alerts(self, user_id: str, agent_id: str) -> None:
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for row in connection.execute(
                """
                SELECT id, status FROM risk_alerts WHERE user_id = ? AND agent_id = ?
                  AND task_id IS NULL AND category = 'device_offline' AND status IN ('open', 'acknowledged')
                """,
                (user_id, agent_id),
            ).fetchall():
                self._transition_risk_alert(connection, row["id"], row["status"], "resolved", source="agent.reconnected")

    def create_runtime_session(
        self,
        *,
        session_id: str,
        agent_id: str,
        provider_id: str,
        runtime_version: str,
        adapter_version: str,
        mode: str,
        status: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO runtime_sessions
                    (id, agent_id, provider_id, runtime_version, adapter_version,
                     mode, status, metadata_json, started_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    session_id,
                    agent_id,
                    provider_id,
                    runtime_version,
                    adapter_version,
                    mode,
                    status,
                    json.dumps(metadata or {}, separators=(",", ":")),
                    now,
                    now,
                ),
            )
        return self.get_runtime_session(session_id)  # type: ignore[return-value]

    def get_runtime_session(self, session_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT * FROM runtime_sessions WHERE id = ?", (session_id,)
            ).fetchone()
        if row is None:
            return None
        result = dict(row)
        raw = result.pop("metadata_json", None)
        result["metadata"] = self._json_load(raw, {})
        return result

    def update_runtime_session(
        self,
        session_id: str,
        *,
        status: str | None = None,
        metadata: dict[str, Any] | None = None,
        ended_at: int | None = None,
    ) -> bool:
        updates = ["last_seen_at = ?"]
        values: list[Any] = [int(time.time())]
        if status is not None:
            updates.append("status = ?")
            values.append(status)
        if metadata is not None:
            updates.append("metadata_json = ?")
            values.append(json.dumps(metadata, separators=(",", ":")))
        if ended_at is not None:
            updates.append("ended_at = ?")
            values.append(ended_at)
        values.append(session_id)
        with self.connect() as connection:
            cursor = connection.execute(
                f"UPDATE runtime_sessions SET {', '.join(updates)} WHERE id = ?",
                values,
            )
        return cursor.rowcount > 0

    def create_agent_bundle(
        self,
        bundle_id: str,
        user_id: str,
        enrollment_token_id: str,
        name: str,
        description: str,
        runtime: str,
        profiles: list[str],
        filename: str,
        expires_at: int,
    ) -> dict[str, Any]:
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO agent_bundles
                    (id, user_id, enrollment_token_id, name, description, runtime,
                     profiles_json, filename, created_at, expires_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    bundle_id,
                    user_id,
                    enrollment_token_id,
                    name,
                    description,
                    runtime,
                    json.dumps(profiles, separators=(",", ":")),
                    filename,
                    now,
                    expires_at,
                ),
            )
        return {
            "id": bundle_id,
            "name": name,
            "description": description,
            "runtime": runtime,
            "profiles": profiles,
            "filename": filename,
            "created_at": now,
            "expires_at": expires_at,
        }

    @staticmethod
    def _bundle_row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["profiles"] = json.loads(result.pop("profiles_json"))
        return result

    def list_agent_bundles(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, description, runtime, profiles_json, filename,
                       created_at, expires_at
                FROM agent_bundles WHERE user_id = ? ORDER BY created_at DESC LIMIT 20
                """,
                (user_id,),
            ).fetchall()
        return [self._bundle_row(row) for row in rows if row is not None]

    def get_owned_agent_bundle(self, user_id: str, bundle_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, name, description, runtime, profiles_json, filename,
                       created_at, expires_at
                FROM agent_bundles WHERE id = ? AND user_id = ?
                """,
                (bundle_id, user_id),
            ).fetchone()
        return self._bundle_row(row)

    def cleanup_expired_agent_bundles(self) -> list[str]:
        now = int(time.time())
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT filename FROM agent_bundles WHERE expires_at <= ?", (now,)
            ).fetchall()
            connection.execute("DELETE FROM agent_bundles WHERE expires_at <= ?", (now,))
        return [row["filename"] for row in rows]
