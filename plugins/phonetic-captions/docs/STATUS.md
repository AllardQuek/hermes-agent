# Hackathon Build Status

**Last updated**: 2 May 2026 (evening — post-v8 language generalization)  
**Deadline**: EOD Sunday 3 May 2026 (1 day remaining)

---

## Architecture

```
Telegram (trigger only)
  → Agent: transcribe (Whisper) + generate phonetics (Kimi K2.6 or Hermes model fallback)
  → Saves job JSON to ~/.hermes/caption-jobs/{id}.json
  → Burns initial draft video (FFmpeg)
  → Returns: draft video + "Edit at http://localhost:9119/captions/{id}"
        ↓
Dashboard /captions/:id  (no LLM, no tokens, visual editing)
  [video player]  |  [editable segment list: text / phonetic / EN-VI toggle]
                  |  [style panel: font, size, color, margin]
                  |  [Re-burn → FFmpeg only]  [Download]
```

---

## What We've Built

### 1. Core Video Captioning Pipeline — `plugins/phonetic-captions/pipeline.py` ✅

> **Pivoted 1 May**: Original design was EN→VI translation. Correct use case is Vietnamese
> language *teaching* shorts — mixed EN/VI audio where the goal is phonetic guides for
> Vietnamese words, not translating English content.
>
> **Moved 2 May (v7)**: Was `tools/video_caption.py` (core tree). Moved into the plugin
> directory so the plugin is fully self-contained and distributable.

A fully self-contained Hermes tool:

| Function | What it does |
|---|---|
| `transcribe(video_path)` | faster-whisper `medium` model, **auto language detect** (`language=None`), VAD filter — returns `{id, start, end, text, lang, phonetic}` segments |
| `generate_phonetics(segments, api_key)` | **With NVIDIA_API_KEY**: Kimi K2.6 via NVIDIA NIM. **Without key**: user's configured Hermes model (automatic fallback). Classifies EN/VI, corrects Vietnamese diacritics, generates English phonetic guides |
| `build_ass(segments, output_path)` | ASS subtitle file with `MAIN` + `PHONETIC` styles: VI segments → Vietnamese text on top + italic phonetic below; EN segments → text only |
| `burn(video_path, ass_path, output_path)` | FFmpeg H.264 burn-in with fast preset |
| `_handle_caption(args)` | Dispatcher supporting 6 operations (see below) |

**Segment schema**: `{id, start, end, text, lang ("en"|"vi"), phonetic}`

**Caption layout on screen:**
```
Vietnamese segment:    không biết        ← MAIN style (bold, full size)
                       [humm biet]       ← PHONETIC style (italic, smaller, semi-transparent)

English segment:       Today we learn... ← MAIN style only
```

**Operations exposed to the agent:**
- `caption` — full pipeline (transcribe → generate_phonetics → build ASS → burn)
- `transcribe` — transcription only
- `generate_phonetics` — classify + correct + add phonetics via Kimi
- `build_ass` — ASS file generation only
- `burn` — burn a given ASS file into video
- `reburn` — apply corrected segments and re-burn (the edit loop)

**Kimi quirk handled**: Response sometimes lands in `reasoning_content` instead of `content` — fallback guard is in place.

**Tool registration**: Via `plugins/phonetic-captions/__init__.py` `register(ctx)` — standard Hermes plugin system, no manual import list needed.

---

### 2. Gateway Video Path Injection Fix — `gateway/run.py` ✅

Telegram caches received video files to disk, but the agent message previously contained no reference to the path — the agent had no way to know the file existed. Fixed by adding a video MIME type injection block (mirrors the existing document injection pattern):

```
[The user sent a video: 'name.mp4'. The file is saved at: /path/to/video.mp4.
You can process it with your available tools.]
```

This block is inserted in `_preprocess_inbound_text()` after the document block, before the reply-to block.

---

### 3. Toolset Registration — `toolsets.py` ✅

Added `video-caption` as an optional toolset (NOT in `_HERMES_CORE_TOOLS`) so it doesn't affect platforms that don't have faster-whisper installed:

```python
"video-caption": {
    "description": "Bilingual video captioning — ...",
  "tools": ["video-caption"],
    "includes": []
}
```

Enable per-instance via `~/.hermes/config.yaml`:
```yaml
toolsets:
  - hermes-cli
  - video-caption
```

---

### 4. Caption Style Config — `hermes_cli/config.py` ✅

Added a `caption` section to `DEFAULT_CONFIG` with fully documented style defaults:

```python
"caption": {
    "style": {
        "font": "Arial",
        "font_size": 48,
        "primary_color": "&H00FFFFFF",  # white  (ASS: &HAABBGGRR)
        "outline_color": "&H00000000",  # black
        "outline_width": 3,
        "alignment": 2,                 # bottom-center
        "margin_edge": 80,
        "max_line_length": 42,
    }
}
```

Users can override any field in `~/.hermes/config.yaml`. The tool reads this at runtime.

---

### 5. Teaching Caption Skill — `skills/video/phonetic-captions/SKILL.md` ✅

Renamed (1 May) from `bilingual-captions` to `phonetic-captions` for clarity and scalability.
Updated to surface dashboard link after generation and explain visual editor workflow.
Chat correction loop retained as fallback for mobile/no-dashboard users.

---

### 6. Dashboard Visual Caption Editor — Plugin ✅ (2 May — migrated to plugin architecture)

> **Migrated 2 May**: Originally implemented as core edits to `web_server.py` / `App.tsx`.
> Moved to a first-class **Hermes dashboard plugin** — zero core file changes, pre-built IIFE,
> drop-in installation. Better demo story: *"We built a plugin, not a fork."*

**Plugin location**: `plugins/phonetic-captions/`

```
plugins/phonetic-captions/
├── plugin.yaml          plugin manifest (pip_dependencies, provides_tools)
├── __init__.py          register(ctx) — wires video-caption tool into Hermes plugin system
├── pipeline.py          caption pipeline (transcribe, phonetics, build_ass, burn)
└── dashboard/
    ├── manifest.json    tab: /captions, icon: FileText, after:skills
    ├── plugin_api.py    13 FastAPI routes at /api/plugins/phonetic-captions/*
    ├── src/index.tsx    React editor UI (state-based nav, no react-router-dom)
    ├── build.mjs        esbuild → IIFE (React from SDK, lucide-react bundled)
    ├── package.json     devDeps: esbuild, lucide-react, typescript
    ├── tsconfig.json
    └── dist/index.js    pre-built 50.7kB IIFE (committed, no user build step)
```

**Plugin API** (`plugin_api.py` — `router = APIRouter()`):

| Endpoint | Purpose |
|---|---|
| `GET /health` | Dependency health check (ffmpeg, faster_whisper, phonetics_source, hermes_model) |
| `GET /jobs` | List all jobs (includes `status` field) |
| `DELETE /presets/{name}` | Delete a named preset |
| `POST /presets/generate` | NL description → `AIAgent` → `CaptionStyle` (not auto-saved) |

Auth: all `/api/plugins/*` routes are auth-exempt by framework design (localhost only).

**Frontend** (`src/index.tsx` → `dist/index.js`):
- `PrerequisitesBanner`: shown at top of job list — fetches `/health`, shows error strip (red) for missing ffmpeg/faster-whisper with install commands, info strip for phonetics source (NVIDIA K2.6 or Hermes model name)
- Three-column layout (≥1280px): Col 1 video+actions, Col 2 segments+style, Col 3 ✦ Hermes panel
- ✦ Hermes panel: Edit segments (NL edit), QA (flags + Fix→), Style presets (gallery + AI creation)
- `PresetGallery`: named preset cards (apply/delete), Save current, Create with AI, Learned card
- Per-segment: text, phonetic field (VI only), EN/VI badge toggle, word-level split (✂)
- QA: amber flag borders on segment cards; flag details + scroll-to-segment in Hermes panel
- `UploadModal`: video + optional segments JSON, auto-pipeline toggle, 2s polling with live status
- State-based routing (`useState` + `window.history.pushState`) — no react-router-dom
- Bundle: 50.7 kB IIFE

**Core files minimally touched**: `web/src/App.tsx` (1-line catch-all guard for hard-refresh deep-link fix)

---

### 7. Hermes-Integrated Dashboard Features ✅ (2 May — plan v4)

#### a) File Upload
- "+ New Job" button on job list opens `UploadModal`
- Video file picker with server-side extension validation
- Toggle: auto-pipeline (Whisper + Kimi) vs. manual segments JSON import
- Background pipeline with 2s polling and live status text in modal
- Job cards show status badges (amber spinner for in-progress, red for error)

#### b) Natural Language Segment Editing
- Text input docked below segment list in editor
- Supports: edit text/phonetics/lang, shift timing, merge segments, split at word boundary
- Agent returns structured JSON patch array; frontend shows before/after diff with per-change checkboxes
- User approves/rejects individually; accepted patches auto-save via `PUT /segments`
- Agent never writes to disk — propose only

#### c) Segment QA Review
- "Review all" button in segments header
- Agent flags: wrong lang, mangled diacritics, phonetics mismatch, timing anomalies, empty text
- Flagged segments get amber left border with issue + suggestion
- "Fix with AI" pre-fills NL panel with the QA suggestion

#### d) Cross-Session Style Memory
- On every burn: style diff vs. defaults written to Hermes `MemoryStore` (silent, instant)
- After ≥ 3 diff entries: "Suggest style" button appears in style panel
- Click → `AIAgent` analyses history → returns `CaptionStyle` + 1-sentence explanation
- Inline dismissible banner with "Apply" button updates local style state

#### e) Style Preset Load/Save
- "Load": file input → `FileReader` → validate keys → `setStyle()` (no backend call)
- "Save": serialize style → Blob → `<a>` download as `caption-style.json` (no backend call)

#### f) Hard Refresh Deep-Link Fix
- `web/src/App.tsx`: `*` catch-all suppressed while `pluginsLoading` is true
- Hard refresh on `/captions/{id}` now stays on the correct page instead of bouncing to `/sessions`

---

### 8. Testing Steps (End-to-End) 🧪

#### Prerequisites (run once)
```bash
# 1. Install Python dependencies
source .venv/bin/activate
pip install faster-whisper openai

# 2. Install FFmpeg (macOS)
brew install ffmpeg

# 3. Add Kimi API key
echo "NVIDIA_API_KEY=nvapi-..." >> ~/.hermes/.env

# 4. Enable the toolset in ~/.hermes/config.yaml
#    toolsets:
#      - hermes-cli
#      - video-caption

# 5. Load the skill (run once)
hermes skills add skills/video/phonetic-captions
```

#### Test 1 — CLI smoke test (no Telegram needed)
```bash
hermes
# In the CLI:
# > caption the video at /path/to/test_clip.mp4
# Expected:
#   - Spinner while Whisper transcribes (~30s for a 30s clip on CPU)
#   - Tool output shows segment list with EN/VI classification
#   - Agent replies with: output path + "Edit at http://localhost:9119/captions/<id>"
```

#### Test 2 — Dashboard editor
```bash
hermes dashboard        # opens browser at http://localhost:9119

# 1. Click "Captions" tab in the sidebar
#    → Job list loads, shows the job from Test 1

# 2. Click the job row
#    → Editor opens: video player on left, segment list on right

# 3. Edit a segment:
#    - Click the VI badge on a Vietnamese segment to toggle it
#    - Fix the text in the inline input
#    - Fix/add a phonetic guide (appears when lang=vi)

# 4. Change a style setting:
#    - Expand "Style settings"
#    - Change font size from 48 → 52

# 5. Click "Re-burn"
#    - Button shows spinner, ~5-10s for FFmpeg
#    - Video player reloads automatically with new captions

# 6. Click "Download"
#    - captioned_<id>.mp4 downloads to ~/Downloads
```

#### Test 3 — Telegram flow
```bash
hermes gateway start telegram

# On Telegram:
# 1. Send a short video (< 60s for quick test)
# 2. Wait for "Processing your video..." reply
# 3. Agent should reply with the captioned video + dashboard link
# 4. Click the link → editor opens on the right job automatically
#    (plugin reads /captions/<id> from window.location on mount)
```

#### Verify plugin discovery
```bash
# With dashboard running:
curl http://localhost:9119/api/dashboard/plugins | python -m json.tool
# → should include { "name": "phonetic-captions", "tab": {"path": "/captions"}, ... }
```

---

### 8. Named Preset Library + 3-Column Editor ✅ (2 May — plan v5)

#### a) Named Style Preset Library
- Server-side preset store at `~/.hermes/caption-presets/{name}.json`
- 4 new API endpoints: `GET /presets`, `PUT /presets/{name}`, `DELETE /presets/{name}`, `POST /presets/generate`
- `PresetGallery` component: named preset cards (click to apply, ✕ to delete)
- "Save current" button with inline name input → card added to gallery
- "Learned" card surfaces the MemoryStore suggestion (amber border, ✦ icon) with Apply + Save as actions
- Replaces old file-based Load/Save JSON buttons entirely

#### b) AI Style Creation
- Expandable "Create with AI" section in preset gallery
- NL description → `POST /presets/generate` → `AIAgent` → `CaptionStyle` preview
- Preview shows all 8 style fields; user names and saves to gallery OR just applies without saving

#### c) Three-Column Editor Layout
- Grid: Col 1 (360px) video + actions, Col 2 (flex) segments + style fields, Col 3 (380px) ✦ Hermes panel
- Responsive: below xl (1280px) Col 3 wraps below Col 2
- Clicking a flag in the Hermes panel scrolls to that segment card in Col 2
- QA "Review" button moved from segment list header into Hermes panel

---

### 9. Hackathon Plans — `plugins/phonetic-captions/docs/` ✅

- `PLAN_v1.md` — original Telegram-native plan (executed)
- `PLAN_v2.md` — dashboard core-edit approach (archived)
- `PLAN_v3.md` — plugin architecture, editor + burn (executed)
- `PLAN_v4.md` — Hermes-integrated dashboard: upload, NL edits, QA, style memory (executed)
- `PLAN_v5.md` — 3-column layout, named preset library, AI style creation (executed)
- `PLAN_v6.md` — v6: alignment picker + drag-to-position overlay (executed)
- `PLAN_v7.md` — v7: plugin distributability, NVIDIA fallback, health banner (executed)
- `PLAN.md` — v8: language generalization — any target language, Whisper auto-detect (current)

---

### 10. Caption Positioning — Alignment Picker + Drag-to-Position ✅ (2 May — plan v6)

#### a) Alignment Picker
- 3×3 grid of arrow buttons (↖↑↗ / ←·→ / ↙↓↘) replacing the previously hidden `alignment` int field
- Clicking any button sets `style.alignment` (1–9 ASS numpad); active button highlighted amber
- Wired into Caption Style section in Col 2 between Outline width and Margin from edge

#### b) Drag-to-Position Overlay
- 160×90 thumbnail widget in the Caption Style section with a reference grid
- Click or drag anywhere on the thumbnail to pin the caption anchor to a normalised `{x, y}` position (0.0–1.0)
- Amber dot tracks the pinned position; "× Reset position" clears the override
- Position persists in `style.position` and is sent with `PUT /jobs/{id}/style` on re-burn

#### c) Burn-side injection (`tools/video_caption.py`)
- When `style["position"]` is set, every Dialogue line is prefixed with `{\anN\pos(x,y)}`
  - `x = position.x × 1920`, `y = position.y × 1080` (hardcoded 1920×1080 canvas matches PlayResX/Y)
  - `N` = `style.alignment` — alignment sets the text anchor point at the pinned pixel
- Jobs without `position` in style burn identically to v5 (no regression)

#### d) Label rename
- "Bottom margin" → "Margin from edge" in the UI (backend field name `margin_bottom` unchanged)

#### e) Data model
- `CaptionStyle` TypeScript interface gains `position?: { x: number; y: number } | null`
- No backend model changes — `position` flows through the existing `style: dict[str, Any]` payload
- No new API routes

### 11. Plugin Distributability ✅ (2 May — plan v7)

#### a) Self-contained plugin structure
- `plugin.yaml` created at `plugins/phonetic-captions/` root — declares `pip_dependencies`, `provides_tools`, no `requires_env`
- Pipeline code moved from `tools/video_caption.py` → `plugins/phonetic-captions/pipeline.py`
- `plugins/phonetic-captions/__init__.py` added with `register(ctx)` — registers `video-caption` tool via Hermes plugin system
- `video-caption` entry removed from `toolsets.py` (tool now registered via plugin)
- `tools/video_caption.py` deleted

#### b) NVIDIA fallback to Hermes model
- `generate_phonetics()` now tries NVIDIA Kimi K2.6 first (if `NVIDIA_API_KEY` set)
- Falls back automatically to user's configured Hermes model if key absent or call fails
- Prompt and JSON parsing logic shared between both paths
- NVIDIA_API_KEY is no longer a prerequisite — only an optional quality upgrade

#### c) Frontend prerequisites health banner
- `GET /health` endpoint returns: `ffmpeg`, `faster_whisper`, `phonetics_source`, `hermes_model`
- `PrerequisitesBanner` component in `src/index.tsx` at top of job list:
  - Error strip (red) if ffmpeg or faster_whisper missing, with exact install command
  - Info strip showing phonetics engine (NVIDIA Kimi K2.6 or Hermes model name)
  - Dismissible; non-blocking

---

### 12. Language Generalization ✅ (2 May — plan v8)

The plugin is no longer tied to Vietnamese. Any target language now works with zero
code changes — the key insight is that Whisper already auto-detects per-segment language,
so the pipeline can derive `target_lang` automatically without asking the user.

#### a) `pipeline.py` — `target_lang` parameter throughout

- **`LANG_NAMES`** dict: BCP-47 code → display name for 17 languages (vi, ko, ja, zh, ar, fr, es, de, it, pt, hi, th, id, tr, nl, pl, ru)
- **`_lang_name(code)`** helper: returns display name from `LANG_NAMES`, falls back to `code.upper()`
- **`_detect_target_lang(segments)`**: inspects the `lang` field on already-transcribed segments (populated by Whisper's per-segment language detection) and returns the most-frequent non-English code. Falls back to `"vi"` with a warning if all segments are English.
- **`_phonetics_prompt(segments, target_lang)`**: prompt now uses `{lang_name}` and `{target_lang}` placeholders — works for Korean, Japanese, Arabic, etc. without edits
- **`_build_ass_content(segments, style, target_lang)`**: `if lang == target_lang` replaces `if lang == "vi"` — phonetic line appears for whichever language is the target
- **`generate_phonetics(segments, ..., target_lang)`**: passes `target_lang` to prompt builder
- **`build_ass(segments, ..., target_lang)`**: passes through to `_build_ass_content`
- **`save_caption_job(..., target_lang)`**: stores `target_lang` in job JSON for later use
- **`_handle_caption(args)`**: reads `target_lang` arg (default `"auto"`), runs `_detect_target_lang` if `"auto"`, passes resolved code through all downstream calls
- **SCHEMA**: `target_lang` parameter added; description updated to language-agnostic wording

#### b) `plugin_api.py` — prompts parameterized, endpoints updated

- **`_NL_SYSTEM_PROMPT`** → **`_nl_system_prompt(target_lang_code, target_lang_name)`** function: NL edit instructions are now language-aware (`"lang" must be "en" or "{code}"`)
- **`_QA_SYSTEM_PROMPT`** → **`_qa_system_prompt(target_lang_code, target_lang_name)`** function: QA checks mention the correct language name for diacritics and classification issues
- **`_STYLE_GENERATE_SYSTEM_PROMPT`**: updated to remove EN/VI hardcoding (style is language-agnostic anyway)
- **`_update_job_status()`**: gains optional `target_lang` kwarg — writes resolved lang to job JSON after Whisper detection
- **`_run_pipeline(job_id, video_path, target_lang="auto")`**: resolves `"auto"` via `_detect_target_lang()` after transcription, stores resolved lang on job before phonetics call
- **`/upload` endpoint**: accepts `target_lang: str = Form("auto")` — stored on job, passed to pipeline
- **`list_jobs`**: includes `target_lang` in each job summary row
- **`/nl-edit` and `/qa` endpoints**: load job's `target_lang` and pass to prompt functions

#### c) `dashboard/src/index.tsx` — language selector + per-job awareness

- **`SUPPORTED_LANGS`** constant: 17 languages + auto-detect option
- **`CaptionSegment.lang`**: widened from `"en" | "vi" | ""` to `string` — accepts any BCP-47 code
- **`CaptionJob.target_lang?: string`** and **`JobSummary.target_lang?: string`** added
- **`UploadModal`**: language selector `<select>` shown when pipeline is on, defaults to "Auto-detect"
- **`EditorView`**: `targetLang` state loaded from `job.target_lang` (fallback `"vi"` for old jobs)
- **`toggleLang`**: uses `targetLang` variable instead of hardcoded `"vi"` — correct toggle for Korean, Japanese, etc.
- **Segment badge**: `isTarget = seg.lang === targetLang` replaces `lang === "vi"` comparisons — badge highlights the actual target language
- **Phonetic input row**: shown when `isTarget` (any target lang), not only when `lang === "vi"`
- **Bundle rebuilt**: `dist/index.js` updated (51.0 kB)

#### Design decisions applied

| Decision | Outcome |
|---|---|
| Auto-detect strategy | Whisper already emits per-segment language codes; `_detect_target_lang()` picks the most-frequent non-English one — no user input needed for the happy path |
| Telegram entry point | No change to UX — auto-detect covers 90%+ of use cases silently |
| Dashboard entry point | Upload modal gains a language selector (default: auto) — power users can override |
| Backwards compatibility | Old jobs without `target_lang` field default to `"vi"` everywhere — zero breakage |
| Non-goals for v8 | Multiple simultaneous target langs, non-English-as-primary videos — deferred |

---

### 13. Post-v7 Polish & Bug Fixes ✅ (2 May — current state)

Changes made after STATUS.md was last written (commits after `7199ddb37`).

#### a) Drag-to-position overlay removed
- The `style.position` override from v6 (normalised `{x, y}` + `\pos()` ASS injection) was removed in the style layout overhaul.
- The **alignment picker** (3×3 numpad grid) remains — it's the canonical way to control caption position.
- No `position` field in `CaptionStyle` TypeScript interface; no `\pos()` in ASS output.

#### b) `margin_bottom` → `margin_edge` rename (complete)
- `pipeline.py` `_DEFAULT_STYLE` and `_build_ass_content()` renamed to `margin_edge`.
- `plugin_api.py` `_STYLE_DEFAULTS` and all endpoints updated to `margin_edge`.
- `dashboard/src/index.tsx` `CaptionStyle` interface, `STYLE_DEFAULTS`, and all usages renamed to `margin_edge` — frontend now sends the key the backend reads.
- `hermes_cli/config.py` `DEFAULT_CONFIG["caption"]["style"]` renamed to `margin_edge` — CLI path now consistent.

#### c) Fixed relative import bug in `plugin_api.py`
- Dashboard loader imports `plugin_api.py` by file path (not as a package module), so `from . import pipeline` failed with 500 errors.
- Fixed by adding `_get_pipeline()` helper that loads `pipeline.py` via `importlib.util.spec_from_file_location` using the absolute path at `_PLUGIN_ROOT / "pipeline.py"`.

#### d) SSE streaming for AI agent calls
- Added `_agent_sse()` async generator in `plugin_api.py` that runs the agent in a thread via `asyncio.to_thread` and yields heartbeat `{"status": "..."}` SSE events every 1.5 s while waiting.
- NL-edit (`/nl-edit`) and QA (`/qa`) endpoints now use `_agent_sse()` and return `StreamingResponse`, preventing browser timeout on slow models.
- Frontend SSE readers in `NLEditPanel` and `HermesPanel` consume status events to show live progress text.

#### e) QA review enhanced with praise flags
- QA system prompt updated to return both `"issue"` and `"praise"` type flags.
- `QAFlag` interface gains `type?: "issue" | "praise"` field; defaults to `"issue"` for backward compat.
- Issues display with amber border + `AlertCircle` icon; praises display with green border + `CheckCircle2` icon.
- Hermes panel header shows separate issue/praise counts: *"2 issues · 3 good"*.
- Praise flag `suggestion` field is left empty (no "Fix →" button shown for praised segments).

#### f) Info sidebar on job list
- `InfoSidebar` component added to the right column of the job list page (visible ≥ xl breakpoint).
- Shows "How it works" 4-step guide (Send video → Review & edit → Style it → Re-burn & download) with icons.
- Shows "Key features" bullet list (AI QA, NL edits, style presets, Telegram-native).
- Always visible to first-time users; no dismiss needed.

#### g) Video player component improved
- Extracted into `VideoPlayer` component with explicit `"loading" | "ready" | "error"` state.
- Loading state shows spinner overlay while buffering; error state shows `VideoOff` icon with "Re-burn to generate it" hint.
- `key={src}` reset on re-burn so the player reloads without a manual page refresh.

#### h) Caption styles layout overhauled
- Style fields reorganised into a cleaner vertical list with labelled rows.
- `StyleNumberField` and `StyleTextField` helper components standardise number/text inputs.
- Alignment picker moved into the style section (Col 2) directly; drag overlay removed.
- "Style with Hermes" AI generation button renamed from "Create with AI" for consistency with branding.

#### i) Undo added then removed
- NL-edit undo (segment snapshot before applying patches) was added in `ee50521ca` and removed in `41d0a494d` to keep the UX simple and avoid stale-state edge cases.

---

### P0 — Must have before demo

| Task | Notes |
|---|---|
| Install dependencies | `pip install faster-whisper openai` in `.venv` |
| Install FFmpeg | `brew install ffmpeg` |
| (Optional) Set `NVIDIA_API_KEY` | Add to `~/.hermes/.env` for best-quality phonetics; otherwise Hermes model is used |
| Enable plugin | `hermes plugins enable phonetic-captions` (replaces toolset enable) |
| Load skill | `hermes skills add skills/video/phonetic-captions` |
| End-to-end smoke test | CLI caption → dashboard → edit → re-burn → download |
| Upload smoke test | "+ New Job" → upload video → pipeline runs → editor opens with segments |
| NL edit smoke test | Type instruction → diff shown → apply → segments updated |
| Preset gallery smoke test | Save current style → card appears → click to apply → delete |
| AI style creation test | "Create with AI" → describe style → preview → save → card in gallery |
| Learned card test | Burn 3+ jobs with non-default style → amber Learned card appears |
| Alignment picker test | Click top-center button → re-burn → captions at top-center |
| Drag position test | Drag thumbnail → re-burn → captions at pinned position |
| Position reset test | "× Reset position" → re-burn uses alignment+margin only |
| Telegram flow test | Send video via Telegram → path injection → captions → dashboard link |
| Health banner test | Open job list → banner shows phonetics source; remove ffmpeg → error strip |

### P1 — Important for demo quality

| Task | Notes |
|---|---|
| Font selection | "Arial" is safe fallback; "Montserrat Bold" looks better for Shorts |
| Style tuning | Run against a real Shorts clip, adjust `font_size` / `margin_edge` to taste |
| Style memory seed | Burn 3+ jobs with non-default style to make "Suggest style" button appear in demo |
| Demo video 1 | 10–20s clip — show raw → captioned → NL correction → re-burn |
| Demo video 2 | Same topic, second take — show style suggestion applied from memory |

### P2 — Nice to have

| Task | Notes |
|---|---|
| Whisper model upgrade | `large-v3` for better accuracy (~3GB RAM, slower CPU) |
| Vietnamese font rendering | Test `ơ ư ạ ề ọ` characters display correctly in burned output |
| Progress feedback in CLI | faster-whisper can take 10–30s — add "transcribing…" acknowledgement before tool call |
| NL split UI polish | Currently defers to interactive ✂ button — could apply programmatically with word index |
| QA auto-run on upload | Run QA pass automatically after pipeline completes, not only on button click |

---

## Known Issues / Risks

| Issue | Severity | Status |
|---|---|---|
| `ffmpeg` path was stale | Fixed | Current binary now resolves to `/opt/homebrew/bin/ffmpeg` |
| Kimi `reasoning_content` quirk | Handled | Fallback guard in `generate_phonetics()` |
| faster-whisper not yet installed | P0 | `pip install faster-whisper` |
| CPU transcription speed | Medium | 20s video takes ~15–30s on CPU — acceptable for demo, mention it in video |
| Telegram 50MB video limit | Low risk | 10–20s Shorts are typically 5–25MB — should be fine |
| No automated tests for new tool | Low risk | Manual E2E smoke test covers the hackathon window |
| ~~`margin_edge` vs `margin_bottom` mismatch~~ | ~~Medium~~ | **Fixed** — `margin_edge` now consistent across `pipeline.py`, `plugin_api.py`, `index.tsx`, and `config.py` |

---

## Setup Checklist (run this before first demo test)

```bash
# 1. Install Python dependencies
source .venv/bin/activate
pip install faster-whisper openai

# 2. Install ffmpeg (macOS)
brew install ffmpeg

# 3. Verify ffmpeg is on PATH
ffmpeg -version

# 3a. Confirm Hermes can load the tool module
python - <<'PY'
from tools.video_caption import _DEFAULT_STYLE
print('video_caption import OK')
print(_DEFAULT_STYLE['font'])
PY

# 4. Add NVIDIA API key
echo "NVIDIA_API_KEY=nvapi-YOUR_KEY_HERE" >> ~/.hermes/.env

# 5. Enable the toolset in config
# Add to ~/.hermes/config.yaml:
#   toolsets:
#     - hermes-cli
#     - video-caption

# 6. Install the skill
hermes skills install skills/video/phonetic-captions

# 7. Quick smoke test from CLI
python -c "
from tools.video_caption import transcribe, translate_to_vietnamese
print('Import OK')
"

# 8. First real test
# Run captioning against a 10-20s local video with spoken English and
# verify the tool returns: transcription, English+Vietnamese pairs, ASS file,
# and a burned output video.
```

---

## Testing Start Plan

### Immediate checks

1. Confirm `ffmpeg -version` works from the shell. This is already green.
2. Use Python 3.11+ for the repo. The current shell Python is 3.8.11, which blocks importing `plugins.phonetic_captions.pipeline` because the codebase uses newer typing syntax.
3. Install `faster-whisper` in a 3.11 virtualenv and verify `plugins.phonetic_captions.pipeline` imports.
4. Enable the plugin: `hermes plugins enable phonetic-captions`.
5. Run the first smoke test on a short local clip before trying Telegram.

### First smoke test

Use a 10-20 second video with clear English speech and no complicated music bed. The goal is to prove the full loop, not caption quality yet:

1. Transcription completes.
2. Kimi translation returns Vietnamese.
3. ASS output is written.
4. FFmpeg burns the subtitles into a new video.

### Next test after that

Send the same clip through Telegram so we can verify the gateway path injection and the end-to-end agent flow, not just the local tool.

---

## Submission Checklist

- [ ] ffmpeg installed and working
- [ ] Python 3.11+ virtualenv active
- [ ] faster-whisper + openai installed
- [ ] NVIDIA_API_KEY set in `~/.hermes/.env` (optional — Hermes model is fallback)
- [ ] `phonetic-captions` plugin enabled (`hermes plugins enable phonetic-captions`)
- [ ] `phonetic-captions` skill loaded
- [ ] End-to-end smoke test: Telegram → pipeline → editor → re-burn → download
- [ ] Upload flow: "+ New Job" → auto-pipeline → editor opens with segments
- [ ] NL edit: instruction → diff shown → apply → segments updated + saved
- [ ] QA review: flagged segments visible, "Fix with AI" pre-fills NL panel
- [ ] Style memory: 3+ burns done, "Suggest style" appears and applies correctly
- [ ] Alignment picker: top-center button → re-burn shows captions at top
- [ ] Drag position: pin position on thumbnail → re-burn respects \pos coordinates
- [ ] Position reset: "× Reset position" reverts to alignment+margin burn
- [ ] Hard refresh: `/captions/{id}` reloads correctly (not redirected to `/sessions`)
- [ ] Health banner: shows phonetics source; missing-dep warnings appear correctly
- [ ] Demo video 1 recorded: raw video → Telegram → captions → dashboard editor → NL correction → re-burn
- [ ] Demo video 2 recorded: show style suggestion applied from memory
- [ ] Health banner: shows NVIDIA or Hermes fallback source correctly
- [ ] Kimi proof: show NVIDIA_API_KEY in config / API call in logs (OR show Hermes model fallback in logs)
- [ ] Tweet posted tagging @NousResearch
- [ ] Discord submission in `#creative-hackathon-submissions`
