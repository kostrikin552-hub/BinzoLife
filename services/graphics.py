import matplotlib
matplotlib.use('Agg')

import io
import logging
import random
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List, Tuple
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
import matplotlib.patheffects as PathEffects
from collections import defaultdict

try:
    from scipy.interpolate import make_interp_spline
    HAS_SCIPY = True
except ImportError:
    HAS_SCIPY = False

from database.session import AsyncSessionLocal
from database.crud import get_price_history, get_station_by_id
from database.models import FuelType

logger = logging.getLogger(__name__)


def aggregate_daily_data(dates: List[datetime], prices: List[float]) -> Tuple[List[datetime], List[float]]:
    """Агрегирует данные по дням: удаляет дубликаты, группирует по дате (берёт последнюю цену за день)."""
    if not dates:
        return [], []
    daily_prices = defaultdict(list)
    for d, p in zip(dates, prices):
        day_key = d.date()
        daily_prices[day_key].append(p)
    sorted_days = sorted(daily_prices.keys())
    agg_dates = [datetime.combine(day, datetime.min.time()) for day in sorted_days]
    agg_prices = [daily_prices[day][-1] for day in sorted_days]
    return agg_dates, agg_prices


def _render_chart_sync(
    station_name: str,
    dates: List[datetime],
    prices: List[float],
    fuel_type: str,
    currency: str,
    aspect_ratio: str,
    show_sma: bool,
    show_stats_grid: bool,
    dpi: int,
    is_demo: bool
) -> bytes:
    """Синхронная функция рендеринга (запускается в отдельном потоке)."""
    agg_dates, agg_prices = aggregate_daily_data(dates, prices)
    if len(agg_dates) < 2:
        raise ValueError("После агрегации осталось меньше 2 точек данных")

    days_count = (agg_dates[-1] - agg_dates[0]).days + 1
    date_nums = [mdates.date2num(d) for d in agg_dates]
    prices_arr = np.array(agg_prices, dtype=float)

    BG_COLOR = "#090D16"
    CARD_BG = "#131D2E"
    PRIMARY_COLOR = "#0284C7"
    AVG_COLOR = "#FB923C"
    SMA_COLOR = "#C084FC"
    MIN_COLOR = "#34D399"
    MAX_COLOR = "#F87171"
    TEXT_MAIN = "#F8FAFC"
    TEXT_MUTED = "#94A3B8"
    GRID_COLOR = "#334155"

    plt.rcParams['font.family'] = 'sans-serif'
    plt.rcParams['font.sans-serif'] = ['Plus Jakarta Sans', 'Inter', 'DejaVu Sans', 'Arial']
    plt.rcParams['axes.edgecolor'] = GRID_COLOR
    plt.rcParams['axes.linewidth'] = 1.0

    figsize = (7.5, 13.33) if aspect_ratio == "9:16" else (8.0, 10.66)
    fig, ax = plt.subplots(figsize=figsize, dpi=dpi, facecolor=BG_COLOR)
    ax.set_facecolor(BG_COLOR)

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

    date_nums_arr = np.array(date_nums)
    diffs = np.diff(date_nums_arr)
    if HAS_SCIPY and len(date_nums) >= 4 and np.all(diffs > 0):
        try:
            x_dense = np.linspace(date_nums_arr.min(), date_nums_arr.max(), 350)
            spline = make_interp_spline(date_nums_arr, prices_arr, k=min(3, len(date_nums) - 1))
            y_dense = spline(x_dense)
            y_dense = np.clip(y_dense, min_price - 0.08, max_price + 0.08)
        except Exception:
            x_dense = np.linspace(date_nums_arr.min(), date_nums_arr.max(), 200)
            y_dense = np.interp(x_dense, date_nums_arr, prices_arr)
    else:
        x_dense = np.linspace(date_nums_arr.min(), date_nums_arr.max(), 200)
        y_dense = np.interp(x_dense, date_nums_arr, prices_arr)

    y_range = max(0.8, max_price - min_price)
    y_min_plot = min_price - (y_range * 0.42)
    y_max_plot = max_price + (y_range * 0.58)

    layers = 20
    for i in range(layers):
        alpha = 0.012 + 0.32 * (1.0 - (i / layers) ** 0.5)
        level_y = y_min_plot + (y_dense - y_min_plot) * (1.0 - (i / layers))
        ax.fill_between(x_dense, level_y, y_dense, color=PRIMARY_COLOR, alpha=alpha, lw=0, zorder=2)

    glow_effect = [
        PathEffects.SimpleLineShadow(offset=(0, -2.5), shadow_color=PRIMARY_COLOR, alpha=0.35, rho=0.85),
        PathEffects.Normal()
    ]
    ax.plot(x_dense, y_dense, color=PRIMARY_COLOR, linewidth=3.4, zorder=4, path_effects=glow_effect)

    if show_sma and len(agg_prices) >= 7:
        sma_window = min(7, len(agg_prices))
        sma_7 = np.convolve(prices_arr, np.ones(sma_window)/sma_window, mode='valid')
        sma_dates = date_nums_arr[sma_window-1:]
        ax.plot(sma_dates, sma_7, color=SMA_COLOR, linewidth=2.0, linestyle='--', alpha=0.85, zorder=3, label=f"SMA {sma_window}")

    ax.axhline(avg_price, color=AVG_COLOR, linestyle=':', linewidth=2.0, alpha=0.75, zorder=3)

    step = max(1, len(date_nums) // 20)
    ax.scatter(date_nums_arr[::step], prices_arr[::step],
               color=CARD_BG, edgecolor=PRIMARY_COLOR,
               s=36, linewidth=2.0, zorder=5)

    last_x, last_y = date_nums_arr[-1], prices_arr[-1]
    ax.scatter([last_x], [last_y], color=PRIMARY_COLOR, alpha=0.25, s=280, zorder=6)
    ax.scatter([last_x], [last_y], color=PRIMARY_COLOR, alpha=0.55, s=120, zorder=7)
    ax.scatter([last_x], [last_y], color=CARD_BG, edgecolor=PRIMARY_COLOR, s=64, linewidth=2.8, zorder=8)

    min_x, min_y = date_nums_arr[min_idx], prices_arr[min_idx]
    ax.annotate(f" Мин: {min_price:.2f} {currency} ", xy=(min_x, min_y),
                xytext=(0, -28), textcoords="offset points",
                ha='center', va='top', fontsize=9.5, fontweight='bold', color=MIN_COLOR,
                bbox=dict(boxstyle="round,pad=0.4", fc=CARD_BG, ec=MIN_COLOR, lw=1.8),
                arrowprops=dict(arrowstyle="->", color=MIN_COLOR, lw=1.5), zorder=9)

    max_x, max_y = date_nums_arr[max_idx], prices_arr[max_idx]
    ax.annotate(f" Макс: {max_price:.2f} {currency} ", xy=(max_x, max_y),
                xytext=(0, 24), textcoords="offset points",
                ha='center', va='bottom', fontsize=9.5, fontweight='bold', color=MAX_COLOR,
                bbox=dict(boxstyle="round,pad=0.4", fc=CARD_BG, ec=MAX_COLOR, lw=1.8),
                arrowprops=dict(arrowstyle="->", color=MAX_COLOR, lw=1.5), zorder=9)

    ax.set_ylim(y_min_plot, y_max_plot)
    ax.set_xlim(date_nums_arr[0] - 0.5, date_nums_arr[-1] + 0.8)
    interval = max(1, len(date_nums) // 6)
    ax.xaxis.set_major_locator(mdates.DayLocator(interval=interval))
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%d.%m'))
    plt.xticks(rotation=25, ha='right', fontsize=9, color=TEXT_MUTED)
    plt.subplots_adjust(bottom=0.12)
    ax.yaxis.set_major_formatter('{x:.2f}')
    plt.yticks(fontsize=9, color=TEXT_MUTED)

    ax.grid(True, linestyle='--', alpha=0.5, color=GRID_COLOR, zorder=1)
    ax.spines['top'].set_visible(False)
    ax.spines['right'].set_visible(False)
    ax.spines['left'].set_color(GRID_COLOR)
    ax.spines['bottom'].set_color(GRID_COLOR)

    demo_label = " [ДЕМО]" if is_demo else ""
    fig.text(0.12, 0.95, f"АЗС {station_name}{demo_label}", fontsize=14, fontweight='bold', color=TEXT_MAIN, va='top')
    total_records = len(dates)
    records_info = f" ({total_records} замеров)" if total_records > days_count else ""
    subtitle = f"Динамика {fuel_type} за {days_count} дн.{records_info} • {agg_dates[0].strftime('%d.%m')} — {agg_dates[-1].strftime('%d.%m.%Y')}"
    fig.text(0.12, 0.925, subtitle, fontsize=9, color=TEXT_MUTED, va='top')

    delta_symbol = "▲ +" if is_up else ("▼ " if is_down else "— ")
    delta_str = f"{delta_symbol}{price_change:+.2f} {currency} ({pct_change:+.1f}%)"
    fig.text(0.88, 0.95, f"{current_price:.2f} {currency}", fontsize=16, fontweight='heavy', color=TEXT_MAIN, ha='right', va='top')
    fig.text(0.88, 0.92, delta_str, fontsize=9.5, fontweight='bold', color=delta_color, ha='right', va='top')

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
                 bbox=dict(boxstyle="round,pad=0.5", fc=CARD_BG, ec=GRID_COLOR, lw=1.2))

    plt.subplots_adjust(top=0.90, left=0.12, right=0.92, bottom=0.12)

    buffer = io.BytesIO()
    plt.savefig(buffer, format='png', dpi=dpi, facecolor=BG_COLOR, edgecolor='none', bbox_inches='tight')
    plt.close(fig)
    buffer.seek(0)
    return buffer.getvalue()


async def generate_price_graph(station_id: int, fuel_type: FuelType, days: int = 30, force_demo: bool = False) -> Optional[bytes]:
    logger.info(f"Генерация графика для station_id={station_id}, fuel_type={fuel_type}, days={days}, force_demo={force_demo}")
    async with AsyncSessionLocal() as db:
        station = await get_station_by_id(db, station_id)
        if not station:
            logger.warning(f"Станция {station_id} не найдена")
            return None

        history = await get_price_history(db, station_id, fuel_type, days)
        station_name = station.name or f"АЗС #{station_id}"
        fuel_type_str = fuel_type.value if hasattr(fuel_type, 'value') else str(fuel_type)

        if force_demo or not history or len(history) < 2:
            logger.info(f"Создаём демо-данные для station_id={station_id}")
            end_date = datetime.now()
            start_date = end_date - timedelta(days=30)
            dates = [start_date + timedelta(days=i) for i in range(31)]
            base_price = 68.0
            prices = [round(base_price + i * 0.15 + random.uniform(-0.8, 0.8), 2) for i in range(31)]
            is_demo = True
        else:
            history.sort(key=lambda x: x.recorded_at)
            dates = [h.recorded_at for h in history]
            prices = [h.price for h in history]
            filtered = [(d, p) for d, p in zip(dates, prices) if 0 < p < 200]
            if not filtered:
                logger.warning(f"Все цены аномальны, переключаемся на демо")
                return await generate_price_graph(station_id, fuel_type, days, force_demo=True)
            dates, prices = zip(*filtered)
            is_demo = False

        if len(dates) < 2:
            return await generate_price_graph(station_id, fuel_type, days, force_demo=True)

        try:
            loop = asyncio.get_event_loop()
            chart_bytes = await loop.run_in_executor(
                None,
                _render_chart_sync,
                station_name,
                list(dates),
                list(prices),
                fuel_type_str,
                "₽",
                "9:16",
                True,
                True,
                200,
                is_demo
            )
            logger.info(f"График сгенерирован, размер {len(chart_bytes) / 1024:.1f} КБ, демо={is_demo}")
            return chart_bytes
        except Exception as e:
            logger.error(f"Ошибка генерации графика: {e}", exc_info=True)
            return None
