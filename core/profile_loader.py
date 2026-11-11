# -*- coding: utf-8 -*-
"""profile 分层装配（P1-2 / P2-2），映射通用平台 D-4「主干 + 私有化 + 领域包」。

对标 DSH ``dsh/README.zh.md:39-42`` 的 bundle 叠加与 ``cordis.patch.yml`` 的
「按 id 整块替换」。这里用 **JSON 而非 YAML**：stdlib 即可解析、零新增依赖，
符合私有化离线部署约束。

两条叠加语义刻意区分：
  * ``extends`` 链（有序，根→叶）：按 **group 整块替换** —— 后层覆盖前层同名 group，
    不做字段级合并；每次覆盖都留在 ``overrides`` 供审计。
  * ``overlays``（兄弟 patch 层）：按 key 合并；**同一 (group,key) 出现不同值即抛
    ProfileConflictError**，绝不静默取最后一个写入值。

验收判据：同一内核可派生 server / cli / offline 三形态且 kernel 层逐字节相同；
构造兄弟层冲突必须启动失败。
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from dataclasses import dataclass

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_CATALOG = os.path.join(ROOT, "profiles", "catalog.json")


class ProfileError(RuntimeError):
    pass


class ProfileConflictError(ProfileError):
    pass


@dataclass(frozen=True)
class EffectiveProfile:
    name: str
    chain: tuple
    layers: dict
    overrides: tuple
    digest: str

    def dump(self) -> dict:
        return {"name": self.name, "chain": list(self.chain),
                "layers": copy.deepcopy(self.layers),
                "overrides": list(self.overrides), "digest": self.digest}

    def layer(self, group: str) -> dict:
        return copy.deepcopy(self.layers.get(group, {}))


def _canon(obj) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"),
                      default=str)


def load_profile(name: str, catalog_path: str = None, overlays=()) -> EffectiveProfile:
    path = catalog_path or DEFAULT_CATALOG
    with open(path, encoding="utf-8") as fh:
        catalog = json.load(fh)
    profiles = catalog.get("profiles") or {}

    chain = []
    cur = name
    while cur:
        if cur in chain:
            raise ProfileError("extends 出现环: %s" % " -> ".join(chain + [cur]))
        if cur not in profiles:
            raise ProfileError("未定义的 profile: %s；可用: %s"
                               % (cur, ", ".join(sorted(profiles)) or "(空)"))
        chain.append(cur)
        cur = profiles[cur].get("extends")
    chain.reverse()                      # 根 → 叶

    layers, overrides = {}, []
    for pid in chain:
        for group, payload in (profiles[pid].get("layers") or {}).items():
            if group in layers and _canon(layers[group]) != _canon(payload):
                overrides.append({"by": pid, "group": group})
            layers[group] = copy.deepcopy(payload)   # 整块替换，不合并字段

    seen = {}
    for overlay in overlays or ():
        with open(overlay, encoding="utf-8") as fh:
            patch = json.load(fh)
        for group, payload in (patch.get("layers") or {}).items():
            for key, val in payload.items():
                k = (group, key)
                if k in seen and _canon(seen[k][0]) != _canon(val):
                    raise ProfileConflictError(
                        "兄弟层对 %s.%s 给出冲突值：%s (%s) vs %s (%s)"
                        % (group, key, _canon(seen[k][0]), seen[k][1],
                           _canon(val), patch.get("id", overlay)))
                seen[k] = (val, patch.get("id", overlay))
            layers.setdefault(group, {}).update(copy.deepcopy(payload))

    digest = hashlib.sha256(_canon(layers).encode("utf-8")).hexdigest()
    return EffectiveProfile(name=name, chain=tuple(chain), layers=layers,
                            overrides=tuple(overrides), digest=digest)


def derive_forms(forms=("server", "cli", "offline"), catalog_path: str = None) -> dict:
    """派生多形态并断言共用同一内核（kernel 层逐字节相同）。"""
    out = {f: load_profile(f, catalog_path) for f in forms}
    kernels = {f: _canon(p.layer("kernel")) for f, p in out.items()}
    if len(set(kernels.values())) != 1:
        raise ProfileError("多形态未共用同一内核: %s" % kernels)
    return out


__all__ = ["ProfileError", "ProfileConflictError", "EffectiveProfile",
           "load_profile", "derive_forms"]
