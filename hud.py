import customtkinter as ctk
from queue import Queue
import time
import threading
from pathlib import Path
try:
    import winsound
except ImportError:
    winsound = None

from tkinter import Label as TkLabel  # usarlo para medir sin mostrar


typing_lock = threading.Lock()
msg_queue = Queue()

root = None
frame = None
bubble_label = None
hud_visible = False
texto_acumulado = ""
_SOUND_FILE = Path(__file__).with_name("notify.wav")

def _play_sound(fallback_alias: str) -> None:
    if winsound is None:
        return
    try:
        if _SOUND_FILE.exists():
            winsound.PlaySound(str(_SOUND_FILE), winsound.SND_FILENAME | winsound.SND_ASYNC)
        else:
            winsound.PlaySound(fallback_alias, winsound.SND_ALIAS | winsound.SND_ASYNC)
    except Exception:
        pass

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
    """Invoca client.run_command en un hilo aparte y muestra la respuesta."""

    def _worker(entrada: str) -> None:
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
        return

    threading.Thread(target=_worker, args=(texto.strip(),), daemon=True).start()

def set_estado(estado, texto):
    if bubble_label:
        color = estado_colores.get(estado, "#888")
        icono = estado_iconos.get(estado, "")
        bubble_label.configure(text=f"{icono} {texto}")
        frame.configure(border_color=color)

def actualizar_texto():
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
    """Realiza una animacion de transparencia sin bloquear el hilo principal."""
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
    global root, frame, bubble_label, POSICION_ORIGINAL_X, POSICION_ORIGINAL_Y

    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")

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

    actualizar_texto()
    root.withdraw()
    root.mainloop()

def fade_in():
    if root:
        _animate_alpha(1.0)

def fade_out():
    if root:
        _animate_alpha(0.0, on_complete=root.withdraw)

def expandir_altura_suave(paso=3, delay=3):
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
            set_texto_animado(texto, estado="procesando", after=after)


def ocultar():
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
    comandos_validos = ("abrir ", "cerrar ", "reiniciar ", "iniciar ", "buscar ")
    es_comando = any(texto.lower().startswith(c) for c in comandos_validos)

    if es_comando:
        log(f"⚙️ Ejecutando comando: {texto}")
        set_estado("ejecutado", "✅ Comando ejecutado")
        root.after(2000, ocultar) 
    else:
        mostrar_respuesta_final(texto)

def mostrar_respuesta_final(texto):
    global hud_visible
    hud_visible = True

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
    dummy = TkLabel(root, text=texto, font=fuente, wraplength=ancho - 40, justify="left")
    dummy.update_idletasks()
    return dummy.winfo_reqheight() + 40  # margen superior + inferior
