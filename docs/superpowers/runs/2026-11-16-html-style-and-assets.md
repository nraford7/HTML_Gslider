# Run: html-style-and-assets
Instruction: "A then B with /do-it" (A: a mode that recreates the HTML's own look in Google Slides: lay each slide out in headless Chrome, read every element's position, font, size and colour, recreate as editable boxes; template mode kept as an option. B: remove link-sharing or delete assets right after insertion; fetch images linked by URL or relative path; native Slides tables for tables.)
Stage: code-review
Route: HEAVY (router: MEDIUM — hard trigger: Drive sharing permissions; BR2 REV1 NOV2 INT1 FC1; no lens) + escalated to HEAVY at code pass 2: surviving BLOCKER (parser differential)
Spec: docs/superpowers/specs/2026-11-16-html-style-and-assets-design.md   Plan: docs/superpowers/plans/2026-11-16-html-style-and-assets.md
Agency project: 01a1e58b-a273-7ea7-a9ab-5a7c2d2c4aa1 (T3 -c3a9-7069, T4 -c3a9-7f75, T5 -c3aa-72b8, T6 -c3aa-78db, T7 -c3aa-70df, T8 -c3aa-7d7b)

## Scorecards
Pass 1 [spec]: 2B/6S/1C/0R · fixed -/- · velocity = (—→9, escalation no) · judge: n/a-medium
  Fable fallback. All fixed as binding amendments 1-9 in the spec.
Pass 1 [plan]: 2B/8S/1C/0R · fixed -/- · velocity = (—→11, escalation no) · judge: n/a-medium
  Fable fallback. All fixed as plan amendments 1-10.
Pass 1 [code:html-style-assets]: 1B/5S/5C/0R · fixed -/- · velocity = (—→11, escalation no) · judge: n/a-medium
  Stage 1 (0B/2S/4C) + Stage 2 Fable blind (1B/2S/2C: html style let Chrome read local files and private hosts into the public plate). Live acceptance added: table border opacity 400 (template build stopped at slide 12), html build 307s (>180s), font weights lost. One fix agent for all; security re-review on the fix delta follows.
Pass 2 [code:html-style-assets]: 1B/0S/1C/0R · fixed 15/15 prior · velocity ↓ (11→2, escalation no) · judge: n/a-medium
  Fable blind security re-review of 33e022e..418f9bf. B: `<!-->` comment hides markup from html.parser but not Chrome (file img/iframe into plate). C: curl font fetch lacks --proto-redir. Escalated to HEAVY. Fix: html5lib parse + comments stripped + CSP meta (nonce'd measure script) on every page Chrome opens.
Pass 3 [code:html-style-assets]: 1B/3S/3C/0R · fixed 1/1 prior · velocity ↑ (2→7, escalation no) · judge: CONTINUE (open BLOCKER)
  Stage 1 (1B/3S/3C) ∥ Stage 2 Fable blind (1B, same class). B: svg/math <style> text re-serialised raw becomes a live <meta refresh> to file:// (CSP does not cover navigation). S: parser slide-count parity; only refresh meta removed; no CSP good-content test. Fix 16e9a12+: '<' escaped in style text, sanitise to a fixed point, all meta http-equiv + mglyph/malignmark dropped, sanitize.page() re-reads the final page and refuses any meta http-equiv or foreign script, build refuses parser slide mismatch. Live after 16e9a12: html 136 s, PASS, plates pixel-identical, 17 files private; after pass-3 fix plates re-measured locally: 15/15 identical.

## Chunks
- [x] Phase 1: T3 2cb457d, T4 3e7b767, T5 0f350db (merged 7dabccb)
- [x] Phase 2: T6 f439acc
- [x] Phase 3: T7 d4fdbb0, T8 33e022e

## Notes
- Public repo: no company, client or personal names or paths in any committed file. The private reference deck is referred to only as GSLIDER_REFERENCE_HTML.
- Codex quota exhausted until 2026-11-19: independent passes use Fable subagents.
- Live html build (reference deck): PASS, 15/15 native, coverage 0, overflow 0, all 15 assets revoked and verified private; image difference 6.4-13.5 (0-255).
