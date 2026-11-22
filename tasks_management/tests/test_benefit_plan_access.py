import importlib
from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

import graphene
from django.apps import apps
from django.core.exceptions import ImproperlyConfigured
from django.db import connection
from django.test import RequestFactory, TestCase, override_settings
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
from tasks_management.gql_queries import TaskGQLType
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

    def test_create_infers_and_enforces_entity_ownership(self):
        with self.permissions(self.upg):
            created = TaskService(self.upg).create({
                "source": "BenefitPlanService",
                "entity": self.plan_upg,
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
                "status": Task.Status.RECEIVED,
                "data": {},
            })
        self.assertFalse(denied["success"])

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

    def test_enabled_mode_fails_closed_without_shared_service(self):
        with patch(
            "tasks_management.benefit_plan_access._shared_access_helpers",
            side_effect=ImproperlyConfigured("shared service unavailable"),
        ):
            with self.assertRaises(ImproperlyConfigured):
                filter_tasks_by_benefit_plan(Task.objects.all(), self.upg)

    def test_shared_registry_sources_and_unknown_sources_are_distinct(self):
        self.assertEqual(
            resolve_task_ownership({"source": "IndividualService", "data": {}}),
            (None, True),
        )
        self.assertEqual(
            resolve_task_ownership({"source": "unrecognised", "data": {}}),
            (None, False),
        )

    def test_known_programme_entity_types_resolve_without_guessing(self):
        content_type = SimpleNamespace(
            app_label="social_protection",
            model="benefitplan",
        )
        payment_plan = SimpleNamespace(
            benefit_plan_type=content_type,
            benefit_plan_id=self.plan_upg.id,
        )
        payroll = SimpleNamespace(
            _meta=SimpleNamespace(app_label="payroll", model_name="payroll"),
            payment_plan=payment_plan,
        )
        upload = SimpleNamespace(
            _meta=SimpleNamespace(
                app_label="social_protection",
                model_name="benefitplandatauploadrecords",
            ),
            benefit_plan_id=self.plan_upg.id,
        )

        class Links(list):
            def filter(self, **kwargs):
                return self

            def select_related(self, *args):
                return self

        benefit = SimpleNamespace(
            _meta=SimpleNamespace(
                app_label="payroll",
                model_name="benefitconsumption",
            ),
            payrollbenefitconsumption_set=Links([
                SimpleNamespace(payroll=payroll),
            ]),
        )
        shared_upload = SimpleNamespace(
            _meta=SimpleNamespace(
                app_label="individual",
                model_name="individualdatauploadrecords",
            ),
        )

        expected = (str(self.plan_upg.id), False)
        self.assertEqual(resolve_task_ownership({"entity": payroll}), expected)
        self.assertEqual(resolve_task_ownership({"entity": benefit}), expected)
        self.assertEqual(resolve_task_ownership({"entity": upload}), expected)
        self.assertEqual(
            resolve_task_ownership({"entity": shared_upload}),
            (None, True),
        )

    def test_historical_backfill_uses_proven_entity_ownership(self):
        scoped = self.make_task("backfill-plan", group=self.group)
        scoped.entity = self.plan_upg
        scoped.save(username=self.admin.username)
        shared = self.make_task("IndividualService", group=self.group)

        migration = importlib.import_module(
            "tasks_management.migrations.0011_task_benefit_plan_ownership"
        )
        migration.backfill_task_ownership(
            apps,
            SimpleNamespace(connection=connection),
        )

        scoped.refresh_from_db()
        shared.refresh_from_db()
        self.assertEqual(scoped.benefit_plan_id, str(self.plan_upg.id))
        self.assertFalse(scoped.benefit_plan_is_shared)
        self.assertTrue(shared.benefit_plan_is_shared)
        self.assertTrue(
            scoped.history.filter(
                benefit_plan_id=str(self.plan_upg.id),
                benefit_plan_is_shared=False,
            ).exists()
        )
