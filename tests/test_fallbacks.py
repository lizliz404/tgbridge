"""Fallback chain: quota classification, model resolution, failover order."""

import unittest
from unittest import mock

from tgbridge_core.runners import (
    AUTO_OPENCODE_GO_MODEL,
    classify_run_error,
    fallback_chain,
    is_quota_error,
    model_candidates,
    resolve_model,
    select_opencode_go_models,
)


class QuotaClassificationTest(unittest.TestCase):
    def test_weekly_limit_variants(self):
        for text in (
            "codex failed rc=1\nweekly limit reached, resets Monday",
            "Usage Limit Exceeded for this week",
            "HTTP 429 too many requests",
            "insufficient_quota: you exceeded your current quota",
            "RATE_LIMIT_EXCEEDED, retry later",
            "billing: credits exhausted",
            "Payment Required (402)",
            "trial expired, add payment method",
            "resource_exhausted: quota",
        ):
            self.assertTrue(is_quota_error(text), text)

    def test_nontagged_text_is_not_quota(self):
        for text in (
            "",
            None,
            "agent returned no text",
            "opencode failed rc=1\nconnection refused",
            "unknown runner 'foo'",
            "runner 'codex' not found on PATH",
            "agent hit the idle timeout and was killed",
            "bridge error: boom",
        ):
            self.assertFalse(is_quota_error(text), repr(text))

    def test_failure_tags_are_labels_not_gates(self):
        # The bridge fails over on ANY failure except cancel; these tags
        # only label the audit trail / user note.
        self.assertEqual(classify_run_error("weekly limit reached"), "quota")
        self.assertEqual(
            classify_run_error("opencode failed rc=1\nconnection refused"),
            "unavailable",
        )
        self.assertEqual(
            classify_run_error("runner 'codex' not found on PATH"), "unavailable"
        )
        self.assertEqual(
            classify_run_error("agent hit the idle timeout and was killed"), "other"
        )
        self.assertEqual(classify_run_error(""), "other")

    def test_extra_markers_extend_builtins(self):
        self.assertFalse(is_quota_error("sptoast saturated? no", []))
        self.assertTrue(is_quota_error("sptoast saturated", ["sptoast saturated"]))
        self.assertTrue(is_quota_error("weekly limit reached", ["never-matches"]))


class ModelResolutionTest(unittest.TestCase):
    def test_legacy_single_model_unchanged(self):
        cfg = {"runner": "codex", "model": "openai/gpt-5"}
        self.assertEqual(resolve_model(cfg, "codex"), "openai/gpt-5")

    def test_empty_means_runner_default(self):
        self.assertEqual(resolve_model({"runner": "opencode"}, "opencode"), "")

    def test_per_runner_override_wins(self):
        cfg = {
            "runner": "codex",
            "model": "openai/gpt-5",
            "runner_models": {"opencode": "opencode-go/muse-spark-1.3-contributor"},
        }
        self.assertEqual(resolve_model(cfg, "codex"), "openai/gpt-5")
        self.assertEqual(
            resolve_model(cfg, "opencode"), "opencode-go/muse-spark-1.3-contributor"
        )

    def test_explicit_step_model_wins_all(self):
        cfg = {"model": "a/b", "runner_models": {"opencode": "c/d"}}
        self.assertEqual(resolve_model(cfg, "opencode", "e/f"), "e/f")

    def test_go_selector_tracks_future_versions_and_provider(self):
        models = [
            "opencode/muse-spark-99-contributor",
            "opencode-go/muse-spark-9.0",
            "opencode-go/muse-spark-1.3-contributor",
            "opencode-go/muse-spark-1.10-contributor",
            "opencode-go/glm-5.3",
            "opencode-go/glm-5.3-flash",
            "opencode-go/glm-5.4-flash",
        ]
        self.assertEqual(
            select_opencode_go_models(models),
            [
                "opencode-go/muse-spark-1.10-contributor",
                "opencode-go/glm-5.4-flash",
            ],
        )

    def test_go_selector_falls_back_to_latest_full_glm(self):
        models = [
            "opencode-go/glm-5.2",
            "opencode-go/glm-5.3-flash",
            "opencode-go/glm-5.3",
        ]
        self.assertEqual(select_opencode_go_models(models), ["opencode-go/glm-5.3"])

    def test_auto_model_resolves_to_first_discovered_candidate(self):
        with mock.patch(
            "tgbridge_core.runners.discover_opencode_go_models",
            return_value=["opencode-go/muse-spark-2-contributor", "opencode-go/glm-6"],
        ):
            self.assertEqual(
                model_candidates({}, AUTO_OPENCODE_GO_MODEL),
                ["opencode-go/muse-spark-2-contributor", "opencode-go/glm-6"],
            )
            self.assertEqual(
                resolve_model(
                    {"runner_models": {"opencode": AUTO_OPENCODE_GO_MODEL}},
                    "opencode",
                ),
                "opencode-go/muse-spark-2-contributor",
            )


class FallbackChainTest(unittest.TestCase):
    def test_no_fallbacks_by_default(self):
        self.assertEqual(fallback_chain({"runner": "codex"}), [])

    def test_chain_resolves_models_and_dedupes_primary(self):
        cfg = {
            "runner": "codex",
            "model": "",
            "runner_models": {"opencode": "opencode-go/muse-spark-1.3-contributor"},
            "runner_fallbacks": [
                {"runner": "codex"},  # exact duplicate of primary: dropped
                {"runner": "opencode"},
                {"runner": "opencode", "model": "opencode/gemini-3.8-flash"},
            ],
        }
        self.assertEqual(
            fallback_chain(cfg),
            [
                ("opencode", "opencode-go/muse-spark-1.3-contributor"),
                ("opencode", "opencode/gemini-3.8-flash"),
            ],
        )

    def test_malformed_entries_ignored(self):
        cfg = {
            "runner": "codex",
            "runner_fallbacks": ["opencode", {"model": "x/y"}, None, {"runner": ""}],
        }
        self.assertEqual(fallback_chain(cfg), [])

    def test_non_list_fallbacks_ignored(self):
        self.assertEqual(
            fallback_chain({"runner": "codex", "runner_fallbacks": "opencode"}), []
        )

    def test_auto_selector_expands_muse_then_glm(self):
        cfg = {
            "runner": "codex",
            "runner_fallbacks": [
                {"runner": "opencode", "model": AUTO_OPENCODE_GO_MODEL}
            ],
        }
        with mock.patch(
            "tgbridge_core.runners.discover_opencode_go_models",
            return_value=[
                "opencode-go/muse-spark-2-contributor",
                "opencode-go/glm-6",
            ],
        ):
            self.assertEqual(
                fallback_chain(cfg),
                [
                    ("opencode", "opencode-go/muse-spark-2-contributor"),
                    ("opencode", "opencode-go/glm-6"),
                ],
            )


if __name__ == "__main__":
    unittest.main()
