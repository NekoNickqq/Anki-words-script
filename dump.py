#!/usr/bin/env python3
"""
dump_page.py — скачивает HTML-страницу Reverso для указанного слова,
чтобы потом можно было изучить её структуру и починить парсер.

Использование:
    python dump_page.py terrifying
    python dump_page.py "take over"
    python dump_page.py terrifying --out mypage.html
"""

import argparse
import sys
from urllib.parse import quote

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
        r = s.get(url, timeout=60)
        return r
    except Exception as e:
        print(f"[cloudscraper] ошибка: {type(e).__name__}: {e}")
        return None


def try_requests(url):
    import requests
    try:
        r = requests.get(url, headers=HEADERS, timeout=60)
        return r
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
    ap.add_argument("word", help="слово или фраза на английском")
    ap.add_argument("--out", default="page.html", help="куда сохранить (по умолчанию page.html)")
    args = ap.parse_args()

    url = f"{BASE}/translation/english-russian/{quote(args.word.strip())}"
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
        print("\n✘ Не удалось получить страницу. Возможные причины:")
        print("  - блокировка Cloudflare (попробуй pip install cloudscraper)")
        print("  - сеть/провайдер/VPN")
        print("  - неверное слово")
        return 1

    with open(args.out, "w", encoding="utf-8") as fh:
        fh.write(resp.text)

    print(f"\n✔ Сохранено в {args.out} ({len(resp.text)} символов)")
   
    return 0


if __name__ == "__main__":
    sys.exit(main())