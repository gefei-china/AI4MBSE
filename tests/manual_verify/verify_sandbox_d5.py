"""D5 沙箱代码执行模块闭环验证：进程隔离 + 超时 + 受限环境 + Python/JS。

T1. 正常执行（python，_in 读输入 → _out 写输出）
T2. 危险操作拦截（import os / open / eval / __import__）
T3. 超时强杀（死循环 → 超时终止，不卡主进程）
T4. JS 执行（node vm：正常输出 + 语法错误 + require 不可用）
T5. 主进程存活（子进程崩溃不影响主进程）
T6. workflows._exec_code 走沙箱路径（python 正常 + js 正常 + 危险代码报错）
"""
import os
import sys
import json

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ["MBSE_LLM_FORCE_MOCK"] = "1"
_TEST_DB = os.path.join(os.path.dirname(__file__), "_d5_test.db")
if os.path.exists(_TEST_DB):
    os.remove(_TEST_DB)
os.environ["MBSE_DB_PATH"] = _TEST_DB

PASS = 0
FAIL = 0


def chk(name, ok, detail=""):
    global PASS, FAIL
    if ok:
        PASS += 1
        print(f"  [PASS] {name}")
    else:
        FAIL += 1
        print(f"  [FAIL] {name}  → {detail}")


# ── T1: 正常执行（python）──
print("== T1: 正常执行（python） ==")
from sandbox import run_code
r = run_code("out = _out\nx = _in.get('a', 0)\ny = _in.get('b', 0)\n_out['sum'] = x + y\n_out['doubled'] = [i*2 for i in range(3)]",
             language="python", inputs={"a": 3, "b": 4}, timeout=5)
chk("T1 正常执行 ok", r.get("ok"), str(r))
chk("T1 sum=7", r.get("outputs", {}).get("sum") == 7, str(r.get("outputs")))
chk("T1 doubled=[0,2,4]", r.get("outputs", {}).get("doubled") == [0, 2, 4], str(r.get("outputs")))

# ── T2: 危险操作拦截 ──
print("== T2: 危险操作拦截 ==")
r = run_code("import os\n_out['x'] = os.getcwd()", language="python")
chk("T2 import os 被拒", (not r.get("ok")) and "沙箱禁止导入" in str(r.get("error")), str(r))
r = run_code("open('C:/tmp.txt','w')", language="python")
chk("T2 open 未定义报错", not r.get("ok"), str(r))
r = run_code("eval('1+1')", language="python")
chk("T2 eval 未定义报错", not r.get("ok"), str(r))
r = run_code("__import__('subprocess').call(['echo','hi'])", language="python")
chk("T2 __import__ 被拒", (not r.get("ok")) and "沙箱禁止导入" in str(r.get("error")), str(r))
r = run_code("getattr(__builtins__, 'open')", language="python")
chk("T2 getattr 未定义报错", not r.get("ok"), str(r))

# ── T3: 超时强杀 ──
print("== T3: 超时强杀 ==")
t0 = __import__("time").time()
r = run_code("while True: pass", language="python", timeout=2)
dt = __import__("time").time() - t0
chk("T3 死循环超时终止", (not r.get("ok")) and "超时" in str(r.get("error")), str(r))
chk("T3 耗时受控(2-8s)", 2 <= dt < 8, f"dt={dt:.1f}s")

# ── T4: JS 执行 ──
print("== T4: JS 执行（node vm） ===")
r = run_code("const a = _in.a || 0; const b = _in.b || 0;\n_out.sum = a + b;\n_out.list = [1,2,3].map(x=>x*2);",
             language="js", inputs={"a": 5, "b": 6}, timeout=5)
chk("T4 JS 正常执行", r.get("ok"), str(r))
chk("T4 JS sum=11", r.get("outputs", {}).get("sum") == 11, str(r.get("outputs")))
chk("T4 JS list=[2,4,6]", r.get("outputs", {}).get("list") == [2, 4, 6], str(r.get("outputs")))
r = run_code("_out.x = ) syntax error", language="js")
chk("T4 JS 语法错误报错", not r.get("ok"), str(r))
r = run_code("const fs = require('fs'); _out.x = fs.readFileSync('C:/a.txt','utf8');",
             language="js")
chk("T4 JS require 不可用", (not r.get("ok")), str(r))

# ── T5: 主进程存活 ──
print("== T5: 主进程存活 ==")
r = run_code("import sys\nsys.exit(99)", language="python")
chk("T5 子进程 exit 不崩主进程", isinstance(r, dict), str(r))

# ── T6: workflows._exec_code 沙箱路径 ──
print("== T6: workflows._exec_code 走沙箱 ==")
from workflows import FlowExecutor
ex = FlowExecutor()
r = ex._exec_code("n2", {"language": "python", "code": "_out['v'] = _in['q'] + '!'",
                          "inputs": {"q": "{{n1.data.outputs.msg}}"}},
                   {"n1": {"data": {"outputs": {"msg": "hi"}}}}, {})
chk("T6 python 节点正常", r.get("status") == "done" and r.get("data", {}).get("outputs", {}).get("v") == "hi!",
    str(r))
r = ex._exec_code("n2j", {"language": "js", "code": "_out['v'] = _in['q'].toUpperCase()",
                           "inputs": {"q": "sandbox"}}, {}, {})
chk("T6 js 节点正常", r.get("status") == "done" and r.get("data", {}).get("outputs", {}).get("v") == "SANDBOX",
    str(r))
r = ex._exec_code("n2b", {"language": "python", "code": "import os", "inputs": {}}, {}, {})
chk("T6 危险代码节点报错", r.get("status") == "error" and "代码执行失败" in r.get("content", ""), str(r))

print(f"\n===== D5 结果: {PASS} 通过 / {FAIL} 失败 =====")
sys.exit(1 if FAIL else 0)
