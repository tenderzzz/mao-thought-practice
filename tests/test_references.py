"""Check source attribution and navigability, without prescribing model wording."""
import json
from pathlib import Path
import re
import unittest
from urllib.parse import unquote

ROOT = Path(__file__).resolve().parents[1]


class ReferenceTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.registry = json.loads((ROOT / "references" / "sources.json").read_text(encoding="utf-8"))
        cls.sources = cls.registry["sources"]
        cls.ids = {source["id"] for source in cls.sources}

    def test_sources_are_unique_and_have_traceable_versions(self):
        self.assertEqual(len(self.ids), len(self.sources))
        self.assertEqual(len({s["path"] for s in self.sources}), len(self.sources))
        self.assertRegex(self.registry["revision"], r"^[0-9a-f]{40}$")
        for source in self.sources:
            with self.subTest(source=source["id"]):
                for key in ("title", "author", "date", "section", "quote", "context", "application_limit", "url"):
                    self.assertTrue(source[key].strip())
                self.assertRegex(source["blob_sha"], r"^[0-9a-f]{40}$")
                self.assertIn(source["title"], source["path"])

    def test_cited_source_ids_exist(self):
        for path in list((ROOT / "references").glob("*.md")) + [ROOT / "SKILL.md", ROOT / "README.md"]:
            for sid in re.findall(r"\bS[0-9]{2,}\b", path.read_text(encoding="utf-8")):
                with self.subTest(file=path.name, source=sid):
                    self.assertIn(sid, self.ids)

    def test_local_markdown_links_resolve(self):
        for path in ROOT.rglob("*.md"):
            if ".git" in path.parts:
                continue
            for target in re.findall(r"\[[^\]]*\]\(([^)]+)\)", path.read_text(encoding="utf-8")):
                if re.match(r"^[a-z]+://", target) or target.startswith("#"):
                    continue
                relative = unquote(target.split("#", 1)[0])
                resolved = (path.parent / relative).resolve()
                with self.subTest(file=str(path.relative_to(ROOT)), target=target):
                    self.assertTrue(resolved.exists(), "Broken local reference")
                    self.assertTrue(resolved == ROOT or ROOT in resolved.parents)

    def test_preparation_quote_attribution_regression(self):
        matches = [s for s in self.sources if "不打无准备之仗" in s["quote"]]
        self.assertEqual(len(matches), 1)
        self.assertEqual(matches[0]["title"], "目前形势和我们的任务")
        self.assertEqual(matches[0]["date"], "1947-12-25")


if __name__ == "__main__":
    unittest.main()
