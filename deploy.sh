#!/usr/bin/env bash
# Перезапуск бота после правок. Код пишется на компьютере и приезжает сюда
# через `git push server main`: git сам обновляет файлы, а хук
# .git/hooks/post-receive запускает этот скрипт и дублирует код на GitHub.
# Код на сервере руками не правим — иначе push откажется затирать правки.
# Ручной запуск (если нужно просто перезапустить): bash deploy.sh
set -euo pipefail

cd /home/ubuntu/lut-bot

echo "▸ Версия: $(git log --oneline -1)"
if [ -n "$(git status --porcelain)" ]; then
    echo "⚠ Есть незакоммиченные правки — бот запустится с ними"
fi

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
