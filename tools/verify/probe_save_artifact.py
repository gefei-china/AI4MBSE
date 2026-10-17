# -*- coding: utf-8 -*-
"""save_artifact 能力配置 验证（2026-10-17，米爸「工程代码写入产物」能力路线）。

背景
----
9446 实证：file_write 只写沙箱磁盘、进不了产物库；harness 兜底只认完整文件形态
（分段展示/片段不命中）⇒ AI 说「已存入产物文档」是空话，产物表为空。
⇒ 能力配置三件套：save_artifact 本地工具（显式归档通道）+ 技能 48「文件操作」扩充
  （allowed_tools/triggers/用法正文）+ agent 735 绑定与提示词完善。

判据
----
S1 save_artifact 单元：正常登记（meta.source=agent_save）/ 幂等 / kind 归一 /
  空参拒绝 / 缺会话上下文拒绝 / ``` 围栏剥离
S2 分发接线：_exec_tool_call save_artifact 分支（假 self 真跑 mixin 方法）
S3 配置落地：tools 表登记 / 技能 48 白名单+触发词 / 735 绑定 / 735 提示词 /
  写意图关键词「存入」/ 平台提示词 / seeds 种子
M1 变异自证：幂等判据失效 ⇒ S2 重复登记转红
"""
import io
import importlib
import json
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
PASSED, FAILED = [], []


def chk(name, ok, detail=""):
    (PASSED if ok else FAILED).append(name)
    print(("  [PASS] " if ok else "  [FAIL] ") + name + (("  -- " + str(detail)) if detail else ""))


def section(t):
    print("\n" + "=" * 4 + " " + t + " " + "=" * 4)


# ── S1. save_artifact 单元（monkeypatch repo/db）─────────
section("S1. save_artifact 单元")

import artifact_tools as _at   # noqa: E402
import repositories.artifact_repo as _armod   # noqa: E402
import database as _dbmod                     # noqa: E402

_ORIG_REPO, _ORIG_GDB = _armod.ArtifactRepo, _dbmod.get_db


class _FakeRepo:
    def __init__(self, conn=None):
        self.created = []
        self.exists_map = {}

    def exists(self, conv, mid, kind, title):
        return self.exists_map.get((conv, mid, kind, title), False)

    def create_artifact(self, conv, mid, kind, title, ptype, content, meta, **kw):
        self.created.append((conv, mid, kind, title, ptype, content, meta))
        return 100 + len(self.created)


class _FakeConn:
    def commit(self):
        pass

    def close(self):
        pass


_repo = _FakeRepo()
_armod.ArtifactRepo = lambda conn=None: _repo
_dbmod.get_db = lambda: _FakeConn()

_r = _at.exec_tool("save_artifact", {"title": "广汽元模型_master.sysml",
                                     "content": "package '广汽' { part p1; }",
                                     "kind": "code", "lang": "sysml"},
                   conversation_id=9446)
chk("S1a 正常登记：ok + 产物 id + meta.source=agent_save",
    _r.get("ok") and _repo.created and _repo.created[0][2] == "code"
    and _repo.created[0][6].get("source") == "agent_save"
    and _repo.created[0][4] == "code", (_r, _repo.created))

_repo.exists_map[(_repo.created[0][0], 0, "code", "广汽元模型_master.sysml")] = True
_r2 = _at.exec_tool("save_artifact", {"title": "广汽元模型_master.sysml",
                                      "content": "package X {}"}, conversation_id=9446)
chk("S1b 幂等：同会话同型同标题不重复登记",
    _r2.get("ok") and "已保存过" in str(_r2.get("result")) and len(_repo.created) == 1,
    (_r2, len(_repo.created)))

_r3 = _at.exec_tool("save_artifact", {"title": "t", "content": "abc", "kind": "word"},
                    conversation_id=1)
chk("S1c kind 越界归一为 document", _r3.get("ok") and _repo.created[-1][2] == "document", _repo.created[-1][2])

chk("S1d 空 title 拒绝", not _at.exec_tool("save_artifact", {"content": "x"}, conversation_id=1).get("ok"))
chk("S1e 空 content 拒绝", not _at.exec_tool("save_artifact", {"title": "x"}, conversation_id=1).get("ok"))
chk("S1f 缺会话上下文拒绝", not _at.exec_tool("save_artifact", {"title": "x", "content": "y"}).get("ok"))

_r6 = _at.exec_tool("save_artifact", {"title": "f.sysml",
                                      "content": "```sysml\npackage P {}\n```"},
                    conversation_id=1)
chk("S1g ``` 围栏剥离", _r6.get("ok") and _repo.created[-1][5] == "package P {}",
    repr(_repo.created[-1][5]))

chk("S1h 未知工具名拒绝", not _at.exec_tool("nope", {}, conversation_id=1).get("ok"))

# ── S2. 分发接线（假 self 真跑 ExecuteMixin._exec_tool_call）──
section("S2. _exec_tool_call 分发分支")

from agent.pipeline_parts.tools import ToolMixin   # noqa: E402

_calls = []


class _FakeSelf(ToolMixin):
    _tool_conv_ctx = 9446
    _tool_intent_ctx = {}
    _tool_agent_ctx = {}

    def _log_tool_call(self, name, ttype, args, result, *a):
        _calls.append((name, ttype))


_fs = _FakeSelf()
_res = _fs._exec_tool_call("save_artifact",
                           {"title": "分发测试.sysml", "content": "package D {}"})
chk("S2 save_artifact 经 _exec_tool_call 真分发并落库",
    _res.get("ok") and _calls and _calls[-1][0] == "save_artifact"
    and _repo.created[-1][3] == "分发测试.sysml", (_res, _calls[-1:]))

# ── S3. 配置落地（DB + 源码）────────────────────────────
section("S3. 工具/技能/绑定/提示词 配置落地")

import sqlite3   # noqa: E402
conn = sqlite3.connect(ROOT / "mbse.db")
conn.row_factory = sqlite3.Row

_t = conn.execute("SELECT side_effect, status, source FROM tools WHERE name='save_artifact'").fetchone()
chk("S3a tools 表已登记 save_artifact（builtin/write/active）",
    _t and _t["side_effect"] == "write" and _t["status"] == "active" and _t["source"] == "builtin", dict(_t) if _t else None)

_sk = conn.execute("SELECT allowed_tools, triggers FROM skills WHERE id=48").fetchone()
_at48 = json.loads(_sk["allowed_tools"] or "[]")
_tg48 = json.loads(_sk["triggers"] or "[]")
chk("S3b 技能48 白名单含 save_artifact", "save_artifact" in _at48, _at48)
chk("S3c 技能48 触发词覆盖「存入产物/写入产物/产物文档」",
    all(w in _tg48 for w in ("存入产物", "写入产物", "产物文档")), _tg48)

_bd = conn.execute("SELECT 1 FROM agent_tools WHERE agent_id=735 AND tool_type='skill' "
                   "AND tool_name='文件操作'").fetchone()
chk("S3d agent 735 已绑技能「文件操作」", _bd is not None)

_sp = conn.execute("SELECT system_prompt FROM agents WHERE id=735").fetchone()["system_prompt"] or ""
chk("S3e agent 735 提示词含 save_artifact 指引",
    "save_artifact" in _sp and "产物归档" in _sp)

_tp = io.open(ROOT / "agent" / "pipeline_parts" / "tools.py", encoding="utf-8").read()
chk("S3f 写意图关键词含「存入」+ 分发分支存在",
    '"存入"' in _tp and 'if name == "save_artifact"' in _tp)
chk("S3i 技能白名单补入段含 save_artifact（在白名单≠在候选，9543 实测断点）",
    'if "save_artifact" in _sk_wl and not any(c[0] == "save_artifact"' in _tp)

_cm = io.open(ROOT / "agent" / "pipeline_parts" / "common.py", encoding="utf-8").read()
chk("S3g 平台提示词指引调用 save_artifact",
    "ARTIFACT_AUTO_RULES_TEXT" in _cm and _cm.count("save_artifact") >= 1
    and "存入产物" in _cm)

_sd = io.open(ROOT / "database" / "seeds.py", encoding="utf-8").read()
chk("S3h seeds.py 已登记 save_artifact 种子", "save_artifact" in _sd and "artifact_tools" in _sd)
conn.close()

# ── M1. 变异自证：幂等判据失效 ⇒ 重复登记 ────────────────
section("M1. 变异自证（幂等判据必须能红）")

_at_path = ROOT / "artifact_tools.py"
_AT_ORIG = _at_path.read_text(encoding="utf-8")
try:
    _old = "if repo.exists(int(conversation_id), 0, kind, title):"
    assert _old in _AT_ORIG
    _at_path.write_text(_AT_ORIG.replace(_old, "if False:"), encoding="utf-8")
    importlib.reload(_at)
    _repo2 = _FakeRepo()
    _armod.ArtifactRepo = lambda conn=None: _repo2
    _dbmod.get_db = lambda: _FakeConn()
    _at.exec_tool("save_artifact", {"title": "m.sysml", "content": "package M {}"}, conversation_id=1)
    _at.exec_tool("save_artifact", {"title": "m.sysml", "content": "package M {}"}, conversation_id=1)
    chk("M1 幂等判据失效 ⇒ 同文重复登记转红", len(_repo2.created) == 2, len(_repo2.created))
finally:
    _at_path.write_text(_AT_ORIG, encoding="utf-8")
    importlib.reload(_at)
    _armod.ArtifactRepo, _dbmod.get_db = _ORIG_REPO, _ORIG_GDB

import py_compile   # noqa: E402
chk("M-restore 变异后已复原且可编译",
    py_compile.compile(str(_at_path), doraise=True) or True)

print("\n" + "=" * 60)
print("PASSED=%d FAILED=%d" % (len(PASSED), len(FAILED)))
if FAILED:
    print("FAILED items:")
    for f in FAILED:
        print("  - " + f)
sys.exit(1 if FAILED else 0)
