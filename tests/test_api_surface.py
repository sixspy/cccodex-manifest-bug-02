"""HTTP 层验收：页面能读到收口芯片，扫描台与批次附页读到同一结局。"""
import os

import pytest


@pytest.fixture()
def authed(tmp_path):
    from litestar.testing import TestClient

    from app.db import init_db
    from app.main import app

    db = tmp_path / "api.db"
    os.environ["DATABASE_PATH"] = str(db)
    init_db(str(db))
    with TestClient(app=app) as c:
        r = c.post("/api/login", json={"username": "admin", "password": "123456"})
        assert r.status_code == 200
        yield c


def _bid(authed, zone="bronze_orders"):
    items = authed.get("/api/batches", params={"status": "收集中"}).json()["items"]
    return next(b["id"] for b in items if b["zone_code"] == zone)


def test_close_response_carries_retain_chip_and_reconcile(authed):
    bid = _bid(authed)
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "a.p", "content_hash": "h1", "byte_size": 1},
    )
    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 200
    body = r.json()
    assert body["run"]["outcome"] == "留存"
    assert body["reconcile"]["chip"] == "留存"
    assert body["reconcile"]["progress_pct"] == 100
    assert body["reconcile"]["drifted"] is False

    # 闭合卡片附页
    runs = authed.get(f"/api/batches/{bid}/close-runs").json()["items"]
    assert runs[0]["outcome"] in ("留存", "抹掉")
    assert runs[0]["outcome"] == "留存"

    # 批次详情附 reconcile，四处同源
    detail = authed.get(f"/api/batches/{bid}").json()
    assert detail["reconcile"]["closed_count"] == 1
    assert detail["batch"]["status"] == "已闭合"


def test_interrupted_batch_resume_via_api(authed):
    bid = _bid(authed, "bronze_events")
    for k in ("x", "y", "z"):
        authed.post(
            f"/api/batches/{bid}/objects",
            json={"object_key": f"{k}.p", "content_hash": k, "byte_size": 1},
        )
    # 手工造半截态：批次闭合中断，只翻一个对象
    from app.db import connect

    with connect() as conn:
        conn.execute(
            "UPDATE landing_batches SET status='闭合中断', closed_object_count=1 WHERE id=?",
            (bid,),
        )
        conn.execute(
            "UPDATE landing_objects SET status='已闭合' WHERE id=("
            "SELECT id FROM landing_objects WHERE batch_id=? ORDER BY id LIMIT 1)",
            (bid,),
        )

    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 200
    assert r.json()["run"]["resumed"] == 1
    assert r.json()["run"]["outcome"] == "留存"
    rc = authed.get(f"/api/batches/{bid}/reconcile").json()
    assert rc["batch_status"] == "已闭合"
    assert rc["closed_count"] == 3
    assert rc["resumable"] is False

    report = authed.post(
        "/api/visibility-reports", json={"as_of_date": "2026-11-16"}
    ).json()["report"]
    assert report["mismatch_count"] == 0


def test_drift_returns_short_error_and_wipe_chip(authed):
    bid = _bid(authed, "bronze_payments")
    authed.post(
        f"/api/batches/{bid}/objects",
        json={"object_key": "p", "content_hash": "h", "byte_size": 1},
    )
    from app.db import connect

    with connect() as conn:
        conn.execute(
            "UPDATE landing_batches SET status='已闭合', closed_object_count=9 WHERE id=?",
            (bid,),
        )
    r = authed.post(f"/api/batches/{bid}/close", json={})
    assert r.status_code == 409
    # 失败吐短句
    assert len(r.json()["error"]) <= 30
    runs = authed.get(f"/api/batches/{bid}/close-runs").json()["items"]
    assert runs[0]["outcome"] == "抹掉"


def test_scan_endpoint_halfway_and_runs_page(authed, tmp_path, monkeypatch):
    root = tmp_path / "zone"
    root.mkdir()
    (root / "f1").write_bytes(b"1")
    (root / "f2").write_bytes(b"22")
    monkeypatch.setenv("SCAN_ROOT", str(root))
    bid = _bid(authed)

    r = authed.post(f"/api/batches/{bid}/scan", json={"rel_dir": ""})
    assert r.status_code == 200
    assert r.json()["scan_run"]["status"] == "扫完"
    assert r.json()["reconcile"]["registered_count"] == 2

    # 越界路径被拒
    bad = authed.post(f"/api/batches/{bid}/scan", json={"rel_dir": "../../etc"})
    assert bad.status_code == 400

    page = authed.get("/api/scan-runs").json()["items"]
    assert page[0]["batch_id"] == bid


def test_legacy_db_migrates_and_keeps_seed(tmp_path):
    import sqlite3

    db = tmp_path / "legacy.db"
    # 旧版结构：状态 CHECK 无闭合中断、close_runs 对 batch_id UNIQUE
    conn = sqlite3.connect(db)
    conn.executescript(
        """
        CREATE TABLE landing_batches (
          id INTEGER PRIMARY KEY AUTOINCREMENT, zone_code TEXT, zone_name TEXT,
          biz_date TEXT, status TEXT CHECK (status IN ('收集中','已闭合','已作废')),
          closed_object_count INTEGER, created_at TEXT DEFAULT (datetime('now')),
          UNIQUE(zone_code, biz_date));
        CREATE TABLE landing_objects (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER, object_key TEXT,
          content_hash TEXT, byte_size INTEGER, status TEXT, created_at TEXT DEFAULT (datetime('now')),
          UNIQUE(batch_id, object_key));
        CREATE TABLE close_runs (
          id INTEGER PRIMARY KEY AUTOINCREMENT, batch_id INTEGER UNIQUE,
          object_count INTEGER, ok INTEGER, detail TEXT, created_at TEXT DEFAULT (datetime('now')));
        CREATE TABLE visibility_reports (id INTEGER PRIMARY KEY, as_of_date TEXT,
          closed_batch_count INTEGER, open_batch_count INTEGER, object_count INTEGER,
          orphan_object_count INTEGER, mismatch_count INTEGER, summary TEXT, created_at TEXT);
        CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta(key,value) VALUES ('seeded','1');
        INSERT INTO landing_batches(zone_code,zone_name,biz_date,status,closed_object_count)
          VALUES ('z','老批','2026-11-10','已闭合',1);
        INSERT INTO landing_objects(batch_id,object_key,content_hash,byte_size,status)
          VALUES (1,'k','h',9,'已闭合');
        INSERT INTO close_runs(batch_id,object_count,ok,detail) VALUES (1,1,1,'老收口');
        """
    )
    conn.commit()
    conn.close()

    os.environ["DATABASE_PATH"] = str(db)
    from app.db import init_db
    from app.services import reconcile_batch

    init_db(str(db))
    # 新状态可写、老数据保留
    with sqlite3.connect(db) as c:
        c.execute(
            "UPDATE landing_batches SET status='闭合中断' WHERE id=1"
        )
        rows = c.execute("SELECT outcome FROM close_runs").fetchall()
    assert rows[0][0] == "留存"
    rc = reconcile_batch(1, str(db))
    assert rc["batch_status"] == "闭合中断"
    assert rc["closed_count"] == 1
