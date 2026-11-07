# CI-OPTIONAL: [local] 需 20 轮真实 SysML 校验（checker.jar + java-runtime，后者不入库）与 ~200s 时长；本机实测 20/20。CI 无 java-runtime 会假红
# -*- coding: utf-8 -*-
"""E2E 门禁：需求 → 条目 → 需求视图 → 候选 → 确认入库（连跑 N 次，断言成功率）。

## 为什么必须"连跑 20 次"
2026-11-07 实测的根因不是某个环节不通，而是**串联可靠性断层**：
`sysml_v2_validate` 95.7% × `import_sysml_text` 57.1% × `zhiyuan_project_tree` 55.8%
⇒ 端到端只有三成。**"跑通过一次"与"可重复"是两件不同的事**，验收口径必须是后者。

## 本门禁覆盖的真实链路（全部走生产代码，不 mock）
    ① requirement_itemize  文本 → items/gaps/需求视图骨架代码
    ② requirement_commit   条目落库（幂等：同文第二次 inserted 必须为 0）
    ③ sysml_v2_validate    骨架代码过 OMG 参考实现校验器（checker.jar）
    ④ sysml_versions       写入版本（project_id 定格）
    ⑤ sysml_to_candidates  版本 → v2g 候选（SYSM- 批次）
    ⑥ v2g_confirm          人工确认 → 实体/关系入库（图谱真的增长）

## 隔离
跑在 `init_db()` 建出的**临时库**上（`MBSE_DB_PATH` 指向 tmp），
绝不碰生产库 —— 20 次循环每个都产生实体与候选，写生产库就是污染。

用法：
    python tools/verify/verify_requirement_closure_e2e.py [--runs 20] [--min-rate 0.9]
"""
import argparse
import os
import shutil
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

#: 20 段**互不相同**的真实需求（不同领域/句数/质量），避免"同一句重复 20 次"自欺
SAMPLES = [
    "电池包在 -20℃ 环境下 30 分钟内应完成加热至 10℃ 以上；电芯间最大温差不超过 5℃；系统应通过 CAN 总线每 100ms 上报状态；系统应具有良好的用户体验。",
    "热管理系统应具备快充工况下的电池预冷功能，使电池在快充开始前温度处于 25℃ 至 35℃；冷却液出口温度不高于 65℃；应支持故障码上报。",
    "乘员舱制冷量在 40℃ 环境温度下不低于 5kW；制热在 -20℃ 下 30 分钟内将舱温提升至 20℃；噪声不超过 55dB(A)。",
    "驱动电机冷却回路应保证电机绕组温度不超过 150℃；冷却液流量不低于 8L/min；应能识别水泵卡滞故障并上报。",
    "热泵系统制热 COP 在 -10℃ 工况下不低于 1.5；应支持 PTC 辅助加热；系统应尽可能节能。",
    "电池热失控预警应至少提前 5 分钟触发；预警后应自动断开高压回路；应记录预警事件的时间与温度。",
    "整车控制器通过 CAN 总线与热管理控制器通信，报文周期不大于 100ms；丢帧率不超过 0.1%。",
    "热管理系统应支持远程 OTA 升级，升级过程不影响整车行驶安全；升级失败应能回滚到上一版本。",
    "空调压缩机转速调节范围 800 至 8000rpm；调节响应时间不超过 200ms；应支持软启动。",
    "电池包冷却板进出口温差不超过 3℃；流阻不超过 15kPa；应满足 IP67 防护等级。",
    "系统应具备高压安全防护，绝缘电阻不低于 100Ω/V；高压互锁断开后 100ms 内切断输出。",
    "热管理控制器应支持 12V 与 24V 双电压输入；静态电流不超过 3mA；应具备反接保护。",
    "在 NEDC 工况下热管理系统能耗不超过整车可用能量的 15%；应支持能量回收模式。",
    "系统应提供故障诊断接口，支持 UDS 服务 0x19 读取故障码；诊断响应时间不超过 50ms。",
    "冷却风扇转速应随冷凝压力线性调节；风扇故障应在 5 秒内被检出并降级运行。",
    "系统应保证乘员舱除霜性能满足国标要求；除霜面积在 20 分钟内不低于 80%。",
    "热管理系统应具备良好的维修便利性；关键部件更换时间不超过 30 分钟。",
    "电池加热膜功率不低于 3kW；加热均匀性偏差不超过 10%；应具备过温保护。",
    "系统应支持与云端平台的数据交互，上传周期可配置为 10s 至 300s；断网时应本地缓存不少于 24 小时。",
    "热管理系统的控制策略应支持参数在线标定；标定参数应掉电不丢失；应支持标定版本管理。",
]


def _new_db() -> tuple:
    """建一个干净的临时库（schema + 种子 + 资产快照）。"""
    tmpdir = tempfile.mkdtemp(prefix="req_e2e_")
    db = os.path.join(tmpdir, "e2e.db")
    os.environ["MBSE_DB_PATH"] = db
    # 必须在 import database 之前设置 env（core.config 在 import 期读环境）
    from database.schema import init_db
    init_db()
    return tmpdir, db


def _seed_project(conn) -> tuple:
    pid = "e2e_closure_proj"
    conn.execute("INSERT OR REPLACE INTO projects (id, name, code, description, tool_binding) "
                 "VALUES (?,?,?,?,?)", (pid, "闭环验证工程", "E2E-CLOSURE", "E2E 门禁用临时工程", "{}"))
    cur = conn.execute("INSERT INTO conversations (title, project_id) VALUES (?,?)",
                       ("E2E 闭环会话", pid))
    conn.commit()
    return pid, cur.lastrowid


def _entity_count(conn) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM entities").fetchone()[0])


def _one_round(conn, repo, text: str, pid: str, conv_id: int, idx: int) -> dict:
    """跑完整闭环一次，返回可断言的观测值。"""
    from requirement_itemize_tools import exec_tool as req_exec
    from sysml_check_tools import exec_tool as check_exec
    from sysml_importer import sysml_to_candidates
    from services.knowledge_service import KnowledgeService

    obs = {"idx": idx}
    # ① 条目化
    r1 = req_exec("requirement_itemize", {"text": text, "domain": "thermal", "source_ref": f"e2e-{idx}"})
    if not r1.get("ok") or not r1.get("items"):
        return {**obs, "fail": f"itemize: {(r1.get('result') or '')[:120]}"}
    obs["items"] = len(r1["items"])
    obs["gaps"] = len(r1.get("gaps") or [])

    # ② 落库（幂等：第二次 inserted=0）
    r2 = req_exec("requirement_commit", {"text": text, "project": pid, "domain": "thermal"},
                  conversation_id=conv_id)
    if not r2.get("ok"):
        return {**obs, "fail": f"commit1: {(r2.get('result') or '')[:120]}"}
    r2b = req_exec("requirement_commit", {"text": text, "project": pid, "domain": "thermal"},
                   conversation_id=conv_id)
    obs["ins1"] = r2.get("inserted")
    obs["ins2"] = r2b.get("inserted")
    obs["db_items"] = repo.stats(pid)["items"]

    # ③ 需求视图骨架过校验器（真实 checker.jar）
    code = r1.get("req_view_code") or ""
    if not code.strip():
        return {**obs, "fail": "req_view_code 为空"}
    ck = check_exec("sysml_v2_validate", {"code": code})
    obs["validate_ok"] = bool(ck.get("ok"))
    if not ck.get("ok"):
        return {**obs, "fail": f"validate: {str(ck.get('result'))[:160]}"}

    # ④ 版本落库（project_id 定格）
    cur = conn.execute(
        "INSERT INTO sysml_versions (conversation_id, project_id, version_label, content, "
        "element_summary, status, created_by, code_text) VALUES (?,?,?,?,?,?,?,?)",
        (conv_id, pid, f"e2e{idx:02d}", "{}", "{}", "current", "e2e", code))
    vid = cur.lastrowid
    conn.commit()
    obs["version_id"] = vid

    # ⑤ 候选化
    before = _entity_count(conn)
    cand = sysml_to_candidates(conn, code, version_id=vid, model_name=f"e2e{idx}", source="text")
    if cand.get("error"):
        return {**obs, "fail": f"candidates: {cand['error']}"}
    batch = cand["batch_id"]
    conn.execute("UPDATE sysml_versions SET imported_batch=? WHERE id=?", (batch, vid))
    conn.commit()
    obs["nodes"] = cand.get("node_count", 0)
    obs["edges"] = cand.get("edge_count", 0)

    # ⑥ 确认入库（真实审核链，不跳过任何闸）
    #   V2 生成候选有**强制待审门闸**（vector2graph：sysml_version_id>0 且
    #   review_status!='submitted' ⇒ 只标记提交、不入库）。所以真实链路是：
    #     confirm#1（自动置 submitted，返回 pending_review）
    #     → confirm#2（生成待审三元组）
    #     → 审核通过（本门禁代替人工点"通过"，**这一步在真实使用中是人做的**）
    #     → commit_approved_triples（三元组反写 entities/relations）
    # 跳过任一环都会得到"看起来通了但图库没变"的假绿。
    ids = [r[0] for r in conn.execute(
        "SELECT id FROM v2g_candidates WHERE batch_id=? AND status='pending' LIMIT 20",
        (batch,)).fetchall()]
    obs["pending"] = len(ids)
    if ids:
        ks = KnowledgeService(conn)
        ks.v2g_confirm(batch, ids, actor="e2e", dup_action="create")   # ① 首次：置 submitted
        conn.commit()
        ks.v2g_confirm(batch, ids, actor="e2e", dup_action="create")   # ② 二次：生成待审三元组
        conn.commit()
        n_triples = conn.execute(
            "SELECT COUNT(*) FROM triples WHERE sysml_version_id=? AND status<>'approved'",
            (vid,)).fetchone()[0]
        obs["triples"] = int(n_triples)
        # 人工审核（门禁代行）
        conn.execute("UPDATE triples SET status='approved', review_decision='approve', "
                     "reviewed_by='e2e', reviewed_at=CURRENT_TIMESTAMP "
                     "WHERE sysml_version_id=? AND status<>'approved'", (vid,))
        conn.commit()
        from triple_commit import commit_approved_triples
        cm = commit_approved_triples(conn, operator="e2e")
        conn.commit()
        obs["confirmed"] = (cm or {}).get("entities", 0)
    else:
        obs["confirmed"] = 0
        obs["triples"] = 0
    after = _entity_count(conn)
    obs["entities_delta"] = after - before
    obs["ok"] = True
    conn.commit()      # 释放写事务：下一轮的 commit 走独立连接，否则必冲突
    return obs


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=20)
    ap.add_argument("--min-rate", type=float, default=0.9)
    args = ap.parse_args()

    t0 = time.time()
    tmpdir, db = _new_db()
    ok_cnt, fails, obs_all = 0, [], []
    try:
        from database import get_db
        from repositories.requirement_repo import RequirementRepo
        conn = get_db()
        pid, conv_id = _seed_project(conn)
        repo = RequirementRepo(conn)
        for i in range(args.runs):
            text = SAMPLES[i % len(SAMPLES)]
            # 同一样本跨轮复用会命中幂等（inserted=0）——只有 i>=len(SAMPLES) 才发生，
            # 故把「首轮必须真插入」的判据只落在 i<len(SAMPLES) 上。
            try:
                obs = _one_round(conn, repo, text, pid, conv_id, i)
            except Exception as exc:                            # noqa: BLE001
                obs = {"idx": i, "fail": f"{type(exc).__name__}: {exc}",
                       "tb": traceback.format_exc()[-400:]}
            obs_all.append(obs)
            if obs.get("ok"):
                ok_cnt += 1
            else:
                fails.append(obs)
        # ── 判据 ─────────────────────────────────────────────
        rate = ok_cnt / max(1, args.runs)
        checks = []
        checks.append(("A1 成功率 ≥ %.0f%%" % (args.min_rate * 100),
                       rate >= args.min_rate, f"{rate:.1%}（{ok_cnt}/{args.runs}）"))
        first_ins = [o.get("ins1") for o in obs_all[:len(SAMPLES)] if o.get("ok")]
        checks.append(("A2 首轮每条样本都真插入（items>0）",
                       bool(first_ins) and all((x or 0) > 0 for x in first_ins),
                       f"ins1={first_ins}"))
        second_ins = [o.get("ins2") for o in obs_all if o.get("ok")]
        checks.append(("A3 幂等：重跑 inserted=0",
                       bool(second_ins) and all((x or 0) == 0 for x in second_ins),
                       f"ins2 去重={sorted(set(second_ins))}"))
        val = [o.get("validate_ok") for o in obs_all if o.get("ok")]
        checks.append(("A4 需求视图骨架过 checker.jar", bool(val) and all(val),
                       f"{sum(1 for v in val if v)}/{len(val)}"))
        ent = [o.get("entities_delta", 0) for o in obs_all if o.get("ok")]
        checks.append(("A5 确认入库真的增长实体", bool(ent) and sum(ent) > 0,
                       f"总增 {sum(ent)}（每轮 {ent[:6]}…）"))
        stats = repo.stats(pid)
        checks.append(("A6 需求落地为资产（按工程可查）", stats["items"] > 0, str(stats)))
        committed = conn.execute("SELECT COUNT(*) FROM sysml_versions WHERE "
                                "imported_batch<>'' AND project_id=?", (pid,)).fetchone()[0]
        checks.append(("A7 版本回填 imported_batch（闭环标记）",
                       committed >= ok_cnt * 0.9, f"{committed}/{ok_cnt}"))

        print("=" * 68)
        print(f"E2E 门禁：需求→条目→视图→候选→入库　runs={args.runs}　用时 {time.time()-t0:.1f}s")
        print("=" * 68)
        for name, passed, detail in checks:
            print(f"  [{'PASS' if passed else 'FAIL'}] {name} —— {detail}")
        if fails:
            print(f"\n失败 {len(fails)} 例（前 3 例）：")
            for f in fails[:3]:
                print(f"  run#{f.get('idx')}: {f.get('fail')}")
        all_ok = all(c[1] for c in checks)
        print("\n" + ("ALL_OK" if all_ok else "FAILED"))
        conn.close()
        return 0 if all_ok else 1
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
