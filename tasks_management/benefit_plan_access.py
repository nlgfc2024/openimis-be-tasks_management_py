"""Optional Benefit Plan ownership and authorization for tasks."""

from uuid import UUID

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db.models import Q


_UNSET = object()


def _user(user_or_info):
    return getattr(getattr(user_or_info, "context", None), "user", user_or_info)


def benefit_plan_access_control_enabled():
    if not apps.is_installed("social_protection"):
        return False
    try:
        from social_protection.apps import SocialProtectionConfig
    except ImportError:
        return False
    return bool(
        getattr(SocialProtectionConfig, "enable_benefit_plan_access_control", False)
    )


def _shared_access_helpers():
    try:
        from social_protection.benefit_plan_access import (
            get_accessible_benefit_plan_ids,
            require_benefit_plan_access,
        )
    except ImportError as exc:
        raise ImproperlyConfigured(
            "Benefit Plan task access is enabled, but the shared Social "
            "Protection authorization service is unavailable."
        ) from exc
    return get_accessible_benefit_plan_ids, require_benefit_plan_access


def _normalise_plan_id(value):
    if value in (None, ""):
        return None
    try:
        return str(UUID(str(value)))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValidationError("Task Benefit Plan ID must be a valid UUID.") from exc


def resolve_task_ownership(data, current=None, *, allow_shared=False):
    """Validate explicit, immutable ownership without inspecting external models."""
    explicit = data.get("benefit_plan_id", _UNSET)
    if explicit is not _UNSET:
        explicit = _normalise_plan_id(explicit)
    shared = data.get("benefit_plan_is_shared", _UNSET)

    if current is not None:
        current_plan_id = _normalise_plan_id(current.benefit_plan_id)
        current_shared = bool(current.benefit_plan_is_shared)
        if explicit is not _UNSET and explicit != current_plan_id:
            raise ValidationError("Task Benefit Plan ownership cannot be changed.")
        if shared is not _UNSET and bool(shared) != current_shared:
            raise ValidationError("Task shared ownership cannot be changed.")
        return current_plan_id, current_shared

    if explicit is not _UNSET and explicit and shared is True:
        raise ValidationError("A Task cannot be both plan-owned and shared.")
    if shared is True:
        if not allow_shared:
            raise PermissionDenied(
                "Only trusted internal callers may create shared Tasks."
            )
        return None, True
    return explicit if explicit is not _UNSET else None, False


def require_task_plan_access(user, task):
    if not benefit_plan_access_control_enabled():
        return task
    user = _user(user)
    if task.benefit_plan_id:
        _, require_benefit_plan_access = _shared_access_helpers()
        require_benefit_plan_access(user, task.benefit_plan_id)
        return task
    if task.benefit_plan_is_shared:
        return task
    get_accessible_benefit_plan_ids, _ = _shared_access_helpers()
    if get_accessible_benefit_plan_ids(user) is None:
        return task
    raise PermissionDenied("Task Benefit Plan ownership is unresolved.")


def filter_tasks_by_benefit_plan(queryset, user):
    if not benefit_plan_access_control_enabled():
        return queryset
    get_accessible_benefit_plan_ids, _ = _shared_access_helpers()
    accessible_ids = get_accessible_benefit_plan_ids(_user(user))
    if accessible_ids is None:
        return queryset
    accessible_ids = [str(value) for value in accessible_ids]
    return queryset.filter(
        Q(benefit_plan_id__in=accessible_ids)
        | Q(benefit_plan_id__isnull=True, benefit_plan_is_shared=True)
    )


def user_has_task_group_scope(user, task):
    """Preserve triage/admin access and require current executor assignment."""
    from tasks_management.apps import TasksManagementConfig

    user = _user(user)
    if not user or getattr(user, "is_anonymous", False):
        return False
    if getattr(user, "is_imis_admin", False) or getattr(user, "is_superuser", False):
        return True
    triage_perms = (
        TasksManagementConfig.gql_task_group_create_perms
        + TasksManagementConfig.gql_task_group_search_perms
        + TasksManagementConfig.gql_task_group_update_perms
        + TasksManagementConfig.gql_task_group_delete_perms
    )
    if user.has_perms(triage_perms):
        return True
    if task.task_group_id is None or task.status == task.Status.RECEIVED:
        return False
    return task.task_group.taskexecutor_set.filter(
        user=user,
        is_deleted=False,
    ).exists()


def require_task_operation_access(user, task, *, trusted_internal=False):
    require_task_plan_access(user, task)
    if not benefit_plan_access_control_enabled():
        return task
    if trusted_internal:
        return task
    if not user_has_task_group_scope(user, task):
        raise PermissionDenied("Task group access is required.")
    return task
