from aiogram import Router, types, F
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from database.session import AsyncSessionLocal
from database.crud import (
    get_user, create_notification, get_active_notifications_for_user,
    get_notification_by_id, deactivate_notification
)
from database.models import FuelType
from keyboards.reply import main_menu_keyboard
from keyboards.inline import notification_action_keyboard
from services.subscription import check_pro

router = Router()

class NotifStates(StatesGroup):
    waiting_price = State()

@router.message(F.text == "🔔 Мои уведомления")
async def list_notifications(message: types.Message):
    if not await check_pro(message.from_user.id):
        await message.answer(
            "🔔 Уведомления доступны только в PRO-подписке.\n"
            "Купите PRO за 99 ₽/месяц, чтобы получать уведомления о ценах и наличии.",
            reply_markup=main_menu_keyboard()
        )
        return
    async with AsyncSessionLocal() as db:
        user = await get_user(db, message.from_user.id)
        if not user:
            await message.answer("Сначала /start")
            return
        notifs = await get_active_notifications_for_user(db, user.id)
        if not notifs:
            await message.answer("У вас нет активных уведомлений.\nЧтобы создать, нажмите «Следить за ценой» при поиске АЗС.")
        else:
            for n in notifs:
                text = f"🔔 Уведомление #{n.id}: {n.fuel_type.value}"
                if n.target_price:
                    text += f", цена ≤ {n.target_price} ₽"
                if n.notify_on_availability:
                    text += ", при появлении наличия"
                await message.answer(text, reply_markup=notification_action_keyboard(n.id))
        await message.answer("Главное меню:", reply_markup=main_menu_keyboard())

@router.callback_query(lambda c: c.data.startswith("unsub_"))
async def unsubscribe_notification(callback: types.CallbackQuery):
    notif_id = int(callback.data.split("_")[1])
    async with AsyncSessionLocal() as db:
        notif = await get_notification_by_id(db, notif_id)
        if not notif:
            await callback.answer("Уведомление не найдено.")
            return
        if notif.user_id != callback.from_user.id:
            await callback.answer("⛔ Вы не можете отписаться от этого уведомления.")
            return
        await deactivate_notification(db, notif_id)
    await callback.answer("Уведомление отключено")
    await callback.message.edit_text("✅ Вы отписались от уведомления.")
