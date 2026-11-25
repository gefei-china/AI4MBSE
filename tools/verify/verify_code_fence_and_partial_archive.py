# -*- coding: utf-8 -*-
"""围栏错位抢救 + 停止固化归档（2026-11-21，真库 conv 999999977 实证）。

## 米爸报的两个问题与本次排查发现

### 问题 1「需求视图、结构视图被放到同一个文件」
排查结论：一轮回复 = 一个版本 = 一个代码文件 是**既定机制**（米爸确认保留）；
真正的问题有二：
  ① 模型在编排汇总里写了**未闭合的 ```json**，后面 ```sysml 被旧正则当成
     json 的闭合围栏消费掉 ⇒ **整段真正的 SysML 代码落在两个被消费的围栏之间，
     连一个匹配都进不去** ⇒ 被 lang 过滤丢光 ⇒ msg 7205 该轮 3 段代码 5 个
     package，归档 0 条（零版本、零产物）。
  ② msg 7203（用例图轮）是**停止固化路径**写的（结尾带「> ⏹ 已停止生成」），
     该路径此前**不调 _archive_artifacts** ⇒ 14k 字用例视图代码在消息里、
     产物库/版本链里没有 ⇒ 下一轮"基于上一版修订"没有基线。

### 问题 2「代码文件里有大量文档性描述」
核验：文件里全部是合法 SysML 注释 —— doc /* */ 是 SysML v2 需求文档正式语法
（承载需求文本，保留）；banner/分节注释密度走提示词约束（另行处理，不在本门禁）。

## 判据
  A1 正常配对（```sysml…```）提取 lang=sysml
  A2 未闭合 ```json + 后续 3 段 ```sysml ⇒ 3 段全部找回（修复核心）
  A3 末尾未闭合的 sysml 块被抢救
  A4 裸 ``` 开块 → text（旧兼容）
  A5 与旧实现在**正常内容**上结果一致（回归保护）
  B1 行为级：fixture 经 _archive_artifacts 落临时全 schema 库
     ⇒ sysml_versions ≥1 且 artifacts(kind=code) 内容含三个 package
  B2 行为级：_persist_partial_stream 子进程探针（真实方法 + 临时库）
     ⇒ messages 1 条 + artifacts(kind=code) ≥1 + sysml_versions ≥1
  M1 反证：旧正则实现在同一 fixture 上 0 段 sysml ⇒ fixture 是真判别器
  M2 反证：B2 探针里把提取函数换回旧实现 ⇒ 产物/版本归 0 ⇒ 归档真的依赖修复
  S1/S2 弱断言：两条固化路径源码都含归档调用（执行期缺陷由 B2 抓）

用法：./.venv/Scripts/python.exe -X utf8 tools/verify/verify_code_fence_and_partial_archive.py
"""
from __future__ import annotations
import os as _svos
import sys as _svsys
_svroot = _svos.path.dirname(_svos.path.abspath(__file__))
while _svroot and not _svos.path.isdir(_svos.path.join(_svroot, 'tools')):
    _svroot = _svos.path.dirname(_svroot)
if _svroot and _svroot not in _svsys.path:
    _svsys.path.insert(0, _svroot)
from tools.stream_source import read_stream_source  # noqa: E402
from _tmpdir import mkdtemp  # 共享：创建即登记，atexit 退出时自动回收临时目录

import os
import sys

_HERE = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(os.path.dirname(_HERE))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

STREAM = os.path.join(_ROOT, "agent", "pipeline_parts", "stream.py")
ROUTER = os.path.join(_ROOT, "routers", "conversations.py")

F = "```"
# 与真库 msg 7205 同构：未闭合 ```json + 三段 ```sysml（每段各自闭合）
FIXTURE = (
    "## 活动图编排汇总\n\n"
    "（附：本轮已校验的 SysML v2 模型代码）\n\n"
    + F + "json\n\n"
    "## 在既有需求模型 v0.1 上结构化活动流相关需求\n"
    '{"t1": "细化活动流需求", "t2": "阈值待标定"}\n\n'
    "（附：本轮已校验的 SysML v2 模型代码）\n\n"
    + F + "sysml\n"
    "package VTMS_ActivityFlowRequirements {\n"
    "    requirement def RemoteACReq { doc /* 手机端远程降温 */ }\n"
    "}\n"
    + F + "\n\n"
    "## 第二段：骨架\n\n（附：本轮已校验的 SysML v2 模型代码）\n\n"
    + F + "sysml\n"
    "package EVTM_ActivitySkeleton {\n"
    "    action def CoolCabin;\n"
    "}\n"
    + F + "\n\n"
    "## 第三段：活动流\n\n（附：本轮已校验的 SysML v2 模型代码）\n\n"
    + F + "sysml\n"
    "package 'EVTM_ActivityFlow' {\n"
    "    action CoolCabinFlow;\n"
    "}\n"
    + F + "\n"
)

SYSML_LANGS = ("sysml", "kerml", "sysmlv2", "kerml2")

fails: list[str] = []
_n_pass = [0]


def chk(name, cond, detail=""):
    print(("  PASS  " if cond else "  FAIL  ") + name
          + (("  | " + str(detail)) if detail else ""))
    if cond:
        _n_pass[0] += 1
    else:
        fails.append(name)


def _old_extract_code_blocks(content, limit=5):
    """修复前的实现（逐字节照抄 git 历史），用于 M1/M2 反证。"""
    import re as _re
    blocks = []
    for m in _re.finditer(r"```([\w+-]*)\n(.*?)```", content or "", _re.S):
        lang = m.group(1).strip() or "text"
        code = m.group(2).strip()
        if code and len(code) >= 20:
            blocks.append({"lang": lang, "code": code})
        if len(blocks) >= limit:
            break
    return blocks


def _sysml_count(blocks):
    return sum(1 for b in blocks
               if str(b.get("lang", "")).lower() in SYSML_LANGS)


def main() -> int:
    print("=" * 74)
    print("围栏错位抢救 + 停止固化归档（conv 999999977 msg 7203/7205 实证）")
    print("=" * 74)

    from agent.utils import _extract_code_blocks

    print("\n[A] _extract_code_blocks 围栏状态机")
    # A1 正常配对（块长需 ≥20 字符，过短会被既有过滤线滤掉）
    a1 = _extract_code_blocks(
        F + "sysml\npackage P1 {\n    part def Heater;\n}\n" + F)
    chk("A1 正常配对提取 sysml", _sysml_count(a1) == 1, a1)

    # A2 未闭合 json + 三段 sysml（修复核心）
    a2 = _extract_code_blocks(FIXTURE)
    a2n = _sysml_count(a2)
    a2pk = "\n".join(b["code"] for b in a2 if b["lang"].lower() in SYSML_LANGS)
    chk("A2 三段 sysml 全部找回（期望 3）", a2n == 3, [(b["lang"], len(b["code"])) for b in a2])
    chk("A2 三段代码内容完整（三个 package 都在）",
        all(k in a2pk for k in ("VTMS_ActivityFlowRequirements",
                                "EVTM_ActivitySkeleton",
                                "EVTM_ActivityFlow")), "")

    # A3 末尾未闭合抢救
    a3 = _extract_code_blocks("说明\n" + F + "sysml\npackage TailP {\n    part x;\n}\n")
    chk("A3 末尾未闭合 sysml 块被抢救", _sysml_count(a3) == 1, a3)

    # A4 裸 ``` 开块 → text（旧兼容）
    a4 = _extract_code_blocks(F + "\nplain text block here long enough\n" + F)
    chk("A4 裸 ``` 开块 → text", bool(a4) and a4[0]["lang"] == "text", a4)

    # A5 与旧实现在正常内容上结果一致（块长 ≥20，避免空对比假绿）
    normal = ("前文\n" + F + "sysml\npackage Alpha {\n    part def Motor;\n}\n" + F
              + "\n中文\n" + F + "python\nresult = compute_pressure(v)\n" + F + "\n尾文\n")
    a5n, a5o = _extract_code_blocks(normal), _old_extract_code_blocks(normal)
    chk("A5 正常内容新旧实现结果一致（非空）",
        a5n and [(b["lang"], b["code"]) for b in a5n]
        == [(b["lang"], b["code"]) for b in a5o],
        f"新={a5n} 旧={a5o}")

    print("\n[M1] 变异反证：旧正则实现在同一 fixture 上")
    m1 = _old_extract_code_blocks(FIXTURE)
    chk("M1 旧实现 0 段 sysml（证明 fixture 是真判别器）", _sysml_count(m1) == 0,
        [(b["lang"], len(b["code"])) for b in m1])

    print("\n[B1] 行为级：fixture 经 _archive_artifacts 落临时全 schema 库")
    db1 = _make_full_db("fence_archive_b1")
    b1 = _run_archive_probe(db1, FIXTURE)
    chk("B1 sysml_versions ≥1", b1.get("versions", 0) >= 1, b1)
    chk("B1 artifacts(kind=code) ≥1", b1.get("code_artifacts", 0) >= 1, b1)
    chk("B1 产物内容含三个 package", b1.get("pkgs", 0) == 3, b1)

    print("\n[B2] 行为级：_persist_partial_stream（停止固化路径）真实探针")
    db2 = _make_full_db("fence_archive_b2")
    b2 = _run_freeze_probe(db2, FIXTURE, mutate=False)
    chk("B2 messages == 1", b2.get("messages", -1) == 1, b2)
    chk("B2 artifacts(kind=code) ≥1", b2.get("code_artifacts", 0) >= 1, b2)
    chk("B2 sysml_versions ≥1", b2.get("versions", 0) >= 1, b2)

    print("\n[M2] 反证：B2 探针换回旧提取实现 ⇒ 归档必须归 0")
    db3 = _make_full_db("fence_archive_m2")
    m2 = _run_freeze_probe(db3, FIXTURE, mutate=True)
    chk("M2 旧实现下 code 产物归 0", m2.get("code_artifacts", -1) == 0, m2)
    chk("M2 旧实现下版本归 0", m2.get("versions", -1) == 0, m2)

    print("\n[S] 弱断言：两条固化路径源码都含归档调用（执行期缺陷由 B2 抓）")
    ssrc = read_stream_source()
    with open(ROUTER, encoding="utf-8") as f:
        rsrc = f.read()
    chk("S1 stream._persist_partial_stream 含归档调用",
        "_archive_artifacts(conn, conversation_id, _cur.lastrowid" in ssrc, "")
    chk("S2 路由 /messages/partial 含归档调用",
        "_archive(conn, conv_id, mid" in rsrc, "")

    print("\n" + "=" * 74)
    print(f"结果：{_n_pass[0]} PASS / {len(fails)} FAIL")
    if fails:
        print("未通过：" + "、".join(fails))
    print("=" * 74)
    return 1 if fails else 0


def _make_full_db(tag: str) -> str:
    """建一套**完整 schema** 的临时库（复用 _tmpdb.ensure_schema，CI 全新 checkout 也能跑）。"""
    from _tmpdb import ensure_schema
    db = os.path.join(mkdtemp(prefix="fence_" + tag + "_"), "t.db")
    if not ensure_schema(db):
        raise RuntimeError(f"临时全 schema 库创建失败：{db}")
    return db


def _run_archive_probe(db: str, content: str) -> dict:
    """在临时库上直接调 _archive_artifacts，返回落库统计。"""
    import subprocess as _sp
    script = (
        "import sqlite3, json, sys\n"
        "sys.path.insert(0, r'" + _ROOT + "')\n"
        "import agent.utils as U\n"
        "conn = sqlite3.connect(r'" + db + "')\n"
        "conn.row_factory = sqlite3.Row\n"
        "conn.execute(\"INSERT OR IGNORE INTO conversations (id, title) VALUES (1, '活动图')\")\n"
        "conn.commit()\n"
        "U._archive_artifacts(conn, 1, 901, '{}', " + repr(content) + ", 'chat')\n"
        "print('VERSIONS', conn.execute('SELECT COUNT(*) FROM sysml_versions').fetchone()[0])\n"
        "print('CODE_ART', conn.execute(\"SELECT COUNT(*) FROM artifacts WHERE kind='code'\").fetchone()[0])\n"
        "pk = 0\n"
        "for (c,) in conn.execute(\"SELECT preview_content FROM artifacts WHERE kind='code'\"):\n"
        "    for k in ('VTMS_ActivityFlowRequirements', 'EVTM_ActivitySkeleton', 'EVTM_ActivityFlow'):\n"
        "        if k in (c or ''): pk += 1\n"
        "print('PKGS', pk)\n"
    )
    out = {}
    r = _sp.run([sys.executable, "-X", "utf8", "-c", script],
                capture_output=True, text=True, timeout=300, cwd=_ROOT)
    for ln in (r.stdout or "").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] in ("VERSIONS", "CODE_ART", "PKGS"):
            out["versions" if parts[0] == "VERSIONS" else
                "code_artifacts" if parts[0] == "CODE_ART" else "pkgs"] = int(parts[1])
    if r.returncode != 0 and not out:
        out["error"] = (r.stderr or "")[-400:]
    return out


def _run_freeze_probe(db: str, content: str, mutate: bool) -> dict:
    """子进程探针：真实 StreamMixin._persist_partial_stream + 注入临时库 db_conn。

    ⚠️ 注入点必须是 `agent.pipeline_parts.stream.db_conn`（该模块自己的全局绑定，
    改 common.db_conn 无效 —— verify_dedup_and_finalize_hint 已踩过这个坑）。
    mutate=True 时把 agent.utils._extract_code_blocks 换回旧实现（M2 反证）。
    """
    import subprocess as _sp
    conn_src = (
        "class _FC:\n"
        "    def __enter__(self):\n"
        "        self.c=sqlite3.connect(r'" + db + "'); self.c.row_factory=sqlite3.Row; return self.c\n"
        "    def __exit__(self,*a):\n"
        "        self.c.commit(); self.c.close(); return False\n"
        "    def execute(self,*a,**k): return self.c.execute(*a,**k)\n"
    )
    mut = ""
    if mutate:
        mut = (
            "import agent.utils as U\n"
            "import re as _re\n"
            "def _old(content, limit=5):\n"
            "    blocks=[]\n"
            "    for m in _re.finditer(r\"```([\\w+-]*)\\n(.*?)```\", content or '', _re.S):\n"
            "        lang=m.group(1).strip() or 'text'; code=m.group(2).strip()\n"
            "        if code and len(code)>=20: blocks.append({'lang':lang,'code':code})\n"
            "        if len(blocks)>=limit: break\n"
            "    return blocks\n"
            "U._extract_code_blocks = _old\n"
        )
    script = (
        "import sqlite3, sys\n"
        "sys.path.insert(0, r'" + _ROOT + "')\n"
        "import agent.pipeline_parts.stream as SM\n"
        "from agent.pipeline_parts.stream import StreamMixin\n"
        + conn_src +
        "SM.db_conn = lambda: _FC()\n"
        + mut +
        "m = StreamMixin()\n"
        "m._persist_partial_stream(1, " + repr(content) + ", 'stop', dry_run=False)\n"
        "c = sqlite3.connect(r'" + db + "')\n"
        "print('MSGS', c.execute('SELECT COUNT(*) FROM messages').fetchone()[0])\n"
        "print('CODE_ART', c.execute(\"SELECT COUNT(*) FROM artifacts WHERE kind='code'\").fetchone()[0])\n"
        "print('VERSIONS', c.execute('SELECT COUNT(*) FROM sysml_versions').fetchone()[0])\n"
    )
    out = {}
    r = _sp.run([sys.executable, "-X", "utf8", "-c", script],
                capture_output=True, text=True, timeout=300, cwd=_ROOT)
    for ln in (r.stdout or "").splitlines():
        parts = ln.split()
        if len(parts) == 2 and parts[0] in ("MSGS", "CODE_ART", "VERSIONS"):
            out[{"MSGS": "messages", "CODE_ART": "code_artifacts",
                 "VERSIONS": "versions"}[parts[0]]] = int(parts[1])
    if r.returncode != 0 and not out:
        out["error"] = (r.stderr or "")[-400:]
    return out


if __name__ == "__main__":
    sys.exit(main())
