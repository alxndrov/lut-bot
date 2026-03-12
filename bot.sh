#!/bin/bash
# Управление ботом на сервере
# Использование: bash bot.sh [команда]
#   bash bot.sh start    — запустить
#   bash bot.sh stop     — остановить
#   bash bot.sh restart  — перезапустить
#   bash bot.sh logs     — логи в реальном времени
#   bash bot.sh status   — статус
#   bash bot.sh update   — git pull + перезапуск

SERVICE="lut-bot"
DIR="/root/lut-bot"

case "$1" in
  start)
    systemctl start $SERVICE && echo "✅ Бот запущен."
    ;;
  stop)
    systemctl stop $SERVICE && echo "⛔ Бот остановлен."
    ;;
  restart)
    systemctl restart $SERVICE && echo "🔄 Бот перезапущен."
    ;;
  logs)
    journalctl -u $SERVICE -f --no-pager
    ;;
  status)
    systemctl status $SERVICE --no-pager
    ;;
  update)
    echo "📥 Получаю обновления из GitHub..."
    cd $DIR && git pull
    echo "🔄 Перезапускаю бота..."
    systemctl restart $SERVICE
    echo "✅ Готово."
    systemctl status $SERVICE --no-pager
    ;;
  *)
    echo "Использование: bash bot.sh {start|stop|restart|logs|status|update}"
    ;;
esac
