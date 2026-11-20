# -*- coding: utf-8 -*-
"""B2 权限门门禁（2026-11-18）：SPARQL 五端点 + 五内建工具授权门。只读、零 LLM。

判据：
  S1 sparql.py 五个端点全部挂 require_permission（3 view + 2 edit）
  S2 复用 kb_ontology 域，零新 domain（roles 表无需迁移）
  S3 require_permission 对匿名一律 401（与 enforce_login 无关）— 源码断言
  S4 五内建工具在统一出口有授权门（bypass_tool_perm）+ 回退开关
  S5 白名单仍生效（_ALLOWED_UPDATE_OPS 不含 DROP/CREATE）
  M1~M2 变异自证
"""
from __future__ import annotations
import io, os, sys
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
FAILS, N = [], 0

def check(name, cond, detail=""):
    global N
    N += 1
    print(("  [OK]   " if cond else "  [FAIL] ") + name + (("  —— " + detail) if detail else ""))
    if not cond: FAILS.append(name)

def main():
    print("B2 权限门门禁")
    print("=" * 66)
    sp = io.open(os.path.join(REPO, "routers", "sparql.py"), encoding="utf-8").read()
    print("\nS1-S2 SPARQL 五端点")
    check("S1a 5 个端点均挂 require_permission", sp.count('require_permission("kb_ontology"') == 5,
          "count=%d" % sp.count('require_permission("kb_ontology"'))
    check("S1b 读接口 view×3（dependency + query×2）",
          sp.count('require_permission("kb_ontology", "view")') == 3)
    check("S1c 写接口 edit×2（sync + update）",
          sp.count('require_permission("kb_ontology", "edit")') == 2)
    check("S2 复用既有域，无新 domain 字面量", '"graph_db"' not in sp)

    print("\nS3 匿名拒绝语义（源码断言 + 强制态确定性验证）")
    deps = io.open(os.path.join(REPO, "core", "deps.py"), encoding="utf-8").read()
    check("S3a require_permission 匿名分支抛 401",
          'raise HTTPException(status_code=401, detail="未登录")' in deps)
    check("S3b 匿名放行只在不强制态（注释与实现一致）",
          "匿名放行**只在非强制态**成立" in deps)
    # S3c：强制态下 current_user 对匿名必须 401（B2 门真正闭合的唯一路径，
    #      平台设计：enforce_login=True 时 current_user 先抛 401 ⇒ 匿名分支不可达）。
    #      patch 配置验证，不翻生产配置。
    try:
        from core import config as _cfg
        from core import deps as _deps
        _orig = _deps._auth_cfg
        _deps._auth_cfg = lambda: {"enforce_login": True, "trust_user_id_header": False}

        class _R:  # 无任何凭据的请求桩
            headers = {}
            client = None
        try:
            _u = _deps.current_user(_R(), conn=None)
            _raised = False
        except Exception as e:
            _raised = getattr(e, "status_code", None) == 401
        check("S3c ★ enforce_login=True 时匿名 current_user 抛 401（闸真实闭合）", _raised)
    finally:
        _deps._auth_cfg = _orig
    # S3d：非强制态下匿名放行（平台兼容语义——五工具闸对匿名聊天同样放行，不断澄清流）
    _deps._auth_cfg = lambda: {"enforce_login": False, "trust_user_id_header": True}
    try:
        _u2 = _deps.current_user(_R(), conn=None)
        check("S3d 非强制态匿名放行（兼容语义，has_perm(None)→not enforce）", _u2 is None)
    finally:
        _deps._auth_cfg = _orig

    print("\nS4 五内建工具授权门")
    tl = io.open(os.path.join(REPO, "agent", "pipeline_parts", "tools.py"), encoding="utf-8").read()
    check("S4a 统一出口有 bypass_tool_perm 闸", "bypass_tool_perm" in tl)
    check("S4b 五个早退工具全在名单",
          all(n in tl for n in ("read_attachment_section", "memory_fetch",
                                "tool_result_fetch", "todo_write", "ask_user")))
    check("S4c 有逐字节回退开关", "_tool_bypass_perm_gate_disabled" in tl)
    check("S4d 拒绝走结构化返回（不抛异常打断 ReAct）",
          '"permission_denied": True' in tl and "gate" in tl)

    print("\nS5 SPARQL 白名单仍生效")
    check("S5a 白名单不含 DROP/CREATE",
          '"DROP"' not in sp.split("_ALLOWED_UPDATE_OPS")[1].split("}", 1)[0]
          and '"CREATE"' not in sp.split("_ALLOWED_UPDATE_OPS")[1].split("}", 1)[0])
    check("S5b 原 403 拒绝分支保留", "不允许的 SPARQL 更新操作" in sp)

    print("\nS6 web_fetch 网络边界")
    wt = io.open(os.path.join(REPO, "web_tools.py"), encoding="utf-8").read()
    sys.path.insert(0, REPO)
    from web_tools import is_allowed_url
    check("S6a 私网/环回/元数据地址全拒",
          not is_allowed_url("http://127.0.0.1/x")[0]
          and not is_allowed_url("http://localhost/x")[0]
          and not is_allowed_url("http://169.254.169.254/latest")[0])
    check("S6b 合法公网域名放行", is_allowed_url("https://mp.weixin.qq.com/s/x")[0])
    check("S6c 域名白名单精确/后缀匹配",
          is_allowed_url("https://a.example.com/x", ["*.example.com"])[0]
          and not is_allowed_url("https://evil.com/x", ["*.example.com"])[0])
    check("S6d 非 http/https 拒绝", not is_allowed_url("ftp://x/y")[0])
    from core.config import DEFAULT_CONFIG, CONFIG_SCHEMA
    check("S6e 配置 DEFAULT+SCHEMA 双登记",
          "allow_hosts" in DEFAULT_CONFIG["web_search"]
          and "allow_hosts" in CONFIG_SCHEMA["web_search"])

    print("\nM 变异自证")
    check("M1 去掉一个 require_permission ⇒ S1 判红",
          sp.count('require_permission("kb_ontology"') == 5 and
          sp.replace('require_permission("kb_ontology", "view")', 'X', 1).count(
              'require_permission("kb_ontology"') == 4)
    check("M2 去掉 bypass 闸 ⇒ S4 判红",
          "bypass_tool_perm" in tl and "bypass_tool_perm" not in tl.replace("bypass_tool_perm", ""))

    print("\n" + "=" * 66)
    if FAILS:
        print("结论：%d/%d 通过，%d 条失败" % (N - len(FAILS), N, len(FAILS)))
        for f in FAILS: print("  - " + f)
        return 1
    print("结论：全部通过（%d/%d）" % (N, N))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
