"""Optional Benefit Plan ownership and authorization for tasks."""

from uuid import UUID

from django.apps import apps
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db.models import Q


_UNSET = object()

# These are registry-owned rather than programme-owned.  Keep this list
# deliberately narrow: an unrecognised task remains restricted when enabled.
_SHARED_ENTITY_TYPES = {
    ("individual", "individualdatauploadrecords"),
}
_SHARED_TASK_SOURCES = {
    "IndividualTaskCreatorService",
    "IndividualService",
    "GroupService",
    "GroupIndividualService",
    "CreateGroupAndMoveIndividualService",
}


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


def _model_identity(instance):
    if instance is None:
        return None, None
    meta = getattr(instance, "_meta", None)
    return (
        getattr(meta, "app_label", None),
        getattr(meta, "model_name", None),
    )


def _plan_from_payment_plan(payment_plan):
    if payment_plan is None:
        return None, False
    content_type = getattr(payment_plan, "benefit_plan_type", None)
    model = getattr(content_type, "model", None)
    app_label = getattr(content_type, "app_label", None)
    if app_label == "social_protection" and model == "benefitplan":
        return _normalise_plan_id(payment_plan.benefit_plan_id), False
    if content_type is not None:
        return None, True
    return None, False


def _plan_from_benefit_consumption(benefit):
    links = getattr(benefit, "payrollbenefitconsumption_set", None)
    if links is None:
        return None, False
    plans = set()
    all_shared = True
    for link in links.filter(is_deleted=False).select_related(
        "payroll__payment_plan__benefit_plan_type"
    ):
        plan_id, shared = _plan_from_payment_plan(link.payroll.payment_plan)
        if plan_id:
            plans.add(plan_id)
            all_shared = False
        elif not shared:
            all_shared = False
    if len(plans) == 1:
        return plans.pop(), False
    if not plans and all_shared:
        return None, True
    return None, False


def _ownership_from_entity(entity):
    app_label, model_name = _model_identity(entity)
    identity = (app_label, model_name)
    if identity in _SHARED_ENTITY_TYPES:
        return None, True
    if identity == ("social_protection", "benefitplan"):
        return _normalise_plan_id(entity.pk), False
    if identity in {
        ("social_protection", "beneficiary"),
        ("social_protection", "groupbeneficiary"),
        ("social_protection", "benefitplandatauploadrecords"),
    }:
        return _normalise_plan_id(entity.benefit_plan_id), False
    if identity == ("payroll", "payroll"):
        return _plan_from_payment_plan(entity.payment_plan)
    if identity == ("payroll", "benefitconsumption"):
        return _plan_from_benefit_consumption(entity)
    return None, False


def _payload_values(payload, keys):
    if not isinstance(payload, dict):
        return []
    values = []
    for key in keys:
        value = payload.get(key)
        if value not in (None, ""):
            values.append(value)
    for nested_key in ("incoming_data", "current_data"):
        nested = payload.get(nested_key)
        if isinstance(nested, dict):
            values.extend(_payload_values(nested, keys))
    return values


def _ownership_from_payload(data):
    payloads = [data.get("data"), data.get("json_ext")]
    values = []
    for payload in payloads:
        values.extend(_payload_values(payload, ("benefit_plan_id", "benefit_plan")))
    plan_ids = {_normalise_plan_id(value) for value in values}
    plan_ids.discard(None)
    payload = data.get("data")
    ids = payload.get("ids") if isinstance(payload, dict) else None
    source = data.get("source")
    if ids and source == "CreateDeduplicationReviewTasksService":
        try:
            beneficiary_model = apps.get_model("social_protection", "Beneficiary")
        except LookupError:
            beneficiary_model = None
        if beneficiary_model is not None:
            rows = list(
                beneficiary_model.objects.filter(
                    id__in=ids,
                    is_deleted=False,
                ).values_list("id", "benefit_plan_id")
            )
            if len(rows) == len(set(ids)):
                plan_ids.update(str(plan_id) for _, plan_id in rows)
    elif ids and source == "CreateDeduplicationPaymentReviewTasksService":
        try:
            benefit_model = apps.get_model("payroll", "BenefitConsumption")
        except LookupError:
            benefit_model = None
        if benefit_model is not None:
            benefits = list(benefit_model.objects.filter(
                id__in=ids,
                is_deleted=False,
            ))
            resolved_ids = set()
            unresolved = len(benefits) != len(set(ids))
            for benefit in benefits:
                plan_id, _ = _plan_from_benefit_consumption(benefit)
                if plan_id:
                    resolved_ids.add(plan_id)
                else:
                    unresolved = True
            if not unresolved:
                plan_ids.update(resolved_ids)
    if len(plan_ids) > 1:
        raise ValidationError("Task data refers to multiple Benefit Plans.")
    return next(iter(plan_ids), None)


def resolve_task_ownership(data, current=None):
    """Return ``(plan_id, is_shared)`` without guessing unknown ownership."""
    explicit = data.get("benefit_plan_id", _UNSET)
    if explicit is not _UNSET:
        explicit = _normalise_plan_id(explicit)

    entity = data.get("entity", _UNSET)
    if entity is _UNSET:
        entity_type = data.get("entity_type")
        entity_id = data.get("entity_id")
        if entity_type is not None and entity_id not in (None, ""):
            try:
                entity = entity_type.get_object_for_this_type(pk=entity_id)
            except Exception:
                entity = None
        elif current is not None:
            entity = current.entity
        else:
            entity = None

    inferred, shared = _ownership_from_entity(entity)
    payload_plan = _ownership_from_payload(data)
    candidates = {value for value in (inferred, payload_plan) if value}
    if explicit is not _UNSET and explicit:
        candidates.add(explicit)
    if len(candidates) > 1:
        raise ValidationError("Task ownership does not match its Benefit Plan data.")

    plan_id = next(iter(candidates), None)
    if explicit is not _UNSET and explicit is None and plan_id:
        raise ValidationError("Task Benefit Plan ownership cannot be cleared.")

    if plan_id:
        return plan_id, False
    if shared or data.get("source") in _SHARED_TASK_SOURCES:
        return None, True
    if current is not None and not any(
        key in data
        for key in ("entity", "entity_type", "entity_id", "data", "json_ext", "source")
    ):
        return current.benefit_plan_id, current.benefit_plan_is_shared
    return None, False


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
