#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
add_photo.py — добавить фотографию позиции сметы.

Уменьшает исходную картинку до 180 × 180 точек (целиком, на белом поле),
кладёт её в docs/assets/estimate/ и записывает источник в
docs/estimate/photo_sources.json.

Нужен Pillow:  python3 -m venv .venv && .venv/bin/pip install Pillow

Примеры:
    add_photo.py gy87.jpg ~/Downloads/gy87.png --url https://example.org/gy87 --title "Модуль GY-87"
    add_photo.py nema23.jpg docs/assets/images/nema23_motor.jpg --repo docs/assets/images/nema23_motor.jpg
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from PIL import Image

ROOT = Path(__file__).resolve().parents[2]
PHOTO_DIR = ROOT / "docs" / "assets" / "estimate"
PHOTO_JSON = ROOT / "docs" / "estimate" / "photo_sources.json"
SIZE = 180


def make_thumbnail(source: Path, target: Path) -> None:
    with Image.open(source) as image:
        image = image.convert("RGBA")
        image.thumbnail((SIZE, SIZE), Image.LANCZOS)
        canvas = Image.new("RGB", (SIZE, SIZE), "white")
        offset = ((SIZE - image.width) // 2, (SIZE - image.height) // 2)
        canvas.paste(image, offset, mask=image.split()[3])
    target.parent.mkdir(parents=True, exist_ok=True)
    canvas.save(target, "JPEG", quality=86, optimize=True)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Добавить фотографию позиции сметы.")
    parser.add_argument("name", help="имя файла в docs/assets/estimate/, например gy87.jpg")
    parser.add_argument("source", help="исходная картинка")
    parser.add_argument("--url", help="страница, с которой взята картинка")
    parser.add_argument("--title", help="подпись источника")
    parser.add_argument("--repo", help="путь к файлу репозитория, если картинка взята из него")
    args = parser.parse_args(argv)

    if not args.name.endswith(".jpg"):
        parser.error("имя файла должно оканчиваться на .jpg")
    if bool(args.url) == bool(args.repo):
        parser.error("укажите либо --url, либо --repo")
    if args.repo and not (ROOT / args.repo).is_file():
        parser.error(f"в репозитории нет файла {args.repo}")

    make_thumbnail(Path(args.source).expanduser(), PHOTO_DIR / args.name)

    sources = json.loads(PHOTO_JSON.read_text(encoding="utf-8")) if PHOTO_JSON.exists() else {}
    if args.repo:
        sources[args.name] = {"repo": args.repo}
    else:
        sources[args.name] = {"url": args.url, "title": args.title or args.url}
    PHOTO_JSON.write_text(
        json.dumps(dict(sorted(sources.items())), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"{args.name}: {SIZE}×{SIZE}, источник записан")
    return 0


if __name__ == "__main__":
    sys.exit(main())
