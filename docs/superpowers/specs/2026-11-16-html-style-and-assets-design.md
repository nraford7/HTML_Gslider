# Match the HTML look, and safer assets (design)

Date: 2026-11-16 · Branch: `feat/html-style-and-assets` · Route: MEDIUM (hard trigger: Drive sharing permissions)

## Problem

1. gslider throws away the HTML's design. Every deck comes out in one fixed template (`layouts.yaml`), whatever the HTML looked like. Users want the Slides deck to look like their HTML.
2. Images are uploaded to Drive and left shared "anyone with the link can view" forever, although Slides keeps its own copy after insertion (Google docs: "The image is fetched once at insertion time and a copy is stored for display inside the presentation").
3. Images linked by URL or relative path make the slide fall back to a picture.
4. HTML tables become loose text boxes; users expect a real Slides table they can edit as a table.

## Success criteria (measurable)

1. `gslider build deck.html` (new default `--style html`) builds every slide with all text as editable text boxes placed where Chrome draws it. `--style template` gives today's behaviour unchanged (all existing tests pass).
2. On the reference deck (`GSLIDER_REFERENCE_HTML`, 15 slides): `check` reports coverage misses 0 and overflow 0; for every slide the text-box left/top edges are within 4pt of Chrome's measured positions (read back from the built deck via `get`); a per-slide image difference between the Slides thumbnail and the Chrome render is reported in `report.md` (mean absolute difference on 400x225 greyscale, informational, plus a side-by-side sheet `compare.png`).
3. After any build, no file gslider uploaded during that build still has an `anyone` permission (verified with `drive permissions list` on each id in `build.json`), and every image is still visible in the deck (thumbnail check).
4. `gslider unshare-assets` revokes `anyone` on every file in the assets folder and reports the count.
5. An `<img src="logo.png">` next to the HTML file, and an `<img src="https://...png">`, both end up as images in the deck (template and html styles). A missing file or failed download makes only that slide fall back, with the reason.
6. In `--style template`, a `table.tbl` becomes a native Slides table (createTable) with the header row styled, column widths from the `th` ratios, cell text styled by role; check coverage reads table cell text.
7. Build of the reference deck in html style under 3 minutes.

## Approach

### A. `--style html` (new default)

**Measure (new `gslider/measure.py`).** For each slide, Chrome headless opens a temp copy of the HTML with the same isolation CSS as fallback renders (only that `.slide-wrap` visible, 1920x1080, zoom cancelled) plus an injected script. The script walks the slide and writes JSON into a `<script type="application/json" id="gslider-measure">` element; Chrome is run with `--dump-dom` and Python reads that element back. For each **text block** (an element with non-whitespace text whose children are all inline), the script records: `getBoundingClientRect`, computed `font-family` (first family), `font-size`, `font-weight`, `font-style`, `color`, `line-height`, `text-align`, `text-transform`, `letter-spacing`, and the text as **runs** (text plus bold/italic/colour per inline span). It also records the slide's background colour and the number of rendered lines per block (`Range.getClientRects` grouped by top).

**Plate.** A second Chrome screenshot of the same slide with all text made invisible (`color: transparent !important; text-shadow: none !important; caret-color: transparent`) gives a 1920x1080 PNG of everything that is not text: backgrounds, gradients, rules, shapes, images. It is inserted full-bleed as the slide's bottom layer.

**Compile (new `gslider/htmlstyle.py`).** 1920px = 960pt, so 1px = 0.5pt. Each text block becomes a TEXT_BOX at `x = left*0.5 - 7.2`, `y = top*0.5 - 7.2`, `w = width*0.5*1.03 + 14.4` (3% slack so Slides does not wrap earlier than Chrome), `h = height*0.5 + 14.4`, autofit off. Text style per run: font family mapped through a small table (generic `sans-serif` → Arial, `serif` → Georgia, `monospace` → Courier New; any other family is passed by name, Slides resolves Google Fonts by name), size `px*0.5` pt, weight (>=600 bold), italic, colour; paragraph alignment; line spacing = `css_line_height / (font_size * normal_line_ratio) * 100` using `fonts.py` metrics when the family is known, else `css_line_height / (1.2*font_size) * 100`. `text-transform` is applied to the text. `letter-spacing` has no Slides equivalent and is dropped (listed in the report when non-zero). Object ids `s{NN}_t{i}` (text) and `s{NN}_plate`.

**Check in html style.** Coverage and overflow (estimate replaced by: Slides thumbnail ink below a box) as today; the per-role style audit is skipped (roles do not exist). Add position read-back and the image difference metric.

**What html style does not do.** No layout rules, no fallback slides (the plate carries all non-text visuals). Text inside SVG and canvas is part of the plate, so it is not editable (listed in the report).

### B1. Revoke sharing after the build

After the last slide and notes batch, revoke the `anyone` permission on every asset file shared for this build (`drive permissions delete`), set `appProperties.shared = "0"`, and record `revoked: [ids]` in `build.json`. The existing dedupe already re-shares a cached file whose `shared` mark is not `"1"`, so later builds keep working. A failed revoke does not fail the build: it is listed in `build.json` and the report, and `unshare-assets` cleans it up. New command `gslider unshare-assets`.

### B2. Linked images

`parse` keeps external `<img src>` / `url(...)` as `image` blocks with `src` and no data (instead of `unknown`). Before compiling, `build` resolves them: relative paths against the HTML file's folder (refusing paths outside it), `file://`, and `http(s)://` downloads (20s timeout, 50 MB cap, content type PNG/JPEG/GIF checked by bytes). A resolved image gets `data`, `mime`, `sha256`; a failed one becomes `unknown` with the reason, so only that slide falls back. In html style Chrome loads linked images itself, so they land in the plate.

### B3. Native tables (template style)

`rows` blocks with `style: "table"` compile to `createTable` (rows x cols incl. header) at the block's position and width; `updateTableColumnProperties` for widths from `widths`; `insertText` per cell; text styles by role (`colhead` for the header row, `body` for cells; bold ranges applied); `updateTableBorderProperties` to the rule colour at 0.75pt, outer borders off; `updateTableCellProperties` to transparent fill. Height estimate per row stays as today (measure each cell), plus cell padding (Slides default 0.1in each side, verified live). Timelines stay as text boxes. Check reads text from `table.tableRows[].tableCells[].text` for coverage and skips tables in the style audit.

## Alternatives considered

| Option | Why not |
|---|---|
| Map every HTML element to a native Slides shape (rectangles, lines) | CSS backgrounds, gradients, borders, shadows and pseudo-elements have no complete mapping; the plate reproduces them exactly while text stays editable. Native shapes can be added later for plain rectangles. |
| Playwright for measurement | Extra dependency and a browser download for users; Chrome `--dump-dom` with an injected script is enough. |
| Delete assets after the build | Revoking keeps the dedupe cache and is reversible; deletion loses it. Both remove public access. |
| Native tables in html style | Chrome's table layout is reproduced exactly by measured text boxes over the plate; a native table would reflow differently. |

## Blast radius and rollback

New modules plus changes to build, cli, parse, compile, check, assets. Template style keeps its tests. Rollback: revert the merge. Permission change only removes access to files gslider itself uploaded (identified by `appProperties.gslider == "1"` and the assets folder owned by the user).

## Open questions

None blocking. Default style becomes `html`; `/gslider` skill and docs updated to say so.

## Review amendments (pass 1, binding where they differ from the text above)

1. **Revoking needs the permission id.** `drive_share_anyone` returns the created permission's `id`; store it in `appProperties.perm` and in `build.json` (`shared: [{file, perm}]`). For a cached file without a stored id, `permissions list` and pick `type == "anyone"`. Delete by id, then list again to verify; criterion 3 checks the same way.
2. **Fonts must be loaded before measuring.** The injected script forces layout (`document.body.offsetHeight`), then `await Promise.all([...document.fonts].map(f => f.load()))`, `await document.fonts.ready`, two `requestAnimationFrame`s, then measures. It writes `document.fonts.status` and any used family that is not loaded into the JSON. Chrome always runs with `--virtual-time-budget=10000` (no budget hangs on network fonts). A slide whose used font did not load becomes a fallback picture with that reason.
3. **Untrusted content.** Decks are treated as the user's own (trusted) input for scripts, which they may need to render. The temp copy Chrome opens has `iframe`, `object` and `embed` elements removed. Linked-image downloads (B2): `http(s)` only plus local paths; resolve the host and refuse loopback, private, link-local and metadata addresses; re-check after each redirect; at most 5 redirects; local paths (including `file://`) must resolve inside the HTML file's folder.
4. **Plate privacy.** Plate files are listed separately in `build.json` (`plates: [ids]`) and revoked with the other assets after the build. `gslider unshare-assets --purge-plates` deletes plate files. Docs state that plates exist briefly with a public link during the build. The hiding CSS also sets `-webkit-text-fill-color: transparent`, `-webkit-text-stroke: 0`, and `background-clip: border-box` on text elements so gradient text is not baked into the plate.
5. **Transformed text.** If a text block or any ancestor has a non-identity `transform`, a `zoom`, or clips it (`overflow: hidden` with the text extending past the clip), the block is left in the plate (not hidden, not a text box) and listed in the report.
6. **Line spacing and fidelity metric.** `fonts.py` is generalised to any Google Fonts family: it downloads the family's TTF through the Google Fonts CSS API (`https://fonts.googleapis.com/css2?family=<name>:wght@400;700`, fetched with a non-browser user agent so the CSS lists TTF files) and caches it; its hhea ratio gives Slides' normal line height (template mode already showed Slides line pitch matches hhea within 1pt). Criterion 2's pass metric becomes: coverage 0, and no text box with ink below it on the Slides thumbnail (overflow 0). Position read-back and the image difference stay in the report as information, not pass/fail.
7. **Table API limits.** Column widths are clamped to at least 32pt (406400 EMU) and renormalised to the table width. Outer borders: `updateTableBorderProperties` with `borderPosition: OUTER` and fill alpha 0. Cell fill: `tableCellBackgroundFill.propertyState: NOT_RENDERED`. Overflow for tables is read back from `tableRows[].rowHeight` after the build instead of estimated.
8. **Fonts Slides lacks.** A family is used by name only if the Google Fonts CSS API knows it (HTTP 200, cached); otherwise it maps to Arial (sans), Georgia (serif) or Courier New (mono) by the CSS generic fallback, and the report lists each substitution.
9. **Speed.** Chrome measure and plate renders run in a pool of 3 workers. Text boxes are emitted in DOM order so overlapping text keeps its stacking order.
