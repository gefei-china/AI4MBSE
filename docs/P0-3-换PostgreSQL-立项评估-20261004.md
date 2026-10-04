# P0-3（换 PostgreSQL）立项评估报告

> 结论先行：**这不是一批能做完的改动，是一次独立立项。** 但它解锁的东西（多副本 + 任务队列 + 在线扩容）只有它能给。
> 本报告只给**实测改造面**与**分期方案**，不含任何迁库代码。
> 口径：HEAD=`403f74f`（2026-10-04），全部数字来自本机grep/库查询，非估算。

---

## 一、P0-3 到底解决什么问题（不是"更快"）

先纠正一个容易误解的点：**SQLite 并不慢**。已开 WAL + `busy_timeout`，并发读没问题；
数据量（321.3 MB / 129 表 / 22487 行）对 PostgreSQL 毫无压力。

真正被卡住的是三件事：

| 卡点 | 实测证据 | 业务后果 |
|---|---|---|
| **不能横向扩** | `docker-compose.yml:8-12` 明写"单副本是硬约束"，且**刻意把 `deploy.replicas` 注释掉**防人误加 | 上云/上集群做不了；单机故障即全停 |
| **长任务没有去处** | celery / rq / arq / BackgroundTasks **全仓 0 命中**（`arq` 的 grep 命中都是中文注释里的 "graph"） | 单轮编排 344~1155 s 只能占着 HTTP 连接跑；没有"提交完就返回 job_id"的能力 |
| **批量写入必须停服务** | 三份规范入库实测 **13.4 分钟**（SQLite 单写者被占满） | 运维不可接受；也是 `safe_init_db` 存在的根因 |

⇒ **P0-3 的价值是"能多开、能排队、能在线扩容"，不是性能。**
如果部署形态就是单机私有化，这项的优先级应当下调（见 §五）。

---

## 二、改造面（这是决定要不要立项的关键）

| 项 | 实测值 | 说明 |
|---|---|---|
| **PG 驱动** | **未引入**（`psycopg/asyncpg/SQLAlchemy/alembic` 全无） | 从零开始，不是"打开开关" |
| **连接层** | `database/connection.py` 直接 `import sqlite3` | **无任何方言抽象**；这是最关键的一条 |
| **DAO 形态** | `repositories/` 21 文件 / 5734 行，确有 `BaseRepo` 收拢，但**只做连接与重试，不做方言** | 好消息：有一层可切入 |
| **直连 SQL** | **1634 处** `conn.execute(` / `cur.execute(` |其中 `routers/` 内 **451 处**、`database/` 内 470 处 |
| **`commit()` 调用** | **625 处** | 隐式事务边界，PG 下语义不同（autocommit / savepoint） |
| **`INSERT OR IGNORE`** | **59 处 / 26 个文件** | PG 无此语法，需改写为 `ON CONFLICT DO NOTHING` |
| **`INSERT OR REPLACE`** | 5 处（19 个文件命中该模式） | PG 无此语法，需改写为 `INSERT ... ON CONFLICT DO UPDATE` |
| **`AUTOINCREMENT`** | 120 处（DDL） | PG 用 `SERIAL`/`GENERATED ... AS IDENTITY` |
| **`sqlite_master` 直查** | 24 处 | PG 是 `information_schema` |
| **`julianday`/`strftime`** | 24 处，集中在 `orch_checkpoint`(4)/`branch_repo`(3)/`run_registry`(2)/`orch_supervisor`(2) | **时间口径是最易出错的一类**（我今天刚在 SQLite 上踩过 UTC vs 本地时间的坑） |
| **`rowid` / `last_insert_rowid()`** | 35 / 3 处 | PG 用 `RETURNING id` |
| **显式 `BEGIN`** | **0 处** | 全部依赖 driver 的隐式事务 ⇒ 切 PG 必须显式设计事务边界 |

**粗估**：按方言适配 + 逐文件改写的口径，这是**数人周**级工作量，且强依赖回归测试覆盖
（当前 `pytest` 只有 34 个用例，**覆盖不到 1634 处 SQL 里的绝大多数**）。

---

## 三、为什么现在不该开工（三个前置条件）

### 前置 1：回归测试覆盖不足 —— 这是硬阻塞
`pytest` **34 passed + 1 skipped / 13,335 行测试代码**，而生产 SQL 有 1634 处。
现在换库 ⇒ **没有安全网**。理性顺序是：**先补关键路径的 DAO 集成测试 → 再换库**。

### 前置 2：真正的业务需求还没确认
多副本/在线扩容的需求是否真实存在？如果仍是**单机私有化交付**，
P0-3 的价值只剩"能接任务队列"这一条 —— 而**队列不一定要靠 PG**
（Redis + RQ/arq 也能解决，且不触碰存储层）。

### 前置 3：替换成本与收益不成比例
若目标只是"长任务别占着HTTP 连接"，**引入队列 + 保持 SQLite** 的成本
远低于换库，且不触碰 1634 处 SQL。

---

## 四、分期方案（**若立项**，建议这么切）

| 阶段 | 内容 | 验收判据 | 是否解你的痛点 |
|---|---|---|---|
| **P-0（已完成）** | 迁移安全闸：dry-run / 自动热备 / 无备份不迁移 | `tools/migrate.py --dry-run` 零副作用；备份保真 | 部分（让"改库"不再不可回滚） |
| **P-A** | **DAO 层方言抽象**：引入 `dialect` 参数，把 `INSERT OR IGNORE` / `AUTOINCREMENT` / `julianday` / `sqlite_master` 收敛成兼容层 | 现有门禁全绿 + 同一份 SQL 在两种方言下都能跑 | 不解，但**是唯一必经之路** |
| **P-B** | **补 DAO 集成测试**（PG 与 SQLite 各跑一遍同一套用例） | 关键路径（会话/编排/知识库/检索）覆盖率达标 | 不解，但是**安全网** |
| **P-C** | **引入任务队列**（RQ/arq + Redis；SQLite 保持不变） | 长任务可"提交即返回 job_id"，且 SQLite 侧零改造 | ✅ **解"长任务占连接"** |
| **P-D** | 真正切 PG：迁移工具 + 数据搬迁 + 双跑校验 | 两库逐表行数与关键读路径结果一致 | ✅ 解全部三条 |

**关键建议**：**P-C 不必等 P-D**。如果卡点是"编排 1155s 占着连接"，
先做队列就能解决，不必动存储层。

---

## 五、决策建议（供拍板）

| 若你的诉求是 | 建议 |
|---|---|
| 私有化单机交付，继续做深| **不做 P0-3**，转做 P-C（队列）+ 继续偿还P2 技术债 |
| 要上云/多副本/高可用 | **立项**，但按 P-A → P-B → P-D 顺序，**先补安全网** |
| 只是想让长任务不占连接 | **只做 P-C**，成本最低、收益直接 |
| 不确定 | 先做 P-C（它对三种诉求都有收益），把 P-3 的决策推迟到有真实多副本需求时 |

---

## 六、可复现命令

```bash
cd mbse_system
# 驱动/ 抽象层
grep -inE "psycopg|pg8000|sqlalchemy|asyncpg|alembic" requirements.txt   # 无输出
grep -n "import sqlite3" database/connection.py
# 改造面
grep -rn "conn\.execute(\|cur\.execute(" --include=*.py . | grep -v "/.venv/" | wc -l   # 1634
grep -rn "\.commit()" --include=*.py . | grep -v "/.venv/" | wc -l                    # 625
grep -rlF "INSERT OR IGNORE" --include=*.py . | grep -v "/.venv/" | wc -l# 26 文件
grep -rn "julianday\|strftime" --include=*.py agent/ routers/ repositories/ core/ | wc -l   # 24
grep -rniE "\bcelery\b|^import rq|BackgroundTasks" --include=*.py . | grep -v "/.venv/" | wc -l  # 0
# 队列缺失确认（注意 arq 会误匹配中文注释里的 graph）
grep -rn "queue\|Queue" --include=*.py task_queue.py | head
```

---

## 七、局限声明

- 全部数字为**本机 grep/库查询**结果，未做 AST 级精确归属（`conn.execute(` 可能含子查询等），
  存在 ±5% 误差；结论不依赖个位精度。
- 未做 PG 实机验证（本机无 PG、无docker），因此**不声称"改造后一定无问题"**。
- 本报告**不含任何迁库代码**；真要启动请从 P-A（方言抽象）开始，
  并先回答 §三 的三个前置条件。