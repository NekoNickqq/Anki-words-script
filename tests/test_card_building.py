#!/usr/bin/env python3
"""Тесты сборки карточки (без сети): частоты, порог 2%, стили front/back, TSV."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from reverso_anki import (
    TranslationEntry, WordCard, Example, classify_entries, GREY_THRESHOLD_RATIO,
    build_front_back, build_tsv_record, highlight_word, parse_translations,
    parse_examples, examples_fragment_url, normalize_pos, front_label,
)

HERE = os.path.dirname(os.path.abspath(__file__))


def test_parse_translations_with_frequencies():
    html = open(os.path.join(HERE, "sample_translation.html"), encoding="utf-8").read()
    entries = parse_translations(html)
    assert [e.text for e in entries] == [
        "приверженность", "обязательство", "вовлеченность", "участие", "напор", "натиск", "преступление"
    ], entries
    assert entries[0].frequency == 10874
    assert entries[-1].frequency == 150


def test_grey_threshold_2_percent():
    # эталон 10000; порог 2% = 200: natisk 210 — остаётся, prestuplenie 150 и na... <=200 — серые
    entries = [
        TranslationEntry("давление", 10000),
        TranslationEntry("натиск", 210),
        TranslationEntry("прессинг", 200),   # ровно 2% -> серый (<=)
        TranslationEntry("стресс", 150),
    ]
    classify_entries(entries)
    assert not entries[0].grey
    assert not entries[1].grey
    assert entries[2].grey and entries[3].grey


def test_examples_fragment_url_anchor_encoded():
    url = examples_fragment_url("commitment", "приверженность")
    assert url.startswith("https://context.reverso.net/translation/english-russian/commitment#")
    assert "%D0%BF%D1%80%D0%B8%D0%B2%D0%B5%D1%80%D0%B6%D0%B5%D0%BD%D0%BD%D0%BE%D1%81%D1%82%D1%8C" in url


def test_highlight_word_unstyled_target():
    h = highlight_word("This is putting huge pressure on infrastructure of the city", "pressure")
    assert 'color: rgb(161, 173, 173);' in h
    # слово вне span без стиля: "...huge </span>pressure<span ..."
    assert "</span>pressure<span" in h, h


def test_parse_examples_from_page():
    html = open(os.path.join(HERE, "sample_examples.html"), encoding="utf-8").read()
    pairs = parse_examples(html, "приверженность", "commitment", limit=5)
    assert len(pairs) == 1
    assert "приверженность" in pairs[0].ru and "commitment" in pairs[0].en.lower()


def _make_card():
    entries = [
        TranslationEntry("приверженность", 10874, examples=[Example(
            en="The new government showed a strong commitment to reform.",
            ru="Новое правительство продемонстрировало сильную приверженность реформам.")]),
        TranslationEntry("обязательство", 5421, examples=[Example(
            en="Signing the treaty was a serious commitment.",
            ru="Подписание договора было серьёзным обязательством.")]),
        TranslationEntry("натиск", 216),
        TranslationEntry("преступление", 150),
    ]
    classify_entries(entries)
    return WordCard(word="commitment", url="u", pos="noun", transcription="/kəˈmɪtmənt/", entries=entries)


def test_build_front_back_style():
    card = _make_card()
    front, back = build_front_back(card)
    # front: the word + транскрипция серым сразу после
    assert front.startswith("the commitment<br><br><span style=\"color: rgb(161, 173, 173);\">/kəˈmɪtmənt/</span>")
    # серые нумерованные примеры с чистым словом
    assert "commitment</a>" not in front
    assert "</span>commitment<span" in front
    assert "1. " in front and "2. " in front
    # back: первый перевод жирным, второй обычным в списке, серые — в серых span
    assert back.startswith("<b>приверженность</b>, обязательство")
    assert '<span style="color: rgb(161, 173, 173);">натиск</span>' in back
    assert "</span>приверженность<span" in back  # слово в ru-примере без стиля
    # у серых переводов НЕТ примеров: только 2 пронумерованных блока на каждой стороне
    assert front.count("<br><br><span style=\"color: rgb(161, 173, 173);\">1. ") == 1
    assert "3. " not in front and "3. " not in back


def test_pos_labels_on_front():
    def label(pos):
        c = WordCard(word="x", url="", pos=pos)
        return front_label(c)
    assert label("noun") == "the x"
    assert label("verb") == "to x"
    assert label("adjective") == "x as adjective"
    assert label("adverb") == "x as adverb"
    assert label("participle") == "x as participle"
    assert normalize_pos("существительное") == "noun"
    assert normalize_pos("communion") == "participle"


def test_tsv_record_quotes_like_deck_format():
    card = _make_card()
    front, back = build_front_back(card)
    rec = build_tsv_record(front, back)
    assert rec.count("\t") == 1
    assert rec.startswith('"') and rec.endswith('"')
    assert '""' in rec  # двойные кавычки внутри (style="...") удвоены


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"PASS {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"\n{len(fns) - failed}/{len(fns)} тестов пройдено")
    sys.exit(1 if failed else 0)
