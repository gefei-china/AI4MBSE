"""pytest 全局配置（2026-09-17 S2 建立）。

**为什么需要它**：项目此前没有任何 pytest 配置，`pytest` 会直接收集 `tests/test_api.py`
—— 而那个文件不是 pytest 测试：它是一个**手工端到端脚本**（自己起 uvicorn 监听 :8000，
再用 httpx 打真实 HTTP，且定义了 `test(name, method, path, ...)` 这种带必填参数的辅助函数）。
pytest 会把它当测试函数调用 → 报缺参数 → 于是 `.pytest_cache/v/cache/lastfailed` 里
长期留着 `tests/test_api.py::test` 这条"失败"。**那条失败不是产品缺陷，是收集方式错配。**

本文件做两件事：
1. `collect_ignore` 排除手工脚本（它们需要真实服务/浏览器，归 `tests/manual_verify/` 与 `tmp/`）；
2. 在**导入任何项目模块之前**把数据库指向独立测试库，保证测试绝不碰生产 `mbse.db`
   （机制见 `core/config.py:158-165`：`MBSE_DB_PATH` → `database.path`）。
"""
import os
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# ── 1) 排除手工脚本 ──
# 这三个是**脚本风格**（PASS/FAIL 计数器 + `if __name__`），不是 pytest 测试：
#   test_api.py                自起 uvicorn 打真实 HTTP；且定义了带必填参数的 `test(...)` 辅助函数
#                              → pytest 把它当测试函数调用 → 报缺参数 → 这就是 .pytest_cache 里
#                                长期挂着 `tests/test_api.py::test` 这条"失败"的真因（不是产品缺陷）
#   test_extended_features.py / test_glossary_routing.py  同为脚本风格，且模块顶层用
#                              `io.TextIOWrapper(sys.stdout.buffer, ...)` 重包装 stdout —— 旧包装器被 GC 时会
#                              连带关闭底层 buffer，导致 pytest 终端写入器写已关闭文件（I/O operation on closed file）
collect_ignore = [
    "tests/test_api.py",
    "tests/test_extended_features.py",
    "tests/test_glossary_routing.py",
]
_manual = ROOT / "tests" / "manual_verify"
if _manual.is_dir():
    collect_ignore += [str(p.relative_to(ROOT)) for p in _manual.glob("*.py")]

# ── 2) 测试环境隔离（必须在 import 项目模块之前生效）──
_TEST_DB = os.environ.get("MBSE_TEST_DB") or str(ROOT / "tests" / "_test_mbse.db")
os.environ["MBSE_DB_PATH"] = _TEST_DB
os.environ.setdefault("MBSE_LLM_FORCE_MOCK", "1")   # 不依赖外部 LLM，回归确定性
# 配置也隔离：指向不存在的路径 → 回落默认配置 + 上面的环境变量覆盖（不读用户真实配置）
os.environ.setdefault("MBSE_CONFIG_PATH", str(ROOT / "tests" / "_test_config.json"))

import pytest  # noqa: E402


@pytest.fixture(scope="session", autouse=True)
def _init_test_db():
    """会话开始时把测试库建好（含种子）。

    为什么必须有：项目里有若干测试模块用**裸 `TestClient(app)`**（不写 `with`，见
    `tests/test_p0_lifecycle_provenance_summary.py:22`）—— 裸客户端不会触发 lifespan，
    因此 `init_db()` 从不执行。以前它们连的是生产库（表早就存在）所以"一直是绿的"，
    一旦把测试指向独立空库就会暴露成 `sqlite3.OperationalError: no such table: documents/messages`。
    这里复刻 `main.py` lifespan 的启动动作（init_db + 种子 + FTS），让裸客户端也能工作。
    """
    from database import init_db
    init_db()
    try:
        from database import db_conn as _db_conn
        from seed_registry import seed_all
        with _db_conn() as _c:
            seed_all(_c)
    except Exception:      # 种子失败不应让整个测试会话崩掉（与 lifespan 的容错口径一致）
        pass
    try:
        from database import get_db
        from fts_search import ensure_fts
        ensure_fts(get_db())
    except Exception:
        pass
    yield


@pytest.fixture(scope="session")
def client():
    """带 lifespan 的 TestClient：触发 init_db + 种子，跑在独立测试库上。"""
    from fastapi.testclient import TestClient
    from main import app
    with TestClient(app) as c:
        yield c


@pytest.fixture(scope="session")
def test_db_path() -> str:
    return os.environ["MBSE_DB_PATH"]
