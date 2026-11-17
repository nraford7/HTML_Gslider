# HTML style and safer assets Implementation Plan

> **For agentic workers:** Executed through Agency (one Agency task per plan task) under /do-it MEDIUM. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add `--style html` (Slides deck that matches the HTML's own look, text editable), revoke asset sharing after each build, fetch linked images safely, and use native Slides tables in template style.

**Architecture:** Phase 1 runs three independent tasks in separate worktrees (fonts, measure, native tables). Phase 2 adds the html-style compiler and wires `--style` through build, CLI and check. Phase 3 adds sharing revocation, then linked images, both in `build.py`, in sequence.

**Tech Stack:** Python 3.11+, bs4, Pillow, fontTools, headless Chrome (`--dump-dom`, `--screenshot`), `gws` CLI, pytest.

**Spec:** `docs/superpowers/specs/2026-11-16-html-style-and-assets-design.md` (the "Review amendments" section is binding).

## Global Constraints

- Public repo: no company, client or personal names or paths in any file. Fixtures use neutral names (ACME, Northwind).
- Template style (`--style template`) output stays as today except tables (Task 3). All existing tests keep passing, adjusted only where Task 3 changes table requests.
- 1920px = 960pt (1px = 0.5pt). Text box inset 7.2pt. Width slack 3%.
- Chrome always runs with `--virtual-time-budget=10000`, a throwaway profile, `--force-device-scale-factor=1`, `--hide-scrollbars`; temp files under `out/` with unique names.
- Every test writes only to `tmp_path`; no Google writes in unit tests.
- Run per task: `python3 -m pytest tests -q`, `ruff check gslider tests`.

## Review Focus

1. A deck whose font never loads (offline, misspelled family) → that slide falls back with "font <name> did not load"; the build continues — Task 5 `test_unloaded_font_reported`, Task 6 `test_unloaded_font_slide_falls_back`.
2. A build that fails halfway → assets already shared are still revoked (revocation runs in a `finally`) — Task 7 `test_revoke_runs_after_failed_push`.
3. Linked image URL that redirects to a private address → refused, slide falls back — Task 8 `test_redirect_to_private_refused`.
4. Text with gradient fill (`background-clip:text`) → appears once (text box), not also in the plate — Task 5 `test_plate_css_hides_gradient_text` (asserts the injected CSS contains the three properties) and acceptance visual check.
5. Template-style table with a column width ratio under 32pt → clamped, no 400 from Slides — Task 3 `test_narrow_column_clamped`.

---

## Phase 1 (parallel worktrees)

### Task 3: Native tables in template style

**Files:** Modify `gslider/compile.py` (`_rows` table path only), `gslider/check.py` (coverage and style audit read tables), `tests/test_compile.py`, `tests/test_check.py`, `tests/fixtures/check_cols3_*` only if needed.

**Interfaces — Produces:** table object id `s{NN}_table{i}`; Box entries for tables carry `role: "table"`, `rows`, `cols`.

- [ ] Step 1: tests: `test_table_compiles_to_create_table` (tbl fixture → one `createTable` with rows = data rows + 1, columns = header count; `insertText` per non-empty cell with `cellLocation`); `test_table_column_widths_from_ratios` (`updateTableColumnProperties` widths proportional to `widths`, summing to the block width in EMU ±1); `test_narrow_column_clamped` (ratio giving <32pt → ≥406400 EMU, total unchanged); `test_table_borders_and_fill` (OUTER border alpha 0, inner rule colour 0.75pt, cells `NOT_RENDERED`); `test_table_bold_ranges_in_cells`; `test_timeline_stays_text_boxes`; `test_check_coverage_reads_table_cells` (fake `get` with `table.tableRows[].tableCells[].text`); `test_style_audit_skips_tables`.
- [ ] Step 2: run, confirm failures. Step 3: implement. Step 4: pytest + ruff PASS; reference tests (`GSLIDER_REFERENCE_HTML`) still ≥80% native.

### Task 4: Fonts for any Google Fonts family

**Files:** Modify `gslider/fonts.py`, `tests/test_fonts.py`, `tests/conftest.py`.

**Interfaces — Produces:** `font_path(family: str) -> Path` (any Google Fonts family; raises `FontUnavailable` if the family is unknown to Google Fonts); `google_font_available(family: str) -> bool` (cached in `~/.cache/gslider/fonts.json`); `normal_line_ratio(family: str) -> float | None` (hhea ratio, None if unavailable); `slides_family(css_family: str, generic: str) -> tuple[str, bool]` (family to use in Slides, and whether it was substituted: Arial for sans-serif, Georgia for serif, Courier New for monospace).

- [ ] Step 1: tests (network calls mocked except one `@pytest.mark.network` live test skipped offline): `test_css_api_ttf_url_parsed` (sample CSS2 response with `src: url(...ttf)` → URL); `test_unknown_family_unavailable` (HTTP 400 → False, cached); `test_slides_family_substitution` (`("Helvetica Neue","sans-serif") → ("Arial", True)`, `("Inter","sans-serif") → ("Inter", False)` when available); `test_existing_families_unchanged` (Playfair Display / Libre Franklin ratios equal today's values 1.333 / 1.212 ±0.001).
- [ ] Step 2–4: fail, implement (CSS API `https://fonts.googleapis.com/css2?family=<Name>:wght@400;700` fetched with user agent `gslider` so it lists TTF URLs; keep existing pinned URLs as first choice for the two current families), pass.

### Task 5: Measure and plate in Chrome

**Files:** Create `gslider/measure.py`, `gslider/measure.js` (injected script, package data), `tests/test_measure.py`, `tests/fixtures/html_style.html` (one slide: heading, paragraph with a bold span, a rotated label, gradient-text heading, a `<table>`). Modify `pyproject.toml` package data.

**Interfaces — Produces:** `measure_slide(html_path: Path, n: int, workdir: Path) -> SlideMeasure` and `measure_deck(html_path, slide_ns: list[int], workdir, workers=3) -> dict[int, SlideMeasure]`; `SlideMeasure = {"n", "background": "#rrggbb", "fonts_status": str, "unloaded": [family], "blocks": [Block], "skipped": [{"text", "reason"}], "plate": Path}`; `Block = {"x","y","w","h" (px), "lines": int, "align": str, "line_height_px": float, "runs": [{"text","family","generic","size_px","bold","italic","color"}], "transform": str|None}`. `n` is the 1-based `.slide-wrap` index (same as `render_slide_png`).

- [ ] Step 1: tests (real Chrome, skipped if `assets.CHROME` is None): `test_measure_text_blocks` (fixture: heading and paragraph found with rects within the slide, runs carry bold on the span, families resolved); `test_rotated_text_skipped` (reason mentions transform); `test_unloaded_font_reported` (fixture referencing a nonexistent web font → `unloaded` lists it); `test_plate_has_no_text` (plate PNG: ink count inside the heading rect ≤ 2% of its area; background colour at a corner matches `background`); `test_plate_css_hides_gradient_text` (the plate CSS string includes `-webkit-text-fill-color: transparent`, `-webkit-text-stroke: 0`, `background-clip: border-box`); `test_iframes_removed_from_temp_copy`; `test_measure_deck_parallel` (3 slides, workers=3, results keyed by n).
- [ ] Step 2–4: fail, implement per spec A and amendments 2, 3, 4, 5, 9 (script waits for fonts, writes JSON to `script#gslider-measure`; Python reads it from `--dump-dom` output), pass.

## Phase 2

### Task 6: html-style compiler and wiring

**Files:** Create `gslider/htmlstyle.py`, `tests/test_htmlstyle.py`. Modify `gslider/build.py` (`--style`), `gslider/cli.py`, `gslider/check.py` (html mode: skip style audit; report substitutions, skipped blocks, position read-back, image diff and `compare.png`), `README.md`, `docs/USAGE.md`, `skills/gslider/SKILL.md`, `tests/test_build.py`, `tests/test_check.py`.

**Interfaces — Consumes:** Task 4 `slides_family`, `normal_line_ratio`; Task 5 `measure_deck`. **Produces:** `compile_html_deck(deck: dict, measures: dict[int, SlideMeasure], assets: dict[str, str]) -> list[SlidePlan]` (same SlidePlan shape as template; keys `plate:<n>` in `assets`; boxes carry `role: "text"`); `build(..., style: str = "html")`; CLI `--style {html,template}` (default html).

- [ ] Step 1: tests: `test_px_to_pt_and_inset` (block at 100,200 px, 400x60 → box x 42.8, y 92.8, w 400*0.5*1.03+14.4, h 44.4 pt); `test_run_styles` (size px*0.5 pt, weight ≥600 bold, colour, alignment); `test_line_spacing_from_css` (Libre Franklin 30px with line-height 45px → spacing 45/(30*1.212)*100 ±0.5); `test_text_transform_applied`; `test_family_substitution_reported`; `test_plate_is_bottom_layer` (createImage for the plate precedes all text boxes); `test_unloaded_font_slide_falls_back`; `test_build_style_html_measures_and_publishes_plates` (measure and publish mocked); `test_build_style_template_unchanged` (template path still calls `compile_deck`); `test_check_html_skips_style_audit`; CLI default style html.
- [ ] Step 2–4: fail, implement, pass. Docs: README "How it works" gains the html style; Limits lists letter-spacing dropped, rotated/clipped text stays in the plate, plates are pictures. Skill: build defaults to html style; `--style template` for the fixed look.

## Phase 3 (sequential, `build.py`)

### Task 7: Revoke sharing after the build

**Files:** Modify `gslider/gws.py` (`drive_share_anyone` returns the permission id; new `drive_permissions(file_id) -> list[dict]`, `drive_unshare(file_id, perm_id)`), `gslider/assets.py` (store `appProperties.perm`; `revoke(ids) -> (revoked, failed)`; `unshare_all(purge_plates: bool) -> dict`), `gslider/build.py` (revoke in `finally` after push and notes; `build.json` gets `shared: [{file, perm}]`, `plates`, `revoked`, `revoke_failed`), `gslider/cli.py` (`unshare-assets [--purge-plates]`), `tests/test_assets.py`, `tests/test_build.py`, `tests/test_gws.py`, docs.

- [ ] Step 1: tests: `test_share_returns_perm_id`; `test_revoke_uses_stored_perm_or_lists_anyone`; `test_revoke_runs_after_failed_push`; `test_revoke_failure_does_not_fail_build` (recorded in build.json and report); `test_dedupe_reshares_after_revoke` (cached file with `shared:"0"` is shared again); `test_unshare_assets_all_and_purge_plates`; protected ids still refused.
- [ ] Step 2–4: fail, implement, pass.

### Task 8: Linked images

**Files:** Modify `gslider/parse.py` (external image → `image` block with `src`, no `data`), create `gslider/fetch.py` (`resolve_image(src: str, base_dir: Path) -> tuple[bytes, str]`, raises `FetchError(reason)`), modify `gslider/build.py` (resolve before compile; failures become `unknown` blocks with the reason), tests `tests/test_fetch.py`, `tests/test_parse.py` (update the external-image tests: now `image` with `src`), `tests/test_build.py`.

- [ ] Step 1: tests: `test_relative_path_inside_folder`; `test_path_outside_folder_refused` (`../secret.png`, absolute path, `file:///etc/...`); `test_http_download_checks_bytes` (mock urlopen; PNG/JPEG/GIF by magic bytes; HTML response refused); `test_private_address_refused` (127.0.0.1, 10.x, 169.254.169.254, ::1); `test_redirect_to_private_refused`; `test_max_five_redirects`; `test_size_cap_50mb`; `test_build_resolves_linked_image` (slide becomes native with an image); `test_unresolvable_image_falls_back_with_reason`.
- [ ] Step 2–4: fail, implement (urllib with a custom redirect handler that re-checks each hop's resolved addresses; `ipaddress` checks), pass.

## Integration and acceptance (orchestrator)

- [ ] Merge Phase 1 worktrees; run tests; then Phase 2; then Phase 3.
- [ ] Live: build the reference deck (`GSLIDER_REFERENCE_HTML`) with `--style html` and with `--style template`; `check` both: coverage 0, overflow 0; html-style report lists position read-back and image differences; look at `compare.png` by eye.
- [ ] Live: after each build, `drive permissions list` on every id in `build.json` → no `anyone` permission; thumbnails still show images.
- [ ] Live: one slide with `<img src="local.png">` next to the HTML → image in the deck.
- [ ] `gslider unshare-assets` run once for real to revoke sharing on assets left public by earlier builds; report count.
- [ ] Build time under 3 minutes for html style.
- [ ] Commit, merge to main, push.

## Rollback

Revert the merge on main; `--style template` remains available throughout.

## Review amendments (plan pass 1, binding)

1. **Task 3 existing tests.** `_rows_block()` blocks without `style` go through the table path. Update, in Task 3: `test_rows_bold_ranges_applied`, `test_rows_without_bold_emit_no_bold_requests`, `test_table_bold_end_to_end` (bold becomes `updateTextStyle` with `cellLocation`); `test_table_widths_normalised_and_narrow_column_rejected` (now clamps to 32pt, no `ValueError`); `test_fixture_requests_unchanged` excludes `tbl.html` (its requests change by design); regenerate `check_cols3_*` only if they contain a table (they do not).
2. **Task 3 check side.** Table overflow: after the build, check reads `tableRows[].rowHeight` from `get` and flags a table whose total height passes the slide's floor (or overlaps the next box).
3. **Task 6 check in html style.** html plans carry `ground` (`"dark"` if the measured background luminance < 0.5 else `"ivory"`) and `background` hex. `obstacles()` ignores the plate object (`s{NN}_plate`). The pixel overflow scan compares the band below a box against the plate PNG at the same place (not a flat ground). Skipped blocks' texts (rotated, clipped, SVG) are appended to the slide's `dropped_text` and listed under "Left in plate" in the report.
4. **Task 6 box shape.** Box `h` is the text-area height `height*0.5`; the created element is `h + 14.4`. Fix the test: box h 30.0, element height 44.4.
5. **Task 7 build.json.** Keep `shared_assets` (ids, as today) and add `shared: [{file, perm}]`, `plates`, `revoked`, `revoke_failed`.
6. **Task 4.** `FontUnavailable` subclasses `ValueError`.
7. **Task 8 image key.** `_image_key` includes `src` so two linked images on one slide stay distinct.
8. **Task 8 fetch security.** Resolve once and connect to the checked IP (custom `HTTPConnection`/`HTTPSConnection` with SNI and `Host` set to the hostname), so DNS cannot change between check and connect. Refuse private, loopback, link-local, unspecified, multicast, reserved, and IPv4-mapped IPv6 forms of any of those. The redirect handler refuses non-http(s) schemes and re-runs the same checks per hop. Enforce the 50 MB cap by streaming reads. Local paths: `Path.resolve()` (following symlinks) then `is_relative_to(base_dir.resolve())`. Add tests: `test_ipv4_mapped_private_refused`, `test_redirect_to_file_scheme_refused`, `test_stream_cap_without_content_length`, `test_symlink_escape_refused`.
9. **Task 5 gradient text.** The script tags elements whose computed `background-clip` is `text`; the plate CSS sets `background-image: none !important` on tagged elements (plus the fill/stroke rules). `test_plate_has_no_text` checks a plain heading and the gradient heading.
10. **Task 7 test note.** `test_dedupe_reshares_after_revoke` is a regression guard (it passes today). The fake drive's `share` returns a permission id.
