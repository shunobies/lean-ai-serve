"""Role-Based Access Control — permission definitions and checks."""

from __future__ import annotations

from lean_ai_serve.models.schemas import AuthUser

# ---------------------------------------------------------------------------
# Permission definitions per role
# ---------------------------------------------------------------------------

ROLE_PERMISSIONS: dict[str, set[str]] = {
    "admin": {"*"},  # All permissions
    "model-manager": {
        "inference:call",
        "model:read",
        "model:write",
        "model:deploy",
        "adapter:read",
        "adapter:deploy",
        "metrics:read",
        "audit:read_own",
    },
    "trainer": {
        "inference:call",
        "model:read",
        "training:submit",
        "training:read",
        "dataset:upload",
        "dataset:read",
        "adapter:read",
        "metrics:read",
        "audit:read_own",
    },
    "user": {
        "inference:call",
        "usage:read_own",
        "audit:read_own",
    },
    "auditor": {
        "audit:read",
        "metrics:read",
        "usage:read",
        "model:read",
    },
    "service-account": {
        "inference:call",
    },
}


def get_permissions(roles: list[str]) -> set[str]:
    """Resolve all permissions for a list of roles."""
    permissions: set[str] = set()
    for role in roles:
        permissions |= ROLE_PERMISSIONS.get(role, set())
    return permissions


def check_permission(user: AuthUser, permission: str) -> bool:
    """Check if a user has a specific permission."""
    user_permissions = get_permissions(user.roles)
    return "*" in user_permissions or permission in user_permissions


def has_any_permission(user: AuthUser, *permissions: str) -> bool:
    """Check if a user has any of the listed permissions."""
    user_permissions = get_permissions(user.roles)
    if "*" in user_permissions:
        return True
    return bool(user_permissions & set(permissions))
