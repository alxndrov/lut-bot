#!/bin/bash
# Запускается один раз на чистом Ubuntu/Debian VPS
# Использование: bash setup.sh

set -e  # Стоп при любой ошибке

REPO_DIR="/root/lut-bot"
SERVICE_NAME="lut-bot"

echo "=== [1/6] Обновление системы ==="
apt-get update -y && apt-get install -y python3 python3-pip python3-venv git

echo "=== [2/6] Клонирование репозитория ==="
# Замени ссылку на свой GitHub-репо
read -p "Вставь ссылку на GitHub-репозиторий (https://github.com/...): " REPO_URL
git clone "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"

echo "=== [3/6] Создание виртуального окружения ==="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== [4/6] Настройка .env ==="
echo "Открываю .env для заполнения..."
cp .env.example .env
nano .env

echo "=== [5/6] Установка systemd-сервиса ==="
cp lut-bot.service /etc/systemd/system/
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl start "$SERVICE_NAME"

echo ""
echo "=== [6/6] Готово! ==="
echo "Статус бота:"
systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Полезные команды:"
echo "  Логи в реальном времени:  journalctl -u $SERVICE_NAME -f"
echo "  Перезапустить бота:       systemctl restart $SERVICE_NAME"
echo "  Остановить:               systemctl stop $SERVICE_NAME"
echo "  Редактировать бота:       cd $REPO_DIR && nano handlers/admin.py"
