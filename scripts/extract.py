#!/usr/bin/env python3
"""Propose reviewable paper regions; never translate or mark pages reviewed.

Dependencies: pdfplumber, pypdf, pypdfium2 (Pillow for preview PNGs).
All manifest boxes refer to the normalized, visible CropBox page: PDF points,
origin top-left. Source bytes are read only. No OCR or network is performed.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import math
import re
import statistics
import sys
from pathlib import Path
from typing import Any

import pdfplumber
import pypdfium2 as pdfium
from pypdf import PdfReader, PdfWriter, Transformation
from pypdf.errors import PdfReadError
from pypdf.generic import ArrayObject, DecodedStreamObject, DictionaryObject, FloatObject, NameObject, RectangleObject


LIMITATIONS = [
    "Reading order, paragraph boundaries and roles are proposals. Inspect every rendered source page before setting reviewed=true.",
    "Raster bounds do not prove complete figure bounds: panel letters, vector overlays, legends and scales may extend outside an image object. Review and expand/merge regions.",
    "Unruled tables, isolated mathematical notation and some vector figures cannot be reliably detected automatically. Add complete preserve regions manually; keep captions separate.",
    "Original characters, including unsupported CID markers, superscripts and line-end hyphens, are retained for audit; do not guess missing glyphs or silently delete characters.",
    "Preview PNGs are inspection aids only. The compositor must copy preserve regions directly from the normalized PDF, never use previews as final figure assets.",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for part in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def normalized_reader(source: str | Path | bytes) -> tuple[PdfReader, list[dict[str, Any]]]:
    """Return an in-memory reader with visible pages at (0,0), rotation zero.

    Normalize content and Link Rect/QuadPoints with the same CropBox transform.
    URI and destination objects are retained. Non-Link annotation appearance
    transformations are not guaranteed and are explicitly reported per page.
    The input file is never opened for writing. Importing this module does not
    execute the command-line interface.
    """
    raw = source if isinstance(source, bytes) else Path(source).expanduser().read_bytes()
    reader = PdfReader(io.BytesIO(raw), strict=False)
    if reader.is_encrypted:
        if reader.decrypt("") == 0:
            raise ValueError("Encrypted PDF requires a password. Export an authorized unlocked copy first.")
    notes: list[dict[str, Any]] = []
    for number, page in enumerate(reader.pages, 1):
        rotation = int(page.get("/Rotate", 0)) % 360
        if rotation not in (0, 90, 180, 270):
            raise ValueError(f"Page {number}: unsupported rotation {rotation}; export pages with 0/90/180/270 degree rotation.")
        user_unit = float(page.get("/UserUnit", 1))
        if not math.isclose(user_unit, 1, abs_tol=1e-8):
            raise ValueError(f"Page {number}: /UserUnit={user_unit} is unsupported. Print/export to a PDF using ordinary 1/72-inch points before extraction.")
        l, b, r, t = [float(v) for v in page.cropbox]
        if not all(math.isfinite(v) for v in (l, b, r, t)) or r <= l or t <= b:
            raise ValueError(f"Page {number}: invalid CropBox. Repair/export this page before extraction.")
        ml, mb, mr, mt = [float(v) for v in page.mediabox]
        if l < ml - .01 or b < mb - .01 or r > mr + .01 or t > mt + .01:
            raise ValueError(f"Page {number}: CropBox extends outside MediaBox; export a copy with a valid visible page before extraction.")
        page_warnings: list[str] = []

        def point(x: float, y: float) -> tuple[float, float]:
            if rotation == 90:
                return y - b, r - x
            if rotation == 180:
                return r - x, t - y
            if rotation == 270:
                return t - y, x - l
            return x - l, y - b

        # Pages may reuse the same /Contents or annotation objects. Detach the
        # objects which we mutate so normalizing one page cannot transform another.
        original_contents = page.get_contents()
        if original_contents is not None:
            detached = DecodedStreamObject()
            detached.set_data(original_contents.get_data())
            page[NameObject("/Contents")] = detached
        link_count = 0
        annotations = page.get("/Annots", [])
        if hasattr(annotations, "get_object"):
            annotations = annotations.get_object()
        detached_annotations = ArrayObject()
        for ref in annotations:
            original_annot = ref.get_object()
            annot = DictionaryObject()
            for key, value in original_annot.items():
                annot[key] = value
            detached_annotations.append(annot)
            subtype = str(annot.get("/Subtype", ""))
            if annot.get("/AP") and (rotation or l or b):
                page_warnings.append("Annotation contains a custom appearance stream: inspect its normalized orientation and clipping before review.")
            if subtype != "/Link":
                page_warnings.append(f"Non-Link annotation {subtype or '(unknown)'}: its appearance may not match the normalized geometry; inspect source and preview.")
            rect = annot.get("/Rect")
            if rect and len(rect) == 4:
                a, c, d, e = [float(v) for v in rect]
                corners = [point(x, y) for x, y in ((a, c), (a, e), (d, c), (d, e))]
                annot[NameObject("/Rect")] = RectangleObject([
                    min(v[0] for v in corners), min(v[1] for v in corners),
                    max(v[0] for v in corners), max(v[1] for v in corners),
                ])
            elif subtype == "/Link":
                page_warnings.append("Link annotation has no valid rectangle; reconstruct its hotspot after geometry review.")
            quad = annot.get("/QuadPoints")
            if quad:
                if len(quad) % 8:
                    page_warnings.append("Annotation has malformed QuadPoints; inspect or reconstruct the hotspot.")
                else:
                    values = []
                    for j in range(0, len(quad), 2):
                        values.extend(point(float(quad[j]), float(quad[j + 1])))
                    annot[NameObject("/QuadPoints")] = ArrayObject([FloatObject(v) for v in values])
            if subtype == "/Link":
                link_count += 1
        if annotations:
            page[NameObject("/Annots")] = detached_annotations
        if rotation:
            page.rotation = rotation
            page.transfer_rotation_to_content()
        else:
            page.rotation = 0
        cb = page.cropbox
        xoff, yoff = float(cb.left), float(cb.bottom)
        width, height = float(cb.width), float(cb.height)
        if xoff or yoff:
            page.add_transformation(Transformation().translate(-xoff, -yoff))
        page.mediabox = RectangleObject([0, 0, width, height])
        page.cropbox = RectangleObject([0, 0, width, height])
        # These optional boxes otherwise can retain an obsolete coordinate system.
        for key in ("/TrimBox", "/BleedBox", "/ArtBox"):
            if key in page:
                page[NameObject(key)] = RectangleObject([0, 0, width, height])
        notes.append({
            "page": number,
            "rotation_before": rotation,
            "cropbox_before": [l, b, r, t],
            "normalized_size": [width, height],
            "transformed_link_count": link_count,
            "warnings": sorted(set(page_warnings)),
        })
    # Re-serialize before any consumer reads it, preventing stale cached page trees.
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    target = io.BytesIO()
    writer.write(target)
    target.seek(0)
    return PdfReader(target, strict=False), notes


def reader_bytes(reader: PdfReader) -> bytes:
    writer = PdfWriter()
    for page in reader.pages:
        writer.add_page(page)
    target = io.BytesIO()
    writer.write(target)
    return target.getvalue()


def parse_pages(value: str | None, count: int) -> list[int]:
    if value is None:
        return list(range(1, count + 1))
    result: list[int] = []
    try:
        for part in value.split(","):
            part = part.strip()
            if not part:
                raise ValueError
            if "-" in part:
                bits = part.split("-")
                if len(bits) != 2:
                    raise ValueError
                first, last = map(int, bits)
                if first > last:
                    raise ValueError
                candidates = range(first, last + 1)
            else:
                candidates = [int(part)]
            for number in candidates:
                if not 1 <= number <= count:
                    raise ValueError
                if number not in result:
                    result.append(number)
    except ValueError as exc:
        raise ValueError(f"Invalid --pages {value!r}; use one-based pages within 1-{count}, e.g. 1,3,5-7.") from exc
    if not result:
        raise ValueError("Select at least one source page.")
    return result


def box(obj: dict[str, Any]) -> list[float]:
    return [float(obj["x0"]), float(obj["top"]), float(obj["x1"]), float(obj["bottom"])]


def union(boxes: list[list[float]]) -> list[float]:
    return [min(v[0] for v in boxes), min(v[1] for v in boxes),
            max(v[2] for v in boxes), max(v[3] for v in boxes)]


def area(bbox: list[float]) -> float:
    return max(0, bbox[2] - bbox[0]) * max(0, bbox[3] - bbox[1])


def contains_char(bbox: list[float], char: dict[str, Any]) -> bool:
    cx = (float(char["x0"]) + float(char["x1"])) / 2
    cy = (float(char["top"]) + float(char["bottom"])) / 2
    return bbox[0] <= cx <= bbox[2] and bbox[1] <= cy <= bbox[3]


def overlap(a: list[float], b: list[float], pad: float = 0) -> bool:
    return a[0] <= b[2] + pad and b[0] <= a[2] + pad and a[1] <= b[3] + pad and b[1] <= a[3] + pad


def clip(bbox: list[float], width: float, height: float, pad: float = 0) -> list[float]:
    return [max(0, bbox[0] - pad), max(0, bbox[1] - pad),
            min(width, bbox[2] + pad), min(height, bbox[3] + pad)]


def char_text(chars: list[dict[str, Any]]) -> str:
    """Keep every character; infer only whitespace, never dehyphenate or correct."""
    chars = sorted(chars, key=lambda c: (c["x0"], c["top"], c["_index"]))
    parts: list[str] = []
    previous = None
    for char in chars:
        text = str(char.get("text", ""))
        if previous is not None:
            gap = float(char["x0"]) - float(previous["x1"])
            size = min(float(char.get("size", 9)), float(previous.get("size", 9)))
            if gap > max(.6, size * .12) and parts and not parts[-1].endswith(" ") and not text.startswith(" "):
                parts.append(" ")
        parts.append(text)
        previous = char
    return "".join(parts).strip()


def make_lines(chars: list[dict[str, Any]], width: float) -> list[dict[str, Any]]:
    """Cluster actual characters into physical lines, then split column gaps."""
    rows: list[dict[str, Any]] = []
    for char in sorted(chars, key=lambda c: (c["top"], c["x0"], c["_index"])):
        top, bottom = float(char["top"]), float(char["bottom"])
        middle = (top + bottom) / 2
        best = None
        distance = float("inf")
        for row in rows[-8:]:
            rt, rb = row["top"], row["bottom"]
            height = min(bottom - top, rb - rt)
            vertical_overlap = min(bottom, rb) - max(top, rt)
            dy = abs(middle - row["middle"])
            # Include baseline-shifted italics/subscripts, without merging normal lines.
            if height > 0 and vertical_overlap / height >= .52 and dy < max(3, height * .68):
                if dy < distance:
                    best, distance = row, dy
        if best is None:
            rows.append({"top": top, "bottom": bottom, "middle": middle, "chars": [char]})
        else:
            best["chars"].append(char)
            best["top"] = min(best["top"], top)
            best["bottom"] = max(best["bottom"], bottom)
            # Robust middle does not drift toward one superscript.
            best["middle"] = statistics.median((c["top"] + c["bottom"]) / 2 for c in best["chars"])
    lines: list[dict[str, Any]] = []
    for row in rows:
        ordered = sorted(row["chars"], key=lambda c: (c["x0"], c["_index"]))
        groups: list[list[dict[str, Any]]] = [[]]
        median_size = statistics.median(float(c.get("size", 9)) for c in ordered)
        # A column gap is normally at least 12pt; ordinary word gaps stay intact.
        split_gap = max(12.0, median_size * 1.4)
        for char in ordered:
            if groups[-1] and char["x0"] - groups[-1][-1]["x1"] > split_gap:
                groups.append([])
            groups[-1].append(char)
        for group in groups:
            bbox = union([box(c) for c in group])
            size = statistics.median(float(c.get("size", 9)) for c in group)
            fonts = [str(c.get("fontname", "")) for c in group]
            text = char_text(group)
            lines.append({
                "bbox": bbox, "text": text, "chars": group, "size": size,
                "bold": sum("Bold" in f or "Black" in f for f in fonts) > len(fonts) * .45,
                "lane": "full" if bbox[0] < width * .46 and bbox[2] > width * .54 else ("left" if (bbox[0] + bbox[2]) / 2 < width / 2 else "right"),
            })
    lines.sort(key=lambda v: (v["bbox"][1], v["bbox"][0]))
    # Short last lines of full-width paragraphs must stay with their paragraph.
    # Adjacent mixed-column lines prevent this propagation.
    changed = True
    for _ in range(2):
        if not changed:
            break
        changed = False
        for current in lines:
            if current["lane"] == "full":
                continue
            for neighbour in lines:
                if neighbour["lane"] != "full":
                    continue
                a, b = current["bbox"], neighbour["bbox"]
                gap = min(abs(a[1] - b[3]), abs(b[1] - a[3]))
                if abs(a[0] - b[0]) > 4 or gap > max(3, current["size"] * .7) or abs(current["size"] - neighbour["size"]) > 1:
                    continue
                # Reject if another column has content on current line or between it and neighbour.
                lo, hi = min(a[1], b[1]), max(a[3], b[3])
                rivals = [v for v in lines if v is not current and v is not neighbour and v["lane"] not in ("full", current["lane"])
                          and lo <= (v["bbox"][1] + v["bbox"][3]) / 2 <= hi]
                if not rivals:
                    current["lane"] = "full"
                    changed = True
                    break
    return lines


def detect_regions(page: Any) -> tuple[list[dict[str, Any]], list[str]]:
    regions: list[dict[str, Any]] = []
    warnings: list[str] = []
    width, height = float(page.width), float(page.height)
    page_area = width * height
    for image in page.images:
        bbox = clip(box(image), width, height)
        if area(bbox) >= page_area * .008 and bbox[2] - bbox[0] >= 45 and bbox[3] - bbox[1] >= 30:
            regions.append({"role": "figure", "bbox": bbox, "origin": "raster-image-object"})
    # Raster tiles which touch form one indivisible proposed region.
    changed = True
    while changed:
        changed = False
        for i in range(len(regions)):
            for j in range(i + 1, len(regions)):
                if overlap(regions[i]["bbox"], regions[j]["bbox"], pad=1):
                    regions[i]["bbox"] = union([regions[i]["bbox"], regions[j]["bbox"]])
                    regions.pop(j)
                    changed = True
                    break
            if changed:
                break
    # A substantive connected vector cluster is a candidate, not a semantic proof.
    paths = []
    for obj in list(page.lines) + list(page.rects) + list(page.curves):
        bbox = clip(box(obj), width, height)
        if bbox[2] > bbox[0] or bbox[3] > bbox[1]:
            if not any(overlap(bbox, r["bbox"]) for r in regions):
                paths.append(bbox)
    clusters: list[list[float]] = []
    for bbox in paths:
        hits = [i for i, c in enumerate(clusters) if overlap(c, bbox, pad=7)]
        if not hits:
            clusters.append(bbox)
        else:
            combined = union([bbox] + [clusters[i] for i in hits])
            clusters = [c for i, c in enumerate(clusters) if i not in hits] + [combined]
    for bbox in clusters:
        w, h = bbox[2] - bbox[0], bbox[3] - bbox[1]
        if w > 65 and h > 45 and area(bbox) > page_area * .025:
            candidate = clip(bbox, width, height, pad=8)
            if not any(overlap(candidate, r["bbox"]) for r in regions):
                regions.append({"role": "figure", "bbox": candidate, "origin": "vector-cluster-candidate"})
                warnings.append("Substantial vector graphics detected: proposed preserve region requires checking complete panel labels, scales and surrounding text; separate any table/caption before review.")
    # Ruled tables are detectable; unruled tables remain an explicit manual check.
    try:
        tables = page.find_tables()
        for table in tables:
            bbox = clip(list(table.bbox), width, height)
            if len(table.rows) >= 2 and len(table.cells) >= 4 and area(bbox) > page_area * .005:
                overlapping = [r for r in regions if overlap(bbox, r["bbox"])]
                if not overlapping:
                    regions.append({"role": "table", "bbox": bbox, "origin": "ruled-table-candidate"})
                    warnings.append("Ruled table candidate detected: check its full bounds, cell contents and footnotes; translate its title/caption separately.")
    except Exception as exc:
        warnings.append(f"Table detection could not run ({type(exc).__name__}); inspect this page for complete table regions manually.")
    regions.sort(key=lambda r: (r["bbox"][1], r["bbox"][0]))
    return regions, warnings


def repeated_margin_keys(document: Any) -> set[tuple[str, str]]:
    counts: dict[tuple[str, str], set[int]] = {}
    for number, page in enumerate(document.pages, 1):
        chars = [dict(c, _index=i) for i, c in enumerate(page.chars)
                 if c.get("upright", True) and (c["bottom"] < page.height * .105 or c["top"] > page.height * .925)]
        for line in make_lines(chars, page.width):
            text = line["text"]
            if not text.strip():
                continue
            where = "header" if line["bbox"][1] < page.height / 2 else "footer"
            key = re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip()
            counts.setdefault((where, key), set()).add(number)
    threshold = max(2, math.ceil(len(document.pages) * .25))
    return {key for key, pages in counts.items() if len(pages) >= threshold}


def line_role(line: dict[str, Any], page_number: int, width: float, height: float, median_size: float,
              repeated: set[tuple[str, str]]) -> tuple[str, str, str | None]:
    text, bbox = line["text"], line["bbox"]
    where = "header" if bbox[1] < height / 2 else "footer"
    key = re.sub(r"\d+", "#", re.sub(r"\s+", " ", text)).strip()
    at_margin = (bbox[3] < height * .105 or bbox[1] > height * .925)
    if at_margin and (where, key) in repeated:
        return where, "omit", "Proposed repeated running header/footer; confirm against the source preview before page review."
    if re.match(r"^(?:Figure|Fig\.?|Table)\s*\d+[.:\s]", text, re.I):
        return "caption", "translate", None
    if re.match(r"^(?:Key\s*words?|Keywords)\s*:", text, re.I):
        return "keywords", "translate", None
    if re.fullmatch(r"(?:ABSTRACT|Summary)", text, re.I):
        return "heading", "translate", None
    if re.match(r"^(?:https?://|doi\s*:|\*?Correspondence\s*:|Received:|Revised:|Accepted:|Published:)", text, re.I):
        return "metadata", "preserve", None
    if page_number == 1 and bbox[1] < height * .105:
        return "metadata", "preserve", None
    if page_number == 1 and bbox[1] < height * .27 and bbox[2] - bbox[0] > width * .2 and line["size"] > max(13, median_size * 1.5):
        return "title", "translate", None
    # Equations: preserve standalone symbolic lines. Inline notation stays in text.
    operators = sum(text.count(v) for v in ("=", "∑", "∫", "√", "≤", "≥", "±"))
    if operators and len(text) < 110 and (operators >= 2 or re.search(r"[∑∫√]", text)) and not re.search(r"\b(?:Figure|Table|p|P)\b", text):
        return "equation", "preserve", None
    if re.match(r"^(?:Published by|This is an open access article|[A-Z][a-z]+ [A-Z]\.,)", text):
        return "metadata", "preserve", None
    if len(text) < 130 and ((line["bold"] and line["size"] >= median_size * .98) or (line["size"] > median_size * 1.04 and not re.search(r"[.!?;,]$", text)) or (text.upper() == text and len(text) > 3 and any(c.isalpha() for c in text))):
        return "heading", "translate", None
    return "body", "translate", None


def group_lines(lines: list[dict[str, Any]], page_number: int, width: float, height: float,
                repeated: set[tuple[str, str]]) -> list[dict[str, Any]]:
    sizes = [v["size"] for v in lines if height * .12 < v["bbox"][1] < height * .9]
    median_size = statistics.median(sizes) if sizes else 9
    for line in lines:
        line["role"], line["action"], line["reason"] = line_role(line, page_number, width, height, median_size, repeated)
    # Make paragraph proposals independently in each physical column.
    groups: list[dict[str, Any]] = []
    for lane in ("full", "left", "right"):
        selected = sorted((v for v in lines if v["lane"] == lane), key=lambda v: (v["bbox"][1], v["bbox"][0]))
        current = None
        for line in selected:
            merge = False
            if current:
                previous = current["lines"][-1]
                gap = line["bbox"][1] - current["bbox"][3]
                same_style = abs(line["size"] - previous["size"]) <= 1.2
                indent = line["bbox"][0] - previous["bbox"][0]
                # Caption continuation has the same small-font physical paragraph.
                caption_cont = current["role"] == "caption" and line["role"] in ("body", "heading") and gap <= max(6, line["size"] * .9) and same_style
                metadata_cont = current["role"] == "metadata" and line["role"] == "body" and gap <= max(5, line["size"] * .65) and same_style
                same_role = line["role"] == current["role"] and line["action"] == current["action"]
                # A visible paragraph indent is a real boundary; never discard it.
                new_indent = indent > max(5, line["size"] * .65) and current["role"] == "body"
                # Large vertical gaps also split captions to avoid swallowing body text.
                merge = (same_role or caption_cont or metadata_cont) and same_style and -2 <= gap <= max(4.5, line["size"] * .65) and not new_indent
                if current["role"] in ("header", "footer", "equation", "keywords"):
                    merge = False
            if merge:
                current["lines"].append(line)
                current["bbox"] = union([current["bbox"], line["bbox"]])
                current["text"] += "\n" + line["text"]
                current["chars"].extend(line["chars"])
            else:
                current = {"role": line["role"], "action": line["action"], "bbox": list(line["bbox"]),
                           "text": line["text"], "chars": list(line["chars"]), "lane": lane,
                           "lines": [line]}
                if line["reason"]:
                    current["reason"] = line["reason"]
                groups.append(current)
    # On the opening page, only metadata strictly between title and abstract is preserved.
    # No broad page-level rule can swallow the later introduction.
    if page_number == 1:
        titles = [g for g in groups if g["role"] == "title"]
        abstracts = [g for g in groups if re.fullmatch(r"ABSTRACT|Summary", g["text"], re.I)]
        if titles and abstracts:
            low, high = max(g["bbox"][3] for g in titles), min(g["bbox"][1] for g in abstracts)
            for group in groups:
                if low < group["bbox"][1] and group["bbox"][3] < high:
                    group["role"], group["action"] = "metadata", "preserve"
            # Abstract text begins below its label; stop at keywords/citation/next heading.
            start = min(g["bbox"][3] for g in abstracts)
            for group in sorted(groups, key=lambda g: g["bbox"][1]):
                if group["bbox"][1] <= start:
                    continue
                if group["role"] in ("keywords", "heading") or re.match(r"^[A-Z][a-z]+\s+[A-Z]\.?,", group["text"]):
                    break
                if group["role"] == "body":
                    group["role"] = "abstract"
    return groups


def reading_order(groups: list[dict[str, Any]], width: float) -> list[dict[str, Any]]:
    """Read full-width anchors, with left then right column between anchors."""
    margins = [g for g in groups if g["role"] in ("header", "footer")]
    body = [g for g in groups if g["role"] not in ("header", "footer")]
    anchors = sorted([g for g in body if g.get("lane") == "full" or (g["bbox"][0] < width * .46 and g["bbox"][2] > width * .54)], key=lambda g: g["bbox"][1])
    ordinary = [g for g in body if g not in anchors]
    result = sorted([g for g in margins if g["role"] == "header"], key=lambda g: (g["bbox"][1], g["bbox"][0]))
    remaining = list(ordinary)
    for anchor in anchors:
        before = [g for g in remaining if (g["bbox"][1] + g["bbox"][3]) / 2 < anchor["bbox"][1]]
        result.extend(sorted(before, key=lambda g: (0 if g["bbox"][0] < width / 2 else 1, g["bbox"][1], g["bbox"][0])))
        remaining = [g for g in remaining if g not in before]
        result.append(anchor)
    result.extend(sorted(remaining, key=lambda g: (0 if g["bbox"][0] < width / 2 else 1, g["bbox"][1], g["bbox"][0])))
    result.extend(sorted([g for g in margins if g["role"] == "footer"], key=lambda g: (g["bbox"][1], g["bbox"][0])))
    return result


def extract_page(page: Any, number: int, repeated: set[tuple[str, str]], references_before: bool,
                 geometry: dict[str, Any]) -> dict[str, Any]:
    width, height = float(page.width), float(page.height)
    warnings = list(geometry.get("warnings", []))
    all_chars = [dict(c, _index=i) for i, c in enumerate(page.chars)
                 if overlap(box(c), [0, 0, width, height])]
    regions, region_warnings = detect_regions(page)
    warnings.extend(region_warnings)
    if not all_chars or sum(bool(c.get("text", "").strip()) for c in all_chars) < 20:
        warnings.append("No usable selectable text layer (or very sparse text): OCR/transcription and source comparison are required. This page has not been translated.")
    if any(not c.get("upright", True) for c in all_chars):
        warnings.append("Rotated text remains within the page content. Check labels and reading direction; page rotation was normalized but individual rotated text cannot be ordered reliably.")
    if any("(cid:" in str(c.get("text", "")) or "\ufffd" in str(c.get("text", "")) for c in all_chars):
        warnings.append("Unmapped PDF glyph/CID or replacement character detected. Resolve against source rendering before translation; original extraction retained in text.")
    assigned: set[int] = set()
    groups = []
    for region in regions:
        chars = [c for c in all_chars if c["_index"] not in assigned and contains_char(region["bbox"], c)]
        assigned.update(c["_index"] for c in chars)
        # Text inside preserved regions is an audit aid only.
        region_text = "\n".join(v["text"] for v in make_lines(chars, width))
        groups.append({"role": region["role"], "action": "preserve", "bbox": region["bbox"], "text": region_text,
                       "chars": chars, "origin": region["origin"], "lane": "full" if region["bbox"][0] < width * .46 and region["bbox"][2] > width * .54 else ("left" if region["bbox"][0] < width / 2 else "right")})
    remaining = [c for c in all_chars if c["_index"] not in assigned]
    groups.extend(group_lines(make_lines(remaining, width), number, width, height, repeated))
    ordered = reading_order(groups, width)
    # Reference state switches at the precise position of its heading, never at page top.
    in_references = references_before
    for group in ordered:
        if re.fullmatch(r"REFERENCES|BIBLIOGRAPHY|LITERATURE CITED", group["text"].strip(), re.I):
            in_references = True
        if in_references and group["role"] not in ("header", "footer"):
            group["role"], group["action"] = "references", "preserve"
    # Preserve reference columns as complete regions, avoiding fragmented accents,
    # hanging indents, line breaks and link hotspots from tiny reference slices.
    collapsed = []
    for group in ordered:
        if collapsed and group["role"] in ("references", "metadata") and collapsed[-1]["role"] == group["role"] and group.get("lane") == collapsed[-1].get("lane") and (group["role"] == "references" or -2 <= group["bbox"][1] - collapsed[-1]["bbox"][3] <= 7):
            prior = collapsed[-1]
            prior["bbox"] = union([prior["bbox"], group["bbox"]])
            prior["text"] += "\n" + group["text"]
            prior["chars"].extend(group["chars"])
        else:
            collapsed.append(group)
    ordered = collapsed
    blocks = []
    coverage = {"translate": 0, "preserve": 0, "omit": 0}
    seen: set[int] = set()
    duplicates: set[int] = set()
    for index, group in enumerate(ordered, 1):
        ids = {c["_index"] for c in group["chars"]}
        duplicates.update(seen.intersection(ids))
        seen.update(ids)
        coverage[group["action"]] += len(ids)
        block = {"id": f"p{number:04d}-b{index:04d}", "role": group["role"], "action": group["action"],
                 "bbox": [round(v, 4) for v in clip(group["bbox"], width, height)], "text": group["text"], "translation": None}
        if "reason" in group:
            block["reason"] = group["reason"]
        if "origin" in group:
            block["origin"] = group["origin"]
        block["source_char_count"] = len(ids)
        blocks.append(block)
    unassigned = len(all_chars) - len(seen)
    if unassigned or duplicates:
        warnings.append(f"Character coverage error: {unassigned} unassigned, {len(duplicates)} assigned more than once. Resolve region assignments before review.")
    if not blocks:
        blocks.append({"id": f"p{number:04d}-b0001", "role": "figure", "action": "preserve", "bbox": [0, 0, width, height], "text": "", "translation": None, "origin": "empty-or-scan-page"})
    return {"number": number, "width": width, "height": height, "reviewed": False, "blocks": blocks,
            "warnings": sorted(set(warnings)),
            "extraction_audit": {"visible_source_chars": len(all_chars), "assigned_chars": len(seen), "unassigned_chars": unassigned,
                                 "multiply_assigned_chars": len(duplicates), "by_action": coverage}}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", help="Read-only source PDF")
    parser.add_argument("--work-dir", required=True, help="New or existing local working directory")
    parser.add_argument("--pages", help="Selected one-based source pages, e.g. 1,3,5-7; default is the complete paper")
    args = parser.parse_args(argv)
    source = Path(args.source).expanduser().resolve()
    work = Path(args.work_dir).expanduser().resolve()
    try:
        if not source.is_file():
            raise ValueError(f"Source PDF does not exist: {source}")
        if source.suffix.lower() != ".pdf":
            raise ValueError("Source must be a PDF file.")
        if work == source or source in work.parents:
            raise ValueError("Work directory must not be the source PDF path.")
        # Do not destroy a previous extraction or a reviewed/translated manifest.
        if (work / "manifest.json").exists():
            raise ValueError(f"{work / 'manifest.json'} already exists. Choose a new work directory to preserve prior reviews/translations.")
        source_hash = sha256(source)
        reader, geometry = normalized_reader(source)
        selected = parse_pages(args.pages, len(reader.pages))
        normalized = reader_bytes(reader)
        work.mkdir(parents=True, exist_ok=True)
        preview_dir = work / "previews"
        preview_dir.mkdir(exist_ok=True)
        manifest = {"schema_version": 1,
                    "source": {"path": str(source), "sha256": source_hash, "page_count": len(reader.pages)},
                    "glossary": [], "review": {"translation_checked": False, "visual_checked": False, "notes": []},
                    "extraction": {"selected_pages": selected, "complete_paper": selected == list(range(1, len(reader.pages) + 1)),
                                   "coordinate_system": "visible CropBox normalized to rotation=0; top-left PDF points",
                                   "limitations": LIMITATIONS, "geometry": geometry}, "pages": []}
        text_parts = []
        with pdfplumber.open(io.BytesIO(normalized)) as document:
            repeated = repeated_margin_keys(document)
            # This detects reference continuation even for selected later pages.
            references_started = False
            references_before: dict[int, bool] = {}
            for number, page in enumerate(document.pages, 1):
                references_before[number] = references_started
                raw_lines = make_lines([dict(c, _index=i) for i, c in enumerate(page.chars)], page.width)
                if any(re.fullmatch(r"REFERENCES|BIBLIOGRAPHY|LITERATURE CITED", v["text"].strip(), re.I) for v in raw_lines):
                    references_started = True
            for number in selected:
                record = extract_page(document.pages[number - 1], number, repeated, references_before[number], geometry[number - 1])
                manifest["pages"].append(record)
                text_parts.append(f"===== SOURCE PAGE {number} (proposed reading order, unreviewed) =====\n")
                for block in record["blocks"]:
                    text_parts.append(f"\n[{block['id']} role={block['role']} action={block['action']} bbox={block['bbox']}]\n{block['text']}\n")
        render_doc = pdfium.PdfDocument(normalized)
        try:
            for number in selected:
                page = render_doc[number - 1]
                try:
                    bitmap = page.render(scale=1.6)
                    try:
                        bitmap.to_pil().save(preview_dir / f"page-{number:04d}.png")
                    finally:
                        bitmap.close()
                finally:
                    page.close()
        finally:
            render_doc.close()
        if sha256(source) != source_hash:
            raise RuntimeError("Source PDF changed during extraction. Discard this extraction and investigate before continuing.")
        (work / "source.txt").write_text("\n".join(text_parts), encoding="utf-8")
        (work / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        print(json.dumps({"manifest": str(work / "manifest.json"), "selected_pages": selected,
                          "page_count": len(reader.pages), "complete_paper": manifest["extraction"]["complete_paper"],
                          "source_unchanged": True, "reviewed": False,
                          "warnings": {str(p["number"]): p["warnings"] for p in manifest["pages"] if p["warnings"]}}, ensure_ascii=False))
        return 0
    except (ValueError, OSError, RuntimeError, PdfReadError) as exc:
        parser.exit(2, f"Extraction stopped: {exc}\n")


if __name__ == "__main__":
    raise SystemExit(main())
