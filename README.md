# HTML_Gslider

Build a Google Slides deck from an HTML slide deck in one pass.

How to run it: [docs/USAGE.md](docs/USAGE.md).

## Install (Claude Code)

```bash
gh repo clone nraford7/HTML_Gslider ~/Projects/HTML_Gslider
bash ~/Projects/HTML_Gslider/scripts/install.sh --init-base
```

This installs the Python dependencies, links the `/gslider` skill into `~/.claude/skills/`, checks the `gws` CLI sign-in, and creates one blank base deck in your Drive. Then type `/gslider` in Claude Code. The skill is in `skills/gslider/SKILL.md`.

Optional: list decks gslider must never write in `~/.config/gslider/config.json` as `"protected_ids": [...]`.

Optional: a brand logo on every slide. Add PNG paths to `~/.config/gslider/config.json`: `"brand": {"logo": "<png for dark slides>", "logo_dark": "<png for light slides>"}`. One file is used for both when only one is set; with none, slides have no logo.
