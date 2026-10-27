"""复现：会话 9629 末轮工具取回 SysML 全文，但产物库为空。

不猜测，直接拿真库里 4876 的 card_data/content + 三段 READ 的真实信封形态，
在 mbse.db 的临时副本上调用**真实** _archive_artifacts（把它吞掉的异常暴露出来）。
"""
import json
import inspect
import os
import shutil
import sqlite3
import sys
import tempfile
import traceback

sys.path.insert(0, ".")

DB = "mbse.db"
CONV = 9629
MSG = 4876


def copy_db(tmp):
    src = sqlite3.connect(DB)
    try:
        src.execute("PRAGMA wal_checkpoint(PASSIVE)")
    except Exception:
        pass
    src.close()
    shutil.copyfile(DB, tmp)
    for suf in ("-wal", "-shm"):
        if os.path.exists(DB + suf):
            shutil.copyfile(DB + suf, tmp + suf)


def main():
    tmp = os.path.join(tempfile.gettempdir(), "mbse_artifact_repro.db")
    for suf in ("", "-wal", "-shm"):
        p = tmp + suf
        if os.path.exists(p):
            os.remove(p)
    copy_db(tmp)

    c = sqlite3.connect(tmp)
    c.row_factory = sqlite3.Row
    row = c.execute("SELECT card_data, content, role FROM messages WHERE id=?", (MSG,)).fetchone()
    card = row["card_data"] or "{}"
    content = row["content"] or ""
    print("=== 4876 ===")
    print("  role=%s content_len=%d" % (row["role"], len(content)))
    cd = json.loads(card) if isinstance(card, str) else card
    print("  card keys:", sorted(cd.keys()))
    print("  intent:", cd.get("intent"))
    print("  has sections:", bool(cd.get("sections")), "| has report:", bool(cd.get("report")))
    print("  sysml_views views:", bool((cd.get("sysml_views") or {}).get("views")))
    print("  skill_hits:", cd.get("skill_hits"))
    # 正文里有没有 sysml 代码块？
    print("  content 含 ```sysml:", "```sysml" in content, "| 含 package:", "package" in content[:2000])

    # 用真实信封形态重建三段 READ 的工具结果（eof/completeFile/content 与真库一致）
    fn = "广汽方法论建模1022_master_v1_20261027044121.sysml"
    seg_head = c.execute("SELECT result FROM tool_call_logs WHERE id=4510").fetchone()["result"]
    tail_body = seg_head[seg_head.index('"content":"') + len('"content":"'):]
    # 反转义成真实文本
    tail_body = json.loads('"' + tail_body.rstrip('"') + '"') if not tail_body.endswith('\\') else tail_body
    seg3 = {"taskId": "2114940498336481282", "fileName": fn, "mimeType": "text/plain",
            "encoding": "UTF-8", "sizeBytes": "80543", "offset": 65534, "bytesRead": 15009,
            "nextOffset": None, "eof": True, "completeFile": False, "content": tail_body}
    result_text = json.dumps(seg3, ensure_ascii=False)
    print("\n=== 重建的第三段信封 ===")
    print("  len=%d eof=True content_len=%d starts_with_package=%s" %
          (len(result_text), len(tail_body), tail_body.lstrip().lower().startswith("package")))

    from agent.utils import extract_sysml_doc_from_result as esd
    doc = esd("export_sysml_v2", result_text)
    print("  extract_sysml_doc_from_result ->", None if not doc else
          {"title": doc["title"], "text_len": len(doc["text"])})
    if not doc:
        print("  ⇒ 判据未命中，不会归档（这就是空库直接原因）")
        c.close()
        return

    # 调真实 _archive_artifacts，但把它吞掉的异常暴露出来
    import agent.utils as U
    src = inspect.getsource(U._archive_artifacts)
    src = src.replace("    except Exception:\n        # 归档失败不阻断主流程（对齐既有降级原则）\n        pass",
                      "    except Exception:\n        traceback.print_exc()\n        raise")
    ns = dict(U.__dict__)
    exec(compile(src, "<patched_archive>", "exec"), ns)
    patched = ns["_archive_artifacts"]

    before = c.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=?", (CONV,)).fetchone()[0]
    print("\n=== 调用真实 _archive_artifacts（异常不吞）===")
    try:
        patched(c, CONV, MSG, card, content, cd.get("intent") or "", tool_docs=[doc])
        print("  调用成功，无异常")
    except Exception:
        print("  !! 抛出异常（被线上 except: pass 吞掉）:")
        traceback.print_exc()
    after = c.execute("SELECT COUNT(*) FROM artifacts WHERE conversation_id=?", (CONV,)).fetchone()[0]
    print("  产物条数 %d -> %d" % (before, after))
    for r in c.execute("SELECT id,kind,title,length(preview_content) pl,meta FROM artifacts WHERE conversation_id=? ORDER BY id", (CONV,)):
        print("   ", dict(r))
    c.close()
    for suf in ("", "-wal", "-shm"):
        p = tmp + suf
        if os.path.exists(p):
            try:
                os.remove(p)
            except Exception:
                pass


if __name__ == "__main__":
    main()
