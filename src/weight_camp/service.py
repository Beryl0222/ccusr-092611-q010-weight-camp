"""减重训练风险台的持久化边界、状态迁移、权限与幂等服务。

保留通用的 :class:`DomainStore` 骨架，业务实现见 :class:`CampService`。

关键不变量：
- 医学评估与运动风险评估全部签署前不得排课；
- 评估一经签署即固化，补交资料只能另存，不能覆盖；
- 红旗出现立即冻结后续课程并向指定角色发出通知；
- 只有授权医务人员可以解除冻结；
- 打卡、退款结算等写操作以请求键幂等，重复提交不会重复结算；
- 全部决定与状态迁移写入哈希链事件表，重启时自动校验，篡改即拒绝服务。
"""
import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from dataclasses import dataclass

from . import domain as D
from .domain import Record, utc_now


class ServiceError(Exception):
    """业务规则被违反时抛出，消息可直接呈现给调用方。"""


@dataclass(frozen=True)
class Actor:
    """操作人：身份编号 + 角色。"""
    user_id: str
    role: str


# ---------------------------------------------------------------------------
# 通用骨架（保留，表名加 legacy 前缀，避免与训练营业务表冲突）
# ---------------------------------------------------------------------------
class DomainStore:
    def __init__(self, database=":memory:", clock=utc_now):
        self.connection = sqlite3.connect(database)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.connection.executescript("""
        PRAGMA foreign_keys=ON;
        CREATE TABLE IF NOT EXISTS legacy_records(
          record_id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, state TEXT NOT NULL,
          version INTEGER NOT NULL, payload TEXT NOT NULL, updated_at TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS legacy_events(
          event_id TEXT PRIMARY KEY, record_id TEXT NOT NULL, kind TEXT NOT NULL,
          body TEXT NOT NULL, created_at TEXT NOT NULL,
          FOREIGN KEY(record_id) REFERENCES legacy_records(record_id));
        CREATE TABLE IF NOT EXISTS legacy_idempotency(
          request_key TEXT PRIMARY KEY, result TEXT NOT NULL);
        """)
        self.connection.commit()

    @contextmanager
    def transaction(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def create(self, record_id, owner_id, payload=None):
        with self.transaction():
            self.connection.execute(
                "INSERT INTO legacy_records VALUES(?,?,?,?,?,?)",
                (record_id, owner_id, "draft", 1,
                 json.dumps(payload or {}), self.clock()))
            self.connection.execute(
                "INSERT INTO legacy_events VALUES(?,?,?,?,?)",
                (record_id + ":created", record_id, "created", "{}", self.clock()))
        return self.get(record_id)

    def get(self, record_id):
        row = self.connection.execute(
            "SELECT * FROM legacy_records WHERE record_id=?", (record_id,)).fetchone()
        if row is None:
            raise ServiceError("记录不存在")
        return Record(row["record_id"], row["owner_id"], row["state"],
                      row["version"], row["updated_at"])

    def transition(self, record_id, owner_id, target, request_key, expected_version=None):
        with self.transaction():
            old = self.connection.execute(
                "SELECT * FROM legacy_records WHERE record_id=?", (record_id,)).fetchone()
            if old is None:
                raise ServiceError("记录不存在")
            if old["owner_id"] != owner_id:
                raise ServiceError("无权操作")
            cached = self.connection.execute(
                "SELECT result FROM legacy_idempotency WHERE request_key=?",
                (request_key,)).fetchone()
            if cached:
                return json.loads(cached["result"])
            if expected_version is not None and old["version"] != expected_version:
                raise ServiceError("版本冲突")
            allowed = {"draft": {"pending"}, "pending": {"approved", "cancelled"},
                       "approved": {"closed"}, "cancelled": set(), "closed": set()}
            if target not in allowed.get(old["state"], set()):
                raise ServiceError("状态迁移不允许")
            version = old["version"] + 1
            now = self.clock()
            self.connection.execute(
                "UPDATE legacy_records SET state=?,version=?,updated_at=? WHERE record_id=?",
                (target, version, now, record_id))
            body = json.dumps({"from": old["state"], "to": target, "version": version})
            self.connection.execute(
                "INSERT INTO legacy_events VALUES(?,?,?,?,?)",
                (request_key + ":event", record_id, "transition", body, now))
            result = {"record_id": record_id, "state": target, "version": version}
            self.connection.execute(
                "INSERT INTO legacy_idempotency VALUES(?,?)",
                (request_key, json.dumps(result)))
            return result

    def close(self):
        self.connection.close()


# ---------------------------------------------------------------------------
# 训练营业务服务
# ---------------------------------------------------------------------------
_GENESIS = "0" * 64


def _canonical(payload) -> str:
    return json.dumps(payload, sort_keys=True, ensure_ascii=False,
                      separators=(",", ":"))


def _digest(prev_hash: str, seq: int, event_id: str, trainee_id: str, kind: str,
            actor_id: str, actor_role: str, body: dict, created_at: str) -> str:
    material = _canonical({
        "seq": seq, "event_id": event_id, "trainee_id": trainee_id, "kind": kind,
        "actor_id": actor_id, "actor_role": actor_role, "body": body,
        "created_at": created_at, "prev_hash": prev_hash,
    })
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class CampService:
    """训练营评估、日程、冻结、转诊与退款决定的事务服务。

    ``database`` 为文件路径时决定依据与历史在进程重启后仍然保留；
    打开已有库时会重放哈希链，任何篡改都会抛出 :class:`ServiceError`。
    """

    def __init__(self, database=":memory:", clock=utc_now,
                 authorized_medic_ids=None, verify_on_open=True):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.authorized_medic_ids = frozenset(authorized_medic_ids or ())
        with self._lock:
            self.connection.executescript(_SCHEMA)
            self.connection.commit()
            if verify_on_open:
                self._verify_chain_locked()

    def close(self):
        with self._lock:
            self.connection.close()

    # -- 事务与幂等 --------------------------------------------------------
    @contextmanager
    def _tx(self):
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _idempotent(self, key, op, producer):
        """在同一事务内处理请求键：命中则回放，未命中则执行并固化结果。"""
        if key is None:
            return producer()
        cached = self.connection.execute(
            "SELECT op, response_json FROM idempotency WHERE request_key=?",
            (key,)).fetchone()
        if cached is not None:
            if cached["op"] != op:
                raise ServiceError("请求键已用于其他操作")
            return json.loads(cached["response_json"])
        result = producer()
        self.connection.execute(
            "INSERT INTO idempotency(request_key,op,response_json,created_at) "
            "VALUES(?,?,?,?)", (key, op, json.dumps(result, ensure_ascii=False),
                                self.clock()))
        return result

    # -- 事件哈希链 --------------------------------------------------------
    def _event(self, trainee_id, kind, actor, body, event_id=None):
        """追加一个哈希链事件，返回 (seq, event_id, hash)。必须在事务内调用。"""
        now = self.clock()
        head = self.connection.execute(
            "SELECT seq,hash FROM events ORDER BY seq DESC LIMIT 1").fetchone()
        seq = (head["seq"] + 1) if head else 1
        prev_hash = head["hash"] if head else _GENESIS
        if event_id is None:
            event_id = f"{trainee_id}:{kind}:{seq}"
        h = _digest(prev_hash, seq, event_id, trainee_id, kind,
                    actor.user_id if actor else "system",
                    actor.role if actor else "system", body, now)
        self.connection.execute(
            "INSERT INTO events(seq,event_id,trainee_id,kind,actor_id,actor_role,"
            "body_json,created_at,prev_hash,hash) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (seq, event_id, trainee_id, kind,
             actor.user_id if actor else "system",
             actor.role if actor else "system",
             json.dumps(body, ensure_ascii=False), now, prev_hash, h))
        return seq, event_id, h

    def _verify_chain_locked(self):
        rows = self.connection.execute(
            "SELECT * FROM events ORDER BY seq").fetchall()
        prev_hash = _GENESIS
        seen_ids = set()
        for row in rows:
            if row["event_id"] in seen_ids:
                raise ServiceError("操作历史校验失败：事件编号重复")
            seen_ids.add(row["event_id"])
            if row["prev_hash"] != prev_hash:
                raise ServiceError("操作历史校验失败：哈希链断裂")
            expect = _digest(row["prev_hash"], row["seq"], row["event_id"],
                             row["trainee_id"], row["kind"], row["actor_id"],
                             row["actor_role"], json.loads(row["body_json"]),
                             row["created_at"])
            if expect != row["hash"]:
                raise ServiceError("操作历史校验失败：事件内容被篡改")
            prev_hash = row["hash"]

    def verify_history(self):
        """对外暴露的完整性校验，通过时返回事件数量。"""
        with self._lock, self._tx():
            self._verify_chain_locked()
            return self.connection.execute("SELECT COUNT(*) AS n FROM events").fetchone()["n"]

    # 各角色在历史投影中需要遮蔽正文的事件类型（哈希仍保留，可独立校验）
    _HISTORY_REDACTED_KINDS = {
        D.ROLE_COACH: frozenset({"referred", "referral_resolved",
                                 "refund_requested", "refund_settled",
                                 "refund_rejected"}),
        D.ROLE_FINANCE: frozenset({"referred", "referral_resolved",
                                   "red_flag_reported", "session_frozen",
                                   "assessment_signed"}),
        D.ROLE_TRAINEE: frozenset({"referred", "referral_resolved",
                                   "assessment_signed"}),
        D.ROLE_MEDIC: frozenset(),
        D.ROLE_ADMIN: frozenset(),
    }

    def history(self, trainee_id, actor=None):
        """返回某学员带哈希的全部事件（决定依据 + 操作历史）。

        传入 ``actor`` 时按角色遮蔽敏感事件正文；事件哈希与链结构不受影响，
        任何角色都可用 :meth:`verify_history` 核对完整性。
        """
        with self._lock:
            row = self._require_trainee(trainee_id)
            if actor is not None and actor.role == D.ROLE_TRAINEE \
                    and row["owner_id"] != actor.user_id:
                raise ServiceError("只能查看本人历史")
            redacted = self._HISTORY_REDACTED_KINDS.get(
                actor.role if actor else D.ROLE_ADMIN, frozenset())
            rows = self.connection.execute(
                "SELECT seq,event_id,kind,actor_id,actor_role,body_json,created_at,"
               "prev_hash,hash FROM events WHERE trainee_id=? ORDER BY seq",
                (trainee_id,)).fetchall()
            result = []
            for r in rows:
                item = dict(r)
                if item["kind"] in redacted:
                    item["body_json"] = json.dumps({"redacted": True})
                result.append(item)
            return result

    # -- 基础读取 ----------------------------------------------------------
    def _require_trainee(self, trainee_id):
        row = self.connection.execute(
            "SELECT * FROM trainees WHERE trainee_id=?", (trainee_id,)).fetchone()
        if row is None:
            raise ServiceError("学员不存在")
        return row

    @staticmethod
    def _require_role(actor, roles, message="无权操作"):
        if actor is None or actor.role not in roles:
            raise ServiceError(message)

    def _set_state(self, trainee_id, target, actor, reason_body):
        old = self._require_trainee(trainee_id)
        body = {"from": old["state"], "to": target, **reason_body}
        self.connection.execute(
            "UPDATE trainees SET state=?,version=version+1,updated_at=? WHERE trainee_id=?",
            (target, self.clock(), trainee_id))
        self._event(trainee_id, "state_changed", actor, body)
        return old["state"]

    def _assessments_ready(self, trainee_id):
        rows = self.connection.execute(
            "SELECT kind FROM assessments WHERE trainee_id=?", (trainee_id,)).fetchall()
        kinds = {r["kind"] for r in rows}
        return D.ASSESSMENT_MEDICAL in kinds and D.ASSESSMENT_FITNESS in kinds

    # -- 报名 --------------------------------------------------------------
    def register(self, trainee_id, name, owner_id, profile=None, request_key=None,
                 actor=None):
        """学员报名。profile 可含病史、禁忌用药、运动基础、缴费金额等。"""
        profile = profile or {}
        with self._lock, self._tx():
            def do():
                now = self.clock()
                try:
                    self.connection.execute(
                        "INSERT INTO trainees(trainee_id,name,owner_id,profile_json,"
                        "state,version,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                        (trainee_id, name, owner_id,
                         json.dumps(profile, ensure_ascii=False),
                         D.ST_DRAFT, 1, now, now))
                except sqlite3.IntegrityError:
                    raise ServiceError("学员已存在")
                body = {"name": name, "owner_id": owner_id,
                        "profile_keys": sorted(profile.keys())}
                self._event(trainee_id, "registered", actor, body)
                return self._snapshot(trainee_id)
            return self._idempotent(request_key, "register", do)

    # -- 评估签署（不可覆盖） ----------------------------------------------
    def sign_assessment(self, trainee_id, kind, content, actor, request_key=None):
        """签署医学或运动风险评估。

        只有对应角色（医务人员/教练）可以签署；同一评估重复签署一律拒绝，
        即便请求方声称“补交资料”——补交资料请走 :meth:`submit_supplement`。
        """
        if kind not in D.ASSESSMENT_TYPES:
            raise ServiceError("未知评估类型")
        required_role = D.ASSESSMENT_SIGNER_ROLE[kind]
        self._require_role(actor, (required_role,), f"仅{required_role}可签署该评估")
        content = dict(content or {})
        content_hash = hashlib.sha256(
            _canonical(content).encode("utf-8")).hexdigest()

        with self._lock, self._tx():
            def do():
                row = self._require_trainee(trainee_id)
                existing = self.connection.execute(
                    "SELECT signer_id,signed_at FROM assessments WHERE trainee_id=? AND kind=?",
                    (trainee_id, kind)).fetchone()
                if existing is not None:
                    raise ServiceError("评估已签署，不能覆盖或重复签署")
                now = self.clock()
                self.connection.execute(
                    "INSERT INTO assessments(trainee_id,kind,signer_id,signer_role,"
                    "content_hash,body_json,signed_at,request_key) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (trainee_id, kind, actor.user_id, actor.role, content_hash,
                     json.dumps(content, ensure_ascii=False), now, request_key))
                self._event(trainee_id, "assessment_signed", actor, {
                    "kind": kind, "content_hash": content_hash,
                    "signer_id": actor.user_id,
                })
                # 两项齐备：draft -> ready；其他状态（如冻结中补交首签）不改状态。
                if row["state"] == D.ST_DRAFT and self._assessments_ready(trainee_id):
                    self._set_state(trainee_id, D.ST_READY, actor,
                                    {"reason": "医学与运动风险评估均已完成"})
                return self._snapshot(trainee_id)
            return self._idempotent(request_key, f"sign:{kind}", do)

    def submit_supplement(self, trainee_id, body, actor, request_key=None):
        """补交资料：另立留痕，绝不覆盖已签署评估。"""
        body = dict(body or {})
        with self._lock, self._tx():
            def do():
                self._require_trainee(trainee_id)
                supplement_id = f"sup-{trainee_id}-{self.clock()}"
                self.connection.execute(
                    "INSERT INTO supplements(supplement_id,trainee_id,body_json,"
                    "submitted_by,submitted_role,created_at) VALUES(?,?,?,?,?,?)",
                    (supplement_id, trainee_id, json.dumps(body, ensure_ascii=False),
                     actor.user_id, actor.role, self.clock()))
                seq, _, _ = self._event(trainee_id, "supplement_submitted", actor, {
                    "supplement_id": supplement_id,
                    "keys": sorted(body.keys()),
                })
                return {"supplement_id": supplement_id, "event_seq": seq,
                        "assessment_overwritten": False}
            return self._idempotent(request_key, "supplement", do)

    # -- 排课与打卡 --------------------------------------------------------
    def schedule_session(self, trainee_id, session_id, scheduled_at, coach_id,
                         actor, request_key=None):
        """排入训练课程；评估未完成或处于冻结/终态一律拒绝。"""
        self._require_role(actor, (D.ROLE_COACH, D.ROLE_ADMIN), "仅教练或管理员可排课")
        with self._lock, self._tx():
            def do():
                row = self._require_trainee(trainee_id)
                if not self._assessments_ready(trainee_id):
                    raise ServiceError("医学与运动风险评估未完成，不能排入训练")
                if row["state"] in (D.ST_FROZEN, D.ST_REFERRED):
                    raise ServiceError("学员处于冻结/转诊状态，不能排入新课程")
                if row["state"] in D.TERMINAL_STATES:
                    raise ServiceError("报名已结束，不能排课")
                now = self.clock()
                try:
                    self.connection.execute(
                        "INSERT INTO sessions(session_id,trainee_id,scheduled_at,"
                        "coach_id,status,created_request_key,created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (session_id, trainee_id, scheduled_at, coach_id,
                         "scheduled", request_key, now))
                except sqlite3.IntegrityError:
                    raise ServiceError("课程编号冲突或同一时间已有课程")
                if row["state"] == D.ST_READY:
                    self._set_state(trainee_id, D.ST_SCHEDULED, actor,
                                    {"reason": "首次排入课程", "session_id": session_id})
                self._event(trainee_id, "session_scheduled", actor, {
                    "session_id": session_id, "scheduled_at": scheduled_at,
                    "coach_id": coach_id,
                })
                return {"session_id": session_id, "trainee_id": trainee_id,
                        "scheduled_at": scheduled_at, "status": "scheduled"}
            return self._idempotent(request_key, "schedule", do)

    def check_in(self, session_id, actor, request_key=None):
        """训练打卡。重复打卡不产生第二条结算/考勤记录。"""
        self._require_role(actor, (D.ROLE_COACH, D.ROLE_ADMIN), "仅教练可确认打卡")
        with self._lock, self._tx():
            def do():
                row = self.connection.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
                if row is None:
                    raise ServiceError("课程不存在")
                trainee = self._require_trainee(row["trainee_id"])
                if row["status"] == "attended":
                    raise ServiceError("该课程已打卡，请勿重复打卡")
                if row["status"] in ("frozen", "cancelled"):
                    raise ServiceError("课程已冻结或取消，不能打卡")
                if trainee["state"] in (D.ST_FROZEN, D.ST_REFERRED):
                    raise ServiceError("学员处于冻结/转诊状态，不能打卡")
                if trainee["state"] in D.TERMINAL_STATES:
                    raise ServiceError("报名已结束，不能打卡")
                now = self.clock()
                self.connection.execute(
                    "UPDATE sessions SET status='attended',checkin_at=?,"
                    "checkin_request_key=? WHERE session_id=?",
                    (now, request_key, session_id))
                self._event(row["trainee_id"], "checked_in", actor, {
                    "session_id": session_id, "checked_in_at": now,
                })
                return {"session_id": session_id,
                        "trainee_id": row["trainee_id"],
                        "status": "attended", "checked_in_at": now}
            return self._idempotent(request_key, "checkin", do)

    # -- 红旗、冻结、解冻、转诊 --------------------------------------------
    def report_red_flag(self, session_id, symptoms, detail, actor, request_key=None):
        """训练中报告红旗症状：冻结该学员后续全部课程并通知指定角色。"""
        symptoms = tuple(symptoms or ())
        unknown = [s for s in symptoms if s not in D.RED_FLAGS]
        if unknown:
            raise ServiceError(f"未知红旗症状：{','.join(unknown)}")
        if not symptoms:
            raise ServiceError("至少需要一项红旗症状")
        self._require_role(actor, (D.ROLE_COACH, D.ROLE_MEDIC, D.ROLE_ADMIN),
                           "仅教练、医务人员可报告红旗")
        with self._lock, self._tx():
            def do():
                session = self.connection.execute(
                    "SELECT * FROM sessions WHERE session_id=?", (session_id,)).fetchone()
                if session is None:
                    raise ServiceError("课程不存在")
                trainee_id = session["trainee_id"]
                row = self._require_trainee(trainee_id)
                if row["state"] in D.TERMINAL_STATES:
                    raise ServiceError("报名已结束")
                active = self.connection.execute(
                    "SELECT freeze_id FROM freezes WHERE trainee_id=? AND status='active'",
                    (trainee_id,)).fetchone()
                if active is not None:
                    raise ServiceError("已存在生效中的冻结，等待医务人员处理")
                freeze_id = f"frz-{trainee_id}-{session_id}"
                now = self.clock()
                self.connection.execute(
                    "INSERT INTO freezes(freeze_id,trainee_id,session_id,symptoms_json,"
                    "detail,reporter_id,reporter_role,status,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?)",
                    (freeze_id, trainee_id, session_id, json.dumps(symptoms), detail,
                     actor.user_id, actor.role, "active", now))
                self._event(trainee_id, "red_flag_reported", actor, {
                    "freeze_id": freeze_id, "session_id": session_id,
                    "symptoms": list(symptoms), "detail": detail,
                })
                # 冻结后续所有未开始课程（含当前课程，禁止继续训练）。
                upcoming = self.connection.execute(
                    "SELECT session_id FROM sessions WHERE trainee_id=? "
                    "AND status='scheduled'", (trainee_id,)).fetchall()
                frozen_sessions = []
                for s in upcoming:
                    self.connection.execute(
                        "UPDATE sessions SET status='frozen' WHERE session_id=?",
                        (s["session_id"],))
                    frozen_sessions.append(s["session_id"])
                    self._event(trainee_id, "session_frozen", actor,
                                {"session_id": s["session_id"], "freeze_id": freeze_id})
                self._set_state(trainee_id, D.ST_FROZEN, actor,
                                {"reason": "红旗症状", "freeze_id": freeze_id,
                                 "symptoms": list(symptoms)})
                self._enqueue_notifications(
                    trainee_id, D.NOTIFY_FREEZE, actor,
                    {"freeze_id": freeze_id, "session_id": session_id,
                     "symptoms": list(symptoms), "detail": detail},
                    suffix=freeze_id)
                return {"freeze_id": freeze_id, "trainee_id": trainee_id,
                        "status": "active", "symptoms": list(symptoms),
                        "frozen_sessions": frozen_sessions,
                        "notified_roles": list(D.NOTIFY_ROLES[D.NOTIFY_FREEZE])}
            return self._idempotent(request_key, "red_flag", do)

    def lift_freeze(self, trainee_id, note, actor, request_key=None):
        """解除冻结——只有授权医务人员可执行。"""
        self._require_role(actor, (D.ROLE_MEDIC,), "仅医务人员可解除冻结")
        if actor.user_id not in self.authorized_medic_ids:
            raise ServiceError("该医务人员未获解冻授权")
        with self._lock, self._tx():
            def do():
                self._require_trainee(trainee_id)
                freeze = self.connection.execute(
                    "SELECT * FROM freezes WHERE trainee_id=? AND status='active' "
                    "ORDER BY created_at DESC LIMIT 1", (trainee_id,)).fetchone()
                if freeze is None:
                    raise ServiceError("没有生效中的冻结")
                open_ref = self.connection.execute(
                    "SELECT 1 FROM referrals WHERE trainee_id=? AND status='open'",
                    (trainee_id,)).fetchone()
                if open_ref is not None:
                    raise ServiceError("转诊进行中，须先关闭转诊再解除冻结")
                now = self.clock()
                self.connection.execute(
                    "UPDATE freezes SET status='lifted',lifter_id=?,lifted_at=?,"
                    "lift_note=? WHERE freeze_id=?",
                    (actor.user_id, now, note, freeze["freeze_id"]))
                self._event(trainee_id, "freeze_lifted", actor, {
                    "freeze_id": freeze["freeze_id"], "note": note,
                    "basis": {"freeze_id": freeze["freeze_id"],
                              "symptoms": json.loads(freeze["symptoms_json"]),
                              "reporter_id": freeze["reporter_id"]},
                })
                # 被冻结的课程需要重新排课，故回到 ready 而非 scheduled。
                self._set_state(trainee_id, D.ST_READY, actor,
                                {"reason": "授权医务人员解除冻结",
                                 "freeze_id": freeze["freeze_id"]})
                self._enqueue_notifications(
                    trainee_id, D.NOTIFY_UNFREEZE, actor,
                    {"freeze_id": freeze["freeze_id"], "note": note},
                    suffix=freeze["freeze_id"])
                return {"freeze_id": freeze["freeze_id"], "trainee_id": trainee_id,
                        "status": "lifted", "lifted_by": actor.user_id,
                        "state": D.ST_READY}
            return self._idempotent(request_key, "lift_freeze", do)

    def refer(self, trainee_id, reason, actor, request_key=None):
        """冻结期间由医务人员发起转诊。"""
        self._require_role(actor, (D.ROLE_MEDIC,), "仅医务人员可发起转诊")
        with self._lock, self._tx():
            def do():
                self._require_trainee(trainee_id)
                freeze = self.connection.execute(
                    "SELECT * FROM freezes WHERE trainee_id=? AND status='active'",
                    (trainee_id,)).fetchone()
                if freeze is None:
                    raise ServiceError("仅在冻结生效期间可以转诊")
                exists = self.connection.execute(
                    "SELECT referral_id FROM referrals WHERE trainee_id=? AND status='open'",
                    (trainee_id,)).fetchone()
                if exists is not None:
                    raise ServiceError("已存在进行中的转诊")
                referral_id = f"ref-{trainee_id}-{freeze['freeze_id']}"
                now = self.clock()
                self.connection.execute(
                    "INSERT INTO referrals(referral_id,trainee_id,freeze_id,reason,"
                    "medic_id,status,created_at) VALUES(?,?,?,?,?,?,?)",
                    (referral_id, trainee_id, freeze["freeze_id"], reason,
                     actor.user_id, D.REFERRAL_OPEN, now))
                self._set_state(trainee_id, D.ST_REFERRED, actor,
                                {"reason": "医学转诊", "referral_id": referral_id,
                                 "freeze_id": freeze["freeze_id"]})
                self._event(trainee_id, "referred", actor, {
                    "referral_id": referral_id, "freeze_id": freeze["freeze_id"],
                    "reason": reason,
                    "basis": {"symptoms": json.loads(freeze["symptoms_json"]),
                              "detail": freeze["detail"]},
                })
                self._enqueue_notifications(
                    trainee_id, D.NOTIFY_REFERRAL, actor,
                    {"referral_id": referral_id, "reason": reason},
                    suffix=referral_id)
                return {"referral_id": referral_id, "trainee_id": trainee_id,
                        "status": D.REFERRAL_OPEN}
            return self._idempotent(request_key, "refer", do)

    def resolve_referral(self, trainee_id, outcome, actor, request_key=None):
        """医务人员关闭转诊；若无生效冻结则学员回到可排课状态。"""
        self._require_role(actor, (D.ROLE_MEDIC,), "仅医务人员可关闭转诊")
        with self._lock, self._tx():
            def do():
                self._require_trainee(trainee_id)
                ref = self.connection.execute(
                    "SELECT * FROM referrals WHERE trainee_id=? AND status='open' "
                    "ORDER BY created_at DESC LIMIT 1", (trainee_id,)).fetchone()
                if ref is None:
                    raise ServiceError("没有进行中的转诊")
                now = self.clock()
                self.connection.execute(
                    "UPDATE referrals SET status=?,outcome=?,resolved_at=? "
                    "WHERE referral_id=?",
                    (D.REFERRAL_RESOLVED, outcome, now, ref["referral_id"]))
                self._event(trainee_id, "referral_resolved", actor,
                            {"referral_id": ref["referral_id"], "outcome": outcome})
                active_freeze = self.connection.execute(
                    "SELECT 1 FROM freezes WHERE trainee_id=? AND status='active'",
                    (trainee_id,)).fetchone()
                if active_freeze is None:
                    self._set_state(trainee_id, D.ST_READY, actor,
                                    {"reason": "转诊结束且无生效冻结",
                                     "referral_id": ref["referral_id"]})
                return {"referral_id": ref["referral_id"], "trainee_id": trainee_id,
                        "status": D.REFERRAL_RESOLVED}
            return self._idempotent(request_key, "resolve_referral", do)

    # -- 退款 --------------------------------------------------------------
    def request_refund(self, trainee_id, reason, actor, amount=None,
                       request_key=None):
        """发起退款请求（学员本人/管理员/财务均可），通知财务。重复请求拒绝。"""
        self._require_role(actor, (D.ROLE_TRAINEE, D.ROLE_ADMIN, D.ROLE_FINANCE),
                           "无权申请退款")
        with self._lock, self._tx():
            def do():
                row = self._require_trainee(trainee_id)
                if actor.role == D.ROLE_TRAINEE and row["owner_id"] != actor.user_id:
                    raise ServiceError("只能为本人申请退款")
                existing = self.connection.execute(
                    "SELECT refund_id,status FROM refunds WHERE trainee_id=?",
                    (trainee_id,)).fetchone()
                if existing is not None:
                    raise ServiceError(
                        f"退款已存在（{existing['status']}），请勿重复发起")
                resolved_amount = amount
                if resolved_amount is None:
                    profile = json.loads(row["profile_json"])
                    resolved_amount = profile.get("payment_amount", 0)
                # 决定依据：当前冻结/转诊/取消情况一并固化。
                basis = self._decision_basis(trainee_id)
                refund_id = f"rfd-{trainee_id}"
                now = self.clock()
                self.connection.execute(
                    "INSERT INTO refunds(refund_id,trainee_id,reason,amount,"
                    "requested_by,status,request_key,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?)",
                    (refund_id, trainee_id, reason, resolved_amount,
                     actor.user_id, D.REFUND_REQUESTED, request_key, now))
                self._event(trainee_id, "refund_requested", actor, {
                    "refund_id": refund_id, "reason": reason,
                    "amount": resolved_amount,
                    "basis": basis,
                })
                self._enqueue_notifications(
                    trainee_id, D.NOTIFY_REFUND, actor,
                    {"refund_id": refund_id, "phase": "requested",
                     "reason": reason, "amount": resolved_amount},
                    suffix=refund_id + ":request")
                return {"refund_id": refund_id, "trainee_id": trainee_id,
                        "status": D.REFUND_REQUESTED,
                        "amount": resolved_amount}
            return self._idempotent(request_key, "refund_request", do)

    def settle_refund(self, trainee_id, approve, actor, settlement_id=None,
                      note=None, request_key=None):
        """财务审批退款：批准则结算（仅一次），拒绝则关闭请求。"""
        self._require_role(actor, (D.ROLE_FINANCE, D.ROLE_ADMIN), "仅财务可处理退款")
        with self._lock, self._tx():
            def do():
                self._require_trainee(trainee_id)
                refund = self.connection.execute(
                    "SELECT * FROM refunds WHERE trainee_id=?",
                    (trainee_id,)).fetchone()
                if refund is None:
                    raise ServiceError("没有退款请求")
                if refund["status"] != D.REFUND_REQUESTED:
                    raise ServiceError(
                        f"退款已{refund['status']}，不能重复结算")
                now = self.clock()
                new_status = D.REFUND_SETTLED if approve else D.REFUND_REJECTED
                sid = settlement_id or (f"st-{refund['refund_id']}" if approve else None)
                self.connection.execute(
                    "UPDATE refunds SET status=?,settlement_id=?,settled_by=?,"
                    "settled_at=?,settle_note=? WHERE refund_id=?",
                    (new_status, sid, actor.user_id, now if approve else None,
                     note, refund["refund_id"]))
                self._event(trainee_id,
                            "refund_settled" if approve else "refund_rejected",
                            actor, {"refund_id": refund["refund_id"],
                                    "settlement_id": sid, "note": note,
                                    "amount": refund["amount"]})
                if approve:
                    self._set_state(trainee_id, D.ST_REFUNDED, actor,
                                    {"reason": "退款已结算",
                                     "refund_id": refund["refund_id"],
                                     "settlement_id": sid})
                self._enqueue_notifications(
                    trainee_id, D.NOTIFY_REFUND, actor,
                    {"refund_id": refund["refund_id"], "phase": new_status,
                     "settlement_id": sid},
                    suffix=refund["refund_id"] + ":" + new_status)
                return {"refund_id": refund["refund_id"], "trainee_id": trainee_id,
                        "status": new_status, "settlement_id": sid}
            return self._idempotent(request_key, "refund_settle", do)

    def _decision_basis(self, trainee_id):
        """汇总退款等决定所依据的当前事实（冻结、转诊、取消原因）。"""
        freeze = self.connection.execute(
            "SELECT freeze_id,symptoms_json,detail,created_at,status FROM freezes "
            "WHERE trainee_id=? ORDER BY created_at DESC LIMIT 1",
            (trainee_id,)).fetchone()
        ref = self.connection.execute(
            "SELECT referral_id,reason,status,created_at FROM referrals "
            "WHERE trainee_id=? ORDER BY created_at DESC LIMIT 1",
            (trainee_id,)).fetchone()
        return {
            "freeze": (None if freeze is None else
                       {"freeze_id": freeze["freeze_id"],
                        "symptoms": json.loads(freeze["symptoms_json"]),
                        "detail": freeze["detail"], "status": freeze["status"],
                        "created_at": freeze["created_at"]}),
            "referral": (None if ref is None else dict(ref)),
        }

    # -- 营期取消 ----------------------------------------------------------
    def cancel_camp(self, reason, actor, request_key=None):
        """取消整个训练营：全部未结束报名置为 cancelled，决定与通知保留。"""
        self._require_role(actor, (D.ROLE_ADMIN,), "仅管理员可取消训练营")
        with self._lock, self._tx():
            def do():
                cancel_id = f"camp-cancel-{self.clock()}"
                rows = self.connection.execute(
                    "SELECT trainee_id FROM trainees WHERE state NOT IN (?,?)",
                    (D.ST_CANCELLED, D.ST_REFUNDED)).fetchall()
                cancelled = []
                for row in rows:
                    tid = row["trainee_id"]
                    self.connection.execute(
                        "UPDATE sessions SET status='cancelled' WHERE trainee_id=? "
                        "AND status IN ('scheduled','frozen')", (tid,))
                    self._set_state(tid, D.ST_CANCELLED, actor,
                                    {"reason": "训练营取消", "camp_reason": reason,
                                     "cancel_id": cancel_id})
                    self._enqueue_notifications(
                        tid, D.NOTIFY_CAMP_CANCELLED, actor,
                        {"cancel_id": cancel_id, "reason": reason},
                        suffix=cancel_id)
                    cancelled.append(tid)
                return {"cancel_id": cancel_id, "cancelled_trainees": cancelled}
            return self._idempotent(request_key, "cancel_camp", do)

    # -- 通知 --------------------------------------------------------------
    def _enqueue_notifications(self, trainee_id, topic, actor, body, suffix):
        """必须在事务内调用；按 topic 的固定角色清单入队，编号确定性保证不重复。"""
        now = self.clock()
        for role in D.NOTIFY_ROLES[topic]:
            nid = f"{trainee_id}:{topic}:{suffix}:{role}"
            self.connection.execute(
                "INSERT INTO notifications(notification_id,trainee_id,topic,role,"
                "status,body_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (nid, trainee_id, topic, role, "pending",
                 json.dumps(body, ensure_ascii=False), now))
            self._event(trainee_id, "notification_enqueued", actor,
                        {"notification_id": nid, "topic": topic, "role": role})

    def deliver_notification(self, notification_id, actor):
        """将通知标记为已送达（幂等）。"""
        with self._lock, self._tx():
            row = self.connection.execute(
                "SELECT * FROM notifications WHERE notification_id=?",
                (notification_id,)).fetchone()
            if row is None:
                raise ServiceError("通知不存在")
            if actor.role != D.ROLE_ADMIN and actor.role != row["role"]:
                raise ServiceError("不能处理其他角色的通知")
            if row["status"] == "pending":
                self.connection.execute(
                    "UPDATE notifications SET status='delivered',delivered_at=? "
                    "WHERE notification_id=?", (self.clock(), notification_id))
            fresh = self.connection.execute(
                "SELECT notification_id,trainee_id,topic,role,status,body_json,"
                "created_at,delivered_at FROM notifications WHERE notification_id=?",
                (notification_id,)).fetchone()
            return dict(fresh)

    def list_notifications(self, actor, topic=None):
        """列出某角色可见的通知及其送达状态。"""
        with self._lock:
            if topic:
                rows = self.connection.execute(
                    "SELECT notification_id,trainee_id,topic,role,status,body_json,"
                    "created_at,delivered_at FROM notifications WHERE role=? AND topic=? "
                    "ORDER BY created_at", (actor.role, topic)).fetchall()
            else:
                rows = self.connection.execute(
                    "SELECT notification_id,trainee_id,topic,role,status,body_json,"
                    "created_at,delivered_at FROM notifications WHERE role=? "
                    "ORDER BY created_at", (actor.role,)).fetchall()
            return [dict(r) for r in rows]

    # -- 视图与可执行动作 --------------------------------------------------
    def trainee_view(self, trainee_id, actor):
        """按角色裁剪的学员视图：病史等敏感字段只对授权角色呈现。"""
        with self._lock:
            row = self._require_trainee(trainee_id)
            profile = json.loads(row["profile_json"])
            visible = D.ROLE_VISIBLE_FIELDS.get(actor.role, frozenset())
            view = {
                "trainee_id": row["trainee_id"], "name": row["name"],
                "state": row["state"], "version": row["version"],
                "updated_at": row["updated_at"],
            }
            if actor.role == D.ROLE_TRAINEE and row["owner_id"] != actor.user_id:
                raise ServiceError("只能查看本人信息")
            is_owner = actor.role == D.ROLE_TRAINEE and row["owner_id"] == actor.user_id
            field_blocks = {"medical_history": "medical_history",
                            "contraindications": "contraindications",
                            "medications": "medications",
                            "fitness_level": "fitness_level",
                            "vitals": "vitals",
                            "payment_amount": "payment"}
            for key, block in field_blocks.items():
                if key in profile and (block in visible or is_owner):
                    view[key] = profile[key]
            # 评估完成情况只暴露签署事实与哈希；正文仅签署角色本人与管理员可见。
            assessments = self.connection.execute(
                "SELECT kind,signer_id,signer_role,content_hash,body_json,"
                "signed_at FROM assessments WHERE trainee_id=? ORDER BY kind",
                (trainee_id,)).fetchall()
            assessment_views = []
            for ar in assessments:
                item = {k: ar[k] for k in
                        ("kind", "signer_id", "signer_role",
                         "content_hash", "signed_at")}
                if actor.role == D.ROLE_ADMIN or actor.role == ar["signer_role"]:
                    item["content"] = json.loads(ar["body_json"])
                assessment_views.append(item)
            view["assessments"] = assessment_views
            freeze = self.connection.execute(
                "SELECT freeze_id,session_id,symptoms_json,detail,reporter_role,"
                "status,created_at,lifter_id,lifted_at,lift_note FROM freezes "
                "WHERE trainee_id=? ORDER BY created_at DESC LIMIT 1",
                (trainee_id,)).fetchone()
            if freeze is not None:
                freeze_view = dict(freeze)
                freeze_view["symptoms"] = json.loads(freeze_view.pop("symptoms_json"))
                if "red_flag_detail" not in visible and actor.role != D.ROLE_TRAINEE:
                    freeze_view.pop("detail", None)
                view["freeze"] = freeze_view
            ref = self.connection.execute(
                "SELECT referral_id,freeze_id,reason,medic_id,status,outcome,"
                "created_at,resolved_at FROM referrals WHERE trainee_id=? "
                "ORDER BY created_at DESC LIMIT 1", (trainee_id,)).fetchone()
            if ref is not None and "referral" in visible:
                view["referral"] = dict(ref)
            refund = self.connection.execute(
                "SELECT refund_id,reason,amount,requested_by,status,settlement_id,"
                "settled_at FROM refunds WHERE trainee_id=?",
                (trainee_id,)).fetchone()
            if refund is not None and (
                    "refund" in visible or is_owner):
                view["refund"] = dict(refund)
            sessions = self.connection.execute(
                "SELECT session_id,scheduled_at,coach_id,status,checkin_at "
                "FROM sessions WHERE trainee_id=? ORDER BY scheduled_at",
                (trainee_id,)).fetchall()
            view["sessions"] = [dict(s) for s in sessions]
            return view

    def available_actions(self, trainee_id, actor):
        """返回该操作人对该学员“当前可执行动作”清单及禁止原因，供现场逐项核对。"""
        with self._lock:
            row = self._require_trainee(trainee_id)
            state = row["state"]
            role = actor.role
            if role == D.ROLE_TRAINEE and row["owner_id"] != actor.user_id:
                raise ServiceError("只能核对本人的可执行动作")
            medical = self.connection.execute(
                "SELECT 1 FROM assessments WHERE trainee_id=? AND kind=?",
                (trainee_id, D.ASSESSMENT_MEDICAL)).fetchone() is not None
            fitness = self.connection.execute(
                "SELECT 1 FROM assessments WHERE trainee_id=? AND kind=?",
                (trainee_id, D.ASSESSMENT_FITNESS)).fetchone() is not None
            active_freeze = self.connection.execute(
                "SELECT 1 FROM freezes WHERE trainee_id=? AND status='active'",
                (trainee_id,)).fetchone() is not None
            open_referral = self.connection.execute(
                "SELECT 1 FROM referrals WHERE trainee_id=? AND status='open'",
                (trainee_id,)).fetchone() is not None
            refund_row = self.connection.execute(
                "SELECT status FROM refunds WHERE trainee_id=?",
                (trainee_id,)).fetchone()
            next_session = self.connection.execute(
                "SELECT session_id,status FROM sessions WHERE trainee_id=? "
                "AND status='scheduled' ORDER BY scheduled_at LIMIT 1",
                (trainee_id,)).fetchone()
            reportable_session = self.connection.execute(
                "SELECT 1 FROM sessions WHERE trainee_id=? "
                "AND status IN ('scheduled','attended') LIMIT 1",
                (trainee_id,)).fetchone() is not None
            terminal = state in D.TERMINAL_STATES

            def a(allowed, reason=""):
                return {"allowed": bool(allowed), "reason": reason}

            actions = {}
            # 评估签署
            actions["sign_medical_assessment"] = a(
                role == D.ROLE_MEDIC and not medical and not terminal,
                "医学评估已签署，不可覆盖" if medical else
                ("终态不可操作" if terminal else
                 ("仅医务人员可签署" if role != D.ROLE_MEDIC else "")))
            actions["sign_fitness_assessment"] = a(
                role == D.ROLE_COACH and not fitness and not terminal,
                "运动风险评估已签署，不可覆盖" if fitness else
                ("终态不可操作" if terminal else
                 ("仅教练可签署" if role != D.ROLE_COACH else "")))
            actions["submit_supplement"] = a(not terminal,
                                             "终态不可操作" if terminal else "")
            # 排课：两项评估齐备且不在冻结/转诊/终态
            can_schedule = (role in (D.ROLE_COACH, D.ROLE_ADMIN)
                            and medical and fitness and not terminal
                            and state not in (D.ST_FROZEN, D.ST_REFERRED))
            why = ""
            if role not in (D.ROLE_COACH, D.ROLE_ADMIN):
                why = "仅教练或管理员可排课"
            elif not (medical and fitness):
                why = "医学与运动风险评估未完成"
            elif state in (D.ST_FROZEN, D.ST_REFERRED):
                why = "冻结/转诊未解除"
            elif terminal:
                why = "终态不可操作"
            actions["schedule_session"] = a(can_schedule, why)
            # 打卡
            can_check = (role in (D.ROLE_COACH, D.ROLE_ADMIN)
                         and next_session is not None
                         and state not in (D.ST_FROZEN, D.ST_REFERRED)
                         and not terminal)
            why = ""
            if role not in (D.ROLE_COACH, D.ROLE_ADMIN):
                why = "仅教练可确认打卡"
            elif next_session is None:
                why = "没有待打卡课程"
            elif state in (D.ST_FROZEN, D.ST_REFERRED):
                why = "冻结/转诊中课程已冻结"
            elif terminal:
                why = "终态不可操作"
            actions["check_in"] = a(can_check, why)
            if next_session is not None:
                actions["check_in"]["next_session_id"] = next_session["session_id"]
            # 红旗上报
            can_report = (role in (D.ROLE_COACH, D.ROLE_MEDIC, D.ROLE_ADMIN)
                          and not active_freeze and not terminal
                          and reportable_session)
            why = ""
            if role not in (D.ROLE_COACH, D.ROLE_MEDIC, D.ROLE_ADMIN):
                why = "仅教练或医务人员可上报"
            elif active_freeze:
                why = "已有生效冻结"
            elif not reportable_session:
                why = "没有可上报红旗的课程"
            elif terminal:
                why = "终态不可操作"
            actions["report_red_flag"] = a(can_report, why)
            # 解冻：授权医务人员 + 有生效冻结
            can_lift = (role == D.ROLE_MEDIC
                        and actor.user_id in self.authorized_medic_ids
                        and active_freeze)
            why = ""
            if role != D.ROLE_MEDIC:
                why = "仅医务人员可解除冻结"
            elif actor.user_id not in self.authorized_medic_ids:
                why = "该医务人员未获解冻授权"
            elif not active_freeze:
                why = "没有生效中的冻结"
            actions["lift_freeze"] = a(can_lift, why)
            # 转诊
            actions["refer"] = a(
                role == D.ROLE_MEDIC and active_freeze and not open_referral,
                "仅冻结期间可转诊" if role == D.ROLE_MEDIC and not active_freeze else
                ("转诊已在进行中" if open_referral else
                 ("仅医务人员可转诊" if role != D.ROLE_MEDIC else "")))
            actions["resolve_referral"] = a(
                role == D.ROLE_MEDIC and open_referral,
                "没有进行中的转诊" if role == D.ROLE_MEDIC and not open_referral else
                ("仅医务人员可关闭转诊" if role != D.ROLE_MEDIC else ""))
            # 退款
            owner = role == D.ROLE_TRAINEE and row["owner_id"] == actor.user_id
            can_request = (role in (D.ROLE_ADMIN, D.ROLE_FINANCE) or owner) \
                and refund_row is None and not terminal
            why = ""
            if not (role in (D.ROLE_ADMIN, D.ROLE_FINANCE) or owner):
                why = "无权申请退款"
            elif refund_row is not None:
                why = f"退款已存在（{refund_row['status']}）"
            elif terminal:
                why = "终态不可操作"
            actions["request_refund"] = a(can_request, why)
            actions["settle_refund"] = a(
                role in (D.ROLE_FINANCE, D.ROLE_ADMIN)
                and refund_row is not None
                and refund_row["status"] == D.REFUND_REQUESTED,
                "没有待处理退款" if role in (D.ROLE_FINANCE, D.ROLE_ADMIN)
                and not (refund_row is not None
                         and refund_row["status"] == D.REFUND_REQUESTED)
                else ("仅财务可处理退款" if role not in
                      (D.ROLE_FINANCE, D.ROLE_ADMIN) else ""))
            # 营期取消仅管理员，且从全局动作看，这里给出按学员的一致性投影
            actions["cancel_camp"] = a(False, "请使用全局取消接口"
                                       if role == D.ROLE_ADMIN else "仅管理员可取消")
            return {"trainee_id": trainee_id, "state": state,
                    "actor": {"user_id": actor.user_id, "role": role},
                    "actions": actions}

    def list_trainees(self):
        with self._lock:
            rows = self.connection.execute(
                "SELECT trainee_id,name,owner_id,state,version,updated_at "
                "FROM trainees ORDER BY trainee_id").fetchall()
            return [dict(r) for r in rows]

    def _snapshot(self, trainee_id):
        row = self._require_trainee(trainee_id)
        return {"trainee_id": row["trainee_id"], "name": row["name"],
                "state": row["state"], "version": row["version"],
                "assessments_complete": self._assessments_ready(trainee_id)}


_SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS trainees(
  trainee_id TEXT PRIMARY KEY, name TEXT NOT NULL, owner_id TEXT NOT NULL,
  profile_json TEXT NOT NULL, state TEXT NOT NULL, version INTEGER NOT NULL,
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS assessments(
  trainee_id TEXT NOT NULL, kind TEXT NOT NULL, signer_id TEXT NOT NULL,
  signer_role TEXT NOT NULL, content_hash TEXT NOT NULL, body_json TEXT NOT NULL,
  signed_at TEXT NOT NULL, request_key TEXT,
  PRIMARY KEY(trainee_id,kind));
CREATE TABLE IF NOT EXISTS supplements(
  supplement_id TEXT PRIMARY KEY, trainee_id TEXT NOT NULL, body_json TEXT NOT NULL,
  submitted_by TEXT NOT NULL, submitted_role TEXT NOT NULL, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY, trainee_id TEXT NOT NULL, scheduled_at TEXT NOT NULL,
  coach_id TEXT NOT NULL, status TEXT NOT NULL, checkin_at TEXT,
  checkin_request_key TEXT UNIQUE, created_request_key TEXT, created_at TEXT NOT NULL,
  UNIQUE(trainee_id,scheduled_at));
CREATE TABLE IF NOT EXISTS freezes(
  freeze_id TEXT PRIMARY KEY, trainee_id TEXT NOT NULL, session_id TEXT NOT NULL,
  symptoms_json TEXT NOT NULL, detail TEXT, reporter_id TEXT NOT NULL,
  reporter_role TEXT NOT NULL, status TEXT NOT NULL, created_at TEXT NOT NULL,
  lifter_id TEXT, lifted_at TEXT, lift_note TEXT);
CREATE TABLE IF NOT EXISTS referrals(
  referral_id TEXT PRIMARY KEY, trainee_id TEXT NOT NULL, freeze_id TEXT NOT NULL,
  reason TEXT NOT NULL, medic_id TEXT NOT NULL, status TEXT NOT NULL,
  outcome TEXT, created_at TEXT NOT NULL, resolved_at TEXT);
CREATE TABLE IF NOT EXISTS refunds(
  refund_id TEXT PRIMARY KEY, trainee_id TEXT NOT NULL UNIQUE, reason TEXT NOT NULL,
  amount REAL NOT NULL, requested_by TEXT NOT NULL, status TEXT NOT NULL,
  request_key TEXT, settlement_id TEXT, settled_by TEXT, settled_at TEXT,
  settle_note TEXT, created_at TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS notifications(
  notification_id TEXT PRIMARY KEY, trainee_id TEXT NOT NULL, topic TEXT NOT NULL,
  role TEXT NOT NULL, status TEXT NOT NULL, body_json TEXT NOT NULL,
  created_at TEXT NOT NULL, delivered_at TEXT);
CREATE TABLE IF NOT EXISTS events(
  seq INTEGER PRIMARY KEY AUTOINCREMENT, event_id TEXT UNIQUE NOT NULL,
  trainee_id TEXT NOT NULL, kind TEXT NOT NULL, actor_id TEXT NOT NULL,
  actor_role TEXT NOT NULL, body_json TEXT NOT NULL, created_at TEXT NOT NULL,
  prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS idempotency(
  request_key TEXT PRIMARY KEY, op TEXT NOT NULL, response_json TEXT NOT NULL,
  created_at TEXT NOT NULL);
"""
