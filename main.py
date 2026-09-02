import asyncio
import logging
import os
import re
import sqlite3
import time
from dataclasses import dataclass
from urllib.parse import urljoin

import aiohttp
from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from bs4 import BeautifulSoup
from dotenv import load_dotenv

load_dotenv()
logging.basicConfig(level=logging.INFO)
router = Router()


@dataclass(frozen=True)
class Book:
    key: str
    label: str
    subject: str
    max_task: int
    source_url: str
    site_id: str = ""
    task_ids: tuple[str, ...] = ()

    def url(self, task: str) -> str:
        if self.key == "makarichev":
            return f"https://reshak.ru/otvet/otvet24.php?otvet1={task}"
        return f"https://reshak.ru/otvet/reshebniki.php?otvet={task}&predmet={self.site_id}"


BOOKS = {
    "makarichev": Book("makarichev", "Алгебра 9 класс, Макарычев", "Алгебра", 1097, "https://reshak.ru/reshebniki/algebra/9/makarichev/index.php"),
    "barkhudarov": Book("barkhudarov", "Русский язык 9 класс, Бархударов", "Русский язык", 527, "https://reshak.ru/reshebniki/russkijazik/9/barh_new/index.html", "barh_new9"),
    "spotlight": Book("spotlight", "Английский 9 класс, Spotlight", "Английский", 500, "https://reshak.ru/spotlight9/index.html", "spotlight9"),
    "perishkin": Book("perishkin", "Физика 9 класс, Перышкин", "Физика", 500, "https://reshak.ru/reshebniki/fizika/9/perishkin/index.html", "perishkin9"),
    "pasechnik": Book("pasechnik", "Биология 9 класс, Пасечник", "Биология", 49, "https://reshak.ru/reshebniki/biologia/9/pasechnik_ucheb/index.html", "pasechnik_ucheb9"),
    "ostroumov": Book("ostroumov", "Химия 9 класс, Остроумов", "Химия", 500, "https://reshak.ru/reshebniki/ximiya/9/ostroumov/index.html", "ostroumov9"),
    "medinsky": Book("medinsky", "История Нового времени, Мединский и Чубарьян", "История Нового времени", 23, "https://reshak.ru/reshebniki/istoria/9/medinsky/index.html", "medinsky9", tuple(map(str, range(1, 24))) + ("itog1", "itog2", "itog3", "itog4", "exam")),
    "medinsky_torkunov": Book("medinsky_torkunov", "История России, Мединский и Торкунов", "История России", 37, "https://reshak.ru/reshebniki/istoria/9/medinsky_torkunov/index.html", "medinsky_torkunov9", ("1", "2", "3", "4", "5", "6-7", "8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19", "20", "21", "22", "23", "24", "25", "26", "27-28", "29", "30", "31", "32", "33", "34", "35", "36", "37", "itog1", "itog2", "itog3", "itog4", "itog5", "itog6", "itog7", "itog8", "exam")),
}
SUBJECTS = {"algebra": ("Алгебра", ["makarichev"]), "russian": ("Русский язык", ["barkhudarov"]), "english": ("Английский", ["spotlight"]), "physics": ("Физика", ["perishkin"]), "biology": ("Биология", ["pasechnik"]), "chemistry": ("Химия", ["ostroumov"]), "history_modern": ("История Нового времени", ["medinsky"]), "history_russia": ("История России", ["medinsky_torkunov"])}


class Cache:
    def __init__(self):
        self.path = os.getenv("CACHE_PATH", "cache.sqlite3")
        self.ttl = int(os.getenv("CACHE_TTL_HOURS", "6")) * 3600
        with sqlite3.connect(self.path) as db:
            db.execute("CREATE TABLE IF NOT EXISTS answers (key TEXT PRIMARY KEY, title TEXT, task TEXT, images TEXT, url TEXT, created INTEGER)")
            columns = {row[1] for row in db.execute("PRAGMA table_info(answers)")}
            old_columns = {"cache_key", "task_text", "image_urls", "source_url", "created_at"}
            if old_columns.issubset(columns):
                db.execute("""CREATE TABLE answers_migrated (
                    key TEXT PRIMARY KEY,
                    title TEXT,
                    task TEXT,
                    images TEXT,
                    url TEXT,
                    created INTEGER
                )""")
                db.execute("""INSERT INTO answers_migrated (key, title, task, images, url, created)
                    SELECT cache_key, title, task_text, image_urls, source_url, created_at
                    FROM answers""")
                db.execute("DROP TABLE answers")
                db.execute("ALTER TABLE answers_migrated RENAME TO answers")

    def get(self, key):
        with sqlite3.connect(self.path) as db:
            row = db.execute("SELECT title, task, images, url, created FROM answers WHERE key=?", (key,)).fetchone()
        if not row or time.time() - row[4] > self.ttl:
            return None
        return row[:2] + ((row[2].split("\n") if row[2] else []), row[3])

    def put(self, key, value):
        with sqlite3.connect(self.path) as db:
            title, task, images, url = value
            db.execute("INSERT OR REPLACE INTO answers VALUES (?, ?, ?, ?, ?, ?)", (key, title, task, "\n".join(images), url, int(time.time())))


async def fetch(book, task, session):
    url = book.url(task)
    async with session.get(url, timeout=aiohttp.ClientTimeout(total=20)) as response:
        if response.status != 200:
            raise RuntimeError(f"Reshak.ru HTTP {response.status}")
        soup = BeautifulSoup(await response.text(), "html.parser")
    title, task_node = soup.select_one("h1.titleh1"), soup.select_one(".text_zad")
    if not title or not task_node:
        raise RuntimeError("задание не найдено")
    images = []
    for image in soup.select("[class*=pic_otvet] img"):
        source = image.get("src") or image.get("data-src")
        if source and urljoin(url, source) not in images:
            images.append(urljoin(url, source))
    return (" ".join(title.get_text(" ", strip=True).split()), " ".join(task_node.get_text(" ", strip=True).split()), images, url)


def subjects_keyboard():
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=name, callback_data=f"subject:{key}")] for key, (name, _) in SUBJECTS.items()])


def books_keyboard(subject):
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text=BOOKS[key].label, callback_data=f"book:{key}")] for key in SUBJECTS[subject][1]] + [[InlineKeyboardButton(text="Назад", callback_data="subjects")]])


def numbers_keyboard(key, page=0):
    choices = BOOKS[key].task_ids or tuple(map(str, range(1, BOOKS[key].max_task + 1)))
    start, end = page * 50, min(page * 50 + 50, len(choices))
    rows = [[InlineKeyboardButton(text=choices[i], callback_data=f"task:{key}:{choices[i]}") for i in range(n, min(n + 5, end))] for n in range(start, end, 5)]
    nav = []
    if page: nav.append(InlineKeyboardButton(text="<<", callback_data=f"page:{key}:{page - 1}"))
    if end < len(choices): nav.append(InlineKeyboardButton(text=">>", callback_data=f"page:{key}:{page + 1}"))
    if nav: rows.append(nav)
    rows.append([InlineKeyboardButton(text="Назад к предметам", callback_data="subjects")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


class SearchState(StatesGroup):
    choosing_book = State()
    entering_number = State()


cache = Cache()


@router.message(CommandStart())
@router.message(Command("help"))
async def start(message: Message, state: FSMContext):
    await state.set_state(SearchState.choosing_book)
    await message.answer("Выберите предмет:", reply_markup=subjects_keyboard())


@router.callback_query(F.data == "subjects")
async def subjects(callback: CallbackQuery, state: FSMContext):
    await state.set_state(SearchState.choosing_book)
    await callback.message.edit_text("Выберите предмет:", reply_markup=subjects_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("subject:"))
async def subject(callback: CallbackQuery):
    await callback.message.edit_text("Выберите учебник:", reply_markup=books_keyboard(callback.data.split(":", 1)[1]))
    await callback.answer()


@router.callback_query(SearchState.choosing_book, F.data.startswith("book:"))
async def book(callback: CallbackQuery, state: FSMContext):
    key = callback.data.split(":", 1)[1]
    await state.update_data(book_key=key)
    await state.set_state(SearchState.entering_number)
    await callback.message.edit_text(f"{BOOKS[key].label}\nВыберите номер задания:", reply_markup=numbers_keyboard(key))
    await callback.answer()


@router.callback_query(SearchState.entering_number, F.data.startswith("page:"))
async def page(callback: CallbackQuery):
    _, key, value = callback.data.split(":")
    await callback.message.edit_reply_markup(reply_markup=numbers_keyboard(key, int(value)))
    await callback.answer()


@router.callback_query(SearchState.entering_number, F.data.startswith("task:"))
async def task_button(callback: CallbackQuery, state: FSMContext):
    _, key, task = callback.data.split(":")
    await state.update_data(book_key=key)
    await callback.answer()
    await process(callback.message, state, task)


@router.message(SearchState.entering_number)
async def task_message(message: Message, state: FSMContext):
    task = (message.text or "").strip()
    data = await state.get_data()
    book = BOOKS.get(data.get("book_key"))
    if not book or not re.fullmatch(r"\d+(?:-\d+)?", task):
        await message.answer("Выберите номер кнопкой или введите номер задания.")
        return
    await process(message, state, task)


async def process(message, state, task):
    book = BOOKS[(await state.get_data()).get("book_key")]
    await message.answer("Ищу решение...")
    try:
        answer = cache.get(f"{book.key}:{task}")
        if not answer:
            async with aiohttp.ClientSession(headers={"User-Agent": "Mozilla/5.0 ReshakTelegramBot/1.0"}) as session:
                answer = await fetch(book, task, session)
            cache.put(f"{book.key}:{task}", answer)
        title, task_text, images, url = answer
        await message.answer(f"<b>{title}</b>\n\n{task_text}\n\n<a href=\"{url}\">Источник на Reshak.ru</a>", disable_web_page_preview=True)
        for image in images:
            try: await message.answer_photo(image)
            except Exception: logging.warning("image send failed: %s", image)
    except Exception as error:
        await message.answer(f"Не удалось загрузить решение: {error}", reply_markup=InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="Открыть на Reshak.ru", url=book.url(task))]]))
    await state.clear()


async def main():
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token: raise RuntimeError("Добавьте BOT_TOKEN в .env")
    bot, dispatcher = Bot(token), Dispatcher()
    dispatcher.include_router(router)
    await bot.delete_webhook(drop_pending_updates=True)
    await dispatcher.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
