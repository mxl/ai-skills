"""Offline packaging checks; not a substitute for the host skill validator."""
from pathlib import Path
import json
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]


class SkillStructureTests(unittest.TestCase):
    def test_frontmatter_name_and_description(self):
        text = (ROOT / "SKILL.md").read_text()
        self.assertTrue(text.startswith("---\n"))
        frontmatter, body = text[4:].split("\n---\n", 1)
        name = re.search(r"^name: (.+)$", frontmatter, re.M).group(1)
        self.assertEqual(name, ROOT.name)
        self.assertRegex(name, r"^[a-z0-9]+(-[a-z0-9]+)*$")
        self.assertIsNotNone(re.search(r"^description: .{80,}$", frontmatter, re.M))
        self.assertIn("## 1.", body)
        self.assertLess(len(text.splitlines()), 500)

    def test_local_markdown_links_exist_and_stay_inside_skill(self):
        documents = [ROOT / "SKILL.md", *(ROOT / "references").glob("*.md")]
        for document in documents:
            for target in re.findall(r"\]\(([^)]+)\)", document.read_text()):
                if "://" in target or target.startswith("#"):
                    continue
                resolved = (document.parent / target.split("#", 1)[0]).resolve()
                self.assertTrue(resolved.is_relative_to(ROOT), (document.name, target))
                self.assertTrue(resolved.is_file(), (document.name, target))

    def test_evals_have_unique_cases_and_checkable_expectations(self):
        value = json.loads((ROOT / "evals" / "evals.json").read_text())
        self.assertEqual(value["skill_name"], "browser-automation")
        cases = value["evals"]
        self.assertEqual(len(cases), 3)
        self.assertEqual(len({case["id"] for case in cases}), len(cases))
        for case in cases:
            self.assertIn("Planning-only", case["prompt"])
            self.assertTrue(case["expected_output"])
            self.assertGreaterEqual(len(case["expectations"]), 5)
            self.assertTrue(all(isinstance(item, str) and len(item) > 30
                                for item in case["expectations"]))

    def test_source_compiles_without_creating_bytecode(self):
        source = ROOT / "scripts" / "browser_session.py"
        compile(source.read_text(), str(source), "exec")

    def test_docs_keep_qualification_and_approval_boundaries(self):
        text = (ROOT / "SKILL.md").read_text()
        self.assertIn("not a sandbox", text)
        self.assertIn("host question tool", text)
        self.assertIn("No Bitwarden CLI, Agent Access", text)
        self.assertIn("before being described as qualified", text)
        self.assertIn("not evidence of human consent", text.replace("**", ""))
        operations = (ROOT / "references" / "operations.md").read_text()
        self.assertIn("no runtime-location override", operations)
        human = (ROOT / "references" / "human-input.md").read_text()
        self.assertIn("leaves human ownership intact", human)

    def test_packaging_has_no_machine_specific_home_paths(self):
        for document in [ROOT / "SKILL.md", *(ROOT / "references").glob("*.md")]:
            self.assertNotRegex(document.read_text(), r"/Users/[^/\s]+/")


if __name__ == "__main__":
    unittest.main()
