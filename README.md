# Telegram ИИ-ассистент

Личный бот-ассистент: расписание, задачи, цели, напоминания. Текст + голос, бесплатный ИИ (Google Gemini), бесплатный хостинг (Render) и БД (Neon).

## Возможности
- Пишите текстом или голосом: «Встреча завтра в 15:00», «Купить хлеб до вечера», «Цель — марафон до сентября».
- Gemini распознаёт намерение, бот показывает карточку подтверждения с inline-кнопками.
- Утренний дайджест дел на день (по умолчанию 07:00, настраивается).
- Напоминание за час до события отдельным сообщением (настраивается per-event).
- Меню: Сегодня / Задачи / Цели / Настройки (время дайджеста, таймзона).

## Деплой (бесплатно)

### 1. База данных — Neon
1. Зарегистрируйтесь на [neon.tech](https://neon.tech) (free plan).
2. Создайте проект, скопируйте connection string вида
   `postgresql://user:pass@ep-xxx.neon.tech/neondb?sslmode=require`.

### 2. Gemini API-ключ
1. Откройте [aistudio.google.com/apikey](https://aistudio.google.com/apikey).
2. «Create API key» — ключ бесплатный, карта не нужна.

### 3. Токен бота
У вас уже есть от @BotFather. Рекомендуется перевыпустить (вы пересылали его в чате):
@BotFather → `/mybots` → бот → API Token → Revoke.

### 4. Render
1. Запушьте репозиторий на GitHub.
2. На [render.com](https://render.com): New → Blueprint → выберите репозиторий
   (используется `render.yaml`).
3. Заполните env-переменные:
   - `BOT_TOKEN` — токен бота;
   - `GEMINI_API_KEY` — ключ Gemini;
   - `DATABASE_URL` — строка Neon;
   - `BASE_URL` — URL сервиса, например `https://tg-ai-assistant.onrender.com`
     (впишите после первого деплоя и передеплойте — при старте поставится webhook).
4. Free-план засыпает без трафика: добавьте на
   [cron-job.org](https://cron-job.org) бесплатное задание — GET `https://<ваш-url>/health`
   каждые 10 минут. Это держит сервис (и планировщик напоминаний) живым.

### 5. Проверка
- Откройте `https://<ваш-url>/health` → должно быть `{"status":"ok"}`.
- В Telegram: `/start` → меню. Отправьте голосовое или «Встреча завтра в 15:00».
- Для теста дайджеста: Настройки → выставите время на пару минут вперёд.

## Локальный запуск
```cmd
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
copy .env.example .env   :: заполните BOT_TOKEN, GEMINI_API_KEY, DATABASE_URL
set POLLING=1
python run_polling.py
```

## Архитектура
```
Telegram --webhook--> FastAPI+aiogram (Render Free) --> Gemini 2.5 Flash (STT + интент)
                              |                                  APScheduler (дайджест, T-60)
                              v
                        Neon Postgres (users, events, tasks, goals, reminder_log)
cron-job.org --пинг /health каждые 10 мин--> анти-sleep Render
```
