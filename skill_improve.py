"""P0-5 技能使用中自改进：SkillImprover（反馈采集 → 周期评估 → 修订草稿）。

对齐 Hermes Agent「技能在使用中自我改进」：
- 读取 skills.use_stats / feedback_notes（_record_skill_feedback 采集）
- 满足触发条件 → LLM 修订产出新版本行（status='draft_revision'，source='ai_improve'），
  保留原 published 行不动，人工审核后发布（沿用现有「人工审核发布」安全策略）。
- Mock/无 key → 跳过修订（不产出草稿，确定性保持）。
"""
import json
import time
from core import config as _cfg


class SkillImprover:
    """技能修订器：扫描触发 + LLM 修订（仅产出草稿，人工发布）。"""

    @staticmethod
    def _cfg(key: str, default):
        return _cfg.get("skill", key, default)

    @staticmethod
    def _enabled() -> bool:
        return _cfg.as_bool("skill", "self_improve_enabled", True)

    @classmethod
    def candidates(cls, conn) -> list:
        """扫描已发布技能，返回满足修订触发条件的技能列表 [{id,name,use_stats,feedback_notes}]。

        ★ 2026-10-29 判据调整（实测驱动的修正，非拍脑袋）：
          原判据 = 比例单通道：`uses>=min_fail and fails>=min_fail and fails/uses>=0.3`
          实测暴露**结构性盲区**：大样本技能被比例系统性惩罚 —
            Requirement_Analysis_Pre-check: uses=120 / fail=17 → 失败率 14.2%
            按 0.3 需攒到 **36 次失败**才提议修订，等攒够时污染面已很大；
            而小样本技能（uses=10/fail=3 = 30%）反而轻松触发。
          ⇒ 比例判据惩罚"用得多的技能"，与"该修谁"的直觉相反。
          现改为**双通道（OR）**：
            ① 比例通道：uses>=improve_min_uses 且 fails>=improve_min_fail
                         且 fails/uses>=improve_fail_ratio（默认已由 0.3 降到 0.12）
            ② 绝对通道（新增）：fails>=improve_abs_min_fail 且 uses>=improve_abs_min_uses
                         —— 高频技能的真实失败量不被比例稀释
            ③ 闲置通道（原有）：uses>=improve_min_uses 且超过 improve_idle_days 未更新
          三者任一命中即纳入候选；参数全部走 `skill.*` 配置（2026-10-29 已登记进
          CONFIG_SCHEMA，可在设置页改、可经 save_override 写，此前是硬编码默认值）。
        """
        if not cls._enabled():
            return []
        min_fail = int(cls._cfg("improve_min_fail", 3))
        fail_ratio = float(cls._cfg("improve_fail_ratio", 0.12))
        min_uses = int(cls._cfg("improve_min_uses", 10))
        # ★ 绝对失败数通道（2026-10-29）：补比例判据对大样本技能的盲区
        abs_min_fail = int(cls._cfg("improve_abs_min_fail", 15))
        abs_min_uses = int(cls._cfg("improve_abs_min_uses", 50))
        idle_days = int(cls._cfg("improve_idle_days", 30))
        max_rev = int(cls._cfg("improve_max_revisions", 5))
        rows = conn.execute(
            "SELECT id, name, content, triggers, description, use_stats, feedback_notes, updated_at "
            "FROM skills WHERE status='published'").fetchall()
        out = []
        now = time.time()
        for r in rows:
            try:
                st = json.loads(r["use_stats"] or "{}")
            except Exception:
                st = {}
            uses = int(st.get("uses", 0))
            fails = int(st.get("fail", 0))
            # 修订上限：同技能已有 ai_improve 草稿数量 ≥ 上限则停止自动提议
            rev_n = conn.execute(
                "SELECT COUNT(*) c FROM skills WHERE name=? AND source='ai_improve' AND status='draft_revision'",
                (r["name"],)).fetchone()
            if rev_n and int(rev_n["c"] or 0) >= max_rev:
                continue
            hit = False
            # ① 比例通道（2026-10-29 阈值 0.3 → 0.12）
            if uses >= min_uses and fails >= min_fail and (fails / max(uses, 1)) >= fail_ratio:
                hit = True
            # ② 绝对失败数通道：高频技能的真实失败量不被比例稀释
            #    （实测 Requirement_Analysis_Pre-check 17 次失败失败率仅 14.2%，
            #      单靠比例永远够不到 30% ⇒ 修不掉"用得越多越难过阈值"的反直觉）
            if fails >= abs_min_fail and uses >= abs_min_uses:
                hit = True
            # ③ 闲置通道：长期未更新的技能也体检
            if uses >= min_uses:
                try:
                    ut = time.mktime(time.strptime(r["updated_at"][:19], "%Y-%m-%d %H:%M:%S"))
                except Exception:
                    ut = now
                if (now - ut) > idle_days * 86400:
                    hit = True
            if hit:
                out.append({"id": r["id"], "name": r["name"], "content": r["content"] or "",
                            "triggers": r["triggers"] or "[]", "description": r["description"] or "",
                            "use_stats": st, "feedback_notes": r["feedback_notes"] or "[]"})
        return out

    @classmethod
    def improve_skill(cls, conn, skill: dict, provider_id=None) -> dict | None:
        """对单个技能执行 LLM 修订，产出 draft_revision 新版本行。成功返回 {name, version}，否则 None。"""
        if not cls._enabled():
            return None
        try:
            from llm import llm_client
            try:
                notes = json.loads(skill.get("feedback_notes") or "[]")
            except Exception:
                notes = []
            notes_txt = "\n".join(f"- {n.get('note')}" for n in notes[-5:] if n.get("note")) or "- 无失败备注"
            old_content = str(skill.get("content") or "")[:3000]
            try:
                triggers = json.loads(skill.get("triggers") or "[]")
            except Exception:
                triggers = []
            prompt = (
                "你是技能改进工程师。基于技能的使用反馈修订以下技能，输出 JSON：\n"
                '{"content": "修订后的技能指令正文（步骤化，200字内）", "triggers": ["触发词"], '
                '"description": "一句话描述", "changelog": "本次修订要点"}\n\n'
                f"技能名：{skill.get('name')}\n"
                f"当前触发词：{json.dumps(triggers[:10], ensure_ascii=False)}\n"
                f"当前描述：{skill.get('description', '')[:200]}\n\n"
                f"使用反馈（失败备注）：\n{notes_txt}\n\n"
                f"技能原文：\n{old_content}\n\n"
                "要求：只按反馈修订（补齐失败场景、明确边界）；保持 MBSE 领域适配；禁止虚构能力；只输出 JSON。"
            )
            resp = llm_client.chat([{"role": "user", "content": prompt}], provider_id=provider_id,
                                   _intent="skill_improve")
            # 降级策略：Mock/无 key 不产出修订草稿（确定性保持，避免低质量自动提议）
            if (resp.get("_meta") or {}).get("used_mock", True):
                return None
            raw = ((resp.get("choices") or [{}])[0].get("message", {}) or {}).get("content", "") or ""
            import re
            m = re.search(r"\{[\s\S]*\}", raw)
            data = json.loads(m.group(0)) if m else None
            if not data or not (data.get("content") or "").strip():
                return None
            new_body = str(data["content"])[:2000]
            new_triggers = [str(t) for t in (data.get("triggers") or [])[:10]]
            new_desc = str(data.get("description") or skill.get("description", ""))[:200]
            row = conn.execute(
                "SELECT version FROM skills WHERE name=? AND status='published'", (skill["name"],)).fetchone()
            old_ver = str(row["version"] if row else "v1")
            # ★ 2026-10-29 修复版本号解析（真跑暴露：产出两条同名同版本的 v11 草稿）：
            #   原实现 `int(re.sub(r"\D", "", old_ver) or 1) + 1`
            #   ⇒ `"v1.0"` 的非数字**全部**删掉（连小数点一起）得 `"10"` ⇒ v11，
            #     而语义上 v1.0 的主版本是 **1**，下一版应是 v2。
            #   后果：① 版本号跳号，审核面板出现"从 v1.0 直接跳到 v11"；
            #        ② 同一技能重复执行都算成 v11 ⇒ **多条同名同版本草稿**，
            #           人工无法区分哪条是新的（实测：一次手动 + 一次 handler 真跑
            #           各产一条 v11，内容还不一样）。
            #   现：取主版本号（v1.0 → 1），并**跳过已占用的版本号**保证唯一。
            _m = re.match(r"\s*[vV]?\s*(\d+)", old_ver)
            try:
                vn = (int(_m.group(1)) if _m else 1) + 1
            except Exception:
                vn = 2
            for _ in range(20):                       # 上限防死循环（既有版本号被打乱时）
                new_ver = f"v{vn}"
                _dup = conn.execute(
                    "SELECT 1 FROM skills WHERE name=? AND version=? LIMIT 1",
                    (skill["name"], new_ver)).fetchone()
                if not _dup:
                    break
                vn += 1
            cur = conn.execute(
                "INSERT OR IGNORE INTO skills (name, skill_type, description, content, triggers, "
                "version, status, source, use_stats, feedback_notes, dependencies, allowed_tools) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (skill["name"], "prompt", new_desc, new_body,
                 json.dumps(new_triggers, ensure_ascii=False), new_ver,
                 "draft_revision", "ai_improve",
                 json.dumps(skill.get("use_stats") or {}, ensure_ascii=False),
                 json.dumps(notes[-5:], ensure_ascii=False),
                 "[]", "[]"))
            conn.commit()
            if cur.rowcount:
                return {"name": skill["name"], "version": new_ver}
        except Exception:
            return None
        return None

    @classmethod
    def run_all(cls, conn, provider_id=None, limit: int = 5) -> dict:
        """周期入口：扫描全部候选并逐修订（限量防批量刷 LLM）。返回 {proposed: n, names: [...]}。"""
        if not cls._enabled():
            return {"proposed": 0, "names": [], "skipped": True}
        cands = cls.candidates(conn)[:max(int(limit), 1)]
        names = []
        for sk in cands:
            r = cls.improve_skill(conn, sk, provider_id)
            if r:
                names.append(f"{r['name']}@{r['version']}")
        return {"proposed": len(names), "names": names, "skipped": False}
