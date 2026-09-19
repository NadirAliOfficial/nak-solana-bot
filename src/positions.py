import sqlite3
import threading
import time
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS positions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    token_mint TEXT NOT NULL,
    token_symbol TEXT NOT NULL,
    entry_price REAL NOT NULL,
    quantity REAL NOT NULL,
    usd_size REAL NOT NULL,
    entry_time INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    exit_price REAL,
    exit_time INTEGER,
    exit_reason TEXT,
    pnl_usd REAL,
    pnl_pct REAL,
    trigger_order_id TEXT,
    exit_style TEXT NOT NULL DEFAULT 'oco',
    peak_price REAL,
    take_profit_pct REAL
)
"""

MIGRATIONS = [
    "ALTER TABLE positions ADD COLUMN trigger_order_id TEXT",
    "ALTER TABLE positions ADD COLUMN exit_style TEXT NOT NULL DEFAULT 'oco'",
    "ALTER TABLE positions ADD COLUMN peak_price REAL",
    "ALTER TABLE positions ADD COLUMN take_profit_pct REAL",
]


class PositionStore:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._lock = threading.Lock()
        conn = self._connect()
        conn.execute(SCHEMA)
        for migration in MIGRATIONS:
            try:
                conn.execute(migration)
            except sqlite3.OperationalError:
                pass  # column already exists from a prior run
        conn.commit()
        conn.close()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=10000")
        return conn

    def has_open_position(self, token_mint: str) -> bool:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT 1 FROM positions WHERE token_mint = ? AND status = 'open'", (token_mint,)
            ).fetchone()
            conn.close()
            return row is not None

    def has_recent_position(self, token_mint: str, cooldown_seconds: int) -> bool:
        with self._lock:
            conn = self._connect()
            cutoff = int(time.time()) - cooldown_seconds
            row = conn.execute(
                "SELECT 1 FROM positions WHERE token_mint = ? AND (status = 'open' OR exit_time >= ?)",
                (token_mint, cutoff),
            ).fetchone()
            conn.close()
            return row is not None

    def open_position(
        self,
        token_mint: str,
        token_symbol: str,
        entry_price: float,
        quantity: float,
        usd_size: float,
        exit_style: str = "oco",
        take_profit_pct: Optional[float] = None,
    ) -> int:
        """take_profit_pct overrides config.take_profit_pct for this specific leg - used
        by the tiered take-profit system where each leg targets a different %."""
        peak_price = entry_price if exit_style == "trailing" else None
        with self._lock:
            conn = self._connect()
            cur = conn.execute(
                "INSERT INTO positions (token_mint, token_symbol, entry_price, quantity, usd_size, entry_time, "
                "status, exit_style, peak_price, take_profit_pct) VALUES (?, ?, ?, ?, ?, ?, 'open', ?, ?, ?)",
                (
                    token_mint,
                    token_symbol,
                    entry_price,
                    quantity,
                    usd_size,
                    int(time.time()),
                    exit_style,
                    peak_price,
                    take_profit_pct,
                ),
            )
            conn.commit()
            position_id = cur.lastrowid
            conn.close()
            return position_id

    def update_peak_price(self, position_id: int, peak_price: float) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute("UPDATE positions SET peak_price = ? WHERE id = ?", (peak_price, position_id))
            conn.commit()
            conn.close()

    def set_trigger_order_id(self, position_id: int, trigger_order_id: str) -> None:
        with self._lock:
            conn = self._connect()
            conn.execute(
                "UPDATE positions SET trigger_order_id = ? WHERE id = ?", (trigger_order_id, position_id)
            )
            conn.commit()
            conn.close()

    def close_position(self, position_id: int, exit_price: float, exit_reason: str) -> None:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT entry_price, quantity FROM positions WHERE id = ?", (position_id,)).fetchone()
            entry_price = row["entry_price"]
            quantity = row["quantity"]
            pnl_usd = (exit_price - entry_price) * quantity
            pnl_pct = ((exit_price - entry_price) / entry_price) * 100
            conn.execute(
                "UPDATE positions SET status = 'closed', exit_price = ?, exit_time = ?, "
                "exit_reason = ?, pnl_usd = ?, pnl_pct = ? WHERE id = ?",
                (exit_price, int(time.time()), exit_reason, pnl_usd, pnl_pct, position_id),
            )
            conn.commit()
            conn.close()

    def get_open_positions(self) -> List[sqlite3.Row]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'open' ORDER BY entry_time DESC, id ASC"
            ).fetchall()
            conn.close()
            return rows

    def get_closed_positions(self, limit: int = 100) -> List[sqlite3.Row]:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT * FROM positions WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?", (limit,)
            ).fetchall()
            conn.close()
            return rows

    def get_position(self, position_id: int) -> Optional[sqlite3.Row]:
        with self._lock:
            conn = self._connect()
            row = conn.execute("SELECT * FROM positions WHERE id = ?", (position_id,)).fetchone()
            conn.close()
            return row

    def get_closed_stats(self) -> dict:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) as total_count, "
                "COALESCE(SUM(pnl_usd), 0.0) as total_pnl, "
                "COALESCE(SUM(CASE WHEN pnl_usd > 0 THEN 1 ELSE 0 END), 0) as wins "
                "FROM positions WHERE status = 'closed'"
            ).fetchone()
            conn.close()
            return {
                "total_count": row["total_count"],
                "total_pnl": row["total_pnl"],
                "wins": row["wins"],
            }

    def get_todays_stats(self, start_of_day_ts: float) -> dict:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT COUNT(*) as today_count, "
                "COALESCE(SUM(pnl_usd), 0.0) as today_pnl "
                "FROM positions WHERE status = 'closed' AND exit_time >= ?",
                (int(start_of_day_ts),),
            ).fetchone()
            conn.close()
            return {
                "today_count": row["today_count"],
                "today_pnl": row["today_pnl"],
            }

    def get_consecutive_losses(self) -> int:
        """Counts losing closes (by realized P&L, not exit_reason label) backward from
        the most recent closed position, stopping at the first non-losing exit. Used
        for the losing-streak cooldown.

        Counts by P&L rather than exit_reason == 'stop_loss' specifically: with partial
        exit enabled, a losing round-trip's trailing leg always closes with exit_reason
        'trailing_stop', never 'stop_loss' - counting by label alone would mean the
        streak counter resets to 0 on every trailing leg regardless of how many losing
        trades happened in a row."""
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT pnl_usd FROM positions WHERE status = 'closed' ORDER BY exit_time DESC, id DESC LIMIT 20"
            ).fetchall()
            conn.close()
        count = 0
        for row in rows:
            if (row["pnl_usd"] or 0) < 0:
                count += 1
            else:
                break
        return count

    def get_last_loss_exit_time(self) -> Optional[int]:
        with self._lock:
            conn = self._connect()
            row = conn.execute(
                "SELECT exit_time FROM positions WHERE status = 'closed' AND pnl_usd < 0 "
                "ORDER BY exit_time DESC LIMIT 1"
            ).fetchone()
            conn.close()
            return row["exit_time"] if row else None

    def get_equity_curve(self, limit: int = 500) -> list:
        with self._lock:
            conn = self._connect()
            rows = conn.execute(
                "SELECT pnl_usd, exit_time FROM ("
                "  SELECT pnl_usd, exit_time FROM positions WHERE status = 'closed' ORDER BY exit_time DESC LIMIT ?"
                ") ORDER BY exit_time ASC",
                (limit,),
            ).fetchall()
            conn.close()
            return [{"pnl_usd": r["pnl_usd"] or 0.0, "exit_time": r["exit_time"]} for r in rows]
