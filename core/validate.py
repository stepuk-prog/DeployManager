"""Валидация соответствия service-файлов проекта и записей program.programdata.

Сверяем:
  • service_name (имя файла) — есть ли запись в programdata;
  • folder (БД) ↔ WorkingDirectory/ExecStart (файл).
При несоответствии — предупреждение и интерактивный запрос: что менять (БД, файл, пропустить, отмена).
"""
import glob
import os
import re
from dataclasses import dataclass

from core import ui
from database import Database
from logs import get_logger
from settings import config

logger = get_logger(__name__)

_WORKDIR_RE = re.compile(r"^WorkingDirectory=(.+)$", re.MULTILINE)
_EXEC_RE = re.compile(r"^ExecStart=(.+)$", re.MULTILINE)
_RESTART_RE = re.compile(r"^Restart=(.+)$", re.MULTILINE)


@dataclass
class LocalService:
    name: str            # имя файла, напр. binodex-1m-otc.service
    path: str            # абсолютный путь к файлу
    working_dir: str | None
    exec_start: str | None  # строка ExecStart= целиком
    restart: str | None  # значение Restart= (None если строки нет → systemd по умолч. 'no')
    is_template: bool    # шаблонный юнит (имя с '@')

    @property
    def restart_enabled(self) -> bool:
        """systemd-автоперезапуск включён (любое значение, кроме 'no')."""
        return bool(self.restart) and self.restart.strip().lower() != "no"

    @property
    def venv_dir(self) -> str | None:
        """Имя каталога venv из ExecStart (компонент перед '/bin/'). На серверах ожидается 'venv'."""
        if not self.exec_start:
            return None
        binpath = self.exec_start.split()[0]  # первый токен — путь к интерпретатору
        parts = binpath.split("/")
        if "bin" in parts:
            i = parts.index("bin")
            if i > 0:
                return parts[i - 1]
        return None


def parse_service_file(path: str) -> LocalService:
    text = open(path, encoding="utf-8").read()
    wd = _WORKDIR_RE.search(text)
    ex = _EXEC_RE.search(text)
    rs = _RESTART_RE.search(text)
    name = os.path.basename(path)
    return LocalService(
        name=name, path=path,
        working_dir=wd.group(1).strip() if wd else None,
        exec_start=ex.group(1).strip() if ex else None,
        restart=rs.group(1).strip() if rs else None,
        is_template="@" in name,
    )


def list_local_services(project_dir: str) -> list[LocalService]:
    paths = sorted(glob.glob(os.path.join(project_dir, "systemd", "*.service")))
    return [parse_service_file(p) for p in paths]


def _rewrite_service_path(svc: LocalService, new_folder: str) -> None:
    """Заменить путь установки в WorkingDirectory и префиксе ExecStart на new_folder."""
    text = open(svc.path, encoding="utf-8").read()
    old = (svc.working_dir or "").rstrip("/")
    new = new_folder.rstrip("/")
    if old:
        text = text.replace(old, new)
    text = _WORKDIR_RE.sub(f"WorkingDirectory={new}", text)
    with open(svc.path, "w", encoding="utf-8") as f:
        f.write(text)
    svc.working_dir = new
    logger.info("Файл %s: путь → %s", svc.name, new)


def _set_restart_no(svc: LocalService) -> None:
    """Выключить автоперезапуск: Restart=<...> → Restart=no (для совместимости с Dispatcher)."""
    text = open(svc.path, encoding="utf-8").read()
    text = _RESTART_RE.sub("Restart=no", text)
    with open(svc.path, "w", encoding="utf-8") as f:
        f.write(text)
    svc.restart = "no"
    logger.info("Файл %s: Restart → no", svc.name)


def _set_venv(svc: LocalService, old: str, new: str) -> None:
    """Заменить каталог venv в ExecStart: /<old>/bin/ → /<new>/bin/."""
    text = open(svc.path, encoding="utf-8").read()
    text = text.replace(f"/{old}/bin/", f"/{new}/bin/")
    with open(svc.path, "w", encoding="utf-8") as f:
        f.write(text)
    logger.info("Файл %s: venv %s → %s в ExecStart", svc.name, old, new)


async def _check_venv(svc: LocalService) -> None:
    """ExecStart должен указывать на серверный venv (config.VENV_DIR, обычно 'venv', не '.venv')."""
    vd = svc.venv_dir
    if not vd or vd == config.VENV_DIR:
        return
    print(f"  {svc.name:26} ⚠️ ExecStart использует venv '{vd}', "
          f"а на серверах ожидается '{config.VENV_DIR}'")
    if await ui.confirm(f"{svc.name}: venv в ExecStart '{vd}' ≠ ожидаемого '{config.VENV_DIR}'. "
                        f"Исправить (/{vd}/bin → /{config.VENV_DIR}/bin)?"):
        _set_venv(svc, vd, config.VENV_DIR)
        print("      ✅ Исправлено.")
    else:
        print("      ⏭️  Оставлено как есть.")


async def _check_restart(svc: LocalService) -> None:
    """Restart должен быть выключен под Dispatcher. При включённом — предупредить и спросить."""
    if not svc.restart_enabled:
        return
    print(f"  {svc.name:26} ⚠️ Restart={svc.restart} (включён)")
    print("      Под Dispatcher автоперезапуск systemd должен быть ВЫКЛЮЧЕН (Restart=no):")
    print("      иначе systemd сам поднимет упавший сервис и сломает failover диспетчера.")
    if await ui.confirm(f"{svc.name}: Restart={svc.restart} (включён), под Dispatcher должен быть "
                        f"выключен. Исправить на Restart=no?"):
        _set_restart_no(svc)
        print("      ✅ Исправлено.")
    else:
        print("      ⏭️  Оставлено как есть.")


def _norm(p: str | None) -> str:
    return (p or "").rstrip("/")


def _is_blank(p: str | None) -> bool:
    """folder в БД пуст: NULL или пустая строка/пробелы."""
    return not (p or "").strip()


def _path_diff_note(file_path: str | None, db_path: str | None) -> str:
    """Короткая пометка, чем отличаются пути, если различие лишь косметическое
    (регистр / пробелы / лишние слэши). Пустая строка — пути расходятся по существу
    (разные каталоги). На Linux пути регистрозависимы, поэтому такие различия —
    всё равно расхождение, но оператору важно видеть его природу."""
    a, b = file_path or "", db_path or ""
    collapse = lambda s: re.sub(r"/+", "/", s.strip()).rstrip("/")
    ca, cb = collapse(a), collapse(b)
    if ca.casefold() != cb.casefold():
        return ""  # действительно разные пути — без подсказки
    kinds = []
    if ca != cb:
        kinds.append("регистре")
    if a.strip() != a or b.strip() != b:
        kinds.append("пробелах")
    if collapse(a) != a.strip().rstrip("/") or collapse(b) != b.strip().rstrip("/"):
        kinds.append("слэшах")
    return f" (отличие только в {', '.join(kinds)})" if kinds else ""


async def validate_paths(db: Database, project_dir: str,
                         only: set[str] | None = None) -> bool:
    """Сверка + интерактивное разрешение. True — можно деплоить, False — отмена.
    only — если задан, валидируем лишь эти service-файлы (+ шаблоны), остальные пропускаем."""
    local = list_local_services(project_dir)
    if only is not None:
        local = [s for s in local if s.name in only]  # шаблоны (@) сюда не попадают — они вне деплой-флоу
    if not local:
        print("⚠️  В systemd/ нет service-файлов — нечего валидировать.")
        return True

    names = [s.name for s in local if not s.is_template]
    records = await db.find_programs_by_service(names)
    db_by_name = {r["service_name"]: r for r in records}

    print("\n── Валидация service-файлов ↔ programdata ──")
    all_ok = True
    for svc in local:
        await _check_venv(svc)     # ExecStart → серверный venv (не .venv)
        await _check_restart(svc)  # Restart должен быть выключен под Dispatcher (для всех юнитов)
        if svc.is_template:
            print(f"  {svc.name:26} — шаблон (в БД не проверяется)")
            continue
        rec = db_by_name.get(svc.name)
        if rec is None:
            print(f"  {svc.name:26} ❌ нет записи в programdata")
            all_ok = False
            if not await _resolve_missing(db, svc):
                return False
            continue
        if _is_blank(rec["folder"]):
            all_ok = False
            print(f"  {svc.name:26} ⚠️ в БД folder = NULL (пусто)")
            if not await _resolve_null_folder(db, svc, rec):
                return False
        elif _norm(svc.working_dir) == _norm(rec["folder"]):
            print(f"  {svc.name:26} ✅ путь совпадает ({rec['folder']})")
        else:
            all_ok = False
            note = _path_diff_note(svc.working_dir, rec["folder"])
            print(f"  {svc.name:26} ❌ путь расходится{note}:")
            print(f"      файл: {svc.working_dir}")
            print(f"      БД:   {rec['folder']}")
            if not await _resolve_mismatch(db, svc, rec):
                return False

    if all_ok:
        print("Все пути согласованы.\n")
    return True


async def _resolve_missing(db: Database, svc: LocalService) -> bool:
    """Юнита нет в programdata. Возвращает True — продолжать деплой, False — отмена."""
    from core.programdata import create_record_interactive
    if not ui.INTERACTIVE:            # неинтерактив (--yes/CLI): безопасно отменяем
        return False
    idx = await ui.select(
        f"{svc.name} — не обнаружен в programdata. Что делаем?",
        ["✅ Создать запись", "➕ Добавить позже"], default_index=0)
    if idx == 0:
        await create_record_interactive(db, service_name=svc.name, folder=svc.working_dir)
        return True
    if idx == 1:
        print("    ⏭️  Продолжаю без записи (добавишь отдельно).")
        return True
    print("    🛑 Отмена.")
    return False


async def _resolve_null_folder(db: Database, svc: LocalService, rec) -> bool:
    """В БД folder = NULL/пусто. Предложить записать путь из service-файла. True — продолжать."""
    if not ui.INTERACTIVE:
        return False
    idx = await ui.select(
        f"{svc.name}: в БД folder = NULL (пусто). Что делаем?",
        ["📝 Записать путь из файла", "⏭️ Пропустить"], default_index=1)
    if idx == 0:
        if _is_blank(svc.working_dir):
            print("    ⚠️ В service-файле тоже нет WorkingDirectory — нечего записать.")
            return await _resolve_null_folder(db, svc, rec)
        await db.update_program_folder(rec["program_id"], _norm(svc.working_dir))
        print("    ✅ БД обновлена.")
        return True
    if idx == 1:
        print("    ⏭️  Пропущено (folder остался NULL).")
        return True
    print("    🛑 Отмена.")
    return False


async def _resolve_mismatch(db: Database, svc: LocalService, rec) -> bool:
    """Интерактивно разрешить расхождение пути. Возвращает True — продолжать."""
    if not ui.INTERACTIVE:
        return False
    idx = await ui.select(
        f"{svc.name}: путь в файле ({svc.working_dir}) ≠ в БД ({rec['folder']}). Что менять?",
        ["📝 БД → путь из файла", "📄 Файл → путь из БД", "⏭️ Пропустить"], default_index=2)
    if idx == 0:
        await db.update_program_folder(rec["program_id"], _norm(svc.working_dir))
        print("    ✅ БД обновлена.")
        return True
    if idx == 1:
        _rewrite_service_path(svc, rec["folder"])
        print("    ✅ Файл обновлён.")
        return True
    if idx == 2:
        print("    ⏭️  Пропущено (расхождение осталось).")
        return True
    print("    🛑 Отмена.")
    return False
