"""Check that new code context remains bounded, pinned, and separately cited."""

import copy
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from nasa_eo_search.classification_context import (
    add_enclosing_classification_context, read_repository_classification_sources,
)
from nasa_eo_search.file_classify import build_file_classification_plan, identify_file_classification_source
from nasa_eo_search.snapshot_evidence import build_snapshot_excerpt_citation, read_snapshot_evidence_groups
from test_snapshot_evidence import create_snapshot_inputs


class ClassificationContextTests(unittest.TestCase):
    """Verify context quality without changing search rows or historical inputs."""

    def test_class_attribute_consumer_is_cited_without_changing_search_rows(self) -> None:
        """Show the method using an identifier table even beyond nearby lines."""
        source_text = ('class PhotonReader:\n    collections = {\n        "ATL03",\n'
                       + ''.join(f'        "OTHER_{number}",\n' for number in range(30))
                       + '    }\n\n    def accepts(self, identifier):\n'
                         '        return identifier in self.collections\n')
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary), source_text)
            source_plan = read_snapshot_evidence_groups(directory)
            original_plan = copy.deepcopy(source_plan)
            original_identity = identify_file_classification_source(source_plan)
            with redirect_stderr(StringIO()):
                plan = build_file_classification_plan(source_plan, True, 10, 24000, None)
            group = plan["groups"][0]
            added_context = [item for item in group["evidence"] if item["kind"] == "enclosing_code"]
            self.assertTrue(any("return identifier in self.collections" in item["text"] for item in added_context))
            self.assertTrue(any("return identifier in self.collections" in block["text"]
                                for packet in group["packets"] for block in packet["evidence"]))
            for citation in added_context:
                self.assertEqual(citation["commit"], "a" * 40)
                self.assertEqual(citation["file"], "src/read.py")
                self.assertEqual(source_text.splitlines()[citation["line_number"] - 1], citation["text"])
            self.assertEqual(group["match_row"], source_plan["groups"][0]["files"][0]["snapshot_match_row"])
            self.assertEqual(source_plan, original_plan)
            self.assertEqual(plan["source_evidence_id"], original_identity)

    def test_changed_zip_between_search_read_and_preparation_is_rejected(self) -> None:
        """Recheck archive bytes before adding source beyond original excerpts."""
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary))
            source_plan = read_snapshot_evidence_groups(directory)
            with (directory / "snapshots" / "snapshot.zip").open("ab") as archive:
                archive.write(b"changed")
            with self.assertRaisesRegex(ValueError, "hash differs"):
                read_repository_classification_sources(source_plan, "github.com/example/project")

    def test_mismatching_source_cannot_supply_structural_context(self) -> None:
        """Leave the original evidence intact if fetched source disagrees."""
        group = {"repository": "github.com/example/project", "files": [{
            "file": "reader.py", "commit": "a" * 40,
            "matching_lines": [{"line_number": 2, "text": '    load("ATL03")'}],
        }], "evidence": [], "evidence_gaps": []}
        add_enclosing_classification_context(group, ["def unrelated():", "    pass"])
        self.assertEqual(group["evidence"], [])
        self.assertIn("differ", group["evidence_gaps"][0])

    def test_context_budget_cannot_expand_a_long_source_line(self) -> None:
        """Keep long-line windows bounded even when the selector sees full text."""
        line_text = 'TOKEN = "' + 'x' * 900_000 + ' ATL03 ' + 'x' * 9000 + '"'
        with TemporaryDirectory() as temporary:
            directory, _ = create_snapshot_inputs(Path(temporary), line_text)
            source_plan = read_snapshot_evidence_groups(directory)
            with redirect_stderr(StringIO()):
                plan = build_file_classification_plan(source_plan, True, 10, 24000, None)
            group = plan["groups"][0]
            self.assertLessEqual(sum(len(item["text"]) for item in group["evidence"]), 12000)
            self.assertGreater(group["evidence"][0]["character_offset"], 899_000)
            self.assertLess(len(group["packets"][0]["evidence"][0]["text"]), 1000)

    def test_existing_budget_is_preserved_and_new_context_omissions_are_reported(self) -> None:
        """Do not discard original citations to fit extra enclosing code."""
        source_lines = ["def load_product():", '    read("ATL03")'] + ["    pass"] * 40
        original = build_snapshot_excerpt_citation(
            "github.com/example/project", "a" * 40, "reader.py", 2, 0,
            source_lines[1], "matching_line")
        filler = {**original, "line_number": 3, "text": "x" * (12000 - len(original["text"]))}
        group = {"repository": "github.com/example/project", "files": [{
            "file": "reader.py", "commit": "a" * 40,
            "matching_lines": [{"line_number": 2, "text": source_lines[1]}],
        }], "evidence": [original, filler], "evidence_gaps": []}
        original_evidence = copy.deepcopy(group["evidence"])
        add_enclosing_classification_context(group, source_lines)
        self.assertEqual(group["evidence"], original_evidence)
        self.assertTrue(any("truncated" in note for note in group["evidence_gaps"]))


if __name__ == "__main__":
    unittest.main()
