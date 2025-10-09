# Dependencias opcionales: pip install pillow
"""
HUD flotante estilo tarjeta moderna para RAI-MINI.
Usa Tkinter con animaciones suaves y efecto de tipeo.
"""
from __future__ import annotations

import queue
import threading
import tkinter as tk
import tkinter.font as tkfont
from dataclasses import dataclass
from typing import Optional


@dataclass
class HUDCommand:
    kind: str
    payload: dict


STATE_GRADIENTS = {
    "escuchando": ("#ff3df5", "#35f5ff"),
    "ejecutando": ("#a66bff", "#6df5ff"),
    "exito": ("#2ecc71", "#7bed9f"),
    "error": ("#ff4d4f", "#ff9292"),
}


class RAIHUD:
    def __init__(self) -> None:
        self._queue: "queue.Queue[HUDCommand]" = queue.Queue()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._ready = threading.Event()
        self._state = "escuchando"
        self._typing_job: Optional[str] = None
        self._close_job: Optional[str] = None
        self._thread.start()
        self._ready.wait()

    def _run(self) -> None:
        self.root = tk.Tk()
        self.root.withdraw()
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self.root.attributes("-alpha", 0.0)
        self.root.configure(bg="#000000")

        available_fonts = {name.lower() for name in tkfont.families()}
        preferred = "SF Pro Display"
        fallback = "Segoe UI"
        if preferred.lower() in available_fonts:
            self._font_family = preferred
        elif "sf pro text" in available_fonts:
            self._font_family = "SF Pro Text"
        elif fallback.lower() in available_fonts:
            self._font_family = fallback
        else:
            self._font_family = "Arial"

        self._width = 320
        self._height = 130

        self.canvas = tk.Canvas(
            self.root,
            width=self._width,
            height=self._height,
            bg="#000000",
            highlightthickness=0,
        )
        self.canvas.pack()

        self.text_var = tk.StringVar(value="")
        self.text_label = tk.Label(
            self.root,
            textvariable=self.text_var,
            font=(self._font_family, 12),
            fg="#f7f7f7",
            bg="#121212",
            wraplength=self._width - 60,
            justify="left",
        )

        self.dots_label = tk.Label(
            self.root,
            text="",
            font=(self._font_family, 12),
            fg="#b0b0b0",
            bg="#121212",
        )

        self._card = tk.Frame(self.root, bg="#121212")
        self._card.place(x=10, y=10, width=self._width - 20, height=self._height - 20)
        self.text_label.place(x=30, y=40)
        self.dots_label.place(x=30, y=20)

        self.root.bind("<Configure>", lambda _: self._draw_gradient())
        self.root.after(30, self._process_queue)

        self._ready.set()
        self.root.mainloop()

    def _draw_gradient(self) -> None:
        self.canvas.delete("gradient")
        start_color, end_color = STATE_GRADIENTS.get(self._state, ("#ff3df5", "#35f5ff"))
        steps = 24
        base_offset = 4
        for i in range(steps):
            ratio = i / max(steps - 1, 1)
            color = self._blend(start_color, end_color, ratio)
            offset = base_offset + (i * 0.6)
            x1 = offset
            y1 = offset
            x2 = self._width - offset
            y2 = self._height - offset
            radius = 30
            points = [
                x1 + radius,
                y1,
                x2 - radius,
                y1,
                x2,
                y1,
                x2,
                y1 + radius,
                x2,
                y2 - radius,
                x2,
                y2,
                x2 - radius,
                y2,
                x1 + radius,
                y2,
                x1,
                y2,
                x1,
                y2 - radius,
                x1,
                y1 + radius,
                x1,
                y1,
            ]
            self.canvas.create_line(
                points,
                smooth=True,
                width=2,
                fill=color,
                tags="gradient",
            )

    def _blend(self, start: str, end: str, ratio: float) -> str:
        def to_rgb(hex_color: str) -> tuple[int, int, int]:
            hex_color = hex_color.lstrip("#")
            return tuple(int(hex_color[i : i + 2], 16) for i in (0, 2, 4))

        start_rgb = to_rgb(start)
        end_rgb = to_rgb(end)
        mixed = tuple(
            int(start_rgb[i] + (end_rgb[i] - start_rgb[i]) * ratio)
            for i in range(3)
        )
        return "#" + "".join(f"{value:02x}" for value in mixed)

    def _process_queue(self) -> None:
        while not self._queue.empty():
            command = self._queue.get()
            if command.kind == "show":
                self._handle_show(**command.payload)
            elif command.kind == "state":
                self._handle_state(**command.payload)
            elif command.kind == "close":
                self._handle_close(**command.payload)
            elif command.kind == "destroy":
                self._handle_destroy()
        self.root.after(50, self._process_queue)

    def set_state(self, state: str) -> None:
        self._state = state
        self._queue.put(HUDCommand("state", {"state": state}))

    def show_message(self, text: str, typing: bool = True) -> None:
        self._queue.put(HUDCommand("show", {"text": text, "typing": typing}))

    def schedule_close(self, delay: float) -> None:
        self._queue.put(HUDCommand("close", {"delay": delay}))

    def destroy(self) -> None:
        self._queue.put(HUDCommand("destroy", {}))

    # --- Handlers ejecutados dentro del hilo del HUD ---
    def _handle_state(self, state: str) -> None:
        self._state = state
        self._draw_gradient()

    def _handle_show(self, text: str, typing: bool) -> None:
        self._cancel_close()
        self._ensure_visible()
        if typing:
            self._start_typing(text)
        else:
            self.text_var.set(text)
            self.dots_label.config(text="")

    def _handle_close(self, delay: float) -> None:
        self._cancel_close()
        self._close_job = self.root.after(int(delay * 1000), self._fade_out)

    def _handle_destroy(self) -> None:
        self._cancel_close()
        self.root.after(0, self.root.destroy)

    def _ensure_visible(self) -> None:
        self.root.deiconify()
        self.root.update_idletasks()
        screen_width = self.root.winfo_screenwidth()
        x = screen_width - self._width - 30
        y = 40
        self.root.geometry(f"{self._width}x{self._height}+{x}+{y}")
        self._draw_gradient()
        self._fade_in()

    def _start_typing(self, text: str) -> None:
        self.text_var.set("")
        self.dots_label.config(text="...")
        if self._typing_job:
            self.root.after_cancel(self._typing_job)
        self._typing_sequence(text, index=0)

    def _typing_sequence(self, text: str, index: int) -> None:
        if index >= len(text):
            self.dots_label.config(text="")
            return
        current = self.text_var.get() + text[index]
        self.text_var.set(current)
        delay = 40 if text[index] != " " else 80
        self._typing_job = self.root.after(delay, self._typing_sequence, text, index + 1)

    def _fade_in(self) -> None:
        target = 0.95
        current = self.root.attributes("-alpha")
        step = 0.1
        if current >= target:
            return
        self.root.attributes("-alpha", min(target, current + step))
        self.root.after(30, self._fade_in)

    def _fade_out(self) -> None:
        current = self.root.attributes("-alpha")
        step = 0.1
        if current <= 0.05:
            self.root.withdraw()
            self.root.attributes("-alpha", 0.0)
            return
        self.root.attributes("-alpha", max(0.0, current - step))
        self.root.after(30, self._fade_out)

    def _cancel_close(self) -> None:
        if self._close_job:
            self.root.after_cancel(self._close_job)
            self._close_job = None

