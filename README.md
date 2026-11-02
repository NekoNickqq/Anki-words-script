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

## Структура карточки

* **Front**: английское слово.
* **Back**: нумерованный список всех переводов; под каждым — примеры
  (синим — английский оригинал, зелёным — русский перевод), плюс ссылка на источник.

## План развития (нейросеть)

`ai_summarize_translation()` — готовое место для LLM: выбор лучшего значения,
генерация подсказок, переформулирование примеров, Cloze-карточки и т.д.
 
