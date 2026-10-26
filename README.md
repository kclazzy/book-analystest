# BookMind Local

Локальное приложение для анализа книг и серий через Ollama и модели Qwen (без облаков и платных API).

**Статус: этап 1 из 9.** Готово: каркас, конфигурация, `OllamaClient`, каталог нескольких моделей Qwen, установка и обновление моделей, тесты без сети. Импорт книг, SQLite, анализ и GUI — следующие этапы.

## Установка (Windows 11, PowerShell)

```powershell
git clone https://github.com/kclazzy/book-analys.git W:\BookMindLocal
cd W:\BookMindLocal
py -3.12 -m venv .venv
.venv\Scripts\Activate.ps1
pip install -e ".[dev]"      # позже: ".[gui,dev]"
pytest
```

## Модели Qwen

Каталог кандидатов лежит в `src/bookmind/data/models_catalog.json` (Qwen3.5, Qwen3, Qwen2.5). Наличие тега в реестре Ollama не предполагается: проверяется при установке. Облачные теги (`*cloud`) заблокированы.

```powershell
python app.py status                 # каталог + что установлено
python app.py recommend --vram 12    # что оценочно влезет в видеопамять
python app.py install qwen3.5:9b-q4_K_M
python app.py install qwen3:8b       # можно держать несколько моделей
python app.py select qwen3.5:9b-q4_K_M
python app.py check                  # есть ли обновления (обращается к registry.ollama.ai, передаются только имена моделей)
python app.py update qwen3.5:9b-q4_K_M
python app.py update-all
python app.py catalog-update my_catalog.json   # добавить новые модели без правки кода
```

Обновление модели = повторный `pull`: Ollama докачивает только изменившиеся слои. При недоступности выбранной модели берутся `fallback_models` из `data/settings.json`, затем модели из каталога.

## Каталог данных
`BOOKMIND_DATA_DIR` задаёт каталог настроек, базы и логов отдельно от кода (по умолчанию `./data`).

## Тесты
`pytest` — без Ollama и сети (подменный сервер). `pytest -m integration` — с реальным Ollama, если запущен.
