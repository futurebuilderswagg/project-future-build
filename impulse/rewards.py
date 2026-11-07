"""Баллы, бонусы, уровни, серия дней и значки."""
from datetime import datetime, timedelta

from impulse.config import (
    BADGES, BONUSES, DISPLAY_TZ, PASS_PERCENT, POINTS_PER_LEVEL, RANKS, SNIPER_BONUS, STREAK_BADGES,
    STREAK_MILESTONE_BONUS, STREAK_WEEK_BONUS, TEST_BONUS_PASS, TEST_BONUS_PERFECT,
)
from impulse.database import get_connection, grant_test_bonus
from impulse.utils import utc_now, utc_text

def get_bonus(key: str) -> dict | None:
    return next((bonus for bonus in BONUSES if bonus["key"] == key), None)


def get_earned_points(class_id: int, student_id: int) -> int:
    """Заработано всего: текущий баланс плюс баллы в действующих заявках."""
    with get_connection() as conn:
        balance = conn.execute("SELECT points FROM class_members WHERE class_id = ? AND student_id = ?",
                               (class_id, student_id)).fetchone()
        spent = conn.execute("""
            SELECT COALESCE(SUM(cost), 0) AS total FROM bonus_orders
            WHERE class_id = ? AND student_id = ? AND status != 'rejected'
        """, (class_id, student_id)).fetchone()
    return (balance["points"] if balance else 0) + spent["total"]


def format_level(earned: int) -> str:
    level = 1 + earned // POINTS_PER_LEVEL
    name = next(title for start, title in reversed(RANKS) if level >= start)
    upcoming = next(((start, title) for start, title in RANKS if start > level), None)
    if upcoming:
        left = (upcoming[0] - 1) * POINTS_PER_LEVEL - earned
        return f"{level}, звание «{name}». До звания «{upcoming[1]}» осталось баллов: {left}."
    return f"{level}, звание «{name}». Это высшее звание."


def update_streak(student_id: int, today=None) -> tuple[int, bool]:
    """Серия «Импульс»: один пропущенный день в неделю не обрывает её."""
    today = today or datetime.now(DISPLAY_TZ).date()
    week = today.strftime("%G-%V")
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("SELECT * FROM student_streaks WHERE student_id = ?", (student_id,)).fetchone()
        current, best, freeze_week, last = 0, 0, None, None
        if row:
            current, best, freeze_week = row["current"], row["best"], row["freeze_week"]
            last = datetime.fromisoformat(row["last_date"]).date() if row["last_date"] else None
        gap = (today - last).days if last else None
        if gap == 0:
            return current, False
        if gap == 2 and freeze_week != week:
            current, freeze_week = current + 1, week
        elif gap == 1:
            current += 1
        else:
            current = 1
        conn.execute("""
            INSERT INTO student_streaks (student_id, current, best, last_date, freeze_week) VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(student_id) DO UPDATE SET current = excluded.current, best = excluded.best,
                last_date = excluded.last_date, freeze_week = excluded.freeze_week
        """, (student_id, current, max(best, current), today.isoformat(), freeze_week))
    return current, True


def award_badge(student_id: int, key: str) -> bool:
    with get_connection() as conn:
        cur = conn.execute("INSERT OR IGNORE INTO student_badges (student_id, badge_key, earned_at) VALUES (?, ?, ?)",
                            (student_id, key, utc_text()))
        conn.commit()
    return cur.rowcount > 0


def get_badges(student_id: int) -> list[str]:
    with get_connection() as conn:
        rows = conn.execute("SELECT badge_key FROM student_badges WHERE student_id = ? ORDER BY earned_at", (student_id,)).fetchall()
    return [BADGES[row["badge_key"]] for row in rows if row["badge_key"] in BADGES]


def get_streak(student_id: int) -> dict:
    with get_connection() as conn:
        row = conn.execute("SELECT current, best FROM student_streaks WHERE student_id = ?", (student_id,)).fetchone()
    return dict(row) if row else {"current": 0, "best": 0}


def apply_test_rewards(test: dict, student_id: int, score: int, total: int) -> list[str]:
    """Начисляет бонусы за результат и серию, выдаёт значки и возвращает строки для ученика."""
    perfect = bool(total) and score == total
    bonuses = []
    if perfect:
        bonuses += [(TEST_BONUS_PERFECT, "тест без ошибок"), (SNIPER_BONUS, "«Снайпер»")]
    elif total and score * 100 >= PASS_PERCENT * total:
        bonuses.append((TEST_BONUS_PASS, f"результат {PASS_PERCENT}% и выше"))
    current, new_day = update_streak(student_id)
    if new_day and current % 7 == 0:
        bonuses.append((STREAK_WEEK_BONUS, f"{current} дней подряд"))
    if new_day and current in STREAK_MILESTONE_BONUS:
        bonuses.append((STREAK_MILESTONE_BONUS[current], f"веха {current} дней"))
    lines = [f"🔥 Импульс: дней подряд {current}."]
    if bonuses and grant_test_bonus(test, student_id, sum(points for points, _ in bonuses)):
        lines.extend(f"🎁 +{points} баллов: {reason}." for points, reason in bonuses)
    keys = ["sniper"] if perfect else []
    if current in STREAK_BADGES:
        keys.append(STREAK_BADGES[current])
    lines.extend(f"🏅 Новый значок. {BADGES[key]}." for key in keys if award_badge(student_id, key))
    return lines


def order_bonus(class_id: int, student_id: int, bonus: dict) -> tuple[int | None, str | None]:
    """Списание и заявка проходят в одной транзакции, двойное нажатие не спишет баллы дважды."""
    now = utc_now()
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        member = conn.execute("SELECT points FROM class_members WHERE class_id = ? AND student_id = ? AND status = 'accepted'",
                              (class_id, student_id)).fetchone()
        if not member:
            return None, "Вы не состоите в этом классе."
        if member["points"] < bonus["cost"]:
            return None, f"Не хватает баллов: нужно {bonus['cost']}, у вас {member['points']}."
        if bonus["limit_days"]:
            since = utc_text(now - timedelta(days=bonus["limit_days"]))
            used = conn.execute("""
                SELECT 1 FROM bonus_orders WHERE class_id = ? AND student_id = ? AND bonus_key = ?
                  AND status != 'rejected' AND created_at >= ?
            """, (class_id, student_id, bonus["key"], since)).fetchone()
            if used:
                return None, f"Этот бонус можно получать не чаще одного раза в {bonus['limit_days']} дней."
        conn.execute("UPDATE class_members SET points = points - ? WHERE class_id = ? AND student_id = ?",
                     (bonus["cost"], class_id, student_id))
        cur = conn.execute("""
            INSERT INTO bonus_orders (class_id, student_id, bonus_key, bonus_name, cost, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
        """, (class_id, student_id, bonus["key"], bonus["name"], bonus["cost"], utc_text(now)))
        return cur.lastrowid, None


def decide_bonus_order(order_id: int, teacher_id: int, approve: bool) -> dict | None:
    with get_connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        order = conn.execute("""
            SELECT o.*, c.name AS class_name, u.full_name, u.max_user_id
            FROM bonus_orders o JOIN classes c ON c.id = o.class_id JOIN users u ON u.id = o.student_id
            WHERE o.id = ? AND c.teacher_id = ? AND o.status = 'pending'
        """, (order_id, teacher_id)).fetchone()
        if not order:
            return None
        conn.execute("UPDATE bonus_orders SET status = ?, decided_at = ? WHERE id = ?",
                     ("approved" if approve else "rejected", utc_text(), order_id))
        if not approve:
            conn.execute("UPDATE class_members SET points = points + ? WHERE class_id = ? AND student_id = ?",
                         (order["cost"], order["class_id"], order["student_id"]))
        return dict(order)


def get_student_orders(class_id: int, student_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT bonus_name, status FROM bonus_orders WHERE class_id = ? AND student_id = ?
            ORDER BY id DESC LIMIT 5
        """, (class_id, student_id)).fetchall()
    return [dict(row) for row in rows]


def get_pending_orders(teacher_id: int) -> list[dict]:
    with get_connection() as conn:
        rows = conn.execute("""
            SELECT o.id, o.bonus_name, o.cost, c.name AS class_name, u.full_name
            FROM bonus_orders o JOIN classes c ON c.id = o.class_id JOIN users u ON u.id = o.student_id
            WHERE c.teacher_id = ? AND o.status = 'pending' ORDER BY o.id LIMIT 10
        """, (teacher_id,)).fetchall()
    return [dict(row) for row in rows]
