#!/usr/bin/env python3
"""Regenerate ``docs/sitemap.xml`` (and the page's visible/structured update date) from git.

Each URL's ``<lastmod>`` — and the homepage's JSON-LD ``dateModified`` and footer ``<time>`` —
is set to the date of the most recent *content* commit that touched that page. This script's own
refresh commits are marked with :data:`SITEMAP_MARKER` and excluded from that lookup, so running it
is idempotent: re-running with no content change reproduces the identical files and never advances
the date on its own.

Run it locally (``python docs/generate_sitemap.py``) or let
``.github/workflows/docs-sitemap.yml`` run it automatically on every push that touches ``docs/``.
Exit status is 0 whether or not anything changed; the workflow decides whether to commit from the
git diff. Uses only the standard library so it needs no install step.
"""

from __future__ import annotations

import datetime as _dt
import re
import subprocess
from pathlib import Path

SITE = "https://agitrack.core-aix.org"
DOCS = Path(__file__).resolve().parent
REPO = DOCS.parent
# The commit-message marker this script's own auto-commits carry, so a timestamp refresh is not
# itself mistaken for a content change on the next run (which would otherwise advance the date).
SITEMAP_MARKER = "auto-update sitemap timestamps"

# (file under docs/, URL path, sitemap priority). Order defines sitemap order.
PAGES: list[tuple[str, str, str]] = [
    ("index.html", "/", "1.0"),
    ("docs.html", "/docs.html", "0.8"),
]


def _last_content_date(rel_path: str) -> str:
    """``YYYY-MM-DD`` of the last commit that changed ``docs/<rel_path>`` and was NOT one of this
    script's own timestamp refreshes. Falls back to today when git history can't be read (e.g. an
    unpushed working copy or a shallow checkout with no matching commit yet)."""
    try:
        out = subprocess.run(
            [
                "git",
                "log",
                "-1",
                "--format=%cs",  # committer date, YYYY-MM-DD
                "--invert-grep",
                f"--grep={SITEMAP_MARKER}",
                "--",
                f"docs/{rel_path}",
            ],
            cwd=REPO,
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
        if out:
            return out
    except (subprocess.CalledProcessError, OSError):
        pass
    return _dt.date.today().isoformat()


def build_sitemap(dates: dict[str, str]) -> str:
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
    ]
    for rel, path, priority in PAGES:
        lines += [
            "  <url>",
            f"    <loc>{SITE}{path}</loc>",
            f"    <lastmod>{dates[rel]}</lastmod>",
            "    <changefreq>weekly</changefreq>",
            f"    <priority>{priority}</priority>",
            "  </url>",
        ]
    lines.append("</urlset>")
    return "\n".join(lines) + "\n"


def _refresh_index_dates(index_date: str) -> bool:
    """Keep the homepage's structured ``dateModified`` and its footer ``<time>`` in step with the
    sitemap, so every place the update date appears agrees. Returns True if the file changed."""
    path = DOCS / "index.html"
    html = original = path.read_text(encoding="utf-8")
    html = re.sub(r'("dateModified":\s*)"[^"]*"', rf'\1"{index_date}"', html)
    html = re.sub(
        r'(<time datetime=")[^"]*(">)[^<]*(</time>)',
        rf"\g<1>{index_date}\g<2>{index_date}\g<3>",
        html,
    )
    if html != original:
        path.write_text(html, encoding="utf-8")
        return True
    return False


def main() -> int:
    dates = {rel: _last_content_date(rel) for rel, _path, _prio in PAGES}

    sitemap_path = DOCS / "sitemap.xml"
    sitemap = build_sitemap(dates)
    changed = False
    if not sitemap_path.exists() or sitemap_path.read_text(encoding="utf-8") != sitemap:
        sitemap_path.write_text(sitemap, encoding="utf-8")
        changed = True

    if _refresh_index_dates(dates["index.html"]):
        changed = True

    print(
        "sitemap: "
        + ("updated" if changed else "already current")
        + " ("
        + ", ".join(f"{rel}={dates[rel]}" for rel, _p, _pr in PAGES)
        + ")"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
