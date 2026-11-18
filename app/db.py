import sqlite3
from contextlib import contextmanager
from pathlib import Path

from app.config import Config

# 批次/对象状态词
COLLECTING = "收集中"
CLOSED = "已闭合"
INTERRUPTED = "闭合中断"
VOID = "已作废"

REGISTERED = "已登记"
OBJECT_CLOSED = "已闭合"
OBJECT_VOID = "已作废"

# 收口芯片（事务命运）
CHIP_RETAIN = "留存"
CHIP_WIPE = "抹掉"


def connect(db_path: str | None = None) -> sqlite3.Connection:
    path = db_path or Config.database()
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False, timeout=5)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    # 写锁最多互等 5s，避免两名仓管同点时无限挂起
    conn.execute("PRAGMA busy_timeout = 5000")
    return conn


@contextmanager
def get_db(db_path: str | None = None):
    conn = connect(db_path)
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


SCHEMA = """
CREATE TABLE IF NOT EXISTS landing_batches (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  zone_code TEXT NOT NULL,
  zone_name TEXT NOT NULL,
  biz_date TEXT NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','闭合中断','已作废')),
  closed_object_count INTEGER,
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(zone_code, biz_date)
);

CREATE TABLE IF NOT EXISTS landing_objects (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_key TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  byte_size INTEGER NOT NULL,
  status TEXT NOT NULL CHECK (status IN ('已登记','已闭合','已作废')),
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  UNIQUE(batch_id, object_key),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS close_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  ok INTEGER NOT NULL,
  outcome TEXT NOT NULL DEFAULT '留存' CHECK (outcome IN ('留存','抹掉')),
  policy TEXT NOT NULL DEFAULT '留存',
  resumed INTEGER NOT NULL DEFAULT 0,
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS scan_runs (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  batch_id INTEGER NOT NULL,
  scan_root TEXT NOT NULL,
  total_files INTEGER NOT NULL,
  ingested_count INTEGER NOT NULL,
  skipped_count INTEGER NOT NULL DEFAULT 0,
  status TEXT NOT NULL CHECK (status IN ('扫完','中断')),
  detail TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now')),
  FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
);

CREATE TABLE IF NOT EXISTS visibility_reports (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  as_of_date TEXT NOT NULL,
  closed_batch_count INTEGER NOT NULL,
  open_batch_count INTEGER NOT NULL,
  object_count INTEGER NOT NULL,
  orphan_object_count INTEGER NOT NULL,
  mismatch_count INTEGER NOT NULL,
  summary TEXT NOT NULL,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
"""

SEED_BATCHES = [
    ("bronze_orders", "订单铜区", "2026-11-14", "收集中"),
    ("bronze_payments", "支付铜区", "2026-11-14", "收集中"),
    ("bronze_events", "事件铜区", "2026-11-13", "收集中"),
    ("silver_orders", "订单银区", "2026-11-12", "已闭合"),
]


def _migrate(conn: sqlite3.Connection) -> None:
    """把旧版库（无“闭合中断”态、close_runs 单批唯一）迁到新结构。"""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(landing_batches)")}
    if cols:
        old_check = conn.execute(
            "SELECT sql FROM sqlite_master WHERE type='table' AND name='landing_batches'"
        ).fetchone()["sql"]
        if "闭合中断" not in old_check:
            conn.execute(
                "ALTER TABLE landing_batches RENAME TO landing_batches_old"
            )
            conn.execute(
                """
                CREATE TABLE landing_batches (
                  id INTEGER PRIMARY KEY AUTOINCREMENT,
                  zone_code TEXT NOT NULL,
                  zone_name TEXT NOT NULL,
                  biz_date TEXT NOT NULL,
                  status TEXT NOT NULL CHECK (status IN ('收集中','已闭合','闭合中断','已作废')),
                  closed_object_count INTEGER,
                  created_at TEXT NOT NULL DEFAULT (datetime('now')),
                  UNIQUE(zone_code, biz_date)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO landing_batches
                  (id, zone_code, zone_name, biz_date, status, closed_object_count, created_at)
                SELECT id, zone_code, zone_name, biz_date, status, closed_object_count, created_at
                FROM landing_batches_old
                """
            )
            conn.execute("DROP TABLE landing_batches_old")

    run_cols = {r["name"] for r in conn.execute("PRAGMA table_info(close_runs)")}
    if run_cols and "outcome" not in run_cols:
        # 旧表 batch_id UNIQUE 会挡住“同一批多次继续闭合”的附页记录，重建去掉
        conn.execute("ALTER TABLE close_runs RENAME TO close_runs_old")
        conn.execute(
            """
            CREATE TABLE close_runs (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              batch_id INTEGER NOT NULL,
              object_count INTEGER NOT NULL,
              ok INTEGER NOT NULL,
              outcome TEXT NOT NULL DEFAULT '留存' CHECK (outcome IN ('留存','抹掉')),
              policy TEXT NOT NULL DEFAULT '留存',
              resumed INTEGER NOT NULL DEFAULT 0,
              detail TEXT NOT NULL DEFAULT '',
              created_at TEXT NOT NULL DEFAULT (datetime('now')),
              FOREIGN KEY(batch_id) REFERENCES landing_batches(id)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO close_runs
              (id, batch_id, object_count, ok, outcome, policy, resumed, detail, created_at)
            SELECT id, batch_id, object_count, ok, '留存', '留存', 0, detail, created_at
            FROM close_runs_old
            """
        )
        conn.execute("DROP TABLE close_runs_old")


def init_db(db_path: str | None = None) -> None:
    with get_db(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)
        row = conn.execute("SELECT value FROM meta WHERE key='seeded'").fetchone()
        if row:
            return
        for zone, name, biz, status in SEED_BATCHES:
            conn.execute(
                "INSERT INTO landing_batches(zone_code, zone_name, biz_date, status, closed_object_count) "
                "VALUES (?,?,?,?,?)",
                (zone, name, biz, status, 2 if status == CLOSED else None),
            )
        closed = conn.execute(
            "SELECT id FROM landing_batches WHERE status=?", (CLOSED,)
        ).fetchone()
        if closed:
            bid = closed["id"]
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-000.parquet", "hash-silver-a", 1024, OBJECT_CLOSED),
            )
            conn.execute(
                "INSERT INTO landing_objects(batch_id, object_key, content_hash, byte_size, status) "
                "VALUES (?,?,?,?,?)",
                (bid, "part-001.parquet", "hash-silver-b", 2048, OBJECT_CLOSED),
            )
            conn.execute(
                "INSERT INTO close_runs(batch_id, object_count, ok, outcome, policy, detail) "
                "VALUES (?,?,1,?,?,?)",
                (bid, 2, CHIP_RETAIN, Config.CLOSE_POLICY, "种子闭合"),
            )
        conn.execute("INSERT INTO meta(key, value) VALUES ('seeded','1')")
