#!/usr/bin/env python3
"""Session Recap Tool - Time-window conversation recap.

Creates recaps for sessions that have messages inside a given time window.
Unlike session_search, this tool is purpose-built for interval recaps and does
not perform keyword-driven FTS ranking.
"""

import asyncio
import concurrent.futures
import json
import logging
import time
from typing import Any, Dict, List, Optional, Union

from agent.auxiliary_client import async_call_llm, extract_content_or_reasoning

MAX_SESSION_CHARS = 100_000
MAX_SUMMARY_TOKENS = 10000
MAX_MESSAGES_PER_SESSION = 300
_DEFAULT_RECAP_LIMIT = 3
_MAX_RECAP_LIMIT = 10

# Keep recap aligned with session_search visibility defaults.
_HIDDEN_SESSION_SOURCES = ("tool",)


def _localize_naive_datetime(dt):
    """Apply local timezone to naive datetimes."""
    from datetime import datetime

    if dt.tzinfo is None:
        local_tz = datetime.now().astimezone().tzinfo
        dt = dt.replace(tzinfo=local_tz)
    return dt


def _is_iso_date_only(value: str) -> bool:
    """Return True for YYYY-MM-DD strings."""
    if len(value) != 10:
        return False
    if value[4] != "-" or value[7] != "-":
        return False
    return value[:4].isdigit() and value[5:7].isdigit() and value[8:10].isdigit()


def _parse_time_boundary(value: Union[int, float, str, None], field_name: str) -> Optional[float]:
    """Parse epoch/ISO inputs into Unix timestamps.

    Naive ISO strings are interpreted in local timezone.
    Invalid values are ignored (return None) instead of raising.
    """
    if value is None:
        return None

    if isinstance(value, (int, float)):
        return float(value)

    if isinstance(value, str):
        raw = value.strip()
        if not raw:
            return None

        if raw.replace(".", "", 1).isdigit() or (
            raw.startswith("-") and raw[1:].replace(".", "", 1).isdigit()
        ):
            return float(raw)

        from datetime import datetime, timedelta

        try:
            iso = raw.replace("Z", "+00:00")
            dt = datetime.fromisoformat(iso)
            if field_name == "window_end" and _is_iso_date_only(raw):
                dt = dt + timedelta(days=1) - timedelta(seconds=1)
            return _localize_naive_datetime(dt).timestamp()
        except ValueError:
            return None

    return None


def _format_timestamp(ts: Union[int, float, str, None]) -> str:
    """Convert a Unix timestamp (float/int) or ISO string to a human-readable date."""
    if ts is None:
        return "unknown"
    try:
        if isinstance(ts, (int, float)):
            from datetime import datetime

            dt = datetime.fromtimestamp(ts)
            return dt.strftime("%B %d, %Y at %I:%M %p")
        if isinstance(ts, str):
            if ts.replace(".", "").replace("-", "").isdigit():
                from datetime import datetime

                dt = datetime.fromtimestamp(float(ts))
                return dt.strftime("%B %d, %Y at %I:%M %p")
            return ts
    except (ValueError, OSError, OverflowError) as e:
        logging.debug("Failed to format timestamp %s: %s", ts, e, exc_info=True)
    except Exception as e:
        logging.debug("Unexpected error formatting timestamp %s: %s", ts, e, exc_info=True)
    return str(ts)


def _format_conversation(messages: List[Dict[str, Any]]) -> str:
    """Format messages into a readable transcript for summarization."""
    parts = []
    for msg in messages:
        role = msg.get("role", "unknown").upper()
        content = msg.get("content") or ""
        tool_name = msg.get("tool_name")

        if role == "TOOL" and tool_name:
            if len(content) > 500:
                content = content[:250] + "\n...[truncated]...\n" + content[-250:]
            parts.append(f"[TOOL:{tool_name}]: {content}")
        elif role == "ASSISTANT":
            tool_calls = msg.get("tool_calls")
            if tool_calls and isinstance(tool_calls, list):
                tc_names = []
                for tc in tool_calls:
                    if isinstance(tc, dict):
                        name = tc.get("name") or tc.get("function", {}).get("name", "?")
                        tc_names.append(name)
                if tc_names:
                    parts.append(f"[ASSISTANT]: [Called: {', '.join(tc_names)}]")
                if content:
                    parts.append(f"[ASSISTANT]: {content}")
            else:
                parts.append(f"[ASSISTANT]: {content}")
        else:
            parts.append(f"[{role}]: {content}")

    return "\n\n".join(parts)


def _resolve_to_parent_session(db, session_id: Optional[str]) -> Optional[str]:
    """Walk delegation/compression lineage to its root session."""
    if not session_id:
        return session_id

    visited = set()
    sid = session_id
    while sid and sid not in visited:
        visited.add(sid)
        try:
            session = db.get_session(sid)
            if not session:
                break
            parent = session.get("parent_session_id")
            sid = parent if parent else sid
            if not parent:
                break
        except Exception as e:
            logging.debug(
                "Error resolving parent for session %s: %s",
                sid,
                e,
                exc_info=True,
            )
            break
    return sid


def _clip_messages_for_budget(messages: List[Dict[str, Any]]) -> tuple[List[Dict[str, Any]], bool]:
    """Keep a deterministic head/tail slice for large windows."""
    if len(messages) <= MAX_MESSAGES_PER_SESSION:
        return messages, False

    head = MAX_MESSAGES_PER_SESSION // 2
    tail = MAX_MESSAGES_PER_SESSION - head
    return messages[:head] + messages[-tail:], True


def _truncate_text_for_budget(text: str, max_chars: int = MAX_SESSION_CHARS) -> tuple[str, bool]:
    """Bound transcript size while preserving early and late context."""
    if len(text) <= max_chars:
        return text, False

    head_len = int(max_chars * 0.25)
    tail_len = max_chars - head_len
    truncated = (
        text[:head_len]
        + "\n\n...[middle conversation truncated]...\n\n"
        + text[-tail_len:]
    )
    return truncated, True


async def _summarize_recap_window_session(
    conversation_text: str,
    session_meta: Dict[str, Any],
    window_start: float,
    window_end: float,
    recap_focus: Optional[str] = None,
) -> Optional[str]:
    """Summarize messages that fall inside a requested time window."""
    focus_line = (
        f"Primary focus: {recap_focus}\n"
        if recap_focus and recap_focus.strip()
        else ""
    )

    system_prompt = (
        "You are writing a time-window recap from a conversation transcript slice. "
        "Only summarize what appears in this provided transcript window. Include:\n"
        "1. Key user goals or requests in this window\n"
        "2. Important actions taken and outcomes\n"
        "3. Decisions, blockers, and unresolved items\n"
        "4. Specific technical artifacts (commands, files, errors, URLs) that matter\n\n"
        "Be concise but specific. Do not infer details outside the provided transcript."
    )

    source = session_meta.get("source", "unknown")
    started = _format_timestamp(session_meta.get("started_at"))

    user_prompt = (
        f"Window start: {_format_timestamp(window_start)}\n"
        f"Window end: {_format_timestamp(window_end)}\n"
        f"Session source: {source}\n"
        f"Session started: {started}\n"
        f"{focus_line}\n"
        f"TRANSCRIPT WINDOW:\n{conversation_text}"
    )

    max_retries = 3
    for attempt in range(max_retries):
        try:
            response = await async_call_llm(
                task="session_recap",
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.1,
                max_tokens=MAX_SUMMARY_TOKENS,
            )
            content = extract_content_or_reasoning(response)
            if content:
                return content

            logging.warning(
                "Session recap LLM returned empty content (attempt %d/%d)",
                attempt + 1,
                max_retries,
            )
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
                continue
            return content
        except RuntimeError:
            logging.warning("No auxiliary model available for session recap summarization")
            return None
        except Exception as e:
            if attempt < max_retries - 1:
                await asyncio.sleep(1 * (attempt + 1))
            else:
                logging.warning(
                    "Session recap summarization failed after %d attempts: %s",
                    max_retries,
                    e,
                    exc_info=True,
                )
                return None


def session_recap(
    window_start: Union[int, float, str, None] = None,
    window_end: Union[int, float, str, None] = None,
    recap_focus: str = None,
    include_current: bool = True,
    limit: int = _DEFAULT_RECAP_LIMIT,
    db=None,
    current_session_id: str = None,
) -> str:
    """Create a recap for sessions with activity in a specified time window."""
    if db is None:
        return tool_error("Session database not available.", success=False)

    if not isinstance(limit, int):
        try:
            limit = int(limit)
        except (TypeError, ValueError):
            limit = _DEFAULT_RECAP_LIMIT
    limit = max(1, min(limit, _MAX_RECAP_LIMIT))

    parsed_start = _parse_time_boundary(window_start, "window_start")
    parsed_end = _parse_time_boundary(window_end, "window_end")
    now_ts = time.time()

    if parsed_end is None:
        parsed_end = now_ts
    if parsed_start is None:
        parsed_start = parsed_end - 86400.0
    if parsed_end < parsed_start:
        parsed_start, parsed_end = parsed_end, parsed_start

    candidate_rows = db.list_sessions_with_messages_in_time_window(
        start_time=parsed_start,
        end_time=parsed_end,
        exclude_sources=list(_HIDDEN_SESSION_SOURCES),
        include_children=True,
        limit=max(100, limit * 20),
        offset=0,
    )

    grouped: Dict[str, Dict[str, Any]] = {}
    for row in candidate_rows:
        sid = row.get("id")
        if not sid:
            continue

        root_sid = _resolve_to_parent_session(db, sid)
        if not root_sid:
            continue

        messages = db.get_messages_in_time_window(
            sid,
            start_time=parsed_start,
            end_time=parsed_end,
        )
        if root_sid not in grouped:
            root_meta = db.get_session(root_sid) or {}
            grouped[root_sid] = {
                "session_id": root_sid,
                "source": root_meta.get("source") or row.get("source") or "unknown",
                "model": root_meta.get("model") or row.get("model"),
                "lineage_session_ids": [],
                "messages": [],
                "message_ids": set(),
            }

        group = grouped[root_sid]
        if sid not in group["lineage_session_ids"]:
            group["lineage_session_ids"].append(sid)

        for msg in messages:
            msg_id = msg.get("id")
            if msg_id in group["message_ids"]:
                continue
            group["message_ids"].add(msg_id)
            group["messages"].append(msg)

    current_root = _resolve_to_parent_session(db, current_session_id) if current_session_id else None
    if include_current and current_root:
        group = grouped.get(current_root)
        if group is None:
            meta = db.get_session(current_root) or {}
            group = {
                "session_id": current_root,
                "source": meta.get("source", "unknown"),
                "model": meta.get("model"),
                "lineage_session_ids": [current_root],
                "messages": [],
                "message_ids": set(),
            }
            grouped[current_root] = group

        # Ensure active session fragment is represented in-window if present.
        if current_session_id and current_session_id not in group["lineage_session_ids"]:
            group["lineage_session_ids"].append(current_session_id)
        if current_session_id:
            current_msgs = db.get_messages_in_time_window(
                current_session_id,
                start_time=parsed_start,
                end_time=parsed_end,
            )
            for msg in current_msgs:
                msg_id = msg.get("id")
                if msg_id in group["message_ids"]:
                    continue
                group["message_ids"].add(msg_id)
                group["messages"].append(msg)

    for group in grouped.values():
        group["messages"].sort(key=lambda m: (m.get("timestamp", 0), m.get("id", 0)))

    ranked_roots = sorted(
        grouped.keys(),
        key=lambda sid: grouped[sid]["messages"][-1].get("timestamp", 0)
        if grouped[sid]["messages"]
        else 0,
        reverse=True,
    )

    selected_roots: List[str] = []
    if include_current and current_root and current_root in grouped:
        selected_roots.append(current_root)
    for sid in ranked_roots:
        if sid in selected_roots:
            continue
        selected_roots.append(sid)
        if len(selected_roots) >= limit:
            break

    tasks = []
    for sid in selected_roots:
        group = grouped[sid]
        clipped_messages, clipped_by_count = _clip_messages_for_budget(group["messages"])
        conversation_text = _format_conversation(clipped_messages)
        conversation_text, clipped_by_chars = _truncate_text_for_budget(conversation_text)

        tasks.append(
            {
                "session_id": sid,
                "source": group["source"],
                "model": group["model"],
                "lineage_session_ids": group["lineage_session_ids"],
                "messages": group["messages"],
                "conversation_text": conversation_text,
                "clipped": clipped_by_count or clipped_by_chars,
            }
        )

    async def _summarize_all() -> List[Union[str, Exception]]:
        coros = []
        for task in tasks:
            if not task["messages"]:
                continue
            coros.append(
                _summarize_recap_window_session(
                    conversation_text=task["conversation_text"],
                    session_meta={"source": task["source"], "started_at": task["messages"][0].get("timestamp")},
                    window_start=parsed_start,
                    window_end=parsed_end,
                    recap_focus=recap_focus,
                )
            )
        return await asyncio.gather(*coros, return_exceptions=True)

    summary_by_session: Dict[str, Optional[str]] = {}
    if any(task["messages"] for task in tasks):
        try:
            from model_tools import _run_async

            results = _run_async(_summarize_all())
        except concurrent.futures.TimeoutError:
            logging.warning("Session recap summarization timed out after 60 seconds", exc_info=True)
            return json.dumps(
                {
                    "success": False,
                    "error": "Session recap timed out. Try a smaller time window or lower limit.",
                },
                ensure_ascii=False,
            )

        result_index = 0
        for task in tasks:
            if not task["messages"]:
                continue
            result = results[result_index]
            result_index += 1

            if isinstance(result, Exception):
                logging.warning(
                    "Failed to summarize recap session %s: %s",
                    task["session_id"],
                    result,
                    exc_info=True,
                )
                result = None

            if result:
                summary_by_session[task["session_id"]] = result
            else:
                preview = task["conversation_text"][:500]
                if len(task["conversation_text"]) > 500:
                    preview += "\n...[truncated]"
                summary_by_session[task["session_id"]] = (
                    "[Raw preview - summarization unavailable]\n" + preview
                )

    entries = []
    for task in tasks:
        messages = task["messages"]
        if messages:
            first_ts = messages[0].get("timestamp")
            last_ts = messages[-1].get("timestamp")
            summary = summary_by_session.get(task["session_id"], "Summary unavailable.")
        else:
            first_ts = None
            last_ts = None
            summary = "No messages from this session fell inside the selected time window."

        entries.append(
            {
                "session_id": task["session_id"],
                "source": task["source"],
                "model": task["model"],
                "window_message_count": len(messages),
                "window_first_message": _format_timestamp(first_ts),
                "window_last_message": _format_timestamp(last_ts),
                "lineage_session_ids": task["lineage_session_ids"],
                "truncated_for_budget": task["clipped"],
                "summary": summary,
            }
        )

    total_window_messages = sum(entry["window_message_count"] for entry in entries)
    sessions_with_messages = sum(1 for entry in entries if entry["window_message_count"] > 0)
    if total_window_messages == 0:
        recap_message = "No messages found in the selected time window."
    else:
        recap_message = (
            f"Found {total_window_messages} messages across "
            f"{sessions_with_messages} session(s) in the selected time window."
        )

    return json.dumps(
        {
            "success": True,
            "mode": "time_window_recap",
            "window_start": _format_timestamp(parsed_start),
            "window_end": _format_timestamp(parsed_end),
            "count": len(entries),
            "sessions_considered": len(grouped),
            "message": recap_message,
            "results": entries,
        },
        ensure_ascii=False,
    )


def check_session_recap_requirements() -> bool:
    """Requires SQLite state database and an auxiliary text model."""
    try:
        from hermes_state import DEFAULT_DB_PATH

        return DEFAULT_DB_PATH.parent.exists()
    except ImportError:
        return False


SESSION_RECAP_SCHEMA = {
    "name": "session_recap",
    "description": (
        "Create a recap of sessions active within a requested time window. "
        "This tool is for interval-based recap rather than keyword lookup, and "
        "summarizes only messages whose timestamps fall inside the resolved window."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "window_start": {
                "type": ["number", "string"],
                "description": (
                    "Optional recap window start time. Preferred formats: "
                    "Unix seconds (number) or ISO-8601 string like "
                    "'2026-04-19T16:00:00' (or with timezone, e.g. '2026-04-19T16:00:00-07:00'). "
                    "If omitted, defaults to 24 hours before window_end."
                ),
            },
            "window_end": {
                "type": ["number", "string"],
                "description": (
                    "Optional recap window end time. Preferred formats: Unix seconds (number) "
                    "or ISO-8601 string like '2026-04-19T17:00:00'. "
                    "Date-only values like '2026-04-19' map to end-of-day local time. "
                    "If omitted, defaults to current time."
                ),
            },
            "recap_focus": {
                "type": "string",
                "description": "Optional focus area for the recap, e.g. 'deployment blockers' or 'product decisions'.",
            },
            "include_current": {
                "type": "boolean",
                "description": "Whether to always include the active current session in results (default: true).",
                "default": True,
            },
            "limit": {
                "type": "integer",
                "description": "Maximum number of session summaries to return (default: 3, max: 10).",
                "default": _DEFAULT_RECAP_LIMIT,
            },
        },
        "required": [],
    },
}


from tools.registry import registry, tool_error

registry.register(
    name="session_recap",
    toolset="session_recap",
    schema=SESSION_RECAP_SCHEMA,
    handler=lambda args, **kw: session_recap(
        window_start=args.get("window_start"),
        window_end=args.get("window_end"),
        recap_focus=args.get("recap_focus"),
        include_current=args.get("include_current", True),
        limit=args.get("limit", _DEFAULT_RECAP_LIMIT),
        db=kw.get("db"),
        current_session_id=kw.get("current_session_id"),
    ),
    check_fn=check_session_recap_requirements,
    emoji="",
)
