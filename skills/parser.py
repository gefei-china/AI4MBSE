"""Skill 技能包解析器（P0 平台化：上传 ZIP 技能包）。

对齐 Dify Skill_Agent / Claude Code 技能包规范：
- skill.md（必需）：frontmatter（name/description/triggers/priority/category/author）+ 指令正文
- scripts/（必需）：执行脚本（main.py / index.js 等）
- requirements.txt / package.json（可选）：依赖声明
- reference/（可选）：参考文档（RAG 增强）

安全策略：仅解析 + 存盘，**不自动执行**任何脚本；文件白名单（.py/.js/.md/.txt/.json/.yaml/.yml）。
"""
import io
import os
import re
import zipfile

ALLOWED_EXT = {".py", ".js", ".md", ".txt", ".json", ".yaml", ".yml", ".csv", ".ts", ".sh", ".toml"}
MAX_FILES = 100
MAX_FILE_BYTES = 5 * 1024 * 1024  # 单文件 5MB


class SkillParseError(Exception):
    pass


def parse_frontmatter(text: str) -> dict:
    """解析 SKILL.md 的 YAML-like frontmatter（--- 包裹的头部），缺失字段取默认值。"""
    meta = {}
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?", text, re.S)
    if m:
        body = m.group(1)
        for line in body.splitlines():
            line = line.strip()
            if not line or line.startswith("#") or ":" not in line:
                continue
            key, _, val = line.partition(":")
            key = key.strip().lower()
            val = val.strip()
            if key in ("name", "description", "category", "author", "version"):
                meta[key] = val.strip("\"'")
            elif key == "triggers":
                # 支持 ["a","b"] 或 ["a", "b"] 或 逗号分隔
                meta[key] = [t.strip().strip("\"'") for t in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_@-]+", val)]
            elif key == "priority":
                try:
                    meta[key] = int(val)
                except ValueError:
                    meta[key] = 0
            elif key in ("allowed-tools", "allowed_tools"):
                # 工具白名单：空格/逗号分隔（P2-2 唯一实现统一支持，供技能池/执行器消费）
                meta["allowed_tools"] = [
                    t.strip().strip("\"'") for t in re.split(r"[\s,]+", val) if t.strip()]
    return meta


def parse_skill_zip(data: bytes, filename: str) -> dict:
    """解析 ZIP 技能包，返回结构化结果（不落盘，由路由层决定存储）。

    返回：{name, description, triggers, category, author, version, content, files:[{path,size}]}
    """
    if not filename.lower().endswith(".zip"):
        raise SkillParseError("技能包必须是 .zip 格式")

    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        raise SkillParseError("ZIP 文件损坏或格式非法")

    names = zf.namelist()
    if len(names) > MAX_FILES:
        raise SkillParseError(f"技能包文件数超过上限（{MAX_FILES}）")

    # 找 skill.md（根目录或子目录）
    skill_md = next((n for n in names if n.endswith("skill.md") or n.endswith("SKILL.md")), None)
    if not skill_md:
        raise SkillParseError("技能包缺少 skill.md（必需文件：skill.md + scripts/）")

    raw = zf.read(skill_md)
    if len(raw) > MAX_FILE_BYTES:
        raise SkillParseError("skill.md 超过 5MB")
    text = raw.decode("utf-8", errors="replace")
    meta = parse_frontmatter(text)

    if not meta.get("name"):
        raise SkillParseError("skill.md 缺少 name 字段（frontmatter 必需）")

    # 校验并收集文件清单（白名单过滤）
    files = []
    has_scripts = False
    for n in names:
        if n.endswith("/"):
            continue
        if n.startswith("__MACOSX") or n.startswith("."):
            continue
        ext = os.path.splitext(n)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        info = zf.getinfo(n)
        if info.file_size > MAX_FILE_BYTES:
            continue
        files.append({"path": n, "size": info.file_size})
        if n.startswith("scripts/") or "/scripts/" in n:
            has_scripts = True

    return {
        "name": meta.get("name"),
        "description": meta.get("description", ""),
        "triggers": meta.get("triggers", []),
        "category": meta.get("category", ""),
        "author": meta.get("author", ""),
        "version": meta.get("version", "v1.0"),
        "content": text,          # skill.md 全文（含 frontmatter，供展示/编辑）
        "skill_type": "package",
        "files": files,
        "has_scripts": has_scripts,
        "warning": "" if has_scripts else "技能包未包含 scripts/ 目录（仅提示类 Skill，运行时无脚本可执行）",
    }


def extract_skill_package(data: bytes, dest_dir: str) -> list:
    """解压技能包到指定目录（白名单过滤，供后续脚本消费）。返回落盘文件清单。"""
    os.makedirs(dest_dir, exist_ok=True)
    saved = []
    try:
        zf = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return saved
    for n in zf.namelist():
        if n.endswith("/") or n.startswith("__MACOSX") or n.startswith("."):
            continue
        ext = os.path.splitext(n)[1].lower()
        if ext not in ALLOWED_EXT:
            continue
        info = zf.getinfo(n)
        if info.file_size > MAX_FILE_BYTES:
            continue
        # 防路径穿越：只保留相对路径
        clean = os.path.normpath(n)
        if clean.startswith("..") or os.path.isabs(clean):
            continue
        target = os.path.join(dest_dir, clean)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with open(target, "wb") as f:
            f.write(zf.read(n))
        saved.append(clean)
    return saved
