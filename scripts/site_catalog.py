"""Read and write the shared static catalog files."""
import json
from pathlib import Path
import re


DATA_PATTERN = re.compile(r'(<script id="catalog-data" type="application/json">)(.*?)(</script>)', re.S)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def read_json(path):
    return json.loads(Path(path).read_text(encoding="utf-8"))


def json_text(value):
    return json.dumps(value, ensure_ascii=False, indent=2) + "\n"


def catalog_rows(catalog):
    require(isinstance(catalog, dict) and isinstance(catalog.get("articles"), list), "Invalid catalog")
    rows = {row["id"]: row for row in catalog["articles"]}
    require(len(rows) == len(catalog["articles"]), "Duplicate catalog IDs")
    return rows


def read_site_catalog(root):
    root = Path(root)
    catalog = read_json(root / "data/catalog.json")
    metadata = catalog["metadata"]
    require(metadata == read_json(root / "data/validation.json"), "Catalog and validation metadata differ")
    html = (root / "index.html").read_text(encoding="utf-8")
    matches = list(DATA_PATTERN.finditer(html))
    require(len(matches) == 1, "Expected exactly one embedded catalog")
    match = matches[0]
    embedded = json.loads(match[2])
    require(embedded.get("records") == catalog["articles"] and embedded.get("report") == metadata,
            "Embedded catalog differs from catalog.json")
    return catalog, catalog_rows(catalog), embedded, html, match


def verify_export_inventory(rows, embedded):
    order = embedded.get("exportOrder")
    require(isinstance(order, list) and len(order) == len(rows) and set(order) == rows.keys(),
            "Embedded export order differs from the catalog")
    return order


def catalog_files(catalog, embedded, html, match):
    metadata = catalog["metadata"]
    embedded["records"] = catalog["articles"]
    embedded["report"] = metadata
    body = json.dumps(embedded, ensure_ascii=False).replace("&", "\\u0026").replace("<", "\\u003c").replace(">", "\\u003e")
    return {"data/catalog.json": json_text(catalog), "data/validation.json": json_text(metadata),
            "index.html": html[:match.start(2)] + body + html[match.end(2):]}
