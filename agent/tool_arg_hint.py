"""远端参数错误的「可执行化」与「学到就用」（S2 · 2026-10-29）。

## 为什么需要（真库实证，@2026-10-29）

近 7 天真实参数错误 **46 条**，其中 `field=value / constraint=KnownField` 占 **18 条**；
更关键的是 **8/12 个会话在重复犯**（conv 9629 犯 11 次、conv 9736 犯 9 次）。

远端（智源平台）返回的报文其实**信息量很足**：

```json
{"code":"INVALID_ARGUMENTS","message":"参数不符合工具定义；按 fields 提示修正字段后重新调用",
 "retryable":false,
 "fields":[{"expected":"keyword, pageNum, pageSize, parentId, type, version",
            "message":"此字段未在工具中声明，请删除或改用 expected 中的字段",
            "constraint":"KnownField","field":"value"}],
 "recovery":{"action":"CORRECT_ARGUMENTS","instructions":"按 fields 中的字段路径、约束和预期值修正参数…"}}
```

**问题不在信息缺失，而在它没被模型采纳**：
- 原文是嵌套 JSON + 英文字段名，模型要在长结果里自己找、自己翻译；
- 平台本期只把它当普通 `result` 文本追加，且现有障碍恢复提示**追加在末尾**
  （`_etxt + _hint`）——实证就是末尾没人读（同会话连犯 11 次）。

⇒ 本模块做两件事：
1. **提纯**：把结构化 `fields[]` 转成**中文、祈使句、点名必删字段 + 唯一可用字段清单**，
   并放在结果**最前**（先读到的位置）。
2. **学到就用**：把「该工具的真实可用字段」落库，下次调用前注入工具描述，
   让模型**第一次就传对**（治本，比事后纠正省一整轮）。

## 纪律

- **只转述远端断言，不自己发明字段规则**。`expected` 原样保留（它来自服务端契约）。
- 解析失败/结构不符时**原样放行**，绝不因提示失败影响错误信息本身的可读性。
- 落库失败不影响主链路（观测/学习都是旁路）。
"""
from __future__ import annotations

import json
import re

#: 能识别为"参数类错误"的远端 code（出现即值得提纯）
ARG_ERROR_CODES = frozenset({
    "INVALID_ARGUMENTS",
    "MISSING_ARGUMENTS",
    "UNKNOWN_FIELD",
    "SCHEMA_VIOLATION",
})

#: 每条 hint 的文本上限（防止异常报文把上下文吃满）
_MAX_HINT = 1200


def _as_dict(text):
    """尽最大努力把 result 文本解析成 dict；失败返回 None。"""
    if isinstance(text, dict):
        return text
    s = str(text or "").strip()
    if not s.startswith("{"):
        return None
    try:
        d = json.loads(s)
        return d if isinstance(d, dict) else None
    except Exception:
        return None


def parse_arg_error(text):
    """从工具结果文本里提取**远端参数错误的结构化信息**。

    返回 dict（无法识别时返回 None）::

        {"code": "INVALID_ARGUMENTS",
         "fields": [{"field": "value", "expected": "keyword, pageNum, ...",
                     "constraint": "KnownField", "message": "..."}],
         "recovery": {"action": "...", "instructions": "..."},
         "allowed": ["keyword", "pageNum", ...]}   # 由 expected 拆出的字段名

    ⚠️ 只认 `fields[]` 里**有 field 名**的条目：没有 field 名的提示无法定位，
    强行展示反而制造噪音。
    """
    d = _as_dict(text)
    if not d:
        return None
    code = str(d.get("code") or "").strip().upper()
    raw_fields = d.get("fields")
    if code not in ARG_ERROR_CODES and not isinstance(raw_fields, list):
        return None
    fields, allowed = [], []
    for f in (raw_fields or []):
        if not isinstance(f, dict):
            continue
        name = str(f.get("field") or "").strip()
        if not name:
            continue
        exp = str(f.get("expected") or "").strip()
        fields.append({
            "field": name,
            "expected": exp,
            "constraint": str(f.get("constraint") or "").strip(),
            "message": str(f.get("message") or "").strip(),
        })
        # expected 可能是 "keyword, pageNum, pageSize" 或 "string" 之类的类型名。
        # 只把**像字段名**的（逗号分隔、标识符形态）当可用字段清单，避免把类型当字段。
        for tok in re.split(r"[,，\s]+", exp):
            tok = tok.strip()
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]{0,40}", tok or ""):
                allowed.append(tok)
    if not fields:
        return None
    rec = d.get("recovery") if isinstance(d.get("recovery"), dict) else {}
    # 去重保序
    seen, uniq = set(), []
    for a in allowed:
        if a not in seen:
            seen.add(a)
            uniq.append(a)
    return {
        "code": code or "INVALID_ARGUMENTS",
        "fields": fields,
        "recovery": {"action": str(rec.get("action") or "").strip(),
                     "instructions": str(rec.get("instructions") or "").strip()},
        "allowed": uniq,
    }


def format_hint_zh(tool_name, hint):
    """把结构化 hint 转成**模型能直接照做**的中文祈使句（约 120~300 字符）。

    为什么是祈使句 + 放最前：
        实证（conv 9629 同会话连犯 11 次）表明，把提示**追加在长结果末尾**基本无效。
        模型需要的是"下一步做什么"的短指令，而不是再读一遍结构化数据。
    """
    if not hint:
        return ""
    lines = ["【参数错误·必须按此修正】工具 `%s` 拒绝了本次入参。" % (tool_name or "")]
    unknown = [f for f in hint["fields"] if f["constraint"] == "KnownField"]
    other = [f for f in hint["fields"] if f["constraint"] != "KnownField"]
    if unknown:
        names = "、".join("`%s`" % f["field"] for f in unknown)
        lines.append("· 这些字段**不存在**，必须删除：%s" % names)
    if other:
        for f in other[:4]:
            c = f["constraint"] or "约束不符"
            lines.append("· 字段 `%s` 不符合 `%s` 约束（%s）" % (f["field"], c, f["message"][:60]))
    if hint["allowed"]:
        # 只列前 12 个：完整清单过长会把上下文吃满，且模型实际只用得到几个
        shown = hint["allowed"][:12]
        more = "" if len(hint["allowed"]) <= 12 else " …（共 %d 个）" % len(hint["allowed"])
        lines.append("· 该工具**只接受**这些字段：%s%s" % ("、".join("`%s`" % a for a in shown), more))
    ins = (hint.get("recovery") or {}).get("instructions") or ""
    if ins:
        lines.append("· 服务端要求：%s" % ins[:160])
    lines.append("⇒ 请**删除上述字段**后按可用字段重试；**不要**再传未声明的字段名。")
    out = "\n".join(lines)
    return out[:_MAX_HINT]


def lead_with_hint(original_text, hint_text):
    """把提示放在原文**最前**（不是末尾）。

    ⚠️ 保留原文完整：模型的其它判断（如读取业务字段）仍依赖它，
    只做"前置一条强指令"，不改写、不删减原文。
    """
    if not hint_text:
        return original_text
    return hint_text + "\n\n--- 服务端原始返回（供核对）---\n" + str(original_text or "")


# ── 学到就用：把「该工具的真实可用字段」记下来，下次注入 ────────────────────
def _conn():
    from database import db_conn
    return db_conn()


def ensure_table(conn) -> None:
    """建学习表（幂等）。字段刻意最小：只要"这个工具认哪些字段"这一条事实。"""
    conn.execute("""CREATE TABLE IF NOT EXISTS tool_arg_hints (
        tool_name TEXT PRIMARY KEY,
        allowed_fields TEXT DEFAULT '[]',
        last_bad_field TEXT DEFAULT '',
        source_code TEXT DEFAULT '',
        hit_count INTEGER DEFAULT 0,
        updated_at TEXT DEFAULT CURRENT_TIMESTAMP
    )""")


def learn(tool_name, hint) -> int:
    """记录/更新该工具的参数契约线索。返回 1=成功，0=跳过/失败。

    ⚠️ 只记**远端明确断言**的 allowed 清单；解析不出 allowed 就不写 ——
    宁可没有提示，也不要给模型一份猜出来的字段表（那会制造新的错误）。
    """
    if not tool_name or not hint or not hint.get("allowed"):
        return 0
    try:
        with _conn() as conn:
            ensure_table(conn)
            allowed = json.dumps(hint["allowed"][:40], ensure_ascii=False)
            bad = ""
            for f in hint.get("fields") or []:
                if f.get("constraint") == "KnownField":
                    bad = f.get("field") or ""
                    break
            conn.execute(
                "INSERT INTO tool_arg_hints (tool_name, allowed_fields, last_bad_field,"
                " source_code, hit_count, updated_at)"
                " VALUES (?,?,?,?,1,CURRENT_TIMESTAMP)"
                " ON CONFLICT(tool_name) DO UPDATE SET"
                "   allowed_fields=excluded.allowed_fields,"
                "   last_bad_field=excluded.last_bad_field,"
                "   source_code=excluded.source_code,"
                "   hit_count=tool_arg_hints.hit_count+1,"
                "   updated_at=CURRENT_TIMESTAMP",
                (str(tool_name), allowed, bad, hint.get("code", "")))
        return 1
    except Exception:
        return 0


def recall(tool_name) -> dict:
    """取回该工具学到的字段清单（没学过返回 {}）。"""
    if not tool_name:
        return {}
    try:
        with _conn() as conn:
            ensure_table(conn)
            row = conn.execute(
                "SELECT allowed_fields, last_bad_field, hit_count FROM tool_arg_hints"
                " WHERE tool_name=?", (str(tool_name),)).fetchone()
        if not row:
            return {}
        allowed = json.loads(row[0] or "[]") if isinstance(row[0], str) else (row[0] or [])
        return {"allowed": allowed or [],
                "last_bad_field": row[1] or "",
                "hit_count": int(row[2] or 0)}
    except Exception:
        return {}


def describe_suffix(tool_name) -> str:
    """给工具描述追加一句「历史上被拒过的字段」提示（供工具定义注入）。

    实测价值：`value` 这个错字段在 8 个会话里各犯 1~11 次，
    只要第一次失败后就把它写进工具描述，后续同工具调用即可避免。
    """
    h = recall(tool_name)
    if not h or not h.get("allowed"):
        return ""
    bad = h.get("last_bad_field") or ""
    shown = h["allowed"][:10]
    s = "\n【参数约束·来自历史校验】只接受：%s" % "、".join(shown)
    if len(h["allowed"]) > 10:
        s += " 等 %d 个" % len(h["allowed"])
    if bad:
        s += "；**不要**使用 `%s`（曾被服务端拒绝）" % bad
    return s
