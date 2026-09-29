# -*- coding: utf-8 -*-
"""P1 自检的**变异自证**（2026-09-28）。

同族纪律：断言"跑通了"丝毫不证明它有效。把 P1 的旧写法注回源码，主脚本
`verify_p1_project_attribution.py` **必须**新增失败项；抓不到 = 空转断言（假绿比没有更糟）。

变异点：
  M1 产物写入去掉「会话归属定格」（改回落平台默认工程）
  M2 闸门去掉「版本定格优先」（只看会话当前归属）
  M3 拉取工具去掉 conversation_id 补传（回到漏传 → 静默用平台 default_vc）
  M4 报告归档改回「显式 or 平台默认」（没有会话这一档）
  M5 迁移去掉两表补列登记
  M6 ★ 迁移**回填存量**（用户明确要求存量不动 —— 这条必须能被抓）

安全：变异期间 `cp -p` 备份，跑完逐文件**内容比对**还原，最后再跑一次基线确认全绿。
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_p1_project_attribution_mutate.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN = os.path.join("tools", "verify", "verify_p1_project_attribution.py")
BAK = os.path.join(ROOT, "tmp", "_mut_bak_p1")
os.makedirs(BAK, exist_ok=True)

PY = sys.executable

MUTATIONS = [
    {
        "tag": "M1 产物写入回落平台默认（去掉会话归属定格）",
        "file": "repositories/artifact_repo.py",
        "anchor": "        if project_id is None:\n"
                  "            from repositories.project_repo import conversation_project_id\n"
                  "            project_id = conversation_project_id(self.conn, conversation_id)\n",
        "inject": "        if project_id is None:\n"
                  "            from repositories.project_repo import resolve_project_id\n"
                  "            project_id = resolve_project_id(self.conn)\n",
        "replace": True,
        "expect": "产物落空串",
    },
    {
        "tag": "M2 闸门只看会话当前归属（去掉版本定格优先）",
        "file": "routers/sysml_versions.py",
        "anchor": '        "COALESCE(NULLIF(TRIM(COALESCE(v.project_id,\'\')),\'\'), TRIM(COALESCE(c.project_id,\'\'))) AS pid "\n',
        "inject": '        "TRIM(COALESCE(c.project_id,\'\')) AS pid "\n',
        "replace": True,
        "expect": "定格",
    },
    {
        "tag": "M3 拉取工具去掉 conversation_id 补传（回到漏传）",
        "file": "agent/pipeline_parts/tools.py",
        "anchor": "                            conversation_id=int(conv_ctx or 0))",
        "inject": "                            )",
        "replace": True,
        "expect": "补传 conversation_id",
    },
    {
        "tag": "M4 报告归档改回「显式 or 平台默认」（无会话档）",
        "file": "routers/reports.py",
        "anchor": "    if not _pid and int(body.conversation_id or 0):\n"
                  "        from repositories.project_repo import conversation_project_id\n"
                  "        _pid = conversation_project_id(conn, int(body.conversation_id or 0))\n"
                  "        _psrc = \"conversation\" if _pid else \"\"\n",
        "inject": "",
        "replace": True,
        "expect": "报告归档归属顺序",
    },
    {
        "tag": "M5 迁移去掉两表补列登记",
        "file": "database/migrations/columns.py",
        "anchor": '    _add("sysml_versions", "project_id", "TEXT DEFAULT \'\'")\n'
                  '    _add("artifacts", "project_id", "TEXT DEFAULT \'\'")\n',
        "inject": "",
        "replace": True,
        "expect": "有 project_id",
    },
]


def run_main():
    r = subprocess.run([PY, "-X", "utf8", MAIN], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    fails = [ln.strip()[6:].split("  |")[0].strip()
             for ln in (r.stdout or "").splitlines() if ln.strip().startswith("FAIL")]
    return set(fails), (r.stdout or "") + (r.stderr or "")


def _undo(m):
    shutil.copy2(os.path.join(BAK, m["file"].replace("\\", "_").replace("/", "_")),
                 os.path.join(ROOT, m["file"]))


def _apply(m, new_text):
    src = os.path.join(ROOT, m["file"])
    with open(src, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)


def main():
    base_fails, _ = run_main()
    if base_fails:
        print("X 基线自身有失败项，变异判据无意义：" + "; ".join(sorted(base_fails)))
        return 1
    print("基线：0 fail —— 变异判据可用（只看「新增失败」）")

    rc = 0
    for m in MUTATIONS:
        src = os.path.join(ROOT, m["file"])
        bak = os.path.join(BAK, m["file"].replace("\\", "_").replace("/", "_"))
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(src, bak)                      # cp -p 备份
        text = open(src, encoding="utf-8").read()
        n = text.count(m["anchor"])
        if n != 1:
            print("X %s：锚点命中 %d 次（期望 1）→ 跳过，源码未改" % (m["tag"], n))
            rc = 1
            continue
        if m.get("replace"):
            new = text.replace(m["anchor"], m["inject"])
        else:
            new = text.replace(m["anchor"], m["inject"] + m["anchor"])
        _apply(m, new)
        try:
            fails, _ = run_main()
            added = fails - base_fails
            hit = bool(added) and any(m["expect"] in x for x in added)
            print(("  [OK] " if hit else "  [NG] ") + m["tag"])
            print("       新增失败：" + ("; ".join(sorted(added))[:220] or "无"))
            if not hit:
                print("       >>> 未抓到（判据空转！）")
                rc = 1
        finally:
            _undo(m)
            same = open(src, encoding="utf-8").read() == open(bak, encoding="utf-8").read()
            print("       还原校验：" + ("一致 [OK]" if same else "[NG] 不一致，源码被污染！"))
            if not same:
                rc = 1

    # ── M6：迁移回填存量（用户明确"存量不动"，这条必须被抓）
    m6 = {"file": "database/migrations/columns.py"}
    src = os.path.join(ROOT, m6["file"])
    bak = os.path.join(BAK, "m6_database_migrations_columns.py")
    shutil.copy2(src, bak)
    t = open(src, encoding="utf-8").read()
    anchor = '    _add("sysml_versions", "project_id", "TEXT DEFAULT \'\'")\n'
    backfill = anchor + (
        "    # M6-VARIANT：回填存量（用户明确要求不回填）\n"
        "    if conn.execute(\"SELECT 1 FROM sqlite_master WHERE type='table' "
        "AND name='sysml_versions'\").fetchone():\n"
        "        conn.execute(\"UPDATE sysml_versions SET project_id="
        "COALESCE((SELECT c.project_id FROM conversations c WHERE c.id=sysml_versions.conversation_id),'')\")\n")
    if t.count(anchor) != 1:
        print("X M6 锚点异常，跳过")
        rc = 1
    else:
        with open(src, "w", encoding="utf-8", newline="") as f:
            f.write(t.replace(anchor, backfill))
        try:
            fails, _ = run_main()
            added = fails - base_fails
            hit = any("存量行" in x for x in added)
            print(("  [OK] " if hit else "  [NG] ") + "M6 迁移回填存量（违反「存量不动」）")
            print("       新增失败：" + ("; ".join(sorted(added))[:220] or "无"))
            if not hit:
                print("       >>> 未抓到（判据空转！）")
                rc = 1
        finally:
            shutil.copy2(bak, src)
            same = open(src, encoding="utf-8").read() == open(bak, encoding="utf-8").read()
            print("       还原校验：" + ("一致 [OK]" if same else "[NG] 不一致，源码被污染！"))
            if not same:
                rc = 1

    # 基线复跑
    fails, _ = run_main()
    print("\n基线复跑：" + ("0 fail [OK]" if not fails else "仍有失败 " + "; ".join(sorted(fails))))
    if fails:
        rc = 1
    print("变异自证：" + ("全部抓到 [OK]" if rc == 0 else "有未抓到/还原不一致 [NG]"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
