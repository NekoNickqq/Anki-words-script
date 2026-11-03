#!/usr/bin/env python3
"""
reverso_anki.py — английский -> переводы с context.reverso.net -> карточка Anki в стиле колоды пользователя.

Что делает:
  1. Скачивает страницу https://context.reverso.net/translation/english-russian/<слово>.
  2. Извлекает ВСЕ значения перевода вместе с частотой (число примеров использования у каждого перевода).
  3. Определяет часть речи слова (сущ / глагол / прил / нар / прич) — по флажку --pos или автоматически.
  4. Для каждого «значимого» перевода открывает страницу примера
     (https://context.reverso.net/translation/english-russian/<слово>#<перевод>) и берёт пару EN/RU
     (пока одну, самую первую; функционал на все пары заложен — их можно «скормить» нейросети для выбора лучшей).
  5. Собирает Front/Back карточки ровно в том же стиле, что в примерах колоды:
       * транскрипция, нумерация и сами примеры — серым rgb(161, 173, 173);
       * изучаемое слово в примере — без стиля (выбивается из серого);
       * частые переводы — жирным; «второстепенные» (частота <= 2% от самой частотной) — просто серым
         списком и БЕЗ примеров на обеих сторонах;
       * на Front — the word (сущ.), to word (глагол), word as adjective/adverb/participle (прочее);
       * транскрипция с сайта пишется сразу после слова и его обозначения части речи;
  6. Отправляет карточку в Anki через AnkiConnect (http://localhost:8765) либо пишет .txt в формате
     импорта колоды (TSV с заголовками #separator:tab / #html:true) — как в присланных примерах карточек.

Нейросеть (заготовка):
  Функция `ai_pick_best_example(word, translation, pairs)` возвращает лучшую пару EN/RU из всех найденных
  (или переписывает некорректный ru-перевод). Пока флаг `--ai` выключен — берётся первая пара.

Зависимости:
  pip install requests beautifulsoup4
  (опционально, для обхода Cloudflare: pip install cloudscraper)

Использование:
  python reverso_anki.py commitment --pos noun --dry-run          # показать Front/Back
  python reverso_anki.py pressure --pos noun --examples 3         # до 3 пар на перевод (для AI-отбора)
  python reverso_anki.py --file words.txt --out anki_export.txt   # список слов -> TSV-файл
  python reverso_anki.py take over --pos verb                     # отправить в Anki
  python reverso_anki.py --check commitment                        # диагностика загрузки Reverso
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

try:  # cloudscraper опционален — помогает обойти Cloudflare-защиту reverso
    import cloudscraper  # type: ignore
except ImportError:
    cloudscraper = None


BASE_URL = "https://context.reverso.net"
LANG_PAIR = "english-russian"          # направление перевода на сайте
REQUEST_DELAY = 1.0                     # вежливая пауза между запросами, сек
MAX_TRANSLATIONS = 15                   # разумный потолок значений
GREY_THRESHOLD_RATIO = 0.02             # перевод «засеривается», если частота <= 2% от самой частотной
EXAMPLES_PER_TRANSLATION = 1            # пока одна пара на перевод (лимит растёт для режима --ai)

# Цвет «второстепенной» информации (транскрипция, примеры, редкие переводы) — как в колоде пользователя
GREY_RGB = "rgb(161, 173, 173)"

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
    text: str                               # перевод на русский ("приверженность", ...)
    frequency: Optional[int] = None         # число примеров использования на reverso.context
    part_of_speech: str = ""                # глагол / существительное и т.п. (если удалось определить)
    examples: list[Example] = field(default_factory=list)   # все найденные пары (для AI-отбора)
    grey: bool = False                      # True => второстепенный: серый, без примеров
    ai_note: str = ""                       # сюда нейросеть позже добавит пояснение

    @property
    def chosen_example(self) -> Optional[Example]:
        """Пока — самая первая пара; позже здесь будет выбор нейросети."""
        return self.examples[0] if self.examples else None


@dataclass
class WordCard:
    word: str
    url: str
    pos: str = "noun"                       # noun / verb / adjective / adverb / participle
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
    "participle": "participle", "part": "participle", "communion": "participle",
    "причастие": "participle",
}

POS_LABELS_RU = {  # подпись для front-стороны карточки
    "noun": "",                # front: the word
    "verb": "",                # front: to word
    "adjective": "as adjective",
    "adverb": "as adverb",
    "participle": "as participle",
}


def normalize_pos(pos: Optional[str]) -> str:
    if not pos:
        return "noun"
    return POS_ALIASES.get(pos.strip().lower(), "noun")


def detect_pos_from_page(page_html: str) -> str:
    """Пытается определить часть речи по разметке страницы Reverso (классы/подписи блоков)."""
    low = page_html.lower()
    checks = [
        ("verb", ("translations-block-verb", ">verbs<", "глаголы")),
        ("adjective", ("translations-block-adjective", ">adjectives<", "прилагательные")),
        ("adverb", ("translations-block-adverb", ">adverbs<", "наречия")),
        ("participle", ("translations-block-participle", ">participles<", "причастия")),
        ("noun", ("translations-block-noun", ">nouns<", "существительные")),
    ]
    scores = []
    for pos, needles in checks:
        score = sum(low.count(n) for n in needles)
        if score:
            scores.append((score, pos))
    if not scores:
        return ""
    scores.sort(reverse=True)
    return scores[0][1]


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
            "  1) pip install cloudscraper\n"
            "  2) открыть страницу в браузере и передать cookies через переменную окружения\n"
            "     REVERSO_COOKIES='cf_clearance=...; ...' (см. README)\n"
            "  3) использовать прокси/браузерный рендеринг (FlareSolverr, Playwright)."
        )
    resp.raise_for_status()
    return resp.text


def check_download(word: str = "commitment", url: Optional[str] = None) -> int:
    """Диагностика: реально ли скачивается страница Reverso."""
    if url is None:
        url = translation_url(word)
    print(f"Тестируем загрузка: {url}\n")
    attempts: list[tuple[str, requests.Session]] = []
    try:
        s = requests.Session()
        s.headers.update(_HEADERS)
        attempts.append(("requests", s))
    except Exception as e:  # pragma: no cover
        print("requests недоступен:", e)
    if cloudscraper is not None:
        try:
            attempts.append(("cloudscraper", cloudscraper.create_scraper()))
        except Exception as e:
            print("cloudscraper не инициализировался:", e)
    if os.environ.get("REVERSO_COOKIES", "").strip():
        try:
            s3 = cloudscraper.create_scraper() if cloudscraper is not None else requests.Session()
            s3.headers.update(_HEADERS)
            apply_cookies_from_env(s3)
            attempts.append(("cookies+cf", s3))
        except Exception as e:
            print("не удалось применить cookies:", e)

    for name, sess in attempts:
        try:
            resp = sess.get(url, timeout=30)
        except Exception as e:
            print(f"[{name:>12}] ОШИБКА сети: {type(e).__name__}: {e}")
            continue
        text_head = resp.text[:600]
        blocked = (
            resp.status_code in (403, 503)
            or "Just a moment" in text_head
            or "cf-browser-verification" in text_head
            or "Attention Required! | Cloudflare" in text_head
        )
        has_content = "translation" in resp.text and ("example" in resp.text or "result" in resp.text)
        status = "ЗАБЛОКИРОВАНО (Cloudflare)" if blocked else ("OK — контент найден" if has_content else "Странная страница")
        print(f"[{name:>12}] HTTP {resp.status_code} | длина {len(resp.text)} симв. | {status}")
        if not blocked and has_content:
            print("\nВывод: страница скачивается, скрипт может работать.")
            return 0
    print(
        "\nВывод: страница НЕ скачивается (Cloudflare).\n"
        "Что сделать:\n"
        "  1) pip install cloudscraper\n"
        "  2) пройти проверку в браузере и экспортировать cookies: REVERSO_COOKIES='...' python reverso_anki.py ...\n"
        "  3) либо браузерный рендеринг (Playwright/FlareSolverr)."
    )
    return 1


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
# URL-ы
# ----------------------------------------------------------------------------

def translation_url(word: str) -> str:
    safe = word.strip().replace(" ", "%20")
    return f"{BASE_URL}/translation/{LANG_PAIR}/{safe}"


def quote_anchor(text: str) -> str:
    """Percent-encode якоря (русские буквы), как это делает сам Reverso в ссылках вида #%D0%BF..."""
    return quote(text.strip().lower())


def examples_fragment_url(word: str, translation_ru: str) -> str:
    """Страница примера конкретного перевода: <страница слова>#<перевод> (как на сайте)."""
    return f"{translation_url(word)}#{quote_anchor(translation_ru)}"


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


def _parse_frequency(node) -> Optional[int]:
    """Достаёт число примеров использования перевода из li/a блока (.frequency-number, .count и т.п.)."""
    if node is None:
        return None
    for sel in (".frequency-number", ".frequency", ".translation-frequency", "[class*=frequency]", ".count"):
        n = node.select_one(sel)
        if n:
            digits = re.sub(r"[^\d]", "", n.get_text())
            if digits:
                return int(digits)
    # последний вариант — числа в конце текста элемента ("приверженность 10874")
    m = re.search(r"(\d[\d\s\u00a0]*)\s*$", _clean(node.get_text()))
    if m:
        digits = re.sub(r"\D", "", m.group(1))
        if digits:
            return int(digits)
    return None


def parse_translations(page_html: str) -> list[TranslationEntry]:
    """Извлекает все англо->русские значения со страницы перевода ВМЕСТЕ с частотой примеров."""
    soup = BeautifulSoup(page_html, "html.parser")
    entries: list[TranslationEntry] = []
    seen: set[str] = set()

    containers = soup.select("div.vertical-block section.section") or [soup]
    for container in containers:
        for li in container.select("ul.translation-list li, .translation-list li"):
            a = li.find("a")
            span_text = li.select_one(".text")
            node = a or span_text
            if node is None:
                continue
            text = _clean(span_text.get_text() if span_text else node.get_text())
            if not text or _is_noise(text) or len(text) > 120:
                continue
            if not re.search(r"[а-яА-ЯёЁ]", text):
                continue
            pos_node = li.select_one(".pos")
            pos = _clean(pos_node.get_text()) if pos_node else ""
            freq = _parse_frequency(li) or _parse_frequency(a)

            key = text.lower()
            if key in seen:
                continue
            seen.add(key)
            entries.append(TranslationEntry(text=text, frequency=freq, part_of_speech=pos))

        if entries:
            break

    # Запасной вариант: любые ссылки вида /translation/english-russian/<rus>
    if not entries:
        for a in soup.select(f'a[href*="/translation/{LANG_PAIR}/"]'):
            text = _clean(a.get_text())
            if not text or text.isascii():
                continue
            if not re.search(r"[а-яА-ЯёЁ]", text):
                continue
            key = text.lower()
            if key in seen or _is_noise(key):
                continue
            seen.add(key)
            entries.append(TranslationEntry(text=text))

    return entries[:MAX_TRANSLATIONS]


def parse_transcription(page_html: str, word: str) -> str:
    """Транскрипция слова со страницы (/prɛʃə/ или [prɛʃə])."""
    soup = BeautifulSoup(page_html, "html.parser")
    for sel in (".transcription", ".phonetic", "[class*=transcription]"):
        n = soup.select_one(sel)
        if n:
            t = _clean(n.get_text())
            if t:
                return t if t.startswith(("/", "[")) else f"/{t}/"
    m = re.search(r"[/\[]/[a-zæŋˈˌəɪʊɒɔʌθðʃʒːˑ\-\s]+/[/\]]", page_html, flags=re.I)
    if m:
        return m.group(0).strip("/[] ")
    return ""


def parse_examples(page_html: str, keyword_ru: str, word_en: str, limit: int) -> list[Example]:
    """Извлекает пары примеров EN/RU (до limit; лимит можно поднять — тогда все пары уходят в AI-отбор)."""
    soup = BeautifulSoup(page_html, "html.parser")
    examples: list[Example] = []

    pairs = []
    for ex in soup.select("div.example, li.example"):
        src = ex.select_one(".source")
        tgt = ex.select_one(".target")
        if src and tgt:
            pairs.append((_clean(src.get_text()), _clean(tgt.get_text())))

    stem_ru = keyword_ru.lower()[:-1] if len(keyword_ru) > 4 else keyword_ru.lower()
    for en, ru in pairs:
        if len(examples) >= limit:
            break
        if not en or not ru or _is_noise(en):
            continue
        ru_low = ru.lower()
        # перевод должен встречаться в ru-предложении (точная форма или русская лексема),
        # а изучаемое слово — в en-предложении
        if (keyword_ru.lower() in ru_low or stem_ru in ru_low) and word_en.lower() in en.lower():
            examples.append(Example(en=en, ru=ru))

    # если точных совпадений нет — берём первые попавшиеся
    if not examples:
        for en, ru in pairs:
            if len(examples) >= limit:
                break
            if en and ru and not _is_noise(en):
                examples.append(Example(en=en, ru=ru))

    return examples


# ----------------------------------------------------------------------------
# Отбор переводов: жирный / серый / порог 2%
# ----------------------------------------------------------------------------

def classify_entries(entries: list[TranslationEntry], ratio: float = GREY_THRESHOLD_RATIO) -> None:
    """
    Самый частый перевод — эталон. Если у перевода примеров <= ratio (2%) от эталона —
    он помечается grey=True: пишется серым и БЕЗ примеров на обеих сторонах карточки.
    """
    known_freqs = [e.frequency for e in entries if e.frequency]
    if not known_freqs:
        return
    top = max(known_freqs)
    threshold = top * ratio
    for e in entries:
        if e.frequency is not None and e.frequency <= threshold:
            e.grey = True


# ----------------------------------------------------------------------------
# Нейросеть (заготовка под отбор лучшего примера)
# ----------------------------------------------------------------------------

def ai_pick_best_example(word: str, translation: str, pairs: list[Example]) -> Optional[Example]:
    """
    Точка подключения LLM: из всех найденных пар (en, ru) выбрать лучшую,
    а при некорректном переводе — заменить ru-часть своим вариантом.
    Сейчас: заглушка, если OPENAI_API_KEY не задан (возвращает None -> используется первая пара).
    """
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
                     "content": "Ты проверяешь пары примеров EN/RU для карточек Anki. Выбери лучшую пару "
                                "(естественный, корректный перевод, изучаемое слово в контексте). "
                                "Ответь JSON: {\"index\": N, \"en\": \"...\", \"ru\": \"...\"}; "
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
# Сборка Front / Back в стиле колоды
# ----------------------------------------------------------------------------

def css_color_decl() -> str:
    return f"color: {GREY_RGB};"


def grey_span(text: str) -> str:
    return f'<span style="{css_color_decl()}">{text}</span>'


def front_label(card: WordCard) -> str:
    """the word / to word / word as adjective|adverb|participle"""
    w = html.escape(card.word)
    if card.pos == "noun":
        return f"the {w}"
    if card.pos == "verb":
        return f"to {w}"
    return f"{w} {POS_LABELS_RU.get(card.pos, '')}".strip()


def highlight_word(sentence: str, target: str) -> str:
    """
    Возвращает HTML: весь текст предложения в сером span, а target — вне span (без стиля),
    ровно как в примерах колоды: "... huge </span>pressure<span ...> on ...".
    Сначала ищем точное целословное вхождение; затем — совпадение с русской лексемой
    ("обязательство" найдётся в "серьёзным обязательством"); если ничего не найдено —
    всё предложение серым.
    """
    esc = html.escape(sentence)
    tgt = html.escape(target)

    m = re.search(r"(?<![A-Za-zА-Яа-яЁё'’-])" + re.escape(tgt) + r"(?![A-Za-zА-Яа-яЁё'’-])",
                  esc, flags=re.IGNORECASE)
    if not m:
        # русские формы: отбрасываем окончание и ищем основу целословно (для EN-слов основа = всё слово)
        stem = tgt[:-1] if len(tgt) > 4 and re.search(r"[а-яА-ЯёЁ]", tgt) else None
        if stem:
            m = re.search(r"(?<![А-Яа-яЁё'])" + re.escape(stem) + r"[А-Яа-яЁё']*(?![А-Яа-яЁё'])",
                          esc, flags=re.IGNORECASE)
    if not m:
        return grey_span(esc)
    before, hit, after = esc[: m.start()], esc[m.start(): m.end()], esc[m.end():]
    parts = []
    if before:
        parts.append(grey_span(before))
    parts.append(hit)  # без стиля — изучаемое слово/форма
    if after:
        parts.append(grey_span(after))
    return "".join(parts)


def build_front_back(card: WordCard) -> tuple[str, str]:
    """
    Front: the/to/... word + транскрипция (серым) + нумерованные англ. примеры (серым, слово чистым).
    Back : переводы (частые — жирным, редкие — серым списком) + русские примеры (серым, слово чистым).
    """
    label = front_label(card)
    trans = f"<br><br>{grey_span(html.escape(card.transcription))}" if card.transcription else ""

    front_parts = [f"{label}{trans}"]
    back_parts: list[str] = []

    # --- строка переводов: значимые (жирным), затем второстепенные (просто серым, без примеров) ---
    bold_tr = [html.escape(e.text) for e in card.entries if not e.grey]
    grey_tr = [grey_span(html.escape(e.text)) for e in card.entries if e.grey]
    back_line = ""
    if bold_tr:
        first = f"<b>{bold_tr[0]}</b>"
        rest = ", ".join(bold_tr[1:])
        back_line = first + (", " + rest if rest else "")
    if grey_tr:
        back_line += (", " if back_line else "") + ", ".join(grey_tr)
    if back_line:
        back_parts.append(back_line)

    # --- примеры: сквозная нумерация, параллельно на front/back ---
    numbered = [e for e in card.entries if (not e.grey) and e.chosen_example]
    if numbered:
        fr, bk = [], []
        for i, ent in enumerate(numbered, 1):
            ex = ent.chosen_example
            fr.append("<br><br>" + grey_span(f"{i}. ") + highlight_word(ex.en, card.word))
            bk.append("<br><br>" + grey_span(f"{i}. ") + highlight_word(ex.ru, ent.text))
        front_parts.append("".join(fr))
        back_parts.append("".join(bk))

    return "".join(front_parts), "<br><br>".join(back_parts)


def build_tsv_record(front: str, back: str) -> str:
    """Одна запись формата импорта Anki (TSV, кавычки удваиваются), как в примерах пользователя."""
    def q(s: str) -> str:
        return '"' + s.replace('"', '""') + '"'
    return f"{q(front)}\t{q(back)}"


# ----------------------------------------------------------------------------
# Scraping оркестрация
# ----------------------------------------------------------------------------

def scrape_word(word: str, pos: Optional[str], examples_per_entry: int,
                 page_html: Optional[str] = None) -> WordCard:
    word = word.strip()
    url = translation_url(word)
    print(f"[1/3] Перевод слова: {url}")
    page = page_html if page_html is not None else fetch(url)

    entries = parse_translations(page)
    transcription = parse_transcription(page, word)
    detected = detect_pos_from_page(page)
    final_pos = normalize_pos(pos or detected)
    print(f"  Часть речи: {final_pos} | транскрипция: {transcription or '—'} | значений: {len(entries)}")

    classify_entries(entries)

    if examples_per_entry > 0:
        active = [e for e in entries if not e.grey]
        for i, ent in enumerate(active, 1):
            eurl = examples_fragment_url(word, ent.text)
            try:
                print(f"[2/3] Примеры для «{ent.text}» ({i}/{len(active)}): {eurl}")
                ent.examples = parse_examples(fetch(eurl), ent.text, word, examples_per_entry)
                print(f"      -> {len(ent.examples)} пример(ов)")
            except Exception as exc:  # не критично: карточку можно собрать и без примеров
                print(f"      ! Примеры не получены: {exc}")

    return WordCard(word=word, url=url, pos=final_pos, transcription=transcription, entries=entries)


# ----------------------------------------------------------------------------
# Отправка в Anki
# ----------------------------------------------------------------------------

ANKI_URL = os.environ.get("ANKICONNECT_URL", "http://localhost:8765")


def send_to_anki(front: str, back: str, deck_name: str, tags: list[str]) -> dict:
    payload = {
        "action": "addNote",
        "version": 6,
        "params": {
            "note": {
                "deckName": deck_name,
                "modelName": "Basic",
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
    ap = argparse.ArgumentParser(description="Reverso (EN->RU) -> Anki-карточки в стиле колоды пользователя")
    ap.add_argument("words", nargs="*", help="слова на английском")
    ap.add_argument("--file", help="файл со словами (по одному в строке; опц. 'слово<TAB>часть речи')")
    ap.add_argument("--pos", choices=sorted(set(POS_ALIASES)),
                    help="часть речи: noun/verb/adjective/adverb/participle (+русские синонимы)")
    ap.add_argument("--deck", default="Reverso EN-RU", help="колода Anki")
    ap.add_argument("--examples", type=int, default=EXAMPLES_PER_TRANSLATION,
                    help="макс. пар примеров на перевод (1 — текущий режим; большой лимит — для AI-отбора)")
    ap.add_argument("--tags", nargs="*", default=[], help="теги для карточек")
    ap.add_argument("--ai", action="store_true", help="нейросеть выбирает лучший пример (OPENAI_API_KEY)")
    ap.add_argument("--dry-run", action="store_true", help="только показать Front/Back, не отправлять")
    ap.add_argument("--out", help="записать TSV-экспорт (формат импорта Anki: #separator:tab/#html:true)")
    ap.add_argument("--check", action="store_true", help="диагностика загрузки Reverso и выход")
    args = ap.parse_args(argv)

    if args.check:
        return check_download(word=(args.words[0] if args.words else "commitment"))

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

    session = get_session()
    apply_cookies_from_env(session)

    need_anki = not (args.dry_run or args.out)
    if need_anki and not check_anki_connect():
        print("AnkiConnect не отвечает на " + ANKI_URL +
              ". Установите аддон AnkiConnect в Anki и повторите (или используйте --dry-run/--out).")
        return 2

    records: list[str] = []
    ok = failed = 0
    for word, pos in words:
        try:
            card = scrape_word(word, pos, examples_per_entry=args.examples)
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
                res = send_to_anki(front, back, args.deck, args.tags)
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
