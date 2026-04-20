"""Tests for tools/session_recap_tool.py."""

import json
from unittest.mock import AsyncMock, MagicMock, patch

from tools.session_recap_tool import _parse_time_boundary, session_recap


class TestParseTimeBoundary:
    def test_parses_epoch_number(self):
        assert _parse_time_boundary(1700000000, "window_start") == 1700000000.0

    def test_parses_epoch_string(self):
        assert _parse_time_boundary("1700000000", "window_end") == 1700000000.0

    def test_parses_iso_string(self):
        ts = _parse_time_boundary("2026-04-18T12:00:00", "window_start")
        assert isinstance(ts, float)

    def test_date_only_end_boundary_maps_to_end_of_day(self):
        start = _parse_time_boundary("2026-04-18", "window_start")
        end = _parse_time_boundary("2026-04-18", "window_end")
        assert end > start
        assert end - start >= 86399

    def test_invalid_input_returns_none(self):
        assert _parse_time_boundary("not-a-time", "window_start") is None

    def test_unsupported_type_returns_none(self):
        assert _parse_time_boundary({"not": "valid"}, "window_end") is None


class TestSessionRecap:
    def test_no_db_returns_error(self):
        result = json.loads(session_recap())
        assert result["success"] is False
        assert "not available" in result["error"].lower()

    def test_invalid_window_input_is_ignored(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = []
        result = json.loads(session_recap(db=mock_db, window_start="bad-time"))
        assert result["success"] is True

    def test_time_window_mode_uses_window_queries(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = [
            {
                "id": "child_1",
                "source": "cli",
                "model": "test-model",
                "first_in_window": 1700000100,
                "last_in_window": 1700000200,
                "messages_in_window": 1,
            }
        ]

        def _get_session(session_id):
            if session_id == "child_1":
                return {"parent_session_id": "root_1", "source": "cli", "model": "test-model"}
            if session_id == "root_1":
                return {"parent_session_id": None, "source": "cli", "model": "test-model"}
            if session_id == "current_sid":
                return {"parent_session_id": None, "source": "cli", "model": "active-model"}
            return None

        def _window_messages(session_id, start_time, end_time):
            if session_id == "child_1":
                return [
                    {
                        "id": 1,
                        "role": "user",
                        "content": "Discussed deploy blockers",
                        "timestamp": 1700000150,
                    }
                ]
            return []

        mock_db.get_session.side_effect = _get_session
        mock_db.get_messages_in_time_window.side_effect = _window_messages

        with patch(
            "tools.session_recap_tool.async_call_llm",
            new_callable=AsyncMock,
            side_effect=RuntimeError("no provider"),
        ) as mock_async_call_llm:
            result = json.loads(
                session_recap(
                    db=mock_db,
                    current_session_id="current_sid",
                    window_start=1700000000,
                    window_end=1700000300,
                    limit=3,
                )
            )

        assert result["success"] is True
        assert result["mode"] == "time_window_recap"
        assert mock_db.list_sessions_with_messages_in_time_window.call_count == 1
        assert mock_db.list_sessions_with_messages_in_time_window.call_args.kwargs["include_children"] is True
        assert mock_async_call_llm.call_args.kwargs["task"] == "session_recap"
        session_ids = {r["session_id"] for r in result["results"]}
        assert "root_1" in session_ids
        assert "current_sid" in session_ids

    def test_include_current_false_does_not_force_current_session(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = []
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli", "model": "m"}
        mock_db.get_messages_in_time_window.return_value = []

        result = json.loads(
            session_recap(
                db=mock_db,
                current_session_id="current_sid",
                include_current=False,
                window_start=1700000000,
                window_end=1700000300,
            )
        )

        assert result["success"] is True
        assert result["count"] == 0
        assert result["message"] == "No messages found in the selected time window."

    def test_include_current_true_with_no_window_messages_has_no_results_message(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = []
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli", "model": "m"}
        mock_db.get_messages_in_time_window.return_value = []

        result = json.loads(
            session_recap(
                db=mock_db,
                current_session_id="current_sid",
                include_current=True,
                window_start=1700000000,
                window_end=1700000300,
            )
        )

        assert result["success"] is True
        assert result["count"] == 1
        assert result["message"] == "No messages found in the selected time window."
        assert result["results"][0]["window_message_count"] == 0

    def test_compact_range_phrase_is_accepted(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = []
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli", "model": "m"}
        mock_db.get_messages_in_time_window.return_value = []

        result = json.loads(
            session_recap(
                db=mock_db,
                include_current=False,
                window_start="between 2-4pm",
                window_end="between 2-4pm",
            )
        )

        assert result["success"] is True
        assert result["count"] == 0

    def test_defaults_to_last_24_hours_when_no_window_is_provided(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = []
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli", "model": "m"}
        mock_db.get_messages_in_time_window.return_value = []

        fixed_now = 1_000_000.0
        with patch("tools.session_recap_tool.time.time", return_value=fixed_now):
            result = json.loads(
                session_recap(
                    db=mock_db,
                    include_current=False,
                )
            )

        assert result["success"] is True
        kwargs = mock_db.list_sessions_with_messages_in_time_window.call_args.kwargs
        assert kwargs["start_time"] == fixed_now - 86400.0
        assert kwargs["end_time"] == fixed_now

    def test_absolute_window_uses_explicit_boundaries(self):
        mock_db = MagicMock()
        mock_db.list_sessions_with_messages_in_time_window.return_value = []
        mock_db.get_session.return_value = {"parent_session_id": None, "source": "cli", "model": "m"}
        mock_db.get_messages_in_time_window.return_value = []

        result = json.loads(
            session_recap(
                db=mock_db,
                include_current=False,
                window_start=1000,
                window_end=2000,
            )
        )

        assert result["success"] is True
        kwargs = mock_db.list_sessions_with_messages_in_time_window.call_args.kwargs
        assert kwargs["start_time"] == 1000.0
        assert kwargs["end_time"] == 2000.0
