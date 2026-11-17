"""Генерация интерактивных обучающих конспектов (LiaScript).

Нейронка превращает конспект лекции в интерактивный курс:
- квизы (выбор ответа, ввод текста)
- флеш-карточки
- самопроверка после каждого раздела

Результат: course.md в LiaScript-диалекте + course.html (через CDN-рендер).
"""
from __future__ import annotations

import logging
from pathlib import Path

from .client import ai

log = logging.getLogger("konspect.quiz")

PROMPT = """Ты — методист-преподаватель. Преврати конспект лекции в ИНТЕРАКТИВНЫЙ
обучающий курс на языке LiaScript (диалект Markdown).

ТРЕБОВАНИЯ:
1. Начни с заголовка курса: `# {title}` и комментария `<!-- data-type="course" -->`
2. Разбей материал на логические разделы `## Раздел`
3. ПОСЛЕ КАЖДОГО РАЗДЕЛА добавь квиз LiaScript:

```
<section class="quiz" data-title="Проверь себя">
[( )] вариант 1
[( )] вариант 2
[(X)] правильный вариант
[[?]]
</section>
```

или текстовый квиз:
```
[[_Раздел базы данных называется ___]]
[[база данных]]
```

4. Добавь 5-8 флеш-карточек в конце (карточка = вопрос + скрытый ответ):
```
## Флеш-карточки
**Вопрос?**
{{решение — краткий ответ}}
```

5. Добавь "ключевые выводы" в конце
6. Сохрани ВСЮ фактуру из конспекта: определения, цифры, термины, цитаты
7. Пиши по-русски, понятным языком

КОНСПЕКТ:
{conspect}

Выведи ТОЛЬКО готовый LiaScript-документ, без пояснений."""


LIA_HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title>
<style>
body {{ margin: 0; }}
</style>
<script src="https://cdn.jsdelivr.net/gh/liaScript/liaScript@master/dist/lia.min.js"></script>
</head>
<body>
<lia-doc>
{md}
</lia-doc>
</body>
</html>"""


async def generate_interactive(conspect_path: Path, title: str) -> dict:
    """Генерирует интерактивный курс из конспекта."""
    md_text = conspect_path.read_text(encoding="utf-8")
    prompt = PROMPT.format(conspect=md_text[:30000], title=title)

    answer = await ai.chat([{"role": "user", "content": prompt}])
    if not answer:
        return {"error": "LLM не ответил"}

    course_md = answer.strip()
    if course_md.startswith("```"):
        course_md = course_md.strip("`").lstrip("markdown\n").lstrip()

    # сохраняем LiaScript-версию
    out_dir = conspect_path.parent
    course_path = out_dir / "course.md"
    course_path.write_text(course_md, encoding="utf-8")

    # HTML-обёртка с CDN-рендером
    html = LIA_HTML.format(title=title, md=course_md)
    html_path = out_dir / "course.html"
    html_path.write_text(html, encoding="utf-8")

    return {
        "course_md": str(course_path),
        "course_html": str(html_path),
        "title": title,
    }
