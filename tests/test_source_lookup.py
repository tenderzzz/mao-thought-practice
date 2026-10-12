"""Offline behavior tests for the optional source lookup tool."""
import contextlib
import importlib.util
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.error import URLError

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("source_lookup", ROOT / "scripts" / "source_lookup.py")
lookup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(lookup)


class LookupTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.cache = Path(self.temp.name)
        self.data = "# 例文\n\n前文说明。\n\n这里包含核验短引。\n\n后文说明。\n".encode("utf-8")
        self.source = {
            "id": "S01", "title": "例文", "author": "作者", "date": "1937-08",
            "path": "docs/src/001-例文.md", "quote": "核验短引",
            "blob_sha": lookup.git_blob_sha(self.data), "url": "https://example.org/read",
        }
        self.registry = {
            "schema_version": 1, "repository": "example/source",
            "revision": "a" * 40, "sources": [self.source],
        }

    def seed(self, data=None):
        target = self.cache / self.registry["revision"] / "S01.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(self.data if data is None else data)
        return target

    def test_blob_hash_uses_git_format(self):
        # The SHA of the empty Git blob is a stable external fixture.
        self.assertEqual(lookup.git_blob_sha(b""), "e69de29bb2d1d6434b8b29ae775ad8c2e48c5391")

    def test_download_uses_pinned_revision_and_handles_unicode_path(self):
        with patch.object(lookup, "fetch_bytes", return_value=self.data) as fetch:
            text, origin = lookup.get_text(self.registry, self.source, self.cache)
        self.assertEqual(text, self.data.decode("utf-8"))
        self.assertEqual(origin, "download")
        self.assertEqual(fetch.call_args.args[0],
                         "https://raw.githubusercontent.com/example/source/" + "a" * 40 + "/docs/src/001-%E4%BE%8B%E6%96%87.md")
        with patch.object(lookup, "fetch_bytes", side_effect=AssertionError("unexpected network")):
            self.assertEqual(lookup.get_text(self.registry, self.source, self.cache, offline=True)[1], "cache")

    def test_missing_offline_cache_never_uses_network(self):
        with patch.object(lookup, "fetch_bytes", side_effect=AssertionError("unexpected network")):
            with self.assertRaises(lookup.SourceError):
                lookup.get_text(self.registry, self.source, self.cache, offline=True)

    def test_corrupt_offline_cache_is_rejected(self):
        self.seed(b"changed")
        with patch.object(lookup, "fetch_bytes", side_effect=AssertionError("unexpected network")):
            with self.assertRaises(lookup.SourceError):
                lookup.get_text(self.registry, self.source, self.cache, offline=True)

    def test_online_lookup_repairs_corrupt_cache_once(self):
        target = self.seed(b"changed")
        with patch.object(lookup, "fetch_bytes", return_value=self.data) as fetch:
            self.assertEqual(lookup.get_text(self.registry, self.source, self.cache)[1], "download")
        fetch.assert_called_once()
        self.assertEqual(target.read_bytes(), self.data)

    def test_unverified_download_is_not_cached(self):
        with patch.object(lookup, "fetch_bytes", return_value=b"server error"):
            with self.assertRaises(lookup.SourceError):
                lookup.get_text(self.registry, self.source, self.cache)
        self.assertFalse((self.cache / self.registry["revision"] / "S01.md").exists())

    def test_passage_includes_context_and_matching_line(self):
        result = lookup.find_passages(self.data.decode("utf-8"), "核验短引")
        self.assertEqual(result["matching_lines"], 1)
        self.assertEqual(result["passages"][0]["match_line"], 5)
        self.assertIn("前文说明", result["passages"][0]["text"])
        self.assertIn("后文说明", result["passages"][0]["text"])

    def test_long_passages_retain_match_and_disclose_truncation(self):
        body = ("前" * 1500 + "目标短语" + "后" * 1500 + "\n") * 5
        result = lookup.find_passages(body, "目标短语")
        self.assertEqual(result["matching_lines"], 5)
        self.assertEqual(len(result["passages"]), 3)
        self.assertEqual(result["omitted_matching_lines"], 2)
        for passage in result["passages"]:
            self.assertIn("目标短语", passage["text"])
            self.assertTrue(passage["truncated"])
            self.assertLessEqual(len(passage["text"]), 906)

    def test_each_excerpt_focuses_on_its_own_matching_line(self):
        body = "第一处：目标" + "甲" * 1000 + "\n第二处：目标" + "乙" * 1000
        result = lookup.find_passages(body, "目标", max_chars=50)
        self.assertIn("第二处：目标", result["passages"][1]["text"])
        self.assertEqual(result["passages"][1]["match_line"], 2)

    def test_no_match_is_explicit(self):
        result = lookup.find_passages(self.data.decode("utf-8"), "不存在")
        self.assertEqual(result["matching_lines"], 0)
        self.assertEqual(result["passages"], [])

    def test_empty_query_is_rejected(self):
        with self.assertRaises(lookup.SourceError):
            lookup.find_passages("some text", " ")

    def test_valid_blob_with_wrong_quote_fails_verification(self):
        self.seed()
        self.source["quote"] = "并不存在的引文"
        result = lookup.verify_sources(self.registry, self.cache, offline=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verified"], 0)
        self.assertFalse(result["results"][0]["ok"])

    def test_verification_reports_success_and_failure_separately(self):
        self.seed()
        self.registry["sources"].append(dict(self.source, id="S02"))
        result = lookup.verify_sources(self.registry, self.cache, offline=True)
        self.assertFalse(result["ok"])
        self.assertEqual(result["total"], 2)
        self.assertEqual(result["verified"], 1)

    def test_network_failure_is_reported_without_fabricating_a_pass(self):
        with patch.object(lookup, "fetch_bytes", side_effect=URLError("unavailable")) as fetch:
            result = lookup.verify_sources(self.registry, self.cache)
        self.assertFalse(result["ok"])
        self.assertEqual(result["verified"], 0)
        fetch.assert_called_once()

    def test_cli_missing_match_returns_nonzero_and_json(self):
        self.seed()
        output = io.StringIO()
        with patch.object(lookup, "load_registry", return_value=self.registry), \
             patch.object(lookup, "cache_directory", return_value=self.cache), \
             contextlib.redirect_stdout(output):
            code = lookup.main(["show", "S01", "--find", "不存在", "--offline"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["found"])

    def test_cli_unknown_source_has_clear_error(self):
        output = io.StringIO()
        with patch.object(lookup, "load_registry", return_value=self.registry), \
             patch.object(lookup, "cache_directory", return_value=self.cache), \
             contextlib.redirect_stderr(output):
            code = lookup.main(["show", "S99", "--offline"])
        self.assertEqual(code, 1)
        self.assertFalse(json.loads(output.getvalue())["ok"])

    def test_cache_inside_repository_is_rejected(self):
        with patch.dict(os.environ, {"MAO_THOUGHT_CACHE": str(ROOT / "source-cache")}):
            with self.assertRaises(lookup.SourceError):
                lookup.cache_directory()


if __name__ == "__main__":
    unittest.main()
