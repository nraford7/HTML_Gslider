# gslider

An HTML slide deck is quick to write and easy to change, but your colleagues can't edit or comment on it. gslider builds a Google Slides deck from the HTML in one pass, with every title, paragraph, column and table as editable text, then checks that nothing was lost.

## The problem

HTML is a good place to draft a deck. An AI assistant can write it, a browser shows it, and a diff shows what changed. The people who review it work in Google Slides, where they edit, comment and present.

The usual ways across all cost something:

- **Export to PDF.** The text is no longer editable.
- **Import a PowerPoint file.** The text boxes arrive with uneven inner padding, so columns no longer line up and every fix is manual.
- **Copy an existing Slides deck and edit it through the API.** It works, but each change becomes its own script, aimed at box IDs and coordinates instead of slides and content.

## What gslider does

It reads the HTML once and creates a new Google Slides deck. There are two styles:

- **html** (the default) keeps the look of your HTML. Chrome draws each slide, gslider measures where every piece of text sits, and the Slides deck gets a picture of everything that is not text (backgrounds, rules, shapes, images) with editable text boxes on top, in the same places and fonts.
- **template** (`--style template`) ignores the HTML's design. Each title, lead, column, table and timeline entry is placed by a fixed layout and styled by its role, so a header looks the same on every slide.

When a slide cannot be built (in template style, content that does not fit the layout; in html style, a font that did not load), gslider inserts a picture of the HTML slide, puts the slide's text in the speaker notes, and tells you which slide and why.

Then it checks the result against the HTML and writes a report with a contact sheet of every slide. In template style, a 15-slide proposal builds in about a minute with 13 slides as editable text.

## How it works

html style:

1. **Measure.** For each slide, headless Chrome opens a copy of the HTML with only that slide visible at 1920 x 1080. A script waits for the fonts, then records every text block: its position, line height, alignment, and each run of text with its font, size, weight, colour and case.
2. **Plate.** In the same Chrome run, the measured text is made transparent and the slide is saved as a PNG. This "plate" holds everything that is not editable text.
3. **Compile.** 1920 px is 960 pt, so each block becomes a text box at half its pixel position, with a little extra width so Slides does not wrap a line earlier than Chrome did. Fonts that Google Fonts serves are used by name; any other font is replaced by Arial, Georgia or Courier New and listed in the report.
4. **Build and check.** The plate goes in as the bottom layer of each slide, the text boxes on top. The check confirms all visible text is on its slide and nothing overflows, compares every slide with the Chrome render (side by side in `compare.png`), and reads back where Slides put each box.

template style:

1. **Parse.** Each `.slide-wrap` in the HTML becomes a list of blocks: section label, title, lead, a row of columns, a table, a timeline, a band, an image. Repeated page furniture (wordmark, footer, page number) is set aside, because the layout draws its own.
2. **Compile.** The blocks are stacked down a 960 x 540 pt page using the geometry and type styles in `gslider/layouts.yaml`. Text is measured with the same Google Fonts files that Slides draws with, so gslider knows where each line will wrap and whether a slide will overflow before anything is sent.
3. **Build.** gslider copies a blank 16:9 base deck in your Drive and sends one batch of Slides API requests per slide through the [`gws`](https://github.com/googleworkspace/cli) command-line tool. Every element gets a readable ID: `s07_body8` is slide 7, body box 8.
4. **Check.** It confirms that every string in the HTML appears on its slide, that no text box overflows, and that each role (title, header, body) has exactly one style. The report and contact sheet go to `out/<deck-name>/`.

## Requirements

- Python 3.11 or later
- The `gws` CLI, signed in to your Google account
- Google Chrome: html style measures every slide with it, and both styles use it for fallback pictures (set `GSLIDER_CHROME` if it is not in a standard place)
- Tested on macOS

## Install

```bash
gh repo clone nraford7/HTML_Gslider ~/Projects/HTML_Gslider
bash ~/Projects/HTML_Gslider/scripts/install.sh --init-base
```

The installer adds the Python dependencies, links the `/gslider` skill for Claude Code into `~/.claude/skills/`, checks your `gws` sign-in, and creates one blank base deck in your Drive. Keep that deck, because every build copies it.

## Use

```bash
python3 -m gslider.cli build /path/to/deck.html --title "Q3 review"
python3 -m gslider.cli check <presentation-id>
```

`build` prints the new deck's URL. Add `--style template` for the fixed design; with it, `--ivory 3,4` gives slides 3 and 4 the light background. In Claude Code, type `/gslider` and it runs the build and check for you, reads the report, and looks at the contact sheet. More options: [docs/USAGE.md](docs/USAGE.md).

## What the HTML should look like

One `.slide-wrap` per slide, each drawn at 1920 x 1080 px. In html style that is all: any design works. In template style, gslider reads these classes. A minimal slide that builds as editable text:

```html
<div class="slide-wrap"><div class="slide dk">
  <div class="header-bar"><span class="header-sec">Quarterly review</span></div>
  <div class="content">
    <div class="content-header"><h2 class="h2">Three things changed this quarter.</h2></div>
    <p class="lead">Each one moves the plan for next year.</p>
    <div class="cols-3x">
      <div class="col"><div class="c-title">Pricing</div><p class="c-p">We moved two plans to annual billing.</p></div>
      <div class="col"><div class="c-title">Hiring</div><p class="c-p">Four engineers joined the platform team.</p></div>
      <div class="col"><div class="c-title">Churn</div><p class="c-p">Monthly churn fell from 3.1% to 2.4%.</p></div>
    </div>
  </div>
</div></div>
```

`.dk` gives the slide the dark background; leave it off for the light one. gslider also reads tables (`table.tbl`), timelines (`.timeline .tl`), bands (`.band`), stats (`.stats .stat`), images embedded as data URIs, and cover and closing slides. The full list of blocks is at the top of `gslider/parse.py`.

## Limits

- **Build once, near the end.** gslider is a one-way trip. After someone comments, make changes in Slides. Rebuilding with `--into` replaces every slide, and gslider refuses to do it on a deck that has comments.
- **Images are shared by link.** Slides can only insert an image it can download, so logos, background images, plates and pictures of fallback slides are uploaded to a folder in your Drive with "anyone with the link can view". `out/<deck-name>/build.json` lists every shared file, with the plates also under `plates`. Think about this before building a confidential deck.
- **html style: plates are pictures.** Backgrounds, rules, shapes, icons and images are one picture per slide. You can move and edit the text, not the shapes under it.
- **html style: some text stays in the picture.** Rotated, scaled or clipped text, and text inside SVG, cannot become a text box. It stays in the plate, and the report lists it under "Left in plate".
- **html style: letter-spacing is dropped.** Slides has no letter-spacing. Spaced-out labels are set without it, and the report lists each one.
- **html style: fonts must load.** A slide whose web font does not load in Chrome becomes a picture, with the reason in the report.
- **template style: one look.** The design uses dark and light backgrounds with Playfair Display and Libre Franklin. To change it, edit `gslider/layouts.yaml`.
- **Not supported:** charts, animation, and images linked by URL (embed them as data URIs, or the slide falls back).

## Safety

gslider writes only to decks it created itself, which it marks in Drive. It refuses to rebuild a deck with comments, and it never touches any deck ID you list under `protected_ids` in `~/.config/gslider/config.json`.

## Your logo

Add one or two PNGs to `~/.config/gslider/config.json`:

```json
"brand": {"logo": "/path/to/logo-for-dark-slides.png", "logo_dark": "/path/to/logo-for-light-slides.png"}
```

One file is used for both backgrounds when only one is set. With none, slides have no logo.

## License

MIT. See [LICENSE](LICENSE).
