# Name2Email Gmail Client

Автоматизація Name2Email у Gmail через підключення до вже відкритого Chrome (`--remote-debugging-port=9222`).

## 1) Підготовка

1. Запустіть Chrome з портом відладки:

```bash
/Applications/Google\ Chrome.app/Contents/MacOS/Google\ Chrome --remote-debugging-port=9222
```

2. Увійдіть у Gmail у цьому ж Chrome і переконайтесь, що розширення Name2Email активне.

3. Встановіть залежності:

```bash
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium
```

## 2) Вхідні дані

Створіть `Input.csv`. Скрипт бере **другу колонку** кожного рядка як запит.

Приклад:

```csv
id,query
1,Ivan Ivanov @company.com
2,Artem Palieiev @google.com
```

## 3) Запуск

```bash
python3 gmail_name2email_client.py
```

### Важливо про логін Name2Email

- Скрипт може автоматично підняти Chrome з `--remote-debugging-port=9222`.
- Якщо в цьому профілі Name2Email ще не авторизований, скрипт поставить паузу і попросить залогінитись, потім натиснути Enter у терміналі.
- Для стабільної роботи використовуйте один і той самий профіль:

```bash
python3 gmail_name2email_client.py --chrome-profile-dir "$HOME/chrome-debug-name2email"
```

Результати пишуться у `Output_With_Emails.csv`:
- `query`
- `email`
- `status` (`found` або `not_found`)
- `processed_at`

## 4) Особливості

- Підключення до існуючого Chrome через CDP (`127.0.0.1:9222`)
- Пошук Gmail-вкладки або відкриття нової
- Автоматичний Compose
- Введення з людською затримкою
- Space-trigger для активації Name2Email
- Polling до 15 секунд
- Автозбереження кожні 10 рядків
- Resume: наступний запуск продовжує з **(N+1)-го** запиту в `Input.csv`, де **N** — кількість рядків із непорожнім `query` у `Output_With_Emails.csv` (порядок той самий, що в Input)
- Коректне завершення з `Ctrl+C` зі збереженням
