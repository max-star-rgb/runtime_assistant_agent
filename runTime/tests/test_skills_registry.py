import unittest

from openclaw_gateway_runtime.skills import SkillsRegistry


class SkillsRegistryTests(unittest.TestCase):
    def test_discovers_repo_skills(self) -> None:
        reg = SkillsRegistry.from_env()
        reg.refresh()
        ids = [s.id for s in reg.list()]
        for name in (
            "example",
            "workspace-nav",
            "datetime",
            "json-tools",
        ):
            self.assertIn(name, ids, msg=f"missing skill {name!r}, got {sorted(ids)}")


if __name__ == "__main__":
    unittest.main()

