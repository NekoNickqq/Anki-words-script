#!/usr/bin/env python3
"""
reverso_anki.py — простой скрипт: английский -> переводы с context.reverso.net -> карточка Anki через AnkiConnect.

Что делает:
  1. Скачивает страницу https://context.reverso.net/translation/english-russian/<слово>.
  2. Извлекает ВСЕ значения перевода (условно их может быть 7 и больше).
  3. Для каждого значения ищет примеры использования на том же сайте
     (страница примеров по ключевому слову перевода) и берёт пару EN/RU.
  4. Собирает HTML-карточку и отправляет её в Anki через AnkiConnect (http://localhost:8765).

Нейросеть (подключение planned):
  В скрипте уже заложено место для неё: функция `enrich_with_ai(word, entries)` вызывает
  `ai_summarize_translation()`. По умолчанию она выключена (флаг --ai) и работает как
  "заглушка", если не задан API-ключ OPENAI_API_KEY. При включении к каждому значению
  добавляется краткая пояснение от модели о оттенке смысла / контексте употребления.

Зависимости:
  pip install requests beautifulsoup4 ankiconnect
  (опционально, для обхода Cloudflare: pip install cloudscraper)

Использование:
  python reverso_anki.py run                      # одно слово
  python reverso_anki.py run house --examples 3   # по 3 примера на значение
  python reverso_anki.py --file words.txt         # список слов, по одному в строке
  python reverso_anki.py run --dry-run            # показать карточку, не отправляя в Anki
  python reverso_anki.py run --ai                 # добавить пояснения нейросети (нужен ключ)
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from typing import Optional

import requests
from bs4 import BeautifulSoup

try:  # cloudscraper опционален — помогает обойти Cloudflare-защиту reverso
    import cloudscraper  # type: ignore
except ImportError:
    cloudscraper = None

try:
    from ankiconnect import AnkiConnectClient  # необязательная библиотека-клиент
except ImportError:
    AnkiConnectClient = None


BASE_URL = "https://context.reverso.net"
LANG_PAIR = "english-russian"          # направление перевода на сайте
REQUEST_DELAY = 1.0                     # вежливая пауза между запросами, сек
MAX_TRANSLATIONS = 10                   # берём все значения, но с разумным потолком

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}

_session: Optional[requests.Session] = None
_last_request_ts: float = 0.0


# ----------------------------------------------------------------------------
# Модель данных
# ----------------------------------------------------------------------------

@dataclass
class Example:
    en: str
    ru: str


@dataclass
class TranslationEntry:
    text: str                              # перевод на русский ("бегать", "пробегать"...)
    part_of_speech: str = ""               # глагол / существительное и т.п. (если удалось определить)
    examples: list[Example] = field(default_factory=list)
    ai_note: str = ""                      # сюда нейросеть позже добавит пояснение


@dataclass
class WordCard:
    word: str
    url: str
    entries: list[TranslationEntry]


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------

def get_session() -> requests.Session:
    global _session
    if _session is None:
        if cloudscraper is not None:
            _session = cloudscraper.create_scraper()
        else:
            _session = requests.Session()
        _session.headers.update(_HEADERS)
    return _session


def fetch(url: str) -> str:
    """GET с вежливой паузой. Бросает RuntimeError при защите Cloudflare/403."""
    global _last_request_ts
    wait = REQUEST_DELAY - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()

    resp = get_session().get(url, timeout=30)
    if resp.status_code == 403 or "Just a moment" in resp.text[:2000]:
        raise RuntimeError(
            f"Reverso закрыл доступ (Cloudflare) для {url}.\n"
            "Варианты решения:\n"
            "  1) pip install cloudscraper  (уже установлено? попробуйте)\n"
            "  2) открыть страницу в браузере и передать cookies через переменную окружения\n"
            "     REVERSO_COOKIES='cf_clearance=...; ...' (см. README)\n"
            "  3) использовать прокси/браузерный рендеринг (FlareSolverr, Playwright)."
        )
    resp.raise_for_status()
    return resp.text


def apply_cookies_from_env(session: requests.Session) -> None:
    raw = os.environ.get("REVERSO_COOKIES", "").strip()
    if not raw:
        return
    for part in raw.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            session.cookies.set(k.strip(), v.strip(), domain="context.reverso.net")


# ----------------------------------------------------------------------------
# Парсинг Reverso
# ----------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _is_noise(text: str) -> bool:
    low = text.lower()
    noise = (
        "advertisement", "see also", "synonyms", "add to list", "feedback",
        "more results", "suggest", "reverso", "copyright", "privacy", "terms",
    )
    return any(n in low for n in noise)


def parse_translations(page_html: str) -> list[TranslationEntry]:
    """Извлекает все английско->русские значения со страницы перевода."""
    soup = BeautifulSoup(page_html, "html.parser")
    entries: list[TranslationEntry] = []
    seen: set[str] = set()

    # Основные блоки translations-block contain ul.translation-list > li > a
    containers = soup.select("div.vertical-block section.section") or [soup]
    for container in containers:
        for li in container.select("ul.translation-list li, .translation-list li"):
            a = li.find("a")
            span_text = li.select_one(".text")
            node = a or span_text
            if node is None:
                continue
            text = _clean(node.get_text())
            if not text or _is_noise(text) or len(text) > 120:
                continue
            # часть речи обычно в <span class="pos">
            pos_node = li.select_one(".pos")
            pos = _clean(pos_node.get_text()) if pos_node else ""

            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(TranslationEntry(text=text, part_of_speech=pos))

        if entries:
            break

    # Запасной вариант: любые ссылки вида /translation/english-russian/<rus>
    if not entries:
        for a in soup.select(f'a[href*="/translation/{LANG_PAIR}/"]'):
            text = _clean(a.get_text())
            if not text or text.isascii():   # нас интересуют кириллические переводы
                continue
            if not re.search(r"[а-яА-ЯёЁ]", text):
                continue
            key = text.lower()
            if key in seen or _is_noise(key):
                continue
            seen.add(key)
            entries.append(TranslationEntry(text=text))

    return entries[:MAX_TRANSLATIONS]


def parse_examples(page_html: str, keyword_ru: str, limit: int) -> list[Example]:
    """Извлекает пары примеров EN/RU со страницы примеров Reverso."""
    soup = BeautifulSoup(page_html, "html.parser")
    examples: list[Example] = []

    # Структура сайта: div.example > a.source(EN) + a.target(RU), либо li.example
    pairs = []
    for ex in soup.select("div.example, li.example"):
        src = ex.select_one(".source")
        tgt = ex.select_one(".target")
        if src and tgt:
            pairs.append((_clean(src.get_text()), _clean(tgt.get_text())))

    for en, ru in pairs:
        if len(examples) >= limit:
            break
        if not en or not ru or _is_noise(en):
            continue
        # предпочитаем примеры, где встречается именно наш перевод
        if keyword_ru.lower() not in ru.lower():
            continue
        examples.append(Example(en=en, ru=ru))

    # если строгих совпадений нет — берём первые попавшиеся
    if not examples:
        for en, ru in pairs:
            if len(examples) >= limit:
                break
            if en and ru and not _is_noise(en):
                examples.append(Example(en=en, ru=ru))

    return examples


def examples_url(term: str) -> str:
    safe = term.replace(" ", "%20")
    return f"{BASE_URL}/examples/{LANG_PAIR}/{safe}"


def scrape_word(word: str, examples_per_entry: int) -> WordCard:
    word = word.strip()
    url = f"{BASE_URL}/{LANG_PAIR.replace('english-russian','translation/english-russian')}/{word.replace(' ', '%20')}"
    print(f"[1/2] Перевод слова: {url}")
    page = fetch(url)
    entries = parse_translations(page)
    if not entries:
        print("  ! Не удалось извлечь переводы (изменилась разметка или страница защищена).")
    print(f"  Найдено значений: {len(entries)}")

    if examples_per_entry > 0:
        for i, ent in enumerate(entries, 1):
            # ищем примеры по самому переводу (русское слово) — как на сайте
            eurl = examples_url(ent.text)
            try:
                print(f"[2/2] Примеры для «{ent.text}» ({i}/{len(entries)}): {eurl}")
                ent.examples = parse_examples(fetch(eurl), ent.text, examples_per_entry)
                print(f"      -> {len(ent.examples)} пример(ов)")
            except Exception as exc:  # не критично: карточку можно собрать и без примеров
                print(f"      ! Примеры не получены: {exc}")

    return WordCard(word=word, url=url, entries=entries)


# ----------------------------------------------------------------------------
# Нейросеть (заготовка)
# ----------------------------------------------------------------------------

def ai_summarize_translation(word: str, translation: str) -> str:
    """
    Точка подключения LLM. Сейчас: OpenAI-совместимый chat/completions, если задан
    OPENAI_API_KEY. Иначе возвращает пустую строку (режим заглушки).

    Позже здесь можно просить модель: выбрать лучший перевод, сгенерировать
    подсказку/контекст, переформулировать пример и т.д.
    """
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return ""
    try:
        resp = requests.post(
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": [
                    {"role": "system",
                     "content": "Ты помогаешь учить английский. Одной короткой фразой (до 20 слов) "
                                "на русском объясни оттенок значения перевода слова "
                                f"«{word}» = «{translation}», когда так говорят, а когда нет."},
                    {"role": "user", "content": f"{word} -> {translation}"},
                ],
                "max_tokens": 60,
                "temperature": 0.3,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"].strip()
    except Exception as exc:
        print(f"  ! AI-пояснение недоступно: {exc}")
        return ""


def enrich_with_ai(card: WordCard) -> None:
    for ent in card.entries:
        note = ai_summarize_translation(card.word, ent.text)
        if note:
            ent.ai_note = note
            time.sleep(0.3)  # не спамим API


# ----------------------------------------------------------------------------
# Сборка карточки и отправка в Anki
# ----------------------------------------------------------------------------

def build_card_html(card: WordCard) -> str:
    parts = [f"<h3>🇬🇧 <b>{html.escape(card.word)}</b> → 🇷🇺</h3>"]
    parts.append('<ol class="reverso-entries">')
    for ent in card.entries:
        pos = f' <i>({html.escape(ent.part_of_speech)})</i>' if ent.part_of_speech else ""
        row = f'<li><b>{html.escape(ent.text)}</b>{pos}'
        if ent.ai_note:
            row += f'<br/><span style="color:#777;font-size:90%">🤖 {html.escape(ent.ai_note)}</span>'
        for ex in ent.examples:
            row += (
                '<br/><span style="color:#1a6fb5">• ' + html.escape(ex.en) + "</span>"
                "<br/><span style=\"color:#2e7d32\">→ " + html.escape(ex.ru) + "</span>"
            )
        row += "</li>"
        parts.append(row)
    parts.append("</ol>")
    parts.append(
        f'<div style="font-size:80%;color:#999">Источник: '
        f'<a href="{card.url}">reverso.net</a></div>'
    )
    return "\n".join(parts)


ANKI_URL = os.environ.get("ANKICONNECT_URL", "http://localhost:8765")


def send_to_anki(card: WordCard, deck_name: str, tags: list[str]) -> dict:
    payload = {
        "action": "addNote",
        "version": 6,
        "params": {
            "note": {
                "deckName": deck_name,
                "modelName": "Basic",
                "fields": {
                    "Front": card.word,
                    "Back": build_card_html(card),
                },
                "options": {"allowDuplicate": False},
                "tags": ["reverso", *tags],
            }
        },
    }
    resp = requests.post(ANKI_URL, json=payload, timeout=30)
    resp.raise_for_status()
    result = resp.json()
    if result.get("error"):
        raise RuntimeError(f"AnkiConnect error: {result['error']}")
    return result


def check_anki_connect() -> bool:
    try:
        r = requests.post(ANKI_URL, json={"action": "version", "version": 6}, timeout=5)
        return r.ok and r.json().get("result") is not None
    except Exception:
        return False


# ----------------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------------

def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Reverso (EN->RU) -> Anki через AnkiConnect")
    ap.add_argument("words", nargs="*", help="слова на английском")
    ap.add_argument("--file", help="файл со словами (по одному в строке)")
    ap.add_argument("--deck", default="Reverso EN-RU", help="колода Anki")
    ap.add_argument("--examples", type=int, default=2,
                    help="примеров на каждое значение (0 — не искать примеры)")
    ap.add_argument("--tags", nargs="*", default=[], help="теги для карточек")
    ap.add_argument("--ai", action="store_true", help="добавить пояснения нейросети (OPENAI_API_KEY)")
    ap.add_argument("--dry-run", action="store_true", help="не отправлять в Anki, только показать HTML")
    args = ap.parse_args(argv)

    words = list(args.words)
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            words += [line.strip() for line in fh if line.strip() and not line.startswith("#")]
    if not words:
        ap.error("Укажите слова или файл через --file")

    session = get_session()
    apply_cookies_from_env(session)

    if not args.dry_run and not check_anki_connect():
        print("AnkiConnect не отвечает на " + ANKI_URL +
              ". Установите аддон AnkiConnect в Anki и повторите (или используйте --dry-run).")
        return 2

    ok = failed = 0
    for word in words:
        try:
            card = scrape_word(word, examples_per_entry=args.examples)
            if args.ai:
                print("[AI] Добавляю пояснения нейросети…")
                enrich_with_ai(card)
            if args.dry_run:
                print("=" * 60)
                print(build_card_html(card))
                print("=" * 60)
            else:
                res = send_to_anki(card, args.deck, args.tags)
                print(f"✔ Карточка «{word}» добавлена в Anki (note id {res['result']})")
            ok += 1
        except Exception as exc:
            print(f"✘ Ошибка на слове «{word}»: {exc}")
            failed += 1

    print(f"\nГотово: успешно {ok}, ошибок {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
