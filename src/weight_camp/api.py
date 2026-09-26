"""减重训练风险台的轻量 HTTP 边界。

鉴权约定：调用方通过 ``X-Actor-Id`` / ``X-Actor-Role`` 两个请求头表明身份，
角色取值见 :mod:`weight_camp.domain`。所有写操作建议携带 ``request_key``
以获得重复提交保护。
"""
import json
import os
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

from .service import CampService, ServiceError, Actor
from . import domain as D


def make_handler(service: CampService):
    """生成绑定到指定服务实例的 Handler 类（便于测试注入）。"""

    class Handler(BaseHTTPRequestHandler):
        def _reply(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if not length:
                return {}
            try:
                return json.loads(self.rfile.read(length).decode("utf-8"))
            except json.JSONDecodeError:
                raise ServiceError("请求体不是合法 JSON")

        def _actor(self):
            user_id = self.headers.get("X-Actor-Id")
            role = self.headers.get("X-Actor-Role")
            if not user_id or not role:
                raise ServiceError("缺少 X-Actor-Id / X-Actor-Role")
            if role not in D.ALL_ROLES:
                raise ServiceError("未知角色")
            return Actor(user_id=user_id, role=role)

        def log_message(self, *_):
            return

        # -- 路由 ----------------------------------------------------------
        def do_GET(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            qs = parse_qs(parsed.query)
            try:
                actor = self._actor()
                if parts == ["healthz"]:
                    return self._reply(200, {"ok": True})
                if parts == ["trainees"]:
                    return self._reply(200, {"trainees": service.list_trainees()})
                if len(parts) == 2 and parts[0] == "trainees":
                    return self._reply(200, service.trainee_view(parts[1], actor))
                if len(parts) == 3 and parts[0] == "trainees" \
                        and parts[2] == "actions":
                    return self._reply(
                        200, service.available_actions(parts[1], actor))
                if len(parts) == 3 and parts[0] == "trainees" \
                        and parts[2] == "history":
                    return self._reply(
                        200, {"events": service.history(parts[1], actor)})
                if parts == ["notifications"]:
                    topic = qs.get("topic", [None])[0]
                    return self._reply(
                        200, {"notifications":
                              service.list_notifications(actor, topic)})
                if parts == ["verify"]:
                    n = service.verify_history()
                    return self._reply(200, {"ok": True, "events": n})
                self._reply(404, {"error": "未知路径"})
            except ServiceError as exc:
                self._reply(400, {"error": str(exc)})

        def do_POST(self):
            parsed = urlparse(self.path)
            parts = [p for p in parsed.path.split("/") if p]
            try:
                actor = self._actor()
                body = self._read_json()
                key = body.get("request_key")
                result = self._dispatch(parts, body, actor, key)
                self._reply(200, result)
            except ServiceError as exc:
                self._reply(400, {"error": str(exc)})

        def _dispatch(self, parts, body, actor, key):
            s = service
            # 学员与评估
            if parts == ["trainees"]:
                return s.register(
                    body["trainee_id"], body["name"],
                    body.get("owner_id", body["trainee_id"]),
                    body.get("profile"), key, actor)
            tid = parts[1] if len(parts) > 1 and parts[0] == "trainees" else None
            if tid and len(parts) == 3 and parts[2] == "assessments":
                return s.sign_assessment(
                    tid, body["kind"], body.get("content"), actor, key)
            if tid and len(parts) == 3 and parts[2] == "supplements":
                return s.submit_supplement(tid, body.get("body", body),
                                           actor, key)
            # 课程
            if parts == ["sessions"]:
                return s.schedule_session(
                    body["trainee_id"], body["session_id"],
                    body["scheduled_at"], body.get("coach_id", actor.user_id),
                    actor, key)
            if len(parts) == 3 and parts[0] == "sessions" \
                    and parts[2] == "check-in":
                return s.check_in(parts[1], actor, key)
            if len(parts) == 3 and parts[0] == "sessions" \
                    and parts[2] == "red-flag":
                return s.report_red_flag(
                    parts[1], body["symptoms"], body.get("detail"),
                    actor, key)
            # 冻结 / 转诊 / 退款
            if tid and len(parts) == 3 and parts[2] == "lift-freeze":
                return s.lift_freeze(tid, body.get("note", ""), actor, key)
            if tid and len(parts) == 3 and parts[2] == "refer":
                return s.refer(tid, body["reason"], actor, key)
            if tid and len(parts) == 4 and parts[2:] == ["referral", "resolve"]:
                return s.resolve_referral(tid, body["outcome"], actor, key)
            if tid and len(parts) == 2:
                return s.request_refund(
                    tid, body["reason"], actor, body.get("amount"), key)
            if tid and len(parts) == 4 and parts[2:] == ["refund", "settle"]:
                return s.settle_refund(
                    tid, bool(body.get("approve")), actor,
                    body.get("settlement_id"), body.get("note"), key)
            # 全局
            if parts == ["camp", "cancel"]:
                return s.cancel_camp(body["reason"], actor, key)
            if len(parts) == 3 and parts[0] == "notifications" \
                    and parts[2] == "deliver":
                return s.deliver_notification(parts[1], actor)
            raise ServiceError("未知路径")

    return Handler


def serve(host=None, port=None, database=None, authorized_medic_ids=None):
    database = database or os.environ.get("WEIGHT_CAMP_DB", ":memory:")
    if authorized_medic_ids is None:
        env = os.environ.get("WEIGHT_CAMP_MEDICS", "")
        authorized_medic_ids = tuple(x for x in env.split(",") if x) or ("medic-1",)
    service = CampService(database, authorized_medic_ids=authorized_medic_ids)
    handler = make_handler(service)
    host = host or os.environ.get("WEIGHT_CAMP_HOST", "127.0.0.1")
    port = port or int(os.environ.get("WEIGHT_CAMP_PORT", "8080"))
    ThreadingHTTPServer((host, port), handler).serve_forever()


if __name__ == "__main__":
    serve()
