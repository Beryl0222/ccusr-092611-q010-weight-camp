"""HTTP 边界冒烟测试：身份头、角色隔离、幂等键、动作核对路径。"""
import json
import threading
import unittest
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

from weight_camp.api import make_handler
from weight_camp.service import CampService


class TestServer:
    def __init__(self):
        self.service = CampService(authorized_medic_ids=("medic-1",))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0),
                                         make_handler(self.service))
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever,
                                       daemon=True)
        self.thread.start()

    def stop(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        self.service.close()


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.srv = TestServer()

    def tearDown(self):
        self.srv.stop()

    def call(self, method, path, body=None, actor=None):
        url = f"http://127.0.0.1:{self.srv.port}{path}"
        data = None
        headers = {"Content-Type": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
        if actor:
            headers["X-Actor-Id"], headers["X-Actor-Role"] = actor
        req = urllib.request.Request(url, data=data, headers=headers,
                                     method=method)
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status, json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            return exc.code, json.loads(exc.read().decode("utf-8"))

    def test_full_flow_over_http(self):
        # 无身份头被拒
        self.assertEqual(self.call("GET", "/healthz")[0], 400)
        self.assertEqual(self.call("GET", "/healthz", actor=("a", "admin"))[0],
                         200)
        admin = ("adm-1", "admin")
        medic = ("medic-1", "medic")
        coach = ("coach-1", "coach")
        fin = ("fin-1", "finance")

        st, _ = self.call("POST", "/trainees", {
            "trainee_id": "t1", "name": "小王",
            "profile": {"medical_history": ["高血压"],
                        "payment_amount": 3000},
            "request_key": "k-reg",
        }, actor=admin)
        self.assertEqual(st, 200)

        # 未完成评估：动作核对显示不可排课
        st, view = self.call("GET", "/trainees/t1/actions", actor=coach)
        self.assertFalse(view["actions"]["schedule_session"]["allowed"])

        st, _ = self.call("POST", "/trainees/t1/assessments",
                          {"kind": "medical", "content": {"bp": "150/95"}},
                          actor=medic)
        self.assertEqual(st, 200)
        st, _ = self.call("POST", "/trainees/t1/assessments",
                          {"kind": "fitness", "content": {"risk": "med"}},
                          actor=coach)
        self.assertEqual(st, 200)
        # 重复签署被拒
        st, err = self.call("POST", "/trainees/t1/assessments",
                            {"kind": "medical", "content": {}}, actor=medic)
        self.assertEqual(st, 400)
        self.assertIn("不能覆盖", err["error"])

        # 排课 -> 红旗 -> 冻结
        st, _ = self.call("POST", "/sessions", {
            "trainee_id": "t1", "session_id": "s1",
            "scheduled_at": "2026-09-27T09:00", "request_key": "k-s1",
        }, actor=coach)
        self.assertEqual(st, 200)
        st, out = self.call("POST", "/sessions/s1/red-flag",
                            {"symptoms": ["chest_pain"], "detail": "胸闷"},
                            actor=coach)
        self.assertEqual(st, 200)
        self.assertEqual(out["frozen_sessions"], ["s1"])

        # 财务视图不含病史
        st, fin_view = self.call("GET", "/trainees/t1", actor=fin)
        self.assertNotIn("medical_history", fin_view)
        # 教练冻结期间无法打卡（动作核对）
        st, acts = self.call("GET", "/trainees/t1/actions", actor=coach)
        self.assertFalse(acts["actions"]["check_in"]["allowed"])
        # 未授权医务解冻被拒
        st, err = self.call("POST", "/trainees/t1/lift-freeze",
                            {"note": "恢复"}, actor=("medic-2", "medic"))
        self.assertEqual(st, 400)
        st, _ = self.call("POST", "/trainees/t1/lift-freeze",
                          {"note": "复查正常", "request_key": "k-lift"},
                          actor=medic)
        self.assertEqual(st, 200)

        # 退款：重复请求键只结算一次
        st, _ = self.call("POST", "/trainees/t1",
                          {"reason": "不参加了", "request_key": "k-rr"},
                          actor=("t1", "trainee"))
        self.assertEqual(st, 200)
        st1, r1 = self.call("POST", "/trainees/t1/refund/settle",
                            {"approve": True, "request_key": "k-rs"},
                            actor=fin)
        st2, r2 = self.call("POST", "/trainees/t1/refund/settle",
                            {"approve": True, "request_key": "k-rs"},
                            actor=fin)
        self.assertEqual((st1, st2), (200, 200))
        self.assertEqual(r1, r2)

        # 历史可校验
        st, verify = self.call("GET", "/verify", actor=admin)
        self.assertTrue(verify["ok"])
        self.assertGreater(verify["events"], 0)
        st, hist = self.call("GET", "/trainees/t1/history", actor=admin)
        self.assertEqual(st, 200)
        self.assertTrue(any(e["kind"] == "refund_settled"
                            for e in hist["events"]))


if __name__ == "__main__":
    unittest.main()
