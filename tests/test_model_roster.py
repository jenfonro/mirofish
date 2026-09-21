"""Pure tests; BUILTIN_SUBSET is source data, NOT a live account response.

Fixture source: Mirasim 0.0.303 Resources/server.cjs, Ecr at UTF-8 byte
2598828. Filtering: FKt; Claude variants: rUe/nUe. Other IDs are synthetic.
No private client source, credentials, network calls or account state.
"""

from copy import deepcopy
import unittest

from mirofish.model_roster import entries, normalize_roster, supported_model


BUILTIN_SUBSET = {
    "version": "builtin",
    "agents": {
        "claude": [{
            "id": "claude-opus-4-6", "label": "opus 4.6",
            "contextWindow": 1_000_000, "maxOutput": 128_000,
            "autoCompactRatio": 0.95, "adaptive": True,
        }],
        "kimi": [{
            "id": "kimi-code/k3", "label": "kimi k3",
            "contextWindow": 1_048_576, "autoCompactRatio": 0.85,
            "effort": ["low", "high", "max"],
        }],
    },
}


def roster_for(window=1_000_000, *, agent="claude", model_id="claude-example", **fields):
    return {"version": "test", "agents": {agent: [
        {"id": model_id, "contextWindow": window, **fields},
    ]}}


class NormalizeRosterTests(unittest.TestCase):
    def test_source_fixture_and_idempotence(self):
        normalized = normalize_roster(BUILTIN_SUBSET)
        self.assertEqual(normalized, BUILTIN_SUBSET)
        self.assertEqual(normalize_roster(normalized), normalized)

    def test_invalid_top_level(self):
        for body in (None, [], True, "roster", {}, {"version": "v"}, {"agents": {}}):
            with self.subTest(body=body):
                self.assertIsNone(normalize_roster(body))

    def test_invalid_version_and_agents(self):
        for version in (None, 1, True, "", " \t"):
            with self.subTest(version=version):
                body = roster_for()
                body["version"] = version
                self.assertIsNone(normalize_roster(body))
        for agents in (None, [], "claude", {}, {"claude": []}, {"claude": {}}):
            with self.subTest(agents=agents):
                self.assertIsNone(normalize_roster({"version": "v", "agents": agents}))

    def test_string_normalization_and_agent_name_preserved(self):
        body = roster_for(agent="CustomAgent", model_id=" Model-A ", label=" Label A ")
        body["version"] = " Release-A "
        self.assertEqual(normalize_roster(body), {
            "version": "Release-A", "agents": {"CustomAgent": [
                {"id": "model-a", "label": "label a", "contextWindow": 1_000_000},
            ]},
        })

    def test_missing_or_invalid_label_falls_back_to_id(self):
        for fields in ({}, {"label": None}, {"label": ""}, {"label": " \t"}, {"label": 7}):
            with self.subTest(fields=fields):
                row = normalize_roster(roster_for(model_id=" Model-A ", **fields))
                self.assertEqual(row["agents"]["claude"][0]["label"], "model-a")

    def test_invalid_rows_are_filtered_without_losing_valid_agents(self):
        body = roster_for()
        body["agents"]["claude"][:0] = [
            None, True, [], "model", {}, {"id": True, "contextWindow": 1},
            {"id": " ", "contextWindow": 1}, {"id": "missing-window"},
        ]
        body["agents"].update({"empty": [], "invalid": {}, "bad": [None]})
        self.assertEqual(normalize_roster(body), normalize_roster(roster_for()))

    def test_context_window_rejects_bool_nonfinite_and_nonpositive(self):
        for value in (None, True, False, "1000000", [], {}, 0, -1,
                      float("nan"), float("inf"), -float("inf")):
            with self.subTest(value=value):
                self.assertIsNone(normalize_roster(roster_for(value)))

    def test_positive_numeric_windows_are_not_rounded(self):
        for value in (1, 0.5, 999_999.5, 1_000_000.0):
            with self.subTest(value=value):
                row = normalize_roster(roster_for(value))["agents"]["claude"][0]
                self.assertEqual(row["contextWindow"], value)

    def test_max_output_is_optional_and_positive_finite(self):
        for value in (None, True, False, "128000", 0, -1, float("nan"), float("inf")):
            with self.subTest(value=value):
                row = normalize_roster(roster_for(maxOutput=value))["agents"]["claude"][0]
                self.assertNotIn("maxOutput", row)
        row = normalize_roster(roster_for(maxOutput=12.5))["agents"]["claude"][0]
        self.assertEqual(row["maxOutput"], 12.5)

    def test_auto_compact_ratio_range(self):
        for value in (None, True, False, "0.95", 0, -1, 1.01, float("nan"), float("inf")):
            with self.subTest(value=value):
                row = normalize_roster(roster_for(autoCompactRatio=value))["agents"]["claude"][0]
                self.assertNotIn("autoCompactRatio", row)
        for value in (0.01, 0.95, 1):
            with self.subTest(value=value):
                row = normalize_roster(roster_for(autoCompactRatio=value))["agents"]["claude"][0]
                self.assertEqual(row["autoCompactRatio"], value)

    def test_effort_filters_without_normalizing_or_deduplicating_strings(self):
        values = [" HIGH ", "low", "low", "", " \t", None, True, 1, {}]
        row = normalize_roster(roster_for(effort=values))["agents"]["claude"][0]
        self.assertEqual(row["effort"], [" HIGH ", "low", "low"])
        for value in (None, "high", [], ["", " ", None], {"high": True}):
            with self.subTest(value=value):
                row = normalize_roster(roster_for(effort=value))["agents"]["claude"][0]
                self.assertNotIn("effort", row)

    def test_adaptive_requires_literal_true(self):
        for value in (None, False, 1, 1.0, "true", []):
            with self.subTest(value=value):
                row = normalize_roster(roster_for(adaptive=value))["agents"]["claude"][0]
                self.assertNotIn("adaptive", row)
        row = normalize_roster(roster_for(adaptive=True))["agents"]["claude"][0]
        self.assertIs(row["adaptive"], True)

    def test_unknown_fields_are_not_permissions(self):
        body = roster_for(enabled=True, variants=["1m"], supports1M=True,
                          capabilities={"max_output": 10})
        body.update({"fetched_epoch": 1, "defaultOn": True})
        self.assertEqual(normalize_roster(body), normalize_roster(roster_for()))

    def test_order_and_duplicate_ids_are_preserved(self):
        body = roster_for()
        body["agents"]["claude"] *= 2
        normalized = normalize_roster(body)
        self.assertEqual(len(normalized["agents"]["claude"]), 2)
        self.assertEqual(normalized["agents"]["claude"][0], normalized["agents"]["claude"][1])


class EntriesTests(unittest.TestCase):
    def test_openai_rows_have_independent_nested_capabilities(self):
        self.assertEqual(entries(BUILTIN_SUBSET), [
            {
                "id": "claude-opus-4-6[1m]", "object": "model", "created": 0,
                "owned_by": "mirofish", "display_name": "opus 4.6",
                "capabilities": {
                    "agent": "claude", "context_window": 1_000_000,
                    "max_output": 128_000, "auto_compact_ratio": 0.95, "adaptive": True,
                },
            },
            {
                "id": "kimi-code/k3", "object": "model", "created": 0,
                "owned_by": "mirofish", "display_name": "kimi k3",
                "capabilities": {
                    "agent": "kimi", "context_window": 1_048_576,
                    "auto_compact_ratio": 0.85, "effort": ["low", "high", "max"],
                },
            },
        ])

    def test_one_m_boundary_replaces_instead_of_copying(self):
        for window, expected in ((999_999, "claude-example"),
                                 (999_999.5, "claude-example"),
                                 (1_000_000, "claude-example[1m]"),
                                 (1_048_576, "claude-example[1m]")):
            with self.subTest(window=window):
                rows = entries(roster_for(window))
                self.assertEqual([row["id"] for row in rows], [expected])
                self.assertEqual(rows[0]["capabilities"]["context_window"], window)

    def test_existing_suffix_is_not_doubled(self):
        self.assertEqual(entries(roster_for(model_id="claude-example[1m]"))[0]["id"],
                         "claude-example[1m]")

    def test_other_agents_never_gain_a_suffix(self):
        for agent, model_id in (("codex", "gpt-example"), ("kimi", "kimi-example"),
                                ("custom", "claude-example")):
            with self.subTest(agent=agent):
                row = entries(roster_for(2_000_000, agent=agent, model_id=model_id))[0]
                self.assertEqual(row["id"], model_id)

    def test_all_agents_are_flattened_without_merging_capabilities(self):
        body = {"version": "test", "agents": {
            "custom": [{"id": "shared", "contextWindow": 10}],
            "other": [{"id": "shared", "contextWindow": 20, "maxOutput": 2}],
        }}
        self.assertEqual([row["capabilities"] for row in entries(body)], [
            {"agent": "custom", "context_window": 10},
            {"agent": "other", "context_window": 20, "max_output": 2},
        ])

    def test_unavailable_roster_is_not_a_builtin_catalog(self):
        for body in (None, {}, {"version": "v", "agents": {}}):
            with self.subTest(body=body):
                self.assertEqual(entries(body), [])


class SupportedModelTests(unittest.TestCase):
    def test_bare_and_explicit_claude_map_to_advertised_one_m(self):
        for requested in ("claude-opus-4-6", "claude-opus-4-6[1m]"):
            with self.subTest(requested=requested):
                self.assertEqual(supported_model(BUILTIN_SUBSET, requested),
                                 "claude-opus-4-6[1m]")

    def test_one_m_boundary_and_no_explicit_downgrade(self):
        for window in (999_999, 1_000_000):
            with self.subTest(window=window):
                body = roster_for(window)
                expected = "claude-example[1m]" if window >= 1_000_000 else "claude-example"
                self.assertEqual(supported_model(body, "claude-example"), expected)
                self.assertEqual(supported_model(body, "claude-example[1m]"),
                                 expected if window >= 1_000_000 else None)

    def test_suffix_alone_does_not_grant_one_m_capability(self):
        body = roster_for(200_000, model_id="claude-example[1m]")
        self.assertIsNone(supported_model(body, "claude-example[1m]"))
        self.assertIsNone(supported_model(body, "claude-example"))

    def test_nonclaude_matching_is_exact(self):
        body = roster_for(agent="codex", model_id="gpt-example")
        self.assertEqual(supported_model(body, "gpt-example"), "gpt-example")
        for requested in ("GPT-EXAMPLE", " gpt-example ", "gpt-example[1m]",
                          "gpt-example-20260101", "gpt"):
            with self.subTest(requested=requested):
                self.assertIsNone(supported_model(body, requested))

    def test_no_cross_agent_or_cross_model_one_m_alias(self):
        body = roster_for(200_000)
        body["agents"]["other"] = [
            {"id": "claude-other", "contextWindow": 2_000_000},
        ]
        self.assertEqual(supported_model(body, "claude-other"), "claude-other")
        self.assertIsNone(supported_model(body, "claude-other[1m]"))
        self.assertIsNone(supported_model(body, "claude-example[1m]"))

    def test_one_m_alias_wins_even_if_bare_id_is_also_listed(self):
        for windows in ((200_000, 1_000_000), (1_000_000, 200_000)):
            with self.subTest(windows=windows):
                body = {"version": "test", "agents": {"claude": [
                    {"id": "claude-example", "contextWindow": value} for value in windows
                ]}}
                self.assertEqual(supported_model(body, "claude-example"), "claude-example[1m]")

    def test_unlisted_models_and_invalid_requests_fail_closed(self):
        for requested in (None, True, "", " ", "CLAUDE-EXAMPLE", "claude-opus-4-6"):
            with self.subTest(requested=requested):
                self.assertIsNone(supported_model(roster_for(), requested))
        for body in (None, {}, {"version": "v", "agents": {}}):
            with self.subTest(body=body):
                self.assertIsNone(supported_model(body, "claude-opus-4-6[1m]"))

    def test_capabilities_are_not_shared_between_rosters(self):
        large, small = roster_for(), roster_for(200_000)
        self.assertEqual(supported_model(large, "claude-example[1m]"), "claude-example[1m]")
        self.assertIsNone(supported_model(small, "claude-example[1m]"))
        self.assertEqual(supported_model(small, "claude-example"), "claude-example")
        self.assertEqual(supported_model(large, "claude-example"), "claude-example[1m]")

    def test_helpers_do_not_mutate_or_alias_inputs(self):
        original = deepcopy(BUILTIN_SUBSET)
        normalized = normalize_roster(BUILTIN_SUBSET)
        rows = entries(BUILTIN_SUBSET)
        supported_model(BUILTIN_SUBSET, "claude-opus-4-6")
        normalized["agents"]["kimi"][0]["effort"].append("synthetic")
        rows[1]["capabilities"]["effort"].append("synthetic")
        rows[0]["capabilities"]["context_window"] = 1
        self.assertEqual(BUILTIN_SUBSET, original)
