"""Ветки группы «Персонажи форума»: аккаунты ИИ-персонажей из БД forum (`person.account`).

Вторая группа юзерботов. От флотовой отличается всем, кроме самого логина: другая база,
другой набор Telegram Desktop (одна программа + `-workdir` слота), две колонки сессий
(pyrofork и Telethon) и привязка не к программе, а к персонажу (`person.persona`).
Общий с флотом только `session.export_session` — сам механизм авторизации.

Пул к БД forum поднимается ЗДЕСЬ и закрывается на выходе: инструмент открыт часами, а
вторая база нужна только внутри этой группы.
"""
import asyncio

from core import ui
from database.person import PersonDatabase
from settings import config
from tools.sessions import apps, person_tg, session

_ACTIONS = [
    ("list",     "📋 Аккаунты персонажей"),
    ("check",    "🔍 Проверить сессии (getMe)"),
    ("recover",  "♻️ Обновить / создать сессию"),
    ("privacy",  "🔒 Приватность аккаунта"),
    ("profile",  "🪪 Профиль: имя / username / фото"),
    ("personas", "🎭 Персонажи и их черты"),
]

# Пауза между аккаунтами при массовом обходе (сек). Как в аудите премиума: подключения
# идут с одного домашнего IP, и очередь из трёх десятков подряд лучше растянуть.
_PAUSE = 2


def _label(row) -> str:
    """Строка аккаунта для списка выбора: слот, телефон, имя, статус, персонаж."""
    slot = f"p{row['slot']:02d}.{row['slot_pos']}" if row["slot"] else "—"
    who = f" · {row['persona_key']}" if row["persona_key"] else ""
    name = row["full_name"] or "—"
    return f"{slot:<7} {row['phone']:<16} {name[:24]:<24} {row['status']}{who}"


async def _pick(db: PersonDatabase, title: str, only_slotted: bool = False):
    """Выбор одного аккаунта из реестра. None — оператор отменил."""
    rows = await db.list_accounts(only_slotted=only_slotted)
    if not rows:
        print("В person.account нет аккаунтов.")
        return None
    idx = await ui.combobox(title, [_label(r) for r in rows])
    if idx is None:
        print("✖️ Отменено.")
        return None
    return rows[idx]


async def _list(db: PersonDatabase) -> None:
    """Карта аккаунтов: где лежит, жив ли, кем занят."""
    rows = await db.list_accounts()
    if not rows:
        print("В person.account нет аккаунтов.")
        return
    print(f"\nАккаунтов персонажей: {len(rows)}\n")
    print(f"{'слот':<7} {'телефон':<16} {'имя':<24} {'статус':<8} "
          f"{'pyro':<5} {'tele':<5} {'2fa':<4} персонаж")
    print("-" * 104)
    for r in rows:
        slot = f"p{r['slot']:02d}.{r['slot_pos']}" if r["slot"] else "—"
        print(f"{slot:<7} {r['phone']:<16} {(r['full_name'] or '—')[:24]:<24} "
              f"{r['status']:<8} {'✅' if r['has_session'] else '—':<5} "
              f"{'✅' if r['has_telethon'] else '—':<5} {'✅' if r['has_2fa'] else '—':<4} "
              f"{r['persona_key'] or '—'}")
    ready = sum(1 for r in rows if r["status"] == "ready")
    dead = sum(1 for r in rows if r["status"] == "dead")
    print(f"\nГотовых: {ready} · мёртвых: {dead} · всего: {len(rows)}.")


async def _check(db: PersonDatabase) -> None:
    """Проверить живость сессий: один `get_me()` на аккаунт, профиль и статус — в реестр.

    Мёртвая сессия здесь не авария: она гасит СВОЙ аккаунт (status=dead), персонаж на нём
    замолкает, остальные работают. Поэтому проверка массовая и безопасная."""
    rows = await db.list_accounts()
    rows = [r for r in rows if r["has_session"]]
    if not rows:
        print("Аккаунтов с session_string нет.")
        return
    idx = await ui.select(f"Что проверяем? Аккаунтов с сессией: {len(rows)}",
                          ["Все", "Только мёртвые (перепроверить)", "Один аккаунт"])
    if idx is None:
        print("✖️ Отменено.")
        return
    if idx == 1:
        rows = [r for r in rows if r["status"] == "dead"]
    elif idx == 2:
        one = await _pick(db, "Какой аккаунт проверяем?")
        if one is None:
            return
        rows = [one]
    if not rows:
        print("Нечего проверять.")
        return

    print(f"\nПроверяю {len(rows)} аккаунт(ов), пауза {_PAUSE} с между ними…\n")
    alive = dead = 0
    for i, row in enumerate(rows):
        if i:
            await asyncio.sleep(_PAUSE)
        full = await db.get_account(row["phone"])
        ok, msg, me = await person_tg.check_alive(full)
        slot = f"p{row['slot']:02d}.{row['slot_pos']}" if row["slot"] else "—"
        if ok and me:
            await db.update_profile(row["id"], me["user_id"], me["first_name"],
                                    me["last_name"], me["username"])
            name = f"{me['first_name']} {me['last_name']}".strip()
            print(f"{slot:<7} {row['phone']:<16} ✅ {name[:30]:<30} "
                  f"{'@' + me['username'] if me['username'] else ''}")
            alive += 1
        else:
            await db.mark_dead(row["id"], msg)
            print(f"{slot:<7} {row['phone']:<16} ❌ {msg[:70]}")
            dead += 1
    print(f"\nЖивых: {alive} · мёртвых: {dead}.")


async def _launch_desktop(row) -> bool:
    """Поднять Desktop-слот персонажа (общая программа + `-workdir pNN`) ради кода входа.

    Справочник `telegram.telegram_apps` здесь ни при чём — у персонажей клиенты заводятся
    скриптом `new-persona-gram`, а слот записан прямо в аккаунте. Сбой запуска логин НЕ
    срывает: код может прийти в SMS или в уже открытый клиент."""
    if not row["slot"]:
        print("ℹ️ Слот Desktop не задан — запуск клиента пропущен.")
        return True
    workdir = f"{config.PERSON_TG_WORKDIR}/p{row['slot']:02d}"
    print(f"🚀 Поднимаю Telegram-слот p{row['slot']:02d} ({workdir})…")
    ok = apps._spawn(config.PERSON_TG_EXEC, workdir)                 # noqa: SLF001
    msg = (f"🚀 Слот p{row['slot']:02d} загружен. Возьмите код подтверждения и нажмите «Дальше»."
           if ok else "⚠️ Клиент не поднялся. Продолжить логин вручную?")
    return await ui.confirm(msg, ok_label="➡️ Дальше", cancel_label="✖️ Отмена")


async def _recover(db: PersonDatabase) -> None:
    """Логин аккаунта персонажа → свежая сессия в person.account.

    Пишем в ту колонку, формат которой выбрали: pyrofork — рабочая для проекта,
    Telethon — запасная (её отдаёт продавец аккаунтов). Перезапись подтверждается:
    вторая сессия того же аккаунта разлогинивает первую не всегда, но потерять
    единственную рабочую строку легко."""
    row = await _pick(db, "Какому аккаунту делаем сессию?")
    if row is None:
        return
    full = await db.get_account(row["phone"])
    if not full["api_id"] or not full["api_hash"]:
        print("❌ У аккаунта нет api_id/api_hash — логин невозможен. "
              "Впишите их в person.account (они приходят вместе с аккаунтом).")
        return
    if full["persona_key"]:
        if not await ui.confirm(
                f"Аккаунт занят персонажем «{full['persona_key']}». Продолжать?",
                danger=True, ok_label="✅ Да", cancel_label="✖️ Отмена"):
            print("✖️ Отменено.")
            return

    fmt = await ui.select("Формат сессии", ["pyrofork (рабочая)", "Telethon (запасная)"])
    if fmt is None:
        print("✖️ Отменено.")
        return
    telethon = fmt == 1

    if not await _launch_desktop(full):
        print("✖️ Отменено.")
        return

    if full["twofa_password"]:
        print(f"🔑 Облачный пароль этого аккаунта: {full['twofa_password']}")

    try:
        result = await session.export_session(full["api_id"], full["api_hash"],
                                              full["phone"], telethon=telethon)
    except session.SessionError as error:
        print(f"❌ {error}")
        return
    if result is None:
        return
    me, session_string = result

    if full["user_id"] and me.id != full["user_id"]:
        print(f"⚠️⚠️ Вошли в аккаунт id={me.id}, а в реестре у этой строки user_id="
              f"{full['user_id']}. Это РАЗНЫЕ аккаунты!")
        if not await ui.confirm("Всё равно записать сессию в выбранную строку?", danger=True):
            print("✖️ Не записано.")
            return

    column = "session_telethon" if telethon else "session_string"
    if full[column]:
        if not await ui.confirm(f"В реестре уже есть {column}. Перезаписать?"):
            print("✖️ Не записано.")
            return
    if not await ui.confirm(f"Записать сессию для {full['phone']} ({column})?"):
        print("✖️ Не записано.")
        return

    res = await db.save_session(full["id"], session_string, telethon=telethon)
    print(f"✅ {column} записан ({res}); статус аккаунта → ready.")


async def _privacy(db: PersonDatabase) -> None:
    """Показать настройки приватности и, по команде, привести их к норме персонажа."""
    idx = await ui.select("Чью приватность смотрим?", ["Один аккаунт", "Все со слотом"])
    if idx is None:
        print("✖️ Отменено.")
        return
    if idx == 0:
        row = await _pick(db, "Какой аккаунт?")
        if row is None:
            return
        rows = [row]
    else:
        rows = [r for r in await db.list_accounts(only_slotted=True) if r["has_session"]]
    if not rows:
        print("Нечего смотреть: нет аккаунтов с сессией.")
        return

    harden = await ui.confirm(
        f"Аккаунтов: {len(rows)}. Сразу приводить несоответствия к норме?\n"
        f"Норма: номер — никому, звонки — никому, добавление в чаты и время захода — "
        f"контакты, пересылки и фото — все.",
        ok_label="🔒 Смотреть и чинить", cancel_label="👀 Только посмотреть")

    for i, row in enumerate(rows):
        if i:
            await asyncio.sleep(_PAUSE)
        full = await db.get_account(row["phone"])
        slot = f"p{row['slot']:02d}.{row['slot_pos']}" if row["slot"] else "—"
        print(f"\n{slot}  {row['phone']}  {(row['full_name'] or '—')[:24]}")
        try:
            items = await person_tg.privacy(full, harden=harden)
        except (Exception,) as error:                                # noqa: BLE001
            print(f"   ❌ сессия не поднялась — {type(error).__name__}: {error}")
            await db.mark_dead(row["id"], f"{type(error).__name__}: {error}")
            continue
        for label, value, ok in items:
            print(f"   {label:<18} {value}{'' if ok else '   ← не норма'}")


async def _profile(db: PersonDatabase) -> None:
    """Оформление личности в самом Telegram: имя, @username, био, фото.

    ⚠️ Имя ставится ОДИН раз и осознанно: Telegram хранит историю смен, и персонаж,
    переименовывающийся раз в неделю, подозрительнее персонажа с чужим именем. Пустые
    ответы означают «не трогать это поле» — так можно поменять только фото или только ник.
    Заодно синхронизируем реестр: `person.account` должен показывать то же, что Telegram."""
    row = await _pick(db, "Чей профиль оформляем?")
    if row is None:
        return
    full = await db.get_account(row["phone"])
    persona = f" · персонаж {full['persona_key']}" if full["persona_key"] else ""
    try:
        cur = await person_tg.get_profile(full)
    except (Exception,) as error:                                    # noqa: BLE001
        print(f"❌ сессия не поднялась — {type(error).__name__}: {error}")
        await db.mark_dead(row["id"], f"{type(error).__name__}: {error}")
        return

    print(f"\nСейчас в Telegram{persona}:")
    print(f"   имя        {cur['first_name']} {cur['last_name']}".rstrip())
    print(f"   username   {'@' + cur['username'] if cur['username'] else '—'}")
    print(f"   био        {cur['bio'] or '—'}")
    print(f"   фото       {'есть' if cur['has_photo'] else 'НЕТ (пустая аватарка заметна)'}")

    first = await ui.ask("Имя (пусто — не менять)", default="", cancelable=True)
    if first is None:
        print("✖️ Отменено.")
        return
    last = await ui.ask("Фамилия (пусто — не менять; «-» — убрать)", default="", cancelable=True)
    if last is None:
        print("✖️ Отменено.")
        return
    uname = await ui.ask("Username без @ (пусто — не менять; «-» — убрать)",
                         default="", cancelable=True)
    if uname is None:
        print("✖️ Отменено.")
        return
    bio = await ui.ask("Био (пусто — не менять; «-» — очистить)", default="", cancelable=True)
    if bio is None:
        print("✖️ Отменено.")
        return
    photo = await ui.ask("Путь к файлу аватарки (пусто — не менять)", default="", cancelable=True)
    if photo is None:
        print("✖️ Отменено.")
        return

    first = first.strip() or None
    last = "" if last.strip() == "-" else (last.strip() or None)
    uname = "" if uname.strip() == "-" else (uname.strip().lstrip("@") or None)
    bio = "" if bio.strip() == "-" else (bio.strip() or None)
    photo = photo.strip() or None
    if not any(v is not None for v in (first, last, uname, bio, photo)):
        print("Нечего менять.")
        return

    plan = [f"имя «{first}»" if first is not None else "",
            f"фамилия «{last or '(убрать)'}»" if last is not None else "",
            f"@{uname}" if uname else ("username убрать" if uname == "" else ""),
            f"био «{bio or '(очистить)'}»" if bio is not None else "",
            f"фото {photo}" if photo else ""]
    plan = [p for p in plan if p]
    if not await ui.confirm("Меняем: " + "; ".join(plan) + f"\nАккаунт {full['phone']}"
                            f"{persona}. Имя в Telegram меняют ОДИН раз — продолжаем?"):
        print("✖️ Отменено.")
        return

    try:
        done = await person_tg.set_profile(full, first_name=first, last_name=last,
                                           bio=bio, username=uname, photo_path=photo)
    except (Exception,) as error:                                    # noqa: BLE001
        print(f"❌ Telegram отказал — {type(error).__name__}: {error}")
        return
    for line in done:
        print(f"   ✅ {line}")

    after = await person_tg.get_profile(full)
    await db.update_profile(row["id"], after["user_id"], after["first_name"],
                            after["last_name"], after["username"])
    print("✅ Реестр person.account синхронизирован с Telegram.")


async def _personas(db: PersonDatabase) -> None:
    """Карточки персонажей: кто на каком аккаунте и какими числами задан характер."""
    rows = await db.list_personas()
    if not rows:
        print("Персонажи не заведены (person.persona пуста).")
        return
    print(f"\nПерсонажей: {len(rows)}\n")
    for r in rows:
        state = "включён" if r["enabled"] else "выключен"
        premod = "премодерация" if r["premoderate"] else "автономия"
        slot = f"p{r['slot']:02d}" if r["slot"] else "—"
        print(f"🎭 {r['key']} ({r['role']}) — {r['nick'] or '—'}")
        print(f"   аккаунт {r['phone']} · слот {slot} · сессия {r['account_status'] or '—'}")
        print(f"   {state}, {premod}")
        print(f"   {r['traits'] or 'черты не выставлены'}\n")


async def run(action: str | None = None) -> None:
    """Под-меню группы «Персонажи». Свой пул к БД forum — поднимаем и закрываем здесь."""
    if not action:
        idx = await ui.select("Персонажи форума — что делаем?", [label for _, label in _ACTIONS])
        if idx is None:
            print("✖️ Отменено.")
            return
        action = _ACTIONS[idx][0]

    db = PersonDatabase()
    try:
        await db.connect()
        if action == "list":
            await _list(db)
        elif action == "check":
            await _check(db)
        elif action == "recover":
            await _recover(db)
        elif action == "privacy":
            await _privacy(db)
        elif action == "profile":
            await _profile(db)
        elif action == "personas":
            await _personas(db)
        else:
            print(f"Неизвестное действие: {action}")
    finally:
        await db.close()
