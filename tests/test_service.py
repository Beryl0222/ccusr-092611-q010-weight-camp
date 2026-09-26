"""领域服务测试：门禁、冻结、权限、幂等结算、转诊、角色视图、恢复与防篡改。"""
import os
import sqlite3
import tempfile
import threading
import unittest

from weight_camp.domain import (
    ASSESSMENT_EXERCISE,
    ASSESSMENT_MEDICAL,
    ROLE_ADMIN,
    ROLE_COACH,
    ROLE_FINANCE,
    ROLE_MEDIC,
    ROLE_OWNER,
)
from weight_camp.service import CampService, ServiceError

MEDIC = {"user_id": "m1", "role": ROLE_MEDIC}
COACH = {"user_id": "c1", "role": ROLE_COACH}
FIN = {"user_id": "f1", "role": ROLE_FINANCE}
ADMIN = {"user_id": "a1", "role": ROLE_ADMIN}
E1 = {"user_id": "e1", "role": ROLE_OWNER}
E2 = {"user_id": "e2", "role": ROLE_OWNER}


class Clock:
    def __init__(self, start="2026-10-01T08:00:00+00:00"):
        self.t = start

    def __call__(self):
        return self.t


class CampTestBase(unittest.TestCase):
    clock = None

    def setUp(self):
        self.clock = Clock()
        self.store = CampService(clock=self.clock)
        self.store.create_camp(ADMIN, "camp1", "rk-camp")
        self.store.enroll(ADMIN, "e1", "camp1", "张三", 100000, "rk-enroll-1")

    def tearDown(self):
        self.store.close()

    def sign_both(self):
        self.store.submit_assessment(
            MEDIC, "e1", ASSESSMENT_MEDICAL, {"conditions": ["hypertension"]}, "rk-med")
        self.store.submit_assessment(
            COACH, "e1", ASSESSMENT_EXERCISE, {"risk": "medium"}, "rk-ex")

    def schedule_two(self):
        self.store.schedule_session(COACH, "e1", "2026-10-01T09:00:00+00:00",
                                    10000, "rk-s1")
        self.store.schedule_session(COACH, "e1", "2026-10-02T09:00:00+00:00",
                                    10000, "rk-s2")


class GateTests(CampTestBase):
    def test_cannot_schedule_without_assessments(self):
        view = self.store.enrollee_view(COACH, "e1")
        self.assertFalse(view["gates"]["can_train"])
        self.assertIn("医学评估未完成", view["gates"]["blocked_reasons"])
        with self.assertRaises(ServiceError):
            self.store.schedule_session(
                COACH, "e1", "2026-10-01T09:00:00+00:00", 10000, "rk-s1")

    def test_schedule_after_both_assessments(self):
        self.sign_both()
        self.schedule_two()
        view = self.store.enrollee_view(COACH, "e1")
        self.assertTrue(view["gates"]["can_train"])
        self.assertEqual([s["status"] for s in view["sessions"]],
                         ["scheduled", "scheduled"])

    def test_assessment_signing_authority(self):
        with self.assertRaises(ServiceError):  # 教练不能签医学评估
            self.store.submit_assessment(
                COACH, "e1", ASSESSMENT_MEDICAL, {"x": 1}, "rk-x1")
        with self.assertRaises(ServiceError):  # 医务人员不能签运动风险评估
            self.store.submit_assessment(
                MEDIC, "e1", ASSESSMENT_EXERCISE, {"x": 1}, "rk-x2")

    def test_signed_assessment_is_locked(self):
        self.sign_both()
        with self.assertRaises(ServiceError):
            self.store.submit_assessment(
                MEDIC, "e1", ASSESSMENT_MEDICAL, {"conditions": []}, "rk-med-again")
        view1 = self.store.enrollee_view(MEDIC, "e1")
        hash_before = view1["medical_assessment"]["content_hash"]
        # 补交资料可以提交，但不覆盖已签署评估
        self.store.submit_supplement(
            E1, "e1", ASSESSMENT_MEDICAL, {"ecg": "normal"}, "rk-sup")
        view2 = self.store.enrollee_view(MEDIC, "e1")
        self.assertEqual(view2["medical_assessment"]["content_hash"], hash_before)
        self.assertEqual(view2["medical_assessment"]["content"],
                         {"conditions": ["hypertension"]})
        self.assertEqual(len(view2["supplements"]), 1)


class FreezeTests(CampTestBase):
    def test_red_flag_freezes_future_sessions_and_notifies(self):
        self.sign_both()
        self.schedule_two()
        result = self.store.report_red_flag(COACH, "e1", "chest_tightness", "rk-flag")
        self.assertTrue(result["frozen"])
        self.assertEqual(set(result["frozen_sessions"]),
                         {"sess:e1:1", "sess:e1:2"})
        view = self.store.enrollee_view(COACH, "e1")
        self.assertTrue(view["gates"]["frozen"])
        self.assertIn("红旗冻结未解除", view["gates"]["blocked_reasons"])
        self.assertTrue(all(s["status"] == "frozen" for s in view["sessions"]))
        # 冻结期间不能打卡、不能排新课
        with self.assertRaises(ServiceError):
            self.store.checkin(COACH, "sess:e1:1", "rk-chk-blocked")
        with self.assertRaises(ServiceError):
            self.store.schedule_session(
                COACH, "e1", "2026-10-03T09:00:00+00:00", 10000, "rk-s3")
        # 通知指定角色且初始为待送达
        medic_notes = self.store.list_notifications(MEDIC)
        actions = {n["event_id"].split(":", 1)[0] for n in medic_notes}
        self.assertIn("red_flag", actions)
        self.assertTrue(all(n["status"] == "pending" for n in medic_notes))
        coach_actions = {n["event_id"].split(":", 1)[0]
                         for n in self.store.list_notifications(COACH)}
        self.assertIn("freeze", coach_actions)
        # 财务不应收到红旗通知
        self.assertEqual(self.store.list_notifications(FIN), [])

    def test_only_medic_can_unfreeze(self):
        self.sign_both()
        self.schedule_two()
        self.store.report_red_flag(MEDIC, "e1", "syncope", "rk-flag2")
        with self.assertRaises(ServiceError):
            self.store.unfreeze(COACH, "e1", "要求继续", "rk-u1")
        with self.assertRaises(ServiceError):
            self.store.unfreeze(ADMIN, "e1", "要求继续", "rk-u2")
        result = self.store.unfreeze(MEDIC, "e1", "心电图复查正常", "rk-u3")
        self.assertEqual(set(result["restored_sessions"]),
                         {"sess:e1:1", "sess:e1:2"})
        # 幂等重放不产生第二条解冻
        again = self.store.unfreeze(MEDIC, "e1", "心电图复查正常", "rk-u3")
        self.assertEqual(again, result)
        view = self.store.enrollee_view(COACH, "e1")
        self.assertFalse(view["gates"]["frozen"])
        self.assertTrue(all(s["status"] == "scheduled" for s in view["sessions"]))

    def test_unknown_symptom_rejected(self):
        with self.assertRaises(ServiceError):
            self.store.report_red_flag(COACH, "e1", "headache", "rk-bad-flag")

    def test_completed_session_not_frozen(self):
        self.sign_both()
        self.schedule_two()
        self.clock.t = "2026-10-01T10:00:00+00:00"  # 第一节课已过
        self.store.checkin(COACH, "sess:e1:1", "rk-chk1")
        self.store.report_red_flag(COACH, "e1", "chest_tightness", "rk-flag3")
        view = self.store.enrollee_view(COACH, "e1")
        statuses = {s["session_id"]: s["status"] for s in view["sessions"]}
        self.assertEqual(statuses["sess:e1:1"], "completed")
        self.assertEqual(statuses["sess:e1:2"], "frozen")


class SettlementTests(CampTestBase):
    def test_duplicate_checkin_settles_once(self):
        self.sign_both()
        self.schedule_two()
        first = self.store.checkin(COACH, "sess:e1:1", "rk-chk")
        replay = self.store.checkin(COACH, "sess:e1:1", "rk-chk")
        self.assertEqual(first, replay)
        # 换一个 request_key 重试：业务拒绝，且账目只有一条
        with self.assertRaises(ServiceError):
            self.store.checkin(COACH, "sess:e1:1", "rk-chk-dup")
        view = self.store.enrollee_view(FIN, "e1")
        charges = [e for e in view["ledger"]["entries"]
                   if e["kind"] == "session_charge"]
        self.assertEqual(len(charges), 1)
        self.assertEqual(view["ledger"]["charged_cents"], 10000)

    def test_concurrent_checkin_only_one_settles(self):
        self.sign_both()
        self.schedule_two()
        outcomes = []

        def worker(i):
            try:
                outcomes.append(self.store.checkin(
                    COACH, "sess:e1:1", f"rk-concurrent-{i}"))
            except ServiceError:
                outcomes.append("error")

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        successes = [o for o in outcomes if isinstance(o, dict)]
        self.assertEqual(len(successes), 1)
        view = self.store.enrollee_view(FIN, "e1")
        self.assertEqual(view["ledger"]["charged_cents"], 10000)

    def test_refund_request_and_duplicate(self):
        self.sign_both()
        self.schedule_two()
        self.store.checkin(COACH, "sess:e1:1", "rk-chk-r")
        req = self.store.request_refund(E1, "e1", 5000, "身体不适", "rk-ref")
        dup = self.store.request_refund(E1, "e1", 99999, "又来一次", "rk-ref-2")
        self.assertTrue(dup["duplicate"])
        self.assertEqual(dup["amount_cents"], 5000)
        # 非财务不能决定
        with self.assertRaises(ServiceError):
            self.store.decide_refund(ADMIN, "e1", True, "越权", "rk-d0")
        decided = self.store.decide_refund(FIN, "e1", True, "同意", "rk-d1")
        self.assertTrue(decided["settled"])
        replay = self.store.decide_refund(FIN, "e1", True, "同意", "rk-d1")
        self.assertEqual(replay, decided)
        with self.assertRaises(ServiceError):  # 不能重复决定/重复结算
            self.store.decide_refund(FIN, "e1", False, "反悔", "rk-d2")
        view = self.store.enrollee_view(FIN, "e1")
        refunds = [e for e in view["ledger"]["entries"] if e["kind"] == "refund"]
        self.assertEqual(len(refunds), 1)
        self.assertEqual(refunds[0]["amount_cents"], -5000)
        self.assertEqual(view["ledger"]["refunded_cents"], 5000)

    def test_refund_cannot_exceed_balance(self):
        with self.assertRaises(ServiceError):
            self.store.request_refund(E1, "e1", 100001, "超额", "rk-over")

    def test_only_owner_or_admin_can_request_refund(self):
        with self.assertRaises(ServiceError):
            self.store.request_refund(E2, "e1", 100, "替别人退", "rk-r-x")
        with self.assertRaises(ServiceError):
            self.store.request_refund(COACH, "e1", 100, "教练代退", "rk-r-y")

    def test_rejected_refund_settles_nothing(self):
        self.store.request_refund(E1, "e1", 1000, "测试", "rk-rej-req")
        result = self.store.decide_refund(FIN, "e1", False, "不符合", "rk-rej")
        self.assertFalse(result["settled"])
        view = self.store.enrollee_view(FIN, "e1")
        self.assertEqual(view["ledger"]["refunded_cents"], 0)


class ReferralAndPauseTests(CampTestBase):
    def test_referral_blocks_training_and_resolves(self):
        self.sign_both()
        self.schedule_two()
        with self.assertRaises(ServiceError):  # 教练不能发起转诊
            self.store.open_referral(COACH, "e1", "胸闷待查", "rk-ref-x")
        self.store.open_referral(MEDIC, "e1", "胸闷待查", "rk-ref-open")
        view = self.store.enrollee_view(COACH, "e1")
        self.assertEqual(view["status"], "referred")
        self.assertIn("转诊未闭环", view["gates"]["blocked_reasons"])
        with self.assertRaises(ServiceError):
            self.store.checkin(COACH, "sess:e1:1", "rk-chk-ref")
        with self.assertRaises(ServiceError):  # 转诊中不能重复发起
            self.store.open_referral(MEDIC, "e1", "再转一次", "rk-ref-open2")
        self.store.resolve_referral(
            MEDIC, view["referral"]["referral_id"], "专科评估无异常", "rk-ref-resolve")
        view2 = self.store.enrollee_view(COACH, "e1")
        self.assertEqual(view2["status"], "enrolled")
        self.assertTrue(view2["gates"]["can_train"])

    def test_pause_blocks_schedule(self):
        self.sign_both()
        self.store.pause(E1, "e1", "外出一周", "rk-pause")
        with self.assertRaises(ServiceError):
            self.store.schedule_session(
                COACH, "e1", "2026-10-01T09:00:00+00:00", 10000, "rk-s-paused")
        self.store.resume(E1, "e1", "rk-resume")
        self.schedule_two()


class RoleViewTests(CampTestBase):
    def test_owner_can_only_view_self(self):
        self.store.enroll(ADMIN, "e2", "camp1", "李四", 50000, "rk-enroll-2")
        with self.assertRaises(ServiceError):
            self.store.enrollee_view(E1, "e2")
        with self.assertRaises(ServiceError):
            self.store.list_enrollees(E1)
        # 本人视图正常
        self.assertEqual(self.store.enrollee_view(E1, "e1")["enrollee_id"], "e1")

    def test_owner_notifications_scoped_to_self(self):
        self.sign_both()
        self.store.request_refund(E1, "e1", 1000, "测试", "rk-ref-e1")
        self.store.enroll(ADMIN, "e2", "camp1", "李四", 50000, "rk-enroll-2")
        self.store.request_refund(E2, "e2", 1000, "测试", "rk-ref-e2")
        notes_e1 = self.store.list_notifications(E1)
        # 决定尚未作出，无通知；构造决定后验证隔离
        self.store.decide_refund(FIN, "e1", True, "同意", "rk-d-e1")
        self.store.decide_refund(FIN, "e2", False, "拒绝", "rk-d-e2")
        notes_e1 = self.store.list_notifications(E1)
        self.assertTrue(notes_e1)
        self.assertTrue(all(n["enrollee_id"] == "e1" for n in notes_e1))

    def test_notification_receipt_permissions(self):
        self.sign_both()
        self.store.report_red_flag(COACH, "e1", "chest_tightness", "rk-flag-v")
        note_id = [n["notification_id"]
                   for n in self.store.list_notifications(MEDIC)][0]
        with self.assertRaises(ServiceError):  # 教练不能签收医务通知
            self.store.mark_delivered(COACH, note_id, "rk-rec-x")
        self.store.mark_delivered(MEDIC, note_id, "rk-rec-ok")
        self.assertEqual(
            self.store.mark_delivered(MEDIC, note_id, "rk-rec-ok")["status"],
            "delivered")

        self.sign_both()
        coach_view = self.store.enrollee_view(COACH, "e1")
        self.assertNotIn("content", coach_view["medical_assessment"])
        self.assertIn("content_hash", coach_view["medical_assessment"])
        medic_view = self.store.enrollee_view(MEDIC, "e1")
        self.assertEqual(medic_view["medical_assessment"]["content"],
                         {"conditions": ["hypertension"]})

    def test_ledger_only_for_finance_admin_owner(self):
        self.assertIsNone(self.store.enrollee_view(COACH, "e1")["ledger"])
        self.assertIsNone(self.store.enrollee_view(MEDIC, "e1")["ledger"])
        self.assertIsNotNone(self.store.enrollee_view(FIN, "e1")["ledger"])
        self.assertIsNotNone(self.store.enrollee_view(ADMIN, "e1")["ledger"])
        self.assertIsNotNone(self.store.enrollee_view(E1, "e1")["ledger"])

    def test_actions_reflect_state(self):
        coach_actions = {a["action"]: a for a in
                         self.store.enrollee_view(COACH, "e1")["actions"]}
        self.assertFalse(coach_actions["schedule_session"]["allowed"])
        medic_actions = {a["action"]: a for a in
                         self.store.enrollee_view(MEDIC, "e1")["actions"]}
        self.assertFalse(medic_actions["unfreeze"]["allowed"])
        self.sign_both()
        coach_actions = {a["action"]: a for a in
                         self.store.enrollee_view(COACH, "e1")["actions"]}
        self.assertTrue(coach_actions["schedule_session"]["allowed"])
        # 财务在没有退款请求时不能决定退款
        fin_actions = {a["action"]: a for a in
                       self.store.enrollee_view(FIN, "e1")["actions"]}
        self.assertFalse(fin_actions["decide_refund"]["allowed"])
        self.schedule_two()
        self.store.report_red_flag(COACH, "e1", "chest_tightness", "rk-flag-a")
        medic_actions = {a["action"]: a for a in
                         self.store.enrollee_view(MEDIC, "e1")["actions"]}
        self.assertTrue(medic_actions["unfreeze"]["allowed"])
        coach_actions = {a["action"]: a for a in
                         self.store.enrollee_view(COACH, "e1")["actions"]}
        self.assertNotIn("unfreeze", coach_actions)


class CampCancelTests(CampTestBase):
    def test_cancel_camp_cancels_sessions_and_notifies(self):
        self.sign_both()
        self.schedule_two()
        result = self.store.cancel_camp(ADMIN, "camp1", "rk-cancel", "场地问题")
        self.assertEqual(len(result["cancelled_sessions"]), 2)
        view = self.store.enrollee_view(COACH, "e1")
        self.assertFalse(view["gates"]["camp_active"])
        with self.assertRaises(ServiceError):
            self.store.schedule_session(
                COACH, "e1", "2026-10-05T09:00:00+00:00", 10000, "rk-s-after")
        # 幂等取消：同一 request_key 返回首次结果，不再追加事件
        chain_before = self.store.verify_chain()["events"]
        again = self.store.cancel_camp(ADMIN, "camp1", "rk-cancel")
        self.assertEqual(again, result)
        self.assertEqual(self.store.verify_chain()["events"], chain_before)
        roles = {n["role"] for n in
                 self.store.connection.execute(
                     "SELECT role FROM notifications n JOIN audit_log a"
                     " ON n.event_id=a.event_id WHERE a.action='camp_cancelled'")}
        self.assertEqual(roles, {ROLE_COACH, ROLE_MEDIC, ROLE_FINANCE})

    def test_non_admin_cannot_cancel(self):
        with self.assertRaises(ServiceError):
            self.store.cancel_camp(COACH, "camp1", "rk-cancel-x")


class PersistenceAndTamperTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def _build(self):
        store = CampService(database=self.path)
        store.create_camp(ADMIN, "camp1", "rk-camp")
        store.enroll(ADMIN, "e1", "camp1", "张三", 100000, "rk-enroll")
        store.submit_assessment(
            MEDIC, "e1", ASSESSMENT_MEDICAL, {"conditions": []}, "rk-med")
        store.submit_assessment(
            COACH, "e1", ASSESSMENT_EXERCISE, {"risk": "low"}, "rk-ex")
        store.schedule_session(
            COACH, "e1", "2026-10-01T09:00:00+00:00", 10000, "rk-s1")
        store.report_red_flag(COACH, "e1", "chest_tightness", "rk-flag")
        note_id = [n["notification_id"]
                   for n in store.list_notifications(MEDIC)
                   if n["event_id"].startswith("red_flag")][0]
        store.mark_delivered(MEDIC, note_id, "rk-deliver")
        store.request_refund(E1, "e1", 3000, "退营", "rk-refund")
        store.decide_refund(FIN, "e1", True, "批准", "rk-decide")
        head = store.verify_chain()["head_hash"]
        store.close()
        return head

    def test_decisions_notifications_and_chain_survive_restart(self):
        head = self._build()
        store = CampService(database=self.path)
        # 哈希链在重开后仍可重算且首尾一致
        self.assertEqual(store.verify_chain()["head_hash"], head)
        view = store.enrollee_view(COACH, "e1")
        self.assertTrue(view["gates"]["frozen"])
        self.assertEqual(view["sessions"][0]["status"], "frozen")
        self.assertTrue(view["medical_assessment"]["locked"])
        self.assertEqual(view["refund"]["status"], "approved")
        # 通知送达状态保留
        notes = store.list_notifications(MEDIC)
        delivered = [n for n in notes if n["status"] == "delivered"]
        self.assertEqual(len(delivered), 1)
        # 已批准的退款不能被重复结算
        with self.assertRaises(ServiceError):
            store.decide_refund(FIN, "e1", True, "再批一次", "rk-decide-again")
        # 仅医学可解冻的规则依然生效
        with self.assertRaises(ServiceError):
            store.unfreeze(COACH, "e1", "重启后越权", "rk-u-x")
        store.unfreeze(MEDIC, "e1", "复查通过", "rk-u-ok")
        store.close()

    def test_audit_log_is_tamper_evident(self):
        self._build()
        conn = sqlite3.connect(self.path)
        with self.assertRaises(sqlite3.Error):
            conn.execute("UPDATE audit_log SET actor='attacker' WHERE seq=1")
        with self.assertRaises(sqlite3.Error):
            conn.execute("DELETE FROM audit_log WHERE seq=1")
        conn.rollback()
        conn.close()
        store = CampService(database=self.path)
        self.assertTrue(store.verify_chain()["ok"])
        store.close()


if __name__ == "__main__":
    unittest.main()
