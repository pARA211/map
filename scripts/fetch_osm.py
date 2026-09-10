#!/usr/bin/env python3
"""
Скачивает объекты инфраструктуры Москвы (детские сады, школы, медицину,
бизнес-центры) из OpenStreetMap через Overpass API и сохраняет их
локально в JSON — без запросов к Overpass из браузера.

Использование:
    python scripts/fetch_osm.py

Результат сохраняется рядом со скриптом, в ../data/:
    data/kindergartens.json
    data/schools.json
    data/medical.json
    data/business_centers.json
"""

import json
import sys
import time
import urllib.error

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

import urllib.request
from pathlib import Path

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
OVERPASS_TIMEOUT_S = 180          # таймаут внутри Overpass QL
HTTP_TIMEOUT_S = 200               # таймаут HTTP-запроса
RETRIES = 3
RETRY_DELAY_S = 10
PAUSE_BETWEEN_CATEGORIES_S = 2     # вежливая пауза, чтобы не долбить публичный сервер

# Москва в старых границах (в пределах и чуть за МКАД) — здесь расположены
# все ЖК из проекта. New Moscow (ТиНАО) сюда не входит, при необходимости
# расширь bbox.
BBOX = (55.49, 37.15, 56.02, 37.97)  # south, west, north, east

SCRIPT_DIR = Path(__file__).resolve().parent
DATA_DIR = SCRIPT_DIR.parent / "data"

CATEGORIES = {
    "kindergartens": {
        "label": "Детский сад",
        "filters": [
            ('node', 'amenity', 'kindergarten'),
            ('way', 'amenity', 'kindergarten'),
            ('relation', 'amenity', 'kindergarten'),
        ],
    },
    "schools": {
        "label": "Школа",
        "filters": [
            ('node', 'amenity', 'school'),
            ('way', 'amenity', 'school'),
            ('relation', 'amenity', 'school'),
        ],
    },
    "medical": {
        "label": "Медицина",
        "filters": [
            ('node', 'amenity', 'hospital'),
            ('way', 'amenity', 'hospital'),
            ('relation', 'amenity', 'hospital'),
            ('node', 'amenity', 'clinic'),
            ('way', 'amenity', 'clinic'),
            ('relation', 'amenity', 'clinic'),
            ('node', 'amenity', 'doctors'),
            ('way', 'amenity', 'doctors'),
        ],
    },
    "business_centers": {
        "label": "Бизнес-центр",
        # у OSM нет единого тега "бизнес-центр": ближе всего офисные здания
        # (building=office) и отдельные объекты office=yes
        "filters": [
            ('way', 'building', 'office'),
            ('relation', 'building', 'office'),
            ('node', 'office', 'yes'),
            ('way', 'office', 'yes'),
        ],
    },
}


def build_query(filters) -> str:
    bbox_str = ",".join(str(x) for x in BBOX)
    lines = [f'  {kind}["{key}"="{value}"]({bbox_str});' for kind, key, value in filters]
    body = "\n".join(lines)
    return f"[out:json][timeout:{OVERPASS_TIMEOUT_S}];\n(\n{body}\n);\nout center tags;"


def fetch_overpass(query: str) -> dict:
    payload = query.encode("utf-8")
    req = urllib.request.Request(
        OVERPASS_URL,
        data=payload,
        headers={
            "Content-Type": "text/plain; charset=utf-8",
            "User-Agent": "zhk-moskvy-naizust-map/1.0 (contact: make.com481@gmail.com)",
            "Accept": "application/json",
        },
        method="POST",
    )
    last_error = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_S) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
            last_error = e
            print(f"  попытка {attempt}/{RETRIES} не удалась: {e}", file=sys.stderr)
            if attempt < RETRIES:
                time.sleep(RETRY_DELAY_S)
    raise RuntimeError(f"Overpass запрос не удался после {RETRIES} попыток: {last_error}")


def element_latlon(el: dict):
    if el.get("type") == "node":
        return el.get("lat"), el.get("lon")
    center = el.get("center")
    if center:
        return center.get("lat"), center.get("lon")
    return None, None


def normalize(elements, label: str):
    items = []
    seen = set()
    for el in elements:
        key = f"{el.get('type')}/{el.get('id')}"
        if key in seen:
            continue
        seen.add(key)
        lat, lon = element_latlon(el)
        if lat is None or lon is None:
            continue
        tags = el.get("tags", {}) or {}
        name = tags.get("name") or tags.get("name:ru") or label
        items.append({
            "id": key,
            "type": el.get("type"),
            "lat": lat,
            "lon": lon,
            "name": name,
            "tags": tags,
        })
    return items


def fetch_category(key: str, cfg: dict):
    print(f"Загружаю «{cfg['label']}» ({key})...")
    query = build_query(cfg["filters"])
    data = fetch_overpass(query)
    items = normalize(data.get("elements", []), cfg["label"])
    print(f"  получено объектов: {len(items)}")
    return items


def save_json(key: str, items):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"{key}.json"
    with out_path.open("w", encoding="utf-8") as f:
        json.dump(items, f, ensure_ascii=False, indent=2)
    print(f"  сохранено: {out_path.relative_to(SCRIPT_DIR.parent)}")


def main():
    total = 0
    for i, (key, cfg) in enumerate(CATEGORIES.items()):
        try:
            items = fetch_category(key, cfg)
        except RuntimeError as e:
            print(f"ОШИБКА при загрузке «{key}»: {e}", file=sys.stderr)
            continue
        save_json(key, items)
        total += len(items)
        if i < len(CATEGORIES) - 1:
            time.sleep(PAUSE_BETWEEN_CATEGORIES_S)
    print(f"\nГотово. Всего объектов во всех категориях: {total}")


if __name__ == "__main__":
    main()
