"""Copy the reviewed public export into this standalone Pages repository."""

from __future__ import annotations

import argparse
import json
import shutil
from pathlib import Path


FILES = (
    "catalog.json",
    "original-authors.jsonl",
    "authors.json",
    "keywords.json",
    "keyword-aliases.json",
    "keyword-per-article.json",
    "keyword-normalization-review.json",
    "validation.json",
    "user-corrections.json",
)
FORBIDDEN = ("/Users/", "file://", "127.0.0.1", "localhost")
CANONICAL_URL = "https://l-m-sherlock.github.io/thoughts-memo-translations/"
REPOSITORY_URL = "https://github.com/L-M-Sherlock/thoughts-memo-translations"


def assert_public(path: Path, text: str) -> None:
    found = [token for token in FORBIDDEN if token in text]
    if found:
        raise ValueError(f"{path} contains non-public references: {found}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("export", type=Path, help="reviewed thoughts-memo-translations export directory")
    args = parser.parse_args()

    source = args.export.resolve()
    root = Path(__file__).resolve().parents[1]
    data = root / "data"
    data.mkdir(exist_ok=True)

    html_path = source / "catalog.html"
    html = html_path.read_text(encoding="utf-8")
    assert_public(html_path, html)
    canonical = f'<link rel="canonical" href="{CANONICAL_URL}">'
    if canonical not in html:
        html = html.replace("</head>", canonical + "</head>")
    downloads = (
        '<p>项目与数据：'
        f'<a href="{REPOSITORY_URL}">GitHub</a> · '
        '<a href="./data/catalog.json">目录 JSON</a> · '
        '<a href="./data/authors.json">作者索引</a> · '
        '<a href="./data/keywords.json">关键词索引</a>'
        "</p>"
    )
    if downloads not in html:
        html = html.replace("</footer>", downloads + "</footer>")
    assert_public(root / "index.html", html)
    (root / "index.html").write_text(html, encoding="utf-8")

    for name in FILES:
        src = source / name
        text = src.read_text(encoding="utf-8")
        assert_public(src, text)
        # Parse structured data before publishing it.
        if name.endswith(".json"):
            json.loads(text)
        elif name.endswith(".jsonl"):
            for line_number, line in enumerate(text.splitlines(), 1):
                if line.strip():
                    try:
                        json.loads(line)
                    except json.JSONDecodeError as error:
                        raise ValueError(f"{name}:{line_number}: {error}") from error
        shutil.copyfile(src, data / name)

    validation = json.loads((data / "validation.json").read_text(encoding="utf-8"))
    if validation["included_articles"] != 2119:
        raise ValueError("unexpected article count")
    if validation["original_authors"]["status_counts"] != {"resolved": 2119}:
        raise ValueError("unresolved author metadata")
    print(
        json.dumps(
            {
                "articles": validation["included_articles"],
                "keywords": validation["unique_keywords"],
                "destination": str(root),
            },
            ensure_ascii=False,
        )
    )


if __name__ == "__main__":
    main()

