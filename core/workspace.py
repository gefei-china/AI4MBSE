# -*- coding: utf-8 -*-
"""工作根沙箱：模型自扩展产物的**唯一落地目录**（2026-11-07，P2 前置）。

## 为什么不能靠"改 file_tools 的允许范围"

`file_tools` 头部写明「安全边界（用户已确认『任意路径』）」——这是**业务需求**：
用户要让 AI 处理他自己磁盘上的文档（D 盘的项目资料、桌面的 PDF）。
把它收紧成"只能写 data/workspace"，等于把已有的文件能力砍掉。

所以本模块的定位是**新增一个受约束的容器**，而不是收紧既有工具：

| 谁 | 允许范围 |
|---|---|
| `file_write` / `file_read`（人工在用） | 任意路径（不变） |
| **模型自注册能力**（capability）的脚本与数据 | **只在 `data/workspace` 内** |
| `skill_script_run`（跑人工审过的既有脚本） | 技能包目录内（已有约束） |

## 三层约束（缺一层就等于没有）

1. **落盘位置**：能力脚本一律写到 `data/workspace/capabilities/<name>/`
2. **执行 cwd**：子进程工作目录固定为能力自己的目录，环境变量给出根路径
   ⇒ 脚本里的相对路径天然落在沙箱内
3. **静态审查**（`core/code_guard.py`）：注册前 AST 扫描，拒绝绝对路径写、
   `open(...,'w')` 于根外、subprocess/socket/eval 等 —— 因为"真解释器"跑脚本时
   第 1、2 层都拦不住 `open("D:/x")`，只有静态审查能。

> ⚠️ 必须说清这层的**能力边界**：静态审查拦得住"直白的越权"，
> 拦不住刻意混淆的代码（字符串拼接路径、反射）。因此它只用于
> **模型自注册的新能力**（第一次注册时审一次），而**不是**运行时沙箱。
> 真正的运行时隔离要靠容器/独立用户；这一点不掩饰。
"""
from __future__ import annotations

import os

__all__ = ["root", "ensure_root", "resolve_in_root", "is_in_root", "rel_of", "ENV_KEY"]

#: 传给子进程的环境变量名（脚本可据此定位自己的沙箱根）
ENV_KEY = "MBSE_WORKSPACE"


def root() -> str:
    """工作根绝对路径：`<BASE_DIR>/data/workspace`。

    ⚠️ 用 `core.config.BASE_DIR` 而不是 `__file__`：BASE_DIR 已处理"被别处 import"与
       环境变量覆盖，且与 `db` 路径同一口径（避免出现"库在这、沙箱在那"）。
    """
    try:
        from core import config as _cfg
        base = _cfg.BASE_DIR
    except Exception:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    return os.path.join(base, "data", "workspace")


def ensure_root(sub: str = "") -> str:
    """确保根（或根下子目录）存在，返回绝对路径。"""
    p = root() if not sub else os.path.join(root(), sub)
    os.makedirs(p, exist_ok=True)
    return p


def is_in_root(path: str) -> bool:
    """`path` 是否落在工作根内（realpath 前缀比较，`D:/a/bc` 不会被误判为在 `D:/a/b` 内）。"""
    if not path or not str(path).strip():
        return False
    try:
        r = os.path.realpath(root())
        p = os.path.realpath(os.path.abspath(os.path.expanduser(str(path).strip())))
    except Exception:
        return False
    return p == r or p.startswith(r + os.sep)


def resolve_in_root(path: str, must_exist: bool = False) -> str:
    """把 `path` 解析为工作根内的绝对路径；越界抛 `PermissionError`。

    相对路径按**工作根**解析（`x.txt` → `<root>/x.txt`），
    绝对路径必须已在根内（否则**拒绝**，不做"悄悄搬到根内"的隐式改写 ——
    隐式改写会让用户以为文件写到了他给的位置）。
    """
    if not path or not str(path).strip():
        raise ValueError("path 不能为空")
    s = os.path.expanduser(str(path).strip())
    p = s if os.path.isabs(s) else os.path.join(root(), s)
    ap = os.path.abspath(p)
    if not is_in_root(ap):
        raise PermissionError(
            "路径越出工作沙箱：%s（自注册能力只允许在 %s 内读写）" % (path, root()))
    if must_exist and not os.path.exists(ap):
        raise FileNotFoundError("路径不存在：%s" % ap)
    return ap


def rel_of(path: str) -> str:
    """返回相对工作根的路径（用于展示/审计；越界返回原值）。"""
    try:
        return os.path.relpath(os.path.abspath(path), root())
    except Exception:
        return str(path)
