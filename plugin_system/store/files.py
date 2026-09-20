"""插件产物文件读写：SKILL.md / server.json 落盘与读取。"""
import json
import os

from plugin_system.store.base import PLUGIN_DIR, _ensure_plugin_dir


def save_server_json(plugin_id, server):
    """MCP 插件的 server.json 落盘（与 base_url/凭证解耦，对齐设计方案 §3.4）。"""
    d = _ensure_plugin_dir(plugin_id)
    path = os.path.join(d, "server.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(server, f, ensure_ascii=False, indent=2)
    return path


def read_server_json(plugin_id):
    path = os.path.join(PLUGIN_DIR, plugin_id, "server.json")
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_skill_md(plugin_id, content):
    d = _ensure_plugin_dir(plugin_id)
    path = os.path.join(d, "SKILL.md")
    with open(path, "w", encoding="utf-8") as f:
        f.write(content or "")
    return path


def read_skill_md(plugin_id):
    path = os.path.join(PLUGIN_DIR, plugin_id, "SKILL.md")
    if not os.path.exists(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        return f.read()
