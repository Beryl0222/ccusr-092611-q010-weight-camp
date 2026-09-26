"""减重训练风险台的持久化边界与业务服务。

设计要点：
- 所有写操作都在单连接事务（BEGIN IMMEDIATE）中完成，失败整体回滚；
- request_key 幂等表 + 数据库唯一约束双保险，重复打卡/重复退款请求不会重复结算；
- 评估一经签署只允许追加“补交资料”，评估本体不可覆盖；
- 红旗触发后冻结该学员全部后续课程并按订阅表通知指定角色，仅授权医务人员可解冻；
- audit_log 为哈希链（sha256），并由触发器禁止 UPDATE/DELETE，重启后可重算校验。
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from contextlib import contextmanager
from functools import wraps

from .domain import (
    ASSESSMENT_EXERCISE,
    ASSESSMENT_KINDS,
    ASSESSMENT_MEDICAL,
    NOTIFY_PLAN,
    RED_FLAGS,
    ROLE_ADMIN,
    ROLE_COACH,
    ROLE_FINANCE,
    ROLE_MEDIC,
    ROLE_OWNER,
    SIGN_AUTHORITY,
    Record,
    utc_now,
)

GENESIS_HASH = "GENESIS"


def synchronized(fn):
    """所有公开方法串行化，保证多线程共用单连接时事务不交错（锁可重入）。"""

    @wraps(fn)
    def wrapper(self, *args, **kwargs):
        with self._lock:
            return fn(self, *args, **kwargs)

    return wrapper


class ServiceError(Exception):
    """业务错误，status 给出建议的 HTTP 状态码。"""

    def __init__(self, message: str, status: int = 409):
        super().__init__(message)
        self.status = status


def _canonical(data) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS camps(
  camp_id TEXT PRIMARY KEY,
  status TEXT NOT NULL,
  created_at TEXT NOT NULL,
  cancelled_at TEXT
);
CREATE TABLE IF NOT EXISTS enrollees(
  enrollee_id TEXT PRIMARY KEY,
  camp_id TEXT NOT NULL REFERENCES camps(camp_id),
  name TEXT NOT NULL,
  fee_paid_cents INTEGER NOT NULL,
  status TEXT NOT NULL,             -- enrolled | paused | referred
  frozen INTEGER NOT NULL DEFAULT 0,
  version INTEGER NOT NULL DEFAULT 1,
  created_at TEXT NOT NULL,
  updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS assessments(
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  kind TEXT NOT NULL,
  signed_by TEXT NOT NULL,
  signed_at TEXT NOT NULL,
  content TEXT NOT NULL,
  content_hash TEXT NOT NULL,
  PRIMARY KEY(enrollee_id, kind)
);
CREATE TABLE IF NOT EXISTS supplements(
  supplement_id TEXT PRIMARY KEY,
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  kind TEXT NOT NULL,
  content TEXT NOT NULL,
  submitted_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS red_flags(
  flag_id TEXT PRIMARY KEY,
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  symptom TEXT NOT NULL,
  reported_by TEXT NOT NULL,
  checkin_id TEXT,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS referrals(
  referral_id TEXT PRIMARY KEY,
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  reason TEXT NOT NULL,
  opened_by TEXT NOT NULL,
  opened_at TEXT NOT NULL,
  status TEXT NOT NULL,             -- open | resolved
  resolution TEXT,
  resolved_by TEXT,
  resolved_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  session_id TEXT PRIMARY KEY,
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  seq INTEGER NOT NULL,
  scheduled_at TEXT NOT NULL,
  status TEXT NOT NULL,             -- scheduled | frozen | cancelled | completed
  price_cents INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL,
  UNIQUE(enrollee_id, seq)
);
CREATE TABLE IF NOT EXISTS checkins(
  checkin_id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL UNIQUE REFERENCES sessions(session_id),
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  checked_by TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS refunds(
  enrollee_id TEXT PRIMARY KEY REFERENCES enrollees(enrollee_id),
  request_key TEXT NOT NULL UNIQUE,
  amount_cents INTEGER NOT NULL,
  reason TEXT NOT NULL,
  status TEXT NOT NULL,             -- requested | approved | rejected
  requested_by TEXT NOT NULL,
  requested_at TEXT NOT NULL,
  decided_by TEXT,
  decided_at TEXT,
  decision_note TEXT
);
CREATE TABLE IF NOT EXISTS ledger(
  entry_id TEXT PRIMARY KEY,        -- check:<checkin_id> / refund:<enrollee_id>
  enrollee_id TEXT NOT NULL REFERENCES enrollees(enrollee_id),
  kind TEXT NOT NULL,               -- session_charge | refund
  amount_cents INTEGER NOT NULL,   -- 扣费为正、退款为负
  ref_type TEXT NOT NULL,
  ref_id TEXT NOT NULL,
  created_at TEXT NOT NULL,
  UNIQUE(ref_type, ref_id)
);
CREATE TABLE IF NOT EXISTS notifications(
  notification_id TEXT PRIMARY KEY, -- <event_id>:<role>
  event_id TEXT NOT NULL,
  enrollee_id TEXT,
  role TEXT NOT NULL,
  status TEXT NOT NULL,             -- pending | delivered
  created_at TEXT NOT NULL,
  delivered_at TEXT,
  UNIQUE(event_id, role)
);
CREATE TABLE IF NOT EXISTS audit_log(
  seq INTEGER PRIMARY KEY AUTOINCREMENT,
  event_id TEXT NOT NULL UNIQUE,
  at TEXT NOT NULL,
  actor TEXT NOT NULL,
  action TEXT NOT NULL,
  details TEXT NOT NULL,
  prev_hash TEXT NOT NULL,
  entry_hash TEXT NOT NULL
);
CREATE TRIGGER IF NOT EXISTS audit_no_update
BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT,'审计记录不可修改'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete
BEFORE DELETE ON audit_log
BEGIN SELECT RAISE(ABORT,'审计记录不可删除'); END;
CREATE TABLE IF NOT EXISTS idempotency(
  request_key TEXT PRIMARY KEY,
  response TEXT NOT NULL,
  created_at TEXT NOT NULL
);
"""


def synchronized_methods(cls):
    """公开方法统一加实例锁；私有辅助方法在同线程的可重入锁内执行。"""
    for name, value in list(vars(cls).items()):
        if not name.startswith("_") and callable(value):
            setattr(cls, name, synchronized(value))
    return cls


@synchronized_methods
class CampService:
    """训练营风险业务服务。线程模型：单连接 + BEGIN IMMEDIATE 串行写。"""

    # 各命令允许的角色
    _CAN_REPORT_FLAG = (ROLE_COACH, ROLE_MEDIC)
    _CAN_REFER = (ROLE_MEDIC,)
    _CAN_FREEZE_CONTROL = (ROLE_MEDIC,)
    _CAN_PAUSE = (ROLE_OWNER, ROLE_COACH, ROLE_ADMIN)
    _CAN_REFUND_DECIDE = (ROLE_FINANCE,)
    _CAN_CANCEL_CAMP = (ROLE_ADMIN,)

    def __init__(self, database: str = ":memory:", clock=utc_now):
        self._lock = threading.RLock()
        self.connection = sqlite3.connect(database, check_same_thread=False)
        self.connection.row_factory = sqlite3.Row
        self.clock = clock
        self.connection.executescript(SCHEMA)
        self.connection.commit()

    def close(self):
        self.connection.close()

    # ---------- 基础设施 ----------

    @contextmanager
    def _tx(self):
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            yield
            self.connection.commit()
        except Exception:
            self.connection.rollback()
            raise

    def _idempotent(self, request_key: str, producer):
        """request_key 去重：命中则直接返回首次结果，不执行 producer。"""
        if not request_key:
            raise ServiceError("缺少 request_key", 400)
        row = self.connection.execute(
            "SELECT response FROM idempotency WHERE request_key=?", (request_key,)
        ).fetchone()
        if row is not None:
            return json.loads(row["response"])
        result = producer()
        self.connection.execute(
            "INSERT INTO idempotency VALUES(?,?,?)",
            (request_key, json.dumps(result, ensure_ascii=False), self.clock()),
        )
        return result

    def _audit(self, actor: str, action: str, details: dict, event_id: str | None = None) -> str:
        """追加一条哈希链审计事件，并按订阅表生成通知。必须在事务内调用。"""
        now = self.clock()
        event_id = event_id or f"{action}:{_sha256(actor + action + _canonical(details) + now)[:16]}"
        prev = self.connection.execute(
            "SELECT entry_hash FROM audit_log ORDER BY seq DESC LIMIT 1"
        ).fetchone()
        prev_hash = prev["entry_hash"] if prev else GENESIS_HASH
        canonical = _canonical(details)
        entry_hash = _sha256(prev_hash + "\n" + now + "\n" + actor + "\n" + action + "\n" + canonical)
        self.connection.execute(
            "INSERT INTO audit_log(event_id,at,actor,action,details,prev_hash,entry_hash)"
            " VALUES(?,?,?,?,?,?,?)",
            (event_id, now, actor, action, canonical, prev_hash, entry_hash),
        )
        for role in NOTIFY_PLAN.get(action, ()):
            self.connection.execute(
                "INSERT OR IGNORE INTO notifications VALUES(?,?,?,?,?,?,?)",
                (
                    f"{event_id}:{role}",
                    event_id,
                    details.get("enrollee_id"),
                    role,
                    "pending",
                    now,
                    None,
                ),
            )
        return event_id

    def verify_chain(self) -> dict:
        """重算哈希链，供重启后核对；返回首尾序号与校验结果。"""
        rows = self.connection.execute("SELECT * FROM audit_log ORDER BY seq").fetchall()
        prev_hash = GENESIS_HASH
        for row in rows:
            canonical = _canonical(json.loads(row["details"]))
            expect = _sha256(
                prev_hash + "\n" + row["at"] + "\n" + row["actor"] + "\n"
                + row["action"] + "\n" + canonical
            )
            if row["prev_hash"] != prev_hash or row["entry_hash"] != expect:
                raise ServiceError(f"审计哈希链在 seq={row['seq']} 处不一致", 500)
            prev_hash = row["entry_hash"]
        return {"events": len(rows), "head_hash": prev_hash, "ok": True}

    # ---------- 读取辅助 ----------

    def _camp(self, camp_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM camps WHERE camp_id=?", (camp_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("训练营不存在", 404)
        return row

    def _enrollee(self, enrollee_id: str) -> sqlite3.Row:
        row = self.connection.execute(
            "SELECT * FROM enrollees WHERE enrollee_id=?", (enrollee_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("学员不存在", 404)
        return row

    @staticmethod
    def _require_roles(actor: dict, roles, message="无权操作"):
        if actor.get("role") not in roles:
            raise ServiceError(message, 403)

    @staticmethod
    def _as_actor(actor) -> dict:
        if not isinstance(actor, dict) or "user_id" not in actor or "role" not in actor:
            raise ServiceError("调用方身份缺失", 401)
        return actor

    def _touch(self, enrollee_id: str):
        self.connection.execute(
            "UPDATE enrollees SET version=version+1, updated_at=? WHERE enrollee_id=?",
            (self.clock(), enrollee_id),
        )

    # ---------- 营期与报名 ----------

    def create_camp(self, actor, camp_id: str, request_key: str):
        actor = self._as_actor(actor)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._create_camp(actor, camp_id),
            )

    def _create_camp(self, actor, camp_id):
        now = self.clock()
        try:
            self.connection.execute(
                "INSERT INTO camps VALUES(?,?,?,?)", (camp_id, "active", now, None)
            )
        except sqlite3.IntegrityError:
            raise ServiceError("训练营已存在", 409)
        self._audit(actor["user_id"], "camp_created", {"camp_id": camp_id}, f"camp_created:{camp_id}")
        return {"camp_id": camp_id, "status": "active"}

    def enroll(self, actor, enrollee_id: str, camp_id: str, name: str,
               fee_paid_cents: int, request_key: str):
        actor = self._as_actor(actor)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._enroll(actor, enrollee_id, camp_id, name, fee_paid_cents),
            )

    def _enroll(self, actor, enrollee_id, camp_id, name, fee_paid_cents):
        self._camp(camp_id)
        if fee_paid_cents < 0:
            raise ServiceError("已缴费用不能为负", 400)
        now = self.clock()
        try:
            self.connection.execute(
                "INSERT INTO enrollees(enrollee_id,camp_id,name,fee_paid_cents,status,frozen,"
                "version,created_at,updated_at) VALUES(?,?,?,?,?,0,1,?,?)",
                (enrollee_id, camp_id, name, fee_paid_cents, "enrolled", now, now),
            )
        except sqlite3.IntegrityError:
            raise ServiceError("学员已报名", 409)
        self._audit(
            actor["user_id"], "enrolled",
            {"enrollee_id": enrollee_id, "camp_id": camp_id, "name": name},
            f"enrolled:{enrollee_id}",
        )
        return {"enrollee_id": enrollee_id, "camp_id": camp_id, "status": "enrolled"}

    # ---------- 评估与补交资料 ----------

    def submit_assessment(self, actor, enrollee_id: str, kind: str, content: dict,
                          request_key: str):
        """签署评估。某类评估一旦签署即锁定，重复提交一律拒绝（补交资料走另一条路）。"""
        actor = self._as_actor(actor)
        if kind not in ASSESSMENT_KINDS:
            raise ServiceError("评估类型无效", 400)
        self._require_roles(actor, (SIGN_AUTHORITY[kind],), "仅指定角色可签署该评估")
        if not isinstance(content, dict) or not content:
            raise ServiceError("评估内容缺失", 400)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._submit_assessment(actor, enrollee_id, kind, content),
            )

    def _submit_assessment(self, actor, enrollee_id, kind, content):
        enr = self._enrollee(enrollee_id)
        existing = self.connection.execute(
            "SELECT 1 FROM assessments WHERE enrollee_id=? AND kind=?",
            (enrollee_id, kind),
        ).fetchone()
        if existing is not None:
            # 已签署的评估不可被覆盖，补交资料也不能从这里进入
            raise ServiceError("评估已签署并锁定，不能覆盖；请使用补交资料接口", 409)
        now = self.clock()
        canonical = _canonical(content)
        self.connection.execute(
            "INSERT INTO assessments VALUES(?,?,?,?,?,?)",
            (enrollee_id, kind, actor["user_id"], now, canonical, _sha256(canonical)),
        )
        self._touch(enrollee_id)
        self._audit(
            actor["user_id"], "assessment_signed",
            {"enrollee_id": enrollee_id, "kind": kind,
             "content_hash": _sha256(canonical)},
            f"assessment_signed:{enrollee_id}:{kind}",
        )
        return {"enrollee_id": enrollee_id, "kind": kind, "signed": True, "locked": True}

    def submit_supplement(self, actor, enrollee_id: str, kind: str, content: dict,
                          request_key: str):
        """补交资料：只追加，不触碰、不覆盖任何已签署评估。"""
        actor = self._as_actor(actor)
        if kind not in ASSESSMENT_KINDS:
            raise ServiceError("资料类型无效", 400)
        if not isinstance(content, dict) or not content:
            raise ServiceError("资料内容缺失", 400)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._submit_supplement(actor, enrollee_id, kind, content),
            )

    def _submit_supplement(self, actor, enrollee_id, kind, content):
        self._enrollee(enrollee_id)
        now = self.clock()
        supplement_id = f"sup:{_sha256(enrollee_id + kind + _canonical(content) + now)[:20]}"
        self.connection.execute(
            "INSERT INTO supplements VALUES(?,?,?,?,?,?)",
            (supplement_id, enrollee_id, kind, _canonical(content), actor["user_id"], now),
        )
        self._audit(
            actor["user_id"], "supplement_submitted",
            {"enrollee_id": enrollee_id, "kind": kind, "supplement_id": supplement_id},
            f"supplement_submitted:{supplement_id}",
        )
        return {"supplement_id": supplement_id, "enrollee_id": enrollee_id,
                "kind": kind, "assessment_unchanged": True}

    # ---------- 排课门禁 ----------

    def _gates(self, enr: sqlite3.Row) -> dict:
        """计算当前是否可排入训练以及阻断原因。"""
        camp = self._camp(enr["camp_id"])
        meds = self.connection.execute(
            "SELECT kind FROM assessments WHERE enrollee_id=?", (enr["enrollee_id"],)
        ).fetchall()
        signed = {r["kind"] for r in meds}
        open_referral = self.connection.execute(
            "SELECT 1 FROM referrals WHERE enrollee_id=? AND status='open'",
            (enr["enrollee_id"],),
        ).fetchone()
        reasons = []
        if ASSESSMENT_MEDICAL not in signed:  # medical
            reasons.append("医学评估未完成")
        if ASSESSMENT_EXERCISE not in signed:
            reasons.append("运动风险评估未完成")
        if enr["frozen"]:
            reasons.append("红旗冻结未解除")
        if open_referral is not None:
            reasons.append("转诊未闭环")
        if enr["status"] == "paused":
            reasons.append("学员处于暂停状态")
        if camp["status"] != "active":
            reasons.append("训练营已取消")
        return {
            "medical_signed": ASSESSMENT_MEDICAL in signed,
            "exercise_signed": ASSESSMENT_EXERCISE in signed,
            "frozen": bool(enr["frozen"]),
            "open_referral": open_referral is not None,
            "paused": enr["status"] == "paused",
            "camp_active": camp["status"] == "active",
            "can_train": not reasons,
            "blocked_reasons": reasons,
        }

    def schedule_session(self, actor, enrollee_id: str, scheduled_at: str,
                         price_cents: int, request_key: str,
                         session_id: str | None = None):
        actor = self._as_actor(actor)
        self._require_roles(actor, (ROLE_COACH, ROLE_ADMIN), "仅教练/管理员可排课")
        if price_cents < 0:
            raise ServiceError("课程价格不能为负", 400)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._schedule_session(
                    actor, enrollee_id, scheduled_at, price_cents, session_id),
            )

    def _schedule_session(self, actor, enrollee_id, scheduled_at, price_cents, session_id):
        enr = self._enrollee(enrollee_id)
        gates = self._gates(enr)
        if not gates["can_train"]:
            raise ServiceError("不满足排课条件：" + "；".join(gates["blocked_reasons"]), 409)
        seq_row = self.connection.execute(
            "SELECT COALESCE(MAX(seq),0)+1 AS next_seq FROM sessions WHERE enrollee_id=?",
            (enrollee_id,),
        ).fetchone()
        seq = seq_row["next_seq"]
        session_id = session_id or f"sess:{enrollee_id}:{seq}"
        now = self.clock()
        try:
            self.connection.execute(
                "INSERT INTO sessions VALUES(?,?,?,?,?,?,?)",
                (session_id, enrollee_id, seq, scheduled_at, "scheduled",
                 price_cents, now),
            )
        except sqlite3.IntegrityError:
            raise ServiceError("课程已存在", 409)
        self._audit(
            actor["user_id"], "session_scheduled",
            {"enrollee_id": enrollee_id, "session_id": session_id, "seq": seq,
             "scheduled_at": scheduled_at},
            f"session_scheduled:{session_id}",
        )
        return {"session_id": session_id, "seq": seq, "status": "scheduled"}

    # ---------- 红旗：冻结 + 通知 ----------

    def report_red_flag(self, actor, enrollee_id: str, symptom: str,
                        request_key: str, checkin_id: str | None = None):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_REPORT_FLAG, "仅教练/医务人员可上报红旗")
        if symptom not in RED_FLAGS:
            raise ServiceError(f"未知红旗症状：{symptom}", 400)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._report_red_flag(actor, enrollee_id, symptom, checkin_id),
            )

    def _report_red_flag(self, actor, enrollee_id, symptom, checkin_id):
        enr = self._enrollee(enrollee_id)
        now = self.clock()
        flag_id = f"flag:{_sha256(enrollee_id + symptom + now + actor['user_id'])[:20]}"
        self.connection.execute(
            "INSERT INTO red_flags VALUES(?,?,?,?,?,?)",
            (flag_id, enrollee_id, symptom, actor["user_id"], checkin_id, now),
        )
        frozen_sessions = []
        if not enr["frozen"]:
            self.connection.execute(
                "UPDATE enrollees SET frozen=1, version=version+1, updated_at=? WHERE enrollee_id=?",
                (now, enrollee_id),
            )
            # 冻结全部尚未开始的已排课程；已完成/已取消的不动
            rows = self.connection.execute(
                "SELECT session_id FROM sessions WHERE enrollee_id=? AND status='scheduled'"
                " AND scheduled_at>?",
                (enrollee_id, now),
            ).fetchall()
            for r in rows:
                self.connection.execute(
                    "UPDATE sessions SET status='frozen' WHERE session_id=?",
                    (r["session_id"],),
                )
                frozen_sessions.append(r["session_id"])
        # red_flag 事件本身通知 medic/coach；首次冻结再补一条 freeze 事件
        self._audit(
            actor["user_id"], "red_flag",
            {"enrollee_id": enrollee_id, "symptom": symptom, "flag_id": flag_id,
             "checkin_id": checkin_id},
            f"red_flag:{flag_id}",
        )
        if frozen_sessions:
            self._audit(
                actor["user_id"], "freeze",
                {"enrollee_id": enrollee_id, "reason_flag": flag_id,
                 "frozen_sessions": frozen_sessions},
                f"freeze:{enrollee_id}:{flag_id}",
            )
        return {"flag_id": flag_id, "frozen": True,
                "frozen_sessions": frozen_sessions,
                "notified_roles": list(NOTIFY_PLAN["red_flag"])}

    def unfreeze(self, actor, enrollee_id: str, note: str, request_key: str):
        """仅授权医务人员可解除冻结。"""
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_FREEZE_CONTROL, "仅授权医务人员可解除冻结")
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._unfreeze(actor, enrollee_id, note))

    def _unfreeze(self, actor, enrollee_id, note):
        enr = self._enrollee(enrollee_id)
        if not enr["frozen"]:
            raise ServiceError("学员当前未被冻结", 409)
        now = self.clock()
        self.connection.execute(
            "UPDATE enrollees SET frozen=0, version=version+1, updated_at=? WHERE enrollee_id=?",
            (now, enrollee_id),
        )
        restored = self.connection.execute(
            "UPDATE sessions SET status='scheduled' WHERE enrollee_id=? AND status='frozen'"
            " RETURNING session_id",
            (enrollee_id,),
        ).fetchall()
        session_ids = [r["session_id"] for r in restored]
        self._audit(
            actor["user_id"], "unfreeze",
            {"enrollee_id": enrollee_id, "note": note, "restored_sessions": session_ids},
            f"unfreeze:{enrollee_id}:{_sha256(note+now)[:12]}",
        )
        return {"frozen": False, "restored_sessions": session_ids}

    # ---------- 暂停 / 恢复 ----------

    def pause(self, actor, enrollee_id: str, reason: str, request_key: str):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_PAUSE)
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._set_pause(actor, enrollee_id, reason, True))

    def resume(self, actor, enrollee_id: str, request_key: str):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_PAUSE)
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._set_pause(actor, enrollee_id, None, False))

    def _set_pause(self, actor, enrollee_id, reason, paused):
        enr = self._enrollee(enrollee_id)
        target_status = "paused" if paused else "enrolled"
        if enr["status"] == "referred":
            raise ServiceError("转诊处理中，暂停状态由转诊闭环决定", 409)
        if enr["status"] == target_status:
            raise ServiceError("学员已处于该状态", 409)
        self.connection.execute(
            "UPDATE enrollees SET status=?, version=version+1, updated_at=? WHERE enrollee_id=?",
            (target_status, self.clock(), enrollee_id),
        )
        self._audit(
            actor["user_id"], "paused" if paused else "resumed",
            {"enrollee_id": enrollee_id, "reason": reason},
            f"{'pause' if paused else 'resume'}:{enrollee_id}:{_sha256(self.clock())[:12]}",
        )
        return {"enrollee_id": enrollee_id, "status": target_status}

    # ---------- 转诊 ----------

    def open_referral(self, actor, enrollee_id: str, reason: str, request_key: str):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_REFER, "仅医务人员可发起转诊")
        if not reason:
            raise ServiceError("转诊原因缺失", 400)
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._open_referral(actor, enrollee_id, reason))

    def _open_referral(self, actor, enrollee_id, reason):
        self._enrollee(enrollee_id)
        open_row = self.connection.execute(
            "SELECT referral_id FROM referrals WHERE enrollee_id=? AND status='open'",
            (enrollee_id,),
        ).fetchone()
        if open_row is not None:
            raise ServiceError("已有进行中的转诊", 409)
        now = self.clock()
        referral_id = f"ref:{_sha256(enrollee_id + reason + now)[:16]}"
        self.connection.execute(
            "INSERT INTO referrals VALUES(?,?,?,?,?,?,?,?,?)",
            (referral_id, enrollee_id, reason, actor["user_id"], now,
             "open", None, None, None),
        )
        self.connection.execute(
            "UPDATE enrollees SET status='referred', version=version+1, updated_at=? WHERE enrollee_id=?",
            (now, enrollee_id),
        )
        self._audit(
            actor["user_id"], "referral_opened",
            {"enrollee_id": enrollee_id, "referral_id": referral_id, "reason": reason},
            f"referral_opened:{referral_id}",
        )
        return {"referral_id": referral_id, "status": "open"}

    def resolve_referral(self, actor, referral_id: str, resolution: str, request_key: str):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_REFER, "仅医务人员可闭环转诊")
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._resolve_referral(actor, referral_id, resolution))

    def _resolve_referral(self, actor, referral_id, resolution):
        row = self.connection.execute(
            "SELECT * FROM referrals WHERE referral_id=?", (referral_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("转诊不存在", 404)
        if row["status"] != "open":
            raise ServiceError("转诊已闭环", 409)
        now = self.clock()
        self.connection.execute(
            "UPDATE referrals SET status='resolved', resolution=?, resolved_by=?, resolved_at=?"
            " WHERE referral_id=?",
            (resolution, actor["user_id"], now, referral_id),
        )
        # 回到在训状态；若仍有红旗冻结，是否解冻由医务人员另行决定
        self.connection.execute(
            "UPDATE enrollees SET status='enrolled', version=version+1, updated_at=? WHERE enrollee_id=?",
            (now, row["enrollee_id"]),
        )
        self._audit(
            actor["user_id"], "referral_resolved",
            {"enrollee_id": row["enrollee_id"], "referral_id": referral_id,
             "resolution": resolution},
            f"referral_resolved:{referral_id}",
        )
        return {"referral_id": referral_id, "status": "resolved"}

    # ---------- 打卡与结算 ----------

    def checkin(self, actor, session_id: str, request_key: str,
                checkin_id: str | None = None):
        """打卡即结算：唯一约束 + 幂等键保证重复打卡不会产生第二笔结算。"""
        actor = self._as_actor(actor)
        self._require_roles(actor, (ROLE_COACH, ROLE_MEDIC, ROLE_ADMIN), "无打卡权限")
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._checkin(actor, session_id, checkin_id))

    def _checkin(self, actor, session_id, checkin_id):
        sess = self.connection.execute(
            "SELECT * FROM sessions WHERE session_id=?", (session_id,)
        ).fetchone()
        if sess is None:
            raise ServiceError("课程不存在", 404)
        if sess["status"] == "completed":
            raise ServiceError("该课程已打卡，禁止重复结算", 409)
        if sess["status"] != "scheduled":
            raise ServiceError(f"课程当前状态为 {sess['status']}，不能打卡", 409)
        enr = self._enrollee(sess["enrollee_id"])
        gates = self._gates(enr)
        if not gates["can_train"]:
            raise ServiceError("训练已被阻断：" + "；".join(gates["blocked_reasons"]), 409)
        now = self.clock()
        checkin_id = checkin_id or f"chk:{session_id}"
        try:
            self.connection.execute(
                "INSERT INTO checkins VALUES(?,?,?,?,?)",
                (checkin_id, session_id, enr["enrollee_id"], actor["user_id"], now),
            )
        except sqlite3.IntegrityError:
            raise ServiceError("该课程已打卡，禁止重复结算", 409)
        self.connection.execute(
            "UPDATE sessions SET status='completed' WHERE session_id=?", (session_id,)
        )
        # 确定性分录 ID + UNIQUE(ref_type,ref_id)，数据库层再挡一次重复结算
        entry_id = f"check:{checkin_id}"
        self.connection.execute(
            "INSERT INTO ledger VALUES(?,?,?,?,?,?,?)",
            (entry_id, enr["enrollee_id"], "session_charge",
             sess["price_cents"], "checkin", checkin_id, now),
        )
        self._touch(enr["enrollee_id"])
        self._audit(
            actor["user_id"], "checked_in",
            {"enrollee_id": enr["enrollee_id"], "session_id": session_id,
             "checkin_id": checkin_id, "charge_cents": sess["price_cents"]},
            f"checked_in:{checkin_id}",
        )
        return {"checkin_id": checkin_id, "session_id": session_id,
                "status": "completed", "charged_cents": sess["price_cents"],
                "settled": True}

    # ---------- 退款 ----------

    def _balance(self, enrollee_id: str) -> dict:
        enr = self._enrollee(enrollee_id)
        charges = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger "
            "WHERE enrollee_id=? AND kind='session_charge'", (enrollee_id,)
        ).fetchone()["s"]
        refunds = self.connection.execute(
            "SELECT COALESCE(SUM(amount_cents),0) AS s FROM ledger "
            "WHERE enrollee_id=? AND kind='refund'", (enrollee_id,)
        ).fetchone()["s"]
        balance = enr["fee_paid_cents"] - charges + refunds  # refunds 为负
        return {"fee_paid_cents": enr["fee_paid_cents"], "charged_cents": charges,
                "refunded_cents": -refunds, "balance_cents": balance}

    def request_refund(self, actor, enrollee_id: str, amount_cents: int,
                       reason: str, request_key: str):
        actor = self._as_actor(actor)
        is_owner_self = actor["role"] == ROLE_OWNER and actor["user_id"] == enrollee_id
        if not (is_owner_self or actor["role"] == ROLE_ADMIN):
            raise ServiceError("仅学员本人或管理员可发起退款", 403)
        if amount_cents <= 0:
            raise ServiceError("退款金额必须大于 0", 400)
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._request_refund(
                    actor, enrollee_id, amount_cents, reason, request_key),
            )

    def _request_refund(self, actor, enrollee_id, amount_cents, reason, request_key):
        balance = self._balance(enrollee_id)
        existing = self.connection.execute(
            "SELECT * FROM refunds WHERE enrollee_id=?", (enrollee_id,)
        ).fetchone()
        if existing is not None:
            # 重复请求返回既有单据，不产生新单据也不重复结算
            return {"enrollee_id": enrollee_id, "status": existing["status"],
                    "amount_cents": existing["amount_cents"], "duplicate": True}
        if amount_cents > balance["balance_cents"]:
            raise ServiceError(
                f"退款金额 {amount_cents} 超过可退余额 {balance['balance_cents']}", 409)
        now = self.clock()
        self.connection.execute(
            "INSERT INTO refunds(enrollee_id,request_key,amount_cents,reason,status,"
            "requested_by,requested_at) VALUES(?,?,?,?,?,?,?)",
            (enrollee_id, request_key, amount_cents, reason, "requested",
             actor["user_id"], now),
        )
        self._audit(
            actor["user_id"], "refund_requested",
            {"enrollee_id": enrollee_id, "amount_cents": amount_cents, "reason": reason},
            f"refund_requested:{enrollee_id}",
        )
        return {"enrollee_id": enrollee_id, "amount_cents": amount_cents,
                "status": "requested"}

    def decide_refund(self, actor, enrollee_id: str, approve: bool,
                      note: str, request_key: str):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_REFUND_DECIDE, "仅财务可作出退款决定")
        with self._tx():
            return self._idempotent(
                request_key,
                lambda: self._decide_refund(actor, enrollee_id, approve, note),
            )

    def _decide_refund(self, actor, enrollee_id, approve, note):
        row = self.connection.execute(
            "SELECT * FROM refunds WHERE enrollee_id=?", (enrollee_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("没有待处理的退款请求", 404)
        if row["status"] != "requested":
            raise ServiceError("退款已有决定，不能重复结算", 409)
        now = self.clock()
        status = "approved" if approve else "rejected"
        self.connection.execute(
            "UPDATE refunds SET status=?, decided_by=?, decided_at=?, decision_note=?"
            " WHERE enrollee_id=?",
            (status, actor["user_id"], now, note, enrollee_id),
        )
        if approve:
            balance = self._balance(enrollee_id)
            if row["amount_cents"] > balance["balance_cents"]:
                raise ServiceError("可退余额不足，无法批准", 409)
            # 确定性分录 + 唯一约束：退款最多结算一次
            self.connection.execute(
                "INSERT INTO ledger VALUES(?,?,?,?,?,?,?)",
                (f"refund:{enrollee_id}", enrollee_id, "refund",
                 -row["amount_cents"], "refund", enrollee_id, now),
            )
        self._audit(
            actor["user_id"], "refund_decided",
            {"enrollee_id": enrollee_id, "decision": status,
             "amount_cents": row["amount_cents"], "note": note},
            f"refund_decided:{enrollee_id}",
        )
        return {"enrollee_id": enrollee_id, "status": status,
                "amount_cents": row["amount_cents"], "settled": approve}

    # ---------- 营期取消 ----------

    def cancel_camp(self, actor, camp_id: str, request_key: str, reason: str = ""):
        actor = self._as_actor(actor)
        self._require_roles(actor, self._CAN_CANCEL_CAMP, "仅管理员可取消训练营")
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._cancel_camp(actor, camp_id, reason))

    def _cancel_camp(self, actor, camp_id, reason):
        camp = self._camp(camp_id)
        if camp["status"] == "cancelled":
            return {"camp_id": camp_id, "status": "cancelled", "duplicate": True}
        now = self.clock()
        self.connection.execute(
            "UPDATE camps SET status='cancelled', cancelled_at=? WHERE camp_id=?",
            (now, camp_id),
        )
        cancelled = self.connection.execute(
            "UPDATE sessions SET status='cancelled' WHERE status IN ('scheduled','frozen')"
            " AND enrollee_id IN (SELECT enrollee_id FROM enrollees WHERE camp_id=?)"
            " RETURNING session_id",
            (camp_id,),
        ).fetchall()
        session_ids = [r["session_id"] for r in cancelled]
        self._audit(
            actor["user_id"], "camp_cancelled",
            {"camp_id": camp_id, "reason": reason, "cancelled_sessions": session_ids},
            f"camp_cancelled:{camp_id}",
        )
        return {"camp_id": camp_id, "status": "cancelled",
                "cancelled_sessions": session_ids}

    # ---------- 通知 ----------

    def list_notifications(self, actor, role: str | None = None):
        actor = self._as_actor(actor)
        role = role or actor["role"]
        if role != actor["role"] and actor["role"] != ROLE_ADMIN:
            raise ServiceError("只能查看本角色的通知", 403)
        sql = "SELECT n.* FROM notifications n WHERE n.role=?"
        params = [role]
        # 学员队列按本人隔离，避免看到其他学员的决定通知
        if role == ROLE_OWNER:
            sql += " AND n.enrollee_id=?"
            params.append(actor["user_id"])
        sql += " ORDER BY n.rowid"
        rows = self.connection.execute(sql, params).fetchall()
        return [dict(r) for r in rows]

    def mark_delivered(self, actor, notification_id: str, request_key: str):
        actor = self._as_actor(actor)
        with self._tx():
            return self._idempotent(
                request_key, lambda: self._mark_delivered(actor, notification_id))

    def _mark_delivered(self, actor, notification_id):
        row = self.connection.execute(
            "SELECT * FROM notifications WHERE notification_id=?", (notification_id,)
        ).fetchone()
        if row is None:
            raise ServiceError("通知不存在", 404)
        if row["role"] != actor["role"] and actor["role"] != ROLE_ADMIN:
            raise ServiceError("只能签收本角色通知", 403)
        if row["status"] != "delivered":
            self.connection.execute(
                "UPDATE notifications SET status='delivered', delivered_at=? WHERE notification_id=?",
                (self.clock(), notification_id),
            )
        return {"notification_id": notification_id, "status": "delivered"}

    # ---------- 角色视图与可执行动作 ----------

    def enrollee_view(self, actor, enrollee_id: str) -> dict:
        """按角色投影学员信息，并给出当前可执行动作清单，供现场逐条核对。"""
        actor = self._as_actor(actor)
        enr = self._enrollee(enrollee_id)
        role = actor["role"]
        is_owner_self = role == ROLE_OWNER and actor["user_id"] == enrollee_id
        if role == ROLE_OWNER and not is_owner_self:
            # 学员只能查看本人信息
            raise ServiceError("学员不存在", 404)
        gates = self._gates(enr)

        view = {
            "enrollee_id": enrollee_id,
            "camp_id": enr["camp_id"],
            "name": enr["name"],
            "status": enr["status"],
            "version": enr["version"],
            "updated_at": enr["updated_at"],
            "gates": gates,
        }

        # 评估：非授权角色只见签署状态与哈希，不见内容；学员本人可见本人全部
        for kind in ASSESSMENT_KINDS:
            a = self.connection.execute(
                "SELECT * FROM assessments WHERE enrollee_id=? AND kind=?",
                (enrollee_id, kind),
            ).fetchone()
            if a is None:
                view[f"{kind}_assessment"] = {"signed": False}
            else:
                item = {"signed": True, "signed_by": a["signed_by"],
                        "signed_at": a["signed_at"], "content_hash": a["content_hash"],
                        "locked": True}
                if is_owner_self or role == SIGN_AUTHORITY[kind] or role == ROLE_ADMIN:
                    item["content"] = json.loads(a["content"])
                view[f"{kind}_assessment"] = item

        sups = self.connection.execute(
            "SELECT supplement_id,kind,submitted_by,created_at FROM supplements"
            " WHERE enrollee_id=? ORDER BY rowid", (enrollee_id,)
        ).fetchall()
        view["supplements"] = [dict(r) for r in sups]

        flags = self.connection.execute(
            "SELECT flag_id,symptom,reported_by,created_at FROM red_flags"
            " WHERE enrollee_id=? ORDER BY rowid", (enrollee_id,)
        ).fetchall()
        view["red_flags"] = [dict(r) for r in flags]

        referral = self.connection.execute(
            "SELECT * FROM referrals WHERE enrollee_id=? ORDER BY rowid DESC LIMIT 1",
            (enrollee_id,),
        ).fetchone()
        view["referral"] = dict(referral) if referral else None

        sessions = self.connection.execute(
            "SELECT session_id,seq,scheduled_at,status,price_cents FROM sessions"
            " WHERE enrollee_id=? ORDER BY seq", (enrollee_id,)
        ).fetchall()
        view["sessions"] = [dict(r) for r in sessions]

        refund = self.connection.execute(
            "SELECT amount_cents,reason,status,requested_by,requested_at,"
            "decided_by,decided_at,decision_note FROM refunds WHERE enrollee_id=?",
            (enrollee_id,),
        ).fetchone()
        if refund is not None:
            view["refund"] = dict(refund)

        # 财务/管理员/学员本人可见账目，其他角色不可见
        if is_owner_self or role in (ROLE_FINANCE, ROLE_ADMIN):
            view["ledger"] = self._balance(enrollee_id)
            ledger_rows = self.connection.execute(
                "SELECT entry_id,kind,amount_cents,ref_type,ref_id,created_at"
                " FROM ledger WHERE enrollee_id=? ORDER BY rowid", (enrollee_id,)
            ).fetchall()
            view["ledger"]["entries"] = [dict(r) for r in ledger_rows]
        else:
            view["ledger"] = None

        # 是否存在可打卡课程：有已排课程且门禁全部通过
        can_checkin_now = gates["can_train"] and any(
            s["status"] == "scheduled" for s in view["sessions"]
        )
        view["actions"] = self._available_actions(
            role, is_owner_self, enr, gates, refund, can_checkin_now)
        return view

    def _available_actions(self, role, is_owner_self, enr, gates, refund,
                           can_checkin_now) -> list[dict]:
        actions = []

        def add(action, allowed, reason=None):
            actions.append({"action": action, "allowed": allowed,
                            "reason": None if allowed else reason})

        if role == ROLE_MEDIC:
            add("sign_medical_assessment", not gates["medical_signed"],
                "医学评估已签署并锁定" if gates["medical_signed"] else None)
            add("report_red_flag", True)
            add("open_referral", enr["status"] != "referred",
                "已有进行中的转诊" if enr["status"] == "referred" else None)
            add("resolve_referral", gates["open_referral"],
                "无进行中的转诊" if not gates["open_referral"] else None)
            add("unfreeze", gates["frozen"], "当前未冻结" if not gates["frozen"] else None)
            add("checkin", can_checkin_now,
                None if can_checkin_now else "无可执行打卡的课程")
        if role == ROLE_COACH:
            add("sign_exercise_assessment", not gates["exercise_signed"],
                "运动风险评估已签署并锁定" if gates["exercise_signed"] else None)
            add("schedule_session", gates["can_train"],
                "；".join(gates["blocked_reasons"]) if not gates["can_train"] else None)
            add("checkin", can_checkin_now,
                None if can_checkin_now else "无可执行打卡的课程")
            add("report_red_flag", True)
            if enr["status"] != "referred":
                add("pause" if enr["status"] != "paused" else "resume", True)
        if role == ROLE_FINANCE:
            pending_refund = refund is not None and refund["status"] == "requested"
            add("decide_refund", pending_refund,
                "无待处理退款" if not pending_refund else None)
        if is_owner_self:
            add("submit_supplement", True)
            pending_refund = refund is not None and refund["status"] == "requested"
            add("request_refund", not pending_refund,
                "已有进行中的退款请求" if pending_refund else None)
            if enr["status"] != "referred":
                add("pause" if enr["status"] != "paused" else "resume", True)
        if role == ROLE_ADMIN:
            add("cancel_camp", gates["camp_active"],
                "训练营已取消" if not gates["camp_active"] else None)
            add("schedule_session", gates["can_train"],
                "；".join(gates["blocked_reasons"]) if not gates["can_train"] else None)
        return actions

    def list_enrollees(self, actor, camp_id: str | None = None) -> list[dict]:
        actor = self._as_actor(actor)
        if actor["role"] == ROLE_OWNER:
            raise ServiceError("学员名单仅员工可见", 403)
        sql = "SELECT enrollee_id,camp_id,name,status,frozen,version FROM enrollees"
        params = ()
        if camp_id:
            sql += " WHERE camp_id=?"
            params = (camp_id,)
        sql += " ORDER BY enrollee_id"
        return [dict(r) for r in self.connection.execute(sql, params).fetchall()]

    def audit_history(self, actor, enrollee_id: str | None = None) -> list[dict]:
        actor = self._as_actor(actor)
        if enrollee_id:
            rows = self.connection.execute(
                "SELECT seq,event_id,at,actor,action,details,prev_hash,entry_hash"
                " FROM audit_log WHERE json_extract(details,'$.enrollee_id')=?"
                " OR (action='camp_cancelled' AND json_extract(details,'$.camp_id')="
                "     (SELECT camp_id FROM enrollees WHERE enrollee_id=?))"
                " ORDER BY seq",
                (enrollee_id, enrollee_id),
            ).fetchall()
        else:
            rows = self.connection.execute(
                "SELECT seq,event_id,at,actor,action,details,prev_hash,entry_hash"
                " FROM audit_log ORDER BY seq"
            ).fetchall()
        return [dict(r, details=json.loads(r["details"])) for r in rows]


# 兼容脚手架旧名称
class DomainStore(CampService):
    pass
