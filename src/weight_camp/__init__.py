"""减重训练风险台领域服务。"""
from .domain import (
    ROLE_ADMIN,
    ROLE_COACH,
    ROLE_FINANCE,
    ROLE_MEDIC,
    ROLE_OWNER,
    RED_FLAGS,
)
from .service import CampService, DomainStore, ServiceError

__all__ = [
    "CampService",
    "DomainStore",
    "ServiceError",
    "ROLE_OWNER",
    "ROLE_COACH",
    "ROLE_MEDIC",
    "ROLE_FINANCE",
    "ROLE_ADMIN",
    "RED_FLAGS",
]
