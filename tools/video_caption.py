"""Bilingual video captioning tool for Hermes Agent.

Transcribes English audio with faster-whisper, translates to Vietnamese via
Kimi K2.5 (NVIDIA NIM), builds a styled ASS subtitle file, and burns the
captions into the video with FFmpeg.

Required dependencies (install in the Hermes venv):
    pip install faster-whisper

Required env var for Kimi translation (add to ~/.hermes/.env):
    NVIDIA_API_KEY=nvapi-...

FFmpeg must be installed system-wide:
    macOS: brew install ffmpeg
    Linux: sudo apt install ffmpeg
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any

from tools.registry import registry
from hermes_constants import get_hermes_home

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".ts", ".mts"}
_ASS_COLOR_RE = re.compile(r"^&H[0-9A-Fa-f]{8}$")

_DEFAULT_STYLE = {
    "font": "Arial",
    "font_size": 48,
    "primary_color": "&H00FFFFFF",   # white   (ASS: &HAABBGGRR)
    "outline_color": "&H00000000",   # black
    "outline_width": 3,
    "alignment": 2,                  # 2 = bottom-center (ASS numpad alignment)
    "margin_bottom": 80,
    "max_line_length": 42,
}


def _load_style() -> dict:
    """Load caption style from config, falling back to built-in defaults."""
    try:
        from hermes_cli.config import load_config
        cfg = load_config()
        user_style = cfg.get("caption", {}).get("style", {})
        style = {**_DEFAULT_STYLE, **{k: v for k, v in user_style.items() if v is not None}}
    except Exception:
        style = dict(_DEFAULT_STYLE)
    return style


def _ass_color(hex_color: str) -> str:
    """Ensure hex_color is in ASS &HAABBGGRR format; pass through if already valid."""
    if _ASS_COLOR_RE.match(hex_color):
        return hex_color
    # Accept #RRGGBB shorthand — convert to &H00BBGGRR
    m = re.match(r"^#([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})([0-9A-Fa-f]{2})$", hex_color)
    if m:
        r, g, b = m.group(1), m.group(2), m.group(3)
        return f"&H00{b}{g}{r}".upper()
    return "&H00FFFFFF"


def _seconds_to_ass_time(seconds: float) -> str:
    """Convert float seconds to ASS timestamp H:MM:SS.cs"""
    cs = int(round(seconds * 100)) % 100
    total_s = int(seconds)
    s = total_s % 60
    total_m = total_s // 60
    m = total_m % 60
    h = total_m // 60
    return f"{h}:{m:02d}:{s:02d}.{cs:02d}"


def _wrap_text(text: str, max_len: int) -> str:
    """Wrap text to max_len characters with \\N (ASS hard break)."""
    if len(text) <= max_len:
        return text
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip() if current else word
        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return r"\N".join(lines)


def _build_ass_content(segments: list[dict], style: dict) -> str:
    """Build a complete ASS subtitle file with stacked EN (top) + VI (bottom) lines."""
    font = style["font"]
    size_en = int(style["font_size"])
    size_vi = max(int(size_en * 0.9), 28)
    primary = _ass_color(style.get("primary_color", "&H00FFFFFF"))
    outline = _ass_color(style.get("outline_color", "&H00000000"))
    outline_w = int(style.get("outline_width", 3))
    alignment = int(style.get("alignment", 2))
    margin_v = int(style.get("margin_bottom", 80))
    max_len = int(style.get("max_line_length", 42))

    # Script header
    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        "PlayResX: 1920\n"
        "PlayResY: 1080\n"
        "ScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
    )

    # English style — sits above Vietnamese
    en_margin_v = margin_v + size_vi + 12
    style_en = (
        f"Style: EN,{font},{size_en},{primary},&H000000FF,{outline},&H80000000,"
        f"-1,0,0,0,100,100,0,0,1,{outline_w},1,{alignment},10,10,{en_margin_v},1\n"
    )
    # Vietnamese style — sits at the base
    style_vi = (
        f"Style: VI,{font},{size_vi},{primary},&H000000FF,{outline},&H80000000,"
        f"0,0,0,0,100,100,0,0,1,{outline_w},1,{alignment},10,10,{margin_v},1\n"
    )

    events_header = (
        "\n[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )

    dialogue_lines: list[str] = []
    for seg in segments:
        start = _seconds_to_ass_time(float(seg["start"]))
        end = _seconds_to_ass_time(float(seg["end"]))
        en_text = _wrap_text(seg.get("en", "").strip(), max_len)
        vi_text = _wrap_text(seg.get("vi", "").strip(), max_len)
        if en_text:
            dialogue_lines.append(f"Dialogue: 0,{start},{end},EN,,0,0,0,,{en_text}")
        if vi_text:
            dialogue_lines.append(f"Dialogue: 0,{start},{end},VI,,0,0,0,,{vi_text}")

    return header + style_en + style_vi + events_header + "\n".join(dialogue_lines) + "\n"


# ---------------------------------------------------------------------------
# Core operations
# ---------------------------------------------------------------------------

def transcribe(video_path: str) -> list[dict]:
    """Transcribe English audio from *video_path* using faster-whisper.

    Returns a list of segment dicts: {start, end, en}.
    """
    try:
        from faster_whisper import WhisperModel  # type: ignore
    except ImportError:
        raise RuntimeError(
            "faster-whisper is not installed. "
            "Run: pip install faster-whisper"
        )

    # Use medium model by default — good balance of speed and accuracy.
    # Override with HERMES_WHISPER_MODEL env var for large-v3 or tiny.
    model_name = os.getenv("HERMES_WHISPER_MODEL", "medium")
    device = "cpu"
    compute = "int8"

    logger.info("Loading faster-whisper model: %s", model_name)
    model = WhisperModel(model_name, device=device, compute_type=compute)

    logger.info("Transcribing: %s", video_path)
    segments_raw, _info = model.transcribe(
        video_path,
        language="en",
        vad_filter=True,
        word_timestamps=False,
    )

    segments: list[dict] = []
    for seg in segments_raw:
        text = seg.text.strip()
        if text:
            segments.append({
                "id": len(segments),
                "start": round(float(seg.start), 3),
                "end": round(float(seg.end), 3),
                "en": text,
                "vi": "",
            })
    return segments


def translate_to_vietnamese(segments: list[dict], api_key: str | None = None) -> list[dict]:
    """Translate English captions to Vietnamese using Kimi K2.5 via NVIDIA NIM.

    Falls back to returning empty VI strings if the API key is not available,
    so the pipeline can still produce English-only output.
    """
    _api_key = api_key or os.getenv("NVIDIA_API_KEY", "")
    if not _api_key:
        logger.warning(
            "NVIDIA_API_KEY not set — skipping Vietnamese translation. "
            "Add it to ~/.hermes/.env to enable Kimi K2.5 translation."
        )
        return segments

    # Build numbered list of lines to translate in one call
    lines = [f"{i+1}. {seg['en']}" for i, seg in enumerate(segments)]
    source_text = "\n".join(lines)

    prompt = (
        "You are a professional subtitler translating English captions to Vietnamese "
        "for a bilingual video. Translate each numbered line, preserving the same "
        "numbering format. Keep the translations natural and concise — these are "
        "captions, not subtitles for speech, so brevity matters. "
        "Output ONLY the numbered Vietnamese translations, one per line.\n\n"
        f"{source_text}"
    )

    try:
        import openai  # type: ignore
    except ImportError:
        logger.warning("openai package not installed — skipping translation. Run: pip install openai")
        return segments

    try:
        client = openai.OpenAI(
            api_key=_api_key,
            base_url="https://integrate.api.nvidia.com/v1",
        )
        response = client.chat.completions.create(
            model="moonshotai/kimi-k2.5",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2048,
        )

        # Handle the known NVIDIA NIM quirk where Kimi puts output in
        # reasoning_content instead of content when extended thinking is on.
        choice = response.choices[0]
        raw = getattr(choice.message, "content", None) or ""
        if not raw.strip():
            raw = getattr(choice.message, "reasoning_content", None) or ""

    except Exception as e:
        logger.warning("Kimi translation API call failed: %s — continuing without translation", e)
        return segments

    # Parse numbered lines back into the segments list
    result_segments = [dict(s) for s in segments]
    for line in raw.strip().splitlines():
        m = re.match(r"^(\d+)\.\s+(.+)$", line.strip())
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(result_segments):
                result_segments[idx]["vi"] = m.group(2).strip()

    return result_segments


def build_ass(segments: list[dict], output_path: str | None = None) -> str:
    """Build an ASS subtitle file from *segments* and write it to *output_path*.

    Returns the path to the written file.
    """
    style = _load_style()
    content = _build_ass_content(segments, style)

    if output_path is None:
        cache_dir = get_hermes_home() / "cache" / "captions"
        cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(cache_dir / f"captions_{uuid.uuid4().hex[:8]}.ass")

    Path(output_path).write_text(content, encoding="utf-8")
    logger.info("ASS subtitle file written: %s", output_path)
    return output_path


def burn(video_path: str, ass_path: str, output_path: str | None = None) -> str:
    """Burn ASS subtitles into *video_path* using FFmpeg.

    Returns the path to the output video.
    """
    _check_ffmpeg()

    if output_path is None:
        base = Path(video_path).stem
        cache_dir = get_hermes_home() / "cache" / "captions"
        cache_dir.mkdir(parents=True, exist_ok=True)
        output_path = str(cache_dir / f"{base}_captioned_{uuid.uuid4().hex[:8]}.mp4")

    # Escape the ASS path for FFmpeg's subtitle filter (colons and backslashes)
    escaped_ass = ass_path.replace("\\", "/").replace(":", "\\:")

    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"ass={escaped_ass}",
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "23",
        "-c:a", "aac",
        "-b:a", "128k",
        output_path,
    ]

    logger.info("Burning captions: %s", " ".join(cmd))
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
    if result.returncode != 0:
        raise RuntimeError(
            f"FFmpeg burn failed (exit {result.returncode}):\n{result.stderr[-2000:]}"
        )

    return output_path


# ---------------------------------------------------------------------------
# Tool handler
# ---------------------------------------------------------------------------

def _check_ffmpeg() -> None:
    """Raise RuntimeError if ffmpeg is not on PATH."""
    if subprocess.run(["which", "ffmpeg"], capture_output=True).returncode != 0:
        raise RuntimeError(
            "FFmpeg is not installed or not on PATH.\n"
            "  macOS: brew install ffmpeg\n"
            "  Linux: sudo apt install ffmpeg"
        )


def check_requirements() -> bool:
    """Return True when the minimum dependencies are present."""
    try:
        import faster_whisper  # noqa: F401  # type: ignore
        return True
    except ImportError:
        return False


def _handle_caption(args: dict, **kw: Any) -> str:
    """Dispatch tool calls to the appropriate caption operation."""
    operation = args.get("operation", "caption")
    video_path = args.get("video_path", "")
    segments_raw = args.get("segments")
    output_path = args.get("output_path")

    if not video_path and operation not in ("build_ass",):
        return json.dumps({"error": "video_path is required"})

    if video_path and not os.path.exists(video_path):
        return json.dumps({"error": f"Video file not found: {video_path}"})

    try:
        if operation == "caption":
            # Full pipeline: transcribe → translate → build ASS → burn
            _check_ffmpeg()
            segments = transcribe(video_path)
            if not segments:
                return json.dumps({"error": "No speech detected in video"})
            segments = translate_to_vietnamese(segments)
            ass_path = build_ass(segments)
            out = burn(video_path, ass_path, output_path)
            return json.dumps({
                "success": True,
                "output_video": out,
                "ass_file": ass_path,
                "segments": segments,
                "segment_count": len(segments),
                "message": (
                    f"Done! {len(segments)} caption segments generated. "
                    f"Output saved to {out}\n"
                    "Here are the captions — let me know what to fix:\n"
                    + "\n".join(
                        f"{i+1}. EN: {s['en']} | VI: {s['vi']}"
                        for i, s in enumerate(segments)
                    )
                ),
            })

        elif operation == "transcribe":
            segments = transcribe(video_path)
            return json.dumps({"success": True, "segments": segments, "count": len(segments)})

        elif operation == "translate":
            if not segments_raw:
                return json.dumps({"error": "segments is required for translate operation"})
            segments = translate_to_vietnamese(segments_raw)
            return json.dumps({"success": True, "segments": segments})

        elif operation == "build_ass":
            if not segments_raw:
                return json.dumps({"error": "segments is required for build_ass operation"})
            ass_path = build_ass(segments_raw, output_path)
            return json.dumps({"success": True, "ass_file": ass_path})

        elif operation == "burn":
            ass_path = args.get("ass_path")
            if not ass_path:
                return json.dumps({"error": "ass_path is required for burn operation"})
            if not os.path.exists(ass_path):
                return json.dumps({"error": f"ASS file not found: {ass_path}"})
            out = burn(video_path, ass_path, output_path)
            return json.dumps({"success": True, "output_video": out})

        elif operation == "reburn":
            # Apply user corrections then re-burn
            if not segments_raw:
                return json.dumps({"error": "segments is required for reburn operation"})
            _check_ffmpeg()
            ass_path = build_ass(segments_raw, output_path=None)
            original_video = args.get("original_video_path", video_path)
            out = burn(original_video, ass_path, output_path)
            return json.dumps({
                "success": True,
                "output_video": out,
                "ass_file": ass_path,
                "message": f"Re-burned with corrections. Output: MEDIA:{out}",
            })

        else:
            return json.dumps({"error": f"Unknown operation: {operation}"})

    except RuntimeError as e:
        return json.dumps({"error": str(e)})
    except Exception as e:
        logger.exception("video_caption tool error")
        return json.dumps({"error": f"Unexpected error: {e}"})


# ---------------------------------------------------------------------------
# Schema & registration
# ---------------------------------------------------------------------------

_SCHEMA = {
    "name": "video_caption",
    "description": (
        "Bilingual video caption tool. Transcribes English speech, translates to Vietnamese "
        "using Kimi K2.5, and burns styled EN+VI captions (stacked) into the video.\n\n"
        "Operations:\n"
        "- caption (default): full pipeline — transcribe + translate + burn. Returns output video path + numbered captions.\n"
        "- transcribe: transcribe only, returns segments.\n"
        "- translate: translate a list of segments to Vietnamese.\n"
        "- build_ass: build ASS subtitle file from segments.\n"
        "- burn: burn an existing ASS file into a video.\n"
        "- reburn: apply corrected segments and re-burn (use after user edits).\n\n"
        "After captioning, present the numbered captions to the user for review. "
        "When the user corrects a line, update the segment and call reburn. "
        "After the user approves, save corrections to memory.\n\n"
        "Requirements: faster-whisper (pip install faster-whisper), ffmpeg (brew/apt install ffmpeg). "
        "Translation requires NVIDIA_API_KEY in ~/.hermes/.env."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {
                "type": "string",
                "enum": ["caption", "transcribe", "translate", "build_ass", "burn", "reburn"],
                "description": "Operation to perform. Default: caption (full pipeline).",
            },
            "video_path": {
                "type": "string",
                "description": "Absolute path to the input video file.",
            },
            "segments": {
                "type": "array",
                "description": "Caption segments for translate/build_ass/reburn operations. Each item: {id, start, end, en, vi}.",
                "items": {"type": "object"},
            },
            "ass_path": {
                "type": "string",
                "description": "Path to the ASS subtitle file (for burn operation).",
            },
            "original_video_path": {
                "type": "string",
                "description": "Original video path for reburn when video_path differs.",
            },
            "output_path": {
                "type": "string",
                "description": "Optional explicit output path for the result file.",
            },
        },
        "required": [],
    },
}

registry.register(
    name="video_caption",
    toolset="video_caption",
    schema=_SCHEMA,
    handler=lambda args, **kw: _handle_caption(args, **kw),
    check_fn=check_requirements,
    requires_env=[],
    emoji="🎬",
)
