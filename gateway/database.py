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
                    credential_prefix TEXT NOT NULL,
                    credential_hash TEXT NOT NULL UNIQUE,
                    created_at INTEGER NOT NULL,
                    last_seen_at INTEGER NOT NULL,
                    revoked_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS idx_agents_user ON agents(user_id);

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
        self, user_id: str, name: str, prefix: str, token_hash: str
    ) -> dict[str, Any]:
        key_id = str(uuid.uuid4())
        now = int(time.time())
        with self.connect() as connection:
            connection.execute(
                """
                INSERT INTO mcp_keys
                    (id, user_id, name, prefix, token_hash, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (key_id, user_id, name, prefix, token_hash, now),
            )
        return {"id": key_id, "name": name, "prefix": prefix, "created_at": now}

    def list_mcp_keys(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, name, prefix, created_at, last_used_at, revoked_at
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
                SELECT mcp_keys.id AS key_id, mcp_keys.user_id, users.email
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
        profiles: list[str],
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
                    (id, user_id, name, description, profiles_json, credential_prefix,
                     credential_hash, created_at, last_seen_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    agent_id,
                    enrollment["user_id"],
                    name,
                    description,
                    json.dumps(profiles, separators=(",", ":")),
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
                SELECT id, user_id, name, description, profiles_json, created_at, last_seen_at
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
        result["profiles"] = json.loads(result.pop("profiles_json"))
        return result

    def touch_agent(self, agent_id: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE agents SET last_seen_at = ? WHERE id = ?", (int(time.time()), agent_id)
            )

    def list_agents(self, user_id: str) -> list[dict[str, Any]]:
        with self.connect() as connection:
            rows = connection.execute(
                """
                SELECT id, user_id, name, description, profiles_json, created_at,
                       last_seen_at, revoked_at
                FROM agents
                WHERE user_id = ? AND revoked_at IS NULL
                ORDER BY created_at DESC
                """,
                (user_id,),
            ).fetchall()
        return [self._agent_row(row) for row in rows if row is not None]

    def get_owned_agent(self, user_id: str, agent_id: str) -> dict[str, Any] | None:
        with self.connect() as connection:
            row = connection.execute(
                """
                SELECT id, user_id, name, description, profiles_json, created_at,
                       last_seen_at, revoked_at
                FROM agents WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (agent_id, user_id),
            ).fetchone()
        return self._agent_row(row)

    def update_agent(self, user_id: str, agent_id: str, name: str, description: str) -> bool:
        with self.connect() as connection:
            cursor = connection.execute(
                """
                UPDATE agents SET name = ?, description = ?
                WHERE id = ? AND user_id = ? AND revoked_at IS NULL
                """,
                (name, description, agent_id, user_id),
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
