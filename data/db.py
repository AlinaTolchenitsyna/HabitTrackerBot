# db.py
import aiosqlite
import os
import json
import datetime
from typing import Optional, List, Dict, Any

DB_DIR = "data"
DB_FILE = os.path.join(DB_DIR, "habits.db")


class Database:
    def __init__(self, path: str = DB_FILE):
        self.path = path
        self.conn: Optional[aiosqlite.Connection] = None

    async def connect(self):
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        # чтобы получать строки как dict-like
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA foreign_keys = ON;")
        await self._create_tables()

    async def close(self):
        if self.conn:
            await self.conn.close()
            self.conn = None

    async def _create_tables(self):
        assert self.conn is not None
        await self.conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              username TEXT,
              chat_id INTEGER UNIQUE NOT NULL
            );

            CREATE TABLE IF NOT EXISTS habits (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              user_id INTEGER NOT NULL,
              name TEXT NOT NULL,
              frequency TEXT NOT NULL DEFAULT 'daily',
              schedule TEXT,
              created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
              FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS progress (
              id INTEGER PRIMARY KEY AUTOINCREMENT,
              habit_id INTEGER NOT NULL,
              date TEXT NOT NULL,
              status INTEGER NOT NULL DEFAULT 1,
              UNIQUE (habit_id, date),
              FOREIGN KEY (habit_id) REFERENCES habits(id) ON DELETE CASCADE
            );
            """
        )
        await self.conn.commit()

    # ---------- Habits / Reminders ----------
    async def get_all_habits_with_reminders(self) -> List[dict]:
        """
        Возвращает все привычки с ненулевым reminder_time.
        """
        assert self.conn is not None
        cur = await self.conn.execute(
            "SELECT * FROM habits WHERE reminder_time IS NOT NULL AND reminder_time != ''"
        )
        rows = await cur.fetchall()
        return [self._row_to_habit_dict(r) for r in rows]


    # ---------- Users ----------
    async def add_user(self, chat_id: int, username: Optional[str] = None) -> int:
        assert self.conn is not None
        
        await self.conn.commit()
        row = await (await self.conn.execute("SELECT id FROM users WHERE chat_id = ?", (chat_id,))).fetchone()
        return row["id"]

    async def get_user_by_chat(self, chat_id: int) -> Optional[dict]:
        assert self.conn is not None
        row = await (await self.conn.execute("SELECT * FROM users WHERE chat_id = ?", (chat_id,))).fetchone()
        return dict(row) if row else None

    # ---------- Habits ----------
    async def add_habit(self, user_id: int, name: str, frequency: str, schedule=None, reminder_time=None):
        async with self.conn.execute(
            """
            INSERT INTO habits (user_id, name, frequency, schedule, reminder_time)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, name, frequency, str(schedule) if schedule else None, reminder_time)
        ) as cursor:
            await self.conn.commit()
            return cursor.lastrowid


    async def get_habits(self, user_id: int) -> List[dict]:
        assert self.conn is not None
        cur = await self.conn.execute("SELECT * FROM habits WHERE user_id = ?", (user_id,))
        rows = await cur.fetchall()
        return [self._row_to_habit_dict(r) for r in rows]

    async def get_habit(self, habit_id: int) -> Optional[dict]:
        assert self.conn is not None
        row = await (await self.conn.execute("SELECT * FROM habits WHERE id = ?", (habit_id,))).fetchone()
        return self._row_to_habit_dict(row) if row else None

    async def update_habit(self, habit_id: int, **fields) -> None:
        assert self.conn is not None
        if not fields:
            return
        allowed = {"name", "frequency", "schedule", "reminder_time"} 
        sets = []
        params = []
        for k, v in fields.items():
            if k not in allowed:
                continue
            if k == "schedule":
                v = json.dumps(v) if v is not None else None
            sets.append(f"{k} = ?")
            params.append(v)
        params.append(habit_id)
        q = f"UPDATE habits SET {', '.join(sets)} WHERE id = ?"
        await self.conn.execute(q, params)
        await self.conn.commit()


    async def delete_habit(self, habit_id: int) -> None:
        assert self.conn is not None
        await self.conn.execute("DELETE FROM habits WHERE id = ?", (habit_id,))
        await self.conn.commit()

    # ---------- Progress ----------
    async def mark_done(self, habit_id: int, date: Optional[str] = None) -> None:
        """
        Отметить привычку сделанной на date (ISO YYYY-MM-DD). По умолчанию сегодня.
        """
        assert self.conn is not None
        if date is None:
            date = datetime.date.today().isoformat()
        await self.conn.execute(
            "INSERT OR REPLACE INTO progress (habit_id, date, status) VALUES (?, ?, 1)",
            (habit_id, date),
        )
        await self.conn.commit()

    async def get_progress_for_habit(self, habit_id: int, start_date: Optional[str] = None, end_date: Optional[str] = None) -> List[dict]:
        assert self.conn is not None
        if start_date and end_date:
            cur = await self.conn.execute(
                "SELECT * FROM progress WHERE habit_id = ? AND date BETWEEN ? AND ? ORDER BY date",
                (habit_id, start_date, end_date),
            )
        elif start_date:
            cur = await self.conn.execute(
                "SELECT * FROM progress WHERE habit_id = ? AND date >= ? ORDER BY date",
                (habit_id, start_date),
            )
        else:
            cur = await self.conn.execute(
                "SELECT * FROM progress WHERE habit_id = ? ORDER BY date",
                (habit_id,),
            )
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

    # ---------- Helpers / Reports ----------
    def _row_to_habit_dict(self, row: aiosqlite.Row) -> dict:
        if row is None:
            return None
        d = dict(row)
        schedule = d.get("schedule")
        d["schedule"] = json.loads(schedule) if schedule else None
        return d

    async def get_today_habits(self, user_id: int) -> List[dict]:
        assert self.conn is not None
        all_habits = await self.get_habits(user_id)
        today_wd = datetime.date.today().weekday() 
        today = []
        for h in all_habits:
            freq = h.get("frequency", "daily")
            schedule = h.get("schedule")
            if freq == "daily":
                today.append(h)
            elif freq == "weekly":
                if schedule is None:
                    today.append(h)  
                else:
                    try:
                        if int(today_wd) in [int(x) for x in schedule]:
                            today.append(h)
                    except Exception:
                        today.append(h)
            else:
                today.append(h)
        return today

    async def get_user_progress_summary(self, user_id: int, start_date: str, end_date: str) -> List[Dict[str, Any]]:
        """
        Возвращает список привычек пользователя с количеством выполненных дней в диапазоне
        """
        assert self.conn is not None
        q = """
        SELECT h.id as habit_id, h.name, COUNT(p.id) as done_count
        FROM habits h
        LEFT JOIN progress p ON p.habit_id = h.id AND p.date BETWEEN ? AND ?
        WHERE h.user_id = ?
        GROUP BY h.id, h.name
        ORDER BY done_count DESC;
        """
        cur = await self.conn.execute(q, (start_date, end_date, user_id))
        rows = await cur.fetchall()
        return [dict(r) for r in rows]

