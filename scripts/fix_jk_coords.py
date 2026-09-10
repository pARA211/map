#!/usr/bin/env python3
"""
Уточняет координаты ЖК в index.html по данным OpenStreetMap (Overpass API).

Для каждого ЖК ищет в OSM полигон/точку с совпадающим name/alt_name/full_name
рядом с текущей (уже записанной) координатой, и если находит подходящий
объект — берёт его центр вместо текущей приблизительной точки.

Не трогает объекты, для которых в OSM ничего похожего не нашлось рядом —
их координаты остаются как были.

Использование:
    python scripts/fix_jk_coords.py            # только отчёт, ничего не меняет
    python scripts/fix_jk_coords.py --apply     # применяет найденные исправления к index.html
"""

import io
import json
import math
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")

OVERPASS_URL = "https://overpass-api.de/api/interpreter"
BBOX = (55.49, 37.15, 56.02, 37.97)  # south, west, north, east
HTTP_TIMEOUT_S = 90
RETRIES = 3
RETRY_DELAY_S = 8
MAX_MATCH_DISTANCE_KM = 6  # отбрасываем совпадения по имени, найденные слишком далеко

SCRIPT_DIR = Path(__file__).resolve().parent
INDEX_HTML = SCRIPT_DIR.parent / "index.html"
REPORT_PATH = SCRIPT_DIR.parent / "jk_coords_report.json"


def haversine_km(lat1, lon1, lat2, lon2):
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def overpass_escape(name: str) -> str:
    return re.sub(r'([.^$*+?()\[\]{}|\\"])', r"\\\1", name)


def build_query(name: str) -> str:
    n = overpass_escape(name)
    bbox_str = ",".join(str(x) for x in BBOX)
    return f"""
[out:json][timeout:60];
(
  way["landuse"="residential"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  relation["landuse"="residential"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  way["landuse"="construction"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  relation["landuse"="construction"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  way["building"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  relation["building"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  node["place"="neighbourhood"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  way["place"="neighbourhood"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  relation["place"="neighbourhood"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
  node["office"="estate_agent"][~"^(name|alt_name|full_name)$"~"{n}",i]({bbox_str});
);
out center tags;
"""


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
            print(f"    попытка {attempt}/{RETRIES} не удалась: {e}", file=sys.stderr)
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


PRIORITY = [
    lambda t: t.get("landuse") == "residential" and t.get("place") == "neighbourhood",
    lambda t: t.get("landuse") == "residential",
    lambda t: t.get("landuse") == "construction",
    lambda t: t.get("building") in ("apartments", "residential", "house", "yes"),
    lambda t: t.get("place") == "neighbourhood",
    lambda t: t.get("office") == "estate_agent",
]


def score(tags: dict) -> int:
    for i, pred in enumerate(PRIORITY):
        if pred(tags):
            return i
    return len(PRIORITY)


def name_matches(name: str, tags: dict) -> bool:
    name_l = name.lower()
    for key in ("name", "alt_name", "full_name"):
        v = tags.get(key)
        if v and name_l in v.lower():
            return True
    return False


def pick_best(name: str, elements: list, ref_lat: float, ref_lon: float):
    candidates = []
    for el in elements:
        lat, lon = element_latlon(el)
        if lat is None or lon is None:
            continue
        tags = el.get("tags", {}) or {}
        if not name_matches(name, tags):
            continue
        dist = haversine_km(ref_lat, ref_lon, lat, lon)
        if dist > MAX_MATCH_DISTANCE_KM:
            continue
        candidates.append({
            "type": el.get("type"),
            "id": el.get("id"),
            "lat": lat,
            "lon": lon,
            "tags": tags,
            "dist_km": round(dist, 3),
            "score": score(tags),
        })
    if not candidates:
        return None, []
    candidates.sort(key=lambda c: (c["score"], c["dist_km"]))
    return candidates[0], candidates


def load_jk_and_coords():
    text = INDEX_HTML.read_text(encoding="utf-8")
    jk_match = re.search(r"^const JK = (.+?);\s*$", text, re.M)
    coords_match = re.search(r"^const BAKED_COORDS = (\{.*?\});", text, re.M)
    if not jk_match or not coords_match:
        raise RuntimeError("Не удалось найти JK / BAKED_COORDS в index.html")
    jk = json.loads(jk_match.group(1))
    coords = json.loads(coords_match.group(1))
    return text, jk, coords, coords_match


def main():
    apply_changes = "--apply" in sys.argv

    text, jk_list, coords, coords_match = load_jk_and_coords()

    report = []
    updated_coords = dict(coords)

    for i, jk in enumerate(jk_list):
        name = jk["name"]
        ref = coords.get(name)
        print(f"[{i+1}/{len(jk_list)}] {name}...")
        if not ref:
            print("    нет текущих координат, пропуск")
            report.append({"name": name, "status": "no_ref_coords"})
            continue
        ref_lat, ref_lon = ref
        try:
            data = fetch_overpass(build_query(name))
        except RuntimeError as e:
            print(f"    ОШИБКА: {e}")
            report.append({"name": name, "status": "fetch_error", "error": str(e)})
            continue

        best, candidates = pick_best(name, data.get("elements", []), ref_lat, ref_lon)
        if not best:
            print(f"    совпадений в OSM рядом с текущей точкой не найдено (координаты не тронуты)")
            report.append({
                "name": name,
                "status": "no_match",
                "old_coord": ref,
            })
        else:
            old_dist = 0.0
            new_lat, new_lon = best["lat"], best["lon"]
            moved_km = haversine_km(ref_lat, ref_lon, new_lat, new_lon)
            print(f"    найдено: {best['type']}/{best['id']} "
                  f"({', '.join(k+'='+v for k,v in best['tags'].items() if k in ('name','landuse','place','building','office'))}) "
                  f"— сдвиг {moved_km*1000:.0f} м")
            updated_coords[name] = [new_lat, new_lon]
            report.append({
                "name": name,
                "status": "matched",
                "old_coord": ref,
                "new_coord": [new_lat, new_lon],
                "moved_m": round(moved_km * 1000, 1),
                "match": {k: best[k] for k in ("type", "id", "tags", "score", "dist_km")},
                "all_candidates": candidates,
            })
        time.sleep(1.2)

    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\nОтчёт сохранён: {REPORT_PATH.relative_to(SCRIPT_DIR.parent)}")

    matched = [r for r in report if r["status"] == "matched"]
    print(f"\nНайдены/уточнены координаты: {len(matched)} из {len(jk_list)}")

    if apply_changes and matched:
        lines = []
        for jk in jk_list:
            name = jk["name"]
            c = updated_coords.get(name)
            if not c:
                continue
            lines.append(f'"{name}": [{c[0]}, {c[1]}]')
        new_coords_obj = "{" + ", ".join(lines) + "}"
        new_line = f"const BAKED_COORDS = {new_coords_obj}; // координаты {len(lines)} ЖК — уточнены по OSM (scripts/fix_jk_coords.py)"
        new_text = text[:coords_match.start()] + new_line + text[coords_match.end():]
        INDEX_HTML.write_text(new_text, encoding="utf-8")
        print(f"index.html обновлён: применено {len(matched)} исправлений координат")
    elif apply_changes:
        print("Нечего применять — совпадений не найдено")
    else:
        print("Это предпросмотр (--apply не указан) — index.html не изменён")


if __name__ == "__main__":
    main()
