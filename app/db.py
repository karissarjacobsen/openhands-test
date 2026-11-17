"""SQLite persistence for agreements, recipients and status history."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS agreements (
    id              TEXT PRIMARY KEY,
    title           TEXT NOT NULL,
    client_name     TEXT NOT NULL DEFAULT '',
    agreement_type  TEXT NOT NULL DEFAULT 'Sales Agreement',
    value           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL DEFAULT 'draft',
    owner           TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    effective_date  TEXT NOT NULL DEFAULT '',
    envelope_id     TEXT,
    document_name   TEXT,
    document_path   TEXT,
    docusign_status TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS recipients (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  TEXT NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    email         TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'signer',
    routing_order INTEGER NOT NULL DEFAULT 1,
    status        TEXT NOT NULL DEFAULT 'created',
    signed_at     TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  TEXT NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    message       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_recipients_agreement ON recipients(agreement_id);
CREATE INDEX IF NOT EXISTS idx_events_agreement ON events(agreement_id);
"""

# Statuses we track locally. `draft` is pre-send; the rest mirror Docusign
# envelope statuses so the UI can display one consistent vocabulary.
STATUSES = [
    "draft",
    "sent",
    "delivered",
    "completed",
    "declined",
    "voided",
    "corrected",
]

TERMINAL_STATUSES = {"completed", "declined", "voided"}


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
                "(title LIKE ? OR client_name LIKE ? OR owner LIKE ? OR id LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like])
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
            agreement["recipients"] = self.list_recipients(agreement["id"])
        return agreements

    def get_agreement(self, agreement_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM agreements WHERE id = ?", (agreement_id,)
        ).fetchone()
        if row is None:
            return None
        agreement = dict(row)
        agreement["recipients"] = self.list_recipients(agreement_id)
        agreement["events"] = self.list_events(agreement_id)
        return agreement

    def get_agreement_by_envelope(self, envelope_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM agreements WHERE envelope_id = ?", (envelope_id,)
        ).fetchone()
        if row is None:
            return None
        agreement = dict(row)
        agreement["recipients"] = self.list_recipients(agreement["id"])
        agreement["events"] = self.list_events(agreement["id"])
        return agreement

    def list_recipients(self, agreement_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM recipients WHERE agreement_id = ? ORDER BY routing_order, id",
            (agreement_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_events(self, agreement_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM events WHERE agreement_id = ? ORDER BY id DESC",
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
            "SELECT DISTINCT agreement_type FROM agreements ORDER BY agreement_type"
        ).fetchall()
        return [r["agreement_type"] for r in rows]

    # ----------------------------------------------------------------- writes

    def create_agreement(
        self,
        *,
        title: str,
        client_name: str = "",
        agreement_type: str = "Sales Agreement",
        value: float = 0.0,
        currency: str = "USD",
        owner: str = "",
        notes: str = "",
        effective_date: str = "",
        status: str = "draft",
        recipients: list[dict[str, Any]] | None = None,
        document_name: str | None = None,
        document_path: str | None = None,
        envelope_id: str | None = None,
        docusign_status: str | None = None,
        completed_at: str | None = None,
        agreement_id: str | None = None,
    ) -> dict[str, Any]:
        agreement_id = agreement_id or str(uuid.uuid4())
        now = utcnow()
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO agreements (
                id, title, client_name, agreement_type, value, currency, status,
                owner, notes, effective_date, envelope_id, document_name,
                document_path, docusign_status, completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agreement_id,
                title,
                client_name,
                agreement_type,
                value,
                currency,
                status,
                owner,
                notes,
                effective_date,
                envelope_id,
                document_name,
                document_path,
                docusign_status,
                completed_at,
                now,
                now,
            ),
        )
        conn.commit()
        for index, recipient in enumerate(recipients or [], start=1):
            self.add_recipient(
                agreement_id,
                name=recipient.get("name", ""),
                email=recipient.get("email", ""),
                role=recipient.get("role", "signer"),
                routing_order=int(recipient.get("routing_order", index)),
                status=recipient.get("status", "created"),
            )
        self.add_event(agreement_id, "created", f"Agreement '{title}' created")
        return self.get_agreement(agreement_id)  # type: ignore[return-value]

    def update_agreement(self, agreement_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "title",
            "client_name",
            "agreement_type",
            "value",
            "currency",
            "status",
            "owner",
            "notes",
            "effective_date",
            "envelope_id",
            "document_name",
            "document_path",
            "docusign_status",
            "completed_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_agreement(agreement_id)
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [agreement_id]
        conn = self._conn()
        conn.execute(
            f"UPDATE agreements SET {assignments} WHERE id = ?", params
        )
        conn.commit()
        return self.get_agreement(agreement_id)

    def delete_agreement(self, agreement_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM agreements WHERE id = ?", (agreement_id,))
        conn.commit()

    def add_recipient(
        self,
        agreement_id: str,
        *,
        name: str,
        email: str,
        role: str = "signer",
        routing_order: int = 1,
        status: str = "created",
    ) -> None:
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO recipients
                (agreement_id, name, email, role, routing_order, status)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (agreement_id, name, email, role, routing_order, status),
        )
        conn.commit()

    def replace_recipients(
        self, agreement_id: str, recipients: list[dict[str, Any]]
    ) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM recipients WHERE agreement_id = ?", (agreement_id,))
        conn.commit()
        for index, recipient in enumerate(recipients, start=1):
            self.add_recipient(
                agreement_id,
                name=recipient.get("name", ""),
                email=recipient.get("email", ""),
                role=recipient.get("role", "signer"),
                routing_order=int(recipient.get("routing_order", index)),
            )

    def set_recipient_status(
        self,
        agreement_id: str,
        email: str,
        status: str,
        signed_at: str | None = None,
    ) -> None:
        conn = self._conn()
        conn.execute(
            """
            UPDATE recipients SET status = ?, signed_at = COALESCE(?, signed_at)
            WHERE agreement_id = ? AND lower(email) = lower(?)
            """,
            (status, signed_at, agreement_id, email),
        )
        conn.commit()

    def upsert_recipient(
        self,
        agreement_id: str,
        *,
        name: str,
        email: str,
        role: str = "signer",
        routing_order: int = 1,
        status: str = "created",
        signed_at: str | None = None,
    ) -> None:
        """Insert a recipient, or update the existing row for that email."""
        conn = self._conn()
        row = conn.execute(
            "SELECT id FROM recipients WHERE agreement_id = ? AND lower(email) = lower(?)",
            (agreement_id, email),
        ).fetchone()
        if row is None:
            conn.execute(
                """
                INSERT INTO recipients
                    (agreement_id, name, email, role, routing_order, status, signed_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (agreement_id, name, email, role, routing_order, status, signed_at),
            )
        else:
            conn.execute(
                """
                UPDATE recipients
                SET name = ?, role = ?, routing_order = ?, status = ?,
                    signed_at = COALESCE(?, signed_at)
                WHERE id = ?
                """,
                (name, role, routing_order, status, signed_at, row["id"]),
            )
        conn.commit()

    def add_event(self, agreement_id: str, kind: str, message: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO events (agreement_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
            (agreement_id, kind, message, utcnow()),
        )
        conn.commit()


def to_json(value: Any) -> str:
    return json.dumps(value, default=str)
