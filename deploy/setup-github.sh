#!/usr/bin/env bash
# Один раз подключает /opt/vktg к репозиторию GitHub, чтобы владелец мог обновлять бота
# кнопкой в Telegram (/update). .env, bridge.db и .venv не трогаются: они в .gitignore.
# Запуск от root: bash setup-github.sh [адрес репозитория]
set -euo pipefail

REPO_URL="${1:-https://github.com/d9souljaM/VKtoTG.git}"
APP_DIR="${APP_DIR:-/opt/vktg}"
APP_USER=vktg

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0"
  exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Не найден $APP_DIR/.env — сначала установите бота (deploy/install.sh)."
  exit 1
fi

echo "==> Устанавливаю git"
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git >/dev/null

# git запускаем от пользователя бота: потом он сам будет делать git pull по кнопке.
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
as_app() { runuser -u "$APP_USER" -- env HOME="$APP_DIR" "$@"; }

echo "==> Подключаю $REPO_URL"
cd "$APP_DIR"
if [[ -d .git ]]; then
  as_app git remote set-url origin "$REPO_URL"
else
  as_app git init -q -b main
  as_app git remote add origin "$REPO_URL"
fi
as_app git fetch -q origin main
as_app git checkout -q -f -B main --track origin/main
echo "    Версия: $(as_app git log -1 --format='%h %s')"

bash "$APP_DIR/deploy/install.sh"
