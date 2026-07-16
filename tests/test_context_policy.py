import unittest

from kirakira_agent.context_policy import (
    build_runtime_context_budget,
    recommended_context_settings,
)


class RecommendedContextSettingsTest(unittest.TestCase):
    def test_reference_window_matches_baseline(self) -> None:
        settings = recommended_context_settings(1_000_000)
        self.assertEqual(settings.memory_window, 160)
        self.assertEqual(settings.output_reserve, 32_768)

    def test_smaller_window_scales_down_and_aligns_to_four(self) -> None:
        settings = recommended_context_settings(128_000)
        self.assertEqual(settings.memory_window % 4, 0)
        self.assertLess(settings.memory_window, 160)
        self.assertGreaterEqual(settings.memory_window, 20)

    def test_tiny_window_clamps_to_minimums(self) -> None:
        settings = recommended_context_settings(8_000)
        self.assertEqual(settings.memory_window, 20)
        self.assertEqual(settings.output_reserve, 4_096)

    def test_output_reserve_never_exceeds_baseline(self) -> None:
        settings = recommended_context_settings(10_000_000)
        self.assertEqual(settings.output_reserve, 32_768)

    def test_rejects_invalid_capacity(self) -> None:
        with self.assertRaises(ValueError):
            recommended_context_settings(0)
        with self.assertRaises(ValueError):
            recommended_context_settings(1000, effective_context_percent=0)
        with self.assertRaises(ValueError):
            recommended_context_settings(1000, effective_context_percent=1.5)


class RuntimeContextBudgetTest(unittest.TestCase):
    def test_budget_splits_effective_context(self) -> None:
        budget = build_runtime_context_budget(100_000, 0.9, 8_192)
        self.assertEqual(budget.effective_context, 90_000)
        self.assertEqual(budget.reserved_output, 8_192)
        self.assertEqual(budget.input_budget, 90_000 - 8_192)

    def test_rejects_output_larger_than_effective_context(self) -> None:
        with self.assertRaises(ValueError):
            build_runtime_context_budget(10_000, 0.9, 9_000_000)

    def test_rejects_non_positive_output(self) -> None:
        with self.assertRaises(ValueError):
            build_runtime_context_budget(10_000, 0.9, 0)


if __name__ == "__main__":
    unittest.main()
