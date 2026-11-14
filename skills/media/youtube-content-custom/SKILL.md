---
name: youtube-content-custom
description: >
  Fetch YouTube video transcripts and transform them into structured content
  (chapters, summaries, threads, blog posts). Use when the user shares a YouTube
  URL or video link, asks to summarize a video, requests a transcript, or wants
  to extract and reformat content from any YouTube video.
version: 1.0.0
author: Teknium (teknium1), Hermes Agent
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [YouTube, Video, Transcripts, Media]
    related_skills: []
---

# YouTube Content Tool

Extract transcripts from YouTube videos and convert them into useful formats.

## Setup

```bash
uv pip install youtube-transcript-api
```


## Helper Script

SKILL_DIR is the directory containing this SKILL.md file. The script accepts any standard YouTube URL format, short links (youtu.be), shorts, embeds, live links, or a raw 11-character video ID.

```bash
# JSON output with metadata
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "https://youtube.com/watch?v=VIDEO_ID"

# Plain text (good for piping into further processing)
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --text-only

# With timestamps
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --timestamps

# Specific language with fallback chain
uv run python3 SKILL_DIR/scripts/fetch_transcript.py "URL" --language tr,en
```


## Output Formats

After fetching the transcript, format it based on what the user asks for:

- Chapters: Timestamped bullet list grouped by topic shifts
- Summary: Concise 5-10 sentence overview of the entire video with inline timestamps per topic — timestamps are required for all YouTube outputs
- Chapter summaries: Chapters with a short paragraph summary for each
- Thread: Twitter/X thread format — numbered posts, each under 280 chars
- Blog post: Full article with title, sections, and key takeaways
- Quotes: Notable quotes with timestamps

Rule: All outputs for time-based media (video/audio/podcast) must include timestamps. Do not produce plain text without them.

### Example — Chapters Output (with clickable timestamp links)

When formatting chapters, use bullet points with the raw timestamp (no calculation needed) as the link text, and convert to total seconds for the t= URL param.

Format: `• [MM:SS] Title text`

Plain text only. No markdown links needed — Telegram auto-links [MM:SS] timestamps when the video URL appears in the message header. Include URL in header for auto-linking to work.

Output structure:

```
TITLE: Video Title
LINK: https://youtu.be/VIDEO_ID

KEY POINTS:
• [01:10] Description of this section
• [02:21] Description of this section
```




## Workflow

1. **Fetch** the transcript using the helper script with `--text-only --timestamps`.
2. **Validate**: confirm the output is non-empty and in the expected language. If empty, retry without `--language` to get any available transcript. If still empty, tell the user the video likely has transcripts disabled.
3. **Chunk if needed**: if the transcript exceeds ~50K characters, split into overlapping chunks (~40K with 2K overlap) and summarize each chunk before merging.
4. **Transform** into the requested output format. If the user did not specify a format, default to chapters (timestamped bullet list). For the chapters format (and any timestamped output), ALWAYS wrap the result in the TITLE/LINK/KEY POINTS block exactly as shown in the "Example — Chapters Output" section below — never return bare bullets without that header. Inline timestamps ([MM:SS]) are mandatory in every format (see Rule line above); the LINK header is what makes Telegram auto-link them.
5. **Verify**: re-read the transformed output to check for coherence, correct timestamps, and completeness before presenting.

## Chapter Verification

When given an existing chapter list to verify against a transcript (e.g. user provides chapters from a previous session or another source), validate each timestamp by fetching the transcript and checking what's actually said at each timestamp.

### Method
1. Fetch transcript with --timestamps --text-only.
2. For each chapter timestamp, convert to total seconds and find the nearest transcript line.
3. Check whether the chapter label matches the actual content. Common failure modes:
   - Wrong label: timestamp is correct but topic description is hallucinated (e.g. labeled "compensation" when the actual topic is "agency")
   - Off by minutes: topic is real but timestamp points to the wrong segment
   - Teaser vs segment: chapters mapped to the teaser/clips at the start instead of the full discussion later
4. Report: which chapters are correct, which are wrong, and what the actual content is at each timestamp.

### Approach for large transcripts

For long videos (60+ min), don't try to verify every line. Instead:
- Get all speaker lines with timestamps by filtering lines containing >> and a timestamp prefix
- Sample every 30-60 seconds to build a topic map
- For specific chapter timestamps, zoom in with ±2min context to verify the topic
- Use execute_code to do the filtering in Python rather than flooding context with raw transcript

## Fallback Strategies (when primary method fails)

YouTube transcript extraction can fail in three ways:
1. IP blocked / captcha — youtube-transcript-api returns HTTP 403 or CAPTCHA errors
2. Truncated/short output — API returns far fewer segments than expected for video duration
3. Transcript disabled — no captions available at all

Use fallbacks in this order:

### Option A: yt-dlp (fast, robust — use first)

yt-dlp downloads subtitles directly and is not affected by YouTube's API IP blocks. It's the most reliable fallback.

```bash
uv pip install yt-dlp  # or: pip install yt-dlp
yt-dlp --write-subs --sub-langs en --skip-download -o "/tmp/yt_%(id)s.%(ext)s" "URL"
```

Output: /tmp/yt_VIDEO_ID.en.vtt (WebVTT). Parse with:

```python
import re
with open("/tmp/yt_VIDEO_ID.en.vtt") as f:
    vtt = f.read()

# Extract cues: HH:MM:SS.mmm --> HH:MM:SS.mmm\ntext
pattern = r'(\d{2}:\d{2}:\d{2}\.\d{3}) -->.*?\n(.+?)(?=\n\n|\Z)'
for m in re.finditer(pattern, vtt, re.DOTALL):
    start = m.group(1)  # HH:MM:SS.mmm
    text = m.group(2).strip()
    # Convert to MM:SS
    h, mms, s_ms = start.split(':')
    minutes = int(h)*60 + int(mms)
    seconds = int(float(s_ms))
    print(f"[{minutes:02d}:{seconds:02d}] {text}")
```


Why first: No browser automation, fast, handles long videos, works from any IP.

### Option B: Browser-use fallback (last resort)

If yt-dlp fails (e.g., JS runtime errors), use browser automation. YouTube blocks youtube-transcript-api from cloud IPs (AWS, GCP, Azure). When the python script fails with "YouTube is blocking requests from your IP", use this approach.

Note on PATH: The binary may live at ~/.browser-use-env/bin/browser-use OR ~/.hermes/hermes-agent/node_modules/.bin/browser-use. Verify with which browser-use.

### Steps

1. Open video via browser-use CLI:

   ```bash
   ~/.browser-use-env/bin/browser-use close --all  # clear stale sessions
   ~/.browser-use-env/bin/browser-use open "https://www.youtube.com/watch?v=VIDEO_ID"
   ```

2. Expand description — find and click the ...more / Read more button:

   ```bash
   ~/.browser-use-env/bin/browser-use state  # find index of expand button (usually "..." or "Read more")
   ~/.browser-use-env/bin/browser-use click <index>
   ```

3. Open transcript panel — find and click "Show transcript":

   ```bash
   ~/.browser-use-env/bin/browser-use state | grep -i transcript
   ~/.browser-use-env/bin/browser-use click <index>
   ```

### Pitfalls
- eval with text > ~100 chars sometimes returns None — always use chunking, not one big eval
- Transcript may be partial: YouTube's transcript panel lazy-loads as you scroll. Use chunked extraction with overlap, or scroll the panel programmatically first. If the last timestamp is far earlier than video duration, scroll and re-extract.
- browser_navigate (Browserbase cloud) and browser-use CLI are different sessions — don't mix
- If browser-use times out, run browser-use close --all then retry
- "Subtitles unavailable" ≠ no transcript: The YouTube player may show "Subtitles/closed captions are unavailable" but the transcript panel (via "Show transcript" in description) still works. Always try the browser-use fallback before declaring a video has no transcript. The player message refers to captions on the video player itself, not the transcript panel.

## Error Handling

- **Transcript disabled**: tell the user; suggest they check if subtitles are available on the video page.
- **Private/unavailable video**: relay the error and ask the user to verify the URL.
- **No matching language**: retry without `--language` to fetch any available transcript, then note the actual language to the user.
- **Dependency missing**: run `uv pip install youtube-transcript-api` and retry.
- **IP blocked by YouTube**: use the browser-use fallback above.
