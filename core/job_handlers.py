# -*- coding: utf-8 -*-
"""P0-C 第二步（2026-10-04）：把**长任务业务端点**接到作业队列。

## 为什么需要这一步
`core/job_queue.py` 提供了"提交即返回 + 进度可查 + 租约回收 + 有上限重试"的队列能力，
但截至上一轮**全仓没有任何业务端点使用它**（已grep 复核：`job_queue` 只出现在
schema / 自身 / 门禁脚本里）。也就是说：能力齐了，**最后一公里没通**——
工程入库实测 13.4 分钟、SysML 批量入库同理，仍旧同步占着 HTTP 连接，
请求断开即前功尽弃（详见 core/job_queue.py 顶部的问题陈述）。

## 接哪些端点（按实测耗时排序，不按代码顺序）
| kind | 业务 | 实测/预估耗时 | 原端点 |
|---|---|---|---|
| `doc_ingest` | 文档入库（解析→分块→向量化→落库→上传即抽取） | 三份规范 ≈ 13.4 min | POST /api/documents/upload |
| `project_ingest` | 工程入库（逐版本候选化→融合闸→落图） | 分钟级 | POST /api/knowledge/project-ingest/commit |
| `v2g_extract` | 向量转图谱抽取（LLM，受本体 schema 约束） | 十秒~分钟级 | POST /api/knowledge/v2g/extract |
| `doc_reindex` | 重索引（全量重新向量化） | 与chunk 数成正比 | POST /api/documents/{id}/reindex |
| `doc_retry` | 失败文档重试（整条管道重跑） | 同 doc_ingest | POST /api/documents/{id}/retry |
| `knowledge_reconcile` | 写后调和（规则预处理 + 冲突检测） | 万级实体分钟级 | POST /api/knowledge/reconcile |

**未接的**：编排（/chat 走 orchestrator，已有独立的 checkpoint + resume 通道，
语义是"断点续跑"而非"重跑一遍"，套队列会与 resume 幂等打架）；
图谱批量实例化（graph/bulk，实测亚秒级，异步化反而增加往返）。

## 三条硬性设计约束（都是踩过坑才定下来的，改动前先读）

### ① 长作业**必须续租**，否则会被自己的 worker 回收并双跑
`job_queue.pick_next` 会把「running 且租约过期」的作业重新捞回来（这是崩溃恢复的正确设计）。
入库实测 13.4 分钟，而默认租约 300 s ⇒ 不续租的话，**单 worker 也会把自己的作业重领一遍**：
候选表被写两次、embedding 白烧一次、audit 出现两条重复记录。
⇒ 长 handler 一律用 `LeaseKeeper` 起后台心跳线程（连接独立，不与业务连接抢事务）。

### ② 幂等键必须锚定「**当次在途**作业」，不能锚定「历史上做过」
这是本仓反复踩过的同一型错误（见 MEMORY「兜底判据必须锚定当次操作」）：
若`submit()` 的去重判据是"这个 doc_id 提交过"，那么用户**第二次点「重试」会被永久吞掉**
（返回的是上一次的终态 job，看起来"点了没反应"）—— 功能被焊死。
⇒ 业务端点一律用 `job_queue.submit(..., reuse_terminal=False)`：
**只对在途（queued/running）去重，终态一律新建**。
这既挡住"手抖点两下"，又不挡"我要重跑一次"。

### ③ handler 不得复用 worker 的连接
`job_queue._run_one` 把连接用于回报终态；handler 若在同一连接上跑 13 分钟事务，
worker 的心跳/进度上报会被拖到事务提交后才落库 ⇒ 进度页看起来"卡住不动"。
⇒ 每个 handler 自开连接、自行commit/close（与 core/orch_supervisor 同一范式）。

## 进度语义（与 job_queue 的既有声明一致）
progress/stage 仅供人看；**业务判据只取 status**（终态/非终态）。
入库过程中能给出的真实阶段只有 parse→chunk→embed→insert 四段的边界，
管道内部不再细分（要细分就得改ingest_document 的签名，收益不抵风险）。
"""
import json
import os
import threading
import time
import traceback

__all__ = ["register_all", "KINDS", "LeaseKeeper", "handler_doc_ingest",
           "handler_doc_retry", "handler_doc_reindex", "handler_project_ingest",
           "handler_v2g_extract", "handler_knowledge_reconcile"]


# ══════════════════════════════════════════════════════════════════
# 租约心跳（约束①）
# ══════════════════════════════════════════════════════════════════
class LeaseKeeper:
    """长作业执行期间的**租约续期**守护（后台 daemon 线程）。

    为什么必须：作业排队时租约由 `pick_next` 写死 `lease_until = now + lease_s`，
    之后只有调用 `heartbeat()` 才会延长。入库实测 13.4 min> 默认租约 300 s，
    ⇒ 不续租 ⇒租约过期 ⇒ `pick_next` 把这条running 作业**当成崩溃残留重新捞回**，
    单worker 也会双跑（重复写候选表 + embedding 白烧 + audit 重复）。

    线程安全：心跳线程**自开连接**，不复用业务连接（避免与长事务互锁）；
    业务连接此刻可能正持有写锁，让心跳去写会直接database is locked。
    心跳失败只计数不抛 —— 心跳是"尽力续命"，抢不到锁时业务本身仍在推进，
    下一轮再续；真正判"作业还活着"的依据仍是 status + 业务进度。
    """

    def __init__(self, job_id: int, lease_s: int = 900, interval_s: float = 0.0):
        self.job_id = int(job_id)
        self.lease_s = int(lease_s or 900)
        #续租间隔取租约的 1/3：容忍连续两次心跳失败仍不丢租约
        self.interval_s = float(interval_s or max(5.0, self.lease_s / 3.0))
        self._stop = threading.Event()
        self._thread = None
        self.beats = 0
        self.errors = 0

    def _loop(self) -> None:
        from core import job_queue as jq
        while not self._stop.wait(self.interval_s):
            conn = None
            try:
                conn = jq._conn()
                if jq.heartbeat(conn, self.job_id, lease_s=self.lease_s):
                    self.beats += 1
                else:
                    self.errors += 1
            except Exception:
                self.errors += 1
            finally:
                try:
                    if conn is not None:
                        conn.close()
                except Exception:
                    pass

    def __enter__(self):
        self._thread = threading.Thread(
            target=self._loop, daemon=True,
            name="job-lease-%d" % self.job_id)
        self._thread.start()
        return self

    def __exit__(self, *_exc) -> bool:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        return False      # 不吞异常


def _own_conn():
    """handler 自开连接（约束③）。"""
    from database import get_db
    return get_db()


def _report(job: dict, percent=None, stage: str = "") -> None:
    """上报进度（用独立短连接，绝不复用业务连接）。"""
    conn = None
    try:
        from core import job_queue as jq
        conn = jq._conn()
        jq.progress(conn, int(job["id"]), percent, stage)
    except Exception:
        pass
    finally:
        try:
            if conn is not None:
                conn.close()
        except Exception:
            pass


def _err(exc: BaseException) -> str:
    return "%s: %s" % (type(exc).__name__, str(exc)[:300])


# ══════════════════════════════════════════════════════════════════
# handler：文档域
# ══════════════════════════════════════════════════════════════════
def handler_doc_ingest(payload: dict, job: dict) -> dict:
    """文档入库（异步版 upload）：从**源文件副本**跑完整条管道。

    为什么不直接传文件字节：作业 payload 是 JSON 文本，大文件塞进去既撑爆
    `job_jobs.payload`，也让"提交"这一步本身变慢（违背"提交即返回"）。
    ⇒ HTTP 侧只做轻量登记（documents行 + 副本落盘，见 knowledge_pipeline.
    stage_upload_document），worker 再从副本读回 —— 与失败重试
    （retry_document）**走完全相同的代码路径**，行为天然一致。
    """
    from knowledge_pipeline import run_staged_ingest
    doc_id = int(payload.get("doc_id") or 0)
    if not doc_id:
        raise ValueError("缺少 doc_id")
    conn = _own_conn()
    try:
        with LeaseKeeper(int(job["id"]), lease_s=int(payload.get("lease_s") or 900)):
            _report(job, 5, "解析文档（文本抽取 / OCR）…")
            res = run_staged_ingest(conn, doc_id, metadata=payload.get("metadata") or None)
            if res.get("parse_status") == "failed":
                _report(job, 100, "入库失败：%s" % (res.get("error") or "")[:80])
                #⚠️ 管道失败**不抛异常**：抛出去会让 worker 按attempt 盲目重试，
                #   而"解析失败"重试多少次都是同一结果（扫描件/损坏文件），
                #   白烧 embedding 额度。用 raise 表达"环境性失败才可重试"。
                res["failed_stage"] = "ingest"
                return res
            _report(job, 80, "落库完成，正在抽取候选…")
            _report(job, 100, "入库完成")
            return res
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handler_doc_retry(payload: dict, job: dict) -> dict:
    """失败文档重试（从源文件副本重跑整条管道）。"""
    from knowledge_pipeline import retry_document, search as _kp_search
    doc_id = int(payload.get("doc_id") or 0)
    if not doc_id:
        raise ValueError("缺少 doc_id")
    conn = _own_conn()
    try:
        with LeaseKeeper(int(job["id"]), lease_s=int(payload.get("lease_s") or 900)):
            _report(job, 10, "重置文档状态并重跑管道…")
            res = _kp_search.retry_document(conn, doc_id)
            if res.get("parse_status") == "failed":
                res["failed_stage"] = "retry"
                return res
            # 与同步端点保持一致：抽取开关关闭时回填 extraction='disabled'
            try:
                import json as _j
                _sw = {r["key"]: r["value"] for r in conn.execute(
                    "SELECT key, value FROM settings").fetchall()}
                if (_sw.get("file_auto_extract_enabled") or "0") != "1":
                    _cur = _j.loads(conn.execute(
                        "SELECT pipeline_detail FROM documents WHERE id=?",
                        (doc_id,)).fetchone()[0] or "{}")
                    _cur["extraction"] = "disabled"
                    conn.execute("UPDATE documents SET pipeline_detail=? WHERE id=?",
                                 (_j.dumps(_cur, ensure_ascii=False), doc_id))
                    conn.commit()
            except Exception:
                pass
            _report(job, 100, "重试完成")
            return res
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handler_doc_reindex(payload: dict, job: dict) -> dict:
    """重索引：按当前 embedder 全量重新向量化（保留 section / bm25_text）。"""
    from knowledge_pipeline import reindex_document
    doc_id = int(payload.get("doc_id") or 0)
    if not doc_id:
        raise ValueError("缺少 doc_id")
    conn = _own_conn()
    try:
        with LeaseKeeper(int(job["id"]), lease_s=int(payload.get("lease_s") or 900)):
            _report(job, 10, "重新向量化全部分块…")
            res = reindex_document(conn, doc_id)
            if res.get("parse_status") == "failed":
                res["failed_stage"] = "reindex"
                return res
            _report(job, 100, "重索引完成")
            return res
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# handler：知识库域
# ══════════════════════════════════════════════════════════════════
def handler_project_ingest(payload: dict, job: dict) -> dict:
    """工程入库（逐版本候选化 → 融合闸 → 自动批准 → 落个人分支图库）。

    失败语义：整体失败（含"工程不存在""没有可入库版本"）抛异常 → worker 按
    attempt 重试；但 `status='partial'`（部分版本失败）**视为成功**返回 ——
    部分成果已经落库且project_ingest_logs 有台账，静默重跑会造成重复写入。
    """
    from services.knowledge_service import KnowledgeService
    from database import get_db
    project_id = str(payload.get("project_id") or "")
    if not project_id:
        raise ValueError("缺少 project_id")
    # 该服务在调用内部频繁 commit（分阶段写库），给它独立连接
    conn = get_db()
    try:
        with LeaseKeeper(int(job["id"]), lease_s=int(payload.get("lease_s") or 900)):
            _report(job, 5, "逐版本候选化与融合…")
            res = KnowledgeService(conn).project_ingest_commit(
                project_id, actor=payload.get("actor") or "system",
                target_branch=payload.get("target_branch") or "personal",
                version_ids=payload.get("version_ids") or None)
            if not res.get("ok"):
                #环境性问题（工程不存在 / 无版本）才值得重试
                if res.get("status") == "failed" and not res.get("versions"):
                    raise RuntimeError(res.get("error") or "工程入库失败")
                res.setdefault("degraded", True)
            _report(job, 100, "工程入库完成（%s）" % (res.get("status") or "?"))
            return res
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handler_v2g_extract(payload: dict, job: dict) -> dict:
    """向量转图谱抽取（LLM，受本体 schema 约束）。"""
    from services.knowledge_service import KnowledgeService
    from database import get_db
    conn = get_db()
    try:
        with LeaseKeeper(int(job["id"]), lease_s=int(payload.get("lease_s") or 900)):
            _report(job, 10, "命中分块并抽取候选…")
            res = KnowledgeService(conn).v2g_extract(
                str(payload.get("query") or ""), payload.get("chunk_ids") or None,
                int(payload.get("top_k") or 5), payload.get("doc_id") or None,
                actor=payload.get("actor") or "system")
            _report(job, 100, "抽取完成：候选 %d"
                    % ((res.get("node_count") or 0) + (res.get("edge_count") or 0)))
            return res
    finally:
        try:
            conn.close()
        except Exception:
            pass


def handler_knowledge_reconcile(payload: dict, job: dict) -> dict:
    """写后调和（规则预处理 + 冲突检测）。幂等作业，可重复执行。"""
    from services.knowledge_service import KnowledgeService
    from database import get_db
    conn = get_db()
    try:
        with LeaseKeeper(int(job["id"]), lease_s=int(payload.get("lease_s") or 900)):
            _report(job, 20, "规则预处理（Blocking 化消歧）…")
            res = KnowledgeService(conn).knowledge_reconcile(
                actor=payload.get("actor") or "system")
            _report(job, 100, "调和完成")
            return res
    finally:
        try:
            conn.close()
        except Exception:
            pass


# ══════════════════════════════════════════════════════════════════
# 注册表
# ══════════════════════════════════════════════════════════════════
KINDS = {
    "doc_ingest": handler_doc_ingest,
    "doc_retry": handler_doc_retry,
    "doc_reindex": handler_doc_reindex,
    "project_ingest": handler_project_ingest,
    "v2g_extract": handler_v2g_extract,
    "knowledge_reconcile": handler_knowledge_reconcile,
}


def register_all() -> list:
    """把全部业务 handler 注册进队列。返回 kind 列表（便于门禁断言）。"""
    from core import job_queue as jq
    for kind, fn in KINDS.items():
        jq.register_handler(kind, fn)
    return sorted(KINDS)


def describe() -> dict:
    """给 /api/jobs/kinds 用：可提交的业务作业类型 + 必填 payload 字段。"""
    return {
        "doc_ingest": {"label": "文档入库", "required": ["doc_id"]},
        "doc_retry": {"label": "失败文档重试", "required": ["doc_id"]},
        "doc_reindex": {"label": "文档重索引", "required": ["doc_id"]},
        "project_ingest": {"label": "工程入库", "required": ["project_id"]},
        "v2g_extract": {"label": "向量转图谱抽取", "required": ["query"]},
        "knowledge_reconcile": {"label": "写后调和", "required": []},
    }