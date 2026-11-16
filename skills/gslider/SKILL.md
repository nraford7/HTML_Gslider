---
name: gslider
description: Use when the user types /gslider, or wants a finished HTML slide deck (one .slide-wrap per slide) turned into an editable Google Slides deck, rebuilt into Slides, or checked after a build. Also use when someone asks to "put the deck in Google Slides", "upload the HTML deck to Slides", or "make it editable for the team". Not for decks that already live in Google Slides.
---

# gslider: HTML deck to Google Slides

Draft and iterate in HTML. Build the Google Slides deck once, near final, with `gslider`. Last-mile edits and comments happen in Slides. Never rebuild a deck after anyone has commented on it.

The tool lives in the HTML_Gslider repo. Run it from the repo with `python3 -m gslider.cli` (the `gslider` command exists only after `pip install -e .`).

```bash
GSLIDER_HOME="${GSLIDER_HOME:-$HOME/Projects/HTML_Gslider}"
cd "$GSLIDER_HOME" && python3 -m gslider.cli --help
```

## First-time setup (once per machine)

Check each line; do only the missing ones.

1. Repo present: `test -d "$GSLIDER_HOME/gslider"`, else `gh repo clone nraford7/HTML_Gslider "$GSLIDER_HOME"`.
2. Python deps: `python3 -c "import bs4, PIL, yaml, pptx, fontTools"`, else `python3 -m pip install --user -e "$GSLIDER_HOME"`.
3. `gws` CLI authenticated: `gws drive files list --params '{"pageSize":1}'` returns JSON. If not, the user runs `gws auth login` themselves.
4. Google Chrome installed (renders fallback slides).
5. Base deck: `test -f ~/.config/gslider/config.json`, else `python3 -m gslider.cli init-base`. This creates "HTML_Gslider base (do not edit)" in the user's Drive. Never delete it.
6. Optional, in `~/.config/gslider/config.json`: decks gslider must never write as `"protected_ids": ["<id>", ...]`; a brand logo as `"brand": {"logo": "<png for dark slides>", "logo_dark": "<png for light slides>"}` (no brand = no logo).

## Build and check

1. Confirm the HTML is close to final and get its absolute path. One slide = one `.slide-wrap`. A slide with `data-slide-id="version-note"` is internal and skipped unless `--include-internal`.
2. Build a new deck:
   ```bash
   python3 -m gslider.cli build "/abs/path/deck.html" --title "Client – Deck name" [--ivory 3,4,14]
   ```
   `--ivory` takes positions in the built deck (1-based, internal slides already skipped). Title defaults to the HTML `<title>`. Takes 1–3 minutes.
3. Check it:
   ```bash
   python3 -m gslider.cli check <presentationId>
   ```
4. Read `out/<deck-slug>/report.md`, then open `out/<deck-slug>/contact.png` and look at every tile yourself. A PASS line is not enough; the contact sheet is how you see the deck.
5. Report to the user: the URL, slides built natively vs fallback (with reasons), the check result, and anything that looked wrong on the contact sheet.

## When slides fall back or the check fails

A **fallback** slide is a picture of the HTML slide: it looks like the HTML, its text is not editable, and the text sits in the speaker notes. The usual reason is `overflow at block N`: the content does not fit the Slides layout.

| Problem | Fix |
|---|---|
| `overflow at block N` | Shorten or split that slide in the HTML, then rebuild |
| `text not placed: …` | The HTML uses a structure the parser does not map. Restructure the slide with standard deck classes, or accept the fallback |
| `unsupported image` | Use PNG, JPEG or GIF in the HTML |
| coverage miss / overflow in report | Fix the HTML slide, rebuild |
| style violation | A bug in gslider, not in the deck: report it, do not hand-fix the slide |

Fix content in the **HTML**. Do not edit `gslider/layouts.yaml` to make one deck fit: it is the shared design system for every deck.

Rebuild into the same deck (same URL) while nobody has commented yet:
```bash
python3 -m gslider.cli build "/abs/path/deck.html" --into <presentationId>
```
`--into` deletes the deck's slides and rebuilds them. It refuses decks gslider did not create, protected decks, and decks with any comment thread.

## After people comment

- Do not rebuild or use `--into`; it would delete the commented slides.
- Make small changes directly in Slides (the user by hand, or a targeted `gws slides presentations batchUpdate` on the element ids gslider set, e.g. `s07_body8` = slide 7, body box 8).
- Copy the same change into the HTML so the two do not drift.
- For a large rework, build a new deck from the updated HTML and tell the user the comments stay on the old one.

## Errors

| Message | Meaning / action |
|---|---|
| `base deck missing; run init-base` | Config points at a deleted base: `init-base --force` |
| `another gslider build … is using …` | Another build of the same HTML is running; wait, or pass `--out <folder>` |
| `refusing to write protected file` | Target is in `protected_ids`: pick another deck |
| `refusing --into …: it has N comment thread(s)` | See "After people comment" |
| `refusing --into …: not created by gslider` | `--into` works only on decks gslider built |
| `no base deck configured` | Run `init-base` once (setup step 5) |
| `retry with --into <pid>` | Build failed midway; the copy exists, rerun with `--into` |

## Privacy

Images used in a deck (cover art, background images, fallback slide renders) are uploaded to the user's Drive folder "HTML_Gslider assets" and shared "anyone with the link can view", because Slides can only insert images it can fetch. `build.json` lists every shared file id. Say this before building a confidential deck.

## Never

- Never build a new deck by copying an existing Slides deck and editing it box by box with one-off scripts, and never import a PPTX. Use gslider.
- Never write to a deck gslider did not build (decks designed by hand in Slides stay hand-edited).
- Never say the deck is done without reading the report and looking at the contact sheet.
