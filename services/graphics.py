import matplotlib
matplotlib.use('Agg')

import io
import logging
from datetime import datetime, date, timedelta
from typing import Optional, List, Union
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from matplotlib.patches import FancyBboxPatch
import matplotlib.patheffects as PathEffects

# Попытка импорта scipy для сглаживания
try:
    from scipy.interpolate import make_interp_spline
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from database.session import AsyncSessionLocal
from database.crud import get_price_history, get_station_by_id
from database.models import FuelType

logger = logging.getLogger(__name__)


def generate_fuel_price_chart(
    station_name: str,
    dates: List[datetime],
    prices: List[float],
    fuel_type: str = "АИ-95",
    currency: str = "₽",
    aspect_ratio: str = "9:16",
    show_sma: bool = True,
    show_range_bar: bool = False,
    show_stats_grid: bool = True,
    show_watermark: bool = False,
    dpi: int = 200
) -> bytes:
    """
    Генерирует премиальный инвестиционный график цен на топливо для Telegram.
    """
    if len(dates) < 2 or len(prices) < 2:
        raise ValueError("Для построения графика требуется минимум 2 точки данных")

    if len(dates) != len(prices):
        raise ValueError(f"Длины dates ({len(dates)}) и prices ({len(prices)}) не совпадают")

    # Конвертация datetime в числовой формат matplotlib
    date_nums = [mdates.date2num(d) for d in dates]
    prices_arr = np.array(prices, dtype=float)

    # 1. Геометрия холста (вертикальный 9:16)
    figsize = (7.5, 13.33) if aspect_ratio == "9:16" else (8.0, 10.66)
    
    # 2. Цветовая палитра Fintech Pro
    BG_COLOR = "#090D16"
    CARD_BG = "#131D2E"
    PRIMARY_COLOR = "#0284C7"
    GRADIENT_LIGHT = "#38BDF8"
    AVG_COLOR = "#FB923C"
    SMA_COLOR = "#C084FC"
    MIN_COLOR = "#34D399"
    MAX_COLOR = "#F87171"
    TEXT_MAIN = "#F8FAFC"
    TEXT_MUTED = "#94A3B8"
    GRID_COLOR = "rgba(51, 65, 85, 0.45)"

    # Шрифты
    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['font.sans-serif'] = ['Plus Jakarta Sans', 'Inter', 'DejaVu Sans', 'Arial', 'Liberation Sans']
    plt.rcParams['axes.edgecolor'] = GRID_COLOR
    plt.rcParams['axes.linewidth'] = 1.0

    # Создание фигуры
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi, facecolor=BG_COLOR)
    ax.set_facecolor(BG_COLOR)

    # 3. Расчет аналитических метрик
    current_price = prices_arr[-1]
    first_price = prices_arr[0]
    min_price = float(np.min(prices_arr))
    max_price = float(np.max(prices_arr))
    min_idx = int(np.argmin(prices_arr))
    max_idx = int(np.argmax(prices_arr))
    avg_price = float(np.mean(prices_arr))

    price_change = current_price - first_price
    pct_change = (price_change / first_price) * 100 if first_price > 0 else 0.0
    is_up = price_change > 0
    is_down = price_change < 0
    delta_color = MAX_COLOR if is_up else (MIN_COLOR if is_down else TEXT_MUTED)

    # 4. Сглаживание кривой (Spline или интерполяция)
    date_nums_arr = np.array(date_nums)
    if HAS_SCIPY and len(date_nums) >= 4:
        x_dense = np.linspace(date_nums_arr.min(), date_nums_arr.max(), 350)
        spline = make_interp_spline(date_nums_arr, prices_arr, k=3)
        y_dense = spline(x_dense)
        y_dense = np.clip(y_dense, min_price - 0.08, max_price + 0.08)
    else:
        x_dense = np.linspace(date_nums_arr.min(), date_nums_arr.max(), 200)
        y_dense = np.interp(x_dense, date_nums_arr, prices_arr)

    # 5. Границы по оси Y
    y_range = max(0.8, max_price - min_price)
    y_min_plot = min_price - (y_range * 0.42)
    y_max_plot = max_price + (y_range * 0.58)

    # 6. Градиентная заливка под кривой (Soft Bloom)
    layers = 20
    for i in range(layers):
        alpha = 0.012 + 0.32 * (1.0 - (i / layers) ** 0.5)
        level_y = y_min_plot + (y_dense - y_min_plot) * (1.0 - (i / layers))
        ax.fill_between(x_dense, level_y, y_dense, color=PRIMARY_COLOR, alpha=alpha, lw=0, zorder=2)

    # 7. Основная линия со свечением
    glow_effect = [
        PathEffects.SimpleLineShadow(offset=(0, -2.5), shadow_color=PRIMARY_COLOR, alpha=0.35, rho=0.85),
        PathEffects.Normal()
    ]
    ax.plot(x_dense, y_dense, color=PRIMARY_COLOR, linewidth=3.4, zorder=4, path_effects=glow_effect)

    # 8. Скользящая средняя (7-Day SMA) – если включена
    if show_sma and len(prices) >= 7:
        sma_7 = np.convolve(prices_arr, np.ones(7)/7, mode='valid')
        sma_dates = date_nums_arr[6:]
        if HAS_SCIPY and len(sma_dates) >= 4:
            x_sma_dense = np.linspace(sma_dates.min(), sma_dates.max(), 200)
            spline_sma = make_interp_spline(sma_dates, sma_7, k=2)
            y_sma_dense = spline_sma(x_sma_dense)
        else:
            x_sma_dense = sma_dates
            y_sma_dense = sma_7
        ax.plot(x_sma_dense, y_sma_dense, color=SMA_COLOR, linewidth=2.0, linestyle='--', alpha=0.85, zorder=3, label="SMA 7")

    # 9. Линия средней цены за 30 дней
    ax.axhline(avg_price, color=AVG_COLOR, linestyle=':', linewidth=2.0, alpha=0.75, zorder=3)

    # 10. Точки данных
    ax.scatter(date_nums_arr, prices_arr, color=CARD_BG, edgecolor=PRIMARY_COLOR, s=36, linewidth=2.0, zorder=5)

    # 11. Акцентная точка на текущей цене
    last_x, last_y = date_nums_arr[-1], prices_arr[-1]
    ax.scatter([last_x], [last_y], color=PRIMARY_COLOR, alpha=0.25, s=280, zorder=6)
    ax.scatter([last_x], [last_y], color=PRIMARY_COLOR, alpha=0.55, s=120, zorder=7)
    ax.scatter([last_x], [last_y], color=CARD_BG, edgecolor=PRIMARY_COLOR, s=64, linewidth=2.8, zorder=8)

    # 12. Выноски экстремумов (Мин и Макс)
    min_x, min_y = date_nums_arr[min_idx], prices_arr[min_idx]
    ax.annotate(
        f" Мин: {min_price:.2f} {currency} ",
        xy=(min_x, min_y),
        xytext=(0, -28),
        textcoords="offset points",
        ha='center', va='top',
        fontsize=9.5, fontweight='bold', color=MIN_COLOR,
        bbox=dict(boxstyle="round,pad=0.4,rounding_size=0.5", fc=CARD_BG, ec=MIN_COLOR, lw=1.8),
        arrowprops=dict(arrowstyle="->", color=MIN_COLOR, lw=1.5, connectionstyle="arc3,rad=0.1"),
        zorder=9
    )

    max_x, max_y = date_nums_arr[max_idx], prices_arr[max_idx]
    ax.annotate(
        f" Макс: {max_price:.2f} {currency} ",
        xy=(max_x, max_y),
        xytext=(0, 24),
        textcoords="offset points",
        ha='center', va='bottom',
        fontsize=9.5, fontweight='bold', color=MAX_COLOR,
        bbox=dict(boxstyle="round,pad=0.4,rounding_size=0.5", fc=CARD_BG, ec=MAX_COLOR, lw=1.8),
        arrowprops=dict(arrowstyle="->", color=MAX_COLOR, lw=1.5, connectionstyle="arc3,rad=-0.1"),
        zorder=9
    )

    # 13. Настройка осей и сетки
    ax.set_ylim(y_min_plot, y_max_plot)
    ax.set_xlim(date_nums_arr[0] - 0.5, date_nums_arr[-1] + 0.8)

    ax.xaxis.set_major_locator(mdates.DayLocator(interval=max(1, len(dates) // 6)))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
    plt.xticks(rotation=25, ha='right', fontsize=9, color=TEXT_MUTED)
    plt.yticks(fontsize=9, color=TEXT_MUTED)

    ax.grid(True, linestyle='--', alpha=0.5, color=GRID_COLOR, zorder=1)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color(GRID_COLOR)
    ax.spines['bottom'].set_color(GRID_COLOR)

    # 14. Премиальный Header
    delta_symbol = "▲ +" if is_up else ("▼ " if is_down else "— ")
    delta_str = f"{delta_symbol}{price_change:+.2f} {currency} ({pct_change:+.1f}%)"

    fig.text(0.12, 0.95, f"⛽ {station_name}", fontsize=14, fontweight='bold', color=TEXT_MAIN, va='top')
    fig.text(0.12, 0.925, f"Динамика {fuel_type} за {len(dates)} дней • {dates[0].strftime('%d.%m')} — {dates[-1].strftime('%d.%m.%Y')}", fontsize=9, color=TEXT_MUTED, va='top')

    fig.text(0.88, 0.95, f"{current_price:.2f} {currency}", fontsize=16, fontweight='heavy', color=TEXT_MAIN, ha='right', va='top')
    fig.text(0.88, 0.92, delta_str, fontsize=9.5, fontweight='bold', color=delta_color, ha='right', va='top')

    # 15. Нижняя плашка аналитики
    if show_stats_grid:
        spread = max_price - min_price
        fair_diff = current_price - avg_price
        if fair_diff < -0.01:
            fair_text = f"Выгоднее ср. ({abs(fair_diff):.2f} {currency})"
        elif fair_diff > 0.01:
            fair_text = f"Выше ср. (+{fair_diff:.2f} {currency})"
        else:
            fair_text = "≈ средней цене"
        stats_line = f"Средняя: {avg_price:.2f} {currency}   |   Размах: ±{spread:.2f} {currency}   |   {fair_text}"
        fig.text(0.5, 0.025, stats_line, fontsize=8.5, fontweight='semibold', color=TEXT_MUTED, ha='center', va='bottom',
                 bbox=dict(boxstyle="round,pad=0.5,rounding_size=0.4", fc=CARD_BG, ec=GRID_COLOR, lw=1.2))

    plt.subplots_adjust(top=0.90, bottom=0.08, left=0.12, right=0.92)

    # 16. Рендеринг в память
    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', dpi=dpi, facecolor=BG_COLOR, edgecolor='none', bbox_inches='tight')
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


# ============================================================================
# АДАПТЕР ДЛЯ ВАШЕГО ПРОЕКТА (заменяет старую функцию generate_price_graph)
# ============================================================================
async def generate_price_graph(station_id: int, fuel_type: FuelType, days: int = 30) -> Optional[bytes]:
    """
    Генерирует премиальный график цен для АЗС, используя данные из БД.
    """
    logger.info(f"Генерация графика для station_id={station_id}, fuel_type={fuel_type}, days={days}")
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            logger.warning(f"Станция {station_id} не найдена")
            return None

        history = await get_price_history(db, station_id, fuel_type, days)
        if not history:
            logger.warning(f"Нет данных для station_id={station_id} за {days} дней")
            return None

        # Сортируем по времени
        history.sort(key=lambda x: x.recorded_at)
        dates = [h.recorded_at for h in history]
        prices = [h.price for h in history]

        # Фильтруем аномалии
        filtered = [(d, p) for d, p in zip(dates, prices) if 0 < p < 200]
        if not filtered:
            logger.warning(f"Все цены аномальны для station_id={station_id}")
            return None
        dates, prices = zip(*filtered)

        # Название станции
        station_name = station.name or f"АЗС #{station_id}"
        fuel_type_str = fuel_type.value if hasattr(fuel_type, 'value') else str(fuel_type)

        # Генерируем график
        try:
            chart_bytes = generate_fuel_price_chart(
                station_name=station_name,
                dates=list(dates),
                prices=list(prices),
                fuel_type=fuel_type_str,
                currency="₽",
                aspect_ratio="9:16",
                show_sma=True,
                show_stats_grid=True,
                dpi=200
            )
            logger.info(f"График сгенерирован, размер {len(chart_bytes) / 1024:.1f} КБ")
            return chart_bytes
        except Exception as e:
            logger.error(f"Ошибка генерации графика: {e}", exc_info=True)
            return None
