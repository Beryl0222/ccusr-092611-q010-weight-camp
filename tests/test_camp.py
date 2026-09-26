"""训练营业务规则测试：评估门禁、冻结、通知、转诊、退款与历史完整性。"""
import os
import sqlite3
import tempfile
import unittest

from weight_camp.service import CampService, ServiceError, Actor
from weight_camp import domain as D

MEDIC = Actor("medic-1", D.ROLE_MEDIC)          # 已授权（见 make_service）
MEDIC2 = Actor("medic-2", D.ROLE_MEDIC)         # 未授权
COACH = Actor("coach-1", D.ROLE_COACH)
COACH2 = Actor("coach-2", D.ROLE_COACH)
FINANCE = Actor("fin-1", D.ROLE_FINANCE)
ADMIN = Actor("adm-1", D.ROLE_ADMIN)
TRAINEE = Actor("t1", D.ROLE_TRAINEE)

PROFILE = {
    "name": "小王",
    "medical_history": ["高血压"],
    "contraindications": ["未控制高血压"],
    "medications": ["降压药"],
    "fitness_level": "low",
    "payment_amount": 3000,
}


def make_service(database=":memory:"):
    return CampService(database, authorized_medic_ids=("medic-1",))


def full_onboarding(svc, tid="t1", trainee_id_owner="t1"):
    """报名并完成两项评估，返回 ready 状态。"""
    svc.register(tid, "小王", trainee_id_owner, PROFILE, f"k-{tid}-reg")
    svc.sign_assessment(tid, D.ASSESSMENT_MEDICAL,
                        {"rest_bp": "160/100", "verdict": "可控"}, MEDIC,
                        f"k-{tid}-med")
    svc.sign_assessment(tid, D.ASSESSMENT_FITNESS,
                        {"risk": "medium", "plan": "低强度起步"}, COACH,
                        f"k-{tid}-fit")
    return tid


class AssessmentGateTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()

    def tearDown(self):
        self.svc.close()

    def test_cannot_schedule_before_assessments(self):
        self.svc.register("t1", "小王", "t1", PROFILE)
        with self.assertRaises(ServiceError):  # 无任何评估
            self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                      "coach-1", COACH, "k-s1")
        self.svc.sign_assessment("t1", D.ASSESSMENT_MEDICAL, {"v": 1}, MEDIC)
        with self.assertRaises(ServiceError):  # 仅有医学评估
            self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                      "coach-1", COACH)
        self.svc.sign_assessment("t1", D.ASSESSMENT_FITNESS, {"v": 1}, COACH)
        out = self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                        "coach-1", COACH)
        self.assertEqual(out["status"], "scheduled")

    def test_wrong_role_cannot_sign(self):
        self.svc.register("t1", "小王", "t1", PROFILE)
        with self.assertRaises(ServiceError):  # 教练不能签医学评估
            self.svc.sign_assessment("t1", D.ASSESSMENT_MEDICAL, {}, COACH)
        with self.assertRaises(ServiceError):  # 医务不能签运动评估
            self.svc.sign_assessment("t1", D.ASSESSMENT_FITNESS, {}, MEDIC)

    def test_signed_assessment_cannot_be_overwritten(self):
        full_onboarding(self.svc)
        # 重新签署 / “补交覆盖”一律拒绝
        with self.assertRaises(ServiceError):
            self.svc.sign_assessment("t1", D.ASSESSMENT_MEDICAL,
                                     {"rest_bp": "120/80"}, MEDIC, "k-cover")
        with self.assertRaises(ServiceError):
            self.svc.sign_assessment("t1", D.ASSESSMENT_FITNESS, {}, COACH2)
        # 原评估内容保持不变
        view = self.svc.trainee_view("t1", MEDIC)
        self.assertEqual(len(view["assessments"]), 2)
        signed = {a["kind"]: a["signer_id"] for a in view["assessments"]}
        self.assertEqual(signed[D.ASSESSMENT_MEDICAL], "medic-1")

    def test_supplement_never_overwrites(self):
        full_onboarding(self.svc)
        before = self.svc.trainee_view("t1", MEDIC)["assessments"]
        out = self.svc.submit_supplement("t1", {"new_doc": "心电图"}, MEDIC)
        self.assertFalse(out["assessment_overwritten"])
        after = self.svc.trainee_view("t1", MEDIC)["assessments"]
        self.assertEqual(before, after)


class FreezeAndNotifyTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        full_onboarding(self.svc)
        self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                  "coach-1", COACH, "k-s1")
        self.svc.schedule_session("t1", "s2", "2026-09-28T09:00",
                                  "coach-1", COACH, "k-s2")

    def tearDown(self):
        self.svc.close()

    def test_red_flag_freezes_future_sessions_and_notifies_roles(self):
        out = self.svc.report_red_flag(
            "s1", ["chest_pain"], "训练中胸闷，要求继续", COACH, "k-rf")
        self.assertEqual(out["status"], "active")
        self.assertIn("s1", out["frozen_sessions"])
        self.assertIn("s2", out["frozen_sessions"])
        # 学员本人想继续也不能打卡/排课
        with self.assertRaises(ServiceError):
            self.svc.check_in("s2", COACH)
        with self.assertRaises(ServiceError):
            self.svc.schedule_session("t1", "s3", "2026-09-29T09:00",
                                      "coach-1", COACH)
        # 通知：医务、教练、管理员；财务与学员不在冻结通知范围
        for role, expect in ((D.ROLE_MEDIC, 1), (D.ROLE_COACH, 1),
                             (D.ROLE_ADMIN, 1), (D.ROLE_FINANCE, 0),
                             (D.ROLE_TRAINEE, 0)):
            notes = self.svc.list_notifications(Actor("x", role),
                                                topic=D.NOTIFY_FREEZE)
            self.assertEqual(len(notes), expect, role)
        # 通知内容含决定依据
        note = self.svc.list_notifications(MEDIC)[0]
        self.assertEqual(note["status"], "pending")
        self.assertIn("chest_pain", note["body_json"])

    def test_duplicate_red_flag_rejected(self):
        self.svc.report_red_flag("s1", ["chest_pain"], "胸闷", COACH, "k-rf")
        with self.assertRaises(ServiceError):
            self.svc.report_red_flag("s2", ["syncope"], "晕厥", COACH)
        # 同一请求键重放：结果一致，不产生第二条冻结
        replay = self.svc.report_red_flag("s1", ["chest_pain"], "胸闷",
                                          COACH, "k-rf")
        self.assertEqual(replay["freeze_id"], "frz-t1-s1")
        freezes = [e for e in self.svc.history("t1")
                   if e["kind"] == "red_flag_reported"]
        self.assertEqual(len(freezes), 1)

    def test_only_authorized_medic_can_lift(self):
        self.svc.report_red_flag("s1", ["chest_pain"], "胸闷", COACH)
        with self.assertRaises(ServiceError):  # 教练不行
            self.svc.lift_freeze("t1", "没事了", COACH)
        with self.assertRaises(ServiceError):  # 未授权医务不行
            self.svc.lift_freeze("t1", "没事了", MEDIC2)
        out = self.svc.lift_freeze("t1", "复查心电图正常，可恢复低强度",
                                   MEDIC, "k-lift")
        self.assertEqual(out["status"], "lifted")
        self.assertEqual(out["state"], D.ST_READY)
        # 解冻通知到教练/管理员，不到财务
        self.assertEqual(
            len(self.svc.list_notifications(FINANCE, D.NOTIFY_UNFREEZE)), 0)
        self.assertEqual(
            len(self.svc.list_notifications(COACH, D.NOTIFY_UNFREEZE)), 1)
        # 解除后可重新排课（被冻课程不自动复活）
        self.svc.schedule_session("t1", "s3", "2026-09-29T09:00",
                                  "coach-1", COACH)

    def test_referral_must_close_before_lift(self):
        self.svc.report_red_flag("s1", ["chest_pain"], "胸闷", COACH)
        self.svc.refer("t1", "疑似运动诱发心律失常，转心内科", MEDIC, "k-ref")
        self.assertEqual(self.svc._snapshot("t1")["state"], D.ST_REFERRED)
        with self.assertRaises(ServiceError):
            self.svc.lift_freeze("t1", "恢复", MEDIC)
        self.svc.resolve_referral("t1", "排除急性问题，建议降压后评估",
                                  MEDIC, "k-res")
        # 转诊关闭但冻结仍在，状态保持 referred，解冻后才回 ready
        self.assertEqual(self.svc._snapshot("t1")["state"], D.ST_REFERRED)
        self.svc.lift_freeze("t1", "转诊结论支持恢复", MEDIC)
        self.assertEqual(self.svc._snapshot("t1")["state"], D.ST_READY)


class CheckinIdempotencyTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        full_onboarding(self.svc)
        self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                  "coach-1", COACH, "k-s1")

    def tearDown(self):
        self.svc.close()

    def test_repeated_checkin_settles_once(self):
        a = self.svc.check_in("s1", COACH, "k-ci")
        b = self.svc.check_in("s1", COACH, "k-ci")  # 同键重放
        self.assertEqual(a, b)
        with self.assertRaises(ServiceError):  # 换新键也不能二次打卡
            self.svc.check_in("s1", COACH, "k-ci-2")
        events = [e for e in self.svc.history("t1")
                  if e["kind"] == "checked_in"]
        self.assertEqual(len(events), 1)


class RefundTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        full_onboarding(self.svc)

    def tearDown(self):
        self.svc.close()

    def test_refund_request_and_settle_idempotent(self):
        self.svc.request_refund("t1", "个人原因", TRAINEE, request_key="k-rr")
        with self.assertRaises(ServiceError):  # 重复请求拒绝
            self.svc.request_refund("t1", "再试一次", TRAINEE, "k-rr2")
        # 财务收到通知
        self.assertEqual(
            len(self.svc.list_notifications(FINANCE, D.NOTIFY_REFUND)), 1)
        a = self.svc.settle_refund("t1", True, FINANCE, request_key="k-rs")
        b = self.svc.settle_refund("t1", True, FINANCE, request_key="k-rs")
        self.assertEqual(a, b)
        with self.assertRaises(ServiceError):  # 重复结算拒绝
            self.svc.settle_refund("t1", True, FINANCE, request_key="k-rs2")
        settled = [e for e in self.svc.history("t1")
                   if e["kind"] == "refund_settled"]
        self.assertEqual(len(settled), 1)  # 只结算一次
        self.assertEqual(self.svc._snapshot("t1")["state"], D.ST_REFUNDED)
        # 结算后不能再排课/打卡
        with self.assertRaises(ServiceError):
            self.svc.schedule_session("t1", "s9", "2026-10-01T09:00",
                                      "coach-1", COACH)

    def test_refund_after_freeze_records_basis(self):
        self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                  "coach-1", COACH)
        self.svc.report_red_flag("s1", ["syncope"], "晕厥", COACH)
        self.svc.request_refund("t1", "训练出现晕厥", TRAINEE)
        events = [e for e in self.svc.history("t1")
                  if e["kind"] == "refund_requested"]
        import json
        basis = json.loads(events[0]["body_json"])["basis"]
        self.assertEqual(basis["freeze"]["status"], "active")
        self.assertIn("syncope", basis["freeze"]["symptoms"])

    def test_reject_refund_keeps_trainee_active(self):
        self.svc.request_refund("t1", "后悔了", TRAINEE)
        out = self.svc.settle_refund("t1", False, FINANCE, note="超过期限")
        self.assertEqual(out["status"], D.REFUND_REJECTED)
        self.assertNotEqual(self.svc._snapshot("t1")["state"], D.ST_REFUNDED)


class CampCancelTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        full_onboarding(self.svc, "t1")
        full_onboarding(self.svc, "t2", "t2")

    def tearDown(self):
        self.svc.close()

    def test_cancel_freezes_all_and_keeps_decisions(self):
        out = self.svc.cancel_camp("场地原因停办", ADMIN, "k-cancel")
        self.assertEqual(sorted(out["cancelled_trainees"]), ["t1", "t2"])
        for tid in ("t1", "t2"):
            self.assertEqual(self.svc._snapshot(tid)["state"],
                             D.ST_CANCELLED)
            with self.assertRaises(ServiceError):
                self.svc.schedule_session(tid, "sx", "2026-10-01T09:00",
                                          "coach-1", COACH)
        # 只有管理员能取消
        with self.assertRaises(ServiceError):
            self.svc.cancel_camp("再取消", COACH)
        # 每个角色都收到取消通知
        for role in (D.ROLE_COACH, D.ROLE_MEDIC, D.ROLE_FINANCE, D.ROLE_ADMIN):
            self.assertEqual(len(self.svc.list_notifications(
                Actor("x", role), D.NOTIFY_CAMP_CANCELLED)), 2)


class RoleViewTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        full_onboarding(self.svc)

    def tearDown(self):
        self.svc.close()

    def test_sensitive_fields_by_role(self):
        medic = self.svc.trainee_view("t1", MEDIC)
        self.assertIn("medical_history", medic)
        coach = self.svc.trainee_view("t1", COACH)
        self.assertNotIn("medical_history", coach)
        self.assertNotIn("medications", coach)
        fin = self.svc.trainee_view("t1", FINANCE)
        self.assertNotIn("medical_history", fin)
        self.assertEqual(fin["payment_amount"], 3000)
        # 评估签署事实对各角色可见，但医学内容仅医务/管理员可见
        coach_assess = {a["kind"]: a for a in coach["assessments"]}
        self.assertNotIn("content", coach_assess[D.ASSESSMENT_MEDICAL])
        medic_assess = {a["kind"]: a for a in medic["assessments"]}
        self.assertEqual(
            medic_assess[D.ASSESSMENT_MEDICAL]["content"]["rest_bp"],
            "160/100")
        self.assertNotIn("rest_bp", str(coach))

    def test_trainee_owner_only(self):
        self.svc.trainee_view("t1", TRAINEE)  # 本人可以
        with self.assertRaises(ServiceError):
            self.svc.trainee_view("t1", Actor("t2", D.ROLE_TRAINEE))

    def test_history_redaction_keeps_hashes(self):
        self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                  "coach-1", COACH)
        self.svc.report_red_flag("s1", ["chest_pain"], "胸闷", COACH)
        self.svc.refer("t1", "转心内科", MEDIC)
        full = {e["seq"]: e for e in self.svc.history("t1", ADMIN)}
        coach_view = {e["seq"]: e for e in self.svc.history("t1", COACH)}
        # 转诊正文对教练遮蔽，但哈希原样保留可供校验
        referred = next(seq for seq, e in full.items()
                        if e["kind"] == "referred")
        import json as _json
        self.assertEqual(
            _json.loads(coach_view[referred]["body_json"]),
            {"redacted": True})
        self.assertEqual(coach_view[referred]["hash"], full[referred]["hash"])
        # 管理员可见完整正文；完整性校验仍通过
        self.assertIn("转心内科", full[referred]["body_json"])
        self.assertGreater(self.svc.verify_history(), 0)


class ActionProjectionTests(unittest.TestCase):
    def setUp(self):
        self.svc = make_service()
        full_onboarding(self.svc)

    def tearDown(self):
        self.svc.close()

    def test_actions_match_enforcement(self):
        # ready + 教练：可排课不可解冻
        actions = self.svc.available_actions("t1", COACH)["actions"]
        self.assertTrue(actions["schedule_session"]["allowed"])
        self.assertFalse(actions["lift_freeze"]["allowed"])
        self.assertTrue(actions["sign_medical_assessment"]["allowed"] is False)
        # 学员视角不能排课，但可申请退款
        user_actions = self.svc.available_actions("t1", TRAINEE)["actions"]
        self.assertFalse(user_actions["schedule_session"]["allowed"])
        self.assertTrue(user_actions["request_refund"]["allowed"])

        self.svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                                  "coach-1", COACH)
        self.svc.report_red_flag("s1", ["chest_pain"], "胸闷", COACH)
        frozen = self.svc.available_actions("t1", COACH)["actions"]
        self.assertFalse(frozen["schedule_session"]["allowed"])
        self.assertFalse(frozen["check_in"]["allowed"])
        self.assertFalse(frozen["lift_freeze"]["allowed"])
        medic_actions = self.svc.available_actions("t1", MEDIC)["actions"]
        self.assertTrue(medic_actions["lift_freeze"]["allowed"])
        self.assertTrue(medic_actions["report_red_flag"]["allowed"] is False)
        unauth = self.svc.available_actions("t1", MEDIC2)["actions"]
        self.assertFalse(unauth["lift_freeze"]["allowed"])
        self.svc.lift_freeze("t1", "恢复", MEDIC)
        again = self.svc.available_actions("t1", MEDIC)["actions"]
        self.assertFalse(again["lift_freeze"]["allowed"])
        self.assertTrue(
            self.svc.available_actions("t1", COACH)
            ["actions"]["schedule_session"]["allowed"])

    def test_refund_actions_consumed(self):
        self.svc.request_refund("t1", "原因", TRAINEE)
        a = self.svc.available_actions("t1", TRAINEE)["actions"]
        self.assertFalse(a["request_refund"]["allowed"])
        f = self.svc.available_actions("t1", FINANCE)["actions"]
        self.assertTrue(f["settle_refund"]["allowed"])
        self.svc.settle_refund("t1", True, FINANCE)
        f2 = self.svc.available_actions("t1", FINANCE)["actions"]
        self.assertFalse(f2["settle_refund"]["allowed"])


class PersistenceAndTamperTests(unittest.TestCase):
    def setUp(self):
        fd, self.path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        os.unlink(self.path)

    def tearDown(self):
        if os.path.exists(self.path):
            os.unlink(self.path)

    def test_restart_preserves_state_notifications_and_basis(self):
        svc = make_service(self.path)
        full_onboarding(svc)
        svc.schedule_session("t1", "s1", "2026-09-27T09:00",
                             "coach-1", COACH, "k-s1")
        svc.report_red_flag("s1", ["chest_pain"], "胸闷", COACH, "k-rf")
        note_id = svc.list_notifications(MEDIC, D.NOTIFY_FREEZE)[0][
            "notification_id"]
        svc.deliver_notification(note_id, MEDIC)
        svc.request_refund("t1", "身体原因", TRAINEE, request_key="k-rr")
        n_events = svc.verify_history()
        svc.close()

        svc2 = make_service(self.path)  # 重放哈希链
        self.assertEqual(svc2.verify_history(), n_events)
        self.assertEqual(svc2._snapshot("t1")["state"], D.ST_FROZEN)
        note = next(n for n in svc2.list_notifications(MEDIC)
                    if n["notification_id"] == note_id)
        self.assertEqual(note["status"], "delivered")
        self.assertIsNotNone(note["delivered_at"])
        refund = svc2.trainee_view("t1", FINANCE)["refund"]
        self.assertEqual(refund["status"], D.REFUND_REQUESTED)
        # 重启后仍可凭原请求键幂等结算（不会重复）
        out = svc2.settle_refund("t1", True, FINANCE, request_key="k-rs1")
        self.assertEqual(out["status"], D.REFUND_SETTLED)
        svc2.close()

    def test_tampered_history_is_detected_on_open(self):
        svc = make_service(self.path)
        full_onboarding(svc)
        svc.close()
        # 绕过服务直接篡改事件内容
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE events SET body_json='{}' WHERE seq=1")
        raw.commit()
        raw.close()
        with self.assertRaises(ServiceError):
            make_service(self.path)

    def test_tampered_head_is_detected_on_verify(self):
        svc = make_service(self.path)
        full_onboarding(svc)
        n = svc.verify_history()
        svc.close()
        raw = sqlite3.connect(self.path)
        raw.execute("UPDATE events SET actor_id='forger' WHERE seq=1")
        raw.commit()
        raw.close()
        # 打开库时重放哈希链，篡改在启动阶段即被拒绝
        with self.assertRaises(ServiceError):
            make_service(self.path)


if __name__ == "__main__":
    unittest.main()
