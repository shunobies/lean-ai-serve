"""Tests for RBAC permission system."""

from lean_ai_serve.models.schemas import AuthUser
from lean_ai_serve.security.rbac import (
    check_permission,
    get_permissions,
    has_any_permission,
)


def test_admin_has_wildcard():
    """Admin role should have wildcard permission."""
    perms = get_permissions(["admin"])
    assert "*" in perms


def test_user_has_inference():
    """User role should have inference:call."""
    perms = get_permissions(["user"])
    assert "inference:call" in perms
    assert "model:write" not in perms


def test_combined_roles():
    """Multiple roles should merge permissions."""
    perms = get_permissions(["user", "trainer"])
    assert "inference:call" in perms
    assert "training:submit" in perms
    assert "dataset:upload" in perms


def test_check_permission_admin():
    """Admin should pass any permission check."""
    user = AuthUser(user_id="admin", display_name="Admin", roles=["admin"])
    assert check_permission(user, "anything:goes") is True


def test_check_permission_user():
    """User should only pass allowed permissions."""
    user = AuthUser(user_id="u1", display_name="User", roles=["user"])
    assert check_permission(user, "inference:call") is True
    assert check_permission(user, "model:write") is False


def test_has_any_permission():
    """has_any_permission should return True if any listed permission matches."""
    user = AuthUser(user_id="u1", display_name="User", roles=["trainer"])
    assert has_any_permission(user, "training:submit", "model:write") is True
    assert has_any_permission(user, "model:write", "audit:read") is False


def test_unknown_role():
    """Unknown role should have no permissions."""
    perms = get_permissions(["nonexistent"])
    assert len(perms) == 0


def test_model_access_wildcard():
    """User with * model access should access any model."""
    user = AuthUser(
        user_id="u1", display_name="User", roles=["user"], allowed_models=["*"]
    )
    assert user.can_access_model("any-model") is True


def test_model_access_specific():
    """User with specific model access should be restricted."""
    user = AuthUser(
        user_id="u1",
        display_name="User",
        roles=["user"],
        allowed_models=["model-a", "model-b"],
    )
    assert user.can_access_model("model-a") is True
    assert user.can_access_model("model-c") is False
