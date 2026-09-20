# -*- coding: utf-8 -*-
"""验证「默认模型切换后实际调用是否生效」（在 8001 新代码上运行）。

用法：python tests/manual_verify/verify_default_llm.py
1) 对话：llm_client.chat(无 provider_id) → 走 get_default_provider()（实时查 DB）
2) 向量：Embedder()._probe_api() → 按 model_type='embedding' 选默认
"""
import sys, os
ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sqlite3
from llm import llm_client

DB = os.path.join(ROOT, "mbse.db")

def db_defaults():
    c = sqlite3.connect(DB)
    c.row_factory = sqlite3.Row
    chat = c.execute("SELECT id,name,model_name,is_default FROM llm_providers "
                     "WHERE model_type='chat' AND is_default=1 AND status='active'").fetchone()
    emb = c.execute("SELECT id,name,model_name,is_default FROM llm_providers "
                    "WHERE model_type='embedding' AND is_default=1 AND status='active'").fetchone()
    c.close()
    return dict(chat) if chat else None, dict(emb) if emb else None

def check_chat():
    """对话默认链路：无 provider_id 调用 → 看实际使用的 provider/model。"""
    db_chat, _ = db_defaults()
    resp = llm_client.chat([{"role": "user", "content": "你好"}], _intent="verify_default")
    meta = resp.get("_meta") or {}
    return {
        "db_chat_default": f"{db_chat['name']} ({db_chat['model_name']})" if db_chat else None,
        "actual_provider": meta.get("provider"),
        "actual_model": meta.get("model"),
        "used_mock": meta.get("used_mock"),
    }

def check_embedding():
    """向量默认链路：Embedder probe → 实际选中的 embedding 模型。"""
    from knowledge_pipeline.embedder import Embedder
    from database import get_db
    conn = get_db()
    e = Embedder()
    e._probe_api(conn)
    conn.close()
    api = e._api  # (base_url, api_key, model_name)
    _, db_emb = db_defaults()
    return {
        "db_embedding_default": f"{db_emb['name']} ({db_emb['model_name']})" if db_emb else None,
        "actual_embedding_model": api[2] if api else None,
        "api_configured": bool(api),
    }

if __name__ == "__main__":
    print("=== 对话默认链路 ===")
    print(check_chat())
    print("=== 向量默认链路 ===")
    print(check_embedding())
