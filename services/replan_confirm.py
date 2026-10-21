# -*- coding: utf-8 -*-
"""P3 replanning：重画确认卡（`user_confirm_redraw`，2026-10-21，**默认关**）。

**为什么需要它**（v2 §4.8 / G6）：
    P2 起重画会自动改用户的计划。虽然"重画必须可见"（修订卡）解决了
    **事后可见**，但没解决**事前控制** —— 用户眼睁睁看着自己的计划被换掉，
    只能事后追责。设计给了两条路：
      · 修订卡（**已随 P2 落地**，零成本、事件常开）—— 事后可见；
      · 确认卡（本模块，`user_confirm_redraw=True` 才启用）—— 事前否决权。

    **为什么确认卡默认关**（v2 §4.8 原文分工原则）：
    「澄清卡用于信息不足，重画用于证据充分 —— 确认卡是给"想要控制权"的
    用户的可选项，不是默认流程」。若默认开，每个重规划都要用户点一次，
    那就变成了新的阻塞点，恰好违反 v1 R10。

**三条硬纪律**：

    1. **确认卡不得成为新阻塞点**（v1 R10）。TTL 默认 60s，
       **超时一律"维持原计划继续"** —— 不是"超时自动批准重画"。
       反过来想：让一个可选卡在超时后改变用户计划，等于用沉默行使否决权，
       那用户会失去计划；让超时维持原计划，代价只是"这次重画没发生"
       （决策留痕可查）。**代价不对称 ⇒ 必须选后者**。
    2. **零行为变更**：默认关 ⇒ 本模块只被 `build_payload` 的早退分支使用，
       编排路径一行不变（门禁 U4 断言挂点段整体在开关内）。
    3. **状态按会话隔离 + 有界 + TTL**：与 `agent/followup.py` 同款纪律
       （模块级字典而非 `self`，因 `agent/__init__.py` 是**模块级单例**；
       挂实例 = 跨会话互相看到对方的重画决策）。

**纯函数纪律**：本模块**零 IO、零 DB**；落库与消费由调用方（stream.py）
与 REST 层负责—— 这样 TTL/越权/超时的判定能被门禁逐格单测。
"""
import threading
import time

#: 待确认决策的存活期（秒）。超时 ⇒ 维持原计划继续（v1 R10）。
DEFAULT_TTL_SEC = 60.0

#: 最多跟踪多少个会话的待确认（防内存泄漏：SSE 断连后没人应答）。
MAX_CONVS = 256

#: 已消费决议的留痕条数上限（与 `_PENDING` 同量级纪律）。
MAX_DECIDED = 256

#: 动作三态（对齐前端按钮文案）。
ACTION_APPROVE = "approve"      # 批准重画
ACTION_KEEP = "keep"            # 维持原计划
ACTION_SUPPLEMENT = "supplement"  # 我来补充指令

_LOCK = threading.Lock()
#: conv_id -> [{"id", "created", "expires", "level", "action_hint",
#:              "added", "dropped", "targets", "reason", "round"}]
_PENDING: dict = {}
#: (conv_id, confirm_id) -> resolve() 的返回；供 `wait_for` 取回（见 resolve 注释）。
#:   为什么需要：`resolve` 取走即清空队列，等待方下一轮只看到"队列空了"。
#:   若不留痕，它会报"无决策" ⇒ 用户点了"批准重画"却被静默当作没点。
_DECIDED: dict = {}


def _now():
    return time.time()


def _fid(conv_id, seq):
    return "rr%d-%d" % (int(conv_id or 0), int(seq or 0))


def build_payload(decision, *, run_id=0, round_no=1, ttl_sec=DEFAULT_TTL_SEC):
    """由重画决策生成确认卡 payload（**纯函数**：不碰模块状态）。

    `decision` 是 `replan_redraw.plan_redraw` 的返回（或任一含
    `level/action/reason/new_tasks/updates/dropped` 的 dict）。

    返回可直接 `yield` 给前端的 dict：
        `{"type": "redraw_confirm", "confirm_id", "level", "reason",
          "added", "dropped", "remap", "round", "ttl_sec", "actions": [...]}`

    ⚠️ `actions` 是**声明式**的（前端按它渲染按钮），不是让前端硬编码文案 ——
    后端改一次三态语义（例如加"暂不重画但记录原因"）时，前端无需同步改。
    """
    d = decision or {}
    added = [str(t.get("key") or "") for t in (d.get("new_tasks") or [])]
    dropped = [str(k) for k in (d.get("dropped") or [])]
    updates = [str(u.get("key") or "") for u in (d.get("updates") or [])]
    remap = {str(k): str(v) for k, v in (d.get("remap") or {}).items()}
    return {
        "type": "redraw_confirm",
        "run_id": int(run_id or 0),
        "level": str(d.get("level") or ""),
        "action_hint": str(d.get("action") or ""),
        "reason": str(d.get("reason") or ""),
        "added": added,
        "dropped": dropped,
        "retask": updates,
        "remap": remap,
        "round": int(round_no or 1),
        "ttl_sec": int(ttl_sec or DEFAULT_TTL_SEC),
        "actions": [
            {"id": ACTION_APPROVE, "label": "批准重画", "primary": True},
            {"id": ACTION_KEEP, "label": "维持原计划", "primary": False},
            {"id": ACTION_SUPPLEMENT, "label": "我来补充指令", "primary": False},
        ],
        # ★ 超时语义必须**显式下发**给前端：否则前端无法区分"没人管"
        #   与"超时自动批准"，文案会写错（R10 的用户可见面）。
        "timeout_behavior": "keep_original",
    }


def open_(conv_id, payload, *, ttl_sec=DEFAULT_TTL_SEC):
    """登记一个待确认决策，返回 `confirm_id`（空串 ⇒ 参数非法）。

    同一会话**同时只允许一个待确认**（新开覆盖旧的）：确认卡是"要不要重画"
    的二选一，堆两张只会让用户不知道自己批的是哪张。
    """
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return ""
    if cid <= 0 or not payload:
        return ""
    t = _now()
    with _LOCK:
        if cid not in _PENDING and len(_PENDING) >= MAX_CONVS:
            try:
                victim = min(_PENDING,
                             key=lambda k: (_PENDING[k][0]["created"]
                                            if _PENDING[k] else 0))
                _PENDING.pop(victim, None)
            except Exception:
                _PENDING.clear()
        seq = len(_PENDING.get(cid) or []) + 1
        _PENDING[cid] = [{
            "id": _fid(cid, t),
            "created": t,
            "expires": t + float(ttl_sec or DEFAULT_TTL_SEC),
            "level": str((payload or {}).get("level") or ""),
            "reason": str((payload or {}).get("reason") or ""),
            "added": list((payload or {}).get("added") or []),
            "dropped": list((payload or {}).get("dropped") or []),
            "retask": list((payload or {}).get("retask") or []),
            "remap": dict((payload or {}).get("remap") or {}),
            "round": int((payload or {}).get("round") or 1),
        }]
    return _fid(cid, t)


def get(conv_id, confirm_id=None):
    """查看待确认决策（**不消费**）。返回 dict 或 None。"""
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return None
    with _LOCK:
        arr = _PENDING.get(cid) or []
        if not arr:
            return None
        it = arr[-1]
        if confirm_id and str(confirm_id) != str(it["id"]):
            return None
        return dict(it)


def resolve(conv_id, confirm_id=None, action=ACTION_KEEP):
    """用户应答：取出并清空待确认决策。返回 `{"ok", "action", "item", "reason"}`。

    **TTL 过期 ⇒ 一律按 `keep` 处理**（v1 R10：超时维持原计划）。
    ⚠️ 这里**不是**"忽略过期项、让重画继续" —— 那正是 R10 禁止的
    "沉默行使否决权"。返回的 `item` 仍带决策内容，供调用方落决策留痕
    （用户能看到"曾有一次重画因超时未执行"）。
    """
    a = str(action or ACTION_KEEP)
    if a not in (ACTION_APPROVE, ACTION_KEEP, ACTION_SUPPLEMENT):
        a = ACTION_KEEP          # 非法动作按"维持原计划"兜底（最保守）
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return {"ok": False, "action": a, "item": None, "reason": "会话号非法"}
    with _LOCK:
        arr = _PENDING.get(cid) or []
        if not arr:
            return {"ok": False, "action": a, "item": None, "reason": "无待确认决策"}
        it = dict(arr[-1])
        _PENDING.pop(cid, None)
    expired = _now() > float(it.get("expires") or 0)
    if expired:
        return {"ok": True, "action": ACTION_KEEP, "item": it, "expired": True,
                "reason": "确认卡 TTL 过期 ⇒ 维持原计划（v1 R10）"}
    out = {"ok": True, "action": a, "item": it, "expired": False,
           "reason": "用户应答：%s" % a}
    # ★ 决议留痕（供 `wait_for` 取回）：`resolve` 取走即清空队列，而等待方
    #   下一次轮询只会看到"队列空了"—— 若不留痕，它只能报"无决策"，
    #   于是用户点了"批准重画"却被静默当作没点（真实链路 100% 复现）。
    #   保留期 = 一个 TTL：足够等待方取回，又不会让陈旧决议复活。
    with _LOCK:
        _DECIDED[(cid, str(it["id"]))] = out
        if len(_DECIDED) > MAX_DECIDED:
            for _k in list(_DECIDED)[:len(_DECIDED) - MAX_DECIDED]:
                _DECIDED.pop(_k, None)
    return out


def wait_for(conv_id, *, poll_s=1.0, timeout_s=None, _sleep=time.sleep,
             _clock=time.time):
    """阻塞等待用户应答（**测试可注入 `_sleep`/`_clock`**，不必真等）。

    返回 `resolve()` 的同款 dict。`timeout_s=None` ⇒ 一直等到 TTL 过期
    （TTL 是唯一的终止条件 —— 再加一个更长的等待上限只会让"卡片还亮着但
    后端已放弃"成为可能）。

⚠️ 阻塞期间**不持有 `_LOCK`**：`resolve()` 要拿同一把锁；持锁等待 =
        应答线程永远拿不到锁 ⇒ 死锁。这是本模块最容易踩的并发坑。
    """
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return {"ok": False, "action": ACTION_KEEP, "item": None,
                "reason": "会话号非法"}
    with _LOCK:
        _cur0 = _PENDING.get(cid) or []
        if not _cur0:
            # ★ 竞态窗口：应答可能发生在本函数取 `_last_seen` **之前**
            #   （REST 线程恰好先跑完）。此时队列已空但 `_DECIDED` 有留痕 ——
            #   入口就报"无待确认决策"会让用户点了批准却等来"无决策"，
            #   最终静默不重画。取回最近一条该会话的决议（留痕只存很短时间，
            #   误取风险可忽略；且宁可误取也不误报）。
            for (_c, _cid2), _d in list(_DECIDED.items()):
                if _c == cid:
                    _DECIDED.pop((_c, _cid2), None)
                    return _d
            return {"ok": False, "action": ACTION_KEEP, "item": None,
                    "reason": "无待确认决策"}
        _last_seen = str(_cur0[-1].get("id") or "")
    deadline = None if timeout_s is None else _clock() + float(timeout_s)
    while True:
        # ★ 四条纪律，每条都对应一个真实故障（③/④ 是 2026-10-21 行为探针
        #   跨线程实测抓到的：用户点"批准"发生在 `wait_for` 真正进入循环
        #   **之前**的竞争窗口里，此时 `_PENDING` 已空、`_DECIDED` 有记录 ——
        #   若只看队列就返回 ok=False，用户明明批准了、后端却按"无决策"收口，
        #   表现为"我点了批准，计划却没变"，且前端也无从提示）：
        #   ① 读 `_PENDING` 必须在锁内，但**调用 `resolve` 必须出锁** ——
        #      `threading.Lock` 不可重入，锁内调 `resolve` 会自己死等自己；
        #   ② 睡眠后要**回看决议**：应答发生在睡眠期间是常态（等待窗口正是
        #      用户点按钮的时刻）；
        #   ③ 只认**自己盯着的那张卡**（`_last_seen`）：期间若被 `open_` 换
        #      成新卡，上一张卡的应答不该应用到这一轮编排；
        #   ④ 队列为空时**先查 `_DECIDED`** 再决定是否报"无决策" ——
        #      应答与进入循环存在竞争窗口，这是唯一可靠的取回途径。
        with _LOCK:
            arr = _PENDING.get(cid) or []
            got = _DECIDED.get((cid, _last_seen))
            if not arr:
                if got:
                    _DECIDED.pop((cid, _last_seen), None)
                    return got
                return {"ok": False, "action": ACTION_KEEP, "item": None,
                        "reason": "无待确认决策"}
            expires = float(arr[-1].get("expires") or 0)
        now = _clock()
        if now >= expires:
            break
        if deadline is not None and now >= deadline:
            break
        _sleep(min(float(poll_s or 1.0),
                   max(0.05, min(expires, deadline or expires) - now)))
        # 出锁后再取决议（①②③）。**只在队列已空时回取决议** —— 卡还在队列里
        # 说明还没人应答，不能当成"已决议"提前返回（否则用户还没点就被推进，
        # 且这一张卡就再也没人认领）。
        with _LOCK:
            cur = _PENDING.get(cid) or []
            gone = (not cur)
            got = _DECIDED.get((cid, _last_seen))
        if gone:
            # 队列空了：`resolve` 成功消费时会写 `_DECIDED`，直接取回结果。
            # ⚠️ 不能直接 `resolve(cid, _last_seen)` —— 卡已被消费，二次
            #   resolve 会返回 ok=False，把用户的"批准"变成"无决策"，
            #   表现为「我明明点了批准，计划却没变」（真实链路 100% 复现）。
            if got:
                _DECIDED.pop((cid, _last_seen), None)
                return got
            return {"ok": False, "action": ACTION_KEEP, "item": None,
                    "reason": "无待确认决策"}
    # 走到这里 = TTL 过期 / 到达等待上限 ⇒ 按维持原计划收口（v1 R10）
    got = _DECIDED.pop((cid, _last_seen), None)
    if got:
        return got
    return resolve(cid, _last_seen)


def pending_count(conv_id):
    """该会话待确认数（给监控看，不消费）。"""
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return 0
    with _LOCK:
        return len(_PENDING.get(cid) or [])


def drop(conv_id):
    """清空该会话待确认（SSE 正常收尾时调用——不该把"要不要重画"留给下一轮）。"""
    try:
        cid = int(conv_id)
    except (TypeError, ValueError):
        return
    with _LOCK:
        _PENDING.pop(cid, None)


def stats():
    """诊断快照。"""
    with _LOCK:
        return {"convs": len(_PENDING),
                "items": sum(len(v) for v in _PENDING.values()),
                "max_convs": MAX_CONVS,
                "ttl_sec": DEFAULT_TTL_SEC}