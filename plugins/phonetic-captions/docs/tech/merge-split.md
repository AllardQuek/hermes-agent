# Segment Merge & Split — Design, Pitfalls, and Safe Usage

## How timestamps work (why this is actually feasible)

Whisper produces **word-level timestamps** alongside each segment. Every
`CaptionSegment` carries an optional `words: Word[]` array where each `Word`
has `{ word, start, end }` in seconds.

Because the timestamps live at the word level, both merge and split can assign
precise new `start`/`end` values without guessing:

- **Merge**: `start` = first word of the first segment, `end` = last word of
  the last segment, `text` = concatenated texts.
- **Split**: each new child segment gets `start`/`end` from its first/last
  word's timestamps.

The LLM is never asked to invent timestamps. It only decides *where* to cut
or which segments to join; the timestamps are computed deterministically from
the stored word array.

---

## Two surfaces for splitting

### 1. Manual scissors (most reliable)

Visible as a ✂ icon on any segment where `words.length >= 2`.

1. Click ✂ — an inline word chip editor opens below the segment.
2. Each chip shows the word text and its Whisper timestamp.
3. Click the ✂ button *between* chips to mark cut points (orange = selected).
4. Click **Apply — N segments** to commit.

You can mark multiple cut points in one go (N segments = cut points + 1).

**Reliability**: 100% — no LLM involved. Timestamps come straight from the
stored word array.

### 2. NL-edit (via the Hermes panel)

Type a plain-English instruction, e.g.:

> *split segment 3 in two*  
> *split #5 into 3 parts*

The backend sends the current segment list — including each segment's Whisper
`words` array with per-word timestamps — to the LLM. The LLM picks split
points at the largest timing gap between adjacent words and returns `split`
patch objects. The frontend applies these using the same word-array logic as
the manual tool.

#### The timing-gap heuristic

For a segment with N words there are N−1 candidate split points. For each
boundary the gap is:

```
gap(i) = words[i].start − words[i-1].end     (i = 1 … N-1)
```

The system prompt instructs the LLM to prefer the boundary with the largest
gap — i.e. where the speaker paused longest. This is a reasonable proxy for
phrase boundaries because speakers naturally pause between phrases, not within
them.

Example with 4 words:

```
" cảm"  0.50–0.95          gap at 1: 1.05 − 0.95 = 0.10 s
" ơn"   1.05–1.50          gap at 2: 1.52 − 1.50 = 0.02 s  ← smallest
" thầy" 1.52–1.90          gap at 3: 2.20 − 1.90 = 0.30 s  ← largest ✓
" cô"   2.20–2.60
```

"Split in two" → `at_word_index: 3` → "cảm ơn thầy" / "cô".

**When it works well**: run-on segments where the speaker breathes between
phrases. The pause is almost always the right cut point.

**When it can fail**:
- Rhythmic or emphatic speech (pause *inside* a phrase for effect).
- Whisper timestamp errors on unclear audio.
- "Split in half" requests — the LLM may honour word-count symmetry over the
  largest gap. The system prompt biases toward largest gap, but it is not
  guaranteed when the user's instruction implies an even split.

The **patch preview** is the safety net: the user sees `"cô" starts new
segment` before applying and can reject it in favour of the scissors tool.

---

## Two surfaces for merging

### 1. NL-edit only

There is no manual merge button in the UI. Type:

> *merge segments 4 and 5*  
> *combine #2 and #3 into one*  
> *merge segments 1, 2 and 3*

The LLM returns a `merge` patch with the segment ids. The frontend joins
the text with a space, sets `start`/`end` from the outermost timestamps,
and concatenates the `words` arrays so the merged segment can be re-split
later if needed.

### 2. "Fix with AI" (QA → merge suggestion)

If QA flags a very short segment (<0.3 s), it will suggest merging. Clicking
**Fix →** pre-fills the NL edit box and auto-submits. The resulting patch goes
through the same path as a manual NL instruction.

---

## How the apply pipeline works

`handleApplyNLPatches` in `EditorView` (index.tsx) runs entirely client-side
in two ordered passes:

**Pass 1 — edits and merges** (processed in reverse patch order to keep array
positions stable while iterating):

- `edit`: finds segment by `id`, updates the named field.
- `merge`: finds all segments by `id`, builds a merged segment at the
  position of the first, removes the rest. `words` is concatenated so future
  splits still have accurate timestamps.

**Pass 2 — splits** (processed tail-first by position so earlier insertions
don't shift indices of later ones):

- Groups all split patches by `segment_id`.
- For each segment, collects all `at_word_index` values, deduplicates, sorts,
  then applies them as a single multi-point split (same logic as the manual
  scissors tool).
- Invalid split points (`pt <= 0` or `pt >= words.length`) are silently
  dropped.

After both passes, all segment ids are reassigned sequentially
(`segs.map((s, i) => ({ ...s, id: i }))`).

---

## Known limitations

### Word array required for split

Segments without a `words` array cannot be split — the scissors button is
hidden and NL split patches against them are silently skipped (the split point
filter `pt < words.length` rejects all points when `words` is empty).

Segments lose their word array if:
- The job was created by pasting a JSON transcript (no Whisper, no word
  timestamps).
- The segment was manually typed into the text field after the fact.

**Workaround**: edit `start`/`end` fields manually via the NL-edit `edit` op
(`set segment 3 start to 12.5`), or use the manual text field and accept
imprecise timing.

### LLM infers word indices from the actual `words` array

The backend now includes each segment's full `words` array
(`[{"word", "start", "end"}, ...]`) in the prompt. The LLM is instructed to
choose `at_word_index` at the point where the timing gap between adjacent
words is largest — the natural pause point — rather than counting
space-delimited tokens in the text string.

This means NL-edit split accuracy is now equivalent to the manual scissors
tool in terms of timestamp precision. The only remaining failure mode is if
the LLM ignores the instruction and still counts spaces (unlikely with
current models, and always visible in the patch preview before you apply).

### "Split into N parts" may produce fewer parts

If the LLM produces duplicate or out-of-range `at_word_index` values, the
deduplication/range-filter step drops them. A request for 3 parts could
yield 2 parts. Since the LLM now sees real word counts this is much less
likely than before, but still possible if a segment has very few words (e.g.
only 2 words can only split into 2 parts regardless of the instruction).
If it happens, use the scissors tool instead.

### No undo

There is no undo history. Patches shown in the preview panel can be
individually unchecked before applying, which is the only review gate.
Changes are persisted to disk only on **Re-burn**. Reloading the page
before Re-burn restores the last saved state.

---

## id-drift: why sequential NL edits are safe (after the fix, May 2026)

Prior to the fix, the NL-edit endpoint loaded segments from disk. If the user
applied a merge or split patch (which reassigns in-memory ids) and then made
another NL-edit request *without* re-burning, the backend would generate
patches referencing stale disk ids that no longer matched the in-memory
segment ids. Patches would silently miss or hit the wrong segment.

**After the fix**: the frontend sends its current in-memory segments
(without the word arrays, for payload compactness) alongside every NL-edit
request. The backend uses the sent segments and ignores the disk state. The
disk state is only used as a fallback for older clients that don't send
segments in the request.

---

## Prompts reliable enough for demos

These patterns consistently produce correct patches in testing. Prefer these
for any live demo.

### Merge (most reliable)

```
merge segments 3 and 4
combine #7 and #8
merge segments 2, 3 and 4
```

- Name adjacent segments by their display numbers (#N = 1-based, shown in the
  UI).
- Merging non-adjacent segments is supported by the patch format but the LLM
  may decline it as semantically nonsensical — don't demo this.
- Merging more than ~4 segments in one instruction is untested; prefer
  sequential merges.

### Split (reliable when segment has ≥3 words)

```
split segment 5 in two
split #3 into two parts
```

- Works best when the target segment has enough words for a natural midpoint
  (≥ 3 words).
- Avoid "split into 3 parts" for demo — the LLM must pick two independent
  word indices and getting both right is harder. Use the scissors tool
  instead.
- For very short segments (2 words) the LLM may return `[]` (no patches) —
  expected and correct.

### Text / phonetic edit (safest of all)

```
fix the diacritics in segment 3
change the phonetic guide of #2 to [dee-uh wee]
set segment 4 lang to en
```

These never touch timestamps or segment count — zero risk of structural
corruption.

### QA-triggered merge (safest merge path for demos)

1. Click **QA → Review** in the Hermes panel.
2. Find a flag recommending a merge (short duration < 0.3 s).
3. Click **Fix →** — the instruction is pre-filled and auto-submitted.
4. Check the patch preview, click **Apply**.

This path is most demo-safe because the QA prompt is deterministic and the
suggested instruction is always a valid merge of existing segments.

---

## Prompts to avoid in demos

| Pattern | Risk |
|---|---|
| `split #2 into 3 parts` | Requires 2 correct word indices; only fails now if segment has ≤ 2 words |
| `merge #1 and #5` (non-adjacent) | LLM may comply but the result looks odd — 4 missing segments between them |
| `split segment 1` (no part count) | LLM may ask for clarification or return `[]` |
| Any instruction after structural patches without re-burning was done *before May 2026 fix* | Now safe — backend uses live segment state |
| Editing a segment that was created by pasting a JSON transcript | Split will silently no-op (no word timestamps) |
