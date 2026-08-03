#!/usr/bin/env bash
# Обновление бота на сервере: забрать код с GitHub и перезапустить.
# Запускается автодеплоем из GitHub Actions и вручную: ./deploy.sh
set -euo pipefail

cd /home/ubuntu/lut-bot

echo "▸ Текущая версия: $(git log --oneline -1)"
git fetch origin main
git reset --hard origin/main          # сервер — копия GitHub, локальных правок тут нет
echo "▸ Новая версия:   $(git log --oneline -1)"

# Зависимости могли добавиться вместе с кодом
venv/bin/pip install -q -r requirements.txt

# Синтаксис проверяем до перезапуска: битый код не должен ронять живого бота
if ! venv/bin/python -m compileall -q bot.py config.py database.py handlers services keyboards; then
    echo "✗ Код не компилируется — перезапуск отменён, бот работает на старой версии"
    exit 1
fi

sudo systemctl restart lut-bot
sleep 5

if systemctl is-active --quiet lut-bot; then
    echo "✓ Бот перезапущен и работает"
else
    echo "✗ Бот не поднялся:"
    journalctl -u lut-bot -n 20 --no-pager
    exit 1
fi
