#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""部署就绪自检（D3，2026-10-15）：私有化部署/换机/换解释器后先跑一遍。

背景（E2E 走查实测）：推理链（owlready2+JPype+JRE）装在 .venv 里，用系统 python
起服务则 /api/swrl/run 500（owlready2 未安装）。核心服务不崩、但功能静默降级
—— 部署方若不知道这个差异，会以为"产品坏了"。本脚本把依赖状态变成**显式报告**。

用法：
    python tools/check_deploy_readiness.py           # 报告；核心缺失 exit 1
    python tools/check_deploy_readiness.py --strict  # SWRL/DL 推理链缺失也算失败

「零外部依赖」的准确含义（对部署方的承诺边界）：
  ✅ 允许复用开源成熟组件（FastAPI/SQLite/owlready2/Pellet 都是）——
     前提是**随部署包本地化**（venv 打死、static/vendor 本地化、捆绑 JRE 随包），
     安装后运行期不联网、不依赖外部服务（无 CDN / 无 Fuseki 等外部进程也可选装）。
  ❌ 不允许的是：运行时联网拉依赖、依赖外部 SaaS、或把"装没装"留给运气。
  本脚本检查的就是第二条边界：装完之后，所有承诺的功能在**本机**是否真的可用。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

STRICT = "--strict" in sys.argv


def _which_java():
    import shutil
    return bool(shutil.which("java"))


report = {"core": [], "swrl": [], "strict": STRICT, "ok": True, "swrl_ready": None}


def _mod(name):
    try:
        __import__(name)
        return True, None
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


# ── 核心（必须）──
ok, err = _mod("fastapi")
report["core"].append({"item": "fastapi", "ok": ok, "err": err})
ok2, err2 = _mod("uvicorn")
report["core"].append({"item": "uvicorn", "ok": ok2, "err": err2})

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
db_ok = os.path.exists(os.path.join(BASE, "mbse.db"))
report["core"].append({"item": "mbse.db 存在", "ok": db_ok,
                       "err": None if db_ok else "未找到数据库文件（首次启动会自动创建，可忽略）"})
static_ok = os.path.isdir(os.path.join(BASE, "static"))
report["core"].append({"item": "static/ 资源目录", "ok": static_ok,
                       "err": None if static_ok else "缺少 static/，前端不可用"})
report["ok"] = report["ok"] and ok and ok2 and db_ok and static_ok

# ── SWRL/DL 推理链（可选但承诺给用户的功能）──
try:
    from services.swrl_engine import dependency_status, ensure_jvm_home
    dep = dependency_status()
    jre = ensure_jvm_home()
    report["swrl"] = [
        {"item": "owlready2", "ok": bool(dep["owlready2"]),
         "err": None if dep["owlready2"] else dep["install_hint"]},
        {"item": "jpype1", "ok": bool(dep["jpype"]),
         "err": None if dep["jpype"] else dep["install_hint"]},
        {"item": "JRE（捆绑 java-runtime / JAVA_HOME / PATH）",
         "ok": jre is not None or bool(os.environ.get("JAVA_HOME")) or _which_java(),
         "err": None if (jre or os.environ.get("JAVA_HOME") or _which_java())
         else "找不到 java：推理不可用（捆绑 JRE 应随部署包放在 java-runtime/）"},
    ]
    report["swrl_ready"] = all(x["ok"] for x in report["swrl"])
except Exception as e:  # dependency_status 自身不可用 = 更严重
    report["swrl"] = [{"item": "swrl_engine 可加载", "ok": False,
                       "err": f"{type(e).__name__}: {e}"}]
    report["swrl_ready"] = False

if STRICT and not report["swrl_ready"]:
    report["ok"] = False

print(json.dumps(report, ensure_ascii=False, indent=1))
if not report["ok"]:
    print("\n结果：未就绪（核心缺失或 --strict 下推理链缺失）", file=sys.stderr)
    sys.exit(1)
print("\n结果：部署就绪" + ("" if report["swrl_ready"]
      else "（注意：SWRL/DL 推理链未就绪，该功能将不可用；加 --strict 可让此项算失败）"))
sys.exit(0)
