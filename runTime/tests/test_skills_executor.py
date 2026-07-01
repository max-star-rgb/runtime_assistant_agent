import unittest

from openclaw_gateway_runtime.skills import SkillsRegistry
from openclaw_gateway_runtime.skills.executor import SkillsExecutor


class SkillsExecutorTests(unittest.TestCase):
    def test_runs_bundled_python_script(self) -> None:
        reg = SkillsRegistry.from_env()
        reg.refresh()
        skill = reg.get("example")
        self.assertIsNotNone(skill)
        assert skill is not None

        exe = SkillsExecutor()
        res = exe.run_script(skill=skill, script="hello.py", args=["--name", "Alice"], timeout_s=10)
        self.assertEqual(res.exit_code, 0)
        self.assertIn("hello Alice", res.stdout)


if __name__ == "__main__":
    unittest.main()

