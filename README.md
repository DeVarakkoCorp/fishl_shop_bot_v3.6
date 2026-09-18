# Fishl Shop Bot — Railway Volume + GitHub backups

## Что изменено

- Новые заказы и скидки хранятся в Railway Volume, а не в эфемерной файловой системе контейнера.
- При первом запуске бот переносит все `orders*.db` из репозитория в Volume, если таких файлов там ещё нет.
- После этого GitHub-файлы не перезаписывают рабочую БД при каждом деплое.
- «Мои заказы» и «Все заказы» читают все `orders*.db` из Volume.
- Добавлен автоматический GitHub-бэкап `orders.db`.
- Бэкапы записываются в отдельную ветку `backups`, чтобы коммиты бэкапов не запускали новый Railway-деплой, если Railway слушает основную ветку.
- Добавлены ручной бэкап через кнопку `💾 Сделать бэкап` в панели менеджера и команда `/backup`.

## Railway Volume

Создай Volume и подключи его к сервису с Mount Path:

`/app/data`

Railway автоматически передаст `RAILWAY_VOLUME_MOUNT_PATH`; вручную эту переменную задавать не нужно.

## Railway Variables

Уже используемые:

- `BOT_TOKEN`
- `MANAGER_USERNAME`
- `ADMIN_CHAT_ID`

Для GitHub-бэкапов добавь:

- `GITHUB_TOKEN` — Fine-grained Personal Access Token с `Contents: Read and write` для репозитория.
- `GITHUB_REPO` — например `username/fishl-shop-bot`.
- `GITHUB_BACKUP_BRANCH` — `backups`
- `GITHUB_BACKUP_PATH` — `backups/orders`
- `GITHUB_BACKUP_INTERVAL_MINUTES` — `360` (6 часов)

Токен GitHub не добавляй в `config.py` и не коммить в репозиторий.

## Важно про ветки

Railway должен быть подключён к основной ветке (`main` или другой выбранной ветке с кодом). Ветка `backups` предназначена только для резервных копий.

Каждый автоматический бэкап создаёт новый файл вида:

`backups/orders/orders_2026-09-18_12-00-00.db`

Поэтому история резервных копий сохраняется.

## Ручной бэкап

Менеджеру доступны:

- кнопка `💾 Сделать бэкап` в панели менеджера;
- команда `/backup`.

## Start Command

`python bot.py`
