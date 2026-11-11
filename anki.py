# anki.py
import argparse
import html
import json
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import requests

from reverso_api import get_word_data, WordCard, TranslationEntry, Example

OLLAMA_DEFAULT_MODEL = "huihui_ai/gemma-4-abliterated:12b"
OLLAMA_DEFAULT_URL = "http://localhost:11434"
ANKI_URL = os.environ.get("ANKICONNECT_URL", "http://localhost:8765")
BADEXAMPLES_DIR = Path(__file__).resolve().parent / "badexamples"
GREY_RGB = "rgb(161, 173, 173)"
BOLD_FREQ_THRESHOLD = 9999
MODAL_VERBS = {"can", "could", "may", "might", "must", "shall", "should", "will", "would", "ought", "need", "dare"}

_log_file = None
def log(msg, level="info"):
    prefix = {"debug": "[DEBUG]", "warn": "[WARN]", "error": "[ERR]"}.get(level, "")
    line = f"{prefix} {msg}" if prefix else msg
    print(line)
    if _log_file:
        with open(_log_file, "a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()} {line}\n")

# --- AI и Ollama ---
# (функции _ollama_chat, ai_generate_examples, ai_verify_pair, save_bad_example, enrich_with_ai остаются без изменений)
def _ollama_chat(model, messages, schema, url):
    payload = {"model": model, "messages": messages, "stream": False, "format": schema, "options": {"temperature": 0.7}}
    try:
        resp = requests.post(f"{url.rstrip('/')}/api/chat", json=payload, timeout=180)
        resp.raise_for_status()
        return json.loads(resp.json()["message"]["content"])
    except Exception as exc:
        log(f"  ! Ollama ошибка: {exc}", "warn")
        return None

def ai_generate_examples(word, translation, pos, count, model, url):
    if count <= 0: return []
    schema = {"type": "object", "properties": {"examples": {"type": "array", "items": {"type": "object", "properties": {"en": {"type": "string"}, "ru": {"type": "string"}}, "required": ["en", "ru"]}}}, "required": ["examples"]}
    pos_label = pos or "unspecified"
    prompt = (
        f"Generate exactly {count} DIVERSE and INTERESTING English sentences for an Anki flashcard.\n"
        f"English word: \"{word}\"\nRussian translation: \"{translation}\"\nPart of speech: {pos_label}\n\n"
        f"1. Level B1-B2. 2. Use \"{word}\" or correct inflected forms. 3. Different subjects/tenses. "
        f"4. 8-20 words long. 5. Russian MUST contain \"{translation}\" or inflected form.\n6. Return ONLY JSON."
    )
    data = _ollama_chat(model, [{"role": "user", "content": prompt}], schema, url)
    return [Example(en=x["en"].strip(), ru=x["ru"].strip()) for x in data.get("examples", [])] if data else []

def ai_verify_pair(word, translation, pos, example, model, url):
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}, "error": {"type": "string"}, "fixed_en": {"type": "string"}, "fixed_ru": {"type": "string"}}, "required": ["ok"]}
    prompt = (
        f"Verify EN/RU pair.\nEN: {example.en}\nRU: {example.ru}\nWord: \"{word}\" -> \"{translation}\" ({pos or 'unspecified'})\n\n"
        f"Check if EN contains \"{word}\", RU contains \"{translation}\", and they match in meaning. "
        f"If wrong, fix EN to match RU. Return JSON: {{'ok': true}} or {{'ok': false, 'error': '...', 'fixed_en': '...', 'fixed_ru': '...'}}."
    )
    data = _ollama_chat(model, [{"role": "user", "content": prompt}], schema, url)
    if not data: return {"ok": True, "error": "", "fixed_en": None, "fixed_ru": None}
    return {"ok": bool(data.get("ok", True)), "error": data.get("error", ""), "fixed_en": (data.get("fixed_en") or "").strip() or None, "fixed_ru": (data.get("fixed_ru") or "").strip() or None}

def save_bad_example(word, translation, pos, example, error):
    BADEXAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    n = 1
    while (BADEXAMPLES_DIR / f"{n:04d}_{word.replace(' ', '_')}.json").exists(): n += 1
    path = BADEXAMPLES_DIR / f"{n:04d}_{word.replace(' ', '_')}.json"
    payload = {"word": word, "translation": translation, "pos": pos, "example": {"en": example.en, "ru": example.ru}, "error": error, "saved_at": datetime.now().isoformat()}
    with open(path, "w", encoding="utf-8") as f: json.dump(payload, f, ensure_ascii=False, indent=2)
    log(f"  ✗ Плохой пример сохранён: {path.name}", "warn")

def enrich_with_ai(card, count, model, url, verify=True, ai_only=False):
    for ent in card.entries:
        if ent.grey: continue
        if ai_only: ent.examples = []
        need = max(0, count - len(ent.examples))
        if need > 0:
            log(f"  [AI] генерирую {need} пример(ов) для «{ent.text}»…")
            ent.examples.extend(ai_generate_examples(card.word, ent.text, card.pos, need, model, url))
        if not verify: continue
        kept = []
        for ex in ent.examples:
            res = ai_verify_pair(card.word, ent.text, card.pos, ex, model, url)
            if res["ok"]: kept.append(ex)
            elif res["fixed_en"] and res["fixed_ru"]: kept.append(Example(en=res["fixed_en"], ru=res["fixed_ru"]))
            else: save_bad_example(card.word, ent.text, card.pos, ex, res["error"])
        ent.examples = kept[:count]

# --- Построение карточки ---
def grey_span(text): return f'<span style="color: {GREY_RGB};">{text}</span>'

def front_label(card):
    w = html.escape(card.word)
    if card.pos == "noun": return f"the {w}"
    if card.pos == "verb": return w if card.word.lower() in MODAL_VERBS else f"to {w}"
    if card.pos in ("adjective", "adverb", "participle"): return f"{w} as {card.pos}"
    return w

def get_rarity_tildes(freq: Optional[int]) -> str:
    if not freq: return ""
    if freq < 100: return "~~~"
    if freq < 300: return "~~"
    if freq < 800: return "~"
    return ""

def _make_flexible_pattern(form: str) -> str:
    parts = []
    for ch in form:
        if ch in ("е", "ё", "Е", "Ё"):
            parts.append("[еёЕЁ]")
        else:
            parts.append(re.escape(html.escape(ch)))
    return "".join(parts)

def highlight_word(sentence, target, extra_forms=None):
    esc = html.escape(sentence)
    if not target: return grey_span(esc)
    all_forms = [target] + (extra_forms or [])
    for form in set(all_forms):
        if not form: continue
        pattern = _make_flexible_pattern(form)
        m = re.search(r"(?<![A-Za-zА-Яа-яЁё'’\-])" + pattern + r"(?![A-Za-zА-Яа-яЁё'’\-])", esc, re.IGNORECASE)
        if m:
            before, hit, after = esc[:m.start()], m.group(0), esc[m.end():]
            parts = []
            if before: parts.append(grey_span(before))
            parts.append(hit)
            if after: parts.append(grey_span(after))
            return "".join(parts)
    return grey_span(esc)

def build_front_back(card: WordCard) -> tuple[str, str]:
    label = front_label(card)
    
    tildes = get_rarity_tildes(card.word_frequency)
    
    # Собираем все "серые" элементы после слова
    grey_parts = []
    if card.transcription:
        grey_parts.append(html.escape(card.transcription))
    if tildes:
        grey_parts.append(tildes)
    
    if grey_parts:
        front_first = f"{label}&nbsp;{grey_span('&nbsp;'.join(grey_parts))}"
    else:
        front_first = label

    bold, plain, grey = [], [], []
    for e in card.entries:
        if e.grey: grey.append(grey_span(html.escape(e.text)))
        elif e.frequency and e.frequency > BOLD_FREQ_THRESHOLD: bold.append(f"<b>{html.escape(e.text)}</b>")
        else: plain.append(html.escape(e.text))
    
    back_line = ", ".join(bold + plain + grey)
    numbered = [e for e in card.entries if not e.grey and e.examples]
    
    fr_lines = [grey_span(f"{i}. ") + highlight_word(e.examples[0].en, card.word) for i, e in enumerate(numbered, 1)]
    bk_lines = [grey_span(f"{i}. ") + highlight_word(e.examples[0].ru, e.text, e.inflected) for i, e in enumerate(numbered, 1)]

    front_parts = [front_first] + fr_lines
    back_parts = [back_line] + bk_lines

    return "<br><br>".join(front_parts), "<br><br>".join(back_parts)

# --- Anki и CLI ---
def send_to_anki(front, back, deck, model, tags):
    payload = {"action": "addNote", "version": 6, "params": {"note": {"deckName": deck, "modelName": model, "fields": {"Front": front, "Back": back}, "options": {"allowDuplicate": False}, "tags": ["reverso", *tags]}}}
    resp = requests.post(ANKI_URL, json=payload, timeout=30)
    resp.raise_for_status()
    res = resp.json()
    if res.get("error"): raise RuntimeError(res["error"])
    return res

def process_word(word, args) -> bool:
    """Обрабатывает одно слово. Возвращает True при успехе."""
    log(f"[1/3] Получаем данные для «{word}»...")
    try:
        card = get_word_data(word, target_pos=args.pos or "", include_blocked=args.include_blocked)
    except Exception as e:
        log(f"Ошибка парсинга: {e}", "error")
        return False

    if not card.entries:
        log("Переводы не найдены.", "error")
        return False

    if args.pos:
        card.pos = args.pos

    if args.ai:
        log(f"[2/3] AI (Ollama: {args.ai_model})...")
        enrich_with_ai(card, args.examples, args.ai_model, args.ai_url, verify=not args.no_verify, ai_only=args.ai_only)

    front, back = build_front_back(card)
    
    if args.dry_run:
        print("=" * 60)
        print("FRONT:", front)
        print("-" * 60)
        print("BACK :", back)
        print("=" * 60)
        return True
    else:
        log("[3/3] Отправка в Anki...")
        try:
            res = send_to_anki(front, back, args.deck, args.model, [])
            log(f"✔ Добавлено (ID: {res['result']})")
            return True
        except Exception as e:
            log(f"✘ Ошибка Anki: {e}", "error")
            return False

def main():
    global _log_file
    ap = argparse.ArgumentParser()
    ap.add_argument("words", nargs="*")
    ap.add_argument("--file", help="txt файл со словами (по одному в строке)")
    ap.add_argument("--pos", help="Часть речи (noun, verb, adjective...)")
    ap.add_argument("--deck", default="Reverso EN-RU")
    ap.add_argument("--model", default="Простая", help="Тип заметки в Anki")
    ap.add_argument("--examples", type=int, default=1)
    ap.add_argument("--include-blocked", action="store_true")
    ap.add_argument("--ai", action="store_true")
    ap.add_argument("--ai-model", default=OLLAMA_DEFAULT_MODEL)
    ap.add_argument("--ai-url", default=OLLAMA_DEFAULT_URL)
    ap.add_argument("--ai-only", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--log", action="store_true")
    ap.add_argument("--log-file")
    args = ap.parse_args()

    if args.log_file:
        _log_file = Path(args.log_file).resolve()
        _log_file.parent.mkdir(parents=True, exist_ok=True)
        _log_file.write_text("", encoding="utf-8")

    words_to_process = []
    if args.file:
        try:
            with open(args.file, "r", encoding="utf-8") as f:
                for line in f:
                    w = line.strip()
                    if w and not w.startswith("#"):
                        words_to_process.append(w)
        except Exception as e:
            log(f"Ошибка чтения файла: {e}", "error")
            return
    for w in args.words:
        words_to_process.append(w)

    if not words_to_process:
        print("Укажите слово или файл со словами (--file).")
        return

    valid_pos = {"noun", "verb", "adjective", "adverb", "participle"}
    if args.pos and args.pos not in valid_pos:
        log(f"Ошибка: '{args.pos}' не является частью речи.", "error")
        return

    success_count = 0
    for i, word in enumerate(words_to_process, 1):
        log(f"\n--- Обработка {i}/{len(words_to_process)}: {word} ---")
        if process_word(word, args):
            success_count += 1
    
    log(f"\nГотово: {success_count} из {len(words_to_process)} обработано.")

if __name__ == "__main__":
    main()