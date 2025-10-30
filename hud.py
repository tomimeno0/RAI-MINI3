import ctypes
import sys
import threading
import time
from pathlib import Path
from queue import Queue
# Módulo HUD (Head-Up Display) visual para RAI-MINI.
# - Implementa una ventana flotante y transparente con CustomTkinter para mostrar estados y mensajes.
# - Ofrece animaciones suaves (fade in/out, tipeo letra a letra) sin bloquear el hilo principal de Tkinter.
# - Expone funciones para mostrar/ocultar/actualizar el HUD desde otros módulos (p. ej., client.py).

import customtkinter as ctk
import tkinter as tk
try:
    # winsound es específico de Windows; si no existe (otro SO), el sonido se deshabilita de forma segura.
    import winsound
except ImportError:
    winsound = None

from tkinter import Label as TkLabel  # usarlo para medir sin mostrar (cálculo de alto requerido)


typing_lock = threading.Lock()  # Lock para sincronizar animación de tipeo y evitar superposición.
msg_queue = Queue()  # Cola de mensajes que alimenta el HUD desde threads externos.

root = None  # Ventana principal de CustomTkinter, creada en iniciar_hud().
frame = None  # Marco de contenido con borde redondeado y color de estado.
content_frame = None  # Contenedor interno opcional (no siempre usado).
bubble_label = None  # Etiqueta donde se muestra el texto de estado/respuesta.
hud_visible = False  # Flag lógico para saber si el HUD está visible o en fade-out.
texto_acumulado = ""  # Buffer de texto para la animación de tipeo.
_SOUND_FILE = Path(__file__).with_name("notify.wav")  # Sonido local para notificaciones si existe.
ICON_FILE = Path(__file__).with_name("RAI_option_A.ico")  # Ícono preferido para ventana/console en Windows.
ICON_PNG_FALLBACK = Path(__file__).with_name("optionA_1024.png")  # Fallback PNG si el .ico falla.
APP_USER_MODEL_ID = "RAI.MINI.HUD"  # AppUserModelID para agrupar iconos en la barra de tareas.

TRANSPARENT_COLOR = "#010101"  # Color que puede usarse como transparencia según el gestor de ventanas.
HUD_BACKGROUND_COLOR = "#0b1324"  # Color de fondo del HUD (tema oscuro).
HUD_BORDER_RADIUS = 28  # Radio de esquinas del marco principal.
HUD_BORDER_WIDTH = 3  # Ancho del borde que cambia de color según estado.
HUD_PADDING = 14  # Margen externo del frame dentro de la ventana.
CONTENT_PADDING = 20  # Margen interno para el contenido (label) dentro del frame.
HUD_MAX_CHARS = 70  # Límite de caracteres visibles; el resto se recorta con elipsis.

window_icon_photo = None  # Referencia al ícono cargado como PhotoImage para evitar GC.
icon_handles: list[int] = []  # Handles de iconos nativos (para liberar si hiciera falta a futuro).

def _play_sound(fallback_alias: str) -> None:
    """Reproduce un sonido de notificación si winsound está disponible.

    - Prefiere un archivo local notify.wav si existe; si no, usa un alias del sistema (p.ej. SystemNotification).
    - Modo asíncrono para no bloquear la UI.
    """
    if winsound is None:
        return
    try:
        if _SOUND_FILE.exists():
            winsound.PlaySound(str(_SOUND_FILE), winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.PlaySound(fallback_alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass  # Fallar en silencio: el HUD no depende del audio.


def _ensure_app_user_model_id() -> None:
    """Configura el AppUserModelID en Windows para agrupar iconos correctamente en la barra de tareas."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(ctypes.c_wchar_p(APP_USER_MODEL_ID))
    except Exception:
        pass  # Si falla, la UI seguirá funcionando; solo se pierde la agrupación precisa del icono.


def _apply_window_icon(window: ctk.CTk | None) -> None:
    """Aplica el ícono a la ventana (ICO si es posible, PNG como fallback)."""
    if window is None:
        return
    icon_path = ICON_FILE if ICON_FILE.exists() else None
    fallback_image = ICON_PNG_FALLBACK if ICON_PNG_FALLBACK.exists() else None

    applied = False  # Marca si al menos una vía logró aplicar un ícono.
    if icon_path:
        try:
            window.iconbitmap(str(icon_path))
            applied = True
        except Exception:
            pass
        if sys.platform == "win32":  # En Windows intentamos además establecer íconos grande/pequeño nativos.
            try:
                handle = window.winfo_id()
                IMAGE_ICON = 1
                LR_LOADFROMFILE = 0x00000010
                LR_DEFAULTSIZE = 0x00000040
                hicon_big = ctypes.windll.user32.LoadImageW(
                    0,
                    str(icon_path),
                    IMAGE_ICON,
                    0,
                    0,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE,
                )
                hicon_small = ctypes.windll.user32.LoadImageW(
                    0,
                    str(icon_path),
                    IMAGE_ICON,
                    32,
                    32,
                    LR_LOADFROMFILE | LR_DEFAULTSIZE,
                )
                if hicon_big:
                    ctypes.windll.user32.SendMessageW(handle, 0x0080, 1, hicon_big)
                    icon_handles.append(hicon_big)
                if hicon_small:
                    ctypes.windll.user32.SendMessageW(handle, 0x0080, 0, hicon_small)
                    icon_handles.append(hicon_small)
                if hicon_big or hicon_small:
                    applied = True  # Consideramos éxito si al menos uno fue aplicado.
            except Exception:
                pass

    if not applied and fallback_image:
        global window_icon_photo
        try:
            window_icon_photo = tk.PhotoImage(file=str(fallback_image))
            window.iconphoto(True, window_icon_photo)
            window.iconbitmap("@" + str(fallback_image))
        except Exception:
            window_icon_photo = None  # Si falla todo, se queda sin ícono pero la ventana sigue operativa.


def _apply_console_icon() -> None:
    """Intenta aplicar el ícono al proceso de consola (si corre con consola visible en Windows)."""
    if sys.platform != "win32" or not ICON_FILE.exists():
        return
    try:
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if not hwnd:
            return
        IMAGE_ICON = 1
        LR_LOADFROMFILE = 0x00000010
        LR_DEFAULTSIZE = 0x00000040
        icon_path = str(ICON_FILE)
        hicon_big = ctypes.windll.user32.LoadImageW(
            0,
            icon_path,
            IMAGE_ICON,
            0,
            0,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        hicon_small = ctypes.windll.user32.LoadImageW(
            0,
            icon_path,
            IMAGE_ICON,
            32,
            32,
            LR_LOADFROMFILE | LR_DEFAULTSIZE,
        )
        if hicon_big:
            ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 1, hicon_big)
            icon_handles.append(hicon_big)
        if hicon_small:
            ctypes.windll.user32.SendMessageW(hwnd, 0x0080, 0, hicon_small)
            icon_handles.append(hicon_small)
    except Exception:
        pass  # Errores aquí no deben afectar el HUD: es una mejora estética.


def _limit_text(texto):
    """Aplica un límite de caracteres con elipsis para evitar desbordes visuales."""
    if not isinstance(texto, str):
        return ""
    texto = texto.strip()
    if len(texto) <= HUD_MAX_CHARS:
        return texto
    if HUD_MAX_CHARS <= 3:  # Caso patológico: no hay espacio para elipsis; recorta directo.
        return texto[:HUD_MAX_CHARS]
    recortado = texto[: HUD_MAX_CHARS - 3].rstrip()
    return f"{recortado}..."


def _update_frame_layout(width: int, height: int) -> None:
    """Recalcula posiciones y tamaños internos cuando cambian las dimensiones del HUD."""
    if frame is None:
        return
    interior_w = max(0, width - 2 * HUD_PADDING)
    interior_h = max(0, height - 2 * HUD_PADDING)
    frame.place(x=HUD_PADDING, y=HUD_PADDING)
    frame.configure(width=interior_w, height=interior_h)

    inner_w = max(0, interior_w - 2 * CONTENT_PADDING)
    inner_h = max(0, interior_h - 2 * CONTENT_PADDING)
    if content_frame is not None:
        content_frame.place(x=CONTENT_PADDING, y=CONTENT_PADDING)
        content_frame.configure(width=inner_w, height=inner_h)
    if bubble_label is not None:
        wrap = max(40, inner_w)
        bubble_label.configure(wraplength=wrap, width=inner_w, height=inner_h)

ANCHO = 420
ALTO_NORMAL = 120
ALTO_EXPANDIDO = 300

POSICION_ORIGINAL_X = 0
POSICION_ORIGINAL_Y = 0

estado_colores = {
    "escuchando": "#00c3ff",
    "procesando": "#ffc107",
    "ejecutado": "#00e676",
    "error": "#ff1744"
}

estado_iconos = {
    "escuchando": "🎤",
    "procesando": "⚙️",
    "ejecutado": "✅",
    "error": "❌"
}

def log(texto):
    msg_queue.put(texto)


def ejecutar_comando_desde_ui(texto: str) -> None:
    """Invoca client.run_command en un hilo aparte y muestra la respuesta.

    Evita bloquear el hilo principal de Tkinter importando dinámicamente el módulo client.
    """

    def _worker(entrada: str) -> None:
        # Hilo trabajador: ejecuta el comando y encola la respuesta para el HUD.
        try:
            from importlib import import_module

            client_mod = import_module("client")
            respuesta = client_mod.run_command(entrada)
        except Exception as exc:  # noqa: BLE001
            respuesta = f"Error al ejecutar comando: {exc}"

        if isinstance(respuesta, dict):
            mensaje = str(respuesta.get("msg", "")).strip()
        else:
            mensaje = str(respuesta).strip()

        log(mensaje or "Respuesta vacia")

    if not isinstance(texto, str) or not texto.strip():
        log("Ingresa un comando valido.")
        return  # Validación rápida: no crear hilo cuando no hay entrada útil.

    threading.Thread(target=_worker, args=(texto.strip(),), daemon=True).start()

def set_estado(estado, texto):
    """Actualiza borde e ícono/texto según el estado semántico (escuchando, procesando, etc.)."""
    if bubble_label and frame:
        color = estado_colores.get(estado, "#888")
        icono = estado_iconos.get(estado, "")
        mensaje = f"{icono} {texto}".strip()
        bubble_label.configure(text=_limit_text(mensaje))
        frame.configure(border_color=color)

def actualizar_texto():
    """Consume la cola de mensajes y ajusta el estado del HUD; se reprograma cada 100 ms."""
    while not msg_queue.empty():
        texto = msg_queue.get()
        if "Escuchando" in texto:
            set_estado("escuchando", texto)
        elif "Procesando" in texto:
            set_estado("procesando", texto)
        elif "Listo" in texto or "fue abierto" in texto:
            set_estado("ejecutado", texto)
        elif "No se pudo" in texto or "Error" in texto:
            set_estado("error", texto)
        else:
            set_estado("procesando", texto)
    root.after(100, actualizar_texto)


def _animate_alpha(target_alpha: float, duration_ms: int = 300, on_complete=None) -> None:
    """Realiza una animacion de transparencia (fade) sin bloquear el hilo principal."""
    if not root:
        return
    try:
        start_alpha = float(root.attributes("-alpha"))
    except Exception:
        start_alpha = 0.0
    steps = max(duration_ms // 30, 1)
    delta = (target_alpha - start_alpha) / steps if steps else 0.0

    def _tick(step: int, current: float) -> None:
        if not root:
            return
        if step >= steps:
            root.attributes("-alpha", target_alpha)
            if on_complete:
                on_complete()
            return
        root.attributes("-alpha", max(0.0, min(1.0, current)))
        root.after(30, lambda: _tick(step + 1, current + delta))

    _tick(0, start_alpha)

def iniciar_hud():
    """Inicializa y muestra el HUD principal (bloquea con mainloop)."""
    global root, frame, bubble_label, POSICION_ORIGINAL_X, POSICION_ORIGINAL_Y

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

    _ensure_app_user_model_id()
    _apply_console_icon()
    root = ctk.CTk()
    root.overrideredirect(True)
    screen_width = root.winfo_screenwidth()
    pos_x = screen_width - ANCHO - 20
    pos_y = 30
    root.geometry(f"{ANCHO}x{ALTO_NORMAL}+{pos_x}+{pos_y}")
    POSICION_ORIGINAL_X = pos_x
    POSICION_ORIGINAL_Y = pos_y

    root.title("RAI HUD")
    root.attributes("-topmost", True)
    root.resizable(False, False)
    root.configure(bg="black")
    root.attributes("-alpha", 0)
    _apply_window_icon(root)  # Intenta aplicar icono a la ventana si hay recursos disponibles.

    frame = ctk.CTkFrame(
        root,
        width=ANCHO,
        height=ALTO_NORMAL,
        corner_radius=25,
        border_width=3,
        fg_color="#000000",
        border_color="#00c3ff"
    )
    frame.place(x=0, y=0, relwidth=1, relheight=1)

    bubble_label = ctk.CTkLabel(
        frame,
        text="",
        text_color="#ffffff",
        font=("SF Pro Display", 17),
        wraplength=ANCHO - 40,
        justify="center"
    )
    bubble_label.place(relx=0.5, rely=0.5, anchor="center")

    actualizar_texto()  # Inicia el ciclo de actualización no bloqueante por after().
    root.withdraw()
    root.mainloop()

def fade_in():
    """Efecto de aparecer suavemente"""
    if root:
        _animate_alpha(1.0)

def fade_out():
    """Efecto de desaparecer suavemente y ocultar la ventana"""
    if root:
        _animate_alpha(0.0, on_complete=root.withdraw)

def expandir_altura_suave(paso=3, delay=3):
    """Aumenta la altura en pequeños pasos programados con after() para animación suave."""
    alto_actual = root.winfo_height()
    if alto_actual < ALTO_EXPANDIDO:
        nuevo_alto = min(alto_actual + paso, ALTO_EXPANDIDO)
        root.geometry(f"{root.winfo_width()}x{nuevo_alto}+{root.winfo_x()}+{root.winfo_y()}")
        root.after(delay, lambda: expandir_altura_suave(paso, delay))

def mostrar(texto=None, es_expansivo=False, after=None, es_bienvenida=False):
    global hud_visible, texto_acumulado
    hud_visible = False
    if root and not hud_visible:
        hud_visible = True
        _play_sound("SystemNotification")
        texto_acumulado = ""
        root.deiconify()
        root.attributes("-alpha", 0)
        bubble_label.configure(text="")

        # Posicionar según tipo de mensaje
        if es_bienvenida:
            bubble_label.place(relx=0.5, rely=0.5, anchor="center")
            bubble_label.configure(font=("SF Pro Display", 20))  # 🔠 Más grande en bienvenida
        else:
            bubble_label.place(relx=0.05, rely=0.1, anchor="nw")
            bubble_label.configure(font=("SF Pro Display", 19))  # 🔠 Letra general más grande

        # Tamaño inicial según expansión
        if es_expansivo:
            root.geometry("600x200")
            frame.configure(width=600, height=170)
            bubble_label.configure(wraplength=560)
        else:
            root.geometry(f"{ANCHO}x{ALTO_NORMAL}")
            frame.configure(width=ANCHO, height=ALTO_NORMAL - 30)
            bubble_label.configure(wraplength=ANCHO - 40)

        frame.configure(border_color=estado_colores.get("procesando", "#888"))
        fade_in()

        if texto:
            set_texto_animado(_limit_text(texto), estado="procesando", after=after)


def ocultar():
    """Oculta el HUD con fade-out y restaura posición/tamaño originales."""
    _play_sound("SystemExit")
    global hud_visible

    def fade_out_paso(i=10):
        try:
            if i < 0:
                if root:
                    root.withdraw()
                    root.geometry(f"{ANCHO}x{ALTO_NORMAL}+{POSICION_ORIGINAL_X}+{POSICION_ORIGINAL_Y}")
                hud_visible = False
                print("HUD ocultado, hud_visible seteado en False")
            else:
                alpha = i / 10
                if root:
                    root.attributes("-alpha", alpha)
                    root.after(30, lambda: fade_out_paso(i - 1))
        except Exception as e:
            print(f"⚠️ Error en fade_out_paso: {e}")
            hud_visible = False
            print("HUD ocultado por excepción, hud_visible seteado en False")

    fade_out_paso()




def set_texto_animado(texto, delay=0.03, estado="procesando", after=None):
    """Escribe el texto caracter por caracter en un hilo, con color de estado."""
    texto = _limit_text(texto)
    def escribir():
        global texto_acumulado
        with typing_lock:
            texto_acumulado = ""
            bubble_label.configure(text="")
            frame.configure(border_color=estado_colores.get(estado, "#888"))
            for letra in texto:
                current = bubble_label.cget("text")
                bubble_label.configure(text=current + letra)
                texto_acumulado += letra
                time.sleep(delay)
            texto_acumulado = ""
            if after:
                after()
    threading.Thread(target=escribir).start()

def procesar_respuesta_rai(texto):
    """Distingue entre comando (acciones) y respuesta conversacional, y actúa en consecuencia."""
    comandos_validos = ("abrir ", "cerrar ", "reiniciar ", "iniciar ", "buscar ")
    es_comando = any(texto.lower().startswith(c) for c in comandos_validos)

    if es_comando:
        log(f"⚙️ Ejecutando comando: {texto}")
        set_estado("ejecutado", "✅ Comando ejecutado")
        root.after(2000, ocultar) 
    else:
        mostrar_respuesta_final(texto)

def mostrar_respuesta_final(texto):
    """Muestra una respuesta de texto expandiendo alto según contenido y aplicando fade-in."""
    global hud_visible
    hud_visible = True

    texto = _limit_text(texto)
    altura_calculada = calcular_altura_requerida(texto, ANCHO)
    altura_final = min(max(ALTO_NORMAL, altura_calculada), 480)

    screen_width = root.winfo_screenwidth()
    root.geometry(f"{ANCHO}x{altura_final}+{screen_width - ANCHO - 20}+30")
    frame.configure(width=ANCHO, height=altura_final, border_color=estado_colores["procesando"])
    bubble_label.place(relx=0.05, rely=0.1, anchor="nw")
    bubble_label.configure(wraplength=ANCHO - 40, font=("SF Pro Display", 19), text="")

    root.deiconify()
    root.attributes("-alpha", 0)
    fade_in()

    def escribir():
        with typing_lock:
            bubble_label.configure(text="")
            texto_completo = ""
            for letra in texto:
                texto_completo += letra
                bubble_label.configure(text=texto_completo)
                time.sleep(0.02)

    # Arrancamos el thread de escritura
    threading.Thread(target=escribir).start()

    # Programamos ocultamiento en el hilo principal (Tkinter)
    root.after(6000, ocultar)




def calcular_altura_requerida(texto, ancho, fuente=("SF Pro Display", 19)):
    """Calcula el alto requerido para el label dado un ancho y fuente, sin mostrarlo."""
    texto = _limit_text(texto)
    dummy = TkLabel(root, text=texto, font=fuente, wraplength=ancho - 40, justify="left")
    dummy.update_idletasks()
    return dummy.winfo_reqheight() + 40  # margen superior + inferior
