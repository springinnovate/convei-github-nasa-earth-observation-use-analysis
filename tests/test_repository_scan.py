"""Test repository ZIP scans, identifier boundaries, and file exclusions offline."""

from pathlib import Path
import random
import re
import stat
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
import warnings
import zipfile

from nasa_eo_search.repository_scan import (
    build_literal_term_matcher, find_literal_terms_in_line, scan_repository_zip,
)


def save_repository_zip(directory: Path, files: list[tuple[str | zipfile.ZipInfo, str | bytes]]) -> Path:
    """Save a repository ZIP fixture containing the supplied files.

    Args:
        directory: Existing temporary directory used only by the current test.
        files: ZIP entry names or metadata paired with text or byte content.

    Returns:
        The path to ``snapshot.zip`` in the temporary directory.

    Raises:
        OSError: If the temporary ZIP cannot be written.
    """
    zip_path = directory / "snapshot.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore", UserWarning)
            for name, content in files:
                if isinstance(name, str):
                    member = zipfile.ZipInfo(name)
                    # Preserve deliberately hostile names despite Windows'
                    # constructor normalization of backslashes.
                    member.filename = name
                    member.compress_type = zipfile.ZIP_DEFLATED
                    name = member
                archive.writestr(name, content)
    return zip_path


class LiteralTermMatcherTests(unittest.TestCase):
    """Check overlapping multi-pattern searches and exact identifier boundaries."""

    def test_boundary_matching_excludes_substrings_but_accepts_underscores(self) -> None:
        """Match identifiers in filenames without accepting MATLAB-like collisions."""
        matcher = build_literal_term_matcher(("ATL03", "ATL03_007", "earthdata.nasa.gov"))
        line = 'MATL03 ATL030 _atl03_007.h5 https://earthdata.nasa.gov/path'
        self.assertEqual(find_literal_terms_in_line(line, matcher), {"ATL03", "ATL03_007", "earthdata.nasa.gov"})
        self.assertEqual(find_literal_terms_in_line("MATL03 ATL030", matcher), set())

    def test_suffix_and_overlapping_terms_are_all_returned(self) -> None:
        """Preserve shorter valid suffix and prefix terms without duplicate hits."""
        terms = ("ATL03", "ATL03_007", "007", "data", "data.gov", "gov", "ATL03")
        matcher = build_literal_term_matcher(terms)
        self.assertEqual(find_literal_terms_in_line("ATL03_007 data.gov data.gov", matcher), set(terms))

    def test_ascii_case_normalization_preserves_unicode_line_offsets(self) -> None:
        """Avoid Unicode lowercasing expansions that could shift match boundaries."""
        matcher = build_literal_term_matcher(("ATL03", "冰"))
        self.assertEqual(find_literal_terms_in_line("İ ATL03 冰", matcher), {"ATL03", "冰"})
        self.assertEqual(find_literal_terms_in_line("İ MATL03", matcher), set())

    def test_automaton_agrees_with_independent_literal_regexes(self) -> None:
        """Compare seeded mixed-prefix samples with simple per-term boundaries."""
        generator = random.Random(42)
        alphabet = "ABabc012_./- "
        terms = tuple({"".join(generator.choice(alphabet[:-1]) for _ in range(generator.randint(1, 8))) for _ in range(100)})
        matcher = build_literal_term_matcher(terms)
        for _ in range(50):
            line = "".join(generator.choice(alphabet) for _ in range(100)) + " " + generator.choice(terms)
            expected = {
                term for term in terms
                if re.search(r"(?<![A-Za-z0-9])" + re.escape(term) + r"(?![A-Za-z0-9])", line, re.IGNORECASE | re.ASCII)
            }
            self.assertEqual(find_literal_terms_in_line(line, matcher), expected)

    def test_large_catalog_is_compiled_once_and_searches_all_terms_together(self) -> None:
        """Exercise a catalog-sized matcher without timing-sensitive assertions."""
        terms = tuple(f"PRODUCT_{number:05d}" for number in range(33_273))
        matcher = build_literal_term_matcher(terms)
        self.assertIs(build_literal_term_matcher(terms), matcher)
        self.assertEqual(find_literal_terms_in_line("PRODUCT_00000 and product_33272", matcher), {"PRODUCT_00000", "PRODUCT_33272"})


class RepositoryZipScanTests(unittest.TestCase):
    """Check scan matches and exclusion reasons without extracting source files."""

    def test_text_matches_preserve_files_terms_lines_and_unbounded_hit_count(self) -> None:
        """Report every matching line with its original spelling and exact location."""
        lines = ["# Intro", "MATL03", "ATL03_007 = 'atl03'", "read('ATL03')"] + ["ATL03"] * 150
        content = "\r\n".join(lines)
        with TemporaryDirectory() as temporary:
            directory = Path(temporary)
            archive = save_repository_zip(directory, [("project-commit/code/run.py", content), ("project-commit/README.md", "Other")])
            result = scan_repository_zip(archive, ["ATL03", "ATL03_007"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["files_scanned"], 2)
            self.assertEqual(result["archive_root"], "project-commit")
            self.assertEqual(result["bytes_scanned"], len(content.encode()) + len(b"Other"))
            matches = {match["term"]: match for match in result["matches"]}
            self.assertEqual(matches["ATL03"]["file"], "code/run.py")
            self.assertEqual(len(matches["ATL03"]["matching_lines"]), 152)
            self.assertEqual(matches["ATL03"]["matching_lines"][0], {"line_number": 3, "text": lines[2]})
            self.assertEqual(len(matches["ATL03_007"]["matching_lines"]), 1)
            self.assertEqual([path.name for path in directory.iterdir()], ["snapshot.zip"])

    def test_single_flat_file_keeps_its_name(self) -> None:
        """Do not mistake an archive's only file for an enclosing directory."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [("README.md", "ATL03")])
            result = scan_repository_zip(archive, ["ATL03"])
            self.assertEqual(result["archive_root"], "")
            self.assertEqual(result["matches"][0]["file"], "README.md")

    def test_unsafe_paths_and_links_are_never_opened(self) -> None:
        """Reject traversal, drives, absolute paths, symlinks, and special files."""
        symbolic_link = zipfile.ZipInfo("root/link.py")
        symbolic_link.create_system = 3
        symbolic_link.external_attr = (stat.S_IFLNK | 0o777) << 16
        device_file = zipfile.ZipInfo("root/device")
        device_file.create_system = 3
        device_file.external_attr = (stat.S_IFCHR | 0o600) << 16
        unsafe_names = ["../escape.py", "/absolute.py", "C:/drive.py", "root/../escape.py", "root/./dot.py", "root\\backslash.py", "root/C:drive.py", "root//empty.py", "root/test:name.py", "root/control\nname.py"]
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [(name, "ATL03") for name in unsafe_names] + [(symbolic_link, "ATL03"), (device_file, "ATL03")])
            with patch("nasa_eo_search.repository_scan.zipfile.ZipFile.open", side_effect=AssertionError("Unsafe member read")):
                result = scan_repository_zip(archive, ["ATL03"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["matches"], [])
            self.assertEqual(len(result["skipped"]), 12)
            self.assertEqual(result["bytes_read"], 0)

    def test_binary_non_utf8_and_lfs_files_are_explicit_gaps(self) -> None:
        """List binary, non-UTF-8, LFS, and submodule exclusions as incomplete."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [
                ("root/binary.bin", b"\x00ATL03"), ("root/latin.txt", b"\xffATL03"),
                ("root/data.h5", "version https://git-lfs.github.com/spec/v1\noid sha256:abc\nsize 100\n"),
                ("root/.gitmodules", '[submodule "nested"]\n path = nested\n url = https://example.com/ATL03\n'),
            ])
            result = scan_repository_zip(archive, ["ATL03"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual({item["reason"] for item in result["skipped"]}, {"binary_file", "non_utf8_file", "git_lfs_content_not_in_snapshot", "submodule_contents_not_in_snapshot"})
            self.assertEqual(result["files_scanned"], 1)
            self.assertEqual(result["matches"][0]["file"], ".gitmodules")

    def test_size_limits_leave_small_later_files_searchable(self) -> None:
        """Skip files over the byte limits and still scan later files that fit."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [("root/large.py", "ATL03 " * 20), ("root/small.py", "ATL03"), ("root/last.py", "ATL03")])
            result = scan_repository_zip(archive, ["ATL03"], max_file_bytes=10, max_uncompressed_bytes=8)
            self.assertEqual(result["bytes_read"], 5)
            self.assertEqual(result["files_scanned"], 1)
            self.assertEqual(result["matches"][0]["file"], "small.py")
            self.assertEqual([item["reason"] for item in result["skipped"]], ["file_size_limit", "archive_uncompressed_size_limit"])

    def test_file_count_limit_and_duplicate_paths_are_visible(self) -> None:
        """Count duplicate ZIP names and report when the file limit is reached."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [("root/a.py", "ATL03"), ("root/a.py", "ATL03"), ("root/b.py", "ATL03")])
            result = scan_repository_zip(archive, ["ATL03"], max_files=2)
            self.assertEqual(result["files_scanned"], 1)
            self.assertEqual(result["files_considered"], 3)
            self.assertEqual([item["reason"] for item in result["skipped"]], ["duplicate_archive_path", "file_count_limit"])
            self.assertEqual(result["matches"], [])

    def test_duplicate_paths_remove_ambiguous_hits_without_losing_other_files(self) -> None:
        """Avoid choosing one duplicate ZIP entry as a real repository file."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [("root/a.py", "ATL03"), ("root/a.py", "other"), ("root/b.py", "ATL03")])
            result = scan_repository_zip(archive, ["ATL03"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual([match["file"] for match in result["matches"]], ["b.py"])
            self.assertEqual(result["skipped"], [{"file": "a.py", "reason": "duplicate_archive_path"}])

    def test_corrupt_member_reads_consume_the_attempted_decompression_budget(self) -> None:
        """Do not let repeated failed reads bypass the archive byte ceiling."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [("root/a.py", "ATL03"), ("root/b.py", "ATL03")])
            with patch("nasa_eo_search.repository_scan.zipfile.ZipFile.open", side_effect=zipfile.BadZipFile("Bad CRC")) as open_member:
                result = scan_repository_zip(archive, ["ATL03"], max_uncompressed_bytes=5)
            self.assertEqual(open_member.call_count, 1)
            self.assertEqual(result["uncompressed_bytes_budget_used"], 5)
            self.assertEqual([item["reason"] for item in result["skipped"]], ["archive_member_read_error", "archive_uncompressed_size_limit"])

    def test_invalid_archive_returns_explicit_incomplete_result(self) -> None:
        """Keep invalid downloads distinguishable from successful zero-hit scans."""
        with TemporaryDirectory() as temporary:
            archive = Path(temporary) / "snapshot.zip"
            archive.write_bytes(b"not a zip")
            result = scan_repository_zip(archive, ["ATL03"])
            self.assertEqual(result["status"], "incomplete")
            self.assertEqual(result["skipped"][0]["reason"], "invalid_archive")

    def test_utf8_bom_is_supported(self) -> None:
        """Search UTF-8 text with a byte-order mark and preserve line numbering."""
        with TemporaryDirectory() as temporary:
            archive = save_repository_zip(Path(temporary), [("root/readme.txt", "ATL03\n冰".encode("utf-8-sig"))])
            result = scan_repository_zip(archive, ["ATL03"])
            self.assertEqual(result["status"], "complete")
            self.assertEqual(result["matches"][0]["matching_lines"], [{"line_number": 1, "text": "ATL03"}])

    def test_invalid_terms_and_limits_fail_before_opening_archive(self) -> None:
        """Reject invalid terms and limits before trying to open the ZIP."""
        for terms in ([], [""], ["ATL03\n"], ["ATL03\x00"], [None], "ATL03"):
            with self.subTest(terms=terms), self.assertRaises(ValueError):
                scan_repository_zip("nonexistent.zip", terms)
        for options in ({"max_files": 0}, {"max_files": True}, {"max_file_bytes": -1}, {"max_uncompressed_bytes": 1.5}):
            with self.subTest(options=options), self.assertRaises(ValueError):
                scan_repository_zip("nonexistent.zip", ["ATL03"], **options)


if __name__ == "__main__":
    unittest.main()
