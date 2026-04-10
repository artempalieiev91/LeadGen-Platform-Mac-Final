# LeadGen Platform

Streamlit-застосунок: Research Validation, Sheets Preparation, MathcURLs, Name2Emails (локально на macOS).

## Швидкий старт (термінал)

```bash
cd "/шлях/до/Streamlit Platform"
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
playwright install chromium
cp .streamlit/secrets.toml.example .streamlit/secrets.toml
# Відредагуйте secrets.toml: [auth] username / password (обов’язково)
streamlit run streamlit_app.py
```

Відкриється браузер (за замовчуванням `http://localhost:8501`).

## PyCharm

1. **File → Open** — виберіть папку проєкту `Streamlit Platform`.
2. **Settings → Project → Python Interpreter** — додайте `.venv` (або створіть venv і виконайте `pip install -r requirements.txt` у терміналі PyCharm).
3. **Run → Edit Configurations → + → Python**:
   - **Script path:** `run_streamlit.py` (у корені проєкту).
   - **Working directory:** корінь проєкту (`Streamlit Platform`).
4. Запуск: **Run** (▶) або **Shift+F10**.

Альтернатива без скрипта: **Module name** `streamlit`, **Parameters** `run streamlit_app.py`, режим **Run with Python module**.

У репозиторії є готова конфігурація **Streamlit LeadGen**: `.idea/runConfigurations/Streamlit_LeadGen.xml` — після відкриття проєкту виберіть її у списку Run і призначте інтерпретатор з `.venv`.

## Секрети

- Локально: файл `.streamlit/secrets.toml` (не комітиться). Приклад — `.streamlit/secrets.toml.example`.
- Потрібна секція **`[auth]`** з `username` і `password`, інакше вхід заблоковано.
- Для Telegram-сповіщень — `[telegram]` з `bot_token` (див. приклад).

## Name2Emails

Працює лише на **macOS** з локальним Chrome; на типовому хмарному Linux-хостингу вкладка свідомо обмежена.
