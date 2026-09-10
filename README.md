# DownloadBot

Личный Telegram-бот: присылаете публичную ссылку, выбираете качество, получаете файл.

Работает только с **открытыми** роликами. Приватные аккаунты, закрытые видео и контент, для которого нужен логин, бот не скачивает — это сделано специально.

Скачивайте только то, на что у вас есть право. Правила YouTube, Instagram и других площадок обычно запрещают выгрузку чужого контента.

## Что умеет

- принимает ссылку в чат
- показывает доступные качества (360p, 720p, 1080p и т.д., плюс «только аудио»)
- скачивает выбранный вариант и отправляет файл в Telegram
- отказывается от приватного / закрытого контента
- не использует cookies и сохранённые сессии
- **не качает плейлисты и сериалы целиком** — только одно видео за раз

## Почему «нельзя больше 50 МБ»

Бот спокойно **скачивает** большой файл на сервер. Упирается отправка **в Telegram**.

| Как бот говорит с Telegram | Максимум файла |
|---|---|
| Облачный Bot API (`api.telegram.org`) | **~50 МБ** |
| Свой локальный Bot API на сервере | **до 2 ГБ** |

Телефон тут ни при чём: лимит на стороне Telegram, когда бот *загружает* файл в чат. Гигабайтный ролик можно отправить, если поднять локальный Bot API (ниже). Целый сезон одной кнопкой бот не заберёт — присылайте серию по одной ссылке, и только если у вас есть право на этот контент.

## Зачем ffmpeg

Пользователям на телефоне он **не нужен**. Телефон только получает готовый файл в Telegram.

ffmpeg ставится на **машину, где крутится бот** (ваш Mac или сервер в Docker). YouTube и похожие площадки часто отдают 720p/1080p двумя потоками: картинка отдельно, звук отдельно. Бот склеивает их в один mp4, который уже можно отправить в чат. Без ffmpeg обычно остаётся только ~360p.

В Docker-образе ffmpeg уже установлен.

## Требования (локальный запуск)

- Python 3.11+
- токен бота от [@BotFather](https://t.me/BotFather)
- ffmpeg — только если запускаете без Docker (`brew install ffmpeg`)

## Запуск локально (до ~50 МБ)

```bash
cd DownloadBot
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

В `.env` укажите токен:

```
BOT_TOKEN=123456:ABC...
```

По желанию ограничьте бота своим Telegram ID (узнать можно у [@userinfobot](https://t.me/userinfobot)):

```
ALLOWED_USER_IDS=123456789
```

```bash
python -m bot
```

Напишите боту `/start`, затем отправьте ссылку на публичное видео.

## Docker (сервер Ubuntu, до ~50 МБ)

Colima на сервере не нужен. `TELEGRAM_API_URL=http://127.0.0.1:8081` из `.env` с ноутбука **ломает** контейнер: localhost внутри бота — это он сам.

Обычный запуск (облако Telegram, лимит ~50 МБ):

```bash
# TELEGRAM_API_URL оставьте пустым
docker compose up -d --build
```

До 2 ГБ — два файла compose и ключи с my.telegram.org:

```bash
docker compose -f docker-compose.yml -f docker-compose.large.yml up -d --build
```

На части машин команда с дефисом: `docker-compose`.

Остановка: `docker compose down`.

## Логи

Бот пишет сразу в два места:

1. **stdout контейнера** — смотрите через Docker
2. **файл** `/app/logs/bot.log` внутри контейнера (крутится, до ~5×5 МБ)

```bash
# живой хвост обоих сервисов
docker compose logs -f

# только бот
docker compose logs -f bot

# только локальный Bot API (если поднят large-стек)
docker compose logs -f telegram-bot-api

# зайти в контейнер и читать файл
docker compose exec bot tail -n 100 -f /app/logs/bot.log
```

Локально без Docker логи лежат в `./logs/bot.log`.

Токен бота в логах затирается. Уровень: `LOG_LEVEL=DEBUG` в `.env`, если нужно больше деталей.

## Файлы больше 50 МБ (локальный Bot API)

`MAX_FILE_MB=2000` в `.env` **не помогает**: облако `api.telegram.org` всё равно отвечает `Request Entity Too Large`. Нужен свой Bot API на вашей машине.

### Пока бот запускаете через `python -m bot`

1. Создайте приложение на [my.telegram.org](https://my.telegram.org) → `api_id` и `api_hash`.
2. В `.env`:

```
TELEGRAM_API_ID=12345
TELEGRAM_API_HASH=your_api_hash
TELEGRAM_API_URL=http://127.0.0.1:8081
MAX_FILE_MB=1900
```

3. Запустите API и бота. На этом Mac Docker — это Colima, команда с дефисом: `docker-compose`.

```bash
colima start
docker-compose -f docker-compose.api.yml up -d
python -m bot
```

Перезапустите бота после правки `.env`.

### Весь стек в Docker (сервер)

```bash
docker compose -f docker-compose.yml -f docker-compose.large.yml up -d --build
```

На сервере должно быть свободное место. Заливка 200 МБ–1 ГБ в Telegram может занять несколько минут.

Остановка API: `docker compose -f docker-compose.api.yml down`.
Остановка полного стека: `docker compose -f docker-compose.yml -f docker-compose.large.yml down`.

## Поведение при ошибках

| Ситуация | Что сделает бот |
|---|---|
| Приватный аккаунт / закрытый ролик | Сообщит, что контент недоступен публично |
| Файл больше лимита Telegram | Предложит качество ниже или локальный Bot API |
| Нет ffmpeg на сервере бота | Часто скачается только 360p |
| Плейлист / сезон сериала | Не качает целиком — нужна ссылка на одно видео |

## Обновление загрузчика

Площадки часто меняют выдачу потоков. Если публичные ссылки вдруг перестали открываться:

```bash
pip install -U yt-dlp
```

В Docker после обновления `requirements.txt`:

```bash
docker compose up -d --build
```
