"""减重训练风险台中的领域对象与约定。"""
from dataclasses import dataclass
from datetime import datetime, timezone

# 角色：学员本人、教练、授权医务人员、财务、营期管理员
ROLE_OWNER = "owner"
ROLE_COACH = "coach"
ROLE_MEDIC = "medic"
ROLE_FINANCE = "finance"
ROLE_ADMIN = "admin"
STAFF_ROLES = (ROLE_COACH, ROLE_MEDIC, ROLE_FINANCE, ROLE_ADMIN)
ALL_ROLES = (ROLE_OWNER,) + STAFF_ROLES

# 评估类型：医学评估（含基础病等）、运动风险评估
ASSESSMENT_MEDICAL = "medical"
ASSESSMENT_EXERCISE = "exercise"
ASSESSMENT_KINDS = (ASSESSMENT_MEDICAL, ASSESSMENT_EXERCISE)
# 谁有权签署哪类评估
SIGN_AUTHORITY = {
    ASSESSMENT_MEDICAL: ROLE_MEDIC,
    ASSESSMENT_EXERCISE: ROLE_COACH,
}

# 红旗症状：出现即冻结后续课程
RED_FLAGS = (
    "chest_tightness",      # 胸闷 / 胸痛
    "dyspnea",              # 异常呼吸困难
    "syncope",              # 晕厥
    "palpitations",         # 明显心悸
    "cyanosis",             # 紫绀
)

# 事件 → 需要通知的角色（“通知指定角色”的订阅表）
NOTIFY_PLAN = {
    "red_flag": (ROLE_MEDIC, ROLE_COACH),
    "freeze": (ROLE_MEDIC, ROLE_COACH),
    "unfreeze": (ROLE_COACH,),
    "referral_opened": (ROLE_MEDIC, ROLE_COACH),
    "referral_resolved": (ROLE_COACH,),
    "refund_requested": (ROLE_FINANCE,),
    "refund_decided": (ROLE_OWNER,),
    "camp_cancelled": (ROLE_COACH, ROLE_MEDIC, ROLE_FINANCE),
}


@dataclass(frozen=True)
class Record:
    """通用记录（兼容脚手架的最小对象）。"""
    record_id: str
    owner_id: str
    state: str
    version: int
    updated_at: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
