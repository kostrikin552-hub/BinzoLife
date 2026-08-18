# ⛽ BinzoLife

Telegram-бот для выбора оптимальной АЗС.

## Локальный запуск
1. Установите зависимости: `pip install -r requirements.txt`
2. Скопируйте `.env.example` в `.env` и заполните.
3. Запустите: `python -m main`

## Деплой на Render
- Создайте Web Service, укажите команду запуска `python -m main`.
- Добавьте переменные окружения.
- Настройте cron для пинга и вызова задач.

## Импорт данных
Используйте команду `/import_csv` и пришлите CSV с колонками: city,name,brand,address,lat,lon,price,status.
