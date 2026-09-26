"""减重训练风险台中的基础对象与领域约定。"""
from dataclasses import dataclass
from datetime import datetime, timezone

# 角色：教练、医务人员、财务人员、学员本人、运营管理员
ROLE_COACH = "coach"
ROLE_MEDIC = "medic"
ROLE_FINANCE = "finance"
ROLE_TRAINEE = "trainee"
ROLE_ADMIN = "admin"
ALL_ROLES = (ROLE_COACH, ROLE_MEDIC, ROLE_FINANCE, ROLE_TRAINEE, ROLE_ADMIN)

# 评估类型：医学评估（由医务人员签署）与运动风险评估（由教练签署）
ASSESSMENT_MEDICAL = "medical"
ASSESSMENT_FITNESS = "fitness"
ASSESSMENT_TYPES = (ASSESSMENT_MEDICAL, ASSESSMENT_FITNESS)
ASSESSMENT_SIGNER_ROLE = {
    ASSESSMENT_MEDICAL: ROLE_MEDIC,
    ASSESSMENT_FITNESS: ROLE_COACH,
}

# 报名生命周期
ST_DRAFT = "draft"            # 已报名，评估未完成
ST_READY = "ready"            # 两项评估均已签署，可排入训练
ST_SCHEDULED = "scheduled"    # 已排入训练日程
ST_FROZEN = "frozen"          # 出现红旗：后续课程冻结
ST_REFERRED = "referred"      # 已转诊
ST_CANCELLED = "cancelled"    # 训练营取消（终态）
ST_CLOSED = "closed"          # 训练结束（终态）
ST_REFUNDED = "refunded"      # 退款已结算（终态）
TERMINAL_STATES = frozenset({ST_CANCELLED, ST_CLOSED, ST_REFUNDED})

# 红旗症状（出现任意一项即冻结）
RED_FLAGS = frozenset({
    "chest_pain",      # 胸闷/胸痛
    "syncope",         # 晕厥
    "dyspnea_rest",    # 静息呼吸困难
    "palpitation",     # 明显心悸
    "cyanosis",        # 发绀
})

# 通知主题与接收角色
NOTIFY_FREEZE = "freeze"             # 红旗冻结
NOTIFY_UNFREEZE = "unfreeze"         # 解除冻结
NOTIFY_REFERRAL = "referral"         # 转诊
NOTIFY_REFUND = "refund"             # 退款决定
NOTIFY_CAMP_CANCELLED = "camp_cancelled"  # 训练营取消
NOTIFY_ROLES = {
    NOTIFY_FREEZE: (ROLE_MEDIC, ROLE_COACH, ROLE_ADMIN),
    NOTIFY_UNFREEZE: (ROLE_COACH, ROLE_ADMIN),
    NOTIFY_REFERRAL: (ROLE_MEDIC, ROLE_ADMIN),
    NOTIFY_REFUND: (ROLE_FINANCE, ROLE_ADMIN),
    NOTIFY_CAMP_CANCELLED: (ROLE_COACH, ROLE_MEDIC, ROLE_FINANCE, ROLE_ADMIN),
}

# 退款决定
REFUND_REQUESTED = "requested"
REFUND_SETTLED = "settled"
REFUND_REJECTED = "rejected"

# 转诊决定
REFERRAL_OPEN = "open"
REFERRAL_RESOLVED = "resolved"

# 角色可见的敏感字段（学员基础病等仅医务人员可见）
ROLE_VISIBLE_FIELDS = {
    ROLE_MEDIC: frozenset({"medical_history", "contraindications", "red_flag_detail",
                           "vitals", "referral", "medications"}),
    ROLE_COACH: frozenset({"red_flag_detail", "fitness_level", "vitals"}),
    ROLE_FINANCE: frozenset({"payment", "refund"}),
    ROLE_ADMIN: frozenset({"medical_history", "contraindications", "red_flag_detail",
                           "vitals", "referral", "medications",
                           "fitness_level", "payment", "refund"}),
    ROLE_TRAINEE: frozenset(),
}


@dataclass(frozen=True)
class Record:
    """兼容旧服务的通用记录视图。"""
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
