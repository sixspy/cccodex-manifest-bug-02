import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class Config:
    LAB_NAME = "湖区落地清单台"
    SECRET = os.environ.get("SECRET_KEY", "cc-manifest-dev-secret")
    ADMIN_USER = "admin"
    ADMIN_PASSWORD = "123456"
    DEFAULT_DB = str(ROOT / "instance" / "manifest_lab.db")
    # 目录扫描台根目录：只允许扫描该根之下的落地文件
    DEFAULT_SCAN_ROOT = str(ROOT / "landing_zone")
    # 收口事务命运：钉死“留存补缺口”单轨，禁止与整笔回滚轨同挂
    CLOSE_POLICY = "留存"

    @classmethod
    def database(cls) -> str:
        return os.environ.get("DATABASE_PATH", cls.DEFAULT_DB)

    @classmethod
    def scan_root(cls) -> str:
        return os.environ.get("SCAN_ROOT", cls.DEFAULT_SCAN_ROOT)
