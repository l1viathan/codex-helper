#!/usr/bin/env python3
"""List Codex sessions and extract their user/assistant conversation."""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import BinaryIO, Iterator


@dataclass(frozen=True)
class Session:
    id: str
    path: Path
    updated_at: str = ""
    cwd: str = ""
    name: str = ""
    title: str = ""
    thread_source: str = ""


@dataclass(frozen=True)
class ThreadState:
    path: Path | None = None
    updated_at: str = ""
    cwd: str = ""
    name: str = ""
    title: str = ""
    thread_source: str = ""


class ExtractorError(Exception):
    def __init__(self, message: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.exit_code = exit_code


SESSION_ID_SUFFIX = re.compile(
    r"([0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12})$"
)
INLINE_CODE_SPAN = re.compile(
    r"(?<!`)(?P<delimiter>`{1,2})(?P<content>[^`\n]+?)(?P=delimiter)(?!`)"
)
CLOSING_PUNCTUATION = frozenset("，。；：！？、）》】」』…")
REVERSE_READ_CHUNK_SIZE = 64 * 1024
AUTOMATIC_TITLE_WIDTH = 60


def warning(message: str) -> None:
    print(f"warning: {message}", file=sys.stderr)


def codex_home(value: str | None) -> Path:
    default = os.environ.get("CODEX_HOME")
    return Path(value if value is not None else default or Path.home() / ".codex").expanduser()


def reverse_raw_lines(
    stream: BinaryIO,
    chunk_size: int = REVERSE_READ_CHUNK_SIZE,
) -> Iterator[tuple[int, bytes]]:
    """Yield physical lines from a seekable binary stream, starting at EOF."""
    if chunk_size <= 0:
        raise ValueError("chunk_size must be positive")

    stream.seek(0, os.SEEK_END)
    file_size = stream.tell()
    position = file_size
    line_fragments: list[bytes] = []
    first_segment = True

    while position:
        read_size = min(chunk_size, position)
        position -= read_size
        stream.seek(position)
        block = stream.read(read_size)
        segments = block.split(b"\n")
        if len(segments) == 1:
            line_fragments.append(block)
            continue

        offsets = [position]
        for segment in segments[:-1]:
            offsets.append(offsets[-1] + len(segment) + 1)

        last_segment = segments[-1]
        if line_fragments:
            last_segment += b"".join(reversed(line_fragments))
        last_offset = offsets[-1]
        if first_segment:
            first_segment = False
            if last_offset != file_size or last_segment:
                yield last_offset, last_segment
        else:
            yield last_offset, last_segment

        for index in range(len(segments) - 2, 0, -1):
            yield offsets[index], segments[index]
        line_fragments = [segments[0]]

    if file_size:
        yield 0, b"".join(reversed(line_fragments))


def json_lines(path: Path, reverse: bool = False) -> Iterator[dict]:
    try:
        stream = path.open("rb")
    except OSError as error:
        raise ExtractorError(f"cannot read {path}: {error}") from error

    with stream:
        if reverse:
            located_lines = (
                (f"{path}:byte {offset}", raw_line)
                for offset, raw_line in reverse_raw_lines(stream)
            )
        else:
            located_lines = (
                (f"{path}:{number}", raw_line)
                for number, raw_line in enumerate(stream, 1)
            )

        for location, raw_line in located_lines:
            try:
                line = raw_line.decode("utf-8")
                value = json.loads(line)
            except (json.JSONDecodeError, UnicodeDecodeError) as error:
                warning(f"skipping {location}: {error}")
                continue
            if isinstance(value, dict):
                yield value


def legacy_session_names(home: Path) -> dict[str, str]:
    names: dict[str, str] = {}
    index = home / "session_index.jsonl"
    if not index.is_file():
        return names

    try:
        for item in json_lines(index):
            session_id = item.get("id")
            name = item.get("thread_name")
            if not isinstance(session_id, str) or not isinstance(name, str):
                continue
            if name:
                names[session_id] = name
            else:
                names.pop(session_id, None)
    except ExtractorError as error:
        warning(str(error))
    return names


def database_mtime(path: Path) -> int:
    try:
        return path.stat().st_mtime_ns
    except OSError:
        return -1


def epoch_timestamp(milliseconds: object, seconds: object) -> str:
    for value, divisor in ((milliseconds, 1000), (seconds, 1)):
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        try:
            timestamp = datetime.fromtimestamp(value / divisor, tz=timezone.utc)
        except (OSError, OverflowError, ValueError):
            continue
        return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    return ""


def read_state_database(path: Path) -> dict[str, ThreadState]:
    uri = f"{path.resolve().as_uri()}?mode=ro"
    with sqlite3.connect(uri, uri=True, timeout=1) as connection:
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(threads)")
            if isinstance(row[1], str)
        }
        if "id" not in columns:
            return {}

        optional = (
            "rollout_path",
            "updated_at_ms",
            "updated_at",
            "cwd",
            "name",
            "title",
            "thread_source",
        )
        selections = ["id"]
        selections.extend(
            column if column in columns else f"NULL AS {column}" for column in optional
        )
        rows = connection.execute(f"SELECT {', '.join(selections)} FROM threads")
        result: dict[str, ThreadState] = {}
        for (
            session_id,
            rollout_path,
            updated_ms,
            updated,
            cwd,
            name,
            title,
            thread_source,
        ) in rows:
            if not isinstance(session_id, str):
                continue
            result[session_id] = ThreadState(
                path=Path(rollout_path).expanduser()
                if isinstance(rollout_path, str) and rollout_path
                else None,
                updated_at=epoch_timestamp(updated_ms, updated),
                cwd=cwd if isinstance(cwd, str) else "",
                name=name if isinstance(name, str) else "",
                title=title if isinstance(title, str) else "",
                thread_source=thread_source if isinstance(thread_source, str) else "",
            )
        return result


def merge_state(old: ThreadState, new: ThreadState) -> ThreadState:
    return ThreadState(
        path=new.path or old.path,
        updated_at=new.updated_at or old.updated_at,
        cwd=new.cwd or old.cwd,
        name=new.name or old.name,
        title=new.title or old.title,
        thread_source=new.thread_source or old.thread_source,
    )


def thread_states(home: Path) -> dict[str, ThreadState]:
    states: dict[str, ThreadState] = {}
    databases = sorted(home.glob("state_*.sqlite"), key=database_mtime)
    for database in databases:
        try:
            rows = read_state_database(database)
        except (OSError, sqlite3.Error) as error:
            warning(f"cannot read Codex state database {database}: {error}")
            continue
        for session_id, state in rows.items():
            states[session_id] = merge_state(states.get(session_id, ThreadState()), state)

    for session_id, name in legacy_session_names(home).items():
        state = states.get(session_id, ThreadState())
        if not state.name:
            states[session_id] = merge_state(state, ThreadState(name=name))
    return states


def read_meta(path: Path) -> tuple[str, str, str]:
    for item in json_lines(path):
        if item.get("type") != "session_meta":
            continue
        payload = item.get("payload")
        if not isinstance(payload, dict):
            warning(f"skipping malformed session metadata in {path}")
            continue
        session_id = payload.get("id")
        timestamp = payload.get("timestamp") or item.get("timestamp")
        cwd = payload.get("cwd")
        return (
            session_id if isinstance(session_id, str) else "",
            timestamp if isinstance(timestamp, str) else "",
            cwd if isinstance(cwd, str) else "",
        )
    return "", "", ""


def fallback_session_id(path: Path) -> str:
    match = SESSION_ID_SUFFIX.search(path.stem)
    return match.group(1) if match else path.stem


def file_timestamp(path: Path, fallback: str = "") -> str:
    try:
        timestamp = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    except OSError:
        return fallback
    return timestamp.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def session_from_path(path: Path, state: ThreadState | None = None) -> Session:
    session_id, created_at, cwd = read_meta(path)
    session_id = session_id or fallback_session_id(path)
    state = state or ThreadState()
    return Session(
        id=session_id,
        path=path,
        updated_at=state.updated_at or file_timestamp(path, created_at),
        cwd=state.cwd or cwd,
        name=state.name,
        title=state.title,
        thread_source=state.thread_source,
    )


def find_sessions(home: Path, reverse: bool = False) -> list[Session]:
    root = home / "sessions"
    if not root.is_dir():
        raise ExtractorError(f"Codex sessions directory not found: {root}")

    states = thread_states(home)
    states_by_path = {
        str(state.path.resolve()): state
        for state in states.values()
        if state.path is not None
    }
    sessions: list[Session] = []
    for path in root.rglob("*.jsonl"):
        try:
            session_id, created_at, cwd = read_meta(path)
        except ExtractorError as error:
            warning(str(error))
            continue
        session_id = session_id or fallback_session_id(path)
        state = states.get(session_id)
        if state is None:
            state = states_by_path.get(str(path.resolve()), ThreadState())
        sessions.append(
            Session(
                id=session_id,
                path=path,
                updated_at=state.updated_at or file_timestamp(path, created_at),
                cwd=state.cwd or cwd,
                name=state.name,
                title=state.title,
                thread_source=state.thread_source,
            )
        )
    return sorted(
        sessions,
        key=lambda item: (item.updated_at, str(item.path)),
        reverse=not reverse,
    )


def relative_time(value: str, now: datetime | None = None) -> str:
    try:
        timestamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value or "unknown"
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=timezone.utc)

    current = now or datetime.now(timezone.utc)
    if current.tzinfo is None:
        current = current.replace(tzinfo=timezone.utc)
    seconds = max(0, int((current - timestamp).total_seconds()))
    if seconds < 2:
        return "now"
    for unit_seconds, suffix in (
        (24 * 60 * 60, "d"),
        (60 * 60, "h"),
        (60, "m"),
        (1, "s"),
    ):
        if seconds >= unit_seconds:
            return f"{seconds // unit_seconds}{suffix} ago"
    return "now"


def clean_field(value: str) -> str:
    return "".join(character if character.isprintable() else " " for character in value)


def session_label(session: Session) -> str:
    """Return an explicit name, or a compact single-line automatic title."""
    if session.name:
        return session.name
    title = " ".join(session.title.split())
    if display_width(title) <= AUTOMATIC_TITLE_WIDTH:
        return title

    width = 0
    result: list[str] = []
    for character in title:
        character_width = display_width(character)
        if width + character_width > AUTOMATIC_TITLE_WIDTH - 1:
            break
        result.append(character)
        width += character_width
    return "".join(result).rstrip() + "…"


def pad_display(value: str, width: int) -> str:
    return value + " " * max(0, width - display_width(value))


def list_sessions(
    sessions: list[Session],
    now: datetime | None = None,
) -> None:
    current = now or datetime.now(timezone.utc)
    rows: list[tuple[str, str, str, str]] = []
    for session in sessions:
        # Internal guardian/worker rollouts have their own JSONL and database row,
        # but they are implementation details of the parent user thread.
        if session.thread_source == "subagent":
            continue
        label = session_label(session)
        if not label:
            continue
        updated_at = session.updated_at or file_timestamp(session.path)
        rows.append(
            tuple(
                clean_field(field)
                for field in (
                    relative_time(updated_at, current),
                    label,
                    session.id,
                    session.cwd,
                )
            )
        )

    last_width = max((display_width("LAST"), *(display_width(row[0]) for row in rows)))
    name_width = max((display_width("NAME"), *(display_width(row[1]) for row in rows)))
    uuid_width = max((display_width("UUID"), *(display_width(row[2]) for row in rows)))
    print(
        f"{pad_display('LAST', last_width)}  "
        f"{pad_display('NAME', name_width)}  "
        f"{pad_display('UUID', uuid_width)}  WORKDIR"
    )
    for last, name, session_id, workdir in rows:
        print(
            f"{pad_display(last, last_width)}  "
            f"{pad_display(name, name_width)}  "
            f"{pad_display(session_id, uuid_width)}  {workdir}"
        )


def unique_match(matches: list[Session], query: str) -> Session:
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        print(f"error: session selector {query!r} is ambiguous:", file=sys.stderr)
        for item in matches:
            print(f"  {item.id}\t{item.name}\t{item.path}", file=sys.stderr)
        raise ExtractorError("choose a full session ID or JSONL path", exit_code=2)
    raise ExtractorError(f"no session matches {query!r}", exit_code=2)


def select_session(sessions: list[Session], query: str) -> Session:
    selectors = (
        lambda item: item.id == query,
        lambda item: item.path.name == query,
        lambda item: item.name == query,
        lambda item: item.id.startswith(query),
    )
    for selector in selectors:
        matches = [item for item in sessions if selector(item)]
        if matches:
            return unique_match(matches, query)
    return unique_match([], query)


def resolve_session(home: Path, query: str) -> Session:
    candidate = Path(query).expanduser()
    if candidate.is_file():
        return session_from_path(candidate)
    return select_session(find_sessions(home), query)


TURN_END_EVENTS = frozenset({"task_complete", "turn_aborted"})


def item_text(item: dict) -> str:
    """Join an item's text parts without inventing content the item does not carry."""
    content = item.get("content")
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    return "".join(
        part["text"]
        for part in content
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _conversation_in_record_order(
    path: Path,
    final_only: bool,
    reverse: bool = False,
) -> Iterator[tuple[str, str]]:
    # Codex 0.157 writes the transcript as event_msg/item_completed, where the
    # visible text lives in payload.item.content[] and there is no "phase" field.
    # Older builds write event_msg/user_message plus event_msg/agent_message with
    # payload.message and payload.phase. Support both.
    #
    # Without "phase", the final answer of a turn is only identifiable from the
    # turn-ending event's last_agent_message, so --final-only resolves it at the
    # turn boundary instead of per message.
    last_agent: tuple[str, str] | None = None
    pending_final: dict[str, str] = {}
    fallback_turns: set[str] = set()
    emitted_turns: set[str] = set()

    for record in json_lines(path, reverse=reverse):
        if record.get("type") != "event_msg":
            continue
        payload = record.get("payload")
        if not isinstance(payload, dict):
            warning(f"skipping malformed event payload in {path}")
            continue
        event_type = payload.get("type")
        turn_id = payload.get("turn_id")
        turn_id = turn_id if isinstance(turn_id, str) else ""

        if event_type in ("user_message", "agent_message"):
            message = payload.get("message")
            if not isinstance(message, str) or not message.strip():
                continue
            if event_type == "user_message":
                yield "USER", message
            elif not final_only or payload.get("phase") in (None, "final_answer"):
                yield "CODEX", message
            continue

        if event_type in TURN_END_EVENTS:
            if not final_only:
                continue
            final = payload.get("last_agent_message")
            final = final if isinstance(final, str) else ""
            buffered = pending_final.pop(turn_id, "")
            chosen = final if final.strip() else buffered
            if chosen.strip() and turn_id not in emitted_turns:
                emitted_turns.add(turn_id)
                yield "CODEX", chosen
            elif not final.strip() and reverse:
                # Reverse order sees the turn end before the turn body, so an empty
                # last_agent_message defers to the turn's newest agent message.
                fallback_turns.add(turn_id)
            continue

        if event_type != "item_completed":
            continue
        item = payload.get("item")
        if not isinstance(item, dict):
            continue
        item_type = item.get("type")
        text = item_text(item)
        if not text.strip():
            continue

        if item_type == "UserMessage":
            if reverse and final_only and turn_id in pending_final and turn_id not in emitted_turns:
                emitted_turns.add(turn_id)
                yield "CODEX", pending_final.pop(turn_id)
            yield "USER", text
            continue
        if item_type != "AgentMessage":
            continue

        # A finalized message is re-emitted under the same id with only trailing
        # whitespace added; keep the first copy and drop the echo.
        message_id = item.get("id")
        message_id = message_id if isinstance(message_id, str) else ""
        if last_agent == (message_id, text.rstrip()):
            continue
        last_agent = (message_id, text.rstrip())

        if not final_only:
            yield "CODEX", text
        elif reverse:
            pending_final.setdefault(turn_id, text)
            if turn_id in fallback_turns and turn_id not in emitted_turns:
                fallback_turns.discard(turn_id)
                emitted_turns.add(turn_id)
                yield "CODEX", text
        else:
            pending_final[turn_id] = text

    for turn_id, text in pending_final.items():
        if turn_id not in emitted_turns:
            emitted_turns.add(turn_id)
            yield "CODEX", text


def conversation(
    path: Path,
    final_only: bool,
    reverse: bool = False,
) -> Iterator[tuple[str, str]]:
    messages = _conversation_in_record_order(path, final_only, reverse)
    if not reverse:
        yield from messages
        return

    # Read groups from newest to oldest. Consecutive user messages before an
    # answer belong to the same group, so restore their original order too.
    # Only one group's messages need to be held in memory.
    users: list[str] = []
    replies: list[str] = []
    for role, message in messages:
        if role == "USER":
            users.append(message)
        else:
            if users:
                for user in reversed(users):
                    yield "USER", user
                users.clear()
                for reply in reversed(replies):
                    yield "CODEX", reply
                replies.clear()
            replies.append(message)
    for user in reversed(users):
        yield "USER", user
    for reply in reversed(replies):
        yield "CODEX", reply


def tui_text(message: str) -> str:
    """Render searchable text close to Codex TUI's visible transcript."""
    text = message.rstrip()

    def replace_inline_code(match: re.Match[str]) -> str:
        left = " " if match.start() > 0 and not text[match.start() - 1].isspace() else ""
        right = " " if match.end() < len(text) and not text[match.end()].isspace() else ""
        return f"{left}{match.group('content')}{right}"

    return INLINE_CODE_SPAN.sub(replace_inline_code, text)


def display_width(text: str) -> int:
    width = 0
    for character in text:
        if unicodedata.combining(character):
            continue
        width += 2 if unicodedata.east_asian_width(character) in ("F", "W") else 1
    return width


def chinese_words(text: str) -> Iterator[str]:
    try:
        import jieba
    except ModuleNotFoundError as error:
        raise ExtractorError(
            "-w requires jieba; install it with "
            "'python3 -m pip install -r requirements.txt'"
        ) from error

    jieba.setLogLevel(logging.WARNING)
    words: list[str] = []
    for word in jieba.cut(text, HMM=False):
        if word in CLOSING_PUNCTUATION and words:
            words[-1] += word
        elif word:
            words.append(word)
    yield from words


def wrap_tokens(text: str) -> Iterator[str]:
    buffered: list[str] = []
    buffered_is_wide = False

    def flush() -> Iterator[str]:
        nonlocal buffered_is_wide
        if buffered:
            value = "".join(buffered)
            buffered.clear()
            if buffered_is_wide:
                yield from chinese_words(value)
            else:
                yield value
            buffered_is_wide = False

    for character in text.expandtabs(4):
        if character.isspace():
            yield from flush()
            yield " "
        else:
            is_wide = unicodedata.east_asian_width(character) in ("F", "W")
            if buffered and is_wide != buffered_is_wide:
                yield from flush()
            buffered_is_wide = is_wide
            buffered.append(character)
    yield from flush()


def wrap_line(text: str, width: int) -> list[str]:
    if not text:
        return [""]

    lines: list[str] = []
    current = ""
    pending_space = ""
    for token in wrap_tokens(text):
        if token == " ":
            if current:
                pending_space = " "
            continue
        separator = pending_space if current else ""
        candidate = current + separator + token
        if current and display_width(candidate) > width:
            lines.append(current)
            current = token
        else:
            current = candidate
        pending_space = ""
    if current:
        lines.append(current)
    return lines or [""]


def output_width(value: str) -> int:
    try:
        width = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("width must be an integer") from error
    if width < 3:
        raise argparse.ArgumentTypeError("width must be at least 3")
    return width


def print_message(role: str, message: str, width: int | None) -> None:
    marker = "› " if role == "USER" else "• "
    continuation = " " * display_width(marker)
    content_width = width - display_width(marker) if width is not None else None
    first = True
    for source_line in tui_text(message).split("\n"):
        should_wrap = content_width is not None and display_width(source_line) > content_width
        lines = wrap_line(source_line, content_width) if should_wrap else [source_line]
        for line in lines:
            print(f"{marker if first else continuation}{line}")
            first = False


def extract(
    session: Session,
    final_only: bool,
    width: int | None,
    reverse: bool,
) -> None:
    first = True
    for role, message in conversation(session.path, final_only, reverse=reverse):
        if not first:
            print()
        print_message(role, message, width)
        first = False


def pager_command() -> list[str] | None:
    value = os.environ.get("PAGER", "less").strip()
    if not value:
        return None
    try:
        command = shlex.split(value)
    except ValueError as error:
        raise ExtractorError(f"invalid PAGER value: {error}") from error
    if not command or shutil.which(command[0]) is None:
        return None
    return command


@contextlib.contextmanager
def pager_output() -> Iterator[None]:
    if not sys.stdout.isatty():
        try:
            yield
        except BrokenPipeError:
            with contextlib.suppress(BrokenPipeError):
                sys.stdout.close()
        return
    command = pager_command()
    if command is None:
        yield
        return

    environment = os.environ.copy()
    if command[0].rsplit("/", 1)[-1] == "less":
        environment.setdefault("LESS", "FRX")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            text=True,
            encoding=sys.stdout.encoding or "utf-8",
            errors="replace",
            env=environment,
        )
    except OSError as error:
        warning(f"cannot start pager {command[0]!r}: {error}")
        yield
        return

    original_stdout = sys.stdout
    assert process.stdin is not None
    sys.stdout = process.stdin
    try:
        yield
    except BrokenPipeError:
        pass
    finally:
        sys.stdout = original_stdout
        try:
            process.stdin.close()
        except BrokenPipeError:
            pass
        process.wait()


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(
        description="List local Codex sessions or extract a session's conversation."
    )
    result.add_argument(
        "--codex-home",
        metavar="DIR",
        help="Codex data directory (default: $CODEX_HOME or ~/.codex)",
    )
    sub = result.add_subparsers(dest="command", required=True)
    list_parser = sub.add_parser("list", help="list named sessions, most recent first")
    list_parser.add_argument(
        "--reverse",
        action="store_true",
        help="show sessions in reverse order (oldest first)",
    )
    extract_parser = sub.add_parser("extract", help="print one session's conversation")
    extract_parser.add_argument("session", help="full/unique ID prefix, exact name, or JSONL path")
    extract_parser.add_argument(
        "--final-only",
        action="store_true",
        help="omit Codex progress/commentary messages",
    )
    extract_parser.add_argument(
        "--reverse",
        action="store_true",
        help="show conversation groups newest first, keeping messages in order",
    )
    extract_parser.add_argument(
        "-w",
        "--width",
        type=output_width,
        metavar="COLUMNS",
        help="hard-wrap output to this terminal display width",
    )
    return result


def main() -> None:
    args = parser().parse_args()
    home = codex_home(args.codex_home)
    try:
        with pager_output():
            if args.command == "list":
                list_sessions(find_sessions(home, args.reverse))
            else:
                extract(
                    resolve_session(home, args.session),
                    args.final_only,
                    args.width,
                    args.reverse,
                )
    except ExtractorError as error:
        print(f"error: {error}", file=sys.stderr)
        raise SystemExit(error.exit_code) from error


if __name__ == "__main__":
    main()
