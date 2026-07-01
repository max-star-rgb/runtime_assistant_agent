"""Unit tests for the deterministic SKILL.md execution engine."""

import os
import unittest

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

from openclaw_gateway_runtime.skills.engine import (
    StepDef,
    StepResult,
    _extract_steps_yaml,
    parse_steps,
    resolve_vars,
    resolve_args,
    _deep_get,
)


class ExtractStepsYamlTests(unittest.TestCase):
    def test_extracts_yaml_from_steps_section(self) -> None:
        body = """\
# My Skill

Some description.

## Steps
```yaml
- id: search
  action: exec
  command: "echo hello"
```

## Other Section
More text.
"""
        raw = _extract_steps_yaml(body)
        self.assertIsNotNone(raw)
        self.assertIn("id: search", raw)
        self.assertIn("echo hello", raw)

    def test_returns_none_when_no_steps(self) -> None:
        body = "# My Skill\n\nJust a description.\n"
        self.assertIsNone(_extract_steps_yaml(body))

    def test_returns_none_when_empty_yaml(self) -> None:
        body = "## Steps\n```yaml\n```\n"
        self.assertIsNone(_extract_steps_yaml(body))


class ParseStepsTests(unittest.TestCase):
    def test_parses_exec_step(self) -> None:
        body = """\
## Steps
```yaml
- id: search
  action: exec
  command: "python search.py --keyword={keyword}"
  params: [keyword, source]
  defaults: {source: "0"}
  timeout: 30
```
"""
        steps = parse_steps(body)
        self.assertIsNotNone(steps)
        self.assertEqual(len(steps), 1)
        s = steps[0]
        self.assertEqual(s.id, "search")
        self.assertEqual(s.action, "exec")
        self.assertIn("{keyword}", s.command)
        self.assertEqual(s.params, ["keyword", "source"])
        self.assertEqual(s.defaults, {"source": "0"})
        self.assertEqual(s.timeout, 30)

    def test_parses_browser_step(self) -> None:
        body = """\
## Steps
```yaml
- id: nav
  action: browser
  browser_action: navigate
  args: {url: "https://example.com?q={keyword}"}
```
"""
        steps = parse_steps(body)
        self.assertIsNotNone(steps)
        self.assertEqual(steps[0].action, "browser")
        self.assertEqual(steps[0].browser_action, "navigate")
        self.assertIn("{keyword}", steps[0].args["url"])

    def test_parses_error_handler(self) -> None:
        body = """\
## Steps
```yaml
- id: click
  action: browser
  browser_action: click
  args: {selector: "#btn"}

- id: retry
  on_error: click
  action: browser
  browser_action: wait
  args: {timeout_s: 30}
  max_retries: 2
```
"""
        steps = parse_steps(body)
        self.assertEqual(len(steps), 2)
        self.assertEqual(steps[1].on_error, "click")
        self.assertEqual(steps[1].max_retries, 2)

    def test_returns_none_for_no_steps(self) -> None:
        self.assertIsNone(parse_steps("# No steps here\n"))

    def test_returns_none_for_invalid_yaml(self) -> None:
        body = "## Steps\n```yaml\nnot a list\n```\n"
        self.assertIsNone(parse_steps(body))


class DeepGetTests(unittest.TestCase):
    def test_dict_access(self) -> None:
        self.assertEqual(_deep_get({"a": "b"}, "a"), "b")

    def test_nested_dict(self) -> None:
        self.assertEqual(_deep_get({"a": {"b": "c"}}, "a.b"), "c")

    def test_list_index(self) -> None:
        self.assertEqual(_deep_get([10, 20, 30], "[1]"), 20)

    def test_mixed_path(self) -> None:
        data = {"result": [{"url": "http://x"}, {"url": "http://y"}]}
        self.assertEqual(_deep_get(data, "result[0].url"), "http://x")

    def test_missing_key(self) -> None:
        self.assertIsNone(_deep_get({"a": 1}, "b"))


class ResolveVarsTests(unittest.TestCase):
    def test_param_substitution(self) -> None:
        result = resolve_vars(
            "search --keyword={keyword} --source={source}",
            {"keyword": "phone"},
            {},
            {"source": "0"},
        )
        self.assertEqual(result, "search --keyword=phone --source=0")

    def test_step_ref_substitution(self) -> None:
        ctx = {
            "extract": StepResult("extract", True, output=[{"url": "http://a"}, {"url": "http://b"}]),
        }
        result = resolve_vars("${extract.output[0].url}", {}, ctx)
        self.assertEqual(result, "http://a")

    def test_unresolved_param_kept(self) -> None:
        result = resolve_vars("{unknown}", {}, {})
        self.assertEqual(result, "{unknown}")

    def test_default_used_when_param_missing(self) -> None:
        result = resolve_vars("{source}", {}, {}, {"source": "0"})
        self.assertEqual(result, "0")


class ResolveArgsTests(unittest.TestCase):
    def test_resolves_string_values(self) -> None:
        args = {"url": "https://x.com?q={keyword}", "timeout": 30}
        out = resolve_args(args, {"keyword": "test"}, {})
        self.assertEqual(out["url"], "https://x.com?q=test")
        self.assertEqual(out["timeout"], 30)  # non-string preserved


class ParseRealSkillTests(unittest.TestCase):
    """Test parsing the actual SKILL.md files in the repo."""

    def _read_skill_body(self, skill_dir: str) -> str:
        import pathlib
        skill_path = pathlib.Path(__file__).parent.parent / "skills" / skill_dir / "SKILL.md"
        if not skill_path.exists():
            self.skipTest(f"{skill_path} not found")
        text = skill_path.read_text(encoding="utf-8")
        # Strip frontmatter
        if text.startswith("---"):
            end = text.index("---", 3)
            return text[end + 3:].strip()
        return text

    def test_taobao_has_steps(self) -> None:
        body = self._read_skill_body("taobao")
        steps = parse_steps(body)
        self.assertIsNotNone(steps)
        self.assertTrue(any(s.id == "search" for s in steps))

    def test_taobao_has_search_and_cart_groups(self) -> None:
        body = self._read_skill_body("taobao")
        steps = parse_steps(body)
        self.assertIsNotNone(steps)
        groups = {s.group for s in steps if s.group}
        self.assertIn("search", groups)
        self.assertIn("add_to_cart", groups)
        self.assertTrue(any(s.action == "browser" and s.group == "add_to_cart" for s in steps))

    def test_datetime_has_no_steps(self) -> None:
        body = self._read_skill_body("datetime")
        steps = parse_steps(body)
        self.assertIsNone(steps)


if __name__ == "__main__":
    unittest.main()
