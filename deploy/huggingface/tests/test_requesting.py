# SPDX-License-Identifier: Apache-2.0
"""Run with: python -m unittest discover -s deploy/huggingface/tests

Requesting a dataset from the Space (requesting.py): the form's choices, a request's arguments and
how requests read back. Plain data, no network.
"""

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "databounty"))

import requesting  # noqa: E402

CATALOG = {
    "sampleGate": {"min": 3, "max": 3},
    "datasetTypes": [
        {"id": "debugging", "name": "Debugging", "category": "debugging", "status": "active", "description": "Find  and fix\na bug.",
         "languageSupport": {"mode": "fixed", "languages": [{"id": "typescript", "label": "TypeScript"}]},
         "difficultyLevels": ["beginner", "intermediate", "advanced"], "auditOptions": [0, 25, 100],
         "fields": [{"key": "prompt"}, {"key": "broken_code"}, {"label": "no key"}]},
        {"id": "refactoring", "name": "Refactoring", "category": "implementation", "status": "active",
         "languageSupport": {"mode": "choice", "languages": [{"label": "Python"}, {"label": "Go"}]},
         "difficultyLevels": ["intermediate", "expert"], "auditOptions": [10, 50]},
        {"id": "fail_to_pass", "name": "Fail to pass", "category": "test_generation", "status": "active",
         "languageSupport": {"mode": "any", "languages": []}, "difficultyLevels": []},
        {"id": "transcription", "name": "Transcription", "category": "annotation", "status": "active",
         "languageSupport": {"mode": "none", "languages": []}, "difficultyLevels": ["beginner", "intermediate"]},
        {"id": "retired", "name": "Retired", "category": "implementation", "status": "draft"},
    ],
}
TYPES = {t["id"]: t for t in requesting.catalog_view(CATALOG)["types"]}
FORM = {
    "datasetTypeId": "refactoring",
    "title": "  Go   refactors with tests ",
    "description": "Real refactors that keep every existing test green.\nWith before and after.",
    "language": "go",
    "framework": "",
    "targetItems": "500",
    "difficultyMix": "balanced",
    "auditCoveragePct": 25,
    "proposedLicense": "CC-BY-4.0",
    "idempotencyKey": "3f2b9a64-1d5e-4c1b-9a77-0d2b5c8e6f10",
}


class CatalogTest(unittest.TestCase):
    def test_difficulty_mixes_follow_databounty_rule(self):
        self.assertEqual(requesting.mixes_for(["beginner", "intermediate", "advanced"]), ["mostly_beginner", "balanced", "mostly_advanced"])
        self.assertEqual(requesting.mixes_for(["intermediate", "expert"]), ["balanced", "mostly_advanced"])  # expert counts as advanced
        self.assertEqual(requesting.mixes_for(["beginner", "intermediate"]), ["mostly_beginner", "balanced"])
        self.assertEqual(requesting.mixes_for([]), ["mostly_beginner", "balanced", "mostly_advanced"])  # no levels: any mix

    def test_only_active_types_are_offered_sorted_by_category_then_name(self):
        view = requesting.catalog_view(CATALOG)
        self.assertEqual([t["id"] for t in view["types"]], ["transcription", "debugging", "refactoring", "fail_to_pass"])
        self.assertEqual(view["sampleMin"], 3)
        self.assertEqual(view["defaults"]["targetItems"], 500)
        self.assertEqual([l["value"] for l in view["licences"]], ["CC-BY-4.0", "CC-BY-SA-4.0", "CC0-1.0", "ODC-By-1.0"])

    def test_a_type_carries_what_the_form_needs(self):
        debugging = TYPES["debugging"]
        self.assertEqual((debugging["languageMode"], debugging["languages"]), ("fixed", ["TypeScript"]))
        self.assertEqual(debugging["description"], "Find and fix a bug.")
        self.assertEqual(debugging["fields"], ["prompt", "broken_code"])
        self.assertEqual(TYPES["refactoring"]["auditOptions"], [10, 50])
        self.assertEqual(TYPES["fail_to_pass"]["auditOptions"], [25])  # none declared: the default


class RequestArgumentsTest(unittest.TestCase):
    def test_a_good_form_becomes_request_dataset_arguments(self):
        arguments, problem = requesting.request_arguments(FORM, TYPES)
        self.assertEqual(problem, "")
        self.assertEqual(arguments, {
            "datasetTypeId": "refactoring",
            "title": "Go refactors with tests",
            "description": "Real refactors that keep every existing test green.\nWith before and after.",
            "targetItems": 500,
            "difficultyMix": "balanced",
            "auditCoveragePct": 25,
            "proposedLicense": "CC-BY-4.0",
            "language": "Go",  # the type's own spelling
            "idempotencyKey": FORM["idempotencyKey"],
        })

    def test_the_language_follows_the_type(self):
        fixed, _ = requesting.request_arguments(dict(FORM, datasetTypeId="debugging", language="whatever"), TYPES)
        self.assertEqual(fixed["language"], "TypeScript")
        free, _ = requesting.request_arguments(dict(FORM, datasetTypeId="fail_to_pass", language="Rust"), TYPES)
        self.assertEqual(free["language"], "Rust")
        none, _ = requesting.request_arguments(dict(FORM, datasetTypeId="transcription", language="Python"), TYPES)
        self.assertNotIn("language", none)

    def test_what_databounty_would_refuse_is_caught_first(self):
        cases = {
            "unknown type": (dict(FORM, datasetTypeId="retired"), "Choose a dataset type"),
            "short title": (dict(FORM, title="abc"), "at least 5"),
            "short description": (dict(FORM, description="too short"), "at least 20"),
            "too few items": (dict(FORM, targetItems=9), "between 10"),
            "not a number": (dict(FORM, targetItems="lots"), "between 10"),
            "mix the type lacks": (dict(FORM, difficultyMix="mostly_beginner"), "difficulty"),
            "coverage out of range": (dict(FORM, auditCoveragePct=150), "by hand"),
            "unknown licence": (dict(FORM, proposedLicense="MIT"), "licence"),
            "language not offered": (dict(FORM, language="Rust"), "Python, Go"),
            "hidden characters": (dict(FORM, framework="Dj‮ango"), "hidden control characters"),
        }
        for name, (form, message) in cases.items():
            with self.subTest(name):
                arguments, problem = requesting.request_arguments(form, TYPES)
                self.assertIsNone(arguments)
                self.assertIn(message, problem)

    def test_a_malformed_retry_key_is_left_out(self):
        arguments, _ = requesting.request_arguments(dict(FORM, idempotencyKey="x y"), TYPES)
        self.assertNotIn("idempotencyKey", arguments)


class RequestRowTest(unittest.TestCase):
    ROW = {"id": "r1", "title": "Go refactors", "status": "submitted", "datasetTypeId": "refactoring", "createdAt": "2026-10-23T09:00:00Z",
           "targetItems": 500, "difficultyMix": "balanced", "language": "Go", "proposedLicense": "CC-BY-4.0", "adminNote": None,
           "mintedBountyId": None, "sampleGate": {"ok": False, "approved": 0, "pending": 0, "rejected": 0, "min": 3, "max": 3}}

    def test_a_new_request_asks_for_examples(self):
        row = requesting.request_row(self.ROW, {"refactoring": "Refactoring"})
        self.assertEqual((row["statusLabel"], row["type"], row["date"], row["difficulty"]), ("Waiting for review", "Refactoring", "2026-10-23", "Balanced"))
        self.assertTrue(row["canAddSample"])
        self.assertIn("Add up to 3", row["sampleText"])

    def test_samples_count_against_the_slots_until_rejected(self):
        full = requesting.request_row(dict(self.ROW, sampleGate={"approved": 1, "pending": 2, "rejected": 1, "min": 3, "max": 3}))
        self.assertFalse(full["canAddSample"])
        self.assertEqual(full["sampleText"], "1 of 3 example items approved, 2 waiting for review.")
        freed = requesting.request_row(dict(self.ROW, sampleGate={"approved": 1, "pending": 0, "rejected": 2, "min": 3, "max": 3}))
        self.assertTrue(freed["canAddSample"])

    def test_a_decided_request_takes_no_more_samples_and_links_its_pool(self):
        live = requesting.request_row(dict(self.ROW, status="implemented", mintedBountyId="p9", adminNote="Looks great.",
                                           sampleGate={"ok": True, "approved": 3, "min": 3, "max": 3}))
        self.assertEqual((live["statusLabel"], live["poolId"], live["adminNote"]), ("Pool is open", "p9", "Looks great."))
        self.assertFalse(live["canAddSample"])
        self.assertEqual(live["sampleText"], "Example items approved.")
        declined = requesting.request_row(dict(self.ROW, status="declined"))
        self.assertEqual(declined["statusTone"], "bad")
        self.assertFalse(declined["canAddSample"])


if __name__ == "__main__":
    unittest.main()
