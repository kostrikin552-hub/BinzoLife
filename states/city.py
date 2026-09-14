# states/city.py — единый источник FSM-состояний для работы с городом
from aiogram.fsm.state import State, StatesGroup


class CitySelectStates(StatesGroup):
    """FSM-состояние для выбора/смены города (общее для start.py и profile.py)."""
    waiting_city_name = State()
