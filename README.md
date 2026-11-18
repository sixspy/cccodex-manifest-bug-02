# cc-manifest-lab

CC_Codex 二期（26.11）**数据工程**产品线：**湖区落地清单台**（Litestar + Preact CDN + SQLite）。

## 一句话骨架（自有，非外仓抄题）

**湖区落地批次 → 对象指纹登记 → 清单闭合 → 可见性对账**

与现有七线正交（不是采集目录、ETL 管道、同步源、解析入库、编排流、种子摄取、留存裁撤）。
不碰外仓常见指纹（Feed 归并 / CDC 游标 / 质检签发 / Slicegate 类仓名）。

## 领域

- **所属领域**：数据工程
- **子方向**：数仓与数据湖（落地清单 / 对象登记闭环）

## 技术栈

| 层 | 选型 |
|----|------|
| 后端 | Python 3.12、Litestar、Uvicorn、SQLite |
| 前端 | 原生 JS SPA（全中文，侧栏），靛青竖纹 + 亮青强调；不依赖外网 CDN |
| 测试 | pytest + httpx（本地秒级闭环，优先于 compose） |

## 基线已通主链

1. 湖区落地批次列表（收集中 / 闭合中断 / 已闭合）
2. 向批次登记对象键与内容指纹
3. 闭合清单（对象数固化，拒绝空批）
4. 可见性对账报告（闭合批 vs 开放批 vs 孤儿对象）

## 收口事务边界（manifest-bug-02）

- 事务命运钉死单轨：**部分留存 + 补缺口**，成功亮 `留存` 芯片；收口失败整笔回滚亮 `抹掉` 芯片，两种命运不会同挂一笔。
- 批次新增 `闭合中断` 态：对象态半挂（部分已闭合、部分已登记）时批次落中断，「继续闭合」只补翻缺口，已闭合段原样留存。
- 收口走 `BEGIN IMMEDIATE` 写锁 + 状态复查：两名仓管同点继续，第二笔 409 挡回，杜绝双写。
- 对象态与批次态核齐（`app/services/reconcile.py` 唯一真相源）：漂了直接拒；对象进度、闭合卡片、批次附页、目录扫描台四处同读一份结局。
- 目录扫描台：逐文件独立事务入库，扫到半截保留已登记段、批次挂中断，再扫/继续闭合补缺口；路径越界拒绝。
- 旧版库启动自动迁移（新状态 CHECK、close_runs 去 batch_id 唯一并补 outcome/policy/resumed 列）。

二次验收步骤纸：[docs/acceptance-steps.md](docs/acceptance-steps.md)；故障演练：`scripts/drill_interrupt.py`。

## 本地（推荐反馈环）

```powershell
cd claude-workspace\cc-manifest-lab
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
pytest
python run.py
```

http://127.0.0.1:6900/login · **admin / 123456**

## Docker

```powershell
docker compose up -d --build
```

端口 **6900**。冒烟：`python ..\scripts\_smoke_lab_access.py --dir .`
