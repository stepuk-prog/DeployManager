"""GUI-бэкенд интерактива (Flet): ask/confirm/select через модальные диалоги.

Каждый запрос создаёт asyncio.Future, показывает диалог, кнопка кладёт результат в future
(всё в одном event-loop — без потоков). Порт FletUi из DeployManager (нужные методы).
"""
import asyncio

import flet as ft


def _ok_button(text: str, on_click) -> ft.Button:
    """Позитивная кнопка (Да/OK) — светло-зелёная, чёрный текст."""
    return ft.Button(content=ft.Text(text, color=ft.Colors.BLACK),
                     bgcolor=ft.Colors.LIGHT_GREEN_400, on_click=on_click)


def _no_button(text: str, on_click) -> ft.Button:
    """Кнопка отрицания (Нет/Отмена) — приглушённый красный, чёрный текст."""
    return ft.Button(content=ft.Text(text, color=ft.Colors.BLACK),
                     bgcolor=ft.Colors.RED_200, on_click=on_click)


def _title_with_close(text: str, on_close) -> ft.Row:
    """Заголовок диалога с крестиком закрытия справа."""
    return ft.Row(
        [ft.Text(text, weight=ft.FontWeight.BOLD, expand=True),
         ft.IconButton(icon=ft.Icons.CLOSE, tooltip="Закрыть", on_click=on_close)],
        alignment=ft.MainAxisAlignment.SPACE_BETWEEN)


class FletUi:
    def __init__(self, page: ft.Page):
        self.page = page

    async def confirm(self, prompt: str, danger: bool = False,
                      ok_label: str = "✅ Да", cancel_label: str = "✖️ Нет") -> bool:
        fut = asyncio.get_running_loop().create_future()

        def done(val):
            def handler(_):
                if not fut.done():
                    fut.set_result(val)
                self.page.pop_dialog()
            return handler

        if danger:
            title = ft.Text("⚠️ Внимание", color=ft.Colors.RED, weight=ft.FontWeight.BOLD)
            content = ft.Text(prompt, width=440, color=ft.Colors.RED)
            yes = ft.Button(content=ft.Text("🗑️ Да", color=ft.Colors.WHITE),
                            bgcolor=ft.Colors.RED, on_click=done(True))
            no = ft.Button(content=ft.Text("✖️ Нет", color=ft.Colors.BLACK),
                           bgcolor=ft.Colors.GREY_400, on_click=done(False))
        else:
            title = ft.Text("Подтверждение")
            content = ft.Text(prompt, width=440)
            yes = _ok_button(ok_label, done(True))
            no = _no_button(cancel_label, done(False))

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=title, content=content, actions=[yes, no],
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut

    async def select(self, title: str, labels: list[str], default_index: int = 0) -> int | None:
        """Выбор одного варианта (→ индекс / None при отмене). Мало коротких — кнопки в ряд;
        много/длинные — прокручиваемый вертикальный список."""
        fut = asyncio.get_running_loop().create_future()

        def choose(idx):
            def handler(_):
                if not fut.done():
                    fut.set_result(idx)
                self.page.pop_dialog()
            return handler

        cancel = _no_button("✖️ Отмена", choose(None))
        header = _title_with_close("Выбор", choose(None))
        compact = len(labels) <= 4 and all(len(lab) <= 24 for lab in labels)
        if compact:
            actions = [ft.Button(content=ft.Text(lab), on_click=choose(i))
                       for i, lab in enumerate(labels)]
            actions.append(cancel)
            dialog = ft.AlertDialog(
                modal=True, title=header, content=ft.Text(title, width=440),
                actions=actions, actions_alignment=ft.MainAxisAlignment.END)
        else:
            items = [ft.Text(title)] + [
                ft.Button(content=ft.Text(lab, text_align=ft.TextAlign.LEFT),
                          on_click=choose(i), width=560)
                for i, lab in enumerate(labels)]
            content = ft.Column(items, scroll=ft.ScrollMode.AUTO, tight=True, spacing=4,
                                width=580, height=min(440, 60 + 42 * len(labels)))
            dialog = ft.AlertDialog(
                modal=True, title=header, content=content,
                actions=[cancel], actions_alignment=ft.MainAxisAlignment.END)
        self.page.show_dialog(dialog)
        return await fut

    async def message(self, prompt: str, title: str = "Сообщение",
                      ok_label: str = "OK") -> None:
        """Информационный диалог с одной кнопкой (для ошибок/итогов)."""
        fut = asyncio.get_running_loop().create_future()

        def ok(_):
            if not fut.done():
                fut.set_result(None)
            self.page.pop_dialog()

        self.page.show_dialog(ft.AlertDialog(
            modal=True, title=ft.Text(title), content=ft.Text(prompt, width=440),
            actions=[_ok_button(ok_label, ok)],
            actions_alignment=ft.MainAxisAlignment.END))
        return await fut
