#!/usr/bin/env python3
"""
dump.py — скачивает HTML-страницу (слово или произвольный URL) и сохраняет её в папку dumps/.

Использование:
    python dump.py terrifying
    python dump.py "https://context.reverso.net/translation/english-russian/run"
    python dump.py terrifying --out mypage.html
"""
import argparse
import sys
from pathlib import Path
from urllib.parse import quote
import re

BASE = "https://context.reverso.net"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

def try_cloudscraper(url):
    try:
        import cloudscraper
    except ImportError:
        print("[cloudscraper] не установлен — пропускаю")
        return None
    try:
        s = cloudscraper.create_scraper()
        s.headers.update(HEADERS)
        return s.get(url, timeout=60)
    except Exception as e:
        print(f"[cloudscraper] ошибка: {type(e).__name__}: {e}")
        return None

def try_requests(url):
    import requests
    try:
        return requests.get(url, headers=HEADERS, timeout=60)
    except Exception as e:
        print(f"[requests] ошибка: {type(e).__name__}: {e}")
        return None

def looks_blocked(text):
    head = text[:2000]
    return (
        "Just a moment" in head
        or "cf-browser-verification" in head
        or "Attention Required! | Cloudflare" in head
        or "Enable JavaScript and cookies to continue" in head
    )

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("target", help="Слово или полная ссылка (URL)")
    ap.add_argument("--out", help="Имя файла (по умолчанию генерируется из URL)")
    args = ap.parse_args()

    # Определяем, что ввел пользователь
    if args.target.startswith("http"):
        url = args.target
    else:
        url = f"{BASE}/translation/english-russian/{quote(args.target.strip())}"

    print(f"URL: {url}\n")

    resp = None
    for name, fn in (("cloudscraper", try_cloudscraper), ("requests", try_requests)):
        print(f"Пробую {name}...")
        resp = fn(url)
        if resp is None:
            continue
        print(f"  HTTP {resp.status_code} | длина {len(resp.text)} симв.")
        if resp.status_code == 200 and not looks_blocked(resp.text):
            print(f"  ✔ страница похожа на нормальную — использую ответ {name}")
            break
        if resp.status_code == 200 and looks_blocked(resp.text):
            print(f"  ⚠ ответ {name} похож на Cloudflare-заглушку")
        resp = None

    if resp is None:
        print("\n✘ Не удалось получить страницу.")
        return 1

    # Создаем папку dumps, если её нет
    dumps_dir = Path(__file__).resolve().parent / "dumps"
    dumps_dir.mkdir(exist_ok=True)

    # Генерируем имя файла
    if args.out:
        filename = args.out
    else:
        # Берем последнюю часть URL и делаем её безопасной для файловой системы
        safe_name = re.sub(r'[^\w\-]', '_', url.split("/")[-1] or "index")
        filename = f"{safe_name}.html"

    out_path = dumps_dir / filename

    with open(out_path, "w", encoding="utf-8") as fh:
        fh.write(resp.text)

    print(f"\n✔ Сохранено в {out_path.relative_to(Path.cwd())} ({len(resp.text)} символов)")
    return 0

if __name__ == "__main__":
    sys.exit(main())