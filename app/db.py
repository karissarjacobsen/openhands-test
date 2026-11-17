"""SQLite persistence: a local cache of agreements read from Agreement Manager.

The app does not own agreement data. Every row here is a copy of an agreement
held in the Docusign Agreement Manager repository, refreshed on each sync so the
dashboard can render without an API round-trip per page load.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS agreements (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL DEFAULT '',
    file_name       TEXT NOT NULL DEFAULT '',
    agreement_type  TEXT NOT NULL DEFAULT '',
    category        TEXT NOT NULL DEFAULT '',
    status          TEXT NOT NULL DEFAULT 'pending',
    value           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT '',
    effective_date  TEXT NOT NULL DEFAULT '',
    expiration_date TEXT NOT NULL DEFAULT '',
    source_name     TEXT NOT NULL DEFAULT '',
    source_id       TEXT NOT NULL DEFAULT '',
    download_url    TEXT NOT NULL DEFAULT '',
    provisions      TEXT NOT NULL DEFAULT '{}',
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  TEXT NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    party_id      TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL DEFAULT '',
    email         TEXT NOT NULL DEFAULT '',
    role          TEXT NOT NULL DEFAULT 'party'
);

CREATE INDEX IF NOT EXISTS idx_parties_agreement ON parties(agreement_id);
"""

# Statuses reported by Agreement Manager. Lower-cased so the UI has one
# consistent vocabulary regardless of how the API capitalises them.
STATUSES = ["pending", "complete", "inactive"]


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    """Thread-safe SQLite wrapper (one connection per thread)."""

    def __init__(self, path: Path):
        self.path = path
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA foreign_keys = ON")
            self._local.conn = conn
        return conn

    def _init_schema(self) -> None:
        conn = self._conn()
        conn.executescript(SCHEMA)
        conn.commit()

    # ------------------------------------------------------------------ reads

    def list_agreements(
        self,
        search: str | None = None,
        status: str | None = None,
        agreement_type: str | None = None,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM agreements"
        clauses: list[str] = []
        params: list[Any] = []
        if search:
            clauses.append(
                "(title LIKE ? OR file_name LIKE ? OR agreement_type LIKE ? OR "
                "category LIKE ? OR source_id LIKE ? OR id LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like, like, like])
        if status:
            clauses.append("status = ?")
            params.append(status)
        if agreement_type:
            clauses.append("agreement_type = ?")
            params.append(agreement_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY datetime(updated_at) DESC"
        rows = self._conn().execute(sql, params).fetchall()
        agreements = [dict(r) for r in rows]
        for agreement in agreements:
            agreement["provisions"] = _load_json(agreement.get("provisions"))
        return agreements

    def get_agreement(self, agreement_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM agreements WHERE id = ?", (agreement_id,)
        ).fetchone()
        if row is None:
            return None
        agreement = dict(row)
        agreement["provisions"] = _load_json(agreement.get("provisions"))
        agreement["parties"] = self.list_parties(agreement_id)
        return agreement

    def list_parties(self, agreement_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM parties WHERE agreement_id = ? ORDER BY id",
            (agreement_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def status_counts(self) -> dict[str, int]:
        rows = self._conn().execute(
            "SELECT status, COUNT(*) AS n FROM agreements GROUP BY status"
        ).fetchall()
        counts = {status: 0 for status in STATUSES}
        for row in rows:
            counts[row["status"]] = row["n"]
        return counts

    def distinct_types(self) -> list[str]:
        rows = self._conn().execute(
            "SELECT DISTINCT agreement_type FROM agreements "
            "WHERE agreement_type <> '' ORDER BY agreement_type"
        ).fetchall()
        return [r["agreement_type"] for r in rows]

    # ----------------------------------------------------------------- writes

    def upsert_agreement(
        self,
        fields: dict[str, Any],
        parties: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Insert or update an agreement cached from Agreement Manager.

        When `parties` is None the cached parties are left untouched, so a
        summary sync (which does not return parties) does not wipe them.
        """
        agreement_id = fields["id"]
        existing = self._conn().execute(
            "SELECT created_at FROM agreements WHERE id = ?", (agreement_id,)
        ).fetchone()
        now = utcnow()
        created_at = existing["created_at"] if existing else (fields.get("created_at") or now)
        columns = {
            "id": agreement_id,
            "title": fields.get("title", ""),
            "file_name": fields.get("file_name", ""),
            "agreement_type": fields.get("agreement_type", ""),
            "category": fields.get("category", ""),
            "status": fields.get("status", "pending"),
            "value": float(fields.get("value") or 0),
            "currency": fields.get("currency", ""),
            "effective_date": fields.get("effective_date", ""),
            "expiration_date": fields.get("expiration_date", ""),
            "source_name": fields.get("source_name", ""),
            "source_id": fields.get("source_id", ""),
            "download_url": fields.get("download_url", ""),
            "provisions": json.dumps(fields.get("provisions") or {}, default=str),
            "created_at": created_at,
            "updated_at": now,
        }
        names = ", ".join(columns)
        placeholders = ", ".join("?" for _ in columns)
        conn = self._conn()
        conn.execute(
            f"INSERT OR REPLACE INTO agreements ({names}) VALUES ({placeholders})",
            list(columns.values()),
        )
        conn.commit()
        if parties is not None:
            self.replace_parties(agreement_id, parties)
        return self.get_agreement(agreement_id)  # type: ignore[return-value]

    def replace_parties(
        self, agreement_id: str, parties: list[dict[str, Any]]
    ) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM parties WHERE agreement_id = ?", (agreement_id,))
        for party in parties:
            conn.execute(
                """
                INSERT INTO parties (agreement_id, party_id, name, email, role)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    agreement_id,
                    party.get("party_id", ""),
                    party.get("name", ""),
                    party.get("email", ""),
                    party.get("role", "party"),
                ),
            )
        conn.commit()

    def delete_agreement(self, agreement_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM agreements WHERE id = ?", (agreement_id,))
        conn.commit()


def _load_json(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if not value:
        return {}
    try:
        loaded = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return loaded if isinstance(loaded, dict) else {}
