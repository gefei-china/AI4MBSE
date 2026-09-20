"""pytest 冒烟测试（2026-09-17 S2 建立）。

目的：为后续清理（删死代码 / 拆巨型文件 / 收敛重复实现）提供**能自动跑的基线**。
铁律：只读断言 + 跑在独立测试库（见 conftest.py），绝不触碰生产 mbse.db。

跑法：`.venv\\Scripts\\python.exe -m pytest`
"""
import os

import pytest

# `static/vendor/`（cytoscape/dagre 等第三方压缩库）属**随包运行时**，被 .gitignore 排除不入库
# （见 .gitignore「大体积二进制与第三方压缩库」条）。全新克隆 / CI 上没有它。
_VENDOR_DIR = os.path.join(os.path.dirname(__file__), "..", "static", "vendor")
_HAS_VENDOR = os.path.isdir(_VENDOR_DIR) and bool(os.listdir(_VENDOR_DIR))


def test_prod_db_not_touched_by_tests(test_db_path):
    """测试库必须是独立文件，且不等于生产库。"""
    assert test_db_path.endswith("_test_mbse.db"), test_db_path
    assert os.path.basename(test_db_path) != "mbse.db", "测试不能指向生产库"


def test_index_html_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<html" in r.text.lower()
    # 主导航 anchor 存在（前端骨架未被破坏的最小断言）
    assert 'id="mainnav"' in r.text


def test_dashboard_api(client):
    r = client.get("/api/dashboard")
    assert r.status_code == 200
    data = r.json()
    assert set(data) & {"active_conversations", "conflicts", "kb_stats"}, data


def test_conversations_list(client):
    r = client.get("/api/conversations")
    assert r.status_code == 200
    assert isinstance(r.json(), list)


def test_documents_list(client):
    r = client.get("/api/documents")
    assert r.status_code == 200
    assert isinstance(r.json(), (list, dict))


def test_static_assets_served(client):
    for p in ("/static/js/mods/01-core.js", "/static/js/mods/02-shell.js", "/static/css/app.css"):
        r = client.get(p)
        assert r.status_code == 200, p
        assert len(r.content) > 0, p


@pytest.mark.skipif(not _HAS_VENDOR,
                    reason="static/vendor/ 为随包运行时（不入库），全新克隆/CI 上无第三方 JS，脚本可达性无从谈起")
def test_frontend_api_surface_present(client):
    """前端 40 个阻塞脚本全部可达（改前端后最容易踩的坑：路径写错/文件被误移）。

    ⚠️ 前提：`static/vendor/` 已就位（开发机随包提供）。CI/全新克隆上本测试被 skip。
    """
    import re

    html = client.get("/").text
    srcs = re.findall(r'<script[^>]+src="(/static/[^"]+)"', html)
    assert len(srcs) >= 30, f"脚本标签过少（{len(srcs)}），index.html 可能被破坏"
    bad = []
    for s in srcs:
        if client.get(s).status_code != 200:
            bad.append(s)
    assert not bad, f"以下脚本 404：{bad}"
