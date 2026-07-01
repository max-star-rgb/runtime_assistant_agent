import os

os.environ.setdefault("OPENCLAW_STRUCTURED_LOG", "0")

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from openclaw_gateway_runtime.agent_runtime.anthropic_runtime import (
    AnthropicAgentRuntime,
    SessionState,
    _chunk_text,
    _find_tool_uses,
    _iter_assistant_text,
    _price_compare_tool_enabled,
    _batch_web_search_tool_enabled,
)
from openclaw_gateway_runtime.agent_runtime.anthropic_client import AnthropicConfig
from openclaw_gateway_runtime.agent_runtime.memory_client import MemoryEntry
from openclaw_gateway_runtime.runtime.openclaw_adapter import AdapterEvent, CancelToken
from openclaw_gateway_runtime.skills.executor import ScriptResult


# ---------------------------------------------------------------------------
# SSE stream helpers — produce event dicts matching AnthropicStreamBuilder
# ---------------------------------------------------------------------------

async def _make_text_stream(text: str, stop_reason: str = "end_turn"):
    """Async generator yielding SSE events for a text-only response."""
    yield {"type": "message_start", "message": {"id": "msg_test", "model": "test"}}
    yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
    yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}
    yield {"type": "content_block_stop", "index": 0}
    yield {"type": "message_delta", "delta": {"stop_reason": stop_reason}}
    yield {"type": "message_stop"}


async def _make_tool_stream(tool_uses: list[dict[str, Any]], stop_reason: str = "tool_use"):
    """Async generator yielding SSE events for a tool_use response.

    *tool_uses* is a list of ``{"id": "...", "name": "...", "input": {...}}``.
    """
    yield {"type": "message_start", "message": {"id": "msg_test"}}
    for idx, tu in enumerate(tool_uses):
        yield {
            "type": "content_block_start",
            "index": idx,
            "content_block": {"type": "tool_use", "id": tu["id"], "name": tu["name"]},
        }
        yield {
            "type": "content_block_delta",
            "index": idx,
            "delta": {"type": "input_json_delta", "partial_json": json.dumps(tu["input"])},
        }
        yield {"type": "content_block_stop", "index": idx}
    yield {"type": "message_delta", "delta": {"stop_reason": stop_reason}}
    yield {"type": "message_stop"}


# ---------------------------------------------------------------------------
# Shared setUp helpers
# ---------------------------------------------------------------------------

def _make_runtime():
    """Create an ``AnthropicAgentRuntime`` with fully-mocked dependencies."""
    mock_client = MagicMock()
    mock_client._cfg = MagicMock(model="test-model", max_tokens=1024)

    mock_skills = MagicMock()
    mock_skills.refresh.return_value = None
    mock_skills.list.return_value = []
    mock_skills.format_available_skills_xml.return_value = "<skills/>"

    mock_executor = MagicMock()
    runtime = AnthropicAgentRuntime(client=mock_client, skills=mock_skills, executor=mock_executor)
    return runtime, mock_client, mock_skills


async def _collect_events(aiter):
    """Drain an async iterator into a list."""
    events = []
    async for ev in aiter:
        events.append(ev)
    return events


# ===================================================================
# 1. TestHelperFunctions
# ===================================================================

class TestHelperFunctions(unittest.TestCase):
    def test_iter_assistant_text_extracts_text_blocks(self):
        content = [
            {"type": "text", "text": "Hello "},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            {"type": "text", "text": "world"},
        ]
        self.assertEqual(_iter_assistant_text(content), "Hello world")

    def test_iter_assistant_text_non_list(self):
        self.assertEqual(_iter_assistant_text(None), "")
        self.assertEqual(_iter_assistant_text("string"), "")
        self.assertEqual(_iter_assistant_text(42), "")

    def test_iter_assistant_text_empty_list(self):
        self.assertEqual(_iter_assistant_text([]), "")

    def test_find_tool_uses_extracts(self):
        content = [
            {"type": "text", "text": "hi"},
            {"type": "tool_use", "id": "t1", "name": "read", "input": {}},
            {"type": "tool_use", "id": "t2", "name": "exec", "input": {}},
        ]
        result = _find_tool_uses(content)
        self.assertEqual(len(result), 2)
        self.assertEqual(result[0]["id"], "t1")
        self.assertEqual(result[1]["id"], "t2")

    def test_find_tool_uses_no_tools(self):
        self.assertEqual(_find_tool_uses([{"type": "text", "text": "hi"}]), [])

    def test_find_tool_uses_non_list(self):
        self.assertEqual(_find_tool_uses(None), [])
        self.assertEqual(_find_tool_uses("x"), [])

    def test_truncate_short(self):
        self.assertEqual(AnthropicAgentRuntime._truncate_tool_text("hello", 100), "hello")

    def test_truncate_at_limit(self):
        self.assertEqual(AnthropicAgentRuntime._truncate_tool_text("hello", 5), "hello")

    def test_truncate_over_limit(self):
        result = AnthropicAgentRuntime._truncate_tool_text("hello world", 5)
        self.assertTrue(result.startswith("hello"))
        self.assertIn("[truncated to 5 chars]", result)

    def test_chunk_text(self):
        self.assertEqual(_chunk_text("abcdef", 2), ["ab", "cd", "ef"])

    def test_chunk_text_zero(self):
        self.assertEqual(_chunk_text("abc", 0), ["abc"])

    @patch.dict(os.environ, {"PRICE_COMPARE_SERVICE_URL": "http://example.com"})
    def test_price_compare_enabled(self):
        self.assertTrue(_price_compare_tool_enabled())

    @patch.dict(os.environ, {"PRICE_COMPARE_SERVICE_URL": ""})
    def test_price_compare_disabled(self):
        self.assertFalse(_price_compare_tool_enabled())

    @patch.dict(os.environ, {"SERPER_API_KEY": "key123"})
    def test_batch_search_enabled(self):
        self.assertTrue(_batch_web_search_tool_enabled())

    @patch.dict(os.environ, {"SERPER_API_KEY": ""})
    def test_batch_search_disabled(self):
        self.assertFalse(_batch_web_search_tool_enabled())


# ===================================================================
# 2. TestSessionState
# ===================================================================

class TestSessionState(unittest.TestCase):
    def test_defaults(self):
        s = SessionState(messages=[])
        self.assertEqual(s.messages, [])
        self.assertIsNone(s.active_skill)
        self.assertFalse(s.tool_used_this_turn)
        self.assertEqual(s.user_id, "default")
        self.assertFalse(s.memory_loaded)


# ===================================================================
# 3. TestMemoryExtraction
# ===================================================================

class TestMemoryExtraction(unittest.TestCase):
    def test_extracts_preference(self):
        entries = AnthropicAgentRuntime._extract_memory_entries("我喜欢红色的衣服")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].type, "preference")

    def test_extracts_fact_name(self):
        entries = AnthropicAgentRuntime._extract_memory_entries("我叫张三同学")
        self.assertTrue(any(e.type == "fact" for e in entries))

    def test_extracts_fact_budget(self):
        entries = AnthropicAgentRuntime._extract_memory_entries("我的预算是两千元左右")
        self.assertTrue(any(e.type == "fact" for e in entries))

    def test_extracts_event(self):
        entries = AnthropicAgentRuntime._extract_memory_entries("我刚买了一台新的笔记本电脑")
        self.assertTrue(any(e.type == "event" for e in entries))

    def test_no_match(self):
        self.assertEqual(AnthropicAgentRuntime._extract_memory_entries("Hello world"), [])

    def test_deduplicates(self):
        text = "我喜欢红色的衣服。另外说一句，我喜欢红色的衣服"
        entries = AnthropicAgentRuntime._extract_memory_entries(text)
        contents = [e.content for e in entries]
        self.assertEqual(len(contents), len(set(contents)))

    def test_importance_is_0_7(self):
        entries = AnthropicAgentRuntime._extract_memory_entries("我喜欢蓝色的汽车")
        for e in entries:
            self.assertAlmostEqual(e.importance, 0.7)


# ===================================================================
# 4. TestMessageCompression
# ===================================================================

class TestMessageCompression(unittest.TestCase):
    def _msgs(self, n: int):
        return [{"role": "user" if i % 2 == 0 else "assistant", "content": f"msg-{i}"} for i in range(n)]

    def test_no_compression_below_threshold(self):
        msgs = self._msgs(10)
        result = AnthropicAgentRuntime._compress_messages(msgs, 20)
        self.assertEqual(len(result), 10)

    def test_compresses_above_threshold(self):
        msgs = self._msgs(24)
        result = AnthropicAgentRuntime._compress_messages(msgs, 20)
        # 1 summary + 12 recent = 13
        self.assertEqual(len(result), 13)
        summary_text = result[0]["content"][0]["text"]
        self.assertIn("[Earlier conversation summary]", summary_text)

    def test_summary_includes_roles(self):
        msgs = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "hi"},
            {"role": "user", "content": "bye"},
            {"role": "assistant", "content": "ok"},
        ]
        result = AnthropicAgentRuntime._compress_messages(msgs, 2)
        summary = result[0]["content"][0]["text"]
        self.assertIn("[user]", summary)
        self.assertIn("[assistant]", summary)

    def test_handles_list_content(self):
        msgs = [
            {"role": "assistant", "content": [{"type": "text", "text": "block-text"}]},
            {"role": "user", "content": "user-text"},
            {"role": "assistant", "content": "done"},
        ]
        result = AnthropicAgentRuntime._compress_messages(msgs, 2)
        summary = result[0]["content"][0]["text"]
        self.assertIn("block-text", summary)


# ===================================================================
# 5. TestSystemPrompt
# ===================================================================

class TestSystemPrompt(unittest.TestCase):
    def setUp(self):
        self.runtime, _, self.mock_skills = _make_runtime()

    def test_contains_skills_xml(self):
        prompt = self.runtime._system_prompt()
        self.assertIn("<skills/>", prompt)

    def test_contains_tool_names(self):
        prompt = self.runtime._system_prompt()
        for name in ["read", "write", "edit", "list_dir", "grep", "web_fetch", "web_search", "exec", "browser"]:
            self.assertIn(name, prompt)

    @patch.dict(os.environ, {"PRICE_COMPARE_SERVICE_URL": "http://x"})
    def test_price_compare_in_prompt(self):
        prompt = self.runtime._system_prompt()
        self.assertIn("price_compare", prompt)

    def test_active_skill_injected(self):
        mock_skill = MagicMock()
        mock_skill.id = "my-skill"
        mock_skill.root_dir = Path("/fake/skills/my-skill")
        self.mock_skills.get.return_value = mock_skill
        self.runtime._sessions["s1"] = SessionState(messages=[], active_skill="my-skill")
        prompt = self.runtime._system_prompt(session_id="s1")
        self.assertIn("[Active skill: my-skill]", prompt)

    def test_memories_injected(self):
        memories = [{"type": "preference", "content": "likes red"}]
        prompt = self.runtime._system_prompt(memories=memories)
        self.assertIn("<user_memory>", prompt)
        self.assertIn("likes red", prompt)

    def test_no_memories_no_block(self):
        prompt = self.runtime._system_prompt(memories=[])
        self.assertNotIn("<user_memory>", prompt)


# ===================================================================
# 6. TestToolDefinitions
# ===================================================================

class TestToolDefinitions(unittest.TestCase):
    def setUp(self):
        self.runtime, _, _ = _make_runtime()

    @patch.dict(os.environ, {"PRICE_COMPARE_SERVICE_URL": "", "SERPER_API_KEY": ""})
    def test_base_tools(self):
        tools = self.runtime._tools()
        names = {t["name"] for t in tools}
        expected = {"read", "write", "edit", "list_dir", "grep", "web_fetch", "web_search", "exec", "browser", "end_turn", "run_skill"}
        self.assertEqual(names, expected)

    @patch.dict(os.environ, {"PRICE_COMPARE_SERVICE_URL": "http://x", "SERPER_API_KEY": ""})
    def test_price_compare_included(self):
        names = {t["name"] for t in self.runtime._tools()}
        self.assertIn("price_compare", names)

    @patch.dict(os.environ, {"PRICE_COMPARE_SERVICE_URL": "http://x", "SERPER_API_KEY": "key"})
    def test_both_optional_tools(self):
        names = {t["name"] for t in self.runtime._tools()}
        self.assertIn("price_compare", names)
        self.assertIn("batch_web_search", names)

    def test_schemas_have_required_fields(self):
        for t in self.runtime._tools():
            self.assertIn("name", t)
            self.assertIn("description", t)
            self.assertIn("input_schema", t)
            self.assertEqual(t["input_schema"]["type"], "object")


# ===================================================================
# 7. TestSkillLock
# ===================================================================

class TestSkillLock(unittest.TestCase):
    def setUp(self):
        self.runtime, _, self.mock_skills = _make_runtime()

    def test_detect_non_skill_md(self):
        self.assertIsNone(self.runtime._detect_skill_from_path("/some/README.md"))

    def test_detect_unknown_skill(self):
        self.mock_skills.list.return_value = []
        self.assertIsNone(self.runtime._detect_skill_from_path("/unknown/SKILL.md"))

    def test_detect_matching_skill(self):
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("test", encoding="utf-8")
            mock_skill = MagicMock()
            mock_skill.id = "my-skill"
            mock_skill.root_dir = skill_dir
            self.mock_skills.list.return_value = [mock_skill]
            result = self.runtime._detect_skill_from_path(str(skill_dir / "SKILL.md"))
            self.assertEqual(result, "my-skill")

    def test_check_lock_no_session(self):
        self.assertIsNone(self.runtime._check_skill_lock("no-sess", "A"))

    def test_check_lock_no_active(self):
        self.runtime._sessions["s1"] = SessionState(messages=[])
        self.assertIsNone(self.runtime._check_skill_lock("s1", "A"))

    def test_check_lock_same_skill(self):
        self.runtime._sessions["s1"] = SessionState(messages=[], active_skill="A")
        self.assertIsNone(self.runtime._check_skill_lock("s1", "A"))

    def test_check_lock_different_skill(self):
        self.runtime._sessions["s1"] = SessionState(messages=[], active_skill="A")
        err = self.runtime._check_skill_lock("s1", "B")
        self.assertIsNotNone(err)
        self.assertIn("Skill lock", err)
        self.assertIn("A", err)
        self.assertIn("B", err)

    def test_set_active_skill_when_none(self):
        self.runtime._sessions["s1"] = SessionState(messages=[])
        self.runtime._set_active_skill("s1", "A")
        self.assertEqual(self.runtime._sessions["s1"].active_skill, "A")

    def test_set_active_skill_no_override(self):
        self.runtime._sessions["s1"] = SessionState(messages=[], active_skill="A")
        self.runtime._set_active_skill("s1", "B")
        self.assertEqual(self.runtime._sessions["s1"].active_skill, "A")


# ===================================================================
# 8. TestExecToolRead
# ===================================================================

class TestExecToolRead(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _, self.mock_skills = _make_runtime()
        self.runtime._sessions["s1"] = SessionState(messages=[])

    async def test_read_success(self):
        self.mock_skills.read_text_file.return_value = "line1\nline2\n"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "read", "id": "t1", "input": {"file_path": "test.txt"}},
        )
        self.assertFalse(r["is_error"])
        self.assertEqual(r["content"][0]["text"], "line1\nline2\n")

    async def test_read_with_offset_limit(self):
        self.mock_skills.read_text_file.return_value = "L1\nL2\nL3\nL4\nL5\n"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "read", "id": "t1", "input": {"file_path": "f.txt", "offset": 2, "limit": 2}},
        )
        self.assertFalse(r["is_error"])
        self.assertEqual(r["content"][0]["text"], "L2\nL3\n")

    async def test_read_failure(self):
        self.mock_skills.read_text_file.side_effect = FileNotFoundError("not found")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "read", "id": "t1", "input": {"file_path": "nope.txt"}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("read failed:", r["content"][0]["text"])

    async def test_read_skill_md_sets_lock(self):
        with tempfile.TemporaryDirectory() as td:
            skill_dir = Path(td) / "my-skill"
            skill_dir.mkdir()
            (skill_dir / "SKILL.md").write_text("---\nname: my-skill\n---\n# test", encoding="utf-8")
            mock_skill = MagicMock()
            mock_skill.id = "my-skill"
            mock_skill.root_dir = skill_dir
            self.mock_skills.list.return_value = [mock_skill]
            self.mock_skills.read_text_file.return_value = "content"
            await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "read", "id": "t1", "input": {"file_path": str(skill_dir / "SKILL.md")}},
            )
            self.assertEqual(self.runtime._sessions["s1"].active_skill, "my-skill")

    async def test_read_locked_different_skill(self):
        with tempfile.TemporaryDirectory() as td:
            skill_a = Path(td) / "skill-a"
            skill_a.mkdir()
            (skill_a / "SKILL.md").write_text("a", encoding="utf-8")
            skill_b = Path(td) / "skill-b"
            skill_b.mkdir()
            (skill_b / "SKILL.md").write_text("b", encoding="utf-8")

            mock_a = MagicMock(id="skill-a", root_dir=skill_a)
            mock_b = MagicMock(id="skill-b", root_dir=skill_b)
            self.mock_skills.list.return_value = [mock_a, mock_b]

            self.runtime._sessions["s1"].active_skill = "skill-a"
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "read", "id": "t1", "input": {"file_path": str(skill_b / "SKILL.md")}},
            )
            self.assertTrue(r["is_error"])
            self.assertIn("Skill lock", r["content"][0]["text"])
            self.mock_skills.read_text_file.assert_not_called()


# ===================================================================
# 9. TestExecToolFileOps
# ===================================================================

class TestExecToolFileOps(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _, self.mock_skills = _make_runtime()
        self.runtime._sessions["s1"] = SessionState(messages=[])

    async def test_write_success(self):
        self.mock_skills.write_file.return_value = "wrote 50 bytes"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "write", "id": "t1", "input": {"file_path": "f.txt", "content": "hello"}},
        )
        self.assertFalse(r["is_error"])
        self.assertIn("wrote", r["content"][0]["text"])

    async def test_write_failure(self):
        self.mock_skills.write_file.side_effect = ValueError("denied")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "write", "id": "t1", "input": {"file_path": "f.txt", "content": "x"}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("write failed:", r["content"][0]["text"])

    async def test_edit_success(self):
        self.mock_skills.edit_file.return_value = "edited ok"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "edit", "id": "t1", "input": {"file_path": "f.txt", "old_string": "a", "new_string": "b"}},
        )
        self.assertFalse(r["is_error"])

    async def test_edit_failure(self):
        self.mock_skills.edit_file.side_effect = ValueError("ambiguous")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "edit", "id": "t1", "input": {"file_path": "f.txt", "old_string": "a", "new_string": "b"}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("edit failed:", r["content"][0]["text"])

    async def test_exec_success(self):
        self.mock_skills.exec_command.return_value = ScriptResult(exit_code=0, stdout="ok", stderr="")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "exec", "id": "t1", "input": {"command": "echo hi"}},
        )
        self.assertFalse(r["is_error"])
        self.assertIn("exit_code=0", r["content"][0]["text"])
        self.assertIn("ok", r["content"][0]["text"])

    async def test_exec_nonzero_exit(self):
        self.mock_skills.exec_command.return_value = ScriptResult(exit_code=1, stdout="", stderr="fail")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "exec", "id": "t1", "input": {"command": "bad"}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("exit_code=1", r["content"][0]["text"])

    async def test_exec_empty_command(self):
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "exec", "id": "t1", "input": {"command": ""}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("command is empty", r["content"][0]["text"])
        self.mock_skills.exec_command.assert_not_called()

    async def test_list_dir_success(self):
        self.mock_skills.list_directory.return_value = "a.txt\nb.txt"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "list_dir", "id": "t1", "input": {"path": "/tmp"}},
        )
        self.assertFalse(r["is_error"])

    async def test_list_dir_failure(self):
        self.mock_skills.list_directory.side_effect = OSError("nope")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "list_dir", "id": "t1", "input": {"path": "/bad"}},
        )
        self.assertTrue(r["is_error"])

    async def test_grep_success(self):
        self.mock_skills.grep_workspace.return_value = "match line 1"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "grep", "id": "t1", "input": {"pattern": "test", "path": "/tmp"}},
        )
        self.assertFalse(r["is_error"])

    async def test_grep_failure(self):
        self.mock_skills.grep_workspace.side_effect = Exception("oops")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "grep", "id": "t1", "input": {"pattern": "x", "path": "/x"}},
        )
        self.assertTrue(r["is_error"])


# ===================================================================
# 10. TestExecToolWeb
# ===================================================================

class TestExecToolWeb(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _, self.mock_skills = _make_runtime()
        self.runtime._sessions["s1"] = SessionState(messages=[])

    @patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.ddg_web_search")
    async def test_web_search_success(self, mock_ddg):
        mock_ddg.return_value = '[{"title":"r1"}]'
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "web_search", "id": "t1", "input": {"query": "test"}},
        )
        self.assertFalse(r["is_error"])
        mock_ddg.assert_called_once()

    async def test_web_search_empty_query(self):
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "web_search", "id": "t1", "input": {"query": ""}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("query is empty", r["content"][0]["text"])

    @patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.ddg_web_search")
    async def test_web_search_exception(self, mock_ddg):
        mock_ddg.side_effect = Exception("network error")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "web_search", "id": "t1", "input": {"query": "test"}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("web_search failed:", r["content"][0]["text"])

    @patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.ddg_web_search")
    async def test_web_search_passes_optional_params(self, mock_ddg):
        mock_ddg.return_value = "[]"
        await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={
                "name": "web_search", "id": "t1",
                "input": {"query": "q", "count": 10, "region": "us-en", "safeSearch": "strict"},
            },
        )
        _, kwargs = mock_ddg.call_args
        self.assertEqual(kwargs.get("count"), 10)
        self.assertEqual(kwargs.get("region"), "us-en")
        self.assertEqual(kwargs.get("safe_search"), "strict")

    async def test_web_fetch_success(self):
        self.mock_skills.http_get_text.return_value = "<html>ok</html>"
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "web_fetch", "id": "t1", "input": {"url": "http://example.com"}},
        )
        self.assertFalse(r["is_error"])

    async def test_web_fetch_failure(self):
        self.mock_skills.http_get_text.side_effect = Exception("timeout")
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "web_fetch", "id": "t1", "input": {"url": "http://bad"}},
        )
        self.assertTrue(r["is_error"])

    @patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.call_price_compare_service")
    async def test_price_compare_success(self, mock_pc):
        mock_pc.return_value = '{"offers": []}'
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "price_compare", "id": "t1", "input": {"query": "phone"}},
        )
        self.assertFalse(r["is_error"])

    async def test_price_compare_empty_query(self):
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "price_compare", "id": "t1", "input": {"query": ""}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("query is empty", r["content"][0]["text"])

    @patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.batch_web_search_queries")
    async def test_batch_web_search_success(self, mock_bws):
        mock_bws.return_value = '{"results": []}'
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "batch_web_search", "id": "t1", "input": {"queries": ["q1", "q2"]}},
        )
        self.assertFalse(r["is_error"])

    async def test_batch_web_search_empty(self):
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "batch_web_search", "id": "t1", "input": {"queries": []}},
        )
        self.assertTrue(r["is_error"])
        self.assertIn("non-empty array", r["content"][0]["text"])

    async def test_batch_web_search_not_list(self):
        r = await self.runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "batch_web_search", "id": "t1", "input": {"queries": "hello"}},
        )
        self.assertTrue(r["is_error"])


# ===================================================================
# 11. TestExecToolBrowser
# ===================================================================

class TestExecToolBrowser(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _, _ = _make_runtime()
        self.runtime._sessions["s1"] = SessionState(messages=[])

    def _patch_browser(self):
        return patch("openclaw_gateway_runtime.agent_runtime.browser_client.BrowserManager")

    async def test_browser_navigate(self):
        with self._patch_browser() as MockBM:
            bm = MockBM.get.return_value

            async def _nav(*a, **k):
                return "Navigated to http://x"

            bm.navigate = _nav
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "navigate", "url": "http://x"}},
            )
            self.assertFalse(r["is_error"])
            self.assertIn("Navigated", r["content"][0]["text"])

    async def test_browser_navigate_missing_url(self):
        with self._patch_browser() as MockBM:
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "navigate", "url": ""}},
            )
            self.assertTrue(r["is_error"])
            self.assertIn("url is required", r["content"][0]["text"])

    async def test_browser_snapshot(self):
        with self._patch_browser() as MockBM:
            bm = MockBM.get.return_value

            async def _snap(*a, **k):
                return "[1] button 'Submit'"

            bm.snapshot = _snap
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "snapshot"}},
            )
            self.assertFalse(r["is_error"])

    async def test_browser_click(self):
        with self._patch_browser() as MockBM:
            bm = MockBM.get.return_value

            async def _click(*a, **k):
                return "clicked ok"

            bm.click = _click
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "click", "ref": 1}},
            )
            self.assertFalse(r["is_error"])

    async def test_browser_evaluate(self):
        with self._patch_browser() as MockBM:
            bm = MockBM.get.return_value

            async def _eval(*a, **k):
                return '{"result": 42}'

            bm.evaluate = _eval
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "evaluate", "script": "1+1"}},
            )
            self.assertFalse(r["is_error"])

    async def test_browser_evaluate_empty_script(self):
        with self._patch_browser() as MockBM:
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "evaluate", "script": ""}},
            )
            self.assertTrue(r["is_error"])
            self.assertIn("script is required", r["content"][0]["text"])

    async def test_browser_unknown_action(self):
        with self._patch_browser() as MockBM:
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "fly"}},
            )
            self.assertTrue(r["is_error"])
            self.assertIn("Unknown browser action", r["content"][0]["text"])

    async def test_browser_exception(self):
        with self._patch_browser() as MockBM:
            bm = MockBM.get.return_value
            async def _raise(*a, **k):
                raise RuntimeError("crash")
            bm.navigate = _raise
            r = await self.runtime._exec_tool_use(
                session_id="s1",
                tool_use={"name": "browser", "id": "t1", "input": {"action": "navigate", "url": "http://x"}},
            )
            self.assertTrue(r["is_error"])
            self.assertIn("browser failed:", r["content"][0]["text"])


# ===================================================================
# 12. TestExecToolUnknown
# ===================================================================

class TestExecToolUnknown(unittest.IsolatedAsyncioTestCase):
    async def test_unknown_tool(self):
        runtime, _, _ = _make_runtime()
        r = await runtime._exec_tool_use(
            session_id="s1",
            tool_use={"name": "nonexistent", "id": "t1", "input": {}},
        )
        self.assertTrue(r["is_error"])
        self.assertEqual(r["content"][0]["text"], "Unknown tool: nonexistent")


# ===================================================================
# 13. TestRunTurnTextOnly
# ===================================================================

@patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.get_memory_client")
class TestRunTurnTextOnly(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, self.mock_client, self.mock_skills = _make_runtime()
        self.cancel = CancelToken()

    async def test_text_only_response(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mc.save.return_value = 0
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda payload: _make_text_stream("Hello world")
        )
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=self.cancel)
        )
        text = "".join(e.payload["text"] for e in events if e.type == "stream.chunk")
        self.assertEqual(text, "Hello world")

    async def test_session_created(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("ok")
        )
        await _collect_events(
            self.runtime.run_turn(session_id="new-session", user_text="hi", cancel=self.cancel)
        )
        self.assertIn("new-session", self.runtime._sessions)

    async def test_messages_appended(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("reply")
        )
        await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hello", cancel=self.cancel)
        )
        msgs = self.runtime._sessions["s1"].messages
        self.assertEqual(msgs[0]["role"], "user")
        self.assertEqual(msgs[0]["content"], "hello")
        self.assertEqual(msgs[1]["role"], "assistant")

    async def test_skills_refresh_called(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("ok")
        )
        await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=self.cancel)
        )
        self.mock_skills.refresh.assert_called_once()

    async def test_memory_recall_called(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("ok")
        )
        await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hello", cancel=self.cancel)
        )
        mc.recall.assert_called_once()
        call_args = mc.recall.call_args
        # recall(user_id, query=user_text) — query may be positional or keyword
        all_args = list(call_args.args) + list(call_args.kwargs.values())
        self.assertIn("hello", all_args)

    async def test_memory_recall_failure_does_not_crash(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.side_effect = RuntimeError("memory down")
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("ok")
        )
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=self.cancel)
        )
        self.assertTrue(any(e.type == "stream.chunk" for e in events))

    async def test_skill_lock_cleared_on_no_tool_turn(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        # Pre-set session with active skill but tool_used_this_turn=False
        self.runtime._sessions["s1"] = SessionState(
            messages=[], active_skill="old-skill", tool_used_this_turn=False
        )
        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("ok")
        )
        await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=self.cancel)
        )
        self.assertIsNone(self.runtime._sessions["s1"].active_skill)

    async def test_api_payload_structure(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_text_stream("ok")
        )
        await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=self.cancel)
        )
        payload = self.mock_client.messages_create_stream.call_args[0][0]
        self.assertEqual(payload["model"], "test-model")
        self.assertEqual(payload["max_tokens"], 1024)
        self.assertIn("system", payload)
        self.assertIn("messages", payload)
        self.assertIn("tools", payload)


# ===================================================================
# 14. TestRunTurnToolLoop
# ===================================================================

@patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.get_memory_client")
class TestRunTurnToolLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, self.mock_client, self.mock_skills = _make_runtime()
        self.cancel = CancelToken()

    async def test_single_tool_round(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_skills.read_text_file.return_value = "file content"
        call_count = [0]

        def stream_factory(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream([
                    {"id": "toolu_1", "name": "read", "input": {"file_path": "test.txt"}},
                ])
            return _make_text_stream("Done reading.")

        self.mock_client.messages_create_stream = MagicMock(side_effect=stream_factory)
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="read test.txt", cancel=self.cancel)
        )
        tool_events = [e for e in events if e.type == "event.tool"]
        self.assertGreater(len(tool_events), 0)
        self.mock_skills.read_text_file.assert_called_once()
        self.assertEqual(call_count[0], 2)

    async def test_tool_result_sent_back(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_skills.read_text_file.return_value = "the content"
        call_count = [0]

        def stream_factory(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream([
                    {"id": "toolu_1", "name": "read", "input": {"file_path": "f.txt"}},
                ])
            # Second call: verify tool_result is in messages
            msgs = payload["messages"]
            last_msg = msgs[-1]
            # The tool results are appended as a user message with list content
            assert last_msg["role"] == "user"
            assert isinstance(last_msg["content"], list)
            return _make_text_stream("final")

        self.mock_client.messages_create_stream = MagicMock(side_effect=stream_factory)
        await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="go", cancel=self.cancel)
        )
        self.assertEqual(call_count[0], 2)

    async def test_parallel_tool_execution(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_skills.read_text_file.return_value = "content"
        self.mock_skills.list_directory.return_value = "dir listing"
        call_count = [0]

        def stream_factory(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream([
                    {"id": "toolu_1", "name": "read", "input": {"file_path": "a.txt"}},
                    {"id": "toolu_2", "name": "list_dir", "input": {"path": "/tmp"}},
                ])
            return _make_text_stream("all done")

        self.mock_client.messages_create_stream = MagicMock(side_effect=stream_factory)
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="go", cancel=self.cancel)
        )
        result_events = [e for e in events if e.type == "event.tool" and e.payload.get("phase") == "result"]
        self.assertEqual(len(result_events), 2)

    async def test_tool_loop_limit(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_skills.read_text_file.return_value = "x"

        # Always return tool_use to trigger the limit
        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_tool_stream([
                {"id": "toolu_1", "name": "read", "input": {"file_path": "f.txt"}},
            ])
        )
        old_max = AnthropicAgentRuntime.MAX_TOOL_ROUNDS
        AnthropicAgentRuntime.MAX_TOOL_ROUNDS = 2
        try:
            events = await _collect_events(
                self.runtime.run_turn(session_id="s1", user_text="go", cancel=self.cancel)
            )
        finally:
            AnthropicAgentRuntime.MAX_TOOL_ROUNDS = old_max
        text = "".join(e.payload.get("text", "") for e in events if e.type == "stream.chunk")
        self.assertIn("tool loop limit reached", text)

    async def test_tool_result_truncation(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        big_text = "x" * 500_000
        self.mock_skills.read_text_file.return_value = big_text
        call_count = [0]

        def stream_factory(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream([
                    {"id": "toolu_1", "name": "read", "input": {"file_path": "big.txt"}},
                ])
            return _make_text_stream("done")

        self.mock_client.messages_create_stream = MagicMock(side_effect=stream_factory)
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="go", cancel=self.cancel)
        )
        # Check that the tool result event has truncated text
        result_events = [e for e in events if e.type == "event.tool" and e.payload.get("phase") == "result"]
        self.assertEqual(len(result_events), 1)
        result_text = result_events[0].payload["result"]["content"][0]["text"]
        self.assertIn("[truncated to", result_text)
        self.assertLessEqual(len(result_text), 500_000)

    async def test_event_tool_emitted(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        self.mock_skills.read_text_file.return_value = "ok"
        call_count = [0]

        def stream_factory(payload):
            call_count[0] += 1
            if call_count[0] == 1:
                return _make_tool_stream([
                    {"id": "toolu_1", "name": "read", "input": {"file_path": "f.txt"}},
                ])
            return _make_text_stream("done")

        self.mock_client.messages_create_stream = MagicMock(side_effect=stream_factory)
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="go", cancel=self.cancel)
        )
        tool_start = [e for e in events if e.type == "event.tool" and "phase" not in (e.payload or {})]
        tool_result = [e for e in events if e.type == "event.tool" and e.payload.get("phase") == "result"]
        self.assertEqual(len(tool_start), 1)
        self.assertEqual(tool_start[0].payload["name"], "read")
        self.assertEqual(len(tool_result), 1)


# ===================================================================
# 15. TestRunTurnCancellation
# ===================================================================

@patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.get_memory_client")
class TestRunTurnCancellation(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, self.mock_client, self.mock_skills = _make_runtime()

    async def test_cancel_before_api_call(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        cancel = CancelToken()
        cancel.cancel()
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=cancel)
        )
        self.assertEqual(len(events), 0)

    async def test_cancel_during_streaming(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        cancel = CancelToken()

        async def _stream_with_cancel(payload):
            yield {"type": "message_start", "message": {"id": "msg_1"}}
            yield {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}
            yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": "partial"}}
            cancel.cancel()  # Cancel mid-stream
            yield {"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": " more"}}
            yield {"type": "content_block_stop", "index": 0}
            yield {"type": "message_delta", "delta": {"stop_reason": "end_turn"}}
            yield {"type": "message_stop"}

        self.mock_client.messages_create_stream = MagicMock(side_effect=lambda p: _stream_with_cancel(p))
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="hi", cancel=cancel)
        )
        # Should have gotten at least the "partial" text before cancellation
        texts = [e.payload["text"] for e in events if e.type == "stream.chunk"]
        full_text = "".join(texts)
        self.assertIn("partial", full_text)

    async def test_cancel_after_tool_execution(self, mock_get_mc):
        mc = MagicMock()
        mc.recall.return_value = []
        mock_get_mc.return_value = mc

        cancel = CancelToken()

        def _read_and_cancel(path, **kw):
            cancel.cancel()
            return "content"

        self.mock_skills.read_text_file = _read_and_cancel
        self.mock_client.messages_create_stream = MagicMock(
            side_effect=lambda p: _make_tool_stream([
                {"id": "toolu_1", "name": "read", "input": {"file_path": "f.txt"}},
            ])
        )
        events = await _collect_events(
            self.runtime.run_turn(session_id="s1", user_text="go", cancel=cancel)
        )
        # Should not have a second API call
        self.assertEqual(self.mock_client.messages_create_stream.call_count, 1)


# ===================================================================
# 16. TestSaveTurnMemories
# ===================================================================

@patch("openclaw_gateway_runtime.agent_runtime.anthropic_runtime.get_memory_client")
class TestSaveTurnMemories(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runtime, _, _ = _make_runtime()
        self.runtime._sessions["s1"] = SessionState(messages=[])

    async def test_saves_extracted_entries(self, mock_get_mc):
        mc = MagicMock()
        mc.save.return_value = 1
        mock_get_mc.return_value = mc

        await self.runtime._save_turn_memories(
            "s1", "我叫张三同学", [{"type": "text", "text": "ok"}]
        )
        mc.save.assert_called_once()

    async def test_no_save_when_no_entries(self, mock_get_mc):
        mc = MagicMock()
        mock_get_mc.return_value = mc

        await self.runtime._save_turn_memories("s1", "hello", [{"type": "text", "text": "world"}])
        mc.save.assert_not_called()

    async def test_save_failure_no_raise(self, mock_get_mc):
        mc = MagicMock()
        mc.save.side_effect = RuntimeError("db down")
        mock_get_mc.return_value = mc

        # Should not raise
        await self.runtime._save_turn_memories(
            "s1", "我喜欢蓝色的东西", [{"type": "text", "text": "ok"}]
        )

    async def test_extracts_from_both_user_and_assistant(self, mock_get_mc):
        mc = MagicMock()
        mc.save.return_value = 2
        mock_get_mc.return_value = mc

        await self.runtime._save_turn_memories(
            "s1",
            "我叫李四同学",
            [{"type": "text", "text": "我喜欢蓝色的汽车"}],
        )
        mc.save.assert_called_once()
        entries = mc.save.call_args[0][1]
        types = {e.type for e in entries}
        self.assertIn("fact", types)
        self.assertIn("preference", types)


if __name__ == "__main__":
    unittest.main()
