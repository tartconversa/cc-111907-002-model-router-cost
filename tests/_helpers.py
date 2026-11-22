"""测试公共夹具：每个用例使用独立临时工作目录，互不污染。"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

# -I 隔离模式下仓库根不在 sys.path，测试夹具显式插入（可信代码路径）。
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from settlement.importer import import_sources  # noqa: E402
from settlement.store import Store  # noqa: E402

INPUTS = ROOT / "inputs"


class SettlementTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.workdir = Path(self.tmp.name) / "wd"
        self.store = Store(self.workdir)
        self.store.init()
        with self.store.transaction():
            self.import_summary = import_sources(self.store, INPUTS)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def fresh_store(self) -> Store:
        """模拟新进程：新建 Store 对象并从磁盘加载。"""
        store = Store(self.workdir)
        store.init()
        return store

    def write_dataset(self, name: str, rows: list, manifest: bool = False) -> Path:
        """在独立目录写一份可扩充契约数据，用于导入新事实。"""
        d = Path(self.tmp.name) / name
        d.mkdir(parents=True, exist_ok=True)
        (d / f"{name}.json").write_text(
            json.dumps(rows, ensure_ascii=False), encoding="utf-8")
        return d

    def use_tight_budget(self, care_fen: int = 1500) -> None:
        """通过可重建的事实修订事件收紧 care 预算。"""
        with self.store.transaction():
            row = dict(self.store.fact("teams", "care|2026-11"))
            row["budget_fen"] = care_fen
            self.store.override_fact(
                "teams", "care|2026-11", row, "测试收紧预算")

    def assertBudgetIdentity(self, store: Store, team: str, period: str = "2026-11"):
        from settlement.ledger import team_ledger
        lg = team_ledger(store, team, period)
        self.assertEqual(
            lg["committed_fen"] + lg["reserved_held_fen"] + lg["available_fen"],
            lg["budget_fen"],
        )
