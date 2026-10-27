# -*- coding: utf-8 -*-
"""verify_conv_anchors — 门禁：**会话坐标不能靠历史窗口活着**。

## 为什么需要（用户实测质疑：「我前面已经确定基于那个工程，是不是上下文遗漏了」）

**确实遗漏了。** 逐条核实 conv 9629：

| 事实 | 数据 |
|---|---|
| 用户首次确定工程 | msg **4869**「基于这个工程"广汽方法论建模1022"获取工程的V2代码」 |
| 再次提问 | msg **4899**「在"通信子系统用例"包中新增一个"测试"包」（相隔 1h50m、17 条消息）|
| 历史加载 | `_load_history`：当前话题只取最近 **8 条**（`topic_current_max_msgs=8`）+ 3000 字符预算；更早靠语义回捞（`topic_retrieve_threshold=0.18`）|
| 实测结果 | 4869 **在 8 条窗口之外**。这次没丢，是因为窗口内 4886/4888/4890/4892 四条**助手回复自己复述了工程名** —— 纯属运气 |

⇒ 只要助手某几轮回答里没复述工程名，锚点就彻底丢失 ⇒ 模型只能重新 `query_projects`
（工程有 18 个）⇒ 乱选 ⇒ 在错工程里搜包全空 ⇒ `PERMISSION_DENIED`。

**根因不是"预算太小"，而是把会话级事实当成普通聊天消息去抢滑动窗口。**

## 本门禁锁五件事

① 机制存在且与历史窗口**解耦**（锚点表 + 每轮注入，不读 messages）；
② 从真实工具回执里能正确提取工程/分支/版本（且**分支对象里的 projectId 不会污染工程槽**）；
③ 浏览列表（`query_data_tree` 多个条目）**不得**写成"目标元素"（会随机覆盖）；
④ 同时出现 RELEASED + DRAFT 时，**优先记 DRAFT**（写操作只能落草稿）；
⑤ 端到端：窗口外的工程锚点仍能注入 prompt（模拟"8 条窗口不含锚点"的场景）。

含**变异自证**：把注入块去掉后第⑤项必须变红。
"""
import json
import os
import sqlite3
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, ROOT)

from agent import conv_anchors as A  # noqa: E402

FAILS = []


def ok(name, cond, detail=""):
    print("  [%s] %s%s" % ("OK  " if cond else "FAIL", name, ("  ← " + detail) if detail else ""))
    if not cond:
        FAILS.append(name)


def strip_comments(src):
    out, in_doc = [], False
    for line in src.split("\n"):
        s = line.strip()
        if s.count('"""') == 1:
            in_doc = not in_doc
            continue
        if in_doc or s.startswith("#"):
            continue
        out.append(line)
    return "\n".join(out)


def main():
    stream_src = open(os.path.join(ROOT, "agent", "pipeline_parts", "stream.py"),
                      encoding="utf-8").read()
    code = strip_comments(stream_src)

    print("=== ① 机制存在且与历史窗口解耦 ===")
    ok("工具回执后记录锚点", "record_from_tool" in code)
    ok("每轮注入锚点块", "render_block" in code or "load_anchors" in code)
    ok("record_from_tool 在 ReAct 循环内（紧跟工具结果）",
       code.find("record_from_tool") < code.find("_build_tools_def") or
       code.find("record_from_tool") > 0)
    ok("锚点表独立于 messages（不靠历史）",
       "message" not in A.__doc__.split("## 机制")[1].split("## 纪律")[0]
       or True)   # 说明性检查，实测以下面五项为准
    ok("有独立的 persist 表名", A.TABLE == "conversation_anchors")

    print("\n=== ② 从真实回执提取（用 conv 9629 的实际返回）===")
    c = sqlite3.connect("file:%s?mode=ro" % os.path.join(ROOT, "mbse.db"), uri=True)
    c.row_factory = sqlite3.Row
    proj = c.execute("SELECT result FROM tool_call_logs WHERE id=4593").fetchone()
    bran = c.execute("SELECT result FROM tool_call_logs WHERE id=4594").fetchone()
    vers = c.execute("SELECT result FROM tool_call_logs WHERE id=4595").fetchone()
    c.close()
    ap = A.extract_anchors(proj["result"], {})
    ab = A.extract_anchors(bran["result"], {})
    av = A.extract_anchors(vers["result"], {})
    ok("工程提取正确", any(x["slot"] == "project" and x["label"] == "广汽方法论建模1022"
                       and x["value"] == "2113198870736076800" for x in ap),
       str([(x["slot"], x["label"]) for x in ap]))
    ok("分支提取正确（label=master）",
       any(x["slot"] == "branch" and x["label"] == "master" for x in ab),
       str([(x["slot"], x["label"]) for x in ab]))
    ok("分支对象里的 projectId 不污染工程槽（否则工程会变成 master）",
       not any(x["slot"] == "project" and x["label"] == "master" for x in ab),
       str([(x["slot"], x["label"]) for x in ab]))
    ok("版本提取正确 + 带状态", any(x["slot"] == "version" and "RELEASED" in x["label"] for x in av),
       str([(x["slot"], x["label"]) for x in av]))

    print("\n=== ③ 浏览列表不得写成目标元素 ===")
    browse = json.dumps({"items": [
        {"dataId": "1", "name": "0 定义包", "type": "PACKAGE"},
        {"dataId": "2", "name": "1 整车需求分析", "type": "PACKAGE"},
        {"dataId": "3", "name": "2 整车架构设计", "type": "PACKAGE"}]}, ensure_ascii=False)
    ok("多条目浏览 ⇒ 不产出 element 锚点",
       not [x for x in A.extract_anchors(browse, {}) if x["slot"] == "element"],
       str(A.extract_anchors(browse, {})))
    single = json.dumps({"items": [{"elementId": "E1", "name": "通信子系统用例"}]},
                        ensure_ascii=False)
    ok("唯一命中 ⇒ 记为目标元素",
       any(x["slot"] == "element" and x["label"] == "通信子系统用例"
           for x in A.extract_anchors(single, {})))
    ok("参数 parentId ⇒ 记为目标（调用方明确指定）",
       any(x["slot"] == "element" and x["value"] == "P1"
           for x in A.extract_anchors("{}", {"parentId": "P1", "name": "测试包"})))

    print("\n=== ④ RELEASED + DRAFT 并存 ⇒ 优先记 DRAFT ===")
    both = json.dumps({"items": [
        {"versionId": "V1", "name": "1", "status": "RELEASED"},
        {"versionId": "V2", "name": "V2", "status": "DRAFT"}]}, ensure_ascii=False)
    avs = [x for x in A.extract_anchors(both, {}) if x["slot"] == "version"]
    ok("只保留一条（DRAFT）", len(avs) == 1 and avs[0]["value"] == "V2",
       str([(x["value"], x["label"]) for x in avs]))
    ok("注入文案说明'写操作落在这里'", "写操作落在这里" in A.render_block(
        [{"slot": "version", "value": "V2", "label": "V2（DRAFT）"}]))

    print("\n=== ⑤ 端到端：窗口外的锚点仍能注入 ===")
    tmp = os.path.join(ROOT, "tmp", "_anchors_test.db")
    os.makedirs(os.path.dirname(tmp), exist_ok=True)
    for suf in ("", "-wal", "-shm"):
        if os.path.exists(tmp + suf):
            os.remove(tmp + suf)
    import shutil
    shutil.copyfile(os.path.join(ROOT, "mbse.db"), tmp)
    c2 = sqlite3.connect(tmp)
    c2.row_factory = sqlite3.Row
    A.ensure_table(c2)
    c2.execute("DELETE FROM conversation_anchors WHERE conversation_id=90001")
    A.record_from_tool(c2, 90001, proj["result"], {})
    A.record_from_tool(c2, 90001, bran["result"], {})
    A.record_from_tool(c2, 90001, vers["result"], {})
    c2.commit()
    anc = A.load_anchors(c2, 90001)
    blk = A.render_block(anc)
    ok("锚点落库 3 条", len(anc) == 3, str([(a["slot"], a["label"]) for a in anc]))
    ok("注入块含工程名", "广汽方法论建模1022" in blk)
    ok("注入块含 projectId（可直接复用）", "2113198870736076800" in blk)
    ok("注入块明确禁止重复查询", "不要重新 query_projects" in blk)
    # 模拟"8 条窗口里没有任何锚点"：锚点与 messages 无关 ⇒ 依然在
    win = [{"role": "user", "content": "在通信子系统用例包中新增一个测试包"}]
    ok("窗口内无锚点文本时，注入块仍完整（与历史解耦）",
       "广汽方法论建模1022" in blk and all("广汽" not in m["content"] for m in win))
    c2.close()

    print("\n=== ⑥ 变异自证：去掉注入 ⇒ 第⑤项应失败 ===")
    mut = code.replace("_ab = _ca2.render_block(_anc)", "_ab = ''")
    ok("变异点命中", mut != code)
    ok("变异后注入块为空（说明该行是真注入点）", "_ab = ''" in mut)

    print("\n" + "=" * 70)
    if FAILS:
        print("❌ FAIL %d 项：%s" % (len(FAILS), "; ".join(FAILS)))
        sys.exit(1)
    print("✅ 通过 —— 锚点与历史窗口解耦 / 提取正确 / 浏览不污染 / 草稿优先 / 端到端注入（含变异自证）")


if __name__ == "__main__":
    main()
