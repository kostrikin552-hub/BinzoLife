import matplotlib
matplotlib.use('Agg')  # для работы без GUI

import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import io
import logging
from datetime import datetime, timedelta, timezone
from typing import Optional, List, Tuple

from database.session import AsyncSessionLocal
from database.crud import get_price_history
from database.models import FuelType

logger = logging.getLogger(__name__)

# Устанавливаем стиль seaborn (если доступен)
try:
    import seaborn as sns
    sns.set_style("whitegrid")
    sns.set_palette("husl")
    USE_SEABORN = True
except ImportError:
    USE_SEABORN = False
    # fallback стиль
    plt.style.use('seaborn-v0_8-darkgrid' if 'seaborn-v0_8-darkgrid' in plt.style.available else 'default')

# Цветовая схема
COLOR_MAIN = '#2E86AB'      # синий
COLOR_AVG = '#D95D39'       # оранжевый
COLOR_MIN = '#2CA02C'       # зелёный
COLOR_MAX = '#D62728'       # красный
COLOR_GRID = '#E0E0E0'

async def generate_price_graph(station_id: int, fuel_type: FuelType, days: int = 30) -> Optional[bytes]:
    """
    Генерирует профессиональный график цены на топливо.
    Возвращает bytes изображения PNG или None при ошибке.
    """
    logger.info(f"Генерация графика для station_id={station_id}, fuel_type={fuel_type}, days={days}")
    async with AsyncSessionLocal() as db:
        history = await get_price_history(db, station_id, fuel_type, days)
        if not history:
            logger.warning(f"Нет данных для station_id={station_id} за {days} дней")
            return None

        # Сортируем по времени
        history.sort(key=lambda x: x.recorded_at)
        dates = [h.recorded_at for h in history]
        prices = [h.price for h in history]

        # Фильтруем аномалии (цены вне разумного диапазона)
        filtered = [(d, p) for d, p in zip(dates, prices) if 0 < p < 200]
        if not filtered:
            logger.warning(f"Все цены аномальны для station_id={station_id}")
            return None
        dates, prices = zip(*filtered)

        # Вычисляем статистику
        avg_price = sum(prices) / len(prices)
        min_price = min(prices)
        max_price = max(prices)
        first_price = prices[0]
        last_price = prices[-1]
        change = last_price - first_price
        change_percent = (change / first_price) * 100 if first_price else 0

        # Создаём фигуру
        fig, ax = plt.subplots(figsize=(10, 5.5), dpi=120, facecolor='white')
        fig.subplots_adjust(left=0.08, right=0.95, top=0.90, bottom=0.15)

        # Основная линия
        ax.plot(dates, prices, color=COLOR_MAIN, linewidth=2.5, marker='o', markersize=5,
                markeredgecolor='white', markeredgewidth=1, label='Цена')

        # Линия средней цены
        ax.axhline(y=avg_price, color=COLOR_AVG, linestyle='--', linewidth=1.5,
                   alpha=0.7, label=f'Средняя: {avg_price:.2f} ₽')

        # Заполнение области между минимумом и максимумом
        ax.fill_between(dates, min_price, max_price, color=COLOR_MAIN, alpha=0.08, label='Диапазон')

        # Аннотация крайних точек
        # Минимум
        min_idx = prices.index(min_price)
        ax.annotate(f'{min_price:.2f} ₽', xy=(dates[min_idx], min_price),
                    xytext=(dates[min_idx], min_price - (max_price - min_price)*0.15),
                    arrowprops=dict(arrowstyle='->', color=COLOR_MIN, lw=1),
                    color=COLOR_MIN, fontsize=9, fontweight='bold',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='none', alpha=0.7))
        # Максимум
        max_idx = prices.index(max_price)
        ax.annotate(f'{max_price:.2f} ₽', xy=(dates[max_idx], max_price),
                    xytext=(dates[max_idx], max_price + (max_price - min_price)*0.15),
                    arrowprops=dict(arrowstyle='->', color=COLOR_MAX, lw=1),
                    color=COLOR_MAX, fontsize=9, fontweight='bold',
                    bbox=dict(boxstyle='round,pad=0.3', facecolor='white', edgecolor='none', alpha=0.7))

        # Оформление осей
        ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
        ax.xaxis.set_major_locator(mdates.AutoDateLocator(maxticks=10))
        ax.yaxis.set_major_formatter('{x:.2f} ₽')
        ax.tick_params(axis='both', labelsize=10)
        plt.setp(ax.get_xticklabels(), rotation=30, ha='right')

        # Сетка
        ax.grid(True, linestyle='--', alpha=0.4, color=COLOR_GRID)

        # Заголовок и подписи
        station_name = history[0].station.name if history[0].station else f"АЗС #{station_id}"
        ax.set_title(
            f'⛽ {station_name}\nДинамика цены {fuel_type.value} за {days} дней',
            fontsize=14, fontweight='bold', pad=20
        )
        ax.set_xlabel('Дата', fontsize=11, labelpad=10)
        ax.set_ylabel('Цена, ₽', fontsize=11, labelpad=10)

        # Легенда
        ax.legend(loc='upper left', frameon=True, facecolor='white', edgecolor='none', fontsize=9)

        # Дополнительная информация в правом нижнем углу
        info_text = (
            f'Изменение: {change:+.2f} ₽ ({change_percent:+.1f}%)\n'
            f'Мин: {min_price:.2f} ₽  |  Макс: {max_price:.2f} ₽'
        )
        ax.text(0.98, 0.02, info_text, transform=ax.transAxes,
                fontsize=9, verticalalignment='bottom', horizontalalignment='right',
                bbox=dict(boxstyle='round,pad=0.5', facecolor='white', alpha=0.8))

        # Убираем лишние рамки
        for spine in ax.spines.values():
            spine.set_visible(False)

        # Сохраняем в буфер
        buf = io.BytesIO()
        plt.savefig(buf, format='png', bbox_inches='tight', pad_inches=0.2, dpi=120)
        buf.seek(0)
        plt.close(fig)
        logger.info(f"График сгенерирован, размер {len(buf.getvalue())} байт")
        return buf.getvalue()
