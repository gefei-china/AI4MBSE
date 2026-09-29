# -*- coding: utf-8 -*-
"""多工程自检的**变异自证**（2026-09-28）。

为什么必须有这个文件：**断言"跑通了"丝毫不证明它有效**。
本脚本把 P0-1/P0-2 的三处旧写法注回源码，主脚本 `verify_multi_project_scope.py`
**必须**新增失败项；抓不到 = 那条断言是空转的（假绿比没有更糟）。

变异点：
  M1 会话归属还原成「未指定 → 回落 settings.default_project_id」（断点② 的原写法）
  M2 反查去掉 ref 精确比较（同工具即命中 —— 会把「同工具另一个工程」错当命中）
  M3 工程闸还原成「会话无归属 → 静默放行」（断点③ 的原写法）
  M4 resolve 端点注册到 `/api/projects/{project_id}` **之后**（被 path 参数吃掉）

安全：变异期间先 `cp -p` 备份，跑完**逐文件 diff 校验还原**，最后再跑一次基线确认全绿。
    .venv/Scripts/python.exe -X utf8 tools/verify/verify_multi_project_scope_mutate.py
"""
import os
import shutil
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MAIN = os.path.join("tools", "verify", "verify_multi_project_scope.py")
BAK = os.path.join(ROOT, "tmp", "_mut_bak_multiproj")
os.makedirs(BAK, exist_ok=True)

PY = sys.executable

MUTATIONS = [
    {
        "tag": "M1 会话归属回落全局默认（断点② 原写法）",
        "file": "repositories/conversation_repo.py",
        "anchor": '        return self.execute(\n'
                  '            "INSERT INTO conversations (title, intent, user_id, project_id) VALUES (?,?,?,?)",\n',
        "inject": '        if project_id is None:\n'
                  '            from repositories.project_repo import resolve_project_id\n'
                  '            project_id = resolve_project_id(self.conn)\n',
        "expect": "未显式给 project_id",
    },
    {
        "tag": "M2 反查去掉 ref 精确比较（同工具即命中）",
        "file": "repositories/project_repo.py",
        "anchor": '            if (b.get("ref") or "").strip() == r:\n',
        "inject": '            if True:\n',
        "replace": True,
        "expect": "ref 不同",
    },
    {
        # 2026-09-28 说明：P1 之后闸门有两道判空（先归属、再工程存在），只摘掉其中一道
        # **打不开**这个洞（另一道会兜住）—— 这恰恰说明代码比修复前更稳。
        # 故本条变异还原的是「断点③ 的真正形态」：JOIN 不到 → 整段跳过 → **放行**。
        "tag": "M3 工程闸静默放行（断点③ 原写法：JOIN 不到就跳过并放行）",
        "file": "routers/sysml_versions.py",
        "anchor": '    if not pid:\n'
                  '        return {"ok": False, "code": "PROJECT_NOT_BOUND",\n'
                  '                "error": "该版本所属会话未关联工程，无法写回建模工具"\n'
                  '                         "（写回是工程操作：先把会话归入项目，或在本会话内选择工程后再试）",\n'
                  '                "tool": ""}\n',
        "inject": '    if not pid:\n'
                  '        return {"ok": True, "code": "", "error": "", "tool": ""}\n',
        "replace": True,
        "expect": "PROJECT_NOT_BOUND",
    },
]


def run_main():
    r = subprocess.run([PY, "-X", "utf8", MAIN], cwd=ROOT,
                       capture_output=True, text=True, encoding="utf-8", errors="replace")
    fails = [ln.strip()[6:].split("  |")[0].strip()
             for ln in (r.stdout or "").splitlines() if ln.strip().startswith("FAIL")]
    # 注：`ln.strip()` 形如 "FAIL  <名称>"，前缀长度 6（"FAIL" + 2 空格）；切多一个会把 ★/首字吃掉。
    return set(fails), (r.stdout or "") + (r.stderr or "")


def _undo(m):
    src = os.path.join(ROOT, m["file"])
    bak = os.path.join(BAK, m["file"].replace("\\", "_").replace("/", "_"))
    shutil.copy2(bak, src)


def main():
    base_fails, out = run_main()
    if base_fails:
        print("❌ 基线自身有失败项，变异判据无意义：" + "; ".join(sorted(base_fails)))
        return 1
    print(f"基线：0 fail —— 变异判据可用（只看「新增失败」）")

    rc = 0
    for m in MUTATIONS:
        src = os.path.join(ROOT, m["file"])
        bak = os.path.join(BAK, m["file"].replace("\\", "_").replace("/", "_"))
        os.makedirs(os.path.dirname(bak), exist_ok=True)
        shutil.copy2(src, bak)          # cp -p 备份
        text = open(src, encoding="utf-8").read()
        n = text.count(m["anchor"])
        if n != 1:
            print(f"❌ {m['tag']}：锚点命中 {n} 次（期望 1）→ 跳过，源码未改")
            continue
        if m.get("replace"):
            new = text.replace(m["anchor"], m["inject"])
        else:
            new = text.replace(m["anchor"], m["inject"] + m["anchor"])
        with open(src, "w", encoding="utf-8", newline="") as f:
            f.write(new)
        try:
            fails, _ = run_main()
            added = fails - base_fails
            hit = bool(added) and any(m["expect"] in x for x in added)
            print(("  ✅ " if hit else "  ❌ ") + m["tag"])
            if hit:
                print("       新增失败：" + "; ".join(sorted(added))[:200])
            else:
                print("       未抓到（判据空转！）新增失败：" + ("; ".join(sorted(added)) or "无"))
                rc = 1
        finally:
            _undo(m)
            same = open(src, encoding="utf-8").read() == open(bak, encoding="utf-8").read()
            print("       还原校验：" + ("一致 ✅" if same else "❌ 不一致，源码被污染！"))
            if not same:
                rc = 1

    # M4：路由顺序变异（把 resolve 端点块挪到 {project_id} 之后）
    fp = os.path.join(ROOT, "routers", "projects.py")
    bak4 = os.path.join(BAK, "routers_projects.py")
    shutil.copy2(fp, bak4)
    t = open(fp, encoding="utf-8").read()
    a = t.find('@router.get("/api/projects/resolve")')
    b = t.find('@router.get("/api/projects/{project_id}")')
    if a < 0 or b < 0 or a > b:
        print("❌ M4 锚点异常，跳过")
        rc = 1
    else:
        block = t[a:b]
        with open(fp, "w", encoding="utf-8", newline="") as f:
            f.write(t[:a] + t[b:] + "\n\n" + block)
        try:
            fails, _ = run_main()
            added = fails - base_fails
            hit = bool(added)
            print(("  ✅ " if hit else "  ❌ ") + "M4 resolve 端点注册顺序后置（被 path 参数吃掉）")
            print("       新增失败：" + "; ".join(sorted(added))[:200])
            if not hit:
                rc = 1
        finally:
            shutil.copy2(bak4, fp)
            same = open(fp, encoding="utf-8").read() == open(bak4, encoding="utf-8").read()
            print("       还原校验：" + ("一致 ✅" if same else "❌ 不一致，源码被污染！"))
            if not same:
                rc = 1

    # 收尾：基线必须回到全绿
    final_fails, _ = run_main()
    print()
    print("收尾基线：" + ("0 fail ✅" if not final_fails else "❌ " + "; ".join(sorted(final_fails))))
    if final_fails:
        rc = 1
    print("变异自证：" + ("全部变异被抓住 ✅" if rc == 0 else "存在未被抓住的变异 ❌"))
    return rc


if __name__ == "__main__":
    sys.exit(main())
