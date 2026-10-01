"""基础通用文件操作工具集（file_*）——系统文件读写/增删能力。

设计（对齐平台工具注册表标准，Agent 与工作流双链路可用）：
- 读类：file_list / file_read        —— side_effect=read，任意路径
- 写类：file_write / file_append / file_mkdir —— side_effect=write（写意图注入 + L2 HIL 人工确认门控）
- 破坏类：file_delete                —— side_effect=destructive（必须人工确认，TR-P2b 全局门控）

安全边界（用户已确认「任意路径」）：
- 允许操作系统任意路径的业务文件；
- 但对平台自身「运行时关键文件」做删除/覆盖保护（数据库 / 配置文件 / 入口脚本），
  防止 Agent 或工作流误操作导致平台自毁；读类操作不设限。
- 单次读取上限 MAX_READ_CHARS，递归列举上限 MAX_LIST_ITEMS / MAX_LIST_DEPTH，防打爆上下文。
"""
import json
import os

from core.fs_guard import bounded_rmtree, bounded_unlink

# ── 工具元数据（注册表唯一事实源，与 database._seed_builtin_tools 共用）──
FILE_TOOL_DEFS = [
    {
        "name": "file_list",
        "description": "列出目录下的文件与子目录（基础文件操作，支持递归，返回名称/类型/大小清单）",
        "side_effect": "read", "risk_level": "low", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录绝对路径或相对路径"},
            "recursive": {"type": "boolean", "description": "是否递归列出子目录（默认 false）"}},
            "required": ["path"]},
    },
    {
        "name": "file_read",
        "description": "读取文本文件内容（UTF-8，支持从文件头读取，超出上限自动截断并提示）",
        "side_effect": "read", "risk_level": "low", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件绝对路径或相对路径"},
            "max_chars": {"type": "integer", "description": "最多读取字符数（默认 100000）"}},
            "required": ["path"]},
    },
    {
        "name": "file_write",
        "description": "创建或覆盖写入文本文件（UTF-8，自动创建父目录；写操作，需写意图或人工确认）",
        "side_effect": "write", "risk_level": "medium", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件绝对路径或相对路径"},
            "content": {"type": "string", "description": "要写入的文件内容"}},
            "required": ["path", "content"]},
    },
    {
        "name": "file_append",
        "description": "追加文本到文件末尾（文件不存在则创建，UTF-8；写操作，需写意图或人工确认）",
        "side_effect": "write", "risk_level": "medium", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "文件绝对路径或相对路径"},
            "content": {"type": "string", "description": "要追加的文件内容"}},
            "required": ["path", "content"]},
    },
    {
        "name": "file_mkdir",
        "description": "创建目录（可递归创建多级目录；写操作，需写意图或人工确认）",
        "side_effect": "write", "risk_level": "low", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "目录绝对路径或相对路径"},
            "recursive": {"type": "boolean", "description": "是否递归创建（默认 true）"}},
            "required": ["path"]},
    },
    {
        "name": "file_delete",
        "description": "删除文件或目录（目录需为空或显式 recursive=true；destructive 高风险操作，必须人工确认）",
        "side_effect": "destructive", "risk_level": "high", "version": "v1.0",
        "input_schema": {"type": "object", "properties": {
            "path": {"type": "string", "description": "待删除文件或目录路径"},
            "recursive": {"type": "boolean", "description": "删除目录时是否递归删除（默认 false）"}},
            "required": ["path"]},
    },
]

FILE_TOOL_NAMES = {d["name"] for d in FILE_TOOL_DEFS}

# ── 运行时安全限额 ──
MAX_READ_CHARS = 200_000        # 单次读取上限（字符）
DEFAULT_READ_CHARS = 100_000    # file_read 默认 max_chars
MAX_LIST_ITEMS = 500            # 单次列举条目上限
MAX_LIST_DEPTH = 3              # 递归列举深度上限

# ── 平台运行时关键文件保护（删除/覆盖拒绝，读不限）──
_CRITICAL_SUFFIX = (".db", ".db-wal", ".db-shm", ".sqlite", ".sqlite3")


def _resolve(p: str) -> str:
    """规范化绝对路径（~ 展开 + abspath）。"""
    if not p or not str(p).strip():
        raise ValueError("path 不能为空")
    return os.path.abspath(os.path.expanduser(str(p).strip()))


def _critical_files() -> set:
    """平台自身运行时关键文件集合（动态计算，含环境变量覆盖的 DB 路径）。"""
    out = set()
    try:
        from core import config as _cfg
        out.add(os.path.abspath(_cfg.get("database", "path")))
        out.add(os.path.abspath(_cfg.CONFIG_PATH))
        out.add(os.path.abspath(os.path.join(_cfg.BASE_DIR, "main.py")))
        out.add(os.path.abspath(os.path.join(_cfg.BASE_DIR, "core", "config.py")))
        for f in os.listdir(_cfg.BASE_DIR):
            if f.lower().endswith(_CRITICAL_SUFFIX):
                out.add(os.path.abspath(os.path.join(_cfg.BASE_DIR, f)))
    except Exception:
        pass
    return {p for p in out if p}


def _is_critical(path: str) -> bool:
    """是否命中平台运行时关键文件（禁止覆盖/删除）。"""
    ap = _resolve(path)
    if ap in _critical_files():
        return True
    return ap.lower().endswith(_CRITICAL_SUFFIX)


def _dir_contains_critical(dpath: str) -> bool:
    """递归检查目录下是否含关键文件（删除目录前的保护）。"""
    try:
        for root, _dirs, files in os.walk(dpath):
            for f in files:
                fp = os.path.abspath(os.path.join(root, f))
                if fp in _critical_files() or fp.lower().endswith(_CRITICAL_SUFFIX):
                    return True
    except Exception:
        pass
    return False


def _size_human(n: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.0f}{unit}" if unit == "B" else f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def _fmt_list(dpath: str, recursive: bool, depth: int = 0) -> list:
    """收集目录条目（受 MAX_LIST_ITEMS / MAX_LIST_DEPTH 限制）。"""
    entries = []
    try:
        items = sorted(os.listdir(dpath))
    except PermissionError:
        return [{"error": f"权限不足，无法读取 {dpath}"}]
    except FileNotFoundError:
        return [{"error": f"目录不存在: {dpath}"}]
    for name in items:
        if len(entries) >= MAX_LIST_ITEMS:
            entries.append({"note": f"条目超过 {MAX_LIST_ITEMS} 上限，已截断"})
            break
        full = os.path.join(dpath, name)
        try:
            if os.path.isdir(full):
                entries.append({"name": name, "type": "dir", "size": ""})
                if recursive and depth < MAX_LIST_DEPTH:
                    for sub in _fmt_list(full, True, depth + 1):
                        if len(entries) >= MAX_LIST_ITEMS:
                            break
                        entries.append({**sub, "name": f"{name}/{sub['name']}"})
            else:
                entries.append({"name": name, "type": "file",
                                "size": _size_human(os.path.getsize(full))})
        except OSError:
            entries.append({"name": name, "type": "?", "size": ""})
    return entries


# ── 各工具实现（返回 {"ok": bool, "result": str}，异常兜底为 result 错误）──
def _do_list(args: dict) -> dict:
    path = _resolve(args.get("path"))
    if not os.path.isdir(path):
        return {"ok": False, "result": f"目录不存在或不是目录: {path}"}
    entries = _fmt_list(path, bool(args.get("recursive")))
    lines = [f"{e['type']:4s} {e['size'] or '':>8s}  {e['name']}" for e in entries]
    return {"ok": True, "result": f"目录 {path} 共 {len(lines)} 项：\n" + "\n".join(lines[:MAX_LIST_ITEMS])}


def _do_read(args: dict) -> dict:
    path = _resolve(args.get("path"))
    if not os.path.isfile(path):
        return {"ok": False, "result": f"文件不存在或不是文件: {path}"}
    try:
        max_chars = int(args.get("max_chars") or DEFAULT_READ_CHARS)
    except (TypeError, ValueError):
        max_chars = DEFAULT_READ_CHARS
    max_chars = min(max(max_chars, 0), MAX_READ_CHARS)
    try:
        with open(path, "rb") as f:
            raw = f.read(MAX_READ_CHARS * 4)  # 预读防止超长文件全量进内存
    except PermissionError:
        return {"ok": False, "result": f"权限不足，无法读取: {path}"}
    text = raw.decode("utf-8", errors="replace")
    truncated = False
    if len(text) > max_chars:
        text, truncated = text[:max_chars], True
    if "\x00" in text and len(text) > 0 and text.count("\x00") > len(text) * 0.05:
        return {"ok": False, "result": f"{path} 疑似二进制文件，仅支持文本文件读取"}
    note = f"\n…（已截断，仅显示前 {len(text)} 字符）" if truncated else ""
    return {"ok": True, "result": f"{path} 内容：\n{text}{note}"}


def _do_write(args: dict) -> dict:
    path = _resolve(args.get("path"))
    if _is_critical(path):
        return {"ok": False, "result": f"拒绝写入：{path} 为平台运行时关键文件（数据库/配置/入口脚本）"}
    content = str(args.get("content") or "")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(content)
    except (PermissionError, OSError) as e:
        return {"ok": False, "result": f"写入失败: {e}"}
    return {"ok": True, "result": f"已写入 {len(content)} 字符 → {path}"}


def _do_append(args: dict) -> dict:
    path = _resolve(args.get("path"))
    if _is_critical(path):
        return {"ok": False, "result": f"拒绝追加：{path} 为平台运行时关键文件（数据库/配置/入口脚本）"}
    content = str(args.get("content") or "")
    try:
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(content)
    except (PermissionError, OSError) as e:
        return {"ok": False, "result": f"追加失败: {e}"}
    return {"ok": True, "result": f"已追加 {len(content)} 字符 → {path}"}


def _do_mkdir(args: dict) -> dict:
    path = _resolve(args.get("path"))
    recursive = bool(args.get("recursive", True))
    try:
        if recursive:
            os.makedirs(path, exist_ok=True)
        else:
            os.mkdir(path)
    except (PermissionError, OSError) as e:
        return {"ok": False, "result": f"创建目录失败: {e}"}
    return {"ok": True, "result": f"已创建目录: {path}"}


def _do_delete(args: dict) -> dict:
    path = _resolve(args.get("path"))
    if _is_critical(path):
        return {"ok": False, "result": f"拒绝删除：{path} 为平台运行时关键文件（数据库/配置/入口脚本）"}
    recursive = bool(args.get("recursive"))
    if os.path.isfile(path):
        # 看门狗删除：防 WorkBuddy 沙箱 tsbx.dll 挂钩 DeleteFileW → 回收站语义死锁
        # （见 core/fs_guard.py）。用户删除与临时文件不同——超时/失败要**如实返回**，
        # 不能让用户以为删成功其实没删。bounded_unlink 返回 False 即视为删除未完成。
        if bounded_unlink(path):
            return {"ok": True, "result": f"已删除文件: {path}"}
        return {"ok": False, "result": f"删除超时或失败（可能被系统保护，请重试）: {path}"}
    if os.path.isdir(path):
        if not recursive:
            if os.listdir(path):
                return {"ok": False,
                        "result": f"目录非空（{len(os.listdir(path))} 项），删除目录需显式 recursive=true"}
            try:
                os.rmdir(path)
            except (PermissionError, OSError) as e:
                return {"ok": False, "result": f"删除失败: {e}"}
            return {"ok": True, "result": f"已删除空目录: {path}"}
        if _dir_contains_critical(path):
            return {"ok": False, "result": f"拒绝递归删除：{path} 内含平台运行时关键文件（数据库/配置）"}
        # 递归删除同样看门狗化：shutil.rmtree 内部逐个 os.remove，同样暴露 tsbx 死锁
        if bounded_rmtree(path):
            return {"ok": True, "result": f"已递归删除目录: {path}"}
        return {"ok": False, "result": f"递归删除超时或失败（可能被系统保护，请重试）: {path}"}
    return {"ok": False, "result": f"路径不存在: {path}"}


_HANDLERS = {
    "file_list": _do_list,
    "file_read": _do_read,
    "file_write": _do_write,
    "file_append": _do_append,
    "file_mkdir": _do_mkdir,
    "file_delete": _do_delete,
}


def exec_file_tool(name: str, arguments: dict | None = None) -> dict:
    """统一入口：执行文件工具，返回 {"ok": bool, "result": str}。异常不抛出。"""
    args = arguments or {}
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"ok": False, "result": f"未知文件工具: {name}"}
    try:
        return handler(args)
    except Exception as e:
        return {"ok": False, "result": f"文件工具执行失败: {str(e)[:200]}"}
