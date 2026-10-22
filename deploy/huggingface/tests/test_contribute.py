# SPDX-License-Identifier: Apache-2.0
"""Run with: python -m unittest discover -s deploy/huggingface/tests  (needs gradio installed)

The Contribute tab's plain-language helpers: no network, no Gradio app.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "databounty"))

try:
    import contribute
except ImportError:  # pragma: no cover - only where gradio isn't installed
    contribute = None

LONG_SPEC = (
    "THE GRADED ARTIFACT. Python code defining EXACTLY ONE top-level function named `build_parser`, with EXACTLY "
    "this signature: `def build_parser():` -- no arguments; it must construct and return a parser."
)


@unittest.skipIf(contribute is None, "gradio is not installed")
class FieldHintTest(unittest.TestCase):
    def test_a_long_spec_gives_way_to_a_plain_hint_for_the_role(self):
        self.assertEqual(contribute.short_hint({"role": "solution_code", "help": LONG_SPEC}), "Your working solution.")

    def test_a_short_plain_help_is_kept(self):
        self.assertEqual(contribute.short_hint({"role": "instruction", "help": "The bug, in one sentence."}), "The bug, in one sentence.")

    def test_no_help_falls_back_to_the_role(self):
        self.assertEqual(contribute.short_hint({"role": "tests"}), "Tests that show the solution works.")
        self.assertEqual(contribute.short_hint({"role": "made-up"}), "")

    def test_code_fields_get_an_editor_language(self):
        self.assertEqual(contribute.code_language({"lang": "py"}), "python")
        self.assertEqual(contribute.code_language({"label": "Parser builder (Python)"}), "python")
        self.assertEqual(contribute.code_language({"label": "Invocations", "help": "JSON array of runs"}), "json")
        self.assertIsNone(contribute.code_language({"label": "Go solution", "lang": "go"}))


@unittest.skipIf(contribute is None, "gradio is not installed")
class PoolCardTest(unittest.TestCase):
    def test_the_short_line_comes_before_the_title(self):
        pool = {"title": "CLI Argument Parser", "difficulty": "advanced", "karma_per_accepted_item": 40, "target_items": 5000, "accepted_items": 60}
        meta, title = contribute.pool_card_label(pool).split("\n")
        self.assertEqual(meta, "Advanced · 40 karma · 4,940 wanted")
        self.assertEqual(title, "CLI Argument Parser")

    def test_search_matches_every_word(self):
        pools = [
            {"id": "a", "title": "LRU cache", "dataset_type": "Caching", "difficulty": "advanced"},
            {"id": "b", "title": "SQL joins", "dataset_type": "Text-to-SQL", "difficulty": "advanced"},
        ]
        self.assertEqual([value for _, value in contribute._pool_choices(pools, "cache advanced")], ["a"])
        self.assertEqual(len(contribute._pool_choices(pools, "")), 2)


@unittest.skipIf(contribute is None, "gradio is not installed")
class CheckStateTest(unittest.TestCase):
    """Checks read the way DataBounty reads them: no score means not run, never failed."""

    def test_states(self):
        cases = {
            "passed": ({"stage": "dedupe", "passed": True, "score": 0.1}, "pass"),
            "not run yet": ({"stage": "execution", "passed": False, "score": None}, "wait"),
            "failed with its reason": (
                {"stage": "execution", "passed": False, "score": 0.4, "detailJson": {"reason": "2 of 5 tests failed"}},
                "fail",
            ),
            "advisory quality review": ({"stage": "llm", "passed": False, "score": 41}, "note"),
            "pool filled first": ({"stage": "pool_capacity", "passed": False, "score": None}, "fail"),
            "close to a benchmark, for a reviewer": (
                {"stage": "contamination", "passed": True, "score": 0.4, "detailJson": {"contaminationDecision": "review_required"}},
                "note",
            ),
        }
        for name, (result, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(contribute.check_state(result)[0], expected)
        self.assertEqual(contribute.check_state(cases["failed with its reason"][0])[1], "2 of 5 tests failed")

    def test_statuses_group_the_way_people_think(self):
        self.assertEqual(contribute.status_view("tests_failed")[:2], ("Needs your fix", "🟠"))
        self.assertEqual(contribute.status_view("in_audit")[0], "In review")
        self.assertEqual(contribute.status_view("brand_new_status")[0], "Brand new status")


@unittest.skipIf(contribute is None, "gradio is not installed")
class TemplateAndRequirementsTest(unittest.TestCase):
    FIELDS = [
        {"key": "task", "label": "Task", "role": "instruction", "required": True, "help": "What to build, e.g. a_b."},
        {"key": "solution", "label": "Solution", "role": "solution_code", "required": True, "help": LONG_SPEC},
        {"key": "screenshot", "label": "Screenshot", "role": "file", "required": False},
    ]

    def test_the_template_has_every_column_but_file_fields(self):
        path = contribute.template_csv({"title": "My Pool", "fields": self.FIELDS})
        self.assertEqual(Path(path).read_text(encoding="utf-8").strip(), "task,solution")

    def test_requirements_keep_code_readable_and_escape_the_rest(self):
        text = contribute._requirements_md({"fields": self.FIELDS, "description": "", "guidance": ""})
        self.assertIn("`build_parser`", text)
        self.assertIn("`def build_parser():`", text)
        self.assertNotIn("build\\_parser", text)
        self.assertIn("a\\_b", text)


if __name__ == "__main__":
    unittest.main()
