"""P0 改造的 Contract Test：覆盖文件生命周期、节点/边追溯、AI 摘要注入。

P0 三项能力（资料库与AI建模优化设计方案-20260910）：
- 文件 lifecycle_status + 废弃/恢复/归档/批量
- 节点/边元数据 + 一键来源追溯
- AI 消息结论速览生成（规则法）

约定：使用 FastAPI TestClient（启动期 init_db 自动迁移，零副作用）。
通过条件：所有断言通过 + 既有 72 条契约测试无回归。
"""
import json
import os
import sys

# 强制 Mock LLM（不依赖外部网络）
os.environ.setdefault("MBSE_LLM_FORCE_MOCK", "1")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from fastapi.testclient import TestClient
from main import app

c = TestClient(app)
results: list[tuple] = []


def _assert(name: str, ok: bool, detail: str = "") -> None:
    results.append(("PASS" if ok else "FAIL", name, detail))


# ── R1：文件生命周期 ─────────────────────────────────────

def test_doc_lifecycle_create_deprecate_restore():
    """完整生命周期：committed → deprecated → committed（force）。"""
    # 1) 取一个真实文档
    docs = c.get("/api/documents?lifecycle_status=committed&include_deprecated=0").json()
    if not docs:
        _assert("R1 setup: have committed doc", False, "no committed docs to test")
        return
    did = docs[0]["id"]
    _assert("R1 setup: have committed doc", True, f"doc#{did}")

    # 2) 废弃
    r = c.post(f"/api/documents/{did}/deprecate", json={"reason": "Contract test 废弃"})
    _assert("R1 deprecate ok", r.status_code == 200 and r.json().get("lifecycle_status") == "deprecated",
            f"{r.status_code} {r.text[:80]}")

    # 3) 列表过滤应隐藏（默认 include_deprecated=0）
    docs_after = c.get(f"/api/documents?lifecycle_status=deprecated").json()
    _assert("R1 list deprecated filter", any(d["id"] == did for d in docs_after),
            f"found in {len(docs_after)} deprecated docs")

    # 4) 重复废弃应 400
    r2 = c.post(f"/api/documents/{did}/deprecate", json={"reason": "再次废弃"})
    _assert("R1 dedupe: double deprecate blocked", r2.status_code == 400, str(r2.status_code))

    # 5) 时间线（应含 1 条审计记录）
    r = c.get(f"/api/documents/{did}/lifecycle")
    log = r.json().get("log", [])
    _assert("R1 lifecycle timeline recorded",
            r.status_code == 200 and len(log) >= 1,
            f"status={r.status_code} log_count={len(log)}")

    # 6) 强制恢复（force=True 跳过 24h 窗口）
    r = c.post(f"/api/documents/{did}/restore", json={"force": True})
    _assert("R1 restore (force)", r.status_code == 200 and r.json().get("lifecycle_status") == "committed",
            f"{r.status_code} {r.text[:80]}")

    # 7) 空 reason 应被拒
    r = c.post(f"/api/documents/{did}/deprecate", json={"reason": ""})
    _assert("R1 deprecate requires reason", r.status_code == 400, str(r.status_code))


def test_doc_lifecycle_batch():
    """批量废弃：失败独立、可恢复。"""
    docs = c.get("/api/documents?lifecycle_status=committed&include_deprecated=0").json()
    if len(docs) < 2:
        _assert("R1 batch setup", False, "need >=2 committed docs")
        return
    ids = [docs[0]["id"], docs[1]["id"]]
    r = c.post("/api/documents/batch-deprecate",
               json={"ids": ids, "reason": "Batch contract test"})
    _assert("R1 batch deprecate", r.status_code == 200 and r.json().get("ok") == 2,
            f"{r.status_code} {r.text[:120]}")

    # 清理
    for did in ids:
        c.post(f"/api/documents/{did}/restore", json={"force": True})


def test_doc_lifecycle_archive():
    """归档：committed → archived。"""
    docs = c.get("/api/documents?lifecycle_status=committed&include_deprecated=0").json()
    if not docs:
        _assert("R1 archive setup", False, "no committed docs")
        return
    did = docs[0]["id"]
    r = c.post(f"/api/documents/{did}/archive", json={"reason": "Contract test 归档"})
    _assert("R1 archive", r.status_code == 200 and r.json().get("lifecycle_status") == "archived",
            f"{r.status_code} {r.text[:80]}")
    # 清理：直接恢复（archived → committed，service 暂未实现，可手动 UPDATE）
    # 这里不清理因为归档属于正常长期状态


def test_doc_lifecycle_include_deprecated():
    """include_deprecated=0 应隐藏已废弃文档。"""
    # 取一个文档，先废弃
    docs = c.get("/api/documents?lifecycle_status=committed&include_deprecated=0").json()
    if not docs:
        _assert("R1 filter setup", False, "no docs")
        return
    did = docs[0]["id"]
    c.post(f"/api/documents/{did}/deprecate", json={"reason": "filter test"})

    # 默认应隐藏
    full = c.get("/api/documents").json()
    hidden = c.get("/api/documents?include_deprecated=0").json()
    _assert("R1 include_deprecated=0 hides",
            len(hidden) < len(full),
            f"full={len(full)} hidden={len(hidden)}")
    # 清理
    c.post(f"/api/documents/{did}/restore", json={"force": True})


# ── R2：节点/边追溯 ─────────────────────────────────────

def test_entity_provenance():
    """节点一键追溯：5 段视图完整。"""
    import sqlite3
    db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "mbse.db"))
    row = db.execute("SELECT id FROM entities LIMIT 1").fetchone()
    db.close()
    if not row:
        _assert("R2 setup: have entity", False, "no entities")
        return
    eid = row[0]
    r = c.get(f"/api/entities/{eid}/provenance")
    _assert("R2 entity provenance ok", r.status_code == 200,
            f"{r.status_code} {r.text[:80]}")
    data = r.json()
    sections = {"entity", "source_document", "source_chunks", "version_chain",
                "audit_trail", "siblings", "deprecated_trace"}
    _assert("R2 entity provenance has 5+ sections",
            sections.issubset(data.keys()),
            f"missing={sections - data.keys()}")
    _assert("R2 audit_trail <=20",
            isinstance(data.get("audit_trail"), list) and len(data["audit_trail"]) <= 20,
            f"len={len(data.get('audit_trail', []))}")
    _assert("R2 source_chunks <=3",
            isinstance(data.get("source_chunks"), list) and len(data["source_chunks"]) <= 3,
            f"len={len(data.get('source_chunks', []))}")


def test_entity_provenance_404():
    r = c.get("/api/entities/nonexistent_entity_id/provenance")
    _assert("R2 404 for nonexistent entity", r.status_code == 404, str(r.status_code))


def test_relation_provenance():
    """边一键追溯：含 source/target 节点引用。"""
    import sqlite3
    db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "mbse.db"))
    row = db.execute("SELECT id FROM relations LIMIT 1").fetchone()
    db.close()
    if not row:
        _assert("R2 setup: have relation", False, "no relations")
        return
    rid = row[0]
    r = c.get(f"/api/relations/{rid}/provenance")
    _assert("R2 relation provenance ok", r.status_code == 200, str(r.status_code))
    data = r.json()
    _assert("R2 relation has source_entity ref",
            data.get("source_entity") is not None,
            f"keys={list(data.keys())}")
    _assert("R2 relation has target_entity ref",
            data.get("target_entity") is not None,
            f"keys={list(data.keys())}")


# ── R3：AI 消息摘要 ─────────────────────────────────────

def test_summary_basic_shape():
    """摘要生成：5 字段齐全。"""
    import sqlite3
    db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "mbse.db"))
    row = db.execute("SELECT id FROM messages WHERE role='assistant' AND card_data IS NOT NULL "
                     "AND card_data != '' ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    if not row:
        _assert("R3 setup: have msg", False, "no assistant messages")
        return
    mid = row[0]
    r = c.post(f"/api/messages/{mid}/summary")
    _assert("R3 summary endpoint", r.status_code == 200, str(r.status_code))
    s = r.json().get("summary", {})
    fields = {"headline", "bullets", "artifacts", "stats", "source"}
    _assert("R3 summary has 5 fields", fields.issubset(s.keys()),
            f"missing={fields - s.keys()}")
    _assert("R3 bullets <=5", isinstance(s.get("bullets"), list) and len(s["bullets"]) <= 5,
            f"len={len(s.get('bullets', []))}")
    _assert("R3 source=rule", s.get("source") == "rule", str(s.get("source")))


def test_summary_persists_to_card_data():
    """摘要应写入 messages.card_data.summary 字段。"""
    import sqlite3
    db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "mbse.db"))
    row = db.execute("SELECT id FROM messages WHERE role='assistant' AND card_data IS NOT NULL "
                     "AND card_data != '' ORDER BY id DESC LIMIT 1").fetchone()
    db.close()
    mid = row[0]
    c.post(f"/api/messages/{mid}/summary")  # 调用一次
    # 校验落库
    db = sqlite3.connect(os.path.join(os.path.dirname(__file__), "..", "mbse.db"))
    cd_raw = db.execute("SELECT card_data FROM messages WHERE id=?", (mid,)).fetchone()[0]
    db.close()
    cd = json.loads(cd_raw) if cd_raw else {}
    _assert("R3 summary persisted to card_data.summary",
            isinstance(cd.get("summary"), dict) and "headline" in cd.get("summary", {}),
            f"summary={cd.get('summary')}")


def test_summary_404_for_nonexistent_msg():
    r = c.post("/api/messages/999999/summary")
    _assert("R3 404 for nonexistent msg", r.status_code == 404, str(r.status_code))


# ── 主流程 ─────────────────────────────────────────────

def run_all():
    test_doc_lifecycle_create_deprecate_restore()
    test_doc_lifecycle_batch()
    test_doc_lifecycle_archive()
    test_doc_lifecycle_include_deprecated()
    test_entity_provenance()
    test_entity_provenance_404()
    test_relation_provenance()
    test_summary_basic_shape()
    test_summary_persists_to_card_data()
    test_summary_404_for_nonexistent_msg()

    passed = sum(1 for r in results if r[0] == "PASS")
    total = len(results)
    print("=" * 70)
    print(f"P0 Contract Tests: {passed}/{total} PASS")
    print("=" * 70)
    for tag, name, detail in results:
        marker = "✅" if tag == "PASS" else "❌"
        print(f"{marker} {tag:4s} {name:55s} {detail}")
    print("=" * 70)
    return passed == total


if __name__ == "__main__":
    ok = run_all()
    sys.exit(0 if ok else 1)