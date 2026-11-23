import importlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import graphene
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured, PermissionDenied, ValidationError
from django.db import connection
from django.db.migrations.loader import MigrationLoader
from django.test import RequestFactory, SimpleTestCase, TestCase, override_settings
from graphql_relay import to_global_id

from core.test_helpers import (
    create_test_interactive_user,
    create_test_language,
    create_test_role,
)
from social_protection.apps import SocialProtectionConfig
from social_protection.models import BenefitPlan, UserBenefitPlanAccess

from tasks_management.benefit_plan_access import (
    filter_tasks_by_benefit_plan,
    resolve_task_ownership,
)
from tasks_management.gql_mutations import ResolveTaskMutation, UpdateTaskMutation
from tasks_management.gql_queries import TaskGQLType, TaskHistoryGQLType
from tasks_management.models import Task, TaskExecutor, TaskGroup
from tasks_management.schema import Query as TasksQuery
from tasks_management.services import TaskService


class Query(TasksQuery, graphene.ObjectType):
    node = graphene.relay.Node.Field()


@override_settings(ROW_SECURITY=False, MODE="dev")
class TaskBenefitPlanAccessTest(TestCase):
    @classmethod
    def setUpTestData(cls):
        create_test_language()
        role = create_test_role(
            perm_names=[
                "gql_task_search_perms",
                "gql_task_update_perms",
            ],
            name="Track 08 task executor",
        )
        cls.admin = create_test_interactive_user(username="track08-admin")
        cls.upg = create_test_interactive_user(
            username="track08-upg", roles=[role.id]
        )
        cls.rmep = create_test_interactive_user(
            username="track08-rmep", roles=[role.id]
        )
        cls.both = create_test_interactive_user(
            username="track08-both", roles=[role.id]
        )
        cls.none = create_test_interactive_user(
            username="track08-none", roles=[role.id]
        )
        cls.all_plan = create_test_interactive_user(
            username="track08-all", roles=[role.id]
        )

        cls.plan_upg = BenefitPlan(code="T08-UPG", name="Track 08 UPG")
        cls.plan_upg.save(username=cls.admin.username)
        cls.plan_rmep = BenefitPlan(code="T08-RMEP", name="Track 08 RMEP")
        cls.plan_rmep.save(username=cls.admin.username)

        for user, plan in (
            (cls.upg, cls.plan_upg),
            (cls.rmep, cls.plan_rmep),
            (cls.both, cls.plan_upg),
            (cls.both, cls.plan_rmep),
        ):
            UserBenefitPlanAccess(user=user, benefit_plan=plan).save(
                username=cls.admin.username
            )

        cls.group = TaskGroup(code="T08", completion_policy="ALL")
        cls.group.save(username=cls.admin.username)
        for user in (cls.upg, cls.rmep, cls.both, cls.none, cls.all_plan):
            TaskExecutor(user=user, task_group=cls.group).save(
                username=cls.admin.username
            )

        cls.task_upg = cls.make_task("task-upg", benefit_plan_id=cls.plan_upg.id)
        cls.task_rmep = cls.make_task("task-rmep", benefit_plan_id=cls.plan_rmep.id)
        cls.task_shared = cls.make_task("task-shared", shared=True)
        cls.task_unknown = cls.make_task("task-unknown")

    @classmethod
    def make_task(cls, source, benefit_plan_id=None, shared=False, group=None):
        task = Task(
            source=source,
            status=Task.Status.ACCEPTED,
            task_group=group or cls.group,
            benefit_plan_id=(
                str(benefit_plan_id) if benefit_plan_id is not None else None
            ),
            benefit_plan_is_shared=shared,
            business_status={},
            data={},
        )
        task.save(username=cls.admin.username)
        return task

    def setUp(self):
        original_enabled = SocialProtectionConfig.enable_benefit_plan_access_control
        original_all = SocialProtectionConfig.gql_benefit_plan_all_access_perms
        SocialProtectionConfig.enable_benefit_plan_access_control = True
        SocialProtectionConfig.gql_benefit_plan_all_access_perms = ["160006"]
        self.addCleanup(
            setattr,
            SocialProtectionConfig,
            "enable_benefit_plan_access_control",
            original_enabled,
        )
        self.addCleanup(
            setattr,
            SocialProtectionConfig,
            "gql_benefit_plan_all_access_perms",
            original_all,
        )

    def permissions(self, user, *, all_plan=False, update=True):
        all_plan_perms = SocialProtectionConfig.gql_benefit_plan_all_access_perms

        def has_perms(perms):
            if perms == all_plan_perms:
                return all_plan
            return update and perms == ["191003"]

        return patch.object(type(user), "has_perms", side_effect=has_perms)

    def info(self, user):
        request = RequestFactory().get("/graphql")
        request.user = user
        return SimpleNamespace(context=request)

    def visible_sources(self, user, *, all_plan=False):
        with self.permissions(user, all_plan=all_plan):
            queryset = TaskGQLType.get_queryset(Task.objects.all(), self.info(user))
            return set(queryset.values_list("source", flat=True))

    def test_assignment_matrix_and_unknown_ownership_fail_closed(self):
        self.assertEqual(self.visible_sources(self.upg), {"task-upg", "task-shared"})
        self.assertEqual(self.visible_sources(self.rmep), {"task-rmep", "task-shared"})
        self.assertEqual(
            self.visible_sources(self.both),
            {"task-upg", "task-rmep", "task-shared"},
        )
        self.assertEqual(self.visible_sources(self.none), {"task-shared"})
        self.assertEqual(
            self.visible_sources(self.all_plan, all_plan=True),
            {"task-upg", "task-rmep", "task-shared", "task-unknown"},
        )
        self.assertEqual(
            self.visible_sources(self.admin),
            {"task-upg", "task-rmep", "task-shared", "task-unknown"},
        )

    def test_graphql_history_and_relay_nodes_are_plan_scoped(self):
        request = RequestFactory().get("/graphql")
        request.user = self.upg
        visible_node = to_global_id("TaskGQLType", self.task_upg.pk)
        hidden_node = to_global_id("TaskGQLType", self.task_rmep.pk)
        with self.permissions(self.upg):
            result = graphene.Schema(query=Query).execute(
                """query($visible: ID!, $hidden: ID!) {
                    task { edges { node { source } } }
                    taskHistory { edges { node { source } } }
                    visible: node(id: $visible) { id }
                    hidden: node(id: $hidden) { id }
                }""",
                context_value=request,
                variable_values={"visible": visible_node, "hidden": hidden_node},
            )
        self.assertIsNone(result.errors, result.errors)
        self.assertEqual(
            {edge["node"]["source"] for edge in result.data["task"]["edges"]},
            {"task-upg", "task-shared"},
        )
        self.assertEqual(
            {
                edge["node"]["source"]
                for edge in result.data["taskHistory"]["edges"]
            },
            {"task-upg", "task-shared"},
        )
        self.assertIsNotNone(result.data["visible"])
        self.assertIsNone(result.data["hidden"])

    def test_task_group_scope_remains_required(self):
        outsider = create_test_interactive_user(
            username=f"t08-out-{str(uuid4())[:8]}",
            roles=[self.upg.i_user.role_id],
        )
        UserBenefitPlanAccess(user=outsider, benefit_plan=self.plan_upg).save(
            username=self.admin.username
        )
        with self.permissions(outsider):
            queryset = TaskGQLType.get_queryset(
                Task.objects.all(), self.info(outsider)
            )
            self.assertFalse(queryset.exists())
            with self.assertRaises(Exception):
                UpdateTaskMutation._validate_mutation(
                    outsider, id=self.task_upg.id, status=Task.Status.COMPLETED
                )

    def test_current_approver_assignment_is_checked_by_services_and_mutations(self):
        with self.permissions(self.rmep):
            denied = TaskService(self.rmep).resolve_task({
                "id": self.task_upg.id,
                "business_status": {"decision": "APPROVED"},
            })
            self.assertFalse(denied["success"])
            with self.assertRaises(Exception):
                ResolveTaskMutation._validate_mutation(
                    self.rmep,
                    id=self.task_upg.id,
                    business_status={"decision": "APPROVED"},
                )

        UserBenefitPlanAccess(user=self.rmep, benefit_plan=self.plan_upg).save(
            username=self.admin.username
        )
        with self.permissions(self.rmep):
            allowed = TaskService(self.rmep).resolve_task({
                "id": self.task_upg.id,
                "business_status": {"decision": "APPROVED"},
            })
        self.assertTrue(allowed["success"], allowed)

    def test_unauthorized_completion_and_resolution_leave_task_unchanged(self):
        original_status = self.task_upg.status
        original_business_status = dict(self.task_upg.business_status)

        with self.permissions(self.rmep):
            completion = TaskService(self.rmep).complete_task({
                "id": self.task_upg.id,
            })
            resolution = TaskService(self.rmep).resolve_task({
                "id": self.task_upg.id,
                "business_status": {"decision": "APPROVED"},
            })

        self.assertFalse(completion["success"])
        self.assertFalse(resolution["success"])
        self.task_upg.refresh_from_db()
        self.assertEqual(self.task_upg.status, original_status)
        self.assertEqual(
            self.task_upg.business_status,
            original_business_status,
        )

        with self.permissions(self.upg):
            completion = TaskService(self.upg).complete_task({
                "id": self.task_upg.id,
            })

        self.assertTrue(completion["success"], completion)
        self.task_upg.refresh_from_db()
        self.assertEqual(self.task_upg.status, Task.Status.COMPLETED)

    def test_trusted_internal_path_bypasses_group_only_not_plan_access(self):
        outsider = create_test_interactive_user(
            username=f"t08-int-{str(uuid4())[:8]}",
            roles=[self.upg.i_user.role_id],
        )
        UserBenefitPlanAccess(user=outsider, benefit_plan=self.plan_upg).save(
            username=self.admin.username
        )
        normal = TaskService(outsider).complete_task({"id": self.task_upg.id})
        self.assertFalse(normal["success"])
        internal = TaskService(
            outsider, trusted_internal=True
        ).complete_task({"id": self.task_upg.id})
        self.assertTrue(internal["success"], internal)

        unauthorized = TaskService(
            self.none, trusted_internal=True
        ).complete_task({"id": self.task_rmep.id})
        self.assertFalse(unauthorized["success"])

    def test_create_accepts_and_enforces_explicit_ownership(self):
        with self.permissions(self.upg):
            created = TaskService(self.upg).create({
                "source": "BenefitPlanService",
                "entity": self.plan_upg,
                "benefit_plan_id": str(self.plan_upg.id),
                "status": Task.Status.RECEIVED,
                "data": {},
            })
        self.assertTrue(created["success"], created)
        task = Task.objects.get(id=created["data"]["id"])
        self.assertEqual(task.benefit_plan_id, str(self.plan_upg.id))
        self.assertFalse(task.benefit_plan_is_shared)

        with self.permissions(self.rmep):
            denied = TaskService(self.rmep).create({
                "source": "BenefitPlanService",
                "entity": self.plan_upg,
                "benefit_plan_id": str(self.plan_upg.id),
                "status": Task.Status.RECEIVED,
                "data": {},
            })
        self.assertFalse(denied["success"])

    def test_update_cannot_change_or_remove_established_ownership(self):
        with self.permissions(self.upg):
            changed = TaskService(self.upg).update({
                "id": self.task_upg.id,
                "benefit_plan_id": str(self.plan_rmep.id),
            })
            removed = TaskService(self.upg).update({
                "id": self.task_upg.id,
                "benefit_plan_id": None,
            })

        self.assertFalse(changed["success"])
        self.assertFalse(removed["success"])
        self.task_upg.refresh_from_db()
        self.assertEqual(
            self.task_upg.benefit_plan_id,
            str(self.plan_upg.id),
        )

    def test_feature_off_and_absent_social_protection_preserve_legacy_scope(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        queryset = Task.objects.all()
        self.assertIs(filter_tasks_by_benefit_plan(queryset, self.none), queryset)

        SocialProtectionConfig.enable_benefit_plan_access_control = True
        with patch(
            "tasks_management.benefit_plan_access.apps.is_installed",
            return_value=False,
        ):
            self.assertIs(filter_tasks_by_benefit_plan(queryset, self.none), queryset)

    def test_feature_off_create_and_update_skip_ownership_handling(self):
        SocialProtectionConfig.enable_benefit_plan_access_control = False
        with patch(
            "tasks_management.services.resolve_task_ownership",
            side_effect=AssertionError("ownership resolution must remain disabled"),
        ):
            created = TaskService(self.upg).create({
                "source": "legacy-task",
                "status": Task.Status.RECEIVED,
                "benefit_plan_id": "legacy-plan-id",
                "benefit_plan_is_shared": True,
                "data": {},
            })
            self.assertTrue(created["success"], created)
            task = Task.objects.get(id=created["data"]["id"])
            self.assertEqual(task.benefit_plan_id, "legacy-plan-id")
            self.assertTrue(task.benefit_plan_is_shared)

            updated = TaskService(self.upg).update({
                "id": task.id,
                "benefit_plan_id": "changed-by-legacy-update",
            })
            self.assertTrue(updated["success"], updated)

        task.refresh_from_db()
        self.assertEqual(task.benefit_plan_id, "changed-by-legacy-update")

    def test_get_queryset_uses_legacy_executor_scope_when_feature_off(self):
        TaskExecutor.objects.filter(
            user=self.none,
            task_group=self.group,
        ).update(is_deleted=True)
        SocialProtectionConfig.enable_benefit_plan_access_control = False

        task_queryset = TaskGQLType.get_queryset(
            Task.objects.all(), self.info(self.none)
        )
        history_queryset = TaskHistoryGQLType.get_queryset(
            Task.history.all(), self.info(self.none)
        )

        self.assertFalse(task_queryset.query.distinct)
        self.assertFalse(history_queryset.query.distinct)
        self.assertEqual(
            set(task_queryset.values_list("source", flat=True)),
            {"task-upg", "task-rmep", "task-shared", "task-unknown"},
        )

        SocialProtectionConfig.enable_benefit_plan_access_control = True
        with self.permissions(self.none):
            secured_queryset = TaskGQLType.get_queryset(
                Task.objects.all(), self.info(self.none)
            )
        self.assertTrue(secured_queryset.query.distinct)
        self.assertFalse(secured_queryset.exists())

    def test_enabled_mode_fails_closed_without_shared_service(self):
        with patch(
            "tasks_management.benefit_plan_access._shared_access_helpers",
            side_effect=ImproperlyConfigured("shared service unavailable"),
        ):
            with self.assertRaises(ImproperlyConfigured):
                filter_tasks_by_benefit_plan(Task.objects.all(), self.upg)

    def test_sources_and_external_models_do_not_infer_ownership(self):
        self.assertEqual(
            resolve_task_ownership({"source": "IndividualService", "data": {}}),
            (None, False),
        )
        self.assertEqual(
            resolve_task_ownership({"entity": self.plan_upg, "data": {}}),
            (None, False),
        )
        self.assertEqual(
            resolve_task_ownership({
                "data": {"benefit_plan_id": str(self.plan_upg.id)},
            }),
            (None, False),
        )
        self.assertEqual(
            resolve_task_ownership({
                "benefit_plan_id": str(self.plan_upg.id),
            }),
            (str(self.plan_upg.id), False),
        )

    def test_untrusted_callers_cannot_create_shared_tasks(self):
        with self.assertRaises(PermissionDenied):
            resolve_task_ownership({"benefit_plan_is_shared": True})
        self.assertEqual(
            resolve_task_ownership(
                {"benefit_plan_is_shared": True},
                allow_shared=True,
            ),
            (None, True),
        )

        denied = TaskService(self.upg).create({
            "source": "untrusted-shared",
            "benefit_plan_is_shared": True,
            "status": Task.Status.RECEIVED,
            "data": {},
        })
        self.assertFalse(denied["success"])

        trusted = TaskService(self.upg, trusted_internal=True).create({
            "source": "trusted-shared",
            "benefit_plan_is_shared": True,
            "status": Task.Status.RECEIVED,
            "data": {},
        })
        self.assertTrue(trusted["success"], trusted)
        task = Task.objects.get(id=trusted["data"]["id"])
        self.assertTrue(task.benefit_plan_is_shared)
        self.assertIsNone(task.benefit_plan_id)

    def test_existing_ownership_is_preserved_without_external_inference(self):
        current = SimpleNamespace(
            benefit_plan_id=str(self.plan_upg.id),
            benefit_plan_is_shared=False,
        )

        self.assertEqual(
            resolve_task_ownership({"data": {"status": "updated"}}, current),
            (str(self.plan_upg.id), False),
        )
        with self.assertRaises(ValidationError):
            resolve_task_ownership({"benefit_plan_id": None}, current)
        with self.assertRaises(ValidationError):
            resolve_task_ownership(
                {"benefit_plan_id": str(self.plan_rmep.id)},
                current,
            )

        shared = SimpleNamespace(
            benefit_plan_id=None,
            benefit_plan_is_shared=True,
        )
        self.assertEqual(
            resolve_task_ownership({"source": "renamed"}, shared),
            (None, True),
        )
        with self.assertRaises(ValidationError):
            resolve_task_ownership(
                {"benefit_plan_is_shared": False},
                shared,
            )

    def test_migration_benefit_ownership_matches_active_link_rules(self):
        migration = importlib.import_module(
            "tasks_management.migrations.0011_task_benefit_plan_ownership"
        )

        class LinkRows(list):
            def values_list(self, field, flat=False):
                return [getattr(link, field) for link in self]

        class LinkManager:
            def __init__(self, links):
                self.links = links

            def filter(self, **kwargs):
                return LinkRows([
                    link
                    for link in self.links
                    if all(getattr(link, key) == value for key, value in kwargs.items())
                ])

        def ownership(links):
            link_model = SimpleNamespace(objects=LinkManager(links))
            with patch.object(migration, "_model", return_value=link_model), patch.object(
                migration,
                "_payroll_ownership",
                return_value=(str(self.plan_upg.id), False),
            ):
                return migration._benefit_ownership(apps, "benefit-id")

        active = SimpleNamespace(
            benefit_id="benefit-id",
            payroll_id="payroll-id",
            is_deleted=False,
        )
        soft_deleted = SimpleNamespace(
            benefit_id="benefit-id",
            payroll_id="payroll-id",
            is_deleted=True,
        )

        self.assertEqual(
            ownership([active]),
            (str(self.plan_upg.id), False),
        )
        self.assertEqual(ownership([]), (None, False))
        self.assertEqual(ownership([soft_deleted]), (None, False))

    def test_historical_backfill_uses_proven_entity_ownership(self):
        scoped = self.make_task("backfill-plan", group=self.group)
        scoped.entity = self.plan_upg
        scoped.save(username=self.admin.username)
        shared = self.make_task("IndividualService", group=self.group)
        unresolved = self.make_task("unresolved-history", group=self.group)
        unresolved.data = {"preserved": True}
        unresolved.save(username=self.admin.username)
        unresolved_history_count = unresolved.history.count()

        migration = importlib.import_module(
            "tasks_management.migrations.0011_task_benefit_plan_ownership"
        )
        with self.assertLogs(migration.logger.name, level="WARNING") as logs:
            migration.backfill_task_ownership(
                apps,
                SimpleNamespace(connection=connection),
            )

        scoped.refresh_from_db()
        shared.refresh_from_db()
        unresolved.refresh_from_db()
        self.assertEqual(scoped.benefit_plan_id, str(self.plan_upg.id))
        self.assertFalse(scoped.benefit_plan_is_shared)
        self.assertTrue(shared.benefit_plan_is_shared)
        self.assertIsNone(unresolved.benefit_plan_id)
        self.assertFalse(unresolved.benefit_plan_is_shared)
        self.assertEqual(unresolved.data, {"preserved": True})
        self.assertEqual(unresolved.history.count(), unresolved_history_count)
        self.assertTrue(
            any("remain restricted" in message for message in logs.output)
        )
        self.assertTrue(
            scoped.history.filter(
                benefit_plan_id=str(self.plan_upg.id),
                benefit_plan_is_shared=False,
            ).exists()
        )

    def test_backfill_preserves_established_ownership_without_active_links(self):
        established = self.make_task(
            "benefit-with-deleted-payroll-link",
            benefit_plan_id=self.plan_upg.id,
        )
        migration = importlib.import_module(
            "tasks_management.migrations.0011_task_benefit_plan_ownership"
        )

        with patch.object(
            migration,
            "_entity_ownership",
            return_value=(None, False),
        ):
            migration.backfill_task_ownership(
                apps,
                SimpleNamespace(connection=connection),
            )

        established.refresh_from_db()
        self.assertEqual(established.benefit_plan_id, str(self.plan_upg.id))
        self.assertFalse(established.benefit_plan_is_shared)


class TaskOwnershipMigrationGraphTest(SimpleTestCase):
    def setUp(self):
        self.migration = importlib.import_module(
            "tasks_management.migrations.0011_task_benefit_plan_ownership"
        )

    def test_installed_optional_module_dependencies_are_in_graph_without_cycles(self):
        expected = {
            (
                "social_protection",
                "0008_benefitplandatauploadrecords_historicalbenefitplandatauploadrecords",
            ),
            ("contribution_plan", "0012_benefit_plan_generic_fk"),
            ("payroll", "0011_auto_20240202_1140"),
        }
        self.assertTrue(expected.issubset(set(self.migration.Migration.dependencies)))

        loader = MigrationLoader(None, ignore_no_migrations=True)
        loader.graph.ensure_not_cyclic()
        task_node = loader.graph.node_map[
            ("tasks_management", "0011_task_benefit_plan_ownership")
        ]
        parent_apps = {parent.key[0] for parent in task_node.parents}
        self.assertTrue(
            {"social_protection", "contribution_plan", "payroll"}.issubset(
                parent_apps
            )
        )

    def test_optional_dependencies_are_omitted_when_modules_are_absent(self):
        registry = SimpleNamespace(is_installed=lambda app_label: False)
        self.assertEqual(
            self.migration._configured_dependencies(registry),
            [],
        )

    def test_installed_app_with_missing_model_is_not_silently_ignored(self):
        class BrokenRegistry:
            def get_app_config(self, app_label):
                return object()

            def get_model(self, app_label, model_name):
                raise LookupError(model_name)

        with patch.object(self.migration, "_AVAILABLE_TABLES", None):
            with self.assertRaisesMessage(
                LookupError,
                "Migration 0011 requires payroll.Payroll",
            ):
                self.migration._model(BrokenRegistry(), "payroll", "Payroll")

    def test_absent_optional_app_returns_no_model(self):
        class OptionalRegistry:
            def get_app_config(self, app_label):
                raise LookupError(app_label)

        with patch.object(self.migration, "_AVAILABLE_TABLES", None):
            self.assertIsNone(
                self.migration._model(
                    OptionalRegistry(),
                    "payroll",
                    "Payroll",
                )
            )


class TaskOwnershipContractTest(SimpleTestCase):
    def test_only_top_level_explicit_ownership_is_accepted(self):
        plan_id = str(uuid4())
        external_entity = SimpleNamespace(
            _meta=SimpleNamespace(
                app_label="external_module",
                model_name="programme_record",
            ),
            benefit_plan_id=plan_id,
        )

        self.assertEqual(
            resolve_task_ownership({
                "source": "ExternalTaskProducer",
                "entity": external_entity,
                "data": {"benefit_plan_id": plan_id},
            }),
            (None, False),
        )
        self.assertEqual(
            resolve_task_ownership({"benefit_plan_id": plan_id}),
            (plan_id, False),
        )

    def test_shared_creation_requires_a_trusted_internal_caller(self):
        with self.assertRaises(PermissionDenied):
            resolve_task_ownership({"benefit_plan_is_shared": True})
        self.assertEqual(
            resolve_task_ownership(
                {"benefit_plan_is_shared": True},
                allow_shared=True,
            ),
            (None, True),
        )

    def test_recorded_plan_and_legacy_shared_ownership_are_immutable(self):
        plan_id = str(uuid4())
        plan_owned = SimpleNamespace(
            benefit_plan_id=plan_id,
            benefit_plan_is_shared=False,
        )
        shared = SimpleNamespace(
            benefit_plan_id=None,
            benefit_plan_is_shared=True,
        )

        self.assertEqual(
            resolve_task_ownership({"source": "renamed"}, plan_owned),
            (plan_id, False),
        )
        self.assertEqual(
            resolve_task_ownership({"source": "renamed"}, shared),
            (None, True),
        )
        with self.assertRaises(ValidationError):
            resolve_task_ownership({"benefit_plan_id": None}, plan_owned)
        with self.assertRaises(ValidationError):
            resolve_task_ownership(
                {"benefit_plan_is_shared": False},
                shared,
            )
