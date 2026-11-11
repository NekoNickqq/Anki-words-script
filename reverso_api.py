# reverso_api.py
import re
import html
import time
import json
from dataclasses import dataclass, field
from typing import Optional
import requests
from bs4 import BeautifulSoup

try:
    import cloudscraper
except ImportError:
    cloudscraper = None

BASE_URL = "https://context.reverso.net"
LANG_PAIR = "english-russian"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}
AJAX_HEADERS = {
    "Connection": "keep-alive",
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": HEADERS["User-Agent"],
    "Content-Type": "application/json; charset=UTF-8",
    "Origin": BASE_URL,
    "Sec-Fetch-Site": "same-origin",
    "Sec-Fetch-Mode": "cors",
    "Referer": f"{BASE_URL}/translation/{LANG_PAIR}/",
    "Accept-Language": HEADERS["Accept-Language"],
}

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
    pos_indexes: list[str] = field(default_factory=list)

@dataclass
class WordCard:
    word: str
    url: str
    pos: str = ""
    transcription: str = ""
    entries: list[TranslationEntry] = field(default_factory=list)

_session = None
_last_request_ts = 0.0

def get_session():
    global _session
    if _session is None:
        _session = cloudscraper.create_scraper() if cloudscraper else requests.Session()
        _session.headers.update(HEADERS)
    return _session

def fetch(url: str) -> str:
    global _last_request_ts
    wait = 1.0 - (time.time() - _last_request_ts)
    if wait > 0:
        time.sleep(wait)
    _last_request_ts = time.time()
    resp = get_session().get(url, timeout=60)
    if resp.status_code == 403 or "Just a moment" in resp.text[:2000]:
        raise RuntimeError(f"Cloudflare блокировка на {url}")
    resp.raise_for_status()
    return resp.text

def _clean(text: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(text)).strip()

def _is_noise(text: str) -> bool:
    low = text.lower()
    return any(n in low for n in (
        "advertisement", "see also", "synonyms", "add to list",
        "more results", "suggest", "reverso", "copyright", "privacy",
        "terms", "premium", "register", "download"
    ))

def _parse_inflected(raw: str) -> list[str]:
    if not raw: return []
    return [p.strip("{} ") for p in raw.split("}--{") if p.strip("{} ")]

_SITE_POS_MAP = {
    "adj": "adjective", "adv": "adverb", "v": "verb", "n": "noun",
    "prtp": "participle", "verb": "verb", "noun": "noun",
    "adjective": "adjective", "adverb": "adverb", "participle": "participle"
}

def _classify_pos(raw: str) -> str:
    raw = raw.lower()
    if "adj" in raw: return "adjective"
    if "adv" in raw: return "adverb"
    if "prtp" in raw: return "participle"
    if raw.startswith("v") or "v." in raw: return "verb"
    if raw.startswith("n") or "n." in raw: return "noun"
    return ""

def fetch_all_examples_via_ajax(word: str, target_text: str = "", source_lang: str = "en", target_lang: str = "ru", max_pages: int = 3) -> list[dict]:
    """Запрашивает примеры для конкретного перевода (target_text)."""
    session = get_session()
    url = f"{BASE_URL}/bst-query-service"
    data = {
        "source_text": word,
        "target_text": target_text,  # <--- ГЛАВНОЕ ИЗМЕНЕНИЕ: фильтруем по переводу
        "source_lang": source_lang,
        "target_lang": target_lang,
        "npage": 1,
        "mode": 0
    }
    all_examples = []
    try:
        resp = session.post(url, headers=AJAX_HEADERS, data=json.dumps(data), timeout=30)
        resp.raise_for_status()
        first_page = resp.json()
        npages = min(int(first_page.get("npages", 1)), max_pages)
        all_examples.extend(first_page.get("list", []))
        
        for npage in range(2, npages + 1):
            data["npage"] = npage
            resp = session.post(url, headers=AJAX_HEADERS, data=json.dumps(data), timeout=30)
            resp.raise_for_status()
            page_data = resp.json()
            all_examples.extend(page_data.get("list", []))
            time.sleep(0.2)
        return all_examples
    except Exception:
        return []

def get_word_data(word: str, target_pos: str = "", include_blocked: bool = False) -> WordCard:
    url = f"{BASE_URL}/translation/{LANG_PAIR}/{word.strip().replace(' ', '%20')}"
    page = fetch(url)
    soup = BeautifulSoup(page, "html.parser")

    # 1. Определяем главную часть речи страницы
    detected_pos = ""
    for btn in soup.select("#pos-filters button"):
        if "no-action" in (btn.get("class") or []): continue
        pos = (btn.get("data-pos") or "").lower().strip(".")
        if pos in _SITE_POS_MAP:
            detected_pos = _SITE_POS_MAP[pos]
            break

    if not target_pos and detected_pos:
        target_pos = detected_pos

    transcription = ""
    for sel in ("#transliteration-content .ipa", "div.ipa"):
        node = soup.select_one(sel)
        if node:
            transcription = _clean(node.get_text())
            break

    pos_map = {}
    for btn in soup.select("#pos-filters button"):
        if "no-action" in (btn.get("class") or []): continue
        idx = (btn.get("data-index") or "").strip()
        pos = (btn.get("data-pos") or "").lower().strip(".")
        if idx and pos in _SITE_POS_MAP:
            pos_map[idx] = _SITE_POS_MAP[pos]

    entries = []
    seen = set()
    container = soup.select_one("#translations-content")
    if container:
        for node in container.select("a.translation, div.translation"):
            if "translation-hidden" in (node.get("class") or []): continue
            if node.get("data-posgroup") == "6": continue
            
            term_node = node.select_one(".display-term")
            text = _clean(term_node.get_text()) if term_node else _clean(node.get("data-term", ""))
            if not text or _is_noise(text) or not re.search(r"[а-яА-ЯёЁ]", text): continue
            
            key = text.lower()
            if key in seen: continue
            seen.add(key)
            
            freq_raw = node.get("data-freq", "")
            freq = int(freq_raw) if freq_raw.isdigit() else None
            
            pos_raw = node.get("data-original-pos", "")
            if not pos_raw:
                span = node.select_one(".pos-mark span")
                if span: pos_raw = span.get("title", "")
            
            ent = TranslationEntry(
                text=text,
                frequency=freq,
                part_of_speech=_clean(pos_raw),
                inflected=_parse_inflected(node.get("data-inflected", "")),
                pos_indexes=[x.strip() for x in node.get("data-pos-index", "").split(",") if x.strip()]
            )
            
            # Фильтрация по POS
            if target_pos:
                direct_pos = _classify_pos(ent.part_of_speech)
                match = (direct_pos == target_pos)
                if not match and ent.pos_indexes:
                    for idx in ent.pos_indexes:
                        if pos_map.get(idx) == target_pos:
                            match = True
                            break
                    if not match:
                        known_mapped = [pos_map.get(idx) for idx in ent.pos_indexes if pos_map.get(idx)]
                        if known_mapped and target_pos not in known_mapped:
                            continue
                if not match and direct_pos != "" and direct_pos != target_pos:
                    continue
            
            entries.append(ent)

    if not entries:
        return WordCard(word=word, url=url, pos=detected_pos, transcription=transcription, entries=[])

    # 4. ТЕПЕРЬ ЗАПРАШИВАЕМ ПРИМЕРЫ ДЛЯ КАЖДОГО ПЕРЕВОДА ОТДЕЛЬНО
    for ent in entries:
        # Сначала пробуем найти в HTML (быстро)
        for ex in soup.select("div.example"):
            if not include_blocked and "blocked" in (ex.get("class") or []): continue
            src = ex.select_one(".src .text")
            trg = ex.select_one(".trg .text")
            if src and trg:
                ru = _clean(trg.get_text())
                if ent.text.lower() in ru.lower():
                    en = _clean(src.get_text())
                    if en and ru and not _is_noise(en):
                        ent.examples.append(Example(en=en, ru=ru))
        
        # Если в HTML не нашли (или нашли мало), идем в AJAX за конкретным переводом
        if len(ent.examples) == 0:
            ajax_examples = fetch_all_examples_via_ajax(word, target_text=ent.text)
            for ex_data in ajax_examples:
                en_raw = ex_data.get("s_text", "")
                ru_raw = ex_data.get("t_text", "")
                if not en_raw or not ru_raw: continue
                
                en = _clean(BeautifulSoup(en_raw, "html.parser").get_text())
                ru = _clean(BeautifulSoup(ru_raw, "html.parser").get_text())
                ent.examples.append(Example(en=en, ru=ru))

    known = [e.frequency for e in entries if e.frequency is not None]
    if known:
        top = max(known)
        threshold = top * 0.02
        filtered = []
        for e in entries:
            if e.frequency is not None:
                if e.frequency <= 11: continue
                if e.frequency <= threshold: e.grey = True
            filtered.append(e)
        entries = filtered

    return WordCard(word=word, url=url, pos=detected_pos, transcription=transcription, entries=entries)