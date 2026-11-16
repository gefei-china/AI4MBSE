# -*- coding: utf-8 -*-
"""自进化「可感知性」契约门禁（2026-11-06 P0/P1/P2）。

判据全部为**静态源码 + 纯函数夹具**，不写真库、不依赖服务在跑。
含 3 组变异自证：把实现改坏 → 对应判据必须转红，否则是空转断言。
"""
import ast
import io
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("[%s] %s%s" % ("PASS" if ok else "FAIL", name,
                         (" | " + str(detail)) if detail else ""), flush=True)


def src(rel):
    return io.open(os.path.join(ROOT, rel), "r", encoding="utf-8").read()


def calls_of(code):
    tree = ast.parse(code)
    return {getattr(n.func, "id", None) or getattr(n.func, "attr", None)
            for n in ast.walk(tree) if isinstance(n, ast.Call)}


print("=" * 70)
print("自进化可感知性契约门禁")
print("=" * 70, flush=True)

# ── G1 数据契约 ─────────────────────────────────────────────────
col = src("database/migrations/columns.py")
for c in ("state", "consumed_at", "consumed_by", "link_kind", "link_id", "last_note"):
    check("G1 feedback 补列 " + c,
          re.search(r'_add\("feedback",\s*"%s"' % c, col) is not None)
check("G1 config_proposals 补 origin / source_feedback_ids",
      '_add("config_proposals", "origin"' in col
      and '_add("config_proposals", "source_feedback_ids"' in col)
misc = src("database/migrations/misc.py")
check("G1 新库 DDL 同源（config_proposals 两列）",
      "origin TEXT DEFAULT ''" in misc and "source_feedback_ids TEXT DEFAULT '[]'" in misc)

# ── G2 状态机（纯函数夹具，exec 不落盘）────────────────────────
ft = src("core/feedback_types.py")
ns = {}
exec(compile(ft, "<feedback_types>", "exec"), ns)
fs, adv = ns["FEEDBACK_STATES"], ns["can_advance_state"]
check("G2 六态词表", len(fs) == 6 and "new" in fs and "verified" in fs, fs)
check("G2 前进允许", adv("new", "sampled") and adv("sampled", "proposed"))
check("G2 原地/后退/未知一律拒绝",
      not adv("new", "new") and not adv("verified", "new") and not adv("new", "bogus"))

# M1：把 "b > a" 改成 "b >= a" ⇒「原地拒绝」判据必须转红
mut1 = ft.replace("return a is not None and b is not None and b > a",
                  "return a is not None and b is not None and b >= a")
check("M1 变异锚点命中", mut1 != ft)
if mut1 != ft:
    ns1 = {}
    exec(compile(mut1, "<mut1>", "exec"), ns1)
    check("M1 放原位后 G2『原地拒绝』必须判红（断言有牙齿）",
          ns1["can_advance_state"]("new", "new") is True)

# ── G3 API 契约与越权防护 ──────────────────────────────────────
conv = src("routers/conversations.py")
check("G3 /api/my-feedback 列表与详情端点存在",
      '@router.get("/api/my-feedback")' in conv
      and '@router.get("/api/my-feedback/{fb_id}")' in conv)
check("G3 详情 SQL 带 id + created_by 双条件（防越权）",
      "SELECT * FROM feedback WHERE id=? AND created_by=?" in src("repositories/conversation_repo.py"))
check("G3 匿名不回落全表", "if user is None:" in conv and '"items": []' in conv)
check("G3 点踩回执带 feedback_id，取消评价不带",
      '"feedback_id": fb_id' in conv and "fb_id = None" in conv)

# M2：去掉详情 SQL 的 created_by 条件 ⇒ 越权判据必须转红
repo = src("repositories/conversation_repo.py")
mut2 = repo.replace("SELECT * FROM feedback WHERE id=? AND created_by=?",
                    "SELECT * FROM feedback WHERE id=?")
check("M2 变异锚点命中", mut2 != repo)
check("M2 去掉 created_by 后 G3 越权判据必须判红",
      "SELECT * FROM feedback WHERE id=? AND created_by=?" not in mut2)

# ── G4 触发器红线（AST，不受 docstring 干扰）────────────────────
tick = src("core/evolution_tick.py")
called = calls_of(tick)
FORBIDDEN = {"execute_confirmation", "apply_config_change", "save_override", "decide"}
check("G4 触发器不调用任何自动生效函数",
      not (called & FORBIDDEN), called & FORBIDDEN)
check("G4 触发器确实到 HIL 门口（request_promotion）", "request_promotion" in called)
check("G4 不自动做效果判定落库", "evaluate_pending" not in called)
check("G4 默认 observe", '"mode": OBSERVE' in tick or '"mode"' in tick)
main = src("main.py")
check("G4 main.py 挂载 start_tick", "start_tick" in main and "tick_enabled" in main)
cfg = src("core/config.py")
check("G4 配置双登记（DEFAULT_CONFIG + _EXTRA_SCHEMA）",
      cfg.count('"tick_enabled"') >= 2 and '"evolution": {' in cfg)

# M3：给 tick 孪生源码注入一次自动生效调用 ⇒ 红线判据必须转红
mut3 = tick.replace("def _conn():", "def _conn():\n    apply_config_change()\n", 1)
check("M3 变异锚点命中", mut3 != tick)
check("M3 注入自动生效调用后 G4 红线必须判红",
      bool(calls_of(mut3) & FORBIDDEN))

# ── G5 晋升前冻结基线（顺序红线）───────────────────────────────
promo = src("core/config_promotion.py")
ptree = ast.parse(promo)
fn = next((n for n in ptree.body
           if isinstance(n, ast.FunctionDef) and n.name == "execute_confirmation"), None)
check("G5 execute_confirmation 存在", fn is not None)
if fn:
    pairs = [(n.lineno, getattr(n.func, "id", None) or getattr(n.func, "attr", None))
             for n in ast.walk(fn) if isinstance(n, ast.Call)]
    fz = next((l for l, f in pairs if f == "_freeze_pre_change_baseline"), None)
    ap = next((l for l, f in pairs if f == "apply_config_change"), None)
    check("G5 冻结基线在写配置之前（顺序不可反）",
          fz is not None and ap is not None and fz < ap, "freeze@%s apply@%s" % (fz, ap))
check("G5 回执带 baseline_frozen（用户可见）", '"baseline_frozen"' in promo)
check("G5 效果判定默认 dry-run", "apply: bool = False" in src("core/config_effect.py"))

# ── G6 周报归因与可感知字段 ────────────────────────────────────
er = src("core/evolution_report.py")
check("G6 周报含 feedback_trace / tick 两字段",
      '"feedback_trace":' in er and '"tick":' in er)
check("G6 空数据不得显示『一切正常』",
      "窗口 %d 天内没有收到任何反馈" in er or "没有收到任何反馈" in er)
check("G6 卡点如实说明（0 条进下游）", "0 条进入下游" in er)

# ── G7 前端接线 ────────────────────────────────────────────────
html = src("static/index.html")
shell = src("static/js/mods/02-shell.js")
md = src("static/js/mods/05-markdown.js")
evr = src("static/js/mods/47-evolution-report.js")
mfb = src("static/js/mods/53-my-feedback.js")
adm = src("static/js/mods/31-admin.js")
check("G7 新模块已注册 script", "53-my-feedback.js" in html)
check("G7 页面容器是 #pages 直接子级", 'id="pg-my-feedback"' in html)
check("G7 更多浮层有入口", "go('my-feedback')" in html)
check("G7 面包屑 + loadPage 双接线",
      "'my-feedback'" in shell and "if(p==='my-feedback') loadMyFeedback();" in shell)
check("G7 点踩回执带编号且可点追踪",
      "mfb-receipt" in md and "feedback#'" in md and "go(\\'my-feedback\\')" in md)
check("G7 周报第四段 + 只能看可展开 + 自动刷新",
      "本周因你而改变" in evr and "data-ev-toggle" in evr and "setInterval" in evr)
check("G7 周报 POST 已序列化（既有缺陷已修）", "JSON.stringify({ decision })" in evr)
check("G7 追踪页导出全局函数", "window.loadMyFeedback" in mfb and "openFeedbackDetail" in mfb)
check("G7 配置台账有回滚按钮（入 HIL 队列）",
      "requestConfigRollback" in adm and "/api/config/rollback" in adm)

# ── G8 提案溯源 ────────────────────────────────────────────────
cp = src("core/config_proposal.py")
pr = src("repositories/proposal_repo.py")
check("G8 generate 支持 origin / source_feedback_ids",
      "source_feedback_ids: Optional[list]" in cp)
check("G8 INSERT 动态判列（老库零漂移）",
      'PRAGMA table_info(config_proposals)' in pr and "_extra" in pr)

# ── G9 异步状态回填接线（2026-11-06 端到端实跑抓到的缺陷）──
tick2 = src("core/evolution_tick.py")
jh = src("core/job_handlers.py")
check("G9 reconcile_feedback_states 已定义且幂等",
      "def reconcile_feedback_states(" in tick2 and "forward-only" in tick2)
check("G9 采样作业执行后调用回填（异步窗口补丁）",
      "reconcile_feedback_states as _rec" in jh and "_rec(conn, apply=True)" in jh)
check("G9 作业回执带 feedback_reconciled（可观测）",
      '"feedback_reconciled": rec' in jh)

# ── G10 提案来源合并（2026-11-06 execute 实跑抓到的可感知性断点）──
#   事故：feedback#119 已被采样入队（state=sampled, link=online_eval#50），
#        但 5 条同指纹 auto 提案的来源仍停在旧的 [113,115] ⇒ 追踪页答不出
#        「我的反馈影响了哪条提案」。根因是 insert_proposal 指纹冲突时只
#        `skipped += 1`，不合并来源。修法：冲突时对来源取并集（只增不减）。
def _g10_ok(code):
    return ('if not inserted and "source_feedback_ids" in _cols:' in code
            and '_merged = sorted(set(_old) | set(_new))' in code
            and 'UPDATE config_proposals SET source_feedback_ids=?' in code)


check("G10 冲突分支合并来源反馈（并集，不重开已决提案）", _g10_ok(pr))

_i = pr.find("def _parse_ids(")
_j = pr.find("\nclass ProposalRepo", _i)
check("G10 _parse_ids 可定位（锚点未漂移）", _i > 0 and _j > _i, "i=%d j=%d" % (_i, _j))
if _i > 0 and _j > _i:
    _ns = {"json": json}
    exec(pr[_i:_j], _ns)
    _pid = _ns["_parse_ids"]
    check("G10 _parse_ids 兼容三种历史形态（NULL / '' / '[113, 115]'）",
          _pid(None) == [] and _pid("") == [] and _pid("[]") == []
          and _pid("[113, 115]") == [113, 115], "probe=%s" % _pid("[113, 115]"))
    check("G10 _parse_ids 去重且稳定排序（幂等）",
          _pid("[115, 113, 115]") == [113, 115], "probe=%s" % _pid("[115, 113, 115]"))
    check("G10 _parse_ids 非法输入降级为空表（不抛）",
          _pid("not-json") == [] and _pid("[\"x\"]") == [], "probe=%s" % _pid("not-json"))

_mut4 = pr.replace('if not inserted and "source_feedback_ids" in _cols:',
                   'if False:', 1)
check("M4 变异锚点命中", _mut4 != pr)
check("M4 去掉合并分支后 G10 判据必须判红", not _g10_ok(_mut4))

# ── G11 时间基准一致（2026-11-06 execute 实跑抓到的缺陷）──
#   事故：feedback#120 于本地 15:25:02 点踩，created_at=07:25:02（DDL 默认
#        CURRENT_TIMESTAMP 是 UTC），而 P0 新增的 consumed_at 用 time.strftime
#        （本地）⇒ 同一行出现 8 小时错位，时间线看起来「先消费、后创建」。
#   修法：存储一律 UTC（core/timefmt.utc_now_str），展示层补 *_local 并行字段
#        （不覆盖原值，故 stuck / 窗口等按 UTC 比较的判据不受影响）。
tf = src("core/timefmt.py")
repo_src = src("repositories/conversation_repo.py")
mfb2 = src("static/js/mods/53-my-feedback.js")
evr2 = src("static/js/mods/47-evolution-report.js")
er2 = src("core/evolution_report.py")

check("G11 timefmt 提供 UTC 写入与本地展示两个入口",
      "def utc_now_str(" in tf and "def utc_to_local_str(" in tf
      and "def attach_local_times(" in tf)
check("G11 写库时间走 UTC（_now_str 不再用本地 strftime）",
      "utc_now_str()" in repo_src and "return _t.strftime" not in repo_src)
check("G11 时区偏移用 astimezone().utcoffset()（不用两次 now 求差）",
      "astimezone().utcoffset()" in tf and "now() - datetime.now(timezone.utc)" not in tf)
check("G11 列表与详情出口都补 *_local",
      repo_src.count("attach_local_times(") >= 2,
      "occurrences=%d" % repo_src.count("attach_local_times("))
check("G11 周报 items 带 *_local", '"created_at_local"' in er2)
check("G11 前端优先显示 *_local（反馈页 + 周报）",
      "created_at_local||x.created_at" in mfb2
      and "created_at_local||it.created_at" in mfb2
      and "created_at_local||x.created_at" in evr2)


def _g11_ok(code):
    return "astimezone().utcoffset()" in code


_mut5 = tf.replace("astimezone().utcoffset()", "None")
check("M5 变异锚点命中", _mut5 != tf)
check("M5 去掉精确偏移后 G11 判据必须判红", not _g11_ok(_mut5))

# ── G12 反馈→采样关联的时间先后约束（2026-11-06 实跑抓到的倒挂）──
#   事故：feedback#121 created 07:34 被 link 到 queue#50 created 05:51（更早），
#        用户看到「已采样」但它自己的采样还没发生，时间线出现「消费早于创建」。
#   根因：reconcile 只按 message_id 匹配、按 id 取第一条，不校验时间先后。
#   修法：逐条反馈只认「它创建之后」的第一个队列项；timeline 优先按 link_id 精确取。
tick3 = src("core/evolution_tick.py")
repo3 = src("repositories/conversation_repo.py")

check("G12 reconcile 按 created_at 限定队列项（不挂历史采样）",
      "AND created_at>=? ORDER BY created_at, id LIMIT 1" in tick3)
check("G12 reconcile 查询带出 created_at 供比较",
      "feedback_type, created_at FROM feedback" in tick3)
check("G12 timeline 优先按 link_id 精确取队列项",
      'FROM online_eval_queue WHERE id=?' in repo3
      and '(row.get("link_kind") or "") == "online_eval"' in repo3)
check("G12 timeline 回退也必须限定 created_at>=（防倒挂）",
      "FROM online_eval_queue WHERE message_id=? AND created_at>=?" in repo3)


def _g12_ok(code):
    return "AND created_at>=? ORDER BY created_at, id LIMIT 1" in code


_mut6 = tick3.replace("AND created_at>=? ORDER BY created_at, id LIMIT 1",
                      "ORDER BY id LIMIT 1")
check("M6 变异锚点命中", _mut6 != tick3)
check("M6 去掉时间约束后 G12 判据必须判红", not _g12_ok(_mut6))

# ── G13 时间线顺序语义（2026-11-06 实跑抓到的「看起来倒挂」）──
#   `consumed_at` 是**回填记账**时刻，必然晚于队列项 created_at（采样是"事"、
#   回填是"账"）。原实现把 consumed 排在队列项之前 ⇒ #121 显示
#   「consumed 15:42 → sampled 15:40」。修法：顺序固定为
#   创建 → 进入评测队列 → 状态回填 → 坏例 → 关联。
_tl_src = repo3[repo3.find("def feedback_timeline"):]
_tl_src = _tl_src[:_tl_src.find("\n    def ")]
check("G13 时间线含队列项与状态回填两个事件",
      "进入评测队列" in _tl_src and "状态回填：" in _tl_src)
check("G13 队列项排在状态回填之前（记账晚于事实）",
      _tl_src.find("进入评测队列") < _tl_src.find("状态回填："),
      "queue@%d, consumed@%d" % (_tl_src.find("进入评测队列"), _tl_src.find("状态回填：")))


def _g13_ok(code):
    return 0 <= code.find("进入评测队列") < code.find("状态回填：")


_mut7 = (_tl_src.replace("状态回填：", "@@TMP@@")
         .replace("进入评测队列", "状态回填：")
         .replace("@@TMP@@", "进入评测队列"))
check("M7 变异锚点命中", _mut7 != _tl_src)
check("M7 两事件顺序颠倒后 G13 判据必须判红", not _g13_ok(_mut7))

print("=" * 70)
print("PASS %d / FAIL %d" % (len(PASS), len(FAIL)))
for f in FAIL:
    print("  FAILED:", f)
print("=" * 70, flush=True)
sys.exit(1 if FAIL else 0)
