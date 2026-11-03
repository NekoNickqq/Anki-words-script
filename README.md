# reverso-anki (Anki-words-script)

Простой скрипт: берёт переводы EN→RU с [context.reverso.net](https://context.reverso.net/),
собирает карточку Anki и отправляет её в Anki через **AnkiConnect**.

## Как это работает

1. Скачивается страница `https://context.reverso.net/translation/english-russian/<слово>`.
2. Извлекаются **все** значения перевода (например, у `run` их ~7).
3. Для каждого значения на том же сайте ищутся примеры использования
   (`/examples/english-russian/<перевод>`) — пара EN/RU добавляется в карточку.
4. Карточка (HTML) отправляется в колоду Anki через AnkiConnect (`http://localhost:8765`, action `addNote`).
5. Заложена точка подключения нейросети (`--ai`): к каждому значению можно добавить
   короткое пояснение оттенка смысла от LLM (OpenAI-совместимый API).

## Установка

```bash
pip install requests beautifulsoup4 cloudscraper
```

В Anki установите аддон **AnkiConnect** (код 2055492159) и перезапустите Anki.

## Использование

```bash
python reverso_anki.py run                        # одно слово -> колода "Reverso EN-RU"
python reverso_anki.py run house --examples 3     # по 3 примера на каждое значение
python reverso_anki.py --file words.txt --deck "My Deck" --tags enru
python reverso_anki.py run --dry-run              # показать HTML карточки без отправки
python reverso_anki.py run --ai                   # добавить пояснения нейросети
```

Переменные окружения:

| Переменная        | Назначение                                               |
|-------------------|----------------------------------------------------------|
| `ANKICONNECT_URL` | адрес AnkiConnect (по умолчанию `http://localhost:8765`) |
| `OPENAI_API_KEY`  | ключ для AI-пояснений (флаг `--ai`)                      |
| `OPENAI_MODEL`    | модель (по умолчанию `gpt-4o-mini`)                       |
| `OPENAI_BASE_URL` | совместимый API (например, локальная модель/Ollama)       |
| `REVERSO_COOKIES` | cookies браузера, если Cloudflare блокирует запросы       |

## Если Reverso отдаёт «Just a moment…» (Cloudflare, HTTP 403)

Скрипт сам использует `cloudscraper`, но при сильной защите может понадобиться:

1. Открыть любую страницу reverso в обычном браузере (пройти проверку).
2. Скопировать cookies (F12 → Application → Cookies) и задать:
   ```bash
   export REVERSO_COOKIES='cf_clearance=...; other=...'
   ```
3. Либо запускать через браузерный рендеринг (Playwright/FlareSolverr) — при необходимости
   функция `fetch()` легко заменяется на такой источник HTML.

## Структура карточки (как в примерах колоды)

* **Front**: `the word` (сущ.) / `to word` (глагол) / `word as adjective|adverb|participle`,
  затем транскрипция с Reverso — всё маловажное в сером `rgb(161, 173, 173)`;
  далее нумерованные англо-примеры (серым, а изучаемое слово — без стиля).
* **Back**: переводы — самый частый **жирным**, остальные значимые обычным текстом,
  второстепенные (частота ≤ 2% от самой частотной) — просто серым списком **без примеров**;
  далее те же нумерованные примеры на русском (серым, перевод слова — без стиля).
* Часть речи задаётся флагом `--pos` (noun/verb/adjective/adverb/participle или по-русски:
  сущ/глагол/прил/нар/причастие) либо колонкой во файле слов (`слово<TAB>часть речи`);
  если не задана — скрипт пытается определить по странице Reverso.
* Пример для каждого перевода берётся со страницы вида
  `.../english-russian/<слово>#<перевод>` (пока самая первая пара EN/RU; все найденные пары
  сохраняются и могут быть отданы нейросети через `--ai`).
* Экспорт в формате импорта Anki: `--out file.txt` пишет `#separator:tab`, `#html:true` и TSV-записи.

## План развития (нейросеть)

`ai_pick_best_example()` — готовое место для LLM: из всех пар примеров выбрать лучшую
или переписать некорректный перевод. Пока флаг `--ai` выключен — берётся первая пара.
 

## Виртуальное окружение (venv)

Venv не обязателен, но **настоятельно рекомендуется**, чтобы не засорять системный Python.
В Windows есть нюанс: в Anaconda/Microsoft Store Python `pip` без venv может ставить пакеты
в окружениеconda, а не в тот интерпретатор, которым вы запускаете скрипт.

```bash
# 1) создать venv (выполнять из папки проекта)
python -m venv .venv            # Windows: py -m venv .venv

# 2) активировать
#    Windows PowerShell :  .venv\Scripts\Activate.ps1
#    Windows cmd        :  .venv\Scripts\activate.bat
#    Linux/macOS        :  source .venv/bin/activate

# 3) установить зависимости (после активации pip уже указывает в venv)
pip install -r requirements.txt

# 4) проверить, что всё установлено в venv
python -c "import requests, bs4, cloudscraper; print('OK')"
```

## Как проверить, скачивается ли страница Reverso

Встроенная диагностика — флаг `--check`:

```bash
python reverso_anki.py --check run
```

Он по очереди пробует обычный `requests`, затем `cloudscraper`
(и `cloudscraper + REVERSO_COOKIES`, если переменная задана), печатает HTTP-статусы
и вердикт: «страница скачивается» или «заблокировано Cloudflare» с инструкцией.

Быстрая проверка одной командой без скрипта:

```bash
curl -sI https://context.reverso.net/translation/english-russian/run | head -5
# 200 OK   — страница отдаётся
# 403/503 + «Just a moment» — блокировка Cloudflare
```
