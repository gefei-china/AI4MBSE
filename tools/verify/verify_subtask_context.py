#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""verify_subtask_context.py —— 主↔子上下文传递三项改进的门禁（2026-11-02）。

对应米爸的三个改进点（调研依据见 `agent/context_compress.py` 模块头）：
  ① 附件默认全量透传 → 按需注入（auto/manifest/all + 显式白名单为权威）
  ② 上游摘要硬截 400 字 → 围栏安全头尾采样 + 协议结论优先 + 取全文可见性
  ③ 协议 JSON 靠模型自觉 → 容错抽取 + 一次性补格式 + protocol_source 可证伪

三层断言：
  A 算法层：smart_excerpt / plan_subtask_attachments / extract_protocol_json
  B 挂钩层：两条执行路径（stream 流式、planner 非流式）都已接线且同源
  C 自证层：**变异自证** —— 故意用"盲切"实现替代 smart_excerpt，确认断言能抓住

运行：python tools/verify/verify_subtask_context.py
"""
import pathlib
import re
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from agent.context_compress import (  # noqa: E402
    plan_subtask_attachments, smart_excerpt)
from services.subtask_protocol import (  # noqa: E402
    build_repair_prompt, extract_protocol_json, validate)
from workflows.planner import build_subtask_context  # noqa: E402

_R = []


def ck(name, ok, detail=""):
    _R.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"  | {detail}" if detail else ""))


CODE = ("package 'X' {\n"
        + "\n".join("\tpart def 'P%d';" % i for i in range(50)) + "\n}")
PROSE = "## 分析\n" + ("过程说明。" * 260) + "\n\n## 结论\n最终结论：根因是定义/实例双轨。\n"
DOC = PROSE + "\n```sysml\n" + CODE + "\n```\n\n## 尾注\n"


def _n_fences(t):
    return str(t).count("```")


def blind_cut(text, cap):
    """变异体：按字符硬切（= 原实现的缺陷形态），用于自证断言有效。"""
    s = str(text or "")
    return (s if len(s) <= cap else s[:cap]), max(0, len(s) - cap)


def main():
    print("=" * 92)
    print("主↔子上下文传递三项改进门禁")
    print("=" * 92)

    # ── A1 smart_excerpt ──
    ck("A1a 未超预算 ⇒ 逐字节原样（零回归面）", smart_excerpt("abc", 100) == ("abc", 0))
    _e, _o = smart_excerpt(DOC, 0)
    ck("A1b cap<=0 ⇒ 不限长（原样返回）", _e == DOC and _o == 0)

    bad = 0
    for cap in (200, 400, 800, 1200, 2000, 2600, 5000):
        ex, _ = smart_excerpt(DOC, cap)
        if _n_fences(ex) % 2 != 0:
            bad += 1
    ck("A1c 任何预算下**围栏都不成半份**（原实现切进 ```sysml 的核心缺陷）",
       bad == 0, f"unbalanced={bad}")

    ex5, _ = smart_excerpt(DOC, 500)
    ck("A1d 尾部采样保住「## 结论」段（只截头会丢结论）", "最终结论" in ex5)

    ex2, om2 = smart_excerpt(DOC, 900)
    ck("A1e 截断处给出**可见**的省略量提示（不静默丢内容）",
       ("省略" in ex2) and om2 > 0, f"omitted={om2}")

    # 变异自证：盲切实现必被 A1c/A1d 抓住
    bx, _ = blind_cut(DOC, 700)
    mut_fence_ok = _n_fences(bx) % 2 == 0
    mut_tail_ok = "最终结论" in bx
    ck("A1f 变异自证：盲切体被抓（围栏半份 或 丢结论）",
       (not mut_fence_ok) or (not mut_tail_ok),
       f"盲切后围栏完整={mut_fence_ok} 含结论={mut_tail_ok}")

    # ── A2 附件按需注入 ──
    big = {"filename": "需求规格书.pdf", "size": 12_000_000, "doc_id": 847, "parse_status": "parsed"}
    small = {"filename": "备注.txt", "size": 1200}
    img = {"filename": "截图.png", "size": 300_000, "is_image": True}

    k1, m1 = plan_subtask_attachments([big])
    ck("A2a 大文档默认**不内联**，改给清单（含 doc_id）",
       k1 == [] and "doc_id=847" in m1)
    ck("A2b 清单带**取回指引**（read_attachment_section）并禁止臆造",
       "read_attachment_section" in m1 and "不要凭空假设" in m1)
    k2, m2 = plan_subtask_attachments([small])
    ck("A2c 小文本仍内联（不因保守而退化常识场景）", k2 == [small] and m2 == "")
    k3, _ = plan_subtask_attachments([img])
    ck("A2d 图片直传（视觉通道无法按需读取）", k3 == [img])
    k4, m4 = plan_subtask_attachments([big, img])
    ck("A2e 混合：图片传、大文档进清单", k4 == [img] and "需求规格书.pdf" in m4)
    k5, m5 = plan_subtask_attachments([big, small], declared=["备注.txt"])
    ck("A2f 显式白名单**权威**（只传匹配项，其余进清单）",
       k5 == [small] and "需求规格书.pdf" in m5)
    k6, m6 = plan_subtask_attachments([big, img], mode="all")
    ck("A2g mode=all 保留旧行为（可回退）", k6 == [big, img] and m6 == "")
    k7, m7 = plan_subtask_attachments([big, img], mode="manifest")
    ck("A2h mode=manifest 全部只给清单", k7 == [] and "截图.png" in m7)
    k8, _ = plan_subtask_attachments([{"filename": "x.docx", "doc_id": 9}])
    ck("A2i 体积未知 ⇒ 保守按需（不无差别灌入）", k8 == [])

    # ── A3 上游摘要（build_subtask_context） ──
    sm = {"status": "partial", "summary": "完成用例视图修正",
          "claims": [{"kind": "fact", "subject": "根因", "text": "定义/实例双轨"},
                     {"kind": "to_verify", "subject": "接口", "text": "字段数未查证"}],
          "risks": [{"level": "high", "title": "元模型路径未核实"}],
          "artifacts": [{"kind": "model", "title": "运行场景定义"}],
          "missing": ["未执行校验"]}
    item = {"task_key": "t1", "title": "问题分析", "result": DOC, "summary": sm}
    tk = {"task_key": "t2", "title": "设计", "deps": ["t1"], "config": {}}
    ctx = build_subtask_context(tk, "目标", done_items=[item],
                               offload_map={"t1": (4321, len(DOC))})
    ck("A3a 协议**结论**进快照（claims/risks/artifacts/missing）",
       all(x in ctx for x in ("关键结论", "根因", "风险", "产物", "自报缺口")))
    ck("A3b offload 全文引用可达（tool_result_fetch）", "tool_result_fetch" in ctx)
    ck("A3c 快照里的代码块不成半份", _n_fences(ctx) % 2 == 0, f"fences={_n_fences(ctx)}")
    _src_planner = (ROOT / "workflows" / "planner.py").read_text(encoding="utf-8")
    ck("A3d 旧硬编码 `[:400]` 已清除（不再切在代码块/结论之前）",
       "str(it.get('result'))[:400]" not in _src_planner
       and 'str(it.get("result"))[:400]' not in _src_planner)

    # ── A4 协议容错抽取 + 补格式 ──
    cases = {
        "纯JSON": ('{"status":"full","summary":"ok"}', lambda d: d["status"] == "full"),
        "正文+尾部JSON": ('正文\n{"status":"partial","missing":["x"]}',
                       lambda d: d["missing"] == ["x"]),
        "围栏JSON": ('正文\n```json\n{"status":"full","artifacts":[]}\n```',
                   lambda d: d["artifacts"] == []),
        "尾随逗号": ('正文\n{"status":"full","artifacts":[{"kind":"model","title":"M",},],}',
                  lambda d: d["artifacts"][0]["title"] == "M"),
        "被截断": ('正文\n{"status":"partial","risks":[{"level":"high","title":"R"}',
                 lambda d: d["risks"][0]["title"] == "R"),
        "正文含示例JSON": ('示例：{"a":1}\n结果\n{"schema_version":1,"status":"full",'
                      '"claims":[{"text":"x"}]}', lambda d: bool(d.get("claims"))),
        "围栏内含花括号": ('说明\n```json\n{"status":"full","summary":"含 } 说明"}\n```',
                     lambda d: "}" in d["summary"]),
    }
    for nm, (txt, pred) in cases.items():
        obj = extract_protocol_json(txt)
        ck(f"A4 抽取-{nm}", isinstance(obj, dict) and pred(obj), repr(obj)[:70])
    ck("A4 纯文本无 JSON ⇒ None（不猜、不伪造协议）",
       extract_protocol_json("纯文本没有协议") is None)

    _pr = validate(extract_protocol_json(cases["被截断"][0]) or {})
    ck("A4 修复后的对象能过 validate 拿到 sanitized",
       isinstance(_pr.get("sanitized"), dict), str(_pr.get("reason"))[:60])
    pr_prompt = build_repair_prompt(DOC, max_body=2000)
    ck("A4 补格式 prompt 自包含（字段清单 + 禁止编造 + 长度受控）",
       "claims" in pr_prompt and "不得补充" in pr_prompt and len(pr_prompt) < 4000,
       f"len={len(pr_prompt)}")

    # ── B 挂钩层 ──
    src_stream = (ROOT / "agent" / "pipeline_parts" / "stream.py").read_text(encoding="utf-8")
    src_planner = _src_planner
    ck("B1 流式路径接 plan_subtask_attachments（①）",
       "plan_subtask_attachments" in src_stream and "_att_manifest" in src_stream)
    ck("B2 非流式路径同样接线（两条路径同源，避免口径分裂）",
       "plan_subtask_attachments" in src_planner and "_sub_att" in src_planner)
    ck("B3 附件清单进入子任务 query（否则规划等于没做）",
       'query += "\\n\\n" + _att_manifest' in src_stream
       or "query += \"\\n\\n\" + _att_manifest" in src_stream)
    ck("B4 协议来源三态落库（parsed/repaired/missing 可证伪）",
       '"protocol_source"' in src_stream and "'missing'" in src_stream)
    ck("B5 补格式只在协议缺失时触发且有开关",
       "json_repair" in src_stream and "_proto_src = \"repaired\"" in src_stream)
    ck("B6 「未取回上游全文」进汇总可见（不静默假定信息完整）",
       "未取回上游完整交付物" in src_stream)

    # ── 配置契约 ──
    from core import config as _cfg
    ck("C1 delegation.attachments_mode 默认 auto",
       str(_cfg.get("delegation", "attachments_mode", "")) == "auto")
    ck("C2 delegation.upstream_digest_chars 默认 900（> 原 400）",
       int(_cfg.get("delegation", "upstream_digest_chars", 0) or 0) == 900)
    ck("C3 subtask_protocol.json_repair 默认开",
       bool(_cfg.get("subtask_protocol", "json_repair", False)) is True)
    src_cfg = (ROOT / "core" / "config.py").read_text(encoding="utf-8")
    ck("C4 三个新键都有 schema 描述（配置面板可见）",
       src_cfg.count('"attachments_mode"') >= 2
       and src_cfg.count('"upstream_digest_chars"') >= 2
       and src_cfg.count('"json_repair"') >= 2)

    # ── D 补格式的**实际可用性**（实测踩到的坑，必须固化） ──
    #   实测（deepseek-v4-flash，2026-11-02）：max_tokens=700 时 reasoning 吃掉全部
    #   预算 ⇒ finish_reason=length、content 为空 ⇒ 补格式"写了但永不生效"。
    #   两条防线都要守：① 该 intent 关思考；② 预算留足余量。
    ck("D1 补格式 intent 在 thinking_policy.off 名单（否则推理吃掉全部预算）",
       "subtask_protocol_repair" in str(_cfg.get("llm", "thinking_policy", "") or ""))
    _mt = re.search(r"_intent=\"subtask_protocol_repair\"", src_stream)
    _bud = re.search(r"max_tokens=(\d+),\s*\n?\s*_intent=\"subtask_protocol_repair\"",
                     src_stream)
    ck("D2 补格式 max_tokens 留余量（≥1500，实测 700 必失败）",
       bool(_bud) and int(_bud.group(1)) >= 1500,
       ("max_tokens=%s" % (_bud.group(1) if _bud else "?")))
    ck("D3 补格式真的被调用（不是只定义了 prompt）", bool(_mt))
    ck("D4 补格式失败原因可查（protocol_repair_error，区分「没试」与「试了没成」）",
       "protocol_repair_error" in src_stream)

    fails = [r for r in _R if not r[1]]
    print("=" * 92)
    print(f"断言汇总：{len(_R) - len(fails)}/{len(_R)} 通过")
    if fails:
        for nm, _, dt in fails:
            print("  ✗ " + nm + (f"  | {dt}" if dt else ""))
        return 1
    print("✅ ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
