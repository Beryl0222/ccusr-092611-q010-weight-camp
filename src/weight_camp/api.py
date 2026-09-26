"""减重训练风险台的本地 HTTP 边界。

身份通过请求头传递（本地服务约定）：
- X-User-Id：调用方标识
- X-User-Role：owner | coach | medic | finance | admin

所有写命令为 POST /<command>，JSON 体中必须带 request_key 用于幂等。
读接口为 GET，按角色返回不同投影；学员视图含“当前可执行动作”清单。
"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from .service import CampService, ServiceError


def make_handler(store: CampService):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def _actor(self):
            user_id = self.headers.get("X-User-Id")
            role = self.headers.get("X-User-Role")
            if not user_id or not role:
                raise ServiceError("缺少身份头 X-User-Id / X-User-Role", 401)
            return {"user_id": user_id, "role": role}

        def _reply(self, code, body):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def _read_json(self):
            length = int(self.headers.get("Content-Length") or 0)
            if length == 0:
                return {}
            raw = self.rfile.read(length)
            try:
                data = json.loads(raw.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                raise ServiceError("请求体不是合法 JSON", 400)
            if not isinstance(data, dict):
                raise ServiceError("请求体必须是 JSON 对象", 400)
            return data

        # 命令 → (service 方法, 必要字段)
        COMMANDS = {
            "camps": ("create_camp", ("camp_id", "request_key")),
            "enrollments": ("enroll", ("enrollee_id", "camp_id", "name",
                                       "fee_paid_cents", "request_key")),
            "assessments": ("submit_assessment",
                            ("enrollee_id", "kind", "content", "request_key")),
            "supplements": ("submit_supplement",
                            ("enrollee_id", "kind", "content", "request_key")),
            "sessions": ("schedule_session",
                         ("enrollee_id", "scheduled_at", "price_cents", "request_key")),
            "red-flags": ("report_red_flag",
                          ("enrollee_id", "symptom", "request_key")),
            "unfreeze": ("unfreeze", ("enrollee_id", "note", "request_key")),
            "pause": ("pause", ("enrollee_id", "reason", "request_key")),
            "resume": ("resume", ("enrollee_id", "request_key")),
            "referrals": ("open_referral",
                          ("enrollee_id", "reason", "request_key")),
            "referral-resolutions": ("resolve_referral",
                                     ("referral_id", "resolution", "request_key")),
            "checkins": ("checkin", ("session_id", "request_key")),
            "refund-requests": ("request_refund",
                                ("enrollee_id", "amount_cents", "reason", "request_key")),
            "refund-decisions": ("decide_refund",
                                 ("enrollee_id", "approve", "note", "request_key")),
            "camp-cancellations": ("cancel_camp",
                                   ("camp_id", "request_key")),
            "notification-receipts": ("mark_delivered",
                                      ("notification_id", "request_key")),
        }

        def do_POST(self):
            try:
                path = urlparse(self.path).path.strip("/")
                if path not in self.COMMANDS:
                    raise ServiceError(f"未知命令：/{path}", 404)
                actor = self._actor()
                body = self._read_json()
                method_name, required = self.COMMANDS[path]
                missing = [f for f in required if f not in body]
                if missing:
                    raise ServiceError("缺少字段：" + ",".join(missing), 400)
                result = getattr(store, method_name)(actor, **body)
                self._reply(200, result)
            except ServiceError as exc:
                self._reply(exc.status, {"error": str(exc)})
            except TypeError as exc:
                self._reply(400, {"error": f"参数不匹配：{exc}"})

        def do_GET(self):
            try:
                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.strip("/").split("/") if p]
                query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
                actor = self._actor()

                if parts == ["health"]:
                    chain = store.verify_chain()
                    self._reply(200, {"status": "ok", "chain": chain})
                elif parts[0] == "enrollees" and len(parts) == 1:
                    self._reply(200, {"enrollees": store.list_enrollees(
                        actor, query.get("camp_id"))})
                elif parts[0] == "enrollees" and len(parts) >= 3 and parts[2] == "view":
                    self._reply(200, store.enrollee_view(actor, parts[1]))
                elif parts[0] == "enrollees" and len(parts) == 2 and parts[1] != "actions":
                    # 兼容脚手架的最小记录读取
                    row = store.enrollee_view(actor, parts[1])
                    self._reply(200, {
                        "record_id": row["enrollee_id"],
                        "owner_id": row["enrollee_id"],
                        "state": row["status"],
                        "version": row["version"],
                        "updated_at": row["updated_at"],
                    })
                elif parts == ["notifications"]:
                    self._reply(200, {"notifications": store.list_notifications(
                        actor, query.get("role"))})
                elif parts == ["audit"]:
                    self._reply(200, {"events": store.audit_history(
                        actor, query.get("enrollee_id"))})
                else:
                    raise ServiceError("未知路径", 404)
            except ServiceError as exc:
                self._reply(exc.status, {"error": str(exc)})

        def log_message(self, *_):
            return

    return Handler


def serve(host="127.0.0.1", port=8080, database=":memory:", store=None):
    store = store or CampService(database)
    httpd = ThreadingHTTPServer((host, port), make_handler(store))
    httpd.serve_forever()
    return httpd, store
