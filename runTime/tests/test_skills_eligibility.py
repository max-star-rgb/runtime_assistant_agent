import os
import tempfile
import textwrap
import unittest
from pathlib import Path

from openclaw_gateway_runtime.skills import SkillsRegistry


class SkillsEligibilityTests(unittest.TestCase):
    def test_disable_model_invocation_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sk = root / "hidden"
            sk.mkdir()
            (sk / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: hidden
                    description: should not show
                    disable-model-invocation: true
                    ---
                    # Hidden
                    """
                ),
                encoding="utf-8",
            )

            os.environ["SKILLS_PATHS"] = str(root)
            reg = SkillsRegistry.from_env()
            reg.refresh()
            xml = reg.format_available_skills_xml()
            self.assertNotIn("<name>hidden</name>", xml)

    def test_requires_missing_bin_excluded(self) -> None:
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            sk = root / "needsbin"
            sk.mkdir()
            (sk / "SKILL.md").write_text(
                textwrap.dedent(
                    """\
                    ---
                    name: needsbin
                    description: requires nonexistent bin
                    metadata:
                      { "openclaw": { "requires": { "bins": ["__definitely_not_a_real_bin__"] } } }
                    ---
                    # NeedsBin
                    """
                ),
                encoding="utf-8",
            )

            os.environ["SKILLS_PATHS"] = str(root)
            reg = SkillsRegistry.from_env()
            reg.refresh()
            xml = reg.format_available_skills_xml()
            self.assertNotIn("<name>needsbin</name>", xml)


if __name__ == "__main__":
    unittest.main()

