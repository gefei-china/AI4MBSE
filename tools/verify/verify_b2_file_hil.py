#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""门禁：B2-4 受控工作区根约束 + B2-5 HIL deny 档与 destructive 自批闸（2026-11-18）

## 为什么有这个门禁
实施计划 B2 的 D4/D5 两个决策落地后，缺一个能证伪的判据：
  · D4「路径根约束」容易写成"配了但没接线"——纯函数存在、入口没调，就是假闸；
  · D5「deny 档」容易写成"加进 LEVELS 但消费侧没分支"——裁决出 deny 后
    走进 confirm 入队分支，变成"可以批准"，与语义相反；
  · 「自批闸」容易写成"写了注释没写判定"。
⇒ 本门禁用**行为**而非字面量证明三件事（F/H/S 三组，共 22 项）。

## 判据设计（为什么这么测）
- F 组用真实系统路径（C:\\Windows）与真实 `..` 穿越构造反例，
  并用 `exec_file_tool` **入口**调用证明接线（不只测纯函数）。
- H 组用真实策略表结构喂 `evaluate` 证明 `deny` 能被裁决出来，
  再用**源码位置比较**证明 deny 分支在入队分支之前（顺序错就等于没做）。
- S 组在**真库副本**上造两张确认单（destructive / write），
  验证「本人批自己被拦、他人批放行、可逆写本人批不拦」三分支。
  零写生产：副本用 sqlite backup 建在临时目录，跑完即删。

## 归层
local。S 组需要真库（建副本 + init_db），干净 checkout 无 mbse.db 必红。

## 时序纪律（本门禁初版踩过）
必须【先建副本 → 再设 MBSE_DB_PATH → 最后导入工程模块】：
`core.config` 在**导入时**读环境变量生成 DEFAULT_CONFIG，导入后再设 env 无效
⇒ init_db 会打到生产库。实测后果仅「多一个预期列、无数据变更」，但仍是越界。

用法：python tools/verify/verify_b2_file_hil.py
"""
import contextlib
import io
import os
import shutil
import sqlite3
import sys
import tempfile

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
os.chdir(REPO)

_P = [0]
_F = [0]
_FAILS = []


def check(name, cond, extra=""):
    if cond:
        _P[0] += 1
        print("  PASS  %s  %s" % (name, extra))
    else:
        _F[0] += 1
        _FAILS.append(name)
        print("  FAIL  %s  %s" % (name, extra))


def main():
    db = os.path.join(REPO, "mbse.db")
    if not os.path.exists(db):
        print("FAIL  找不到 mbse.db（本门禁归 local 层：需要真库建副本）")
        return 1

    # ── 第 1 步：建副本（纯 stdlib，尚未导入工程模块）──
    tmpd = tempfile.mkdtemp(prefix="b2fh_")
    dst = os.path.join(tmpd, "probe.db")
    try:
        _s = sqlite3.connect("file:" + db.replace("\\", "/") + "?mode=ro", uri=True)
        _d = sqlite3.connect(dst)
        _s.backup(_d)
        _d.close()
        _s.close()
        for suf in ("-wal", "-shm"):
            w = db + suf
            if os.path.exists(w):
                shutil.copy2(w, dst + suf)

        # ── 第 2 步：设环境变量（早于 core.config 导入）──
        os.environ["MBSE_DB_PATH"] = dst

        # ── 第 3 步：F 组 文件写入根约束 ──
        print("\nF 组：受控工作区根约束（写/删/建目录受限，读不限）")
        import file_tools as FT
        from core.config import DEFAULT_CONFIG as DC

        deny = DC["file_tools"]["deny_roots"]
        check("F0 deny_roots 默认非空（默认生效，不是空配置）", bool(str(deny).strip()),
              str(deny)[:60])
        check("F1 系统目录拒写",
              FT.check_write_path(r"C:\Windows\System32\drivers\etc\hosts",
                                  deny_roots=deny)[0] is False)
        check("F2 工程目录可写",
              FT.check_write_path(os.path.join(REPO, "outputs", "a.md"),
                                  deny_roots=deny)[0] is True)
        check("F3 白名单内可写",
              FT.check_write_path(os.path.join(REPO, "outputs", "a.md"),
                                  allow_roots=os.path.join(REPO, "outputs"),
                                  deny_roots=deny)[0] is True)
        check("F4 白名单外拒写",
              FT.check_write_path(os.path.join(REPO, "docs", "a.md"),
                                  allow_roots=os.path.join(REPO, "outputs"),
                                  deny_roots=deny)[0] is False)
        check("F5 '..' 穿越被 realpath 解掉并拒",
              FT.check_write_path(os.path.join(REPO, "outputs", "..", "docs", "a.md"),
                                  allow_roots=os.path.join(REPO, "outputs"),
                                  deny_roots=deny)[0] is False)
        check("F6 空 path 拒", FT.check_write_path("", deny_roots=deny)[0] is False)
        r = FT.exec_file_tool("file_write", {"path": r"C:\Windows\_b24_probe.txt",
                                             "content": "x"})
        check("F7 exec 入口真拦（接线证据，不只纯函数）",
              r.get("ok") is False and r.get("code") == "PATH_OUT_OF_ROOT",
              str(r.get("code")))
        r2 = FT.exec_file_tool("file_read", {"path": r"C:\Windows\win.ini",
                                             "max_chars": 10})
        check("F8 读不受根约束（只拦写，不打断正常读）",
              r2.get("code") != "PATH_OUT_OF_ROOT")

        # ── H 组 HIL deny 档 ──
        print("\nH 组：deny 档（系统硬拒，不入队，无批准入口）")
        from services.hil_policy import LEVELS, LEVEL_RANK, evaluate, escalate

        check("H1 LEVELS 含 deny 且为最严档",
              "deny" in LEVELS and LEVEL_RANK["deny"] == max(LEVEL_RANK.values()),
              str(LEVELS))
        d = evaluate([{"id": 1, "decision_point": "tool_write", "match_key": "any",
                       "level": "deny", "priority": 10, "conditions": {}}],
                     decision_point="tool_write", match_key="write", context={})
        check("H2 策略表 level=deny 能被裁决出来", d.get("level") == "deny",
              str(d.get("level")))
        check("H3 escalate 不会把 deny 放宽", escalate("deny", "confirm") == "deny")
        check("H4 escalate 可收紧到 deny", escalate("auto", "deny") == "deny")
        src = io.open(os.path.join(REPO, "agent", "pipeline_parts", "tools.py"),
                      encoding="utf-8").read()
        i_deny = src.find('if _level == "deny":')
        i_conf = src.find("# ── confirm / approve：入队并挂起 ──")
        check("H5 deny 分支位于入队分支之前（顺序错=语义反）",
              0 < i_deny < i_conf, "deny@%d confirm@%d" % (i_deny, i_conf))
        check("H6 deny 分支返回 denied=True 且不建确认单",
              '"denied": True' in src[i_deny:i_deny + 600])

        # ── S 组 destructive 自批闸（副本）──
        print("\nS 组：destructive 自批闸（四眼原则最小切口）")
        from database import init_db

        _buf = io.StringIO()
        with contextlib.redirect_stdout(_buf):
            init_db()
        c = sqlite3.connect(dst)
        c.row_factory = sqlite3.Row
        cols = {row[1] for row in c.execute("PRAGMA table_info(hil_confirmations)")}
        check("S1 副本上 requested_by 列已迁移", "requested_by" in cols)

        import hil_service as HS

        HIL = HS.HILService

        def mk(action, ttype):
            rec = HIL.queue_confirmation(c, "agent_probe", action, {"path": "x"},
                                         preview="p", conversation_id=0, run_id=0,
                                         tool_type=ttype, level="confirm",
                                         requested_by="张三")
            return int((rec or {}).get("id") or 0)

        cid_d = mk("delete_model_elements", "mcp")
        cid_w = mk("write_file", "local")
        check("S2 两张确认单入队成功", cid_d > 0 and cid_w > 0, "%s/%s" % (cid_d, cid_w))
        check("S2b 发起人已落库",
              str((HIL.get(c, cid_d) or {}).get("requested_by")) == "张三")
        r_self = HIL.decide(c, cid_d, True, decided_by="张三")
        check("S3 destructive 自批被拦",
              r_self.get("status") == "self_approval_blocked", r_self.get("status"))
        check("S4 被拦后仍 pending（未放行）",
              (HIL.get(c, cid_d) or {}).get("status") == "pending")
        r_other = HIL.decide(c, cid_d, True, decided_by="李四")
        check("S5 他人批准放行", r_other.get("status") == "approved", r_other.get("status"))
        r_w = HIL.decide(c, cid_w, True, decided_by="张三")
        check("S6 可逆写本人确认不拦（不误伤正常用法）",
              r_w.get("status") == "approved", r_w.get("status"))

        # ── M 变异自证：判据有牙齿（改配置就会红）──
        print("\nM 组：变异自证")
        ok_off, _ = FT.check_write_path(r"C:\Windows\a.txt", deny_roots="")
        check("M1 置空 deny_roots ⇒ F1 判据失效（证明 F1 靠它）", ok_off is True)
        r_bad = HS.HILService.decide(c, mk("delete_model_elements", "mcp"), True,
                                     decided_by="张三")
        check("M2 新建的 destructive 单自批仍被拦（非偶然命中旧状态）",
              r_bad.get("status") == "self_approval_blocked", r_bad.get("status"))
        c.close()
    finally:
        shutil.rmtree(tmpd, ignore_errors=True)

    print("\n" + "=" * 74)
    print("断言汇总：%d PASS / %d FAIL" % (_P[0], _F[0]))
    if _FAILS:
        print("失败项：", "；".join(_FAILS))
    print("=" * 74)
    return 0 if _F[0] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
