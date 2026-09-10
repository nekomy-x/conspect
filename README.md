# bbb-lectures 🎓

**Пайплайн «запись BigBlueButton → красивый конспект».**

Скачивает запись лекции с BBB (аудио + слайды), распознаёт речь через Whisper, передаёт всё нейросети (GPT-4o) — и на выходе получаются структурированные конспекты: Markdown для Obsidian + красивый HTML-сайт на MkDocs Material.

## Что делает

```
BBB URL → Скачать ассеты → Транскрипция (Whisper) → Нейросеть (GPT-4o) → Конспект
                                                                       ├─ Markdown (Obsidian)
                                                                       └─ Сайт (MkDocs Material)
```

### Функции
- **Скачивание** ассетов BBB: metadata, слайды (SVG+текст), видео, аудио
- **Транскрипция**: mlx-whisper (Apple Silicon GPU) или faster-whisper (CPU)
- **Генерация конспекта**: GPT-4o с интуитивным стилем (интуиция → формула → пример)
- **Три формата вывода**: Obsidian Markdown, MkDocs Material сайт, PDF (опционально)
- **Автоматическая обработка**: watcher следит за очередью ссылок

## Быстрый старт

### 1. Установка

```bash
git clone <url> && cd bbb-lectures
python -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

### 2. Конфигурация

```bash
cp .env.example .env
# Отредактируйте .env: вставьте ключ OpenAI
```

Нужен API-ключ OpenAI (или совместимого провайдера):
- [platform.openai.com/api-keys](https://platform.openai.com/api-keys)

### 3. Запуск

```bash
# Обработать одну лекцию
python main.py "https://bbb.edu.vsu.ru/playback/presentation/2.3/..."

# Пересобрать сайт
python main.py --build-site

# Добавить в очередь
echo "https://bbb.edu.vsu.ru/playback/..." >> queue.txt

# Автообработка очереди
python watch.py --once      # обработать и выйти
python watch.py             # демон: проверяет каждые 60 сек
```

### 4. Просмотр результатов

```bash
# Obsidian / Typora / VS Code — откройте папку vault/
# Сайт — откройте в браузере:
python -m http.server -d site 8000
# Затем: http://localhost:8000
```

## Структура проекта

```
bbb-lectures/
├── main.py           # CLI
├── watch.py          # Watcher (очередь ссылок)
├── config.py         # Конфигурация
├── fetch.py          # Скачивание ассетов BBB
├── extract.py        # ffmpeg + rsvg-convert + таймлайны
├── transcribe.py     # mlx-whisper транскрипция
├── align.py          # Привязка транскрипта к слайдам
├── summarize.py      # Генерация конспекта (LLM)
├── notes.py          # Форматирование Markdown
├── site.py           # MkDocs Material сайт
├── prompts/          # Промпты для LLM
├── docs/             # Конспекты для сайта (auto-generated)
├── vault/            # Markdown-конспекты (Obsidian)
├── .env.example      # Шаблон конфигурации
└── queue.txt         # Очередь ссылок
```

## Без LLM

Если `LLM_API_KEY` не задан — скрипт сгенерирует «сырой» конспект:
структуру слайдов с текстами и транскриптом, но без красивой обработки нейросетью.

## Moodle-ссылки (edu.vsu.ru)

Ссылки на `edu.vsu.ru/mod/bigbluebuttonbn/` требуют авторизации.
Для них нужно экспортировать куки браузера в формате Netscape:

1. Установите расширение [Get cookies.txt LOCALLY](https://chrome.google.com/webstore/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
2. Откройте `edu.vsu.ru` в браузере (войдите)
3. Экспортируйте куки: правый клик → «Get cookies.txt LOCALLY»
4. Сохраните как `cookies.txt` в корне проекта
5. Укажите в `.env`: `MOODLE_COOKIES=./cookies.txt`

## Системные требования

- Python 3.10+
- ffmpeg (аудио)
- rsvg-convert (SVG → PNG слайды)
- 7+ ГБ свободного места (модель Whisper medium = 1.5 ГБ)
- Apple Silicon GPU — рекомендуется для быстрой транскрипции
