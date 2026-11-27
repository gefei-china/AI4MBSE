# -*- coding: utf-8 -*-
"""进程测试态判定 —— 「真实使用中永不使用 Mock」的唯一权威。

## 为什么需要这个模块（2026-11-27 事故）

`llm.force_mock` 是**进程级全局开关**，且可被 `PUT /api/system/config/static`
在运行时打开。它的设计初衷是"回归测试不依赖外部网络"，但没有任何机制把
"测试"与"正在给用户干活的生产进程"区分开。

实测事故（conv 1000000246，2026-11-27）：
  10:19 用户发起低空预警雷达建模（真实任务，进行中）
  10:20 一个**验证脚本**在同一个服务进程上 PUT `llm.force_mock=true`
  10:21 该会话其后 11 次 LLM 调用全部 `used_mock=1`（延迟 3~10ms、输出固定 161 token）
  ⇒ 模型被换成固定样板 ⇒ 待办连续 2 次无进展 ⇒ 闸门判定"卡死"强制放行
  ⇒ 用户拿到 841 字与需求毫无关系的「3 个方案对比」，已生成的 646 字正文作废。

根因不是"脚本误操作"，而是**开关本身没有作用域**：它能作用于真实用户请求。

## 判据

`is_test_mode()` 只认**进程启动时显式声明**的测试态：

  - 环境变量 `MBSE_TEST_MODE` ∈ {1,true,yes,on}
  - 环境变量 `MBSE_LLM_FORCE_MOCK` ∈ 同上（历史部署通道，语义等价）
  - 命令行含 `--test-mode`（供 `python main.py --test-mode` 独立实例用）

**刻意不做**的两件事：
  1. 不因为 `pytest` 在 `sys.modules` 里就判测试态 —— 那会让"在测试进程里
     跑起来的一段生产代码"悄悄拿到 Mock，缺陷会以"测试通过"的形式隐藏。
  2. 不在运行期改变判定结果 —— 进程是测试实例还是服务实例，**启动即定死**，
     运行期没有任何 API 能把它变成测试态（这正是事故的入口）。

## 效果

- 非测试态：即使配置文件里 `llm.force_mock=true`，`llm.chat()` 也**不走 Mock**，
  照常走真实调用（无 key 则抛 `LLMUnavailableError`，不编造内容）；
  同时 `PUT /api/system/config/static` 拒绝把该键写成 true（409）。
- 测试态：行为与改动前完全一致（回归测试仍不依赖网络）。
"""
from __future__ import annotations

import os
import sys

_TRUE = ("1", "true", "yes", "on")

# 进程级缓存：一次判定，终身不变。运行期改环境变量也不影响（防"运行期提权"）。
_TEST_MODE: bool | None = None
_SOURCE: str = ""


def _env_true(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in _TRUE


def _detect() -> tuple[bool, str]:
    if _env_true("MBSE_TEST_MODE"):
        return True, "env:MBSE_TEST_MODE"
    if _env_true("MBSE_LLM_FORCE_MOCK"):
        return True, "env:MBSE_LLM_FORCE_MOCK"
    if any(str(a).strip() == "--test-mode" for a in (sys.argv or [])):
        return True, "argv:--test-mode"
    return False, "production"


def is_test_mode() -> bool:
    """本进程是否处于显式测试态（启动即定死，运行期不可变）。"""
    global _TEST_MODE, _SOURCE
    if _TEST_MODE is None:
        _TEST_MODE, _SOURCE = _detect()
    return bool(_TEST_MODE)


def source() -> str:
    """判定来源（写进 /api/system/config 的 guards 段，便于运维确认）。"""
    if _TEST_MODE is None:
        is_test_mode()
    return _SOURCE or "production"


def describe() -> dict:
    """给配置查询接口用的只读快照。"""
    return {
        "test_mode": is_test_mode(),
        "source": source(),
        "force_mock_effective": is_test_mode(),
        "why": ("进程以测试态启动，llm.force_mock 可生效（仅供回归/门禁使用）"
                if is_test_mode() else
                "进程为服务态：llm.force_mock 一律不生效，真实使用中不会产出 Mock 内容"),
    }


def allow_force_mock() -> bool:
    """`llm.force_mock` 是否允许生效。唯一真源，勿在别处重写判定。"""
    return is_test_mode()


def force_mock_active() -> bool:
    """配置写 true **且** 本进程是测试态 —— 两者都满足才算 Mock 生效。

    全仓所有 `force_mock` 消费点都应改用本函数，而不是各自
    `_cfg.as_bool("llm", "force_mock", False)`：后者只看配置，会让服务态进程
    被一次运行时写入改行为（正是 2026-11-27 事故）。门禁
    `tools/verify/verify_mock_scope.py` 把这个「唯一消费点」锁死。
    """
    if not is_test_mode():
        return False
    try:
        from core import config as _cfg
        return bool(_cfg.as_bool("llm", "force_mock", False))
    except Exception:
        return False

