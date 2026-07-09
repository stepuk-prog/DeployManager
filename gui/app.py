"""Flet-окно DeployManager. Переиспользует CLI-ядро (cli.run) с GUI-бэкендом ui +
перенаправлением вывода в лог-панель. Ветки те же: new / add / check / manage / uninstall.
"""
import sys
from argparse import Namespace

import flet as ft

import cli
import tools
from core import scripts as scripts_mod
from classes.manifest import local_version
from core import ui
from gui.backend import FletUi
from gui.log_sink import LogSink
from settings import config


async def main(page: ft.Page):
    page.title = "DeployManager"
    try:
        page.window.width, page.window.height = 980, 760
    except Exception:
        pass

    state = {"path": ""}
    path_field = ft.TextField(label="Папка проекта", read_only=True, expand=True)
    version_lbl = ft.Text("—")
    log_view = ft.ListView(expand=True, auto_scroll=True, spacing=1)
    file_picker = ft.FilePicker()
    try:
        page.services.append(file_picker)
    except Exception:
        page.services = [file_picker]

    sink = LogSink(log_view, page)
    flet_ui = FletUi(page)
    spinner = ft.ProgressRing(visible=False, width=18, height=18)
    status_lbl = ft.Text("", italic=True, color=ft.Colors.GREY)
    flet_ui.status_label = status_lbl              # ui.progress(...) пишет сюда

    async def choose_project(_):
        path = await file_picker.get_directory_path(
            dialog_title="Выберите папку проекта", initial_directory=config.PROJECTS_DIR or None)
        if not path:
            return
        state["path"] = path
        path_field.value = path
        try:
            lv = local_version(path)
            version_lbl.value = f"{lv.short} ({lv.branch}){'  DIRTY' if lv.dirty else ''}"
        except Exception as ex:
            version_lbl.value = f"не git: {ex}"
        page.update()

    branch_buttons: list[ft.Control] = []

    def set_busy(b: bool):
        for btn in branch_buttons:
            btn.disabled = b
        spinner.visible = b            # крутилка во время операции
        if not b:
            status_lbl.value = ""      # очистить статус по завершении
        page.update()

    async def run_branch(action: str, component: str | None = None):
        # infra-деплой (GD/WD/CD/DispatcherCtl) и суб-инструменты (tools: сессии и пр.) папку
        # проекта НЕ требуют — работают с БД напрямую. Прочие ветки требуют выбранный проект.
        if (action not in ("infra", "setup-node") and action not in tools.TOOL_KEYS
                and action not in scripts_mod.SCRIPT_KEYS and not state["path"]):
            sink.write("Сначала выберите папку проекта (кнопка «Обзор…»).\n")
            return
        set_busy(True)
        old_stdout = sys.stdout
        sys.stdout = sink
        ui.set_backend(flet_ui)
        try:
            args = Namespace(project=state["path"], action=action, nodes=None,
                             dry_run=False, yes=False, command=None, component=component, check=False)
            await cli.run(args)
        except SystemExit:
            pass
        except Exception as ex:
            sink.write(f"\n‼️ Ошибка: {ex}\n")
        finally:
            sys.stdout = old_stdout
            ui.set_backend(None)
            set_busy(False)
            sink.write("— готово —\n")

    def branch(label: str, action: str, **kw) -> ft.Control:
        btn = ft.Button(content=ft.Text(label),
                        on_click=lambda e, a=action: page.run_task(run_branch, a), **kw)
        branch_buttons.append(btn)
        return btn

    def infra_btn(label: str, component: str) -> ft.Control:
        # control-plane группа (GD/WD/CD/DispatcherCtl) — отдельный цвет, чтобы визуально
        # отделить инфра-деплой от обычных бот-веток
        btn = ft.Button(
            content=ft.Text(label, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.INDIGO_600,
            on_click=lambda e, c=component: page.run_task(run_branch, "infra", c))
        branch_buttons.append(btn)
        return btn

    def node_btn(label: str) -> ft.Control:
        # turnkey ввод новой ноды (bootstrap → тип → регистрация → WD). БД+SSH,
        # папка проекта не нужна (как control-plane). Зелёный — «поднять новый узел».
        btn = ft.Button(
            content=ft.Text(label, color=ft.Colors.WHITE),
            bgcolor=ft.Colors.GREEN_700,
            on_click=lambda e: page.run_task(run_branch, "setup-node"))
        branch_buttons.append(btn)
        return btn

    def script_btn(spec: dict) -> ft.Control:
        # операционный скрипт флота из реестра scripts.SCRIPTS (БД+SSH, проект не нужен).
        # action = ключ скрипта (как control-plane); danger-скрипты — янтарным.
        btn = ft.Button(
            content=ft.Text(f"{spec['icon']} {spec['label']}", color=ft.Colors.WHITE),
            bgcolor=getattr(ft.Colors, spec["color"], ft.Colors.TEAL_600),
            tooltip=spec.get("desc"),
            on_click=lambda e, k=spec["key"]: page.run_task(run_branch, k))
        branch_buttons.append(btn)
        return btn

    # ── навигатор экранов: flow-инструмент печатает в лог-панель (run_branch), screen-инструмент
    #    (cookies и пр.) ПЕРЕКЛЮЧАЕТ страницу на свой встроенный экран с кнопкой «← Назад» ──
    nav = {"teardown": None}

    async def go_home():
        td = nav.pop("teardown", None)
        nav["teardown"] = None
        if td is not None:
            try:
                await td()                 # закрыть пулы БД/браузер экрана, вернуть stdout
            except Exception as ex:         # noqa: BLE001 — не падать на выходе
                print(f"⚠️ Ошибка выхода с экрана: {ex}")
        page.controls.clear()
        page.add(*home_controls)
        page.update()

    async def open_screen(tool: dict):
        if nav.get("teardown") is not None:   # уже на каком-то экране — сперва закрыть
            await go_home()
        page.controls.clear()
        page.update()
        try:
            nav["teardown"] = await tools.build_screen(tool["key"], page, go_home)
        except Exception as ex:               # noqa: BLE001 — вернуть домой с сообщением
            page.controls.clear()
            page.add(*home_controls)
            sink.write(f"‼️ Не удалось открыть «{tool['label']}»: {ex}\n")
            page.update()

    def tool_btn(tool: dict) -> ft.Control:
        # суб-инструмент из реестра tools. flow → run_branch (лог-панель); screen → open_screen.
        if tool["kind"] == "screen":
            handler = lambda e, t=tool: page.run_task(open_screen, t)
        else:
            handler = lambda e, k=tool["key"]: page.run_task(run_branch, k)
        btn = ft.Button(
            content=ft.Text(f"{tool['icon']} {tool['label']}", color=ft.Colors.WHITE),
            bgcolor=getattr(ft.Colors, tool["color"], ft.Colors.TEAL_600),
            on_click=handler)
        branch_buttons.append(btn)
        return btn

    home_controls = [
        ft.Row([path_field, ft.Button(content=ft.Text("📂 Обзор…"), on_click=choose_project)]),
        ft.Row([ft.Text("Версия:"), version_lbl]),
        ft.Row([
            branch("🚀 Деплой с нуля", "new"),
            branch("➕ Добавить сервер", "add"),
            branch("🔍 Проверить версии", "check"),
            branch("🎛️ Управление", "manage"),
            branch("♻️ Обновить .env/юниты", "sync"),
            branch("🗑️ Деинсталляция", "uninstall"),
        ], wrap=True),
        ft.Row([ft.Text("Control-plane (без выбора проекта):", italic=True,
                        color=ft.Colors.INDIGO_300)]),
        ft.Row([
            infra_btn("🌐 GD", "GD"),
            infra_btn("🌐 WD", "WD"),
            infra_btn("🌐 CD", "CD"),
            infra_btn("🌐 DispatcherCtl", "DispatcherCtl"),
            node_btn("🖥️ Настроить ноду"),
        ], wrap=True),
        ft.Row([ft.Text("Скрипты флота (без выбора проекта):", italic=True,
                        color=ft.Colors.CYAN_300)]),
        ft.Row([script_btn(s) for s in scripts_mod.SCRIPTS], wrap=True),
        ft.Row([ft.Text("Инструменты (без выбора проекта):", italic=True,
                        color=ft.Colors.TEAL_300)]),
        ft.Row([tool_btn(t) for t in tools.TOOLS], wrap=True),
        ft.Row([spinner, status_lbl,
                ft.TextButton(content=ft.Text("🧹 Очистить лог"),
                              on_click=lambda _: sink.clear())],
               alignment=ft.MainAxisAlignment.SPACE_BETWEEN),
        ft.Container(content=log_view, expand=True, padding=8,
                     border=ft.Border.all(1, ft.Colors.GREY), border_radius=6),
    ]
    page.add(*home_controls)