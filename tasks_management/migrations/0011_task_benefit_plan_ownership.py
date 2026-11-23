import logging

from django.apps import apps as django_apps
from django.db import migrations, models


SHARED_ENTITY_TYPES = {
    ("individual", "individualdatauploadrecords"),
}
SHARED_SOURCES = {
    "IndividualTaskCreatorService",
    "IndividualService",
    "GroupService",
    "GroupIndividualService",
    "CreateGroupAndMoveIndividualService",
}
_AVAILABLE_TABLES = None
logger = logging.getLogger(__name__)

OPTIONAL_DEPENDENCIES = (
    (
        "social_protection",
        "0008_benefitplandatauploadrecords_historicalbenefitplandatauploadrecords",
    ),
    ("contribution_plan", "0012_benefit_plan_generic_fk"),
    ("payroll", "0011_auto_20240202_1140"),
)


def _configured_dependencies(app_registry):
    return [
        dependency
        for dependency in OPTIONAL_DEPENDENCIES
        if app_registry.is_installed(dependency[0])
    ]


def _model(apps, app_label, model_name):
    try:
        apps.get_app_config(app_label)
    except LookupError:
        return None
    try:
        model = apps.get_model(app_label, model_name)
    except LookupError as exc:
        raise LookupError(
            f"Migration 0011 requires {app_label}.{model_name} when "
            f"the {app_label} app is installed."
        ) from exc
    if _AVAILABLE_TABLES is not None and model._meta.db_table not in _AVAILABLE_TABLES:
        raise RuntimeError(
            f"Migration 0011 requires table {model._meta.db_table} for "
            f"{app_label}.{model_name}. Check the migration dependencies."
        )
    return model


def _payload_plan_id(payload):
    if not isinstance(payload, dict):
        return None
    values = []
    for key in ("benefit_plan_id", "benefit_plan"):
        if payload.get(key):
            values.append(str(payload[key]))
    for key in ("incoming_data", "current_data"):
        value = _payload_plan_id(payload.get(key))
        if value:
            values.append(value)
    values = set(values)
    return values.pop() if len(values) == 1 else None


def _payment_plan_ownership(apps, payment_plan_id):
    PaymentPlan = _model(apps, "contribution_plan", "PaymentPlan")
    ContentType = _model(apps, "contenttypes", "ContentType")
    if PaymentPlan is None or ContentType is None or not payment_plan_id:
        return None, False
    payment_plan = PaymentPlan.objects.filter(pk=payment_plan_id).first()
    if payment_plan is None or payment_plan.benefit_plan_type_id is None:
        return None, False
    content_type = ContentType.objects.filter(
        pk=payment_plan.benefit_plan_type_id
    ).first()
    if content_type is None:
        return None, False
    if (
        content_type.app_label == "social_protection"
        and content_type.model == "benefitplan"
    ):
        return str(payment_plan.benefit_plan_id), False
    return None, True


def _payroll_ownership(apps, payroll_id):
    Payroll = _model(apps, "payroll", "Payroll")
    if Payroll is None:
        return None, False
    payroll = Payroll.objects.filter(pk=payroll_id).first()
    if payroll is None:
        return None, False
    return _payment_plan_ownership(apps, payroll.payment_plan_id)


def _benefit_ownership(apps, benefit_id):
    Link = _model(apps, "payroll", "PayrollBenefitConsumption")
    if Link is None:
        return None, False
    ownership = {
        _payroll_ownership(apps, payroll_id)
        for payroll_id in Link.objects.filter(
            benefit_id=benefit_id,
            is_deleted=False,
        ).values_list("payroll_id", flat=True)
    }
    plan_ids = {plan_id for plan_id, _ in ownership if plan_id}
    if len(plan_ids) == 1:
        return plan_ids.pop(), False
    if ownership and all(shared for _, shared in ownership):
        return None, True
    return None, False


def _deduplication_payload_ownership(apps, task):
    if not isinstance(task.data, dict) or not task.data.get("ids"):
        return None
    ids = task.data["ids"]
    if task.source == "CreateDeduplicationReviewTasksService":
        Beneficiary = _model(apps, "social_protection", "Beneficiary")
        if Beneficiary is None:
            return None
        rows = list(
            Beneficiary.objects.filter(
                id__in=ids,
                is_deleted=False,
            ).values_list("id", "benefit_plan_id")
        )
        if len(rows) != len(set(ids)):
            return None
        plan_ids = {str(plan_id) for _, plan_id in rows}
    elif task.source == "CreateDeduplicationPaymentReviewTasksService":
        Benefit = _model(apps, "payroll", "BenefitConsumption")
        if Benefit is None:
            return None
        benefit_ids = list(
            Benefit.objects.filter(
                id__in=ids,
                is_deleted=False,
            ).values_list("id", flat=True)
        )
        if len(benefit_ids) != len(set(ids)):
            return None
        ownership = [_benefit_ownership(apps, benefit_id) for benefit_id in benefit_ids]
        if any(not plan_id for plan_id, _ in ownership):
            return None
        plan_ids = {plan_id for plan_id, _ in ownership}
    else:
        return None
    return plan_ids.pop() if len(plan_ids) == 1 else None


def _entity_ownership(apps, task):
    content_type = task.entity_type
    if content_type is None or not task.entity_id:
        return None, False
    identity = (content_type.app_label, content_type.model)
    if identity in SHARED_ENTITY_TYPES:
        return None, True
    if identity == ("social_protection", "benefitplan"):
        return str(task.entity_id), False
    if identity in {
        ("social_protection", "beneficiary"),
        ("social_protection", "groupbeneficiary"),
        ("social_protection", "benefitplandatauploadrecords"),
    }:
        model = _model(apps, content_type.app_label, content_type.model)
        entity = model.objects.filter(pk=task.entity_id).first() if model else None
        return (
            (str(entity.benefit_plan_id), False)
            if entity is not None
            else (None, False)
        )
    if identity == ("payroll", "payroll"):
        return _payroll_ownership(apps, task.entity_id)
    if identity == ("payroll", "benefitconsumption"):
        return _benefit_ownership(apps, task.entity_id)
    return None, False


def backfill_task_ownership(apps, schema_editor):
    global _AVAILABLE_TABLES
    _AVAILABLE_TABLES = set(
        schema_editor.connection.introspection.table_names()
    )
    Task = apps.get_model("tasks_management", "Task")
    HistoricalTask = apps.get_model("tasks_management", "HistoricalTask")
    for model in (Task, HistoricalTask):
        unresolved = 0
        for task in model.objects.select_related("entity_type").iterator():
            plan_id, is_shared = _entity_ownership(apps, task)
            payload_ids = {
                value
                for value in (
                    task.benefit_plan_id,
                    _payload_plan_id(task.data),
                    _payload_plan_id(task.json_ext),
                )
                if value
            }
            deduplication_plan_id = _deduplication_payload_ownership(apps, task)
            if deduplication_plan_id:
                payload_ids.add(deduplication_plan_id)
            if plan_id:
                payload_ids.add(plan_id)
            if len(payload_ids) == 1:
                plan_id = payload_ids.pop()
                is_shared = False
            elif len(payload_ids) > 1:
                plan_id = None
                is_shared = False
            elif task.benefit_plan_is_shared:
                is_shared = True
            elif task.source in SHARED_SOURCES:
                is_shared = True
            if not plan_id and not is_shared:
                unresolved += 1
            model.objects.filter(pk=task.pk).update(
                benefit_plan_id=plan_id,
                benefit_plan_is_shared=is_shared,
            )
        if unresolved:
            logger.warning(
                "Migration 0011 left %s %s rows with unresolved Benefit Plan "
                "ownership; they remain restricted.",
                unresolved,
                model._meta.label,
            )


class Migration(migrations.Migration):

    dependencies = [
        ("tasks_management", "0010_add_search_all_perms_admin"),
        *_configured_dependencies(django_apps),
    ]

    operations = [
        migrations.AddField(
            model_name="task",
            name="benefit_plan_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="task",
            name="benefit_plan_is_shared",
            field=models.BooleanField(default=False),
        ),
        migrations.AddField(
            model_name="historicaltask",
            name="benefit_plan_id",
            field=models.CharField(
                blank=True,
                db_index=True,
                max_length=255,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="historicaltask",
            name="benefit_plan_is_shared",
            field=models.BooleanField(default=False),
        ),
        migrations.RunPython(backfill_task_ownership, migrations.RunPython.noop),
    ]
