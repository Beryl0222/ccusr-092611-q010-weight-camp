"""HTTP 接口冒烟测试：身份头、命令路由、角色投影、幂等重放。"""
import json
import threading
import unittest
from http.server import ThreadingHTTPServer
from urllib import request as urlrequest
from urllib.error import HTTPError

from weight_camp.api import make_handler
from weight_camp.domain import (
    ASSESSMENT_EXERCISE,
    ASSESSMENT_MEDICAL,
    ROLE_COACH,
    ROLE_MEDIC,
)
from weight_camp.service import CampService

MEDIC = {"X-User-Id": "m1", "X-User-Role": ROLE_MEDIC}
COACH = {"X-User-Id": "c1", "X-User-Role": ROLE_COACH}
ADMIN = {"X-User-Id": "a1", "X-User-Role": "admin"}
OWNER = {"X-User-Id": "e1", "X-User-Role": "owner"}


class ApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.store = CampService()
        handler = make_handler(cls.store)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        cls.port = cls.httpd.server_address[1]
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        cls.store.close()

    def call(self, method, path, body=None, headers=None):
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(body).encode() if body is not None else None
        req = urlrequest.Request(url, data=data, method=method,
                                 headers={"Content-Type": "application/json",
                                          **(headers or {})})
        try:
            with urlrequest.urlopen(req) as resp:
                return resp.status, json.loads(resp.read())
        except HTTPError as exc:
            return exc.code, json.loads(exc.read())

    def test_full_flow_over_http(self):
        self.assertEqual(
            self.call("POST", "/camps", {"camp_id": "c1", "request_key": "h1"},
                      ADMIN)[0], 200)
        self.assertEqual(self.call("POST", "/enrollments", {
            "enrollee_id": "e1", "camp_id": "c1", "name": "张三",
            "fee_paid_cents": 100000, "request_key": "h2"}, ADMIN)[0], 200)

        # 未完成评估不能排课
        code, body = self.call("POST", "/sessions", {
            "enrollee_id": "e1", "scheduled_at": "2026-10-01T09:00:00+00:00",
            "price_cents": 10000, "request_key": "h3"}, COACH)
        self.assertEqual(code, 409)
        self.assertIn("医学评估未完成", body["error"])

        self.assertEqual(self.call("POST", "/assessments", {
            "enrollee_id": "e1", "kind": ASSESSMENT_MEDICAL,
            "content": {"conditions": []}, "request_key": "h4"}, MEDIC)[0], 200)
        self.assertEqual(self.call("POST", "/assessments", {
            "enrollee_id": "e1", "kind": ASSESSMENT_EXERCISE,
            "content": {"risk": "low"}, "request_key": "h5"}, COACH)[0], 200)
        # 重复签署被拒
        code, _ = self.call("POST", "/assessments", {
            "enrollee_id": "e1", "kind": ASSESSMENT_MEDICAL,
            "content": {"conditions": ["x"]}, "request_key": "h6"}, MEDIC)
        self.assertEqual(code, 409)

        self.assertEqual(self.call("POST", "/sessions", {
            "enrollee_id": "e1", "scheduled_at": "2026-10-01T09:00:00+00:00",
            "price_cents": 10000, "request_key": "h7"}, COACH)[0], 200)

        # 红旗冻结
        code, body = self.call("POST", "/red-flags", {
            "enrollee_id": "e1", "symptom": "chest_tightness",
            "request_key": "h8"}, COACH)
        self.assertEqual(code, 200)
        self.assertEqual(body["frozen_sessions"], ["sess:e1:1"])

        # 教练解冻 → 403；医务人员解冻 → 200
        self.assertEqual(self.call("POST", "/unfreeze", {
            "enrollee_id": "e1", "note": "学员要求继续",
            "request_key": "h9"}, COACH)[0], 403)
        self.assertEqual(self.call("POST", "/unfreeze", {
            "enrollee_id": "e1", "note": "复查正常",
            "request_key": "h10"}, MEDIC)[0], 200)

        # 打卡幂等
        ok1, r1 = self.call("POST", "/checkins",
                            {"session_id": "sess:e1:1", "request_key": "h11"}, COACH)
        ok2, r2 = self.call("POST", "/checkins",
                            {"session_id": "sess:e1:1", "request_key": "h11"}, COACH)
        self.assertEqual((ok1, ok2), (200, 200))
        self.assertEqual(r1, r2)
        code, _ = self.call("POST", "/checkins",
                            {"session_id": "sess:e1:1", "request_key": "h12"}, COACH)
        self.assertEqual(code, 409)

    def test_views_and_actions(self):
        # 医务视图：应有 unfreeze 等动作；教练视图不应出现账目
        code, medic_view = self.call("GET", "/enrollees/e1/view", headers=MEDIC)
        self.assertEqual(code, 200)
        actions = {a["action"] for a in medic_view["actions"]}
        self.assertIn("unfreeze", actions)
        _, coach_view = self.call("GET", "/enrollees/e1/view", headers=COACH)
        self.assertIsNone(coach_view["ledger"])
        self.assertNotIn("content", coach_view["medical_assessment"])
        _, owner_view = self.call("GET", "/enrollees/e1/view", headers=OWNER)
        self.assertIsNotNone(owner_view["ledger"])

    def test_notifications_and_audit(self):
        code, body = self.call("GET", "/notifications", headers=MEDIC)
        self.assertEqual(code, 200)
        self.assertTrue(any(n["status"] == "pending" for n in body["notifications"]))
        code, body = self.call("GET", "/audit", headers=ADMIN)
        self.assertEqual(code, 200)
        actions = {e["action"] for e in body["events"]}
        self.assertIn("red_flag", actions)
        self.assertIn("freeze", actions)

    def test_auth_required(self):
        code, _ = self.call("GET", "/enrollees")
        self.assertEqual(code, 401)

    def test_health_reports_chain(self):
        code, body = self.call("GET", "/health", headers=ADMIN)
        self.assertEqual(code, 200)
        self.assertTrue(body["chain"]["ok"])


if __name__ == "__main__":
    unittest.main()
