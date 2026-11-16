# Using gslider

Run as `gslider ...` after `pip install -e .`, or as `python3 -m gslider.cli ...` from the repo. Google calls go through the `gws` CLI, which must already be signed in.

1. Once per machine: `gslider init-base`. This makes a blank 960×540pt base deck and saves its id to `~/.config/gslider/config.json`. To test the new deck, it adds one blank slide and deletes it again.
2. Build: `gslider build deck.html --title "Client – Proposal"`. Each build makes a NEW deck and prints its URL, the native/fallback count and the output folder. Without `--title`, the deck title is the HTML `<title>` (or the file name when the HTML has none).
3. Check: `gslider check <presentationId>`. This writes `report.md` (coverage, overflow, style audit, slide count, build status) and `contact.png` (all slides, 4 per row). The exit code is 0 only when all checks pass. A check fails when `build.json` says the build did not finish. With `--out`, the folder must belong to the same presentation id (exit 2 otherwise).

Build flags:

- `--ivory 3,4,14` sets these slides to the ivory ground. The numbers are 1-based positions in the built deck, counted after internal slides (version-note) are removed.
- `--include-internal` keeps internal slides.
- `--into <presentationId>` replaces all slides of a deck that gslider made earlier. It refuses any deck gslider did not make, and any deck with a comment thread. It checks for comments again just before it removes the old slides.
- `--out <dir>` sets the output folder. Only one build at a time can use a folder. If a second build of the same HTML is started, it stops at once; give it a different `--out`.

If a build fails:

- The error says which slide failed and gives the deck URL. `build.json` gets `status: "failed"`, the error text and the last good slide.
- When a deck was already made, the message says `retry with --into <presentationId>`. Use that command to rebuild into the same deck.
- An `--into` build removes the old slides only when it starts to build slides (after the images are uploaded). If it fails before that step, the deck is not changed. If it fails after that step, the deck keeps only the slides built before the failure. Retry the `--into` build to fill it again.
- Speaker notes for fallback slides are written after all slides are built. If only the notes fail, all slides are in the deck and `build.json` says `failed`. Rerun with `--into <presentationId>` to add the notes. This rebuilds the whole deck.

Rule: after anyone comments in Slides, do not re-upload. Make the later edits in Slides by hand.

Outputs go to `out/<slugified-name>/` (not in git). The folder name is the HTML file name in lower case, with accents removed and other characters changed to `-` (for example `reference-deck.html` gives `out/reference-deck/`). The folder holds `deck.json`, `plans.json`, `build.json`, `fallback/` renders, `thumbs/`, `contact.png` and `report.md`.

Images are uploaded to the Drive folder `HTML_Gslider assets` (a folder you own) and shared view-only by link. This includes the fallback slides. Each one is a PNG of the HTML slide, uploaded and shared by link like the other images. `build.json` lists every shared file id in `shared_assets`.

Fallback slides are rendered with Google Chrome. gslider uses `GSLIDER_CHROME` when it is set (the path to the Chrome or Chromium program), else the Google Chrome app on macOS, else `google-chrome` or `chromium` on the PATH. If `GSLIDER_CHROME` names a file that does not exist, or no Chrome is found, a build that needs a fallback slide stops before it uploads anything.

Fallback slides are images, so they look like the HTML and not like the native slides (kicker case, rules, page-number position). Their text cannot be edited in Slides. The text is in the slide's speaker notes.

Optional: a brand logo on every slide. Add PNG paths to `~/.config/gslider/config.json`: `"brand": {"logo": "<png for dark slides>", "logo_dark": "<png for light slides>"}`. One file is used for both when only one is set; with none, slides have no logo.
