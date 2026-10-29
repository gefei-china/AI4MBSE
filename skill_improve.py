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

    # ══════════════════════════════════════════════════════════════════
    # 修订自检闸门（self-verification，2026-10-29）
    # ══════════════════════════════════════════════════════════════════
    # 为什么必须有：实测产出过一份 4830 → 453 字（-90.6%）的草稿，
    # 丢掉的正是「写操作前必须复述并获确认」「不得虚构已完成」
    # 「先过 sysml_v2_validate」等**安全护栏**。若靠人眼看，这类草稿会先污染
    # 审核队列（实测 3 条里有 1 条不合格，占 33%）。
    # 行业依据：Voyager（arXiv:2305.16291）的消融显示
    #   **去掉 self-verification 后效果下降 73%**，是其最重要的反馈信号；
    #   且论文明确「保存的技能应视为**未审核代码**，而非知识」。
    #   ⇒ 产出前做确定性校验，不合格**不产出草稿**（比产出坏草稿更安全：
    #     坏草稿一旦被误采纳，会拆掉线上护栏）。
    # 判据全部是**确定性规则**（不依赖 LLM 自评，避免"LLM 评 LLM"的自我循环）。
    #: 允许的最大缩水比例（修订后 < 原文 85% 视为破坏性压缩）
    REVISION_MIN_KEEP_RATIO = 0.85
    #: 安全护栏关键词：原文含有的，修订后必须仍然含有（缺一即拒绝）
    GUARDRAIL_KEYS = (
        "必须", "不得", "禁止", "严禁", "务必", "应先", "须先", "先确认",
        "回读", "校验", "确认后", "影响面", "不许", "不能",
    )

    @classmethod
    def _verify_revision(cls, old_body: str, new_body: str) -> tuple:
        """修订自检：返回 (是否通过, 原因)。确定性规则，不调 LLM。

        三条判据（任一不过即拒绝产出草稿）：
          ① **不得破坏性压缩**：修订后字数 < 原文 × REVISION_MIN_KEEP_RATIO
             （实测事故：4830 → 453 字，-90.6%）
          ② **不得丢失安全护栏**：原文出现的护栏关键词，修订后必须仍有
             （例：原文「写操作前必须复述并获用户确认」被删 ⇒ 拒绝）
          ③ **不得清空**：修订后去掉空白后过短（< 原文 10% 且 < 40 字）
        只对**长技能**（原文 ≥ 300 字）做强校验：短技能（如 154 字的 Pre-check）
        本来就应该被重写，强行卡 85% 反而阻止合理改进。
        """
        old_len = len((old_body or "").strip())
        new_len = len((new_body or "").strip())
        if new_len == 0:
            return False, "修订后正文为空"
        # ③ 清空判定（对任何长度的技能都适用）
        if new_len < 40 and new_len < old_len * 0.1:
            return False, f"修订后过短（{new_len} 字，原文 {old_len} 字）"
        # 短技能不做比例与护栏强校验（重写是合理的）
        _MIN_LEN_FOR_STRICT = 300
        if old_len < _MIN_LEN_FOR_STRICT:
            return True, ""
        # ① 破坏性压缩
        ratio = new_len / max(old_len, 1)
        if ratio < cls.REVISION_MIN_KEEP_RATIO:
            return False, (f"破坏性压缩：{old_len} → {new_len} 字"
                           f"（保留 {ratio:.1%}，下限 {cls.REVISION_MIN_KEEP_RATIO:.0%}）")
        # ② 安全护栏丢失
        lost = [k for k in cls.GUARDRAIL_KEYS if k in (old_body or "") and k not in (new_body or "")]
        if lost:
            return False, f"丢失安全护栏关键词：{'、'.join(lost[:6])}"
        return True, ""

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
            # ★★ 2026-10-29 修复「破坏性压缩」（实测事故）：
            #   原实现 `old_content = str(...)[:3000]` —— 4830 字的《智源平台管理》手册
            #   **只有前 3000 字进了 prompt**，第 4~6 章（源码导出协议、报错自愈、
            #   安全规范）LLM 根本没看见；叠加 prompt 里"200字内"的硬要求，
            #   产出草稿 4830 → 453 字（-90.6%），丢掉的正是
            #   「写操作前必须复述并获确认」「不得虚构已完成」「先过 sysml_v2_validate」
            #   这些**安全护栏**。
            #   行业对照：
            #     · Voyager（arXiv:2305.16291）技能库「只增不改」——
            #       "learning a new skill cannot overwrite an old one"；
            #       改写既有大型技能必须保留其全部能力，否则等价于"覆盖丢失"。
            #     · GEPA（DSPy）靠 metric 驱动小步改写，从不给字符数上限。
            #   修法：① 输入按原文长度动态给预算（不再硬截 3000）；
            #        ② 明确"保留原文全部要点与安全护栏，只改与失败相关部分"；
            #        ③ 输出上限也按原文长度放大，禁止反向压缩。
            _raw_body = str(skill.get("content") or "")
            _orig_len = len(_raw_body)
            # 输入预算：原文不超过 24k 字就全给（单技能手册量级足够）；超长才截并标注
            _IN_BUDGET = 24000
            if _orig_len > _IN_BUDGET:
                old_content = (_raw_body[:_IN_BUDGET]
                               + f"\n\n……（原文过长，此处省略后续 {_orig_len - _IN_BUDGET} 字；"
                                 f"**修订时不得删减任何已给出章节**）")
            else:
                old_content = _raw_body
            try:
                triggers = json.loads(skill.get("triggers") or "[]")
            except Exception:
                triggers = []
            # 输出下限提示：不得少于原文 85%（防"浓缩成提纲"）
            _min_len = max(60, int(_orig_len * 0.85))
            prompt = (
                "你是技能改进工程师。基于技能的使用反馈做**最小必要修订**，输出 JSON：\n"
                '{"content": "修订后的技能指令正文", "triggers": ["触发词"], '
                '"description": "一句话描述", "changelog": "本次修订要点"}\n\n'
                f"技能名：{skill.get('name')}\n"
                f"当前触发词：{json.dumps(triggers[:10], ensure_ascii=False)}\n"
                f"当前描述：{skill.get('description', '')[:200]}\n\n"
                f"使用反馈（失败备注）：\n{notes_txt}\n\n"
                f"技能原文（共 {_orig_len} 字）：\n{old_content}\n\n"
                "★★★ 修订铁律（违反即视为修订失败）：\n"
                "1. **保留原文全部章节、步骤与约束**——只修改与失败反馈直接相关的部分，"
                "其余原样保留。宁可少改，不可删减。\n"
                "2. **严禁删减安全护栏**：凡涉及「必须先确认」「不得虚构」「先校验再导入」"
                "「删除前看影响面」「无回执不得声称完成」等约束，一律原样保留。\n"
                "3. **禁止压缩成提纲**：修订后正文**不得少于原文 85%**"
                f"（即不少于约 {_min_len} 字；原文 {_orig_len} 字）。"
                "若反馈只涉及某一步，就只改那一步。\n"
                "4. 保持 MBSE 领域适配；禁止虚构原文没有的能力；只输出 JSON。"
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
            new_body = str(data["content"])
            # ★ 2026-10-29：输出不再硬截 2000 字（原实现会把长手册的修订内容砍掉一半）。
            #   上限按"原文 1.5 倍 + 2000 字"给足余量 —— 允许合理增补（补护栏、补边界），
            #   但不允许无限膨胀。下限由下游的自检闸门校验（见 _verify_revision）。
            _cap = max(2000, int(_orig_len * 1.5) + 2000)
            new_body = new_body[:_cap]
            new_triggers = [str(t) for t in (data.get("triggers") or [])[:10]]
            new_desc = str(data.get("description") or skill.get("description", ""))[:200]
            # ★★ 自检闸门（2026-10-29）：不合格就**不产出草稿**。
            #   为什么放在写库之前而不是写库之后标记：坏草稿一旦进了审核队列，
            #   就存在被误采纳的风险（实测 3 条里 1 条不合格 = 33% 污染率），
            #   而采纳一份丢掉安全护栏的草稿会直接拆掉线上防线。
            #   失败原因写入 feedback_notes 尾部与返回值，保证"拒绝"是可见的
            #   （静默不产出会让人误以为"没有需要修订的技能"）。
            _ok, _why = cls._verify_revision(_raw_body, new_body)
            if not _ok:
                try:
                    conn.execute(
                        "INSERT INTO skill_revision_rejects (name, reason, old_len, new_len, created_at) "
                        "VALUES (?,?,?,?,datetime('now','localtime'))",
                        (skill.get("name") or "", _why, _orig_len, len(new_body)))
                    conn.commit()
                except Exception:
                    pass        # 台账表缺失不阻断（原因已通过返回值上报）
                return {"name": skill.get("name"), "rejected": True, "reason": _why}
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
        """周期入口：扫描全部候选并逐修订（限量防批量刷 LLM）。

        返回 `{proposed, names, rejected, reject_reasons, skipped}`。

        ⚠️ 2026-10-29：`improve_skill` 现在可能返回 **拒绝结构**
          `{"rejected": True, "reason": ...}`（自检闸门未通过），
          它**没有 `version` 字段**。原实现直接 `r['version']` 会抛 KeyError，
          而外层 try 把它吞掉 ⇒ 表现为"这条技能被静默跳过"，
          看板上既不算成功也不算失败 —— 正是"静默失败看起来像没问题"的形态。
          现显式分类：proposed（成功）/ rejected（自检拒绝，附原因）。
        """
        if not cls._enabled():
            return {"proposed": 0, "names": [], "rejected": 0,
                    "reject_reasons": [], "skipped": True}
        cands = cls.candidates(conn)[:max(int(limit), 1)]
        names, reject_reasons = [], []
        for sk in cands:
            r = cls.improve_skill(conn, sk, provider_id)
            if not r:
                continue
            if r.get("rejected"):
                reject_reasons.append(f"{r.get('name')}: {r.get('reason')}")
                continue
            if r.get("version"):
                names.append(f"{r['name']}@{r['version']}")
        return {"proposed": len(names), "names": names,
                "rejected": len(reject_reasons), "reject_reasons": reject_reasons,
                "skipped": False}
