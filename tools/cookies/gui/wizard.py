"""WizardDialog — один модальный диалог на весь флоу аккаунта: инфо-строки сверху
(имя/почта/пароль с копированием), лог статуса и заменяемый ряд кнопок действий.

Шаги флоу обновляют один и тот же диалог: status(...) добавляет строку в лог,
choose([...]) показывает кнопки и ждёт выбор (asyncio.Future, один event-loop).
"""
import asyncio

import flet as ft

# Цвета кнопок по роли.
_KIND_STYLE = {
    "ok": (ft.Colors.LIGHT_GREEN_400, ft.Colors.BLACK),
    "no": (ft.Colors.RED_200, ft.Colors.BLACK),
    "neutral": (ft.Colors.BLUE_GREY_200, ft.Colors.BLACK),
}


class WizardDialog:
    def __init__(self, page: ft.Page, title: str, info_rows: list[ft.Control] | None = None):
        self.page = page
        self._log = ft.Column([], scroll=ft.ScrollMode.AUTO, tight=True, spacing=2,
                              width=520, height=240, auto_scroll=True)
        self._spinner = ft.ProgressRing(width=18, height=18, visible=False)
        content_controls: list[ft.Control] = []
        if info_rows:
            content_controls.extend(info_rows)
            content_controls.append(ft.Divider())
        content_controls.append(ft.Row([self._spinner, ft.Text("Журнал:")], spacing=8))
        content_controls.append(ft.Container(
            content=self._log, border=ft.Border.all(1, ft.Colors.GREY), border_radius=6,
            padding=6))
        self._dialog = ft.AlertDialog(
            modal=True, title=ft.Text(title, weight=ft.FontWeight.BOLD),
            content=ft.Column(content_controls, tight=True, spacing=8, width=540),
            actions=[], actions_alignment=ft.MainAxisAlignment.END)

    # ----- жизненный цикл -----
    def open(self) -> None:
        self.page.show_dialog(self._dialog)
        self.page.update()

    def close(self) -> None:
        try:
            self.page.pop_dialog()
        except (Exception,):
            pass
        self.page.update()

    # ----- статус -----
    def status(self, text: str, color=None) -> None:
        self._log.controls.append(ft.Text(text, size=12, color=color, selectable=True))
        self.page.update()

    def busy(self, on: bool) -> None:
        """Показать/скрыть спиннер (идёт длительный шаг). Кнопки на это время убираем."""
        self._spinner.visible = on
        if on:
            self._dialog.actions = []
        self.page.update()

    # ----- выбор действия -----
    async def choose(self, options: list[tuple]) -> str:
        """options: список (label, value, kind). kind ∈ {'ok','no','neutral'}. → value."""
        self.busy(False)
        fut = asyncio.get_running_loop().create_future()

        def make(value):
            def handler(_):
                if not fut.done():
                    fut.set_result(value)
            return handler

        buttons = []
        for label, value, kind in options:
            bg, fg = _KIND_STYLE.get(kind, _KIND_STYLE["neutral"])
            buttons.append(ft.Button(content=ft.Text(label, color=fg), bgcolor=bg,
                                     on_click=make(value)))
        self._dialog.actions = buttons
        self.page.update()
        return await fut
