# -*- coding: utf-8 -*-
"""P2-1 misevolution 仓库：misevolution_baselines / misevolution_checks 的唯一读写面。

纪律（同其它域 repo）：
- 只做数据访问，不含业务编排（编排在 routers/monitor.py 的端点与
  core/config_promotion.py 的晋升复测钩子）；
- 连接注入（构造传入，路由侧由 Depends(db_session) 提供）；
- 写操作经 BaseRepo.execute（SQLite 写锁重试），且**每个写方法自带 commit**
  ——BaseRepo.execute 本身不提交（路由路径靠 db_session 兜底），但晋升复测
  将来若被非路由路径消费（作业/脚本），没有提交者就会整行丢失（P1-4 在
  sandbox_repo 上实测踩过：sandbox_runs 0 行）；
- **信号计算不在这里**：core/misevolution.py 是只读引擎，本 repo 只存/取
  快照 JSON。快照必须与记录时刻一致——事后重算会因窗口滚动变得与当时结论
  不符（基线的意义正是「过去那个时刻的样子」）；
- **本 repo 只碰 misevolution_baselines / misevolution_checks 两张表**——
  监控的「零生产副作用」在数据面上可断言（门禁 E 段静态 + 行为级证明）。
"""
import json
from typing import Optional

from repositories.base import BaseRepo


class MisevolutionRepo(BaseRepo):
    """基线冻结/读取 + 复测记录。"""

    # ── 基线 ──
    def freeze_baseline(self, *, epoch: str, signals: dict, note: str = "",
                        frozen_by: str = "") -> dict:
        """冻结当前信号快照为基线。epoch 标注冻结理由（人写，如 pre_p2_promotions）。"""
        run_id = self.execute(
            "INSERT INTO misevolution_baselines (epoch, signals_json, note, frozen_by) "
            "VALUES (?,?,?,?)",
            (str(epoch), json.dumps(signals, ensure_ascii=False),
             str(note or ""), str(frozen_by or "")))
        self.conn.commit()
        return self.get_baseline(int(run_id)) or {"id": int(run_id)}

    def get_baseline(self, baseline_id: int) -> Optional[dict]:
        row = self.one("SELECT * FROM misevolution_baselines WHERE id=?",
                       (int(baseline_id),))
        return self._with_signals(row) if row else None

    def latest_baseline(self) -> Optional[dict]:
        """当前基线 = 最新一条（基线是状态不是流水：同一时刻只认一个当前基线）。"""
        row = self.one("SELECT * FROM misevolution_baselines ORDER BY id DESC LIMIT 1")
        return self._with_signals(row) if row else None

    def list_baselines(self, limit: int = 20) -> list:
        lim = max(1, min(int(limit or 20), 100))
        rows = self.rows("SELECT * FROM misevolution_baselines ORDER BY id DESC LIMIT ?",
                         (lim,))
        return [self._with_signals(r) for r in rows]

    # ── 复测记录 ──
    def insert_check(self, *, epoch: str, source: str, signals: dict,
                     comparison: dict, verdict: str, detail: str = "") -> dict:
        """记一行复测（晋升复测 / 手动 / CI）。signals/comparison 原样存快照。"""
        check_id = self.execute(
            "INSERT INTO misevolution_checks "
            "(epoch, source, signals_json, comparison_json, verdict, detail) "
            "VALUES (?,?,?,?,?,?)",
            (str(epoch), str(source or "manual"),
             json.dumps(signals, ensure_ascii=False),
             json.dumps(comparison or {}, ensure_ascii=False),
             str(verdict or ""), str(detail or "")))
        self.conn.commit()
        return self.get_check(int(check_id)) or {"id": int(check_id)}

    def get_check(self, check_id: int) -> Optional[dict]:
        row = self.one("SELECT * FROM misevolution_checks WHERE id=?", (int(check_id),))
        return self._with_json(row) if row else None

    def list_checks(self, limit: int = 20) -> list:
        """复测记录（倒序）。返回**解析后**的视图：signals/comparison 已是 dict
        （面板直接用；坏 JSON 降级空 dict，不崩）。"""
        lim = max(1, min(int(limit or 20), 100))
        rows = self.rows("SELECT * FROM misevolution_checks ORDER BY id DESC LIMIT ?",
                         (lim,))
        return [self._with_json(r) for r in rows]

    def count_by_verdict(self) -> dict:
        rows = self.rows("SELECT verdict, COUNT(*) AS n FROM misevolution_checks "
                         "GROUP BY verdict")
        return {r["verdict"]: r["n"] for r in rows}

    # ── 内部 ──
    @staticmethod
    def _with_signals(row) -> dict:
        out = dict(row)
        try:
            out["signals"] = json.loads(out.get("signals_json") or "{}")
        except Exception:
            out["signals"] = {}      # 坏 JSON ⇒ 空快照（上层按无基线处理，不崩）
        return out

    @staticmethod
    def _with_json(row) -> dict:
        out = dict(row)
        for src, dst in (("signals_json", "signals"), ("comparison_json", "comparison")):
            try:
                out[dst] = json.loads(out.get(src) or "{}")
            except Exception:
                out[dst] = {}
        return out
