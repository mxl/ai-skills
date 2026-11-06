"""Discovery metadata and routing fixtures; not a model-invocation guarantee."""
import json
from pathlib import Path
import re
import unittest

ROOT = Path(__file__).resolve().parents[1]


class ActivationContractTests(unittest.TestCase):
    def test_native_advertisement_is_explicitly_enabled(self):
        frontmatter = (ROOT / "SKILL.md").read_text().split("---", 2)[1]
        self.assertRegex(frontmatter, r"(?m)^metadata:\n  opencode/autoinvoke: true$")
        self.assertNotRegex(frontmatter, r"(?m)^disable-model-invocation: true$")
        self.assertNotIn("opencode/autoinvoke: false", frontmatter)

    def test_description_routes_single_task_actions_without_skill_name(self):
        text = (ROOT / "SKILL.md").read_text()
        description = re.search(r"(?m)^description: (.+)$", text).group(1)
        self.assertIn("even for one task", description)
        self.assertIn("without an explicit skill request", description)
        for phrase in ("открой сайт", "заполни форму", "проверь в браузере"):
            self.assertIn(phrase, description)
        for negative in ("static HTTP fetching", "backend-only tests", "explaining browser APIs"):
            self.assertIn(negative, description)

    def test_coordination_recovery_and_recorded_executable_live_in_skill(self):
        text = (ROOT / "SKILL.md").read_text()
        self.assertIn("Use the helpers for normal browser commands", text)
        self.assertIn("tab_gone", text)
        self.assertIn("tab_locked", text)
        self.assertIn("do not automatically create a replacement target", text)
        self.assertIn("recorded/running browser executable", text)
        self.assertIn('--chrome "<verified-project-browser-executable>"', text)

    def test_activation_docs_distinguish_advertisement_from_forced_invocation(self):
        text = (ROOT / "references" / "activation.md").read_text()
        self.assertIn("does **not** mean OpenCode deterministically executes", text)
        self.assertIn("a project AGENTS.md pointer is not required", text)
        self.assertIn('"skills": ["~/projects/ai-skills"]', text)
        self.assertIn("separately approved V2 plugin/tool gate", text)

    def test_routing_set_contains_actions_and_near_miss_negatives(self):
        data = json.loads((ROOT / "evals" / "activation-evals.json").read_text())
        self.assertEqual(data["skill_name"], "browser-automation")
        rows = data["queries"]
        self.assertEqual(len(rows), 20)
        self.assertEqual(len({row["id"] for row in rows}), 20)
        self.assertGreaterEqual(sum(row["should_trigger"] is True for row in rows), 8)
        self.assertGreaterEqual(sum(row["should_trigger"] is False for row in rows), 8)
        for row in rows:
            self.assertIs(type(row["should_trigger"]), bool)
            self.assertNotIn("browser-automation", row["query"])
            self.assertNotIn("AGENTS.md", row["query"])


if __name__ == "__main__":
    unittest.main()
