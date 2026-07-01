import os
import tempfile
import unittest
from pathlib import Path

from openclaw_gateway_runtime.skills import SkillsRegistry


class WriteEditToolTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = SkillsRegistry.from_env()
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_write_new_file(self) -> None:
        p = self.repo_root / "tests" / "_tmp_write_test.txt"
        try:
            msg = self.reg.write_file(str(p), "hello\nworld\n")
            self.assertIn("wrote", msg)
            self.assertEqual(p.read_text(encoding="utf-8"), "hello\nworld\n")
        finally:
            p.unlink(missing_ok=True)

    def test_write_rejects_outside_root(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            with self.assertRaises(ValueError):
                self.reg.write_file(os.path.join(td, "x.txt"), "data")

    def test_edit_replaces_unique(self) -> None:
        p = self.repo_root / "tests" / "_tmp_edit_test.txt"
        try:
            p.write_text("aaa bbb ccc", encoding="utf-8")
            msg = self.reg.edit_file(str(p), "bbb", "XXX")
            self.assertIn("edited", msg)
            self.assertEqual(p.read_text(encoding="utf-8"), "aaa XXX ccc")
        finally:
            p.unlink(missing_ok=True)

    def test_edit_rejects_ambiguous(self) -> None:
        p = self.repo_root / "tests" / "_tmp_edit_ambig.txt"
        try:
            p.write_text("ab ab ab", encoding="utf-8")
            with self.assertRaises(ValueError) as ctx:
                self.reg.edit_file(str(p), "ab", "XX")
            self.assertIn("ambiguous", str(ctx.exception))
        finally:
            p.unlink(missing_ok=True)

    def test_edit_rejects_not_found(self) -> None:
        p = self.repo_root / "tests" / "_tmp_edit_nf.txt"
        try:
            p.write_text("hello", encoding="utf-8")
            with self.assertRaises(ValueError):
                self.reg.edit_file(str(p), "NONEXISTENT", "X")
        finally:
            p.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
