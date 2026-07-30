from __future__ import annotations

import io
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "codex-helper.py"
sys.path.insert(0, str(PROJECT_ROOT))

import importlib.machinery
import importlib.util

_loader = importlib.machinery.SourceFileLoader(
    "codex_helper",
    str(SCRIPT),
)
_spec = importlib.util.spec_from_loader("codex_helper", _loader)
codex_helper = importlib.util.module_from_spec(_spec)
sys.modules["codex_helper"] = codex_helper
_loader.exec_module(codex_helper)


EBOOKNT_ID = "019f6731-a3ba-7981-914a-31e6b1571f1d"
OTHER_ID = "019fb4f5-78c2-7dd3-8f70-3f183e5649f8"


class TrackingBytesIO(io.BytesIO):
    def __init__(self, value: bytes) -> None:
        super().__init__(value)
        self.bytes_read = 0

    def read(self, size: int = -1) -> bytes:
        value = super().read(size)
        self.bytes_read += len(value)
        return value


class CodexSessionsCliTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.home = self.root / ".codex"
        self.sessions = self.home / "sessions" / "2026" / "07" / "30"
        self.sessions.mkdir(parents=True)

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def write_session(
        self,
        session_id: str,
        timestamp: str,
        cwd: str = "/work",
        user_message: str = "user prompt",
        final_message: str = "final answer",
    ) -> Path:
        path = self.sessions / f"rollout-2026-07-30T00-00-00-{session_id}.jsonl"
        records = (
            {
                "type": "session_meta",
                "payload": {
                    "id": session_id,
                    "timestamp": timestamp,
                    "cwd": cwd,
                },
            },
            {
                "type": "event_msg",
                "payload": {"type": "user_message", "message": user_message},
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "commentary",
                    "message": "我改用同一份active`DISPLAYCONFIG_PATH_INFO`和target",
                },
            },
            {
                "type": "event_msg",
                "payload": {
                    "type": "agent_message",
                    "phase": "final_answer",
                    "message": final_message,
                },
            },
        )
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def write_item_session(
        self,
        session_id: str,
        timestamp: str,
        turns: list[dict],
        cwd: str = "/work",
    ) -> Path:
        """Write the Codex 0.157 rollout layout (event_msg/item_completed).

        Each turn is a dict of::

            {"turn": str, "user": str | list[str], "messages": [(id, text), ...],
             "last_agent_message": str | None, "end": "task_complete"|"turn_aborted"}
        """
        path = self.sessions / f"rollout-2026-07-30T00-00-00-{session_id}.jsonl"
        records: list[dict] = [
            {
                "type": "session_meta",
                "payload": {"id": session_id, "timestamp": timestamp, "cwd": cwd},
            }
        ]
        for turn in turns:
            turn_id = turn["turn"]
            records.append(
                {
                    "type": "event_msg",
                    "payload": {"type": "task_started", "turn_id": turn_id},
                }
            )
            users = turn.get("user", [])
            if isinstance(users, str):
                users = [users]
            for index, user in enumerate(users):
                records.append(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "turn_id": turn_id,
                            "item": {
                                "type": "UserMessage",
                                "id": f"um_{turn_id}_{index}",
                                "content": [{"type": "text", "text": user}],
                            },
                        },
                    }
                )
            for message_id, text in turn.get("messages", []):
                records.append(
                    {
                        "type": "event_msg",
                        "payload": {
                            "type": "item_completed",
                            "turn_id": turn_id,
                            "item": {
                                "type": "AgentMessage",
                                "id": message_id,
                                "content": [{"type": "Text", "text": text}],
                            },
                        },
                    }
                )
            end = turn.get("end", "task_complete")
            payload = {"type": end, "turn_id": turn_id}
            if "last_agent_message" in turn:
                payload["last_agent_message"] = turn["last_agent_message"]
            records.append({"type": "event_msg", "payload": payload})
        path.write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )
        return path

    def write_names(self, *entries: tuple[str, str]) -> None:
        records = (
            {"id": session_id, "thread_name": name, "updated_at": "2026-07-30T00:00:00Z"}
            for session_id, name in entries
        )
        (self.home / "session_index.jsonl").write_text(
            "".join(json.dumps(record) + "\n" for record in records),
            encoding="utf-8",
        )

    def write_state(
        self,
        *rows: tuple[str, Path, int, str, str | None, str],
    ) -> None:
        database = self.home / "state_5.sqlite"
        with sqlite3.connect(database) as connection:
            connection.execute(
                """
                CREATE TABLE threads (
                    id TEXT PRIMARY KEY,
                    rollout_path TEXT NOT NULL,
                    updated_at_ms INTEGER,
                    updated_at INTEGER,
                    cwd TEXT NOT NULL,
                    title TEXT NOT NULL,
                    name TEXT,
                    thread_source TEXT
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO threads
                    (id, rollout_path, updated_at_ms, updated_at, cwd, title, name,
                     thread_source)
                VALUES (?, ?, ?, 0, ?, ?, ?, 'user')
                """,
                (
                    (session_id, str(path), updated_at_ms, cwd, title, name)
                    for session_id, path, updated_at_ms, cwd, name, title in rows
                ),
            )

    def run_cli(self, *arguments: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--codex-home",
                str(self.home),
                *arguments,
            ],
            text=True,
            capture_output=True,
            check=False,
        )

    def test_list_includes_uuid_to_disambiguate_duplicate_names(self) -> None:
        ebooknt_path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z", "/ebooknt")
        other_path = self.write_session(OTHER_ID, "2026-07-30T21:36:53Z", "/other")
        self.write_names((EBOOKNT_ID, "extractor"), (OTHER_ID, "extractor"))
        self.write_state(
            (EBOOKNT_ID, ebooknt_path, 2_000_000, "/ebooknt", None, "automatic old title"),
            (OTHER_ID, other_path, 1_000_000, "/other", None, "x" * 10_000),
        )

        result = self.run_cli("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0].split(), ["LAST", "NAME", "UUID", "WORKDIR"])
        self.assertIn("extractor", lines[1])
        self.assertTrue(lines[1].endswith("/ebooknt"))
        self.assertNotIn("\t", result.stdout)
        self.assertIn(EBOOKNT_ID, result.stdout)
        self.assertIn(OTHER_ID, result.stdout)
        self.assertNotIn("automatic old title", result.stdout)
        self.assertNotIn("x" * 100, result.stdout)

        reverse = self.run_cli("list", "--reverse")

        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        self.assertIn("extractor", reverse.stdout.splitlines()[1])
        self.assertIn(OTHER_ID, reverse.stdout.splitlines()[1])
        self.assertTrue(reverse.stdout.splitlines()[1].endswith("/other"))

    def test_list_uses_automatic_title_when_session_has_no_explicit_name(self) -> None:
        path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        self.write_state(
            (EBOOKNT_ID, path, 2_000_000, "/ebooknt", None, "  automatic\n  title  "),
        )

        result = self.run_cli("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("automatic title", result.stdout)
        self.assertIn(EBOOKNT_ID, result.stdout)

    def test_list_truncates_long_automatic_title(self) -> None:
        path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        self.write_state(
            (EBOOKNT_ID, path, 2_000_000, "/ebooknt", None, "长" * 40),
        )

        result = self.run_cli("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("长" * 29 + "…", result.stdout)
        self.assertNotIn("长" * 30, result.stdout)

    def test_list_omits_subagent_rollout_but_keeps_parent_user_thread(self) -> None:
        parent = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z", cwd="/ebooknt")
        child = self.write_session(OTHER_ID, "2026-07-15T19:12:15Z", cwd="/ebooknt")
        self.write_state(
            (EBOOKNT_ID, parent, 2_000_000, "/ebooknt", None, "parent title"),
            (OTHER_ID, child, 3_000_000, "/ebooknt", None, "guardian title"),
        )
        with sqlite3.connect(self.home / "state_5.sqlite") as connection:
            connection.execute(
                "UPDATE threads SET thread_source = 'subagent' WHERE id = ?",
                (OTHER_ID,),
            )

        result = self.run_cli("list")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("parent title", result.stdout)
        self.assertIn(EBOOKNT_ID, result.stdout)
        self.assertNotIn("guardian title", result.stdout)
        self.assertNotIn(OTHER_ID, result.stdout)

    def test_list_aligns_cjk_names_by_terminal_display_width(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)
        sessions = [
            codex_helper.Session(
                EBOOKNT_ID,
                Path("first.jsonl"),
                updated_at="2026-07-30T11:59:52Z",
                cwd="/short",
                name="ebooknt",
            ),
            codex_helper.Session(
                OTHER_ID,
                Path("second.jsonl"),
                updated_at="2026-07-30T11:59:36Z",
                cwd="/long",
                name="优化extract分页与按序加载",
            ),
        ]
        output = io.StringIO()

        with mock.patch.object(codex_helper.sys, "stdout", output):
            codex_helper.list_sessions(sessions, now)

        header, short_name, long_name = output.getvalue().splitlines()

        def column(line: str, value: str) -> int:
            return codex_helper.display_width(line[: line.index(value)])

        self.assertEqual(
            {column(header, "NAME"), column(short_name, "ebooknt"), column(long_name, "优化")},
            {9},
        )
        self.assertEqual(
            {
                column(header, "UUID"),
                column(short_name, EBOOKNT_ID),
                column(long_name, OTHER_ID),
            },
            {36},
        )
        self.assertEqual(
            {
                column(header, "WORKDIR"),
                column(short_name, "/short"),
                column(long_name, "/long"),
            },
            {74},
        )

    def test_relative_time_uses_resume_picker_style_units(self) -> None:
        now = datetime(2026, 7, 30, 12, 0, tzinfo=timezone.utc)

        self.assertEqual(codex_helper.relative_time("2026-07-30T12:00:00Z", now), "now")
        self.assertEqual(codex_helper.relative_time("2026-07-30T11:59:45Z", now), "15s ago")
        self.assertEqual(codex_helper.relative_time("2026-07-30T11:58:00Z", now), "2m ago")
        self.assertEqual(codex_helper.relative_time("2026-07-30T09:00:00Z", now), "3h ago")
        self.assertEqual(codex_helper.relative_time("2026-07-26T12:00:00Z", now), "4d ago")

    def test_plain_pipe_stops_cleanly_when_the_consumer_closes(self) -> None:
        stdout = mock.Mock()
        stdout.isatty.return_value = False

        with mock.patch.object(codex_helper.sys, "stdout", stdout):
            with codex_helper.pager_output():
                raise BrokenPipeError

        stdout.close.assert_called_once_with()

    def test_extract_resolves_exact_rename_and_includes_commentary(self) -> None:
        self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        self.write_names((EBOOKNT_ID, "ebooknt"))

        result = self.run_cli("extract", "ebooknt")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("› user prompt", result.stdout)
        self.assertIn(
            "• 我改用同一份active DISPLAYCONFIG_PATH_INFO 和target",
            result.stdout,
        )
        self.assertIn("• final answer", result.stdout)
        self.assertNotIn("=====", result.stdout)
        self.assertNotIn("`DISPLAYCONFIG_PATH_INFO`", result.stdout)

    def test_final_only_omits_commentary(self) -> None:
        self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        self.write_names((EBOOKNT_ID, "ebooknt"))

        result = self.run_cli("extract", "ebooknt", "--final-only")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("• final answer", result.stdout)
        self.assertNotIn("DISPLAYCONFIG_PATH_INFO", result.stdout)

    def test_reverse_keeps_user_before_replies(self) -> None:
        path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")

        result = self.run_cli("extract", str(path), "--reverse")

        self.assertEqual(result.returncode, 0, result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith(("› ", "• "))]
        self.assertEqual(markers, [
            "› user prompt",
            "• 我改用同一份active DISPLAYCONFIG_PATH_INFO 和target",
            "• final answer",
        ])

    def test_reverse_orders_turns_but_keeps_multiple_replies_in_order(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "q1",
                    "messages": [("msg-a", "c1"), ("msg-b", "f1")],
                    "last_agent_message": "f1",
                },
                {
                    "turn": "turn-2",
                    "user": "q2",
                    "messages": [("msg-c", "c2"), ("msg-d", "c3"), ("msg-e", "f2")],
                    "last_agent_message": "f2",
                },
            ],
        )

        result = self.run_cli("extract", str(path), "--reverse")

        self.assertEqual(result.returncode, 0, result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith(("› ", "• "))]
        self.assertEqual(markers, [
            "› q2", "• c2", "• c3", "• f2",
            "› q1", "• c1", "• f1",
        ])

    def test_reverse_keeps_consecutive_user_inputs_before_their_reply(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "older question",
                    "messages": [("msg-a", "older answer")],
                    "last_agent_message": "older answer",
                },
                {
                    "turn": "turn-2",
                    "user": ["A", "B", "C"],
                    "messages": [("msg-b", "first output"), ("msg-c", "answer")],
                    "last_agent_message": "answer",
                },
            ],
        )

        result = self.run_cli("extract", str(path), "--reverse")

        self.assertEqual(result.returncode, 0, result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith(("› ", "• "))]
        self.assertEqual(markers, [
            "› A", "› B", "› C", "• first output", "• answer",
            "› older question", "• older answer",
        ])

        final_only = self.run_cli("extract", str(path), "--reverse", "--final-only")
        self.assertEqual(final_only.returncode, 0, final_only.stderr)
        final_markers = [
            line for line in final_only.stdout.splitlines()
            if line.startswith(("› ", "• "))
        ]
        self.assertEqual(final_markers, [
            "› A", "› B", "› C", "• answer",
            "› older question", "• older answer",
        ])

    def test_reverse_file_lines_are_chunked_and_preserve_order(self) -> None:
        long_line = b"x" * 100
        data = b"first\n" + long_line + b"\nmiddle\nlast\n"
        stream = TrackingBytesIO(data)
        lines = codex_helper.reverse_raw_lines(stream, chunk_size=16)

        self.assertEqual(next(lines)[1], b"last")
        self.assertEqual(stream.bytes_read, 16)
        self.assertEqual(
            [line for _offset, line in lines],
            [b"middle", long_line, b"first"],
        )

    def test_reverse_extract_prints_before_consuming_all_messages(self) -> None:
        session = codex_helper.Session("id", Path("unused.jsonl"))

        def messages(
            path: Path,
            final_only: bool,
            reverse: bool = False,
        ):
            self.assertEqual(path, session.path)
            self.assertTrue(final_only)
            self.assertTrue(reverse)
            yield "CODEX", "newest"
            raise AssertionError("extract consumed past the first pager write")

        with (
            mock.patch.object(codex_helper, "conversation", side_effect=messages),
            mock.patch.object(
                codex_helper,
                "print_message",
                side_effect=BrokenPipeError,
            ) as print_message,
        ):
            with self.assertRaises(BrokenPipeError):
                codex_helper.extract(session, final_only=True, width=None, reverse=True)

        print_message.assert_called_once_with("CODEX", "newest", None)

    def test_width_wraps_chinese_and_never_breaks_english_words(self) -> None:
        path = self.write_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            user_message="不会用其它路径伪造结果",
            final_message="unbreakableword",
        )

        result = self.run_cli("extract", str(path), "--final-only", "-w", "18")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("› 不会用其它路径\n  伪造结果", result.stdout)
        self.assertNotIn("不会用其它路径伪\n", result.stdout)
        self.assertIn("• unbreakableword", result.stdout)
        self.assertNotIn("unbreakable\n", result.stdout)

    def test_current_sqlite_name_overrides_legacy_name(self) -> None:
        path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        self.write_names((EBOOKNT_ID, "old-name"))
        self.write_state(
            (EBOOKNT_ID, path, 2_000_000, "/ebooknt", "ebooknt", "automatic title"),
        )

        result = self.run_cli("extract", "ebooknt")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("final answer", result.stdout)

    def test_jsonl_path_does_not_require_a_codex_helper_directory(self) -> None:
        path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        missing_home = self.root / "missing"

        result = subprocess.run(
            [
                sys.executable,
                str(SCRIPT),
                "--codex-home",
                str(missing_home),
                "extract",
                str(path),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("final answer", result.stdout)

    def test_malformed_utf8_and_payload_are_skipped_during_extract(self) -> None:
        path = self.write_session(EBOOKNT_ID, "2026-07-15T19:12:14Z")
        original = path.read_bytes()
        malformed = (
            b"\xff\n"
            b'{"type":"event_msg","payload":"not-an-object"}\n'
            b'{"type":"event_msg","payload":{"type":"agent_message",'
            b'"phase":"final_answer","message":"after malformed data"}}\n'
        )
        path.write_bytes(original + malformed)

        result = self.run_cli("extract", str(path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("after malformed data", result.stdout)
        self.assertIn("warning:", result.stderr)

        reverse = self.run_cli("extract", str(path), "--reverse")

        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        self.assertIn("after malformed data", reverse.stdout)
        self.assertIn("warning:", reverse.stderr)


    def test_extract_reads_item_completed_transcript(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "问题描述",
                    "messages": [
                        ("msg-a", "先看一下日志"),
                        ("msg-b", "结论：是路径问题"),
                    ],
                    "last_agent_message": "结论：是路径问题",
                }
            ],
        )

        result = self.run_cli("extract", str(path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("› 问题描述", result.stdout)
        self.assertIn("• 先看一下日志", result.stdout)
        self.assertIn("• 结论：是路径问题", result.stdout)

    def test_final_only_uses_turn_end_last_agent_message(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "first question",
                    "messages": [("msg-a", "commentary one"), ("msg-b", "final one")],
                    "last_agent_message": "final one",
                },
                {
                    "turn": "turn-2",
                    "user": "second question",
                    "messages": [("msg-c", "commentary two"), ("msg-d", "final two")],
                    "last_agent_message": "final two",
                },
            ],
        )

        result = self.run_cli("extract", str(path), "--final-only")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("commentary one", result.stdout)
        self.assertNotIn("commentary two", result.stdout)
        self.assertIn("• final one", result.stdout)
        self.assertIn("• final two", result.stdout)

    def test_final_only_falls_back_when_turn_ended_without_last_agent_message(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "aborted question",
                    "messages": [("msg-a", "partial thought"), ("msg-b", "last words")],
                    "end": "turn_aborted",
                }
            ],
        )

        forward = self.run_cli("extract", str(path), "--final-only")

        self.assertEqual(forward.returncode, 0, forward.stderr)
        self.assertIn("• last words", forward.stdout)
        self.assertNotIn("partial thought", forward.stdout)

        reverse = self.run_cli("extract", str(path), "--final-only", "--reverse")

        self.assertEqual(reverse.returncode, 0, reverse.stderr)
        self.assertIn("• last words", reverse.stdout)
        self.assertNotIn("partial thought", reverse.stdout)

    def test_final_only_falls_back_when_last_agent_message_is_empty(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "question",
                    "messages": [("msg-a", "body")],
                    "last_agent_message": "",
                }
            ],
        )

        result = self.run_cli("extract", str(path), "--final-only")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("• body", result.stdout)

    def test_final_only_emits_nothing_for_a_turn_without_agent_text(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "interrupted before any reply",
                    "messages": [],
                    "end": "turn_aborted",
                }
            ],
        )

        result = self.run_cli("extract", str(path), "--final-only")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("•", result.stdout)

    def test_reemission_of_the_same_agent_message_is_not_duplicated(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "question",
                    "messages": [
                        ("msg-a", "answer body"),
                        ("msg-a", "answer body\n"),
                    ],
                    "last_agent_message": "answer body\n",
                }
            ],
        )

        result = self.run_cli("extract", str(path))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.count("answer body"), 1)

    def test_final_only_reverse_matches_the_forward_selection(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "q1",
                    "messages": [("msg-a", "c1"), ("msg-b", "f1")],
                    "last_agent_message": "f1",
                },
                {
                    "turn": "turn-2",
                    "user": "q2",
                    "messages": [("msg-c", "c2"), ("msg-d", "f2")],
                    "end": "turn_aborted",
                },
            ],
        )

        forward = self.run_cli("extract", str(path), "--final-only").stdout
        reverse = self.run_cli("extract", str(path), "--final-only", "--reverse").stdout

        def codex_lines(output: str) -> list[str]:
            return [line for line in output.splitlines() if line.startswith("• ")]

        self.assertEqual(sorted(codex_lines(forward)), sorted(codex_lines(reverse)))
        self.assertEqual(codex_lines(reverse), list(reversed(codex_lines(forward))))
        self.assertEqual(
            [line for line in reverse.splitlines() if line.startswith(("› ", "• "))],
            ["› q2", "• f2", "› q1", "• f1"],
        )

    def test_final_only_reverse_keeps_unfinished_turn_reply_with_its_user(self) -> None:
        path = self.write_item_session(
            EBOOKNT_ID,
            "2026-07-15T19:12:14Z",
            [
                {
                    "turn": "turn-1",
                    "user": "q1",
                    "messages": [("msg-a", "f1")],
                    "last_agent_message": "f1",
                },
                {
                    "turn": "turn-2",
                    "user": "q2",
                    "messages": [("msg-b", "c2"), ("msg-c", "latest reply")],
                },
            ],
        )
        path.write_bytes(path.read_bytes().rsplit(b"\n", 2)[0] + b"\n")

        result = self.run_cli("extract", str(path), "--final-only", "--reverse")

        self.assertEqual(result.returncode, 0, result.stderr)
        markers = [line for line in result.stdout.splitlines() if line.startswith(("› ", "• "))]
        self.assertEqual(markers, [
            "› q2", "• latest reply",
            "› q1", "• f1",
        ])


if __name__ == "__main__":
    unittest.main()
