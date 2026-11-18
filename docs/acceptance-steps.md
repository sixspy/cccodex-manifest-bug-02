# 二次验收步骤纸（manifest-bug-02 收口事务边界）

事务命运已钉死单轨：**部分留存 + 补缺口**。成功亮 `留存` 芯片，失败整笔回滚亮 `抹掉` 芯片，两种命运不会同挂一笔。

## 0. 起服务

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python run.py        # http://127.0.0.1:6900 ，admin / 123456
```

自动化：`.venv/bin/python -m pytest -q`（应 15 passed）。

## 1. 正常闭合回归（对象进度 + 闭合卡片）

1. 打开「落地批次」→ 打开订单铜区（bronze_orders）。
2. 登记两个对象，点「闭合清单」。
3. 期望：对象列表两行全部 `已闭合`，进度条 100%；闭合卡片亮 **留存** 芯片，附页 close_runs 新增一行 `留存/首次`；批次状态 `已闭合`。
4. 再点闭合 → 短句「批次已闭合」，409，无新写入。

## 2. 闭合中断 → 继续闭合补缺口（核心验收）

造现场（本机库可查）：

```bash
.venv/bin/python scripts/drill_interrupt.py --db instance/manifest_lab.db --half-close <批次ID>
```

1. 刷新批次详情：批次状态为黄色 `闭合中断`，进度条停在 50%，对象态一半 `已闭合` 一半 `已登记`；按钮文案变「继续闭合（补缺口）」。
2. 点「继续闭合」：期望成功，**已闭合段原样留存**（对照对象 ID 未变），缺口对象补翻为已闭合，批次升 `已闭合`，进度 100%。
3. 闭合附页新增一行：**留存 / 补缺口**，说明含「本次补 N 个」。
4. 跑「可见性对账」：错位 = 0。

## 3. 漂移拒绝（对象态与批次态核齐）

```sql
-- 批次自称已闭合、固化数 9，实际只有 1 个对象闭合
UPDATE landing_batches SET status='已闭合', closed_object_count=9 WHERE id=<BID>;
```

点继续闭合 → 期望 409 短句「对象态与批次态不一致，已拒绝收口」；对象态零改动；附页新增 **抹掉** 芯片一行（ok=0）。

## 4. 双仓管并发（无锁双写防护）

```bash
# 两个终端同时对同一个闭合中断批次发请求
curl -s -X POST -b cookie.txt -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:6900/api/batches/<BID>/close &
curl -s -X POST -b cookie.txt -H 'Content-Type: application/json' -d '{}' \
  http://127.0.0.1:6900/api/batches/<BID>/close &
```

期望：一笔 200，一笔 409「另一笔收口正在执行，请稍后再试」；库中 close_runs 成功行恰好 1 条，对象无重复翻转。自动化对应 `test_concurrent_resume_no_double_write`。

## 5. 目录扫描台半截 → 补缺口（复验一：目录扫描）

1. 造不可读文件：
   ```bash
   .venv/bin/python scripts/drill_interrupt.py --unscan landing_zone/bronze_orders 2
   ```
2. 扫描台选收集中批次，子目录填 `bronze_orders`，点「目录入库扫描」。
3. 期望：提示「扫描中断」，scan_runs 附页状态 `中断`，已登记段保留（part-000 已入库），批次挂 `闭合中断`。
4. 恢复权限：`... --reset landing_zone/bronze_orders 2`，再扫一次：状态 `扫完`，新文件入库、旧文件计入跳过。
5. 点继续闭合 → 整批 `已闭合`；扫描台卡片与批次详情卡片读到同一 reconcile 结局（进度、芯片、漂移字段一致）。

## 6. 收口失败整笔回滚（复验二：补缺口路径的抹掉）

自动化：`test_close_failure_leaves_wipe_chip_and_rolls_back` 注入写故障。
期望：批次仍 `收集中`、对象全部仍 `已登记`，close_runs 留 **抹掉** 痕；故障消除后同批可重新收口成功并亮 **留存**。

## 7. 本机库查询速查

```bash
sqlite3 instance/manifest_lab.db \
 "SELECT b.id,b.status,b.closed_object_count,
   SUM(o.status='已闭合') 实闭合, SUM(o.status='已登记') 挂登记
   FROM landing_batches b LEFT JOIN landing_objects o ON o.batch_id=b.id
   GROUP BY b.id;"
SELECT id,batch_id,ok,outcome,resumed,detail FROM close_runs ORDER BY id DESC;
SELECT id,batch_id,status,ingested_count,total_files FROM scan_runs ORDER BY id DESC;
```
