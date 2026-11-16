"""Command line: gslider parse | build | check | init-base.

Runs as the console script `gslider` or as `python3 -m gslider.cli` (no install needed).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

if __package__ in (None, ""):  # `python3 gslider/cli.py`: make the package importable
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from gslider import build as B  # noqa: E402
from gslider import gws  # noqa: E402
from gslider.parse import norm, parse, to_jsonable  # noqa: E402


def _ivory(s: str) -> set[int]:
    try:
        return {int(x) for x in s.replace(" ", "").split(",") if x}
    except ValueError:
        raise argparse.ArgumentTypeError(f"--ivory takes slide numbers like 3,4,14, got {s!r}")


def _html_title(path: Path) -> str | None:
    """The HTML <title> text (whitespace collapsed), read without parsing the slides."""
    from bs4 import BeautifulSoup, SoupStrainer

    # svg subtrees are kept so their own <title> (an icon label) can be told apart and skipped
    soup = BeautifulSoup(Path(path).read_text(encoding="utf-8"), "html.parser",
                         parse_only=SoupStrainer(["title", "svg"]))
    title = next((t for t in soup.find_all("title") if t.find_parent("svg") is None), None)
    return (norm(title.get_text()) if title else None) or None


def _parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="gslider", description="Build editable Google Slides decks from HTML slide decks.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("parse", help="HTML deck -> deck.json (no network)")
    p.add_argument("html", type=Path)
    p.add_argument("--include-internal", action="store_true", help="keep internal slides (version-note)")
    p.add_argument("--out", type=Path, help="output folder (default out/<slug>/); writes deck.json there")

    b = sub.add_parser("build", help="create a new Google Slides deck from an HTML deck")
    b.add_argument("html", type=Path)
    b.add_argument("--title", help="deck title in Drive (default: HTML <title> or file name)")
    b.add_argument("--ivory", type=_ivory, default=set(), metavar="N,N",
                   help="force these slides to the ivory ground; 1-based positions in the built deck, "
                        "counted after internal slides are skipped (e.g. 3,4,14)")
    b.add_argument("--include-internal", action="store_true", help="keep internal slides (version-note)")
    b.add_argument("--into", metavar="PRESENTATION_ID",
                   help="rebuild into an existing gslider deck (all its slides are replaced); refused if the "
                        "deck was not made by gslider or has any comment thread")
    b.add_argument("--out", type=Path, help="output folder (default out/<slug>/)")

    c = sub.add_parser("check", help="coverage, overflow, style audit, contact sheet for a built deck")
    c.add_argument("pid", metavar="PRESENTATION_ID")
    c.add_argument("--out", type=Path, help="build output folder (default: found by presentation id under out/)")

    i = sub.add_parser("init-base", help="create the 960x540pt base deck once (id saved to ~/.config/gslider)")
    i.add_argument("--force", action="store_true", help="make a new base even if one is configured")
    return ap


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.cmd == "parse":
            deck = parse(args.html.read_text(encoding="utf-8"), include_internal=args.include_internal,
                         source=str(args.html))
            out = (args.out or B.OUT / B.slugify(args.html.stem)) / "deck.json"
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(to_jsonable(deck), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(out)
        elif args.cmd == "build":
            title = args.title or _html_title(args.html) or args.html.stem
            res = B.build(args.html, title, ivory=args.ivory, include_internal=args.include_internal,
                          into=args.into, out_dir=args.out)
            fb = [p["n"] for p in res.plans if p["status"] == "fallback"]
            print(f"{res.url}\n{len(res.plans)} slides, {len(res.plans) - len(fb)} native"
                  + (f", fallback: {', '.join(map(str, fb))}" if fb else "") + f"\noutput: {res.out_dir}")
        elif args.cmd == "check":
            from gslider import check as C

            out = args.out or B.find_build(args.pid)
            if out is None:
                print(f"no out/*/build.json for {args.pid}; pass --out", file=sys.stderr)
                return 2
            out = Path(out)
            try:
                built_pid = json.loads((out / "build.json").read_text(encoding="utf-8")).get("pid")
            except (OSError, json.JSONDecodeError):
                built_pid = None
            if built_pid and built_pid != args.pid:
                print(f"gslider: {out}/build.json is for {built_pid}, not {args.pid}; "
                      "pass the matching --out", file=sys.stderr)
                return 2
            deck = json.loads((out / "deck.json").read_text(encoding="utf-8"))
            plans = json.loads((out / "plans.json").read_text(encoding="utf-8"))
            res = C.check(args.pid, deck, plans, out)
            print(f"report: {res.report}\ncontact sheet: {res.contact}\nok: {res.ok}")
            return 0 if res.ok else 1
        elif args.cmd == "init-base":
            pid = B.init_base(force=args.force)
            print(f"base deck {pid}\n{B.deck_url(pid)}\nsaved to {B.CONFIG_PATH}")
    except (B.BuildError, gws.GwsError, OSError, RuntimeError, ValueError) as e:
        msg = " | ".join(line.strip() for line in str(e).splitlines() if line.strip()) or repr(e)
        print(f"gslider: {msg}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
