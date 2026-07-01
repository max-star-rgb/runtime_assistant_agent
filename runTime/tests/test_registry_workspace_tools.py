import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from openclaw_gateway_runtime.skills import SkillsRegistry


class RegistryWorkspaceToolsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.reg = SkillsRegistry.from_env()
        self.reg.refresh()
        self.repo_root = Path(__file__).resolve().parents[1]

    def test_list_directory_skills_folder(self) -> None:
        skills_dir = self.repo_root / "skills"
        text = self.reg.list_directory(str(skills_dir), max_entries=50)
        self.assertIn("dir", text)
        self.assertIn("example", text)

    def test_grep_finds_example_skill(self) -> None:
        p = self.repo_root / "skills" / "example"
        out = self.reg.grep_workspace(r"name:\s*example", str(p), max_matches=20)
        self.assertNotEqual(out, "no matches")

    def test_extra_workspace_dirs_allows_outside_repo(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            extra = Path(td)
            (extra / "note.txt").write_text("x", encoding="utf-8")
            old = os.environ.get("EXTRA_WORKSPACE_DIRS")
            os.environ["EXTRA_WORKSPACE_DIRS"] = str(extra)
            try:
                reg = SkillsRegistry.from_env()
                text = reg.list_directory(str(extra))
                self.assertIn("note.txt", text)
            finally:
                if old is None:
                    os.environ.pop("EXTRA_WORKSPACE_DIRS", None)
                else:
                    os.environ["EXTRA_WORKSPACE_DIRS"] = old

    def test_path_outside_repo_rejected_without_extra(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            p = Path(td)
            reg = SkillsRegistry.from_env()
            with self.assertRaises(ValueError) as ctx:
                reg.list_directory(str(p))
            self.assertIn("EXTRA_WORKSPACE_DIRS", str(ctx.exception))

    @patch("urllib.request.urlopen")
    def test_http_get_text(self, m_urlopen: MagicMock) -> None:
        mock_resp = MagicMock()
        mock_resp.read.return_value = b'{"ok":true}'
        mock_resp.__enter__.return_value = mock_resp
        mock_resp.__exit__.return_value = None
        m_urlopen.return_value = mock_resp

        txt = self.reg.http_get_text("https://example.com/api", max_bytes=1000, timeout_s=5)
        self.assertIn("ok", txt)


if __name__ == "__main__":
    unittest.main()
