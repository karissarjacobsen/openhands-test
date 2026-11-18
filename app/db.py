"""SQLite persistence for agreements, parties, bulk jobs and status history.

Agreement Manager is the system of record for agreement content. This local
database keeps a cache of agreements the user has imported from (or uploaded
to) Agreement Manager, plus the bulk upload jobs they started.
"""

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
    agm_id          TEXT,
    title           TEXT NOT NULL,
    client_name     TEXT NOT NULL DEFAULT '',
    agreement_type  TEXT NOT NULL DEFAULT '',
    value           REAL NOT NULL DEFAULT 0,
    currency        TEXT NOT NULL DEFAULT 'USD',
    status          TEXT NOT NULL DEFAULT 'draft',
    owner           TEXT NOT NULL DEFAULT '',
    notes           TEXT NOT NULL DEFAULT '',
    effective_date  TEXT NOT NULL DEFAULT '',
    summary         TEXT NOT NULL DEFAULT '',
    parties_json    TEXT NOT NULL DEFAULT '[]',
    provisions_json TEXT NOT NULL DEFAULT '{}',
    document_name   TEXT,
    document_path   TEXT,
    agm_status      TEXT,
    completed_at    TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS parties (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  TEXT NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'party',
    routing_order INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS bulk_jobs (
    id            TEXT PRIMARY KEY,
    job_id        TEXT,
    name          TEXT NOT NULL DEFAULT '',
    language      TEXT NOT NULL DEFAULT 'en-US',
    status        TEXT NOT NULL DEFAULT 'OPEN',
    expected_docs INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    agreement_id  TEXT NOT NULL REFERENCES agreements(id) ON DELETE CASCADE,
    kind          TEXT NOT NULL,
    message       TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_parties_agreement ON parties(agreement_id);
CREATE INDEX IF NOT EXISTS idx_events_agreement ON events(agreement_id);
CREATE INDEX IF NOT EXISTS idx_agreements_agm ON agreements(agm_id);
"""

# Local status vocabulary. Mirrors the Agreement Manager agreement statuses
# (`PENDING`, `COMPLETE`, `INACTIVE`) plus a local-only `draft`.
STATUSES = [
    "draft",
    "pending",
    "complete",
    "inactive",
]

TERMINAL_STATUSES = {"complete", "inactive"}


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
                "(title LIKE ? OR client_name LIKE ? OR owner LIKE ? OR id LIKE ? "
                "OR agm_id LIKE ?)"
            )
            like = f"%{search}%"
            params.extend([like, like, like, like, like])
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
        agreements = [self._hydrate(dict(r)) for r in rows]
        for agreement in agreements:
            agreement["parties"] = self.list_parties(agreement["id"])
        return agreements

    def get_agreement(self, agreement_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM agreements WHERE id = ?", (agreement_id,)
        ).fetchone()
        if row is None:
            return None
        agreement = self._hydrate(dict(row))
        agreement["parties"] = self.list_parties(agreement_id)
        agreement["events"] = self.list_events(agreement_id)
        return agreement

    def get_agreement_by_agm_id(self, agm_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM agreements WHERE agm_id = ?", (agm_id,)
        ).fetchone()
        if row is None:
            return None
        return self.get_agreement(row["id"])

    @staticmethod
    def _hydrate(agreement: dict[str, Any]) -> dict[str, Any]:
        agreement["provisions"] = _loads(agreement.get("provisions_json"), {})
        return agreement

    def list_parties(self, agreement_id: str) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM parties WHERE agreement_id = ? ORDER BY routing_order, id",
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
            "SELECT DISTINCT agreement_type FROM agreements "
            "WHERE agreement_type <> '' ORDER BY agreement_type"
        ).fetchall()
        return [r["agreement_type"] for r in rows]

    # -------------------------------------------------------------- bulk jobs

    def create_bulk_job(
        self,
        *,
        name: str,
        language: str = "en-US",
        expected_docs: int = 0,
        job_id: str | None = None,
        status: str = "OPEN",
    ) -> dict[str, Any]:
        local_id = str(uuid.uuid4())
        now = utcnow()
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO bulk_jobs (id, job_id, name, language, status,
                                   expected_docs, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (local_id, job_id, name, language, status, expected_docs, now, now),
        )
        conn.commit()
        return self.get_bulk_job(local_id)  # type: ignore[return-value]

    def get_bulk_job(self, local_id: str) -> dict[str, Any] | None:
        row = self._conn().execute(
            "SELECT * FROM bulk_jobs WHERE id = ?", (local_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_bulk_jobs(self) -> list[dict[str, Any]]:
        rows = self._conn().execute(
            "SELECT * FROM bulk_jobs ORDER BY datetime(created_at) DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def update_bulk_job(self, local_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {"job_id", "name", "language", "status", "expected_docs"}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_bulk_job(local_id)
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [local_id]
        conn = self._conn()
        conn.execute(f"UPDATE bulk_jobs SET {assignments} WHERE id = ?", params)
        conn.commit()
        return self.get_bulk_job(local_id)

    # ----------------------------------------------------------------- writes

    def create_agreement(
        self,
        *,
        title: str,
        client_name: str = "",
        agreement_type: str = "",
        value: float = 0.0,
        currency: str = "USD",
        owner: str = "",
        notes: str = "",
        effective_date: str = "",
        status: str = "draft",
        agm_id: str | None = None,
        summary: str = "",
        parties: list[dict[str, Any]] | None = None,
        provisions: dict[str, Any] | None = None,
        document_name: str | None = None,
        document_path: str | None = None,
        agm_status: str | None = None,
        completed_at: str | None = None,
        agreement_id: str | None = None,
    ) -> dict[str, Any]:
        agreement_id = agreement_id or str(uuid.uuid4())
        now = utcnow()
        conn = self._conn()
        conn.execute(
            """
            INSERT INTO agreements (
                id, agm_id, title, client_name, agreement_type, value, currency,
                status, owner, notes, effective_date, summary, parties_json,
                provisions_json, document_name, document_path, agm_status,
                completed_at, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agreement_id,
                agm_id,
                title,
                client_name,
                agreement_type,
                value,
                currency,
                status,
                owner,
                notes,
                effective_date,
                summary,
                json.dumps(parties or [], default=str),
                json.dumps(provisions or {}, default=str),
                document_name,
                document_path,
                agm_status,
                completed_at,
                now,
                now,
            ),
        )
        conn.commit()
        for index, party in enumerate(parties or [], start=1):
            self.add_party(
                agreement_id,
                name=party.get("name") or party.get("name_in_agreement", ""),
                role=party.get("role", "party"),
                routing_order=int(party.get("routing_order", index)),
            )
        self.add_event(agreement_id, "created", f"Agreement '{title}' created")
        return self.get_agreement(agreement_id)  # type: ignore[return-value]

    def update_agreement(self, agreement_id: str, **fields: Any) -> dict[str, Any] | None:
        allowed = {
            "agm_id",
            "title",
            "client_name",
            "agreement_type",
            "value",
            "currency",
            "status",
            "owner",
            "notes",
            "effective_date",
            "summary",
            "parties_json",
            "provisions_json",
            "document_name",
            "document_path",
            "agm_status",
            "completed_at",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return self.get_agreement(agreement_id)
        updates["updated_at"] = utcnow()
        assignments = ", ".join(f"{key} = ?" for key in updates)
        params = list(updates.values()) + [agreement_id]
        conn = self._conn()
        conn.execute(f"UPDATE agreements SET {assignments} WHERE id = ?", params)
        conn.commit()
        return self.get_agreement(agreement_id)

    def delete_agreement(self, agreement_id: str) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM agreements WHERE id = ?", (agreement_id,))
        conn.commit()

    def add_party(
        self,
        agreement_id: str,
        *,
        name: str,
        role: str = "party",
        routing_order: int = 1,
    ) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO parties (agreement_id, name, role, routing_order) "
            "VALUES (?, ?, ?, ?)",
            (agreement_id, name, role, routing_order),
        )
        conn.commit()

    def replace_parties(
        self, agreement_id: str, parties: list[dict[str, Any]]
    ) -> None:
        conn = self._conn()
        conn.execute("DELETE FROM parties WHERE agreement_id = ?", (agreement_id,))
        conn.commit()
        for index, party in enumerate(parties, start=1):
            self.add_party(
                agreement_id,
                name=party.get("name") or party.get("name_in_agreement", ""),
                role=party.get("role", "party"),
                routing_order=int(party.get("routing_order", index)),
            )

    def add_event(self, agreement_id: str, kind: str, message: str) -> None:
        conn = self._conn()
        conn.execute(
            "INSERT INTO events (agreement_id, kind, message, created_at) VALUES (?, ?, ?, ?)",
            (agreement_id, kind, message, utcnow()),
        )
        conn.commit()


def _loads(value: Any, default: Any) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return default


def seed_sample_data(db: Database) -> None:
    """Populate a few realistic agreements so the dashboard is never empty."""
    if db.list_agreements():
        return

    samples = [
        {
            "agm_id": "a8c893b9-1f2e-4a3b-9c1d-333d80e4728f",
            "title": "Acme Corp - Master Services Agreement",
            "client_name": "Acme Corp",
            "agreement_type": "Master Service Agreement",
            "value": 125000.0,
            "owner": "karissa@example.com",
            "status": "complete",
            "agm_status": "COMPLETE",
            "effective_date": "2026-01-15",
            "notes": "Annual renewal. Signed by both parties.",
            "summary": "Umbrella services agreement between Acme Corp and the provider.",
            "completed_at": "2026-01-22T14:05:00+00:00",
            "provisions": {
                "effective_date": "2026-01-15T00:00:00Z",
                "total_agreement_value": 125000,
                "total_agreement_value_currency_code": "USD",
                "renewal_type": "AUTO_RENEW",
                "governing_law": "California",
            },
            "parties": [
                {"name": "ACME, INC", "role": "party"},
                {"name": "Karissa Jacobsen", "role": "party"},
            ],
        },
        {
            "agm_id": "b6b55050-2a1b-4c3d-8e2f-ae05bb7f803e",
            "title": "Northwind Traders - Sales Agreement",
            "client_name": "Northwind Traders",
            "agreement_type": "Sales Agreement",
            "value": 48900.0,
            "owner": "karissa@example.com",
            "status": "pending",
            "agm_status": "PENDING",
            "effective_date": "2026-02-01",
            "notes": "Awaiting AI extraction review.",
            "summary": "Sale of goods with net-30 payment terms.",
            "provisions": {
                "effective_date": "2026-02-01T00:00:00Z",
                "payment_terms_due_date": "THIRTY_DAYS",
                "total_agreement_value": 48900,
            },
            "parties": [{"name": "NORTHWIND TRADERS", "role": "party"}],
        },
        {
            "agm_id": "c1d2e3f4-3b2c-4d4e-9f30-bf16cc8f914f",
            "title": "Globex - Mutual NDA",
            "client_name": "Globex",
            "agreement_type": "Non-disclosure Agreement",
            "value": 0.0,
            "owner": "legal@example.com",
            "status": "pending",
            "agm_status": "PENDING",
            "effective_date": "2026-02-10",
            "notes": "Draft pending legal review of section 4.",
            "summary": "Two-way confidentiality agreement for evaluating a partnership.",
            "provisions": {"effective_date": "2026-02-10T00:00:00Z"},
            "parties": [{"name": "GLOBEX", "role": "party"}],
        },
        {
            "agm_id": "d4e5f6a7-4c3d-4e5f-a041-cf27dd9fa25e",
            "title": "Initech - Statement of Work",
            "client_name": "Initech",
            "agreement_type": "Statement of Work",
            "value": 18750.0,
            "owner": "delivery@example.com",
            "status": "inactive",
            "agm_status": "INACTIVE",
            "effective_date": "2026-01-05",
            "notes": "Superseded by a newer statement of work.",
            "summary": "Project-based statement of work with milestones and fees.",
            "provisions": {"total_agreement_value": 18750, "governing_law": "Texas"},
            "parties": [{"name": "INITECH", "role": "party"}],
        },
        {
            "agm_id": "e5f6a7b8-5d4e-4f60-b152-d038eea0b36f",
            "title": "Umbrella Health - Vendor Agreement",
            "client_name": "Umbrella Health",
            "agreement_type": "Vendor Agreement",
            "value": 73200.0,
            "owner": "karissa@example.com",
            "status": "complete",
            "agm_status": "COMPLETE",
            "effective_date": "2026-02-05",
            "notes": "Compliance addendum included.",
            "summary": "Vendor services agreement with a compliance addendum.",
            "provisions": {"effective_date": "2026-02-05T00:00:00Z", "governing_law": "Ohio"},
            "parties": [
                {"name": "UMBRELLA HEALTH", "role": "party"},
                {"name": "Rita Chen", "role": "party"},
            ],
        },
    ]

    for sample in samples:
        parties = sample.pop("parties")
        agreement = db.create_agreement(parties=parties, **sample)
        db.add_event(
            agreement["id"],
            "seeded",
            "Sample agreement loaded for demonstration",
        )
