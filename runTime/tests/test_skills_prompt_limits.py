import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from openclaw_gateway_runtime.skills import SkillsRegistry


def _make_skill(root: Path, name: str, desc: str) -> None:
    d = root / name
    d.mkdir()
    (d / "SKILL.md").write_text(
        textwrap.dedent(
            f"""\
            ---
            name: {name}
            description: {desc}
            ---
            # {name}
            """
        ),
        encoding="utf-8",
    )


class SkillsPromptLimitsTests(unittest.TestCase):
    def test_compact_mode_kicks_in_under_budget(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(30):
                _make_skill(root, f"s{i}", "A" * 400)

            os.environ["SKILLS_PATHS"] = str(root)
            os.environ["SKILLS_MAX_IN_PROMPT"] = "150"
            # Budget small enough that full doesn't fit, compact does.
            os.environ["SKILLS_MAX_PROMPT_CHARS"] = "3000"

            reg = SkillsRegistry.from_env()
            reg.refresh()
            out = reg.format_available_skills_xml()
            self.assertIn("compact format", out)
            self.assertNotIn("<description>", out)

    def test_truncates_when_even_compact_too_large(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            for i in range(200):
                _make_skill(root, f"s{i}", "desc")

            os.environ["SKILLS_PATHS"] = str(root)
            os.environ["SKILLS_MAX_IN_PROMPT"] = "150"
            os.environ["SKILLS_MAX_PROMPT_CHARS"] = "800"

            reg = SkillsRegistry.from_env()
            reg.refresh()
            out = reg.format_available_skills_xml()
            self.assertIn("Skills truncated", out)
            # Should include at least one skill and omit descriptions (compact truncation).
            self.assertIn("<available_skills>", out)
            self.assertNotIn("<description>", out)


if __name__ == "__main__":
    unittest.main()

