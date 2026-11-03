#!/usr/bin/env python3
"""
reverso_anki.py — английский -> переводы с context.reverso.net -> карточка Anki
в стиле колоды пользователя.

Что нового в этой версии (по сравнению с исходной):
  * Парсеры переписаны под текущую вёрстку Reverso (2025-2026):
      - переводы берутся из #translations-content a.translation,
        текст — .display-term, частота — data-freq, POS — data-original-pos;
        скрытые словоформы (translation-hidden и data-posGroup="6") пропускаются;
      - транскрипция — из #transliteration-content .ipa;
      - часть речи — из srcPos в JS-объекте response, из #pos-filters
        или из data-original-pos у переводов (старый detect_pos_from_page,
        искавший строки старой вёрстки, не работал);
      - примеры — из div.example .src .text / .trg .text,
        блоки с class="blocked" пропускаются;
  * Один запрос к базовой странице вместо N запросов по #фрагментам
    (фрагмент после # не уходит на сервер, поэтому старый подход
    отдавал одни и те же примеры для всех переводов);
  * Примеры раскладываются по переводам через подсвеченный <em> в RU
    и data-inflected у соответствующего перевода;
  * При отсутствии POS карточка больше не получает "the " по умолчанию;
  * Добавлен аргумент --model для имени типа заметки Anki
    (если у вас не "Basic", передайте точное имя модели).

Зависимости:
  pip install requests beautifulsoup4
  (опционально) pip install cloudscraper

Использование:
  python reverso_anki.py terrifying --dry-run
  python reverso_anki.py terrifying --examples 3 --ai --dry-run
  python reverso_anki.py terrifying --deck "Reverso EN-RU" --model Basic
  python reverso_anki.py --file words.txt --out anki_export.txt
  python reverso_anki.py --check terrifying
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
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

try:  # cloudscraper опционален — помогает обойти Cloudflare
    import cloudscraper  # type: ignore
except ImportError:
    cloudscraper = None


BASE_URL = "https://context.reverso.net"
LANG_PAIR = "english-russian"
REQUEST_DELAY = 1.0
MAX_TRANSLATIONS = 15
GREY_THRESHOLD_RATIO = 0.02
EXAMPLES_PER_TRANSLATION = 1
GREY_RGB = "rgb(161, 173, 173)"
BOLD_FREQ_THRESHOLD = 9999

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
    text: str
    frequency: Optional[int] = None
    part_of_speech: str = ""
    inflected: list[str] = field(default_factory=list)
    examples: list[Example] = field(default_factory=list)
    grey: bool = False
    ai_note: str = ""
    pos_indexes: list[str] = field(default_factory=list)


    @property
    def chosen_example(self) -> Optional[Example]:
        return self.examples[0] if self.examples else None

    @property
    def all_forms(self) -> set[str]:
        forms = {self.text.lower()} if self.text else set()
        for f in self.inflected:
            if f:
                forms.add(f.lower())
        return forms


@dataclass
class WordCard:
    word: str
    url: str
    pos: str = ""
    transcription: str = ""
    entries: list[TranslationEntry] = field(default_factory=list)

# ----------------------------------------------------------------------------
# Части речи
# ----------------------------------------------------------------------------

POS_ALIASES = {
    "noun": "noun", "n": "noun", "сущ": "noun", "существительное": "noun",
    "verb": "verb", "v": "verb", "gl": "verb", "глагол": "verb",
    "adjective": "adjective", "adj": "adjective", "a": "adjective",
    "прил": "adjective", "прилагательное": "adjective",
    "adverb": "adverb", "adv": "adverb", "r": "adverb",
    "нар": "adverb", "наречие": "adverb",
    "participle": "participle", "part": "participle",
    "причастие": "participle",
}

POS_LABELS_RU = {
    "noun": "",
    "verb": "",
    "adjective": "as adjective",
    "adverb": "as adverb",
    "participle": "as participle",
}

# srcPos с сайта: "adj.", "v.", "n.", "adv.", "prtp."
_SITE_POS_MAP = {
    "adj": "adjective",
    "adjective": "adjective",
    "adv": "adverb",
    "adverb": "adverb",
    "v": "verb",
    "verb": "verb",
    "n": "noun",
    "noun": "noun",
    "prtp": "participle",
    "participle": "participle",
}


def normalize_pos(pos: Optional[str]) -> str:
    if not pos:
        return ""
    return POS_ALIASES.get(pos.strip().lower(), "")

def parse_pos_filters(page_html: str) -> dict[str, str]:
    """{data-index: pos} для активных кнопок в #pos-filters."""
    soup = BeautifulSoup(page_html, "html.parser")
    mapping: dict[str, str] = {}
    for btn in soup.select("#pos-filters button"):
        if "no-action" in (btn.get("class") or []):
            continue
        idx = btn.get("data-index", "").strip()
        pos = (btn.get("data-pos") or "").lower().strip(".")
        if idx and pos in _SITE_POS_MAP:
            mapping[idx] = _SITE_POS_MAP[pos]
    return mapping


def classify_entry_pos(entry: "TranslationEntry") -> str:
    """POS перевода: сначала data-original-pos, при неудаче — по индексу фильтра."""
    raw = (entry.part_of_speech or "").lower()
    if "adj" in raw:    return "adjective"
    if "adv" in raw:    return "adverb"
    if "prtp" in raw:   return "participle"
    if raw.startswith("v") or "v." in raw:
        return "verb"
    if raw.startswith("n") or "n." in raw:
        return "noun"
    return ""


def entry_matches_pos(
    entry: "TranslationEntry",
    target: str,
    pos_map: dict[str, str],
) -> bool:
    """
    Подходит ли перевод под target POS.
    Сначала точное совпадение classify_entry_pos, потом — через pos_indexes.
    Пустой classify_entry_pos трактуем как «подходит» (страховка, чтобы не потерять).
    """
    if not target:
        return True
    direct = classify_entry_pos(entry)
    if direct == target:
        return True
    if direct:  # POS известен и не совпал — не подходит
        return False
    # fallback: пробуем через индексы фильтра
    for idx in entry.pos_indexes:
        if pos_map.get(idx) == target:
            return True
    return direct == ""  # если совсем ничего — оставляем

def detect_pos_from_page(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")

    # 1) #pos-filters — активный фильтр (самый надёжный, это UI Reverso)
    for btn in soup.select("#pos-filters button"):
        if "no-action" in (btn.get("class") or []):
            continue
        pos = (btn.get("data-pos") or "").lower().strip(".")
        if pos in _SITE_POS_MAP:
            return _SITE_POS_MAP[pos]

    # 2) srcPos из JS-объекта
    m = re.search(r'srcPos\s*:\s*"([^"]+)"', page_html)
    if m:
        raw = m.group(1).replace("\\/", "/").lower()
        for token in re.split(r"[/\s]+", raw):
            token = token.strip(".")
            if token in _SITE_POS_MAP:
                return _SITE_POS_MAP[token]

    # 3) голосование по data-original-pos всех видимых переводов
    counts: dict[str, int] = {}
    for a in soup.select("#translations-content a.translation"):
        if "translation-hidden" in (a.get("class") or []):
            continue
        raw = (a.get("data-original-pos") or "").lower()
        if "adj" in raw:
            counts["adjective"] = counts.get("adjective", 0) + 1
        elif "adv" in raw:
            counts["adverb"] = counts.get("adverb", 0) + 1
        elif "prtp" in raw:
            counts["participle"] = counts.get("participle", 0) + 1
        elif raw.startswith("v") or "v." in raw:
            counts["verb"] = counts.get("verb", 0) + 1
        elif raw.startswith("n") or "n." in raw:
            counts["noun"] = counts.get("noun", 0) + 1
    if counts:
        return max(counts, key=counts.get)

    return ""


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
    global _last_request_ts
    wait = REQUEST_DELAY - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()

    resp = get_session().get(url, timeout=60)
    if resp.status_code == 403 or "Just a moment" in resp.text[:2000]:
        raise RuntimeError(
            f"Reverso закрыл доступ (Cloudflare) для {url}.\n"
            "Варианты: pip install cloudscraper; REVERSO_COOKIES='...'; "
            "или браузерный рендеринг."
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


def check_download(word: str = "commitment") -> int:
    url = translation_url(word)
    print(f"Тестируем загрузку: {url}\n")
    attempts: list[tuple[str, requests.Session]] = []

    s = requests.Session()
    s.headers.update(_HEADERS)
    attempts.append(("requests", s))

    if cloudscraper is not None:
        try:
            attempts.append(("cloudscraper", cloudscraper.create_scraper()))
        except Exception as e:
            print("cloudscraper не инициализировался:", e)

    if os.environ.get("REVERSO_COOKIES", "").strip():
        try:
            s3 = cloudscraper.create_scraper() if cloudscraper else requests.Session()
            s3.headers.update(_HEADERS)
            apply_cookies_from_env(s3)
            attempts.append(("cookies+cf", s3))
        except Exception as e:
            print("не удалось применить cookies:", e)

    for name, sess in attempts:
        try:
            resp = sess.get(url, timeout=60)
        except Exception as e:
            print(f"[{name:>12}] ОШИБКА сети: {type(e).__name__}: {e}")
            continue
        head = resp.text[:600]
        blocked = (
            resp.status_code in (403, 503)
            or "Just a moment" in head
            or "cf-browser-verification" in head
            or "Attention Required! | Cloudflare" in head
        )
        has_content = (
            "#translations-content" in resp.text
            and "display-term" in resp.text
        )
        if blocked:
            status = "ЗАБЛОКИРОВАНО (Cloudflare)"
        elif has_content:
            status = "OK — контент найден"
        else:
            status = "Странная страница (нет #translations-content)"
        print(f"[{name:>12}] HTTP {resp.status_code} | длина {len(resp.text)} | {status}")
        if not blocked and has_content:
            print("\nВывод: страница скачивается, скрипт может работать.")
            return 0

    print(
        "\nВывод: страница НЕ скачивается.\n"
        "  1) pip install cloudscraper\n"
        "  2) REVERSO_COOKIES='cf_clearance=...; ...' python reverso_anki.py ...\n"
        "  3) браузерный рендеринг (Playwright/FlareSolverr)."
    )
    return 1


# ----------------------------------------------------------------------------
# URL-ы
# ----------------------------------------------------------------------------

def translation_url(word: str) -> str:
    safe = word.strip().replace(" ", "%20")
    return f"{BASE_URL}/translation/{LANG_PAIR}/{safe}"


# ----------------------------------------------------------------------------
# Парсинг Reverso (актуальная вёрстка)
# ----------------------------------------------------------------------------

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def _is_noise(text: str) -> bool:
    low = text.lower()
    noise = (
        "advertisement", "see also", "synonyms", "add to list",
        "more results", "suggest", "reverso", "copyright", "privacy",
        "terms", "premium", "register", "download",
    )
    return any(n in low for n in noise)


def _parse_inflected(raw: str) -> list[str]:
    """data-inflected='форма1}--{форма2}--{форма3' -> [форма1, форма2, форма3]."""
    if not raw:
        return []
    forms: list[str] = []
    for part in raw.split("}--{"):
        part = part.strip("{} ")
        if part:
            forms.append(part)
    return forms


def parse_translations(page_html: str) -> list[TranslationEntry]:
    soup = BeautifulSoup(page_html, "html.parser")
    container = soup.select_one("#translations-content")
    if container is None:
        return []

    entries: list[TranslationEntry] = []
    seen: set[str] = set()

    for a in container.select("a.translation"):
        classes = a.get("class") or []
        if "translation-hidden" in classes:
            continue
        if a.get("data-posGroup") == "6":
            continue

        term_node = a.select_one(".display-term")
        text = _clean(term_node.get_text()) if term_node else _clean(a.get("data-term", ""))
        if not text or _is_noise(text):
            continue
        if not re.search(r"[а-яА-ЯёЁ]", text):
            continue

        key = text.lower()
        if key in seen:
            continue
        seen.add(key)

        freq_raw = a.get("data-freq", "")
        freq = int(freq_raw) if freq_raw.isdigit() else None

        pos = a.get("data-original-pos", "")
        if not pos:
            span = a.select_one(".pos-mark span")
            if span is not None:
                pos = span.get("title", "") or ""

        inflected = _parse_inflected(a.get("data-inflected", ""))

        raw_idx = a.get("data-pos-index", "")
        pos_indexes = [x.strip() for x in raw_idx.split(",") if x.strip()]

        entries.append(
            TranslationEntry(
                text=text,
                frequency=freq,
                part_of_speech=_clean(pos),
                inflected=inflected,
                pos_indexes=pos_indexes,
            )
        )

    return entries[:MAX_TRANSLATIONS]


def parse_transcription(page_html: str) -> str:
    soup = BeautifulSoup(page_html, "html.parser")
    for sel in ("#transliteration-content .ipa", "div.ipa", ".transcription", ".phonetic"):
        node = soup.select_one(sel)
        if node:
            t = _clean(node.get_text())
            if t:
                return t
    return ""


def parse_all_examples(page_html: str) -> list[tuple[str, Example]]:
    """
    Возвраща список (форма-в-<em>, Example) для всех не-blocked примеров на странице.
    Форма-в-<em> — то русское слово, которое Reverso подсветил как перевод.
    """
    soup = BeautifulSoup(page_html, "html.parser")
    result: list[tuple[str, Example]] = []

    for ex in soup.select("div.example"):
        if "blocked" in (ex.get("class") or []):
            continue
        src = ex.select_one(".src .text")
        trg = ex.select_one(".trg .text")
        if src is None or trg is None:
            continue
        en = _clean(src.get_text())
        ru = _clean(trg.get_text())
        if not en or not ru or _is_noise(en):
            continue
        em = trg.select_one("em")
        form = _clean(em.get_text()).lower() if em is not None else ""
        result.append((form, Example(en=en, ru=ru)))

    return result


def distribute_examples_by_translation(
    entries: list[TranslationEntry],
    pairs: list[tuple[str, Example]],
    limit: int,
) -> None:
    """Раскладывает примеры по переводам через <em> + data-inflected."""
    if limit <= 0:
        return

    form_to_entry: dict[str, TranslationEntry] = {}
    for ent in entries:
        if ent.grey:
            continue
        for form in ent.all_forms:
            form_to_entry.setdefault(form, ent)

    for form, example in pairs:
        ent = form_to_entry.get(form)
        if ent is None:
            continue
        if len(ent.examples) >= limit:
            continue
        ent.examples.append(example)


def classify_entries(entries: list[TranslationEntry], ratio: float = GREY_THRESHOLD_RATIO) -> None:
    known = [e.frequency for e in entries if e.frequency]
    if not known:
        return
    top = max(known)
    threshold = top * ratio
    for e in entries:
        if e.frequency is not None and e.frequency <= threshold:
            e.grey = True


# ----------------------------------------------------------------------------
# Нейросеть (заготовка)
# ----------------------------------------------------------------------------

def ai_pick_best_example(word: str, translation: str, pairs: list[Example]) -> Optional[Example]:
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key or not pairs:
        return None
    try:
        listing = "\n".join(f"{i + 1}. EN: {p.en} || RU: {p.ru}" for i, p in enumerate(pairs))
        resp = requests.post(
            os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1") + "/chat/completions",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": os.environ.get("OPENAI_MODEL", "gpt-4o-mini"),
                "messages": [
                    {"role": "system",
                     "content": "Ты проверяешь пары примеров EN/RU для карточек Anki. "
                                "Выбери лучшую пару (естественный, корректный перевод, "
                                "изучаемое слово в контексте). Ответь JSON: "
                                "{\"index\": N, \"en\": \"...\", \"ru\": \"...\"}; "
                                "если перевод плохой — исправь ru-часть."},
                    {"role": "user", "content": f"Слово: {word} -> {translation}\n{listing}"},
                ],
                "max_tokens": 300,
                "temperature": 0.2,
            },
            timeout=30,
        )
        resp.raise_for_status()
        content = resp.json()["choices"][0]["message"]["content"].strip()
        m = re.search(r"\{.*\}", content, flags=re.S)
        if not m:
            return None
        data = json.loads(m.group(0))
        return Example(en=data.get("en") or pairs[0].en, ru=data.get("ru") or pairs[0].ru)
    except Exception as exc:
        print(f"  ! AI-отбор примера недоступен: {exc}")
        return None


def enrich_with_ai(card: WordCard) -> None:
    for ent in card.entries:
        if ent.grey or not ent.examples:
            continue
        best = ai_pick_best_example(card.word, ent.text, ent.examples)
        if best:
            ent.examples = [best]
        time.sleep(0.3)


# ----------------------------------------------------------------------------
# Сборка Front / Back
# ----------------------------------------------------------------------------

def grey_span(text: str) -> str:
    return f'<span style="color: {GREY_RGB};">{text}</span>'


def front_label(card: WordCard) -> str:
    w = html.escape(card.word)
    if card.pos == "noun":
        return f"the {w}"
    if card.pos == "verb":
        return f"to {w}"
    if card.pos in POS_LABELS_RU and card.pos != "":
        return f"{w} {POS_LABELS_RU[card.pos]}".strip()
    return w


def _wrap_grey_around_hit(esc: str, m: re.Match) -> str:
    before, hit, after = esc[: m.start()], esc[m.start(): m.end()], esc[m.end():]
    parts = []
    if before:
        parts.append(grey_span(before))
    parts.append(hit)
    if after:
        parts.append(grey_span(after))
    return "".join(parts)


def highlight_word(sentence: str, target: str, extra_forms: Optional[list[str]] = None) -> str:
    """
    Всё предложение серым, target (и его формы) — без стиля.
    extra_forms — inflected-формы с Reverso (data-inflected), чтобы ловить
    страшного/страшным/страшная и т.п.
    """
    esc = html.escape(sentence)
    if not target:
        return grey_span(esc)

    all_forms: list[str] = [target]
    if extra_forms:
        all_forms.extend(extra_forms)

    seen: set[str] = set()
    for form in all_forms:
        if not form:
            continue
        key = form.lower()
        if key in seen:
            continue
        seen.add(key)
        tgt = html.escape(form)
        m = re.search(
            r"(?<![A-Za-zА-Яа-яЁё'’\-])" + re.escape(tgt) + r"(?![A-Za-zА-Яа-яЁё'’\-])",
            esc, flags=re.IGNORECASE,
        )
        if m:
            return _wrap_grey_around_hit(esc, m)

    # STEM fallback: для русского отрезаем 2 последних символа
    # (страшный -> страшн, ужасающий -> ужасающ), чтобы ловить падежи.
    if re.search(r"[а-яА-ЯёЁ]", target) and len(target) > 5:
        stem = target[:-2]
        m = re.search(
            r"(?<![А-Яа-яЁё'’])" + re.escape(html.escape(stem)) + r"[А-Яа-яЁё'’]*(?![А-Яа-яЁё'’])",
            esc, flags=re.IGNORECASE,
        )
        if m:
            return _wrap_grey_around_hit(esc, m)

    return grey_span(esc)


def build_front_back(card: WordCard) -> tuple[str, str]:
    """
    Front: label + &nbsp; + транскрипция(серая), затем нумерованные EN-примеры
           через <br><br> (слово чистое, остальное серое).
    Back : переводы (жирным — если frequency > BOLD_FREQ_THRESHOLD, иначе обычным,
           «серые» — серым) + русские примеры через <br><br>.
    Никаких лишних <br><br> перед первым элементом.
    """
    label = front_label(card)

    # --- Front: label + неразрывный пробел + транскрипция ---
    if card.transcription:
        front_first = f"{label}&nbsp;{grey_span(html.escape(card.transcription))}"
    else:
        front_first = label

    # --- Back: строка переводов ---
    bold_parts: list[str] = []
    plain_parts: list[str] = []
    grey_parts: list[str] = []
    for e in card.entries:
        if e.grey:
            grey_parts.append(grey_span(html.escape(e.text)))
        elif e.frequency is not None and e.frequency > BOLD_FREQ_THRESHOLD:
            bold_parts.append(f"<b>{html.escape(e.text)}</b>")
        else:
            plain_parts.append(html.escape(e.text))

    trans_parts = bold_parts + plain_parts + grey_parts
    back_trans_line = ", ".join(trans_parts) if trans_parts else ""

    # --- Примеры (параллельно на front/back) ---
    numbered = [e for e in card.entries if (not e.grey) and e.chosen_example]
    fr_lines: list[str] = []
    bk_lines: list[str] = []
    for i, ent in enumerate(numbered, 1):
        ex = ent.chosen_example
        fr_lines.append(grey_span(f"{i}. ") + highlight_word(ex.en, card.word))
        bk_lines.append(
            grey_span(f"{i}. ") + highlight_word(ex.ru, ent.text, ent.inflected)
        )

    # --- Сборка Front: label&nbsp;trans, потом <br><br> между примерами ---
    front_parts = [front_first]
    if fr_lines:
        front_parts.append("<br><br>".join(fr_lines))

    # --- Сборка Back: переводы, потом <br><br> между примерами ---
    back_parts: list[str] = []
    if back_trans_line:
        back_parts.append(back_trans_line)
    if bk_lines:
        back_parts.append("<br><br>".join(bk_lines))

    return "<br><br>".join(front_parts), "<br><br>".join(back_parts)


def build_tsv_record(front: str, back: str) -> str:
    def q(s: str) -> str:
        return '"' + s.replace('"', '""') + '"'
    return f"{q(front)}\t{q(back)}"


# ----------------------------------------------------------------------------
# Оркестрация
# ----------------------------------------------------------------------------

def scrape_word(word: str, pos: Optional[str], examples_per_entry: int) -> WordCard:
    word = word.strip()
    url = translation_url(word)
    print(f"[1/3] Перевод слова: {url}")
    page = fetch(url)

    entries = parse_translations(page)
    transcription = parse_transcription(page)
    detected = detect_pos_from_page(page)
    user_pos = normalize_pos(pos)
    final_pos = user_pos or detected

    # Фильтр по части речи: --pos noun для lock оставит только
    # замок/блокировка/шлюз/лок/прядь/локон, а глаголы уберёт.
    if final_pos:
        pos_map = parse_pos_filters(page)
        kept = [e for e in entries if entry_matches_pos(e, final_pos, pos_map)]
        if kept and len(kept) != len(entries):
            print(f"  Фильтр POS={final_pos}: {len(entries)} -> {len(kept)} переводов")
            entries = kept

    print(f"  Часть речи: {final_pos or '— не определена'} | "
          f"транскрипция: {transcription or '—'} | значений: {len(entries)}")

    if not entries:
        print("  ! Не удалось распарсить переводы — проверьте вёрстку Reverso "
              "или запустите `python reverso_anki.py --check <слово>`")
        return WordCard(word=word, url=url, pos=final_pos,
                        transcription=transcription, entries=[])

    classify_entries(entries)

    if examples_per_entry > 0:
        print(f"[2/3] Извлекаю примеры с базовой страницы")
        pairs = parse_all_examples(page)
        print(f"      найдено не-blocked пар: {len(pairs)}")
        distribute_examples_by_translation(entries, pairs, examples_per_entry)
        assigned = sum(len(e.examples) for e in entries)
        print(f"      разложено по переводам: {assigned}")

    return WordCard(word=word, url=url, pos=final_pos,
                    transcription=transcription, entries=entries)


# ----------------------------------------------------------------------------
# AnkiConnect
# ----------------------------------------------------------------------------

ANKI_URL = os.environ.get("ANKICONNECT_URL", "http://localhost:8765")


def send_to_anki(front: str, back: str, deck_name: str, model_name: str, tags: list[str]) -> dict:
    payload = {
        "action": "addNote",
        "version": 6,
        "params": {
            "note": {
                "deckName": deck_name,
                "modelName": model_name,
                "fields": {"Front": front, "Back": back},
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


def list_anki_models() -> list[str]:
    try:
        r = requests.post(ANKI_URL, json={"action": "modelNames", "version": 6}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if data.get("error"):
            return []
        return data.get("result") or []
    except Exception:
        return []


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
    ap = argparse.ArgumentParser(
        description="Reverso (EN->RU) -> карточки Anki в стиле колоды пользователя"
    )
    ap.add_argument("words", nargs="*", help="слова на английском")
    ap.add_argument("--file", help="файл со словами (по одному в строке; опц. 'слово<TAB>часть речи')")
    ap.add_argument("--pos", choices=sorted(set(POS_ALIASES)),
                    help="часть речи: noun/verb/adjective/adverb/participle (+русские синонимы)")
    ap.add_argument("--deck", default="Reverso EN-RU", help="колода Anki")
    ap.add_argument("--model", default="Basic",
                    help="имя типа заметки Anki (по умолчанию 'Basic'). "
                         "Если у вас русская локализация — передайте точное имя, "
                         "например --model 'Основная'")
    ap.add_argument("--examples", type=int, default=EXAMPLES_PER_TRANSLATION,
                    help="макс. примеров на каждый перевод (по умолчанию 1; "
                         "для --ai рекомендуется 3)")
    ap.add_argument("--tags", nargs="*", default=[], help="теги для карточек")
    ap.add_argument("--ai", action="store_true",
                    help="нейросеть выбирает лучший пример (нужен OPENAI_API_KEY)")
    ap.add_argument("--dry-run", action="store_true",
                    help="только показать Front/Back, не отправлять в Anki")
    ap.add_argument("--out", help="записать TSV (формат импорта Anki)")
    ap.add_argument("--check", action="store_true",
                    help="диагностика загрузки Reverso и выход")
    ap.add_argument("--list-models", action="store_true",
                    help="показать доступные типы заметок в Anki и выйти")
    args = ap.parse_args(argv)

    if args.check:
        return check_download(word=(args.words[0] if args.words else "commitment"))

    if args.list_models:
        if not check_anki_connect():
            print(f"AnkiConnect не отвечает на {ANKI_URL}")
            return 2
        models = list_anki_models()
        if not models:
            print("Не удалось получить список моделей.")
            return 2
        print("Доступные модели Anki:")
        for m in models:
            print(f"  - {m}")
        return 0

    words: list[tuple[str, Optional[str]]] = []
    for w in args.words:
        words.append((w, args.pos))
    if args.file:
        with open(args.file, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                parts = re.split(r"[\t;]+", line)
                if len(parts) >= 2 and parts[1].strip():
                    words.append((parts[0].strip(), parts[1].strip().lower()))
                else:
                    words.append((parts[0].strip(), args.pos))
    if not words:
        ap.error("Укажите слова или файл через --file")

    # AI без достаточного количества примеров бесполезен — поднимем лимит,
    # только если пользователь не задал его явно больше 1
    if args.ai and args.examples <= 1:
        print("[AI] --examples < 2 — поднимаю до 3, иначе нейросети не из чего выбирать")
        args.examples = 3

    session = get_session()
    apply_cookies_from_env(session)

    need_anki = not (args.dry_run or args.out)
    if need_anki and not check_anki_connect():
        print("AnkiConnect не отвечает на " + ANKI_URL +
              ". Установите аддон AnkiConnect в Anki и повторите "
              "(или используйте --dry-run/--out).")
        return 2

    records: list[str] = []
    ok = failed = 0
    for word, pos in words:
        try:
            card = scrape_word(word, pos, examples_per_entry=args.examples)
            if not card.entries:
                print(f"✘ Пропускаю «{word}»: пустая карточка (нет переводов)")
                failed += 1
                continue
            if args.ai:
                print("[AI] Нейросеть выбирает лучшие примеры…")
                enrich_with_ai(card)
            front, back = build_front_back(card)
            records.append(build_tsv_record(front, back))
            if args.dry_run:
                print("=" * 60)
                print("FRONT:", front)
                print("-" * 60)
                print("BACK :", back)
                print("=" * 60)
            elif need_anki:
                res = send_to_anki(front, back, args.deck, args.model, args.tags)
                print(f"✔ Карточка «{word}» добавлена в Anki (note id {res['result']})")
            ok += 1
        except Exception as exc:
            print(f"✘ Ошибка на слове «{word}»: {exc}")
            failed += 1

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write("#separator:tab\n#html:true\n")
            fh.write("\n".join(records) + ("\n" if records else ""))
        print(f"Экспорт записей: {len(records)} -> {args.out}")

    print(f"\nГотово: успешно {ok}, ошибок {failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())