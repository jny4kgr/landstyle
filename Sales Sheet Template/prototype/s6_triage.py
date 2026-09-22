#!/usr/bin/env python3
"""設計図書 PDF から、間取り図に使えるベクター平面図を選ぶ。"""
from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path

import pymupdf

sys.path.insert(0, str(Path(__file__).resolve().parent))
import platform_paths  # noqa: F401  Windows のコンソールで文字が書けずに止まるのを防ぐ

ROOM_WORDS = ("LDK", "洋室", "玄関", "浴室", "UB", "トイレ", "納戸", "洗面", "収納", "物入", "クローゼット", "バルコニー")


def compact(text: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", text))


def page_stats(path: Path, page_no: int, page) -> dict:
    text = compact(page.get_text())
    drawings = page.get_drawings()
    lines = sum(len(d.get("items", ())) for d in drawings)
    colored = sum(len(d.get("items", ())) for d in drawings
                  if d.get("color") is not None and max(d["color"]) > .15)
    page_area = max(1.0, page.rect.width * page.rect.height)
    image_area = 0.0
    for block in page.get_text("dict").get("blocks", []):
        if block.get("type") == 1:
            x0, y0, x1, y1 = block["bbox"]
            image_area += max(0, x1-x0) * max(0, y1-y0)
    cover = min(1.0, image_area / page_area)
    chars = len(text)
    if cover >= .55 and chars < 80 and lines < 150:
        form = "scan"
    elif cover >= .35 and (lines >= 150 or chars >= 80):
        form = "mixed"
    elif lines >= 80 and chars >= 8:
        form = "vector"
    elif cover >= .35:
        form = "scan"
    else:
        form = "vector" if lines >= 80 else "mixed"

    room_hits = sum(text.count(word) for word in ROOM_WORDS)
    floor_word = "平面" in text
    elev_word = any(w in text for w in ("立面図", "立面詳細", "東立面", "西立面", "南立面", "北立面"))
    spec_word = any(w in text for w in ("仕様書", "仕上表", "特記仕様", "仕様概要")) or text.count("仕様") >= 3
    floor_title = bool(re.search(r"[123一二三]階.{0,20}(?:平面|クロス貼り分け|電気図|平面詳細)", text))
    if elev_word and room_hits < 2:
        kind = "elevation"
    elif spec_word and not floor_title:
        kind = "spec"
    elif (floor_title or floor_word or room_hits >= 3) and lines >= 80:
        kind = "floorplan"
    elif chars > 250 and lines < 250:
        kind = "spec"
    else:
        kind = "other"

    floor = ""
    # 図枠タイトルを先に見る。設備注記の「1階へ」「2FL」に引かれないようにする。
    patterns = (("1F", r"(?:1|一)階.{0,20}(?:平面|クロス貼り分け|電気図|平面詳細)"),
                ("2F", r"(?:2|二)階.{0,20}(?:平面|クロス貼り分け|電気図|平面詳細)"),
                ("3F", r"(?:3|三)階.{0,20}(?:平面|クロス貼り分け|電気図|平面詳細)"))
    for label, pat in patterns:
        if re.search(pat, text, re.I):
            floor = label
            break
    reasons = [f"lines={lines}", f"rooms={room_hits}"]
    if cover >= .2: reasons.append(f"image={cover:.0%}")
    if floor_word: reasons.append("平面")
    if elev_word: reasons.append("立面")
    return {"file": path.name, "page": page_no, "format": form, "kind": kind,
            "floor": floor, "reason": ",".join(reasons), "recommended": "",
            "_room_hits": room_hits, "_colored": colored, "_lines": lines}


def pdfs(inputs: list[str]) -> list[Path]:
    result = []
    for value in inputs:
        p = Path(value).expanduser()
        if p.is_dir(): result.extend(sorted(p.glob("*.pdf")))
        elif p.suffix.lower() == ".pdf": result.append(p)
        else: print(f"warning: not a PDF or directory: {p}", file=sys.stderr)
    return result


def inspect(inputs: list[str]) -> list[dict]:
    rows = []
    for path in pdfs(inputs):
        try:
            with pymupdf.open(path) as doc:
                rows.extend(page_stats(path, i, page) for i, page in enumerate(doc, 1))
        except Exception as exc:
            print(f"warning: cannot read {path}: {exc}", file=sys.stderr)
    for floor in ("1F", "2F", "3F"):
        candidates = [r for r in rows if r["floor"] == floor and r["kind"] == "floorplan" and r["format"] != "scan"]
        if candidates:
            # 赤・青などの追記が少ないことを優先し、その中で室名が多いページを選ぶ。
            best = max(candidates, key=lambda r: (-r["_colored"], r["_room_hits"], r["_lines"]))
            best["recommended"] = "use"
    for row in rows:
        for key in tuple(row):
            if key.startswith("_"): del row[key]
    return rows


def table(rows: list[dict]) -> None:
    headers = ("ファイル名", "ページ", "形式", "種類", "階", "根拠", "推奨")
    keys = ("file", "page", "format", "kind", "floor", "reason", "recommended")
    data = [[str(r[k]) for k in keys] for r in rows]
    widths = [max(len(headers[i]), *(len(row[i]) for row in data)) for i in range(len(keys))]
    print("  ".join(headers[i].ljust(widths[i]) for i in range(len(keys))))
    print("  ".join("-" * w for w in widths))
    for row in data: print("  ".join(row[i].ljust(widths[i]) for i in range(len(keys))))


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("inputs", nargs="+", help="PDF または PDF を含むディレクトリ")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    rows = inspect(args.inputs)
    if args.json: print(json.dumps(rows, ensure_ascii=False, indent=2))
    else: table(rows)


if __name__ == "__main__": main()
