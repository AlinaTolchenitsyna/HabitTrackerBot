import asyncio
import logging
import re
from typing import List
from aiogram import Bot, Dispatcher, Router
from aiogram.filters import Command, CommandStart
from aiogram.filters.state import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    Message,
    ReplyKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardRemove,
    InlineKeyboardButton, CallbackQuery
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
import datetime
from data.utils import get_motivation
from config import get_settings
from data.db import Database
from datetime import date, timedelta, datetime as dt
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# ---------- Настройка ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s") # Настройка логирования

settings = get_settings()
bot = Bot(token=settings.bot_token)

storage = MemoryStorage()
dp = Dispatcher(storage=storage)

router = Router()
dp.include_router(router)

db = Database()

# ---------- FSM ----------
class AddHabit(StatesGroup):
    name = State()
    frequency = State()
    schedule = State()
    reminder_time = State()

class EditHabitStates(StatesGroup):
    waiting_for_name = State()
    waiting_for_frequency = State()
    waiting_for_schedule = State()
    waiting_for_reminder = State()

# Клавиатура для выбора частоты
FREQ_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[
        [KeyboardButton(text="Ежедневно"), KeyboardButton(text="Еженедельно")],
        [KeyboardButton(text="/cancel")],
    ],
    resize_keyboard=True,
)

CANCEL_KEYBOARD = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text="/cancel")]],
    resize_keyboard=True
)

# ---------- Утилиты ----------
def parse_weekdays(input_text: str) -> List[int]:
    """
    Поддерживает:
      - числа 0..6 (0=понедельник, 6=воскресенье)
      - числа 1..7 (1=понедельник)
      - русские сокращения: пн, вт, ср, чт, пт, сб, вс
      - полные названия (понедельник и т.д.)
    Возвращает отсортированный список уникальных целых 0..6.
    Бросает ValueError при неверном формате.
    """
    mapping = {
        "пн": 0, "пон": 0, "понедельник": 0,
        "вт": 1, "втор": 1, "вторник": 1,
        "ср": 2, "сред": 2, "среда": 2,
        "чт": 3, "чет": 3, "четверг": 3,
        "пт": 4, "пят": 4, "пятница": 4,
        "сб": 5, "суб": 5, "суббота": 5,
        "вс": 6, "воск": 6, "воскресенье": 6,
    }
    text = input_text.lower().strip()
    if not text:
        raise ValueError("Пустой ввод")
    parts = re.split(r"[,\s;]+", text)
    result = set()
    for p in parts:
        if not p:
            continue
        if p.isdigit():
            n = int(p)
            if 0 <= n <= 6:
                result.add(n)
                continue
            if 1 <= n <= 7:
                result.add(n - 1)
                continue
            raise ValueError(f"Число вне диапазона 0..6: {p}")
        # убрать возможные точки (например, "пн.")
        p_clean = p.rstrip(".")
        if p_clean in mapping:
            result.add(mapping[p_clean])
        else:
            raise ValueError(f"Неизвестный день: {p}")
    if not result:
        raise ValueError("Не получилось распарсить дни")
    return sorted(result)

# ---------- Хэндлеры ----------
@router.message(CommandStart())
async def on_start(message: Message):
    user_id = await db.add_user(chat_id=message.chat.id, username=message.from_user.username)
    await message.answer(
        "Привет! Я бот-трекер привычек.\n"
        "Сохранил профиль в базе.\n"
        "Добавить новую привычку — команда /add\n"
        "Отметить выполнение — /done\n"
        "Посмотреть на сегодня — /today"
    )

@router.message(Command("help"))
async def on_help(message: Message):
    await message.answer(
        "Команды:\n"
        "/start — регистрация\n"
        "/add — добавить привычку\n"
        "/cancel — отменить текущее добавление\n"
        "/today /week /month — статистика\n"
    )

# /cancel — универсальная отмена состояний
@router.message(Command("cancel"))
async def cmd_cancel(message: Message, state: FSMContext):
    current = await state.get_state()
    if current is None:
        await message.answer("Нечего отменять.", reply_markup=ReplyKeyboardRemove())
        return
    await state.clear()
    await message.answer("Операция отменена.", reply_markup=ReplyKeyboardRemove())

# /add — старт
@router.message(Command("add"))
async def cmd_add(message: Message, state: FSMContext):
    await state.set_state(AddHabit.name)
    await message.answer(
        "Как называется привычка? Пример: «Читать 20 страниц»\n\nНапиши название или /cancel, чтобы выйти.",
        reply_markup=ReplyKeyboardRemove(),
    )

# Имя привычки
@router.message(StateFilter(AddHabit.name))
async def process_habit_name(message: Message, state: FSMContext):
    name = message.text.strip()
    if not name or len(name) < 2:
        await message.answer("Слишком короткое название. Введите более понятное имя (мин. 2 символа) или /cancel.")
        return
    if len(name) > 200:
        await message.answer("Название слишком длинное. Уменьши до 200 символов.")
        return
    await state.update_data(name=name)
    await state.set_state(AddHabit.frequency)
    await message.answer("Выбери частоту выполнения:", reply_markup=FREQ_KEYBOARD)

# Частота
@router.message(StateFilter(AddHabit.frequency))
async def process_frequency(message: Message, state: FSMContext):
    text = message.text.strip().lower()
    if text in ("ежедневно", "daily"):
        freq = "daily"
    elif text in ("еженедельно", "еженед", "weekly"):
        freq = "weekly"
    else:
        await message.answer("Пожалуйста, выбери одну из кнопок: 'Ежедневно' или 'Еженедельно', либо /cancel.")
        return

    await state.update_data(frequency=freq)
    if freq == "daily":
        
        await state.set_state(AddHabit.reminder_time)
        await message.answer(
            "Укажи время для напоминания в формате HH:MM (например, 08:30), или оставь пустым для без напоминания.",
            reply_markup=ReplyKeyboardRemove()
        )
    else:
        await state.set_state(AddHabit.schedule)
        await message.answer(
            "Отлично. Введи дни недели, в которые нужно напоминать.\n"
            "Примеры: `пн, ср, пт` или `0,2,4` (0=понедельник, 6=воскресенье).\n"
            "Также можно использовать 1..7 (1=понедельник).\n\n"
            "Введи через запятую или пробел. Или /cancel.",
            reply_markup=CANCEL_KEYBOARD,
        )

@router.message(StateFilter(AddHabit.reminder_time))
async def process_reminder_time(message: Message, state: FSMContext):
    text = message.text.strip()
    reminder_time = None
    if text:
        try:
            dt.strptime(text, "%H:%M")  # проверка формата
            reminder_time = text
        except ValueError:
            await message.answer("Неверный формат. Используй HH:MM, например 08:30, или оставь пустым.")
            return
    data = await state.get_data()
    user = await db.get_user_by_chat(message.chat.id)
    user_id = user["id"]
    await db.add_habit(
        user_id=user_id,
        name=data["name"],
        frequency=data["frequency"],
        schedule=data.get("schedule"),
        reminder_time=reminder_time
    )
    await state.clear()
    await message.answer(f"Готово — привычка '{data['name']}' добавлена ✅", reply_markup=ReplyKeyboardRemove())
    
# Расписание (дни недели)
@router.message(StateFilter(AddHabit.schedule))
async def process_schedule(message: Message, state: FSMContext):
    text = message.text.strip()
    try:
        days = parse_weekdays(text)  
    except ValueError as e:
        await message.answer(f"Не понял дни: {e}\nПопробуй ещё раз (пример: 'пн, ср, пт' или '0,2,4') или /cancel.")
        return

    data = await state.get_data()
    name = data.get("name")
    
    user = await db.get_user_by_chat(message.chat.id)
    if not user:
        user_id = await db.add_user(chat_id=message.chat.id, username=message.from_user.username)
    else:
        user_id = user["id"]
    await db.add_habit(user_id=user_id, name=name, frequency="weekly", schedule=days)
    await state.clear()
    
    wd_names = ["пн", "вт", "ср", "чт", "пт", "сб", "вс"]
    pretty = ", ".join(wd_names[d] for d in days)
    await message.answer(f"Готово — привычка '{name}' добавлена (еженедельно: {pretty}) ✅", reply_markup=ReplyKeyboardRemove())

# ---------- Main ----------
async def main():
    await db.connect()
    await schedule_reminders()
    # снятие возможного вебхука (безопасно)
    await bot.delete_webhook(drop_pending_updates=True)
    try:
        await dp.start_polling(bot)
    finally:
        await db.close()

async def schedule_reminders():
    habits = await db.get_all_habits_with_reminders()
    for h in habits:
        rt = h.get("reminder_time")
        if not rt:
            continue
        hour, minute = map(int, rt.split(":"))
        
        scheduler.add_job(
            send_reminder,
            trigger=CronTrigger(hour=hour, minute=minute),
            args=[h],
            id=f"habit_{h['id']}",
            replace_existing=True,
            coalesce=True,
        )
    scheduler.start()
    

        # ----------------- вспомогательная функция -----------------
async def build_today_habits_keyboard(user_id: int):
    kb = InlineKeyboardBuilder()
    habits = await db.get_today_habits(user_id)
    today = datetime.date.today().isoformat()

    if not habits:
        return None

    for habit in habits:
        prog = await db.get_progress_for_habit(habit["id"], start_date=today, end_date=today)
        status = "✅" if prog else "⬜"

        kb.row(
            InlineKeyboardButton(text=habit["name"], callback_data=f"habit:{habit['id']}"),
            InlineKeyboardButton(text=status, callback_data=f"mark:{habit['id']}")
        )
        kb.row(
            InlineKeyboardButton(text="✏️ Редактировать", callback_data=f"habit:edit:{habit['id']}"),
            InlineKeyboardButton(text="🗑 Удалить", callback_data=f"habit:del:{habit['id']}")
        )

    kb.adjust(2)
    kb.row(InlineKeyboardButton(text="Отмена", callback_data="mark:cancel"))
    return kb.as_markup()

# ----------------- /done — показать привычки и клавиатуру -----------------
@router.message(Command("done"))
async def cmd_done(message: Message):
    user = await db.get_user_by_chat(message.chat.id)
    if not user:
        # регистрация
        user = await db.get_user_by_chat(message.chat.id)
    kb = await build_today_habits_keyboard(user["id"])
    if kb is None:
        await message.answer("На сегодня у тебя нет привычек. Добавь новую привычку командой /add.")
        return
    await message.answer("Выбери привычку, чтобы отметить её выполненной:", reply_markup=kb)

# ----------------- Callback для отметки выполнения -----------------
@router.callback_query(lambda c: c.data and c.data.startswith("mark:"))
async def cb_mark_done(callback: CallbackQuery):
    
    data = callback.data.split(":", 1)[1]
    
    if data == "cancel":
        await callback.answer("Отмена.", show_alert=False)
        # убрать клавиатуру
        try:
            await callback.message.edit_reply_markup(reply_markup=None)
        except Exception:
            pass
        return

    try:
        habit_id = int(data)
    except ValueError:
        await callback.answer("Неверные данные.", show_alert=True)
        return

    # проверка принадлежности привычки пользователю
    habit = await db.get_habit(habit_id)
    user = await db.get_user_by_chat(callback.message.chat.id)
    if not habit or not user or habit["user_id"] != user["id"]:
        await callback.answer("Нельзя отмечать эту привычку.", show_alert=True)
        return

    today = datetime.date.today().isoformat()
    # проверка, не отмечена ли уже
    already = await db.get_progress_for_habit(habit_id, start_date=today, end_date=today)
    if already:
        await callback.answer("Эта привычка уже отмечена сегодня ✅", show_alert=False)
        # обновление интерфейса
        try:
            kb = await build_today_habits_keyboard(user["id"])
            if kb:
                await callback.message.edit_reply_markup(reply_markup=kb)
        except Exception:
            pass
        return

    await db.mark_done(habit_id, date=today)
    phrase = get_motivation()
    # короткое всплывающее подтверждение
    await callback.answer(phrase, show_alert=False)

    try:
        await callback.message.edit_text(f"Готово — ты отметил(а) привычку: «{habit['name']}»\n\n{phrase}")
    except Exception:
        # если редактирование не удалось - простое сообщение
        await callback.message.answer(f"Готово — ты отметил(а) привычку: «{habit['name']}»\n\n{phrase}")

# ---------- Вспомогательные утилиты для статистики ----------
def daterange(start_date: date, end_date: date):
    """Генерирует даты от start_date до end_date включительно."""
    for n in range((end_date - start_date).days + 1):
        yield start_date + timedelta(days=n)

def iso(d: date) -> str:
    return d.isoformat()

def parse_date(s: str) -> date:
    """Парсинг строки 'YYYY-MM-DD' в date"""
    return dt.strptime(s, "%Y-%m-%d").date()

def expected_occurrences(habit: dict, start_date: date, end_date: date) -> int:
    """
    Считает, сколько раз привычка должна была появиться в интервале.
    Правила:
      - daily -> каждый день в интервале
      - weekly -> если schedule не None -> считаем дни недели из schedule
                 если schedule is None -> используем день недели created_at
      - другие частоты -> считаем как daily (фоллбек)
    """
    freq = (habit.get("frequency") or "daily").lower()
    if freq == "daily":
        return (end_date - start_date).days + 1

    # weekly
    if freq == "weekly":
        sched = habit.get("schedule") 
        if sched:
            # schedule — список чисел 0..6 (понедельник=0)
            s = set(int(x) for x in sched)
            cnt = 0
            for d in daterange(start_date, end_date):
                if d.weekday() in s:
                    cnt += 1
            return cnt
        
        created = habit.get("created_at")
        if created:
            try:
                c_date = dt.strptime(created.split(" ")[0], "%Y-%m-%d").date()
                target_wd = c_date.weekday()
                cnt = sum(1 for d in daterange(start_date, end_date) if d.weekday() == target_wd)
                return cnt
            except Exception:
                pass
            
        days = (end_date - start_date).days + 1
        return max(1, days // 7)

    return (end_date - start_date).days + 1

def pretty_percent(done: int, total: int) -> str:
    if total <= 0:
        return "—"
    p = int(round((done / total) * 100))
    return f"{p}%"

def progress_bar(done: int, total: int, width: int = 10) -> str:
    if total <= 0:
        return " " * width
    filled = int(round((done / total) * width))
    filled = min(max(filled, 0), width)
    return "█" * filled + "░" * (width - filled)

@router.message(Command("today"))
async def cmd_today(message: Message):
    user = await db.get_user_by_chat(message.chat.id)
    if not user:
        await db.add_user(chat_id=message.chat.id, username=message.from_user.username)
        user = await db.get_user_by_chat(message.chat.id)

    today = date.today()
    today_iso = iso(today)

    # получение актуальных привычек на сегодня
    habits = await db.get_today_habits(user["id"])
    if not habits:
        await message.answer("На сегодня у тебя нет привычек — добавь с помощью /add.")
        return

    lines = [f"📅 Статус на сегодня — {today_iso}\n"]
    for idx, h in enumerate(habits, start=1):
        done = await db.get_progress_for_habit(h["id"], start_date=today_iso, end_date=today_iso)
        mark = "✅" if done else "❌"
        lines.append(f"{idx}. {h['name']} — {mark}")

    await message.answer("\n".join(lines))

@router.message(Command("week"))
async def cmd_week(message: Message):
    user = await db.get_user_by_chat(message.chat.id)
    if not user:
        await db.add_user(chat_id=message.chat.id, username=message.from_user.username)
        user = await db.get_user_by_chat(message.chat.id)

    end_date = date.today()
    start_date = end_date - timedelta(days=6)  # последние 7 дней
    dates = list(daterange(start_date, end_date))

    lines = [f"📊 Прогресс за последние 7 дней ({start_date.isoformat()} — {end_date.isoformat()}):\n"]

    habits = await db.get_habits(user["id"])
    if not habits:
        await message.answer("У тебя ещё нет привычек. Добавь через /add.")
        return

    for idx, h in enumerate(habits, start=1):
        prog_rows = await db.get_progress_for_habit(h["id"], start_date=start_date.isoformat(), end_date=end_date.isoformat())
        done_dates = set(r["date"] for r in prog_rows)

        done_count = len(done_dates)
        expected = expected_occurrences(h, start_date, end_date)

        per_day = " ".join("✅" if d.isoformat() in done_dates else "·" for d in dates)

        pct = pretty_percent(done_count, expected) if expected > 0 else "—"
        bar = progress_bar(done_count, expected) if expected > 0 else ""

        lines.append(f"{idx}. {h['name']}\n   {done_count}/{expected} {pct} {bar}\n   {per_day}\n")

    await message.answer("\n".join(lines))

@router.message(Command("month"))
async def cmd_month(message: Message):
    user = await db.get_user_by_chat(message.chat.id)
    if not user:
        await db.add_user(chat_id=message.chat.id, username=message.from_user.username)
        user = await db.get_user_by_chat(message.chat.id)

    today = date.today()
    start_date = today.replace(day=1)
    end_date = today
    dates = list(daterange(start_date, end_date))

    lines = [f"📅 Прогресс за месяц ({start_date.isoformat()} — {end_date.isoformat()}):\n"]

    habits = await db.get_habits(user["id"])
    if not habits:
        await message.answer("У тебя ещё нет привычек. Добавь через /add.")
        return

    for idx, h in enumerate(habits, start=1):
        prog_rows = await db.get_progress_for_habit(h["id"], start_date=start_date.isoformat(), end_date=end_date.isoformat())
        done_dates = set(r["date"] for r in prog_rows)

        done_count = len(done_dates)
        expected = expected_occurrences(h, start_date, end_date)

        per_day = " ".join("✅" if d.isoformat() in done_dates else "·" for d in dates)
        pct = pretty_percent(done_count, expected) if expected > 0 else "—"
        bar = progress_bar(done_count, expected) if expected > 0 else ""

        lines.append(f"{idx}. {h['name']}\n   {done_count}/{expected} {pct} {bar}\n   {per_day}\n")

    await message.answer("\n".join(lines))


scheduler = AsyncIOScheduler()

async def send_reminder(habit):
    chat_id = habit["chat_id"]

    today_iso = date.today().isoformat()

    # проверка, не выполнена ли привычка сегодня
    done = await db.get_progress_for_habit(habit["id"], start_date=today_iso, end_date=today_iso)
    if done:
        return

    # проверка расписания для weekly
    if habit["frequency"] == "weekly" and habit["schedule"]:
        today_wd = date.today().weekday()  
        if today_wd not in eval(habit["schedule"]):
            return  # не день из расписания

    try:
        await bot.send_message(chat_id, f"⏰ Напоминание: {habit['name']}")
    except Exception as e:
        print("Ошибка отправки напоминания:", e)

@router.callback_query(lambda c: c.data and c.data.startswith("habit:del:") and not c.data.startswith("habit:del:yes:"))
async def cb_habit_delete_confirm(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split(":")
    habit_id = int(parts[2])

    habit = await db.get_habit(habit_id)
    user = await db.get_user_by_chat(callback.message.chat.id)
    if not habit or not user or habit["user_id"] != user["id"]:
        await callback.answer("Нельзя удалить эту привычку.", show_alert=True)
        return

    kb = InlineKeyboardBuilder()
    kb.row(
        InlineKeyboardButton(text="Да, удалить", callback_data=f"habit:del:yes:{habit_id}"),
        InlineKeyboardButton(text="Нет, отмена", callback_data="menu:myhabits")
    )
    markup = kb.as_markup()  

    await callback.message.edit_text(
        f"Ты уверен, что хочешь удалить привычку «{habit['name']}»?",
        reply_markup=markup
    )


@router.callback_query(lambda c: c.data and c.data.startswith("habit:del:yes:"))
async def cb_habit_delete_execute(callback: CallbackQuery):
    await callback.answer()
    parts = callback.data.split(":")
    habit_id = int(parts[3])

    habit = await db.get_habit(habit_id)
    user = await db.get_user_by_chat(callback.message.chat.id)
    if not habit or not user or habit["user_id"] != user["id"]:
        await callback.answer("Нельзя удалить эту привычку.", show_alert=True)
        return

    await db.delete_habit(habit_id)
    await callback.message.edit_text(f"Привычка «{habit['name']}» удалена ✅")

@router.callback_query(lambda c: c.data and c.data.startswith("habit:edit:"))
async def cb_habit_edit(callback: CallbackQuery, state: FSMContext):
    await callback.answer()
    parts = callback.data.split(":")
    habit_id = int(parts[2])

    habit = await db.get_habit(habit_id)
    user = await db.get_user_by_chat(callback.message.chat.id)
    if not habit or not user or habit["user_id"] != user["id"]:
        await callback.answer("Нельзя редактировать эту привычку.", show_alert=True)
        return

    await state.update_data(habit_id=habit_id)

    await callback.message.answer(
        f"Текущее название привычки: {habit['name']}\n\n"
        f"Введи новое название или напиши «-», чтобы оставить без изменений."
    )
    await state.set_state(EditHabitStates.waiting_for_name)

@router.message(EditHabitStates.waiting_for_name)
async def edit_habit_name(message: Message, state: FSMContext):
    new_name = message.text.strip()
    data = await state.get_data()
    habit_id = data["habit_id"]

    if new_name != "-":
        await db.update_habit(habit_id, name=new_name)

    await message.answer("Укажи частоту (daily/weekly) или «-» чтобы не менять.")
    await state.set_state(EditHabitStates.waiting_for_frequency)

@router.message(EditHabitStates.waiting_for_frequency)
async def edit_habit_frequency(message: Message, state: FSMContext):
    freq = message.text.strip().lower()
    data = await state.get_data()
    habit_id = data["habit_id"]

    if freq != "-":
        if freq not in ("daily", "weekly"):
            await message.answer("Некорректная частота. Введи daily или weekly, либо «-».")
            return
        await db.update_habit(habit_id, frequency=freq)

    await message.answer("Укажи расписание (например [0,2,4]) или «-», чтобы оставить как есть.")
    await state.set_state(EditHabitStates.waiting_for_schedule)

@router.message(EditHabitStates.waiting_for_schedule)
async def edit_habit_schedule(message: Message, state: FSMContext):
    sched = message.text.strip()
    data = await state.get_data()
    habit_id = data["habit_id"]

    if sched != "-":
        try:
            schedule = json.loads(sched) 
            await db.update_habit(habit_id, schedule=schedule)
        except Exception:
            await message.answer("Некорректный формат. Введи JSON массив (например [0,2,4]) или «-».")
            return

    await message.answer("Укажи время напоминания (HH:MM) или «-», чтобы оставить как есть.")
    await state.set_state(EditHabitStates.waiting_for_reminder)

@router.message(EditHabitStates.waiting_for_reminder)
async def edit_habit_reminder(message: Message, state: FSMContext):
    rem = message.text.strip()
    data = await state.get_data()
    habit_id = data["habit_id"]

    if rem != "-":
        if not re.match(r"^\d{2}:\d{2}$", rem):
            await message.answer("Некорректный формат времени. Используй HH:MM или «-».")
            return
        await db.update_habit(habit_id, reminder_time=rem)

    await state.clear()
    await message.answer("✅ Привычка успешно обновлена!")


if __name__ == "__main__":
    asyncio.run(main())

