"""SQLite persistence layer + historical snapshot management."""

from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Generator

from src.data_fetcher import Holding, PortfolioSnapshot


# ── JSON encoder for Decimal ──────────────────────────────────────────────────

class _DecimalEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Decimal):
            return str(obj)
        if isinstance(obj, datetime):
            return obj.isoformat()
        return super().default(obj)


# ── Schema ────────────────────────────────────────────────────────────────────

_DDL = """
CREATE TABLE IF NOT EXISTS snapshots (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    fetched_at  TEXT    NOT NULL,
    total_value TEXT    NOT NULL,
    payload     TEXT    NOT NULL    -- full JSON blob
);

CREATE TABLE IF NOT EXISTS holdings_history (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id),
    fetched_at      TEXT    NOT NULL,
    account_id      TEXT    NOT NULL,
    security_id     TEXT    NOT NULL,
    ticker          TEXT    NOT NULL,
    name            TEXT    NOT NULL,
    quantity        TEXT    NOT NULL,
    institution_value TEXT  NOT NULL,
    cost_basis      TEXT,
    unrealised_gain TEXT
);

CREATE TABLE IF NOT EXISTS alerts_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL,
    alert_type  TEXT    NOT NULL,
    ticker      TEXT,
    message     TEXT    NOT NULL,
    delivered   INTEGER NOT NULL DEFAULT 0   -- bitmask: 1=slack 2=email
);

CREATE INDEX IF NOT EXISTS idx_snapshots_fetched ON snapshots(fetched_at);
CREATE INDEX IF NOT EXISTS idx_holdings_ticker   ON holdings_history(ticker, fetched_at);
"""


# ── DataStore ─────────────────────────────────────────────────────────────────

class DataStore:
    def __init__(self, db_path: Path, snapshots_dir: Path) -> None:
        self.db_path = db_path
        self.snapshots_dir = snapshots_dir
        db_path.parent.mkdir(parents=True, exist_ok=True)
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        self._init_db()

    # ── Connection ─────────────────────────────────────────────────────────────

    @contextmanager
    def _conn(self) -> Generator[sqlite3.Connection, None, None]:
        conn = sqlite3.connect(str(self.db_path))
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(_DDL)

    # ── Snapshots ──────────────────────────────────────────────────────────────

    def save_snapshot(self, snapshot: PortfolioSnapshot) -> int:
        """Persist a snapshot to DB and write a JSON file. Returns the snapshot id."""
        payload = self._snapshot_to_dict(snapshot)
        payload_json = json.dumps(payload, cls=_DecimalEncoder)

        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO snapshots (fetched_at, total_value, payload) VALUES (?, ?, ?)",
                (snapshot.fetched_at.isoformat(), str(snapshot.total_value), payload_json),
            )
            snapshot_id = cursor.lastrowid

            # Insert rows for easy time-series queries
            rows = [
                (
                    snapshot_id,
                    snapshot.fetched_at.isoformat(),
                    h.account_id,
                    h.security_id,
                    h.ticker,
                    h.name,
                    str(h.quantity),
                    str(h.institution_value),
                    str(h.cost_basis) if h.cost_basis is not None else None,
                    str(h.unrealised_gain) if h.unrealised_gain is not None else None,
                )
                for h in snapshot.holdings
            ]
            conn.executemany(
                """INSERT INTO holdings_history
                   (snapshot_id, fetched_at, account_id, security_id, ticker, name,
                    quantity, institution_value, cost_basis, unrealised_gain)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                rows,
            )

        # Write JSON snapshot file
        ts = snapshot.fetched_at.strftime("%Y%m%d_%H%M%S")
        snap_file = self.snapshots_dir / f"snapshot_{ts}.json"
        snap_file.write_text(json.dumps(payload, cls=_DecimalEncoder, indent=2))

        return snapshot_id

    def get_latest_snapshot_payload(self) -> dict | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM snapshots ORDER BY fetched_at DESC LIMIT 1"
            ).fetchone()
        if row:
            return json.loads(row["payload"])
        return None

    def get_snapshot_payloads(self, limit: int = 10) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT payload FROM snapshots ORDER BY fetched_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [json.loads(r["payload"]) for r in rows]

    # ── Holdings History ───────────────────────────────────────────────────────

    def get_ticker_history(self, ticker: str, limit: int = 30) -> list[dict]:
        """Return time-series rows for a specific ticker."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT fetched_at, quantity, institution_value, cost_basis, unrealised_gain
                   FROM holdings_history
                   WHERE ticker = ?
                   ORDER BY fetched_at DESC
                   LIMIT ?""",
                (ticker.upper(), limit),
            ).fetchall()
        return [dict(r) for r in rows]

    def get_previous_holdings(self, snapshot_limit: int = 2) -> list[dict]:
        """Return holdings from the second-most-recent snapshot for drift comparison."""
        with self._conn() as conn:
            rows = conn.execute(
                """SELECT h.*
                   FROM holdings_history h
                   JOIN (
                       SELECT id FROM snapshots
                       ORDER BY fetched_at DESC
                       LIMIT ? OFFSET 1
                   ) s ON h.snapshot_id = s.id""",
                (snapshot_limit,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Alerts Log ─────────────────────────────────────────────────────────────

    def log_alert(
        self,
        alert_type: str,
        message: str,
        ticker: str | None = None,
        delivered: int = 0,
    ) -> int:
        with self._conn() as conn:
            cursor = conn.execute(
                "INSERT INTO alerts_log (created_at, alert_type, ticker, message, delivered) "
                "VALUES (?, ?, ?, ?, ?)",
                (datetime.utcnow().isoformat(), alert_type, ticker, message, delivered),
            )
            return cursor.lastrowid

    def get_recent_alerts(self, limit: int = 20) -> list[dict]:
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT * FROM alerts_log ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return [dict(r) for r in rows]

    # ── Helpers ────────────────────────────────────────────────────────────────

    @staticmethod
    def _snapshot_to_dict(snapshot: PortfolioSnapshot) -> dict:
        return {
            "fetched_at": snapshot.fetched_at.isoformat(),
            "total_value": str(snapshot.total_value),
            "accounts": [
                {
                    "account_id": a.account_id,
                    "name": a.name,
                    "type": a.account_type,
                    "subtype": a.subtype,
                    "balances_current": str(a.balances_current) if a.balances_current else None,
                }
                for a in snapshot.accounts
            ],
            "holdings": [
                {
                    "account_id": h.account_id,
                    "ticker": h.ticker,
                    "name": h.name,
                    "quantity": str(h.quantity),
                    "institution_price": str(h.institution_price),
                    "institution_value": str(h.institution_value),
                    "cost_basis": str(h.cost_basis) if h.cost_basis is not None else None,
                    "unrealised_gain": (
                        str(h.unrealised_gain) if h.unrealised_gain is not None else None
                    ),
                    "unrealised_gain_pct": (
                        str(round(h.unrealised_gain_pct * 100, 2))
                        if h.unrealised_gain_pct is not None
                        else None
                    ),
                }
                for h in snapshot.holdings
            ],
            "transactions": [
                {
                    "id": t.investment_transaction_id,
                    "account_id": t.account_id,
                    "ticker": t.ticker,
                    "name": t.name,
                    "date": t.date.isoformat(),
                    "type": t.transaction_type,
                    "subtype": t.subtype,
                    "quantity": str(t.quantity),
                    "amount": str(t.amount),
                    "price": str(t.price),
                    "fees": str(t.fees) if t.fees is not None else None,
                }
                for t in snapshot.transactions
            ],
        }
