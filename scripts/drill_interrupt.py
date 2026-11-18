#!/usr/bin/env python3
"""二次验收故障演练：造“闭合中断 / 扫描半截”现场。

用法（在已运行的实例库上操作，默认 instance/manifest_lab.db）：
  .venv/bin/python scripts/drill_interrupt.py --db instance/manifest_lab.db --half-close 1
  .venv/bin/python scripts/drill_interrupt.py --db instance/manifest_lab.db --unscan landing_zone/bronze_orders 2

--half-close BID   把批次 BID 置为闭合中断：翻一半对象到已闭合，其余留已登记
--unscan DIR N     把 DIR 下第 N 个文件设为不可读，供扫描台复现“扫到半截”
--reset DIR N      恢复该文件权限
"""
import argparse
import os
import stat
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.db import INTERRUPTED, OBJECT_CLOSED, connect  # noqa: E402


def half_close(db: str, bid: int) -> None:
    with connect(db) as conn:
        batch = conn.execute(
            "SELECT status FROM landing_batches WHERE id=?", (bid,)
        ).fetchone()
        if not batch:
            raise SystemExit(f"批次 {bid} 不存在")
        rows = conn.execute(
            "SELECT id FROM landing_objects WHERE batch_id=? AND status='已登记' ORDER BY id",
            (bid,),
        ).fetchall()
        if not rows:
            raise SystemExit("批次下没有已登记对象，无法演练中断")
        half = max(1, len(rows) // 2)
        for r in rows[:half]:
            conn.execute(
                "UPDATE landing_objects SET status=? WHERE id=?",
                (OBJECT_CLOSED, r["id"]),
            )
        conn.execute(
            "UPDATE landing_batches SET status=?, closed_object_count=? WHERE id=?",
            (INTERRUPTED, half, bid),
        )
        print(f"批次 {bid} 已置为闭合中断：{half}/{len(rows)} 个对象闭合，缺口 {len(rows) - half} 个")


def chmod_file(root: str, index: int, mode: int) -> None:
    files = sorted(p for p in Path(root).rglob("*") if p.is_file())
    if not files:
        raise SystemExit(f"{root} 下没有文件")
    target = files[index - 1]
    target.chmod(mode)
    print(f"{target} 权限 -> {oct(stat.S_IMODE(target.stat().st_mode))}")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=os.environ.get("DATABASE_PATH", "instance/manifest_lab.db"))
    ap.add_argument("--half-close", type=int, metavar="BID")
    ap.add_argument("--unscan", nargs=2, metavar=("DIR", "N"))
    ap.add_argument("--reset", nargs=2, metavar=("DIR", "N"))
    args = ap.parse_args()
    if args.half_close:
        half_close(args.db, args.half_close)
    if args.unscan:
        chmod_file(args.unscan[0], int(args.unscan[1]), 0o000)
    if args.reset:
        chmod_file(args.reset[0], int(args.reset[1]), 0o644)
    if not (args.half_close or args.unscan or args.reset):
        ap.print_help()


if __name__ == "__main__":
    main()
