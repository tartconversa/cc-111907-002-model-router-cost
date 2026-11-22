"""额度受理：能力边界、保守预留、等待/重评估、幂等、消耗原子性、过期。"""

import json
import unittest

from _helpers import SettlementTestCase
from settlement import quota as quota_mod
from settlement.ledger import team_ledger
from settlement.reports import integrity_check
from settlement.store import StoreError





class QuotaEvaluationTests(SettlementTestCase):
    def test_only_capability_matching_models_are_eligible(self):
        ev = quota_mod.evaluate(self.store, "B2")  # reasoning，不可延迟
        models = {o["model"] for o in ev["eligible_options"]}
        self.assertEqual(models, {"large"})  # small 不具备 reasoning

    def test_cannot_switch_mandatory_work_to_cheaper_unfit_model(self):
        with self.store.transaction():
            with self.assertRaises(StoreError):
                quota_mod.reserve(self.store, "B2", model="small")

    def test_cost_range_and_risk_labels(self):
        ev_b1 = quota_mod.evaluate(self.store, "B1")
        self.assertEqual(ev_b1["cost_range_fen"], {"low": 1200, "high": 3600})
        self.assertEqual(ev_b1["overspend_risk"], "within_budget")
        self.use_tight_budget()
        ev_b5_like = quota_mod.evaluate(self.store, "B3")
        self.assertIn(ev_b5_like["overspend_risk"],
                      ("partial_overrun", "over_budget"))

    def test_default_reservation_uses_high_end_conservatively(self):
        with self.store.transaction():
            out = quota_mod.reserve(self.store, "B3")
        self.assertEqual(out["reservation"]["model"], "large")
        self.assertEqual(out["reservation"]["reserved_fen"], 10800)


class QuotaLifecycleTests(SettlementTestCase):
    def test_duplicate_submission_does_not_double_hold(self):
        with self.store.transaction():
            first = quota_mod.reserve(self.store, "B1", model="small")
            second = quota_mod.reserve(self.store, "B1", model="small")
        self.assertNotIn("idempotent_hit", first)
        self.assertTrue(second["idempotent_hit"])
        lg = team_ledger(self.store, "care", "2026-11")
        self.assertEqual(lg["reserved_held_fen"], 1200)

    def test_deferrable_request_waits_when_over_budget_no_duplicate_events(self):
        self.use_tight_budget()
        with self.store.transaction():
            quota_mod.reserve(self.store, "B1", model="small")  # 1200，仍可受理
            # B3 源数据不可延迟；本用例把它改成可延迟以验证等待清单
            self.store.fact("requests", "B3")["deferrable"] = True
            out = quota_mod.reserve(self.store, "B3", model="small")  # 3600，不足
        self.assertTrue(out["waiting"])
        self.assertIn("B3", self.store.snapshot["waiting"])
        # 再次触发（模拟重评估仍不足）不产生第二条 waiting 事件
        with self.store.transaction():
            quota_mod.reevaluate_waiting(self.store)
        waiting_events = [
            e for e in self._events() if e["type"] == "reservation.waiting"
        ]
        self.assertEqual(len(waiting_events), 1)

    def test_budget_increase_releases_waiting_request(self):
        self.use_tight_budget()
        with self.store.transaction():
            quota_mod.reserve(self.store, "B1", model="small")  # 占 1200
            self.store.fact("requests", "B3")["deferrable"] = True
            quota_mod.reserve(self.store, "B3", model="small")  # 需 3600，等待
            result = quota_mod.increase_budget(
                self.store, "care", "2026-11", 4000, "月度追加")
        self.assertNotIn("B3", self.store.snapshot["waiting"])
        self.assertEqual(
            self.store.snapshot["reservations"]["B3"]["status"], "held")
        self.assertTrue(any(r["request_id"] == "B3" for r in result["reevaluated"]))

    def test_mandatory_over_budget_creates_shortfall_but_keeps_approval(self):
        self.use_tight_budget()
        # B3 在源数据中即不可延迟；按 large 高端口径需 10800，远超可用 1248
        with self.store.transaction():
            out = quota_mod.reserve(self.store, "B3", model="large")
        # 业务仍获批（不能被报告成从未获批），同时挂差额单
        self.assertEqual(out["reservation"]["status"], "held")
        self.assertIn("SF-B3", self.store.snapshot["shortfalls"])
        self.assertEqual(
            self.store.snapshot["shortfalls"]["SF-B3"]["status"], "open")

    def test_consume_is_atomic_cost_and_release(self):
        with self.store.transaction():
            quota_mod.reserve(self.store, "B1", model="small")  # 1200
            payload = quota_mod.consume(self.store, "B1", 300, 1000, "实际用量")
        self.assertEqual(payload["consumed_fen"], 1000)
        r = self.store.snapshot["reservations"]["B1"]
        self.assertEqual(r["status"], "consumed")
        self.assertEqual(r["released_fen"], 200)
        # 配对成本调整行同事务存在（无半次处置）
        ids = {x["line_id"] for x in self.store.snapshot["manual_invoice_lines"]}
        self.assertIn("ADJ-CONSUME-B1", ids)
        self.assertTrue(integrity_check(self.store)["ok"])

    def test_cannot_consume_without_held_reservation(self):
        with self.store.transaction():
            with self.assertRaises(StoreError):
                quota_mod.consume(self.store, "B1", 1, 10)

    def test_expiration_releases_held_reservation(self):
        with self.store.transaction():
            quota_mod.reserve(self.store, "B1", model="small")
            changed = quota_mod.expire_due(
                self.store, "2026-12-01T00:00:01Z")
        self.assertEqual([p["request_id"] for p in changed], ["B1"])
        self.assertEqual(
            self.store.snapshot["reservations"]["B1"]["status"], "expired")

    def test_cancel_releases_reservation(self):
        with self.store.transaction():
            quota_mod.reserve(self.store, "B1", model="small")
            quota_mod.cancel(self.store, "B1")
        self.assertEqual(
            self.store.snapshot["reservations"]["B1"]["status"], "released")
        self.assertEqual(
            team_ledger(self.store, "care", "2026-11")["reserved_held_fen"], 0)

    def test_budget_identity_always_balances(self):
        with self.store.transaction():
            quota_mod.reserve(self.store, "B1", model="small")
            quota_mod.reserve(self.store, "B2")
            quota_mod.consume(self.store, "B1", 200, 900)
        for team in ("care", "ops"):
            lg = team_ledger(self.store, team, "2026-11")
            self.assertEqual(
                lg["committed_fen"] + lg["reserved_held_fen"] + lg["available_fen"],
                lg["budget_fen"])

    def _events(self):
        for line in self.store.events_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                yield json.loads(line)


if __name__ == "__main__":
    unittest.main()
