"""JSON literal equality through extract → condition → the selected tool.

Callbacks are synthetic: no model, MCP client, database, or network is used.
"""

from __future__ import annotations

import json
import unittest

from app.services.quick_flow import execute_graph


def _graph(operator, expected, *, field="value"):
    return {
        "nodes": [
            {"id": "in", "kind": "input", "config": {}},
            {"id": "extract", "kind": "extract", "config": {"fields": ["value"]}},
            {"id": "check", "kind": "condition", "config": {
                "field": field, "operator": operator, "value": expected,
            }},
            {"id": "yes", "kind": "mcp", "config": {
                "tool_id": 11, "arguments": {"branch": "true"},
            }},
            {"id": "no", "kind": "mcp", "config": {
                "tool_id": 12, "arguments": {"branch": "false"},
            }},
            {"id": "out", "kind": "output", "config": {}},
        ],
        "edges": [
            {"source": "in", "target": "extract"},
            {"source": "extract", "target": "check"},
            {"source": "check", "target": "yes", "branch": "true"},
            {"source": "check", "target": "no", "branch": "false"},
            {"source": "yes", "target": "out"},
            {"source": "no", "target": "out"},
        ],
    }


class ConditionLiteralFlowTests(unittest.IsolatedAsyncioTestCase):
    async def assert_branch(self, actual, expected, operator, decision, *, field="value"):
        models, tools = [], []

        async def model(messages, temperature):
            models.append((messages, temperature))
            return json.dumps({"value": actual}, ensure_ascii=False, allow_nan=False)

        async def tool(tool_id, arguments):
            tools.append((tool_id, arguments))
            return {"selected_branch": arguments["branch"]}

        async def unused(*args, **kwargs):
            self.fail("This graph must not invoke retrieval or document callbacks")

        result = await execute_graph(
            _graph(operator, expected, field=field), "合成條件分支問題",
            model_call=model, tool_call=tool, rag_call=unused, document_call=unused,
        )
        self.assertEqual(result["status"], "completed", result)
        self.assertEqual(len(models), 1)
        branch = str(decision).lower()
        selected_id, skipped_id = ("yes", "no") if decision else ("no", "yes")
        self.assertEqual(tools, [(11 if decision else 12, {"branch": branch})])
        self.assertEqual(json.loads(result["output"]), {"selected_branch": branch})
        steps = {step["node_id"]: step for step in result["steps"]}
        self.assertEqual(steps["check"]["branch"], branch)
        self.assertIn(selected_id, steps)
        self.assertNotIn(skipped_id, steps)
        extracted = steps["extract"]["output"]["fields"]["value"]
        self.assertIs(type(extracted), type(actual))
        self.assertEqual(extracted, actual)

    async def test_boolean_and_number_never_select_equal_branch(self):
        for actual, expected in (
            (False, 0), (0, False), (True, 1), (1, True),
            (False, 0.0), (0.0, False), (True, 1.0), (1.0, True),
        ):
            for operator, decision in (("equals", False), ("not_equals", True)):
                with self.subTest(actual=actual, expected=expected, operator=operator):
                    await self.assert_branch(actual, expected, operator, decision)

    async def test_same_boolean_values_keep_normal_branch_behavior(self):
        for actual, expected, equal in (
            (False, False, True), (True, True, True),
            (False, True, False), (True, False, False),
        ):
            for operator in ("equals", "not_equals"):
                with self.subTest(actual=actual, expected=expected, operator=operator):
                    await self.assert_branch(
                        actual, expected, operator, equal if operator == "equals" else not equal,
                    )

    async def test_integer_and_float_share_json_number_equality(self):
        for actual, expected, equal in (
            (0, 0.0, True), (0.0, 0, True), (1, 1.0, True),
            (1.5, 1, False), (0, 1.0, False),
        ):
            for operator in ("equals", "not_equals"):
                with self.subTest(actual=actual, expected=expected, operator=operator):
                    await self.assert_branch(
                        actual, expected, operator, equal if operator == "equals" else not equal,
                    )

    async def test_strings_do_not_coerce_to_number_or_boolean(self):
        for actual, expected, equal in (
            ("0", 0, False), (0, "0", False), ("true", True, False),
            (False, "false", False), ("0", "0", True), ("甲", "乙", False),
        ):
            for operator in ("equals", "not_equals"):
                with self.subTest(actual=actual, expected=expected, operator=operator):
                    await self.assert_branch(
                        actual, expected, operator, equal if operator == "equals" else not equal,
                    )

    async def test_null_and_missing_actual_keep_existing_false_equality_policy(self):
        for field in ("value", "missing"):
            for expected in (None, False, 0, ""):
                for operator in ("equals", "not_equals"):
                    with self.subTest(field=field, expected=expected, operator=operator):
                        await self.assert_branch(None, expected, operator, False, field=field)

    async def test_is_empty_and_nonnull_compared_to_null_are_unchanged(self):
        for field in ("value", "missing"):
            with self.subTest(field=field):
                await self.assert_branch(None, "ignored", "is_empty", True, field=field)
        await self.assert_branch("", None, "is_empty", True)
        await self.assert_branch(False, None, "is_empty", False)
        for actual in (False, 0, "value"):
            for operator, decision in (("equals", False), ("not_equals", True)):
                with self.subTest(actual=actual, operator=operator):
                    await self.assert_branch(actual, None, operator, decision)
