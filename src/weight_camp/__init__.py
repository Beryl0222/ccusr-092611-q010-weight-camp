"""减重训练风险台领域服务。"""
from .service import CampService, DomainStore, ServiceError, Actor
from .domain import (
    ROLE_COACH, ROLE_MEDIC, ROLE_FINANCE, ROLE_TRAINEE, ROLE_ADMIN,
    ASSESSMENT_MEDICAL, ASSESSMENT_FITNESS, RED_FLAGS,
)
__all__ = ["CampService", "DomainStore", "ServiceError", "Actor",
           "ROLE_COACH", "ROLE_MEDIC", "ROLE_FINANCE", "ROLE_TRAINEE",
           "ROLE_ADMIN", "ASSESSMENT_MEDICAL", "ASSESSMENT_FITNESS",
           "RED_FLAGS"]
