"""Восстановление сессии существующего юзербота: логин → заливка session_string в БД.

Юзербот уже есть строкой в telegram.telegram; задача — получить свежий session_string
(старый пуст/протух) и записать. Если в строке нет api_id/api_hash — оператор вписывает
их здесь же (без них логин невозможен), и они дописываются в БД.
"""
from core import ui
from database.db import Database
from tools.sessions import apps, session


async def _ask_int(prompt: str, default: str = "") -> int | None:
    """Запросить целое (api_id). None — оператор отменил; повтор при нечисловом вводе."""
    while True:
        raw = await ui.ask(prompt, default=default, cancelable=True)
        if raw is None:
            return None
        raw = raw.strip()
        if raw.isdigit():
            return int(raw)
        print("❌ Нужно целое число (api_id).")


async def recover_session(db: Database, id_telegram: int | None = None,
                          telethon: bool = False, only_without_session: bool = False) -> None:
    """Залогинить юзербота и записать session_string. id_telegram задан (GUI выбрал во
    встроенной открывашке) → выбор пропускаем; None (CLI) → показываем combobox.
    only_without_session=False (ветка «Обновить») → список юзерботов С сессией, подписи
    «Программа — Имя»; True (ветка «Создать») → список БЕЗ сессии, подписи — только имя.
    telethon=True — записать строку в формате Telethon (та же колонка session_string)."""
    if id_telegram is None:
        rows = await db.list_userbots(having_session=not only_without_session)
        if not rows:
            print("Нет аккаунтов без сессии." if only_without_session
                  else "В telegram.telegram нет юзерботов с непустым session_string.")
            return
        title = ("Выбери юзербота для создания сессии" if only_without_session
                 else "Выбери юзербота для восстановления сессии")
        labels = ([f"{r['name']} ({r['programs']})" if r["programs"] else r["name"]
                   for r in rows] if only_without_session
                  else [f"{r['programs']} — {r['name']}" if r["programs"]
                        else f"(без программы) — {r['name']}" for r in rows])
        idx = await ui.combobox(title, labels)
        if idx is None:
            print("✖️ Отменено.")
            return
        id_telegram = rows[idx]["id_telegram"]
    ub = await db.get_userbot(id_telegram)
    if ub is None:
        print(f"❌ Юзербот id_telegram={id_telegram} не найден.")
        return
    print(f"Юзербот: {ub['name']} (id_telegram={ub['id_telegram']})")

    # «Создать»: сразу после выбора (до запуска клиента/логина) предупредить, если аккаунт
    # уже привязан к программе (program.programdata.user_bot). При «Обновить» не проверяем —
    # там привязка к программе нормальна (сессию просто освежаем).
    if only_without_session:
        used_by = await db.programs_using(ub["id_telegram"])
        if used_by:
            if not await ui.confirm(
                    f"Данный юзербот используется программой {', '.join(used_by)}. Продолжать?",
                    danger=True, ok_label="✅ Да", cancel_label="✖️ Отмена"):
                print("✖️ Отменено.")
                return

    # поднять Desktop-клиент (my_gram) — оттуда оператор возьмёт код; «Отмена» = выход
    if not await apps.launch_for(db, ub["my_gram"]):
        print("✖️ Отменено.")
        return

    # api-креды: если пусты — спросить (и потом дописать в БД). Хэш из БД триммим:
    # хвостовой пробел/перенос строки → API_ID_INVALID при send_code.
    creds_changed = False
    api_id = ub["api_id"]
    api_hash = (ub["api_hash"] or "").strip()
    if api_hash != (ub["api_hash"] or ""):
        creds_changed = True            # в БД лежал хэш с мусором — перезапишем очищенный
    if not api_id:
        api_id = await _ask_int("api_id (в БД пусто)")
        if api_id is None:
            print("✖️ Отменено.")
            return
        creds_changed = True
    if not api_hash:
        api_hash = await ui.ask("api_hash (в БД пусто)", cancelable=True)
        if api_hash is None:
            print("✖️ Отменено.")
            return
        api_hash = api_hash.strip()
        creds_changed = True
    phone = await ui.ask("Телефон (для логина)", default=ub["phone"] or "", cancelable=True)
    if phone is None:
        print("✖️ Отменено.")
        return
    phone = phone.strip()
    if phone != (ub["phone"] or ""):
        creds_changed = True

    # логин с повтором: при неверной паре api_id/api_hash переспрашиваем и пробуем снова
    while True:
        try:
            result = await session.export_session(api_id, api_hash, phone, telethon=telethon)
        except session.ApiCredentialsError as e:
            print(f"❌ {e}")
            new_api_id = await _ask_int("api_id (верный, с my.telegram.org)",
                                        default=str(api_id or ""))
            if new_api_id is None:
                print("✖️ Отменено.")
                return
            new_hash = await ui.ask("api_hash (верный)", default=api_hash, cancelable=True)
            if new_hash is None:
                print("✖️ Отменено.")
                return
            api_id, api_hash = new_api_id, new_hash.strip()
            creds_changed = True
            continue
        break
    if result is None:
        return
    me, session_string = result

    # Защита: вошли НЕ в тот аккаунт, что в строке БД → запись под чужой id_telegram опасна.
    if me.id != ub["id_telegram"]:
        print(f"⚠️⚠️ Вошли в аккаунт id={me.id}, а строка БД — id_telegram={ub['id_telegram']}. "
              f"Это РАЗНЫЕ аккаунты!")
        if not await ui.confirm("Всё равно записать session_string в выбранную строку?",
                                danger=True):
            print("✖️ Не записано.")
            return

    if ub["session_string"]:
        if not await ui.confirm("В БД уже есть session_string. Перезаписать?"):
            print("✖️ Не записано.")
            return

    if not await ui.confirm(f"Записать свежую сессию для {ub['name']} (id_telegram={ub['id_telegram']})?"):
        print("✖️ Не записано.")
        return

    if creds_changed:
        await db.update_creds(ub["id_telegram"], api_id, api_hash, phone)
        print("✅ api_id/api_hash/phone обновлены в БД.")
    res = await db.save_session_string(ub["id_telegram"], session_string)
    print(f"✅ session_string записан ({res}).")
