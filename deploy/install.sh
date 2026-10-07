#!/usr/bin/env bash
# Установка (и обновление) моста VK ↔ Telegram на сервере Debian/Ubuntu как службы systemd.
# Запуск от root из распакованной папки: bash /opt/vktg/deploy/install.sh
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SERVICE=vktg
APP_USER=vktg

if [[ $EUID -ne 0 ]]; then
  echo "Запустите от root: sudo bash $0"
  exit 1
fi
if [[ ! -f "$APP_DIR/.env" ]]; then
  echo "Нет файла $APP_DIR/.env — скопируйте его с компьютера вместе с кодом."
  exit 1
fi
if ! command -v apt-get >/dev/null; then
  echo "Скрипт рассчитан на Debian/Ubuntu (apt). На другой системе поставьте Python 3.10+ вручную."
  exit 1
fi

echo "==> Устанавливаю Python"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3 python3-venv curl git >/dev/null
if ! python3 -c 'import sys; sys.exit(sys.version_info < (3, 10))'; then
  echo "Нужен Python 3.10 или новее, а установлен $(python3 --version)."
  exit 1
fi
echo "    $(python3 --version)"

echo "==> Проверяю доступ к Telegram и VK"
if curl -s -o /dev/null -m 10 https://api.telegram.org; then
  echo "    Telegram доступен напрямую"
  if grep -q '^TG_PROXY=' "$APP_DIR/.env"; then
    sed -i 's/^TG_PROXY=/# TG_PROXY=/' "$APP_DIR/.env"
    echo "    TG_PROXY в .env отключён: на сервере прокси не нужен"
  fi
else
  echo "    ВНИМАНИЕ: api.telegram.org недоступен с этого сервера."
  echo "    Нужен сервер за рубежом или прокси в TG_PROXY."
fi
if curl -s -o /dev/null -m 10 https://api.vk.com; then
  echo "    VK доступен"
else
  echo "    ВНИМАНИЕ: api.vk.com недоступен с этого сервера."
fi

echo "==> Ставлю зависимости"
id -u "$APP_USER" >/dev/null 2>&1 || useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install -q --disable-pip-version-check -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR"
chmod 600 "$APP_DIR/.env"

echo "==> Настраиваю автозапуск"
cat > "/etc/systemd/system/$SERVICE.service" <<EOF
[Unit]
Description=VK <-> Telegram bridge bot
After=network-online.target
Wants=network-online.target

[Service]
User=$APP_USER
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/.venv/bin/python -m vktg
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable "$SERVICE" >/dev/null 2>&1
systemctl restart "$SERVICE"

sleep 8
echo
echo "==> Последние строки лога:"
journalctl -u "$SERVICE" -n 15 --no-pager -o cat
echo
if systemctl is-active --quiet "$SERVICE"; then
  echo "Готово: бот работает и будет запускаться сам после перезагрузки сервера."
  echo "Лог в реальном времени: journalctl -u $SERVICE -f"
else
  echo "Бот не запустился — пришлите строки лога выше."
  exit 1
fi
