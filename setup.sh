#!/bin/bash
# Запускается один раз на чистом Ubuntu/Debian VPS
# Использование: bash setup.sh
# Работает как с root, так и с обычным пользователем (ubuntu и т.д.)

set -e  # Стоп при любой ошибке

CURRENT_USER="$(whoami)"
REPO_DIR="$HOME/lut-bot"
SERVICE_NAME="lut-bot"

echo "=== Запуск от пользователя: $CURRENT_USER ==="
echo "=== Папка проекта: $REPO_DIR ==="
echo ""

echo "=== [1/6] Обновление системы ==="
sudo apt-get update -y && sudo apt-get install -y python3 python3-pip python3-venv git

echo "=== [2/6] Клонирование репозитория ==="
read -p "Вставь ссылку на GitHub-репозиторий (https://github.com/...): " REPO_URL
git clone "$REPO_URL" "$REPO_DIR"
cd "$REPO_DIR"

echo "=== [3/6] Создание виртуального окружения ==="
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt

echo "=== [4/6] Настройка .env ==="
cp .env.example .env
nano .env

echo "=== [5/6] Установка systemd-сервиса ==="
# Подставляем реального пользователя и путь в service-файл
sed -e "s|User=root|User=$CURRENT_USER|g" \
    -e "s|/root/lut-bot|$REPO_DIR|g" \
    lut-bot.service | sudo tee /etc/systemd/system/$SERVICE_NAME.service > /dev/null

sudo systemctl daemon-reload
sudo systemctl enable "$SERVICE_NAME"
sudo systemctl start "$SERVICE_NAME"

echo ""
echo "=== [6/6] Готово! ==="
sudo systemctl status "$SERVICE_NAME" --no-pager

echo ""
echo "Полезные команды:"
echo "  Логи в реальном времени:  sudo journalctl -u $SERVICE_NAME -f"
echo "  Перезапустить бота:       sudo systemctl restart $SERVICE_NAME"
echo "  Остановить:               sudo systemctl stop $SERVICE_NAME"
echo "  Редактировать бота:       cd $REPO_DIR && nano handlers/admin.py"
echo "  Быстрое управление:       bash $REPO_DIR/bot.sh"
