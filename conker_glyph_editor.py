"""
conker_glyph_editor.py - Tkinter editor for the glyph rectangles, metrics and
character map of Conker: Live & Reloaded CAFF font files (default.bin).

Usage: python conker_glyph_editor.py [default.bin [texture.png]]
"""

import os
import sys
import re
import struct
import traceback
import tkinter as tk
from tkinter import ttk
from tkinter import filedialog as _native_filedialog
from tkinter import messagebox as _native_messagebox
import tkinter.font as tkfont

from PIL import Image, ImageTk

from conker_glyph_format import ConkerFont, FONT_PROFILES


HANDLE_SIZE = 6          # Size of corner handle box for dragging (in screen pixels)
DEFAULT_ZOOM = 4
MIN_ZOOM, MAX_ZOOM = 1, 16


# Errors that mean "this file is unreadable/corrupt" (expected, user-facing):
# ValueError - bad magic/charmap, invalid image dimensions
# OSError    - missing/unreadable file (PIL's UnidentifiedImageError is one too)
# struct.error - truncated file / corrupt header read via struct.unpack
# DecompressionBombError - PIL refusing an absurdly large image
EXPECTED_LOAD_ERRORS = (ValueError, OSError, struct.error, Image.DecompressionBombError)

# --- Fonts -------------------------------------------------------------
# Preferred family first, then fallbacks (Windows font -> Linux metric-compatible
# equivalent).
UI_FONT_CANDIDATES = (
    "Segoe UI",         # Windows
    "Ubuntu",           # Ubuntu
    "sans-serif",
)

MONO_FONT_CANDIDATES = (
    "Consolas",         # Windows
    "Ubuntu Mono",      # Ubuntu
    "monospace",
)

# Font used for **bold** fragments in the Help / Info text: (family, weight).
# "Segoe UI Black" is a separate family (already heavy), so it is used with
# weight "normal"; Ubuntu and generic sans-serif get a real bold weight.
BOLD_FONT_CANDIDATES = (
    ("Segoe UI Black", "normal"),   # Windows
    ("Ubuntu", "bold"),             # Ubuntu
    ("sans-serif", "bold"),
)

UI_FONT_SIZE = 9
MONO_FONT_SIZE = 10

TAB_WIDTHS = {
    "Segoe UI": 16,
    "Ubuntu": 14,
    "sans-serif": 13,
}

def _pick_family(root, candidates):
    """Return the first installed font family from candidates (case-insensitive), or None."""
    installed = {f.lower(): f for f in tkfont.families(root)}
    for name in candidates:
        if name.lower() in installed:
            return installed[name.lower()]
    return None


def setup_fonts(root):
    """Resolve UI/monospace families, apply them to Tk's named fonts, and
    return (ui_font, mono_font) tuples for widgets that set a font explicitly.

    Configuring the named fonts (TkDefaultFont etc.) also covers every widget
    that has no explicit font: ttk buttons/labels/entries/tabs, menus, dialogs.
    """
    ui_family = _pick_family(root, UI_FONT_CANDIDATES)
    mono_family = _pick_family(root, MONO_FONT_CANDIDATES)

    if ui_family:
        for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont",
                     "TkCaptionFont", "TkSmallCaptionFont", "TkIconFont", "TkTooltipFont"):
            try:
                tkfont.nametofont(name, root).configure(family=ui_family, size=UI_FONT_SIZE)
            except tk.TclError:
                pass
    if mono_family:
        try:
            tkfont.nametofont("TkFixedFont", root).configure(family=mono_family, size=MONO_FONT_SIZE)
        except tk.TclError:
            pass

    ui_font = (ui_family, UI_FONT_SIZE) if ui_family else "TkDefaultFont"
    mono_font = (mono_family, MONO_FONT_SIZE) if mono_family else "TkFixedFont"
    return ui_font, mono_font


def setup_bold_font(root, ui_font):
    """Return a tkfont.Font for emphasized text: the first installed family from
    BOLD_FONT_CANDIDATES, otherwise the UI font made bold."""
    found = _pick_family(root, [family for family, _ in BOLD_FONT_CANDIDATES])
    if found:
        weight = dict((f.lower(), w) for f, w in BOLD_FONT_CANDIDATES)[found.lower()]
        return tkfont.Font(root=root, family=found, size=UI_FONT_SIZE, weight=weight)
    # Font(font=..., weight=...) ignores the extra options, so configure afterwards.
    fallback = tkfont.Font(root=root, font=ui_font)
    fallback.configure(weight="bold")
    return fallback


# --- Theme -------------------------------------------------------------
# One palette for the whole UI (clam theme + classic tk widgets), so that no
# widget keeps the default white background. Tweak colors here.
PALETTE = {
    "bg":       "#24292e",   # window / frames
    "fg":       "#d9dcde",   # text
    "field":    "#1d2125",   # entries, listboxes, help text, scrollbar troughs
    "canvas":   "#1d2125",   # texture canvas
    "button":   "#2f363d",   # buttons, inactive tabs, scrollbar thumbs
    "active":   "#333b42",   # hover / pressed
    "border":   "#41484f",
    "select":   "#444d56",   # selection background
    "disabled": "#959da5",
    "tab_active": "#1d2125",   # active tab background
    "button_border_hover": "#58a6ff",   # button border on hover
}


def _theme_tk_file_dialog(root, bg, fg):
    """Tk's built-in file dialog (used on Linux, and on Windows via
    USE_TK_FILE_DIALOG) hardcodes a white canvas and black text for the file
    list. Patch its IconList constructor so it uses our colors."""
    if sys.platform == "darwin":
        return
    tcl = root.tk
    try:
        # The classes/procs are autoloaded on first dialog use; load them now.
        tcl.call("auto_load", "::tk::IconList")
        tcl.call("auto_load", "::tk::IconList_Create")
        if tcl.call("info", "commands", "::tk::IconList"):                # newer Tk 8.6 (TclOO)
            arglist, body = tcl.call("info", "class", "definition", "::tk::IconList", "Create")
            kind = "oo"
        elif tcl.call("info", "commands", "::tk::IconList_Create"):       # older Tk 8.6 (procs)
            arglist = tcl.call("info", "args", "::tk::IconList_Create")
            body = tcl.call("info", "body", "::tk::IconList_Create")
            kind = "proc"
        else:
            return
        body = str(body).replace("-background white", f"-background {bg}")
        body = body.replace("set fill black", f"set fill {fg}")
        if kind == "oo":
            tcl.call("oo::define", "::tk::IconList", "method", "Create", arglist, body)
        else:
            tcl.call("proc", "::tk::IconList_Create", arglist, body)
    except tk.TclError:
        pass                                    # cosmetic only: never block startup


def apply_theme(root):
    """Switch ttk to 'clam' and recolor ttk + classic tk widgets from PALETTE.
    Must be called before the widgets are created (option_add only affects
    widgets created afterwards)."""
    c = PALETTE
    style = ttk.Style(root)
    style.theme_use("clam")
    root.configure(background=c["bg"])

    style.configure(".", background=c["bg"], foreground=c["fg"], fieldbackground=c["field"],
                    bordercolor=c["border"], darkcolor=c["bg"], lightcolor=c["bg"],
                    troughcolor=c["field"], focuscolor=c["select"], insertcolor=c["fg"],
                    selectbackground=c["select"], selectforeground=c["fg"])
    style.map(".", foreground=[("disabled", c["disabled"])])

    style.configure("TButton", background=c["button"], bordercolor=c["border"],
                    darkcolor=c["button"], lightcolor=c["button"],
                    padding=(0, 2))

    style.map("TButton",
              background=[("pressed", c["active"]), ("active", c["active"]), ("disabled", c["bg"])],
              lightcolor=[("pressed", c["active"]), ("active", c["button"]), ("focus", c["button"])],
              darkcolor=[("pressed", c["active"]), ("active", c["button"]), ("focus", c["button"])],
              bordercolor=[("disabled", c["border"]), ("pressed", c["border"]),
                          ("active", c["button_border_hover"]), ("focus", c["border"]),
                          ("alternate", c["border"])])

    # Menubuttons (e.g. "Directory" / "Files of type" in Tk's file dialog): clam turns
    # them light on hover and draws a black arrow.
    style.configure("TMenubutton", background=c["button"], foreground=c["fg"],
                    bordercolor=c["border"], darkcolor=c["button"], lightcolor=c["button"],
                    arrowcolor=c["fg"], padding=2)
    style.map("TMenubutton",
              background=[("pressed", c["active"]), ("active", c["active"]),
                          ("disabled", c["bg"])],
              lightcolor=[("pressed", c["active"]), ("active", c["active"])],
              darkcolor=[("pressed", c["active"]), ("active", c["active"])],
              foreground=[("disabled", c["disabled"])],
              arrowcolor=[("disabled", c["disabled"])],
              bordercolor=[("active", c["button_border_hover"])])

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, fieldbackground=c["field"], foreground=c["fg"],
                        bordercolor=c["border"], darkcolor=c["field"], lightcolor=c["field"],
                        insertcolor=c["fg"], arrowcolor=c["fg"])
        style.map(name,
                  fieldbackground=[("disabled", c["bg"]), ("readonly", c["field"])],
                  foreground=[("disabled", c["disabled"])],
                  selectbackground=[("!focus", c["select"])],
                  selectforeground=[("!focus", c["fg"])])
    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.map(name, background=[("readonly", c["field"]), ("disabled", c["bg"])])
    style.configure("TCombobox", background=c["button"])
    style.configure("TSpinbox", background=c["button"])
    style.map("TCombobox", background=[("active", c["active"])])
    style.map("TSpinbox", background=[("active", c["active"])])

    style.configure("TNotebook", background=c["bg"], bordercolor=c["border"])
    style.configure("TNotebook.Tab", background=c["button"], foreground=c["fg"],
                    bordercolor=c["border"])
    style.map("TNotebook.Tab", background=[("selected", c["tab_active"]), ("active", c["active"])],
              lightcolor=[("selected", c["tab_active"]), ("!selected", c["button"])])
    style.configure("TNotebook.Tab", lightcolor=c["button"], darkcolor=c["button"])

    style.configure("TScrollbar", background=c["button"], bordercolor=c["border"],
                    troughcolor=c["field"], arrowcolor=c["fg"],
                    darkcolor=c["button"], lightcolor=c["button"])
    style.map("TScrollbar", background=[("active", c["active"])])

    # Draggable splitter between the side panels and the canvas
    style.configure("TPanedwindow", background=c["bg"])
    style.configure("Sash", sashthickness=4, gripcount=0, background=c["bg"],
                    bordercolor=c["border"], lightcolor=c["bg"], darkcolor=c["bg"])

    style.configure("TLabelframe", background=c["bg"], bordercolor=c["border"])
    style.configure("TLabelframe.Label", background=c["bg"], foreground=c["fg"])

    # Classic tk widgets (Listbox, Text, Menu, dialogs) and the combobox popup list.
    for pattern, value in (
        ("*Listbox.background", c["field"]), ("*Listbox.foreground", c["fg"]),
        ("*Listbox.selectBackground", c["select"]), ("*Listbox.selectForeground", c["fg"]),
        ("*Listbox.highlightThickness", 0), ("*Listbox.borderWidth", 1),
        ("*Listbox.relief", "solid"),
        ("*TCombobox*Listbox.background", c["field"]), ("*TCombobox*Listbox.foreground", c["fg"]),
        ("*TCombobox*Listbox.selectBackground", c["select"]),
        ("*TCombobox*Listbox.selectForeground", c["fg"]),
        ("*Menu.background", c["bg"]), ("*Menu.foreground", c["fg"]),
        ("*Menu.activeBackground", c["select"]), ("*Menu.activeForeground", c["fg"]),
        ("*Dialog.background", c["bg"]), ("*Dialog.foreground", c["fg"]),
    ):
        root.option_add(pattern, value)
    _theme_tk_file_dialog(root, c["field"], c["fg"])
    return c


# --- Themed popups -----------------------------------------------------
# On Windows, tkinter's messagebox/filedialog are native OS dialogs: always light,
# cannot be recolored. There we use our own message boxes and Tk's built-in
# (themeable) file dialog. On Linux Tk already draws its own dialogs; on macOS the
# native ones follow the system appearance.
USE_THEMED_DIALOGS = sys.platform.startswith("win")
USE_TK_FILE_DIALOG = sys.platform.startswith("win")

_APP_ROOT = None     # the Tk root (set by GlyphEditorApp); dialog helpers need it
_ICON_PATH = None    # window icon, also applied to our dialogs


def set_app_root(root):
    global _APP_ROOT
    _APP_ROOT = root


def set_app_icon(icon_path):
    """Set the icon path to be used for all dialog windows."""
    global _ICON_PATH
    _ICON_PATH = icon_path


def apply_window_icon(window, icon_path):
    """Give a window the app icon: .ico via iconbitmap on Windows (the only place
    it works), otherwise iconphoto via PIL. A missing/broken icon never raises.
    Returns the PhotoImage (the caller must keep a reference) or None."""
    if not icon_path or not os.path.exists(icon_path):
        return None
    if sys.platform.startswith("win"):
        try:
            window.iconbitmap(icon_path)
            return None
        except tk.TclError:
            pass
    try:
        with Image.open(icon_path) as ico:
            ico.load()
            photo = ImageTk.PhotoImage(ico.convert("RGBA"))
        window.iconphoto(True, photo)
        return photo
    except Exception:
        return None


def set_dark_titlebar(window):
    """Windows 10 (1809+) / 11: ask DWM for a dark title bar. Silently does nothing elsewhere.
    `window` is a Tk widget or a Tk window path (str)."""
    if not sys.platform.startswith("win"):
        return
    try:
        import ctypes
        tcl = (window if isinstance(window, tk.Misc) else _APP_ROOT).tk
        path = str(window)
        tcl.call("update", "idletasks")
        user32 = ctypes.windll.user32
        # `winfo id` returns a hex string such as '0x00200003' - plain int() would fail on it.
        hwnd = user32.GetParent(int(str(tcl.call("winfo", "id", path)), 0))
        value = ctypes.c_int(1)
        for attr in (20, 19):    # DWMWA_USE_IMMERSIVE_DARK_MODE (20; 19 on early builds)
            if ctypes.windll.dwmapi.DwmSetWindowAttribute(
                    hwnd, attr, ctypes.byref(value), ctypes.sizeof(value)) == 0:
                break
        # Force the non-client area (title bar) to repaint if the window is already visible.
        # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
        user32.SetWindowPos(hwnd, 0, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0004 | 0x0010 | 0x0020)
    except Exception:
        pass


def _darken_tk_dialog_titlebars(root):
    """Tk's built-in file dialog is a Tcl-created toplevel (class TkFDialog): give it a
    dark title bar the moment it is shown."""
    if not USE_TK_FILE_DIALOG or not sys.platform.startswith("win"):
        return
    try:
        root.tk.createcommand("conker_dark_titlebar", set_dark_titlebar)
        root.tk.call("bind", "TkFDialog", "<Map>", "conker_dark_titlebar %W")
    except tk.TclError:
        pass


_ICON_COLORS = {"info": "#4aa3df", "question": "#4aa3df", "warning": "#e5b53b", "error": "#e05252"}


def _themed_dialog(title, message, icon, buttons, default, cancel, parent=None):
    """Modal message box in the app palette. buttons: [(key, label), ...];
    returns the key of the pressed button (cancel key on Esc / window close)."""
    c = PALETTE
    parent = parent or _APP_ROOT
    dlg = tk.Toplevel(parent)
    dlg.withdraw()
    dlg.title(title)
    dlg.configure(background=c["bg"])
    dlg.resizable(False, False)
    if parent is not None:
        dlg.transient(parent.winfo_toplevel())
    dlg._icon_photo = apply_window_icon(dlg, _ICON_PATH)   # keep a reference
    result = {"key": cancel}

    def finish(key):
        result["key"] = key
        dlg.destroy()

    body = ttk.Frame(dlg, padding=(16, 14, 16, 8))
    body.pack(fill=tk.BOTH, expand=True)
    tk.Label(body, bitmap=icon, background=c["bg"], foreground=_ICON_COLORS.get(icon, c["fg"])
             ).grid(row=0, column=0, sticky="n", padx=(0, 14))
    ttk.Label(body, text=message, wraplength=420, justify=tk.LEFT
              ).grid(row=0, column=1, sticky="w")

    row = ttk.Frame(dlg, padding=(16, 4, 16, 14))
    row.pack(fill=tk.X)
    buttons_widgets = {}
    for key, label in reversed(buttons):
        b = ttk.Button(row, text=label, width=10, command=lambda k=key: finish(k))
        b.pack(side=tk.RIGHT, padx=(6, 0))
        buttons_widgets[key] = b

    dlg.bind("<Return>", lambda e: finish(default))
    dlg.bind("<Escape>", lambda e: finish(cancel))
    dlg.protocol("WM_DELETE_WINDOW", lambda: finish(cancel))

    # Center over the parent window, then show modally.
    dlg.update_idletasks()
    w, h = dlg.winfo_reqwidth(), dlg.winfo_reqheight()
    if parent is not None and parent.winfo_viewable():
        top = parent.winfo_toplevel()
        x = top.winfo_rootx() + (top.winfo_width() - w) // 2
        y = top.winfo_rooty() + (top.winfo_height() - h) // 2
    else:
        x = (dlg.winfo_screenwidth() - w) // 2
        y = (dlg.winfo_screenheight() - h) // 2
    dlg.geometry(f"+{max(x, 0)}+{max(y, 0)}")
    set_dark_titlebar(dlg)
    dlg.deiconify()
    dlg.wait_visibility()
    dlg.grab_set()
    buttons_widgets[default].focus_set()
    dlg.wait_window()
    return result["key"]


class _MessageBoxes:
    """Drop-in for tkinter.messagebox (the subset this app uses)."""

    def _native(self, name, title, message, kw):
        return getattr(_native_messagebox, name)(title, message, **kw)

    def showinfo(self, title, message, **kw):
        if not USE_THEMED_DIALOGS:
            return self._native("showinfo", title, message, kw)
        _themed_dialog(title, message, "info", [("ok", "OK")], "ok", "ok", kw.get("parent"))
        return "ok"

    def showwarning(self, title, message, **kw):
        if not USE_THEMED_DIALOGS:
            return self._native("showwarning", title, message, kw)
        _themed_dialog(title, message, "warning", [("ok", "OK")], "ok", "ok", kw.get("parent"))
        return "ok"

    def showerror(self, title, message, **kw):
        if not USE_THEMED_DIALOGS:
            return self._native("showerror", title, message, kw)
        _themed_dialog(title, message, "error", [("ok", "OK")], "ok", "ok", kw.get("parent"))
        return "ok"

    def askyesno(self, title, message, **kw):
        if not USE_THEMED_DIALOGS:
            return self._native("askyesno", title, message, kw)
        return _themed_dialog(title, message, "question", [("yes", "Yes"), ("no", "No")],
                              "yes", "no", kw.get("parent")) == "yes"

    def askyesnocancel(self, title, message, **kw):
        if not USE_THEMED_DIALOGS:
            return self._native("askyesnocancel", title, message, kw)
        key = _themed_dialog(title, message, "question",
                             [("yes", "Yes"), ("no", "No"), ("cancel", "Cancel")],
                             "yes", "cancel", kw.get("parent"))
        return {"yes": True, "no": False}.get(key)


def _tk_file_dialog(kind, options):
    """Call Tk's own (Tcl-implemented, themeable) file dialog: kind is 'open' or 'save'."""
    root = options.get("parent") or _APP_ROOT
    args = []
    for key, value in options.items():
        if key == "parent":
            value = str(value)
        if value:
            args += ["-" + key, value]
    if "parent" not in options:
        args += ["-parent", str(root)]
    root.tk.call("auto_load", "::tk::dialog::file::")

    # Apply dark titlebar and icon immediately using after_idle
    def apply_dialog_settings():
        try:
            root.tk.call("update", "idletasks")
            for w in root.tk.call("winfo", "children", "."):
                try:
                    if root.tk.call("winfo", "class", w) == "TkFDialog":
                        set_dark_titlebar(w)
                        root.tk.call("wm", "geometry", w, "480x320")

                        # Set icon for file dialog
                        if _ICON_PATH and os.path.exists(_ICON_PATH):
                            try:
                                if sys.platform.startswith("win"):
                                    root.tk.call("wm", "iconbitmap", w, _ICON_PATH)
                            except tk.TclError:
                                pass
                except tk.TclError:
                    pass
        except Exception:
            pass

    root.after_idle(apply_dialog_settings)

    result = root.tk.call("::tk::dialog::file::", kind, *args)
    # Cancel comes back as an empty Tcl value (tkinter shows it as an empty tuple).
    if not result:
        return ""
    if isinstance(result, (tuple, list)):
        result = result[0]
    return str(result)


class _FileDialogs:
    """Drop-in for tkinter.filedialog (askopenfilename / asksaveasfilename)."""

    def askopenfilename(self, **options):
        if USE_TK_FILE_DIALOG:
            try:
                return _tk_file_dialog("open", options)
            except tk.TclError:
                pass                              # fall back to the native dialog
        return _native_filedialog.askopenfilename(**options)

    def asksaveasfilename(self, **options):
        if USE_TK_FILE_DIALOG:
            try:
                return _tk_file_dialog("save", options)
            except tk.TclError:
                pass
        return _native_filedialog.asksaveasfilename(**options)


messagebox = _MessageBoxes()
filedialog = _FileDialogs()


class GlyphEditorApp:
    def __init__(self, root):
        self.root = root
        set_app_root(root)
        self.ui_font, self.mono_font = setup_fonts(root)
        self.bold_font = setup_bold_font(root, self.ui_font)   # keep a reference
        self.colors = apply_theme(root)
        set_dark_titlebar(root)
        _darken_tk_dialog_titlebars(root)
        self.root.title("Conker Glyph Editor")
        self.root.geometry("1200x760")

        # Set window icon
        # Handle both development and PyInstaller-compiled environments
        if getattr(sys, 'frozen', False):
            # Running in a PyInstaller bundle
            base_path = sys._MEIPASS
        else:
            # Running in normal Python environment
            base_path = os.path.dirname(__file__)

        icon_path = os.path.join(base_path, "resources", "icon.ico")
        if os.path.exists(icon_path):
            set_app_icon(icon_path)
            self._icon_photo = apply_window_icon(self.root, icon_path)   # keep a reference

        self.font = None               # ConkerFont instance
        self.tex_image = None          # PIL.Image of the original texture
        self.tex_photo = None          # ImageTk.PhotoImage for display (cached per zoom)
        self._tex_photo_zoom = None    # zoom level tex_photo was rendered for
        self.zoom = DEFAULT_ZOOM
        self.selected_index = None
        self.selected_code = None   # Current charmap code for the selected glyph
        self.current_file_path = None  # Track current file path
        self.drag_mode = None          # None | "move" | "x0y0" | "x1y0" | "x0y1" | "x1y1"
        self.drag_start = None
        self.drag_orig_rect = None
        self.unsaved_changes = False

        self._build_ui()
        self.root.protocol("WM_DELETE_WINDOW", self.on_closing)

    # ------------------------------------------------------------------ UI

    def _add_pane(self, frame, min_width, weight):
        """Add a pane to the main PanedWindow and remember its minimum width."""
        self._paned.add(frame, weight=weight)
        self._pane_min_widths.append(min_width)

    def _enable_pane_clamp(self, paned):
        """Keep every pane at or above its minimum width while a sash is dragged.
        The widget's bindtags are reordered so the class binding (which moves the
        sash) runs first, then we clamp the result."""
        tags = paned.bindtags()
        paned.bindtags((tags[1], tags[0]) + tags[2:])
        paned.bind("<B1-Motion>", self._clamp_sashes, add="+")
        paned.bind("<ButtonRelease-1>", self._clamp_sashes, add="+")

    def _clamp_sashes(self, _event=None):
        paned = self._paned
        mins = self._pane_min_widths
        n = len(mins) - 1                      # number of sashes
        if n < 1 or len(paned.panes()) != len(mins):
            return
        pos = [paned.sashpos(i) for i in range(n)]
        total = paned.winfo_width()
        for i in range(n):                     # left -> right: left pane minimum
            prev = pos[i - 1] if i else 0
            pos[i] = max(pos[i], prev + mins[i])
        for i in reversed(range(n)):           # right -> left: right pane minimum
            nxt = pos[i + 1] if i + 1 < n else total
            pos[i] = min(pos[i], nxt - mins[i + 1])
        for i in range(n):
            if pos[i] != paned.sashpos(i):
                paned.sashpos(i, pos[i])

    @staticmethod
    def _char_display(code, fallback=None):
        """Printable form of a code point; control codes become U+XXXX (or fallback)."""
        if code >= 0x20:
            return chr(code)
        return fallback if fallback is not None else f"U+{code:04X}"

    def _mark_modified(self, message):
        self.unsaved_changes = True
        self.status_var.set(f"{message} (changes not saved to disk)")

    def _commit_glyph_edit(self, g, refresh_list=True):
        """Write an edited glyph back into the font and refresh the UI."""
        self.font.write_glyph(g)
        if refresh_list:
            self._refresh_glyph_list()
        self.select_glyph(g.index, preferred_code=self.selected_code)
        self._mark_modified(f"Glyph #{g.index} updated")

    @staticmethod
    def _insert_marked_text(text_widget, content):
        """Insert content into a Text widget; fragments wrapped in **...** get the
        "bold" tag (the markers themselves are not shown)."""
        for i, chunk in enumerate(re.split(r"\*\*(.+?)\*\*", content, flags=re.S)):
            if chunk:
                text_widget.insert(tk.END, chunk, ("bold",) if i % 2 else ())

    def _build_ui(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(6, 2))

        ttk.Button(toolbar, text="Open .bin...", command=self.open_bin, padding=(8, 2)).pack(side=tk.LEFT, padx=(0, 3))
        ttk.Button(toolbar, text="Open texture...", command=self.open_texture, padding=(8, 2)).pack(side=tk.LEFT, padx=(3, 0))

        ttk.Label(toolbar, text="Profile:").pack(side=tk.LEFT, padx=(16, 2))
        self.profile_var = tk.StringVar(value="ConkerFont")
        profile_combo = ttk.Combobox(
            toolbar, textvariable=self.profile_var,
            values=list(FONT_PROFILES.keys()), state="readonly", width=20
        )
        profile_combo.pack(side=tk.LEFT)
        profile_combo.bind("<<ComboboxSelected>>", lambda e: self.on_profile_changed())

        ttk.Label(toolbar, text="Zoom:").pack(side=tk.LEFT, padx=(16, 2))
        self.zoom_var = tk.IntVar(value=DEFAULT_ZOOM)
        zoom_spin = ttk.Spinbox(toolbar, from_=MIN_ZOOM, to=MAX_ZOOM, textvariable=self.zoom_var,
                                 width=4, command=self.on_zoom_changed)
        zoom_spin.pack(side=tk.LEFT)
        for sequence in ("<Return>", "<FocusOut>"):      # typed values count too
            zoom_spin.bind(sequence, lambda e: self.on_zoom_changed())

        ttk.Button(toolbar, text="Save As...", command=self.save_as, padding=(8, 2)).pack(side=tk.RIGHT, padx=(3, 0))
        ttk.Button(toolbar, text="Save (overwrite)", command=self.save_overwrite, padding=(8, 2)).pack(side=tk.RIGHT, padx=(0, 3))

        self.status_var = tk.StringVar(value="Open default.bin and texture to start.")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # One PanedWindow hosts left / center / right. The same sash logic serves
        # both splitters; the center pane absorbs window resizing (weight=1).
        main = ttk.PanedWindow(self.root, orient=tk.HORIZONTAL)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)
        self._paned = main
        self._pane_min_widths = []
        self._enable_pane_clamp(main)

        # Left panel: glyph list / charmap, in tabs
        left = ttk.Frame(main, width=224)
        left.pack_propagate(False)
        self._add_pane(left, min_width=150, weight=0)

        self.left_notebook = ttk.Notebook(left)
        self.left_notebook.pack(fill=tk.BOTH, expand=True, padx=(4, 0), pady=4)

        if isinstance(self.ui_font, tuple):
            font_name = self.ui_font[0]
        else:                       # named Tk font ("TkDefaultFont" fallback)
            font_name = tkfont.nametofont(self.ui_font, self.root).actual("family")
        tab_width = TAB_WIDTHS.get(font_name, 13)

        # --- Tabs ---
        style = ttk.Style(self.root)
        style.configure("TNotebook.Tab", width=tab_width, padding=(0, 4, 0, 4), anchor="center")
        # 'clam' maps its own padding for the selected tab ("6 4 6 2"), which overrides
        # the line above and makes tabs resize on click. Pin it for every state.
        style.map("TNotebook.Tab", padding=[("selected", (0, 7, 0, 4))], expand=[("selected", (0, 0, 0, 0))])

        # --- Tab 1: Glyphs ---
        glyphs_tab = ttk.Frame(self.left_notebook)
        self.left_notebook.add(glyphs_tab, text="Glyphs")

        glyph_list_frame = ttk.Frame(glyphs_tab)
        glyph_list_frame.pack(fill=tk.BOTH, expand=True, pady=0)

        glyph_scrollbar = ttk.Scrollbar(glyph_list_frame)
        glyph_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.glyph_listbox = tk.Listbox(glyph_list_frame, yscrollcommand=glyph_scrollbar.set,
                                         font=self.mono_font,
                                         selectmode=tk.SINGLE, exportselection=False,
                                         borderwidth=0, relief="flat")
        self.glyph_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        glyph_scrollbar.config(command=self.glyph_listbox.yview)
        self.glyph_listbox.bind("<<ListboxSelect>>", self.on_listbox_select)

        # --- Tab 2: Charmap ---
        charmap_tab = ttk.Frame(self.left_notebook)
        self.left_notebook.add(charmap_tab, text="Charmap")

        charmap_list_frame = ttk.Frame(charmap_tab)
        charmap_list_frame.pack(fill=tk.BOTH, expand=True, pady=0)

        charmap_scrollbar = ttk.Scrollbar(charmap_list_frame)
        charmap_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.charmap_listbox = tk.Listbox(charmap_list_frame, yscrollcommand=charmap_scrollbar.set,
                                           font=self.mono_font,
                                           selectmode=tk.SINGLE, exportselection=False,
                                           borderwidth=0, relief="flat")
        self.charmap_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        charmap_scrollbar.config(command=self.charmap_listbox.yview)
        self.charmap_listbox.bind("<<ListboxSelect>>", self.on_charmap_listbox_select)
        # Maps a row in self.charmap_listbox to (glyph_index, code_or_None)
        self._charmap_row_data = []
        # Maps glyph_index -> preferred row in charmap_listbox to highlight
        self._charmap_index_by_glyph = {}
        # Maps (glyph_index, code) -> exact row, so selecting a specific
        # character keeps that exact row highlighted instead of falling
        # back to the glyph's generic preferred row
        self._charmap_row_by_glyph_and_code = {}
        # Maps a row in self.glyph_listbox to (glyph_index, preferred_code_or_None)
        self._glyph_row_data = []

        # Center panel: texture canvas
        center = ttk.Frame(main)
        self._add_pane(center, min_width=250, weight=1)

        canvas_frame = ttk.Frame(center)
        canvas_frame.pack(fill=tk.BOTH, expand=True, pady=(4, 0))

        hbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(canvas_frame, bg=self.colors["canvas"],
                                 highlightthickness=1,
                                 highlightbackground=self.colors["border"],
                                 highlightcolor=self.colors["border"],
                                 xscrollcommand=hbar.set, yscrollcommand=vbar.set)
        hbar.config(command=self.canvas.xview)
        vbar.config(command=self.canvas.yview)

        self.canvas.grid(row=0, column=0, sticky="nsew")
        vbar.grid(row=0, column=1, sticky="ns")
        hbar.grid(row=1, column=0, sticky="ew")
        canvas_frame.rowconfigure(0, weight=1)
        canvas_frame.columnconfigure(0, weight=1)

        self.canvas.bind("<Button-1>", self.on_canvas_click)
        self.canvas.bind("<B1-Motion>", self.on_canvas_drag)
        self.canvas.bind("<ButtonRelease-1>", self.on_canvas_release)

        # Right panel: selected glyph properties
        right = ttk.Frame(main, width=265)
        right.pack_propagate(False)
        self._add_pane(right, min_width=200, weight=0)

        props = ttk.LabelFrame(right, text="Glyph Properties")
        props.pack(fill=tk.X, padx=(0, 4), pady=4)

        self.prop_vars = {}
        prop_fields = [
            ("index", "Index"),
            ("x0", "Start X"),
            ("y0", "Start Y"),
            ("x1", "End X"),
            ("y1", "End Y"),
            ("field1_lo", "X Bearing (-←/+→)"),
            ("field1_hi", "Y Bearing (-↑/+↓)"),
            ("field2_lo", "Glyph Width (↔)"),
            ("field2_hi", "Glyph Height (↕)"),
            ("byte14", "Advance Width"),
        ]
        for key, label in prop_fields:
            row = ttk.Frame(props)
            row.pack(fill=tk.X, pady=(2, 1))
            ttk.Label(row, text=label + ":", width=17, anchor="e").pack(side=tk.LEFT, padx=(0, 2))
            var = tk.StringVar(value="")
            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
            self.prop_vars[key] = var
            if key == "index":
                entry.config(state="readonly")

        ttk.Button(props, text="Apply Changes", command=self.apply_property_edits, padding=2).pack(
            fill=tk.X, padx=2, pady=2
        )

        # Character Management - separate frame
        char_mgmt_frame = ttk.LabelFrame(right, text="Character Management")
        char_mgmt_frame.pack(fill=tk.X, padx=(0, 4), pady=4)

        # First row for action selector
        action_row = ttk.Frame(char_mgmt_frame)
        action_row.pack(fill=tk.X, pady=(2, 1))

        ttk.Label(action_row, text="Action:", width=17, anchor="e").pack(
            side=tk.LEFT, padx=(0, 2)
        )

        self.char_action_var = tk.StringVar(value="add")
        char_action_combo = ttk.Combobox(
            action_row, textvariable=self.char_action_var,
            values=["add", "remove", "replace", "clear_glyph"], state="readonly", width=10
        )
        char_action_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        char_action_combo.bind("<<ComboboxSelected>>", self.on_char_action_changed)

        # Second row for old char (used only for replace/remove)
        replace_row = ttk.Frame(char_mgmt_frame)
        replace_row.pack(fill=tk.X, pady=(2, 1))

        ttk.Label(replace_row, text="Old Char:", width=17, anchor="e").pack(
            side=tk.LEFT, padx=(0, 2)
        )

        self.old_char_var = tk.StringVar(value="")
        self.old_char_combo = ttk.Combobox(replace_row, textvariable=self.old_char_var, width=11)
        self.old_char_combo.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))
        self.old_char_combo.config(state="disabled")  # Initially disabled

        # Third row for new char
        char_mgmt_row = ttk.Frame(char_mgmt_frame)
        char_mgmt_row.pack(fill=tk.X, pady=(2, 1))

        ttk.Label(char_mgmt_row, text="New Char:", width=17, anchor="e").pack(
            side=tk.LEFT, padx=(0, 2)
        )

        self.char_mgmt_var = tk.StringVar(value="")
        self.char_mgmt_entry = ttk.Entry(char_mgmt_row, textvariable=self.char_mgmt_var, width=8)
        self.char_mgmt_entry.pack(side=tk.LEFT, fill=tk.X, expand=True, padx=(0, 2))

        # Fourth row for apply button
        ttk.Button(char_mgmt_frame, text="Apply", command=self.apply_char_action, padding=2).pack(
            fill=tk.X, padx=2, pady=2
        )

        help_frame = ttk.LabelFrame(right, text="Help / Info")
        help_frame.pack(fill=tk.BOTH, expand=True, padx=(0, 4), pady=4)

        # Create scrollable text widget for help
        help_scrollbar = ttk.Scrollbar(help_frame)
        help_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        help_text = tk.Text(help_frame, wrap=tk.WORD, width=30, height=10,
                            yscrollcommand=help_scrollbar.set,
                            font=self.ui_font, state=tk.DISABLED,
                            relief=tk.FLAT, highlightthickness=0,
                            background=self.colors["field"],
                            foreground=self.colors["fg"],
                            insertbackground=self.colors["fg"])
        help_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        help_scrollbar.config(command=help_text.yview)

        help_content = (
            "**LMB** on glyph on texture - select.\n\n"
            "**Drag corner handle** - resize rectangle.\n"
            "**Drag glyph** - move rectangle.\n\n"
            "You can also manually enter Start/End X/Y (in texture pixels) and click **'Apply Changes'**.\n\n"
            "**Action selector:** choose operation then, fill relevant fields and Apply.\n\n"
            "**Old Char / New Char** accept one BMP character or U+XXXX.\n\n"
            "**add:** maps New Char to the selected glyph (in addition to any existing characters). Duplicate codes are rejected.\n"
            "**remove:** deletes the character chosen in Old Char from the selected glyph.\n"
            "**replace:** reassigns the character chosen in Old Char to point to New Char instead.\n"
            "**clear_glyph:** clears the whole selected glyph. Clears every charmap entry pointing to it (like an empty slot) and zeroes the glyph's own data. Asks for confirmation first; cannot be undone once saved.\n\n"
            "**VERIFIED IN-GAME (via XEMU):**\n"
            "**Glyph Width/Height:** physical glyph size. Changing these visibly stretches/quashes the glyph on screen along X/Y. Stored as **TWO** independent bytes, not one number.\n"
            "**Advance Width:** horizontal step after this character - where the next one starts.\n"
            "**X/Y Bearing:** offset of the glyph from the baseline. Negative X = left, positive X = right. Positive Y = lower, negative Y = higher. Shown/entered as **SIGNED** bytes (-128..127).\n\n"
            "Pixel-conversion formula (both X and Y) found empirically."
        )
        help_text.tag_configure("bold", font=self.bold_font)
        help_text.config(state=tk.NORMAL)
        self._insert_marked_text(help_text, help_content)
        help_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------- actions

    def _report_unexpected_error(self, title, error):
        """Show an unexpected exception (most likely a bug) and print its
        traceback to the console so it is not hidden behind a message box."""
        traceback.print_exc()
        messagebox.showerror(
            title,
            f"Unexpected error ({type(error).__name__}):\n{error}\n\n"
            "Details were printed to the console.",
        )

    def _confirm_discard_or_save(self):
        """Ask what to do with unsaved edits.

        Returns True when it is safe to continue (nothing to save, changes
        were saved, or the user chose to discard them) and False when the
        user cancelled or saving did not actually happen.
        """
        if not self.unsaved_changes or self.font is None:
            return True
        answer = messagebox.askyesnocancel(
            "Unsaved Changes",
            "You have unsaved changes.\n\nSave before continuing?",
        )
        if answer is None:      # Cancel
            return False
        if answer:              # Yes: continue only if the save really succeeded
            return self.save_overwrite()
        return True             # No: discard changes

    def on_closing(self):
        if self._confirm_discard_or_save():
            self.root.destroy()

    def open_bin(self):
        path = filedialog.askopenfilename(
            title="Open default.bin",
            filetypes=[("BIN files", "*.bin *.BIN"), ("All files", "*")],
        )
        if path and self._confirm_discard_or_save():
            self.load_bin(path)

    def load_bin(self, path):
        """Load a .bin into the editor. Returns True on success; failures are
        reported to the user and leave the current font untouched."""
        try:
            font = ConkerFont(path, profile_name=None)  # auto-detect the profile
        except EXPECTED_LOAD_ERRORS as e:
            messagebox.showerror("Loading Error", str(e))
            return False
        except Exception as e:
            self._report_unexpected_error("Critical Loading Error", e)
            return False

        keep_selection = (self.current_file_path == path
                          and self.selected_index is not None
                          and self.selected_index < len(font.glyphs))
        self.font = font
        self.profile_var.set(font.profile_name)
        self.current_file_path = path
        self.unsaved_changes = False

        if not keep_selection:
            self.selected_index = None
            self.selected_code = None
            for var in self.prop_vars.values():
                var.set("")

        self._refresh_glyph_list()
        if keep_selection:
            self.select_glyph(self.selected_index)   # also redraws the canvas
        else:
            self._redraw_canvas()
        how = "auto-detected" if font.profile_autodetected else "not detected, default used"
        self.status_var.set(
            f"Loaded {os.path.basename(path)}: {font.glyph_count} glyphs, "
            f"profile {font.profile_name} ({how})"
        )
        return True

    def open_texture(self):
        path = filedialog.askopenfilename(
            title="Open Texture",
            filetypes=[("Images", "*.png *.PNG *.bmp *.BMP"), ("All files", "*")],
        )
        if path:
            self.load_texture(path)

    def load_texture(self, path):
        """Load a texture image. Returns True on success; failures are reported."""
        try:
            with Image.open(path) as img:
                if img.format not in ("PNG", "BMP"):
                    messagebox.showwarning(
                        "Texture Format Warning",
                        f"Image format '{img.format}' may not be supported. PNG or BMP recommended."
                    )
                if img.width > 1024 or img.height > 1024:
                    messagebox.showwarning(
                        "Texture Size Warning",
                        f"Image dimensions ({img.width}x{img.height}) are very large. This may cause performance issues."
                    )
                tex_image = img.convert("RGB")
        except EXPECTED_LOAD_ERRORS as e:
            messagebox.showerror("Texture Loading Error", str(e))
            return False
        except Exception as e:
            self._report_unexpected_error("Critical Texture Loading Error", e)
            return False
        self.tex_image = tex_image
        self._tex_photo_zoom = None      # force the scaled copy to be rebuilt
        self._redraw_canvas()
        self.status_var.set(
            f"Texture loaded: {os.path.basename(path)} ({tex_image.width}x{tex_image.height})"
        )
        return True

    def load_paths(self, bin_path, tex_path=None):
        """Load files given on the command line (missing paths are reported)."""
        for path, loader in ((bin_path, self.load_bin), (tex_path, self.load_texture)):
            if path is None:
                continue
            if os.path.exists(path):
                loader(path)
            else:
                self.status_var.set(f"File not found: {path}")

    def on_profile_changed(self):
        if self.font is None:
            return
        name = self.profile_var.get()
        self.font.profile_name = name
        self.font.profile = FONT_PROFILES[name]
        # The pixel fields and every rectangle depend on the profile: rebuild them,
        # otherwise "Apply Changes" would mix old-profile pixels with the new profile.
        if self.selected_index is not None:
            self.select_glyph(self.selected_index, preferred_code=self.selected_code)
        else:
            self._redraw_canvas()

    def on_zoom_changed(self):
        try:
            value = int(self.zoom_var.get())
        except (tk.TclError, ValueError):       # empty / non-numeric text in the box
            value = self.zoom
        new_zoom = max(MIN_ZOOM, min(MAX_ZOOM, value))
        self.zoom_var.set(new_zoom)
        if new_zoom != self.zoom:
            self.zoom = new_zoom
            self._redraw_canvas()

    def save_as(self):
        if self.font is None:
            messagebox.showinfo("No Data", "Please open default.bin first")
            return False
        path = filedialog.asksaveasfilename(
            title="Save As",
            defaultextension=".bin",
            filetypes=[("BIN files", "*.bin *.BIN"), ("All files", "*")],
        )
        if not path:
            return False
        try:
            self.font.save(path)
        except OSError as e:
            messagebox.showerror("Save Error", str(e))
            return False
        self.font.path = path               # later "Save (overwrite)" targets this file
        self.current_file_path = path
        self.unsaved_changes = False
        self.status_var.set(f"Saved: {path}")
        return True

    def save_overwrite(self):
        if self.font is None:
            messagebox.showinfo("No Data", "Please open default.bin first")
            return False
        if not messagebox.askyesno(
            "Confirmation",
            f"Overwrite original file?\n{self.font.path}\n\n"
            "Making a backup copy first is recommended."
        ):
            return False
        try:
            self.font.save()
        except OSError as e:
            messagebox.showerror("Save Error", str(e))
            return False
        self.unsaved_changes = False
        self.status_var.set(f"File overwritten: {self.font.path}")
        return True

    # --------------------------------------------------------------- list

    def _refresh_glyph_list(self):
        """Repopulate the glyph list.

        Rows are built first and inserted with a single Listbox.insert() call
        (one insert per row makes Tk redraw thousands of times).  The
        glyph -> codes lookup is also built once, instead of scanning the whole
        charmap for every glyph.
        """
        self.glyph_listbox.delete(0, tk.END)
        self._glyph_row_data = []
        if self.font is None:
            self._refresh_charmap_list()
            return

        codes_by_glyph = {}
        for code, idx in self.font.charmap.items():
            codes_by_glyph.setdefault(idx, []).append(code)

        rows = []
        row_data = []
        for g in self.font.glyphs:
            # All characters mapped to this glyph, sorted by code point
            glyph_codes = sorted(codes_by_glyph.get(g.index, ()))
            if glyph_codes:
                char_disp = "".join(chr(code) for code in glyph_codes)
                # Use the lowest code as the preferred one
                preferred_code = glyph_codes[0]
            else:
                char_disp = "\u00b7"
                preferred_code = None
            special = " [spec]" if g.is_special else ""
            rows.append(f"{g.index:3d}  char={char_disp}  adv={g.byte14:3d}{special}")
            row_data.append((g.index, preferred_code))

        if rows:
            self.glyph_listbox.insert(tk.END, *rows)
        self._glyph_row_data = row_data
        self._refresh_charmap_list()

    def on_listbox_select(self, event):
        sel = self.glyph_listbox.curselection()
        if not sel:
            return
        row = sel[0]
        if row >= len(self._glyph_row_data):
            return
        glyph_index, preferred_code = self._glyph_row_data[row]
        self.select_glyph(glyph_index, preferred_code=preferred_code)

    def _refresh_charmap_list(self):
        """Populates the Charmap tab: one row per assigned code point (char,
        code, glyph index), plus - at the end - the raw empty slots of the
        fixed charmap hash table (physical FFFF FFFF entries), so gaps in
        the charmap itself are visible, not just glyphs without a character.

        Builds the whole set of rows first and inserts them in a single
        Listbox.insert() call - inserting thousands of rows one at a time
        (there can be up to ~2048 charmap slots) is what made this dialog
        feel like it hung after every glyph edit/drag."""
        self.charmap_listbox.delete(0, tk.END)
        self._charmap_row_data = []
        self._charmap_index_by_glyph = {}
        self._charmap_row_by_glyph_and_code = {}
        if self.font is None:
            return

        rows = []

        # Assigned charmap entries, sorted by code point for readability.
        for code, glyph_index in sorted(self.font.charmap.items()):
            char_disp = self._char_display(code, fallback="·")
            rows.append(f"U+{code:04X}  {char_disp}   -> glyph #{glyph_index}")
            self._charmap_row_data.append((glyph_index, code))

        # Raw empty (FFFF FFFF) slots in the fixed charmap hash table.
        empty_count = self.font.count_empty_charmap_slots()
        if empty_count:
            rows.append(f"--- empty slots ({empty_count}) ---")
            self._charmap_row_data.append((None, None))

        if rows:
            self.charmap_listbox.insert(tk.END, *rows)

        # Precompute, for each glyph index, the preferred row to highlight:
        # prefer a row whose code matches the glyph's displayed character,
        # otherwise the first row that maps to that glyph. Built once here
        # instead of re-scanning every row on every select_glyph() call.
        for row, (glyph_index, code) in enumerate(self._charmap_row_data):
            if glyph_index is None:
                continue
            self._charmap_row_by_glyph_and_code[(glyph_index, code)] = row
            if glyph_index not in self._charmap_index_by_glyph:
                self._charmap_index_by_glyph[glyph_index] = row
            g = self.font.glyphs[glyph_index] if glyph_index < len(self.font.glyphs) else None
            if g is not None and g.char and code == ord(g.char):
                self._charmap_index_by_glyph[glyph_index] = row

    def on_charmap_listbox_select(self, event):
        sel = self.charmap_listbox.curselection()
        if not sel:
            return
        row = sel[0]
        if row >= len(self._charmap_row_data):
            return
        glyph_index, code = self._charmap_row_data[row]
        if glyph_index is None:
            return
        self.select_glyph(glyph_index, preferred_code=code)

    @staticmethod
    def _byte_to_signed(unsigned_val):
        """Converts a raw 0-255 byte value to its signed int8 reading (-128..127)."""
        return unsigned_val - 256 if unsigned_val > 127 else unsigned_val

    @staticmethod
    def _signed_to_byte(signed_val):
        """Converts a signed int8 value (-128..127) back to the raw 0-255 byte
        stored in the file."""
        if not (-128 <= signed_val <= 127):
            raise ValueError("value must be between -128 and 127 (it's a signed byte)")
        return signed_val + 256 if signed_val < 0 else signed_val

    @staticmethod
    def _parse_character(value):
        """Parse one BMP character or a U+XXXX / 0xXXXX code-point literal."""
        error = "Character must be one symbol or U+XXXX"
        text = value.strip()
        if text[:2].upper() in ("U+", "0X"):
            try:
                code = int(text[2:], 16)
            except ValueError as e:
                raise ValueError(error) from e
        else:
            # Do not strip this path: a literal space is a valid character.
            if len(value) != 1:
                raise ValueError(error)
            code = ord(value)

        if not (0 <= code < 0xFFFF):
            raise ValueError("Character must be a BMP code point from U+0000 to U+FFFE")
        return code

    def select_glyph(self, index, preferred_code=None):
        if self.font is None or index is None or index >= len(self.font.glyphs):
            return
        self.selected_index = index
        g = self.font.glyphs[index]

        glyph_codes = self.font.codes_for_glyph(index)
        if preferred_code not in glyph_codes:
            preferred_code = ord(g.char) if g.char else None
        if preferred_code in glyph_codes:
            self.selected_code = preferred_code
        elif len(glyph_codes) == 1:
            self.selected_code = glyph_codes[0]
        else:
            self.selected_code = None

        self.prop_vars["index"].set(str(g.index))
        # field1 is stored as one uint16 in the file, but behaves as two independent
        # bytes. CONFIRMED IN-GAME: lo byte = X Bearing (horizontal offset from the
        # baseline - negative shifts left, positive shifts right), hi byte = Y Bearing
        # (vertical offset from the baseline - positive moves the glyph down, negative
        # moves it up). Displayed/edited here as SIGNED int8 (-128..127),
        # since the raw unsigned readings (e.g. 254/255) only made sense once
        # reinterpreted as small negative numbers (-2/-1).
        f1_hi, f1_lo = g.field1 >> 8, g.field1 & 0xFF
        self.prop_vars["field1_lo"].set(str(self._byte_to_signed(f1_lo)))
        self.prop_vars["field1_hi"].set(str(self._byte_to_signed(f1_hi)))
        # field2 is stored as one uint16 in the file, but behaves as two INDEPENDENT
        # single-byte values. CONFIRMED IN-GAME: lo byte = Glyph Width, hi byte =
        # Glyph Height - changing either one visibly stretches/squashes the glyph on
        # screen along that axis (not just the atlas rectangle size).
        # Displayed with -1 offset for user convenience.
        f2_hi, f2_lo = g.field2 >> 8, g.field2 & 0xFF
        self.prop_vars["field2_lo"].set(str(f2_lo - 1))
        self.prop_vars["field2_hi"].set(str(f2_hi - 1))
        # byte14 CONFIRMED IN-GAME to be the real Advance Width - it determines
        # where the NEXT character starts, unlike the 'Unknown' hi/lo field above
        # which showed no visible effect when changed in isolation.
        self.prop_vars["byte14"].set(str(g.byte14))

        pixels = self.font.to_pixels(g)
        if pixels:
            x0, y0, x1, y1 = pixels
            self.prop_vars["x0"].set(f"{x0:.0f}")
            self.prop_vars["y0"].set(f"{y0:.0f}")
            self.prop_vars["x1"].set(f"{x1:.0f}")
            self.prop_vars["y1"].set(f"{y1:.0f}")
        else:
            for k in ("x0", "y0", "x1", "y1"):
                self.prop_vars[k].set("(special glyph)")

        self._redraw_canvas()

        # Keep the glyph list selection in sync
        self.glyph_listbox.selection_clear(0, tk.END)
        self.glyph_listbox.selection_set(index)
        self.glyph_listbox.see(index)

        # Update old char combo when glyph selection changes
        self._update_old_char_combo()

        # Defer charmap listbox sync to avoid reentrancy issues
        self.root.after_idle(self._sync_charmap_selection, index)

    def _sync_charmap_selection(self, index):
        self.charmap_listbox.selection_clear(0, tk.END)
        target_row = None
        # Prefer the row matching the character actually selected (e.g. the
        # user clicked the glyph's second/third mapped character in the
        # Charmap tab) over the glyph's generic "preferred" row, so selecting
        # a specific code point doesn't visually snap back to the first one.
        if self.selected_code is not None:
            target_row = self._charmap_row_by_glyph_and_code.get((index, self.selected_code))
        if target_row is None:
            target_row = self._charmap_index_by_glyph.get(index)
        if target_row is not None:
            self.charmap_listbox.selection_set(target_row)
            self.charmap_listbox.see(target_row)

    def apply_property_edits(self):
        if self.font is None or self.selected_index is None:
            return
        orig = self.font.glyphs[self.selected_index]
        g = orig.clone()
        try:
            # field1_lo and field1_hi are treated as two INDEPENDENT single bytes,
            # entered/displayed as SIGNED int8 (-128..127) - see select_glyph for why.
            f1_lo = self._signed_to_byte(int(self.prop_vars["field1_lo"].get()))
            f1_hi = self._signed_to_byte(int(self.prop_vars["field1_hi"].get()))
            g.field1 = (f1_hi << 8) | f1_lo

            # field2_lo and field2_hi are likewise INDEPENDENT single bytes, each
            # validated and packed separately. The user enters them with a -1
            # offset, so 1 is added back when storing.
            f2_lo = int(self.prop_vars["field2_lo"].get()) + 1
            f2_hi = int(self.prop_vars["field2_hi"].get()) + 1
            if not (0 <= f2_lo <= 255):
                raise ValueError("Glyph Width must be an integer between -1 and 254 (displayed with -1 offset)")
            if not (0 <= f2_hi <= 255):
                raise ValueError("Glyph Height must be an integer between -1 and 254 (displayed with -1 offset)")
            g.field2 = (f2_hi << 8) | f2_lo

            byte14_val = int(self.prop_vars["byte14"].get())
            if not (0 <= byte14_val <= 255):
                raise ValueError("Advance Width must be an integer between 0 and 255 (it's a single byte in the file)")
            g.byte14 = byte14_val

            if not g.is_special:
                rect = tuple(float(self.prop_vars[k].get()) for k in ("x0", "y0", "x1", "y1"))
                # raw -> pixels -> raw is lossy: rewrite the rectangle only if it was edited
                if rect != self.font.to_pixels(orig):
                    self.font.set_pixels(g, *rect)
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e))
            return

        if g.same_data(orig):
            self.status_var.set(f"Glyph #{g.index}: no changes")
            return
        self._commit_glyph_edit(g)

    def _update_old_char_combo(self):
        """Update the old char combo with current glyph's characters."""
        if self.selected_index is None or self.font is None:
            return
        glyph_codes = self.font.codes_for_glyph(self.selected_index)
        if glyph_codes:
            self.old_char_combo['values'] = [
                f"{self._char_display(code)} (U+{code:04X})" for code in glyph_codes
            ]
            self.old_char_combo.current(0)
        else:
            self.old_char_combo['values'] = []
            self.old_char_var.set("")

    def on_char_action_changed(self, event):
        """Enable/disable old/new char fields based on selected action."""
        action = self.char_action_var.get()
        if action in ("replace", "remove"):
            self.old_char_combo.config(state="readonly")
            self._update_old_char_combo()
        else:
            self.old_char_combo.config(state="disabled")
            self.old_char_var.set("")  # Clear when disabled
            self.old_char_combo['values'] = []

        if action in ("remove", "clear_glyph"):
            # Neither 'remove' (picks the code via Old Char) nor
            # 'clear_glyph' (wipes the whole glyph, no code needed) uses
            # the New Char field.
            self.char_mgmt_var.set("")
            self.char_mgmt_entry.config(state="disabled")
        else:
            self.char_mgmt_entry.config(state="normal")

    def _resolve_old_code(self, glyph_codes):
        """Resolve the character code selected in the 'Old Char' combo box.

        Used by both 'remove' and 'replace' actions. Falls back to the
        currently selected character (or the first available one) if the
        combo box is empty.
        """
        old_char_value = self.old_char_var.get().strip()
        if old_char_value:
            # Extract U+XXXX from the combo display format "char (U+XXXX)"
            if "U+" in old_char_value:
                match = re.search(r'U\+([0-9A-Fa-f]+)', old_char_value)
                if match:
                    old_code = int(match.group(1), 16)
                else:
                    raise ValueError(f"Could not parse character code from: {old_char_value}")
            else:
                # Fallback to direct parsing
                old_code = self._parse_character(old_char_value)
        else:
            # If old_char_var is empty, use the currently selected character or first one
            old_code = self.selected_code if self.selected_code in glyph_codes else glyph_codes[0]

        if old_code not in glyph_codes:
            raise ValueError(f"Character U+{old_code:04X} is not assigned to this glyph.")
        return old_code

    def apply_char_action(self):
        if self.font is None or self.selected_index is None:
            return

        action = self.char_action_var.get()
        glyph_index = self.selected_index
        glyph_codes = self.font.codes_for_glyph(glyph_index)
        status = None
        try:
            if action == "add":
                code = self._parse_character(self.char_mgmt_var.get())
                self.font.add_glyph_character_alias(glyph_index, code)
                messagebox.showinfo(
                    "Character Added",
                    f"Character '{self._char_display(code)}' (U+{code:04X}) "
                    f"successfully added to glyph #{glyph_index}"
                )
                status = f"U+{code:04X} added to glyph #{glyph_index}"
            elif action == "remove":
                if not glyph_codes:
                    raise ValueError("Glyph has no characters to remove.")
                code = self._resolve_old_code(glyph_codes)
                self.font.remove_glyph_character_alias(glyph_index, code)
                messagebox.showinfo(
                    "Character Removed",
                    f"Character '{self._char_display(code)}' (U+{code:04X}) "
                    f"successfully removed from glyph #{glyph_index}"
                )
                status = f"U+{code:04X} removed from glyph #{glyph_index}"
            elif action == "replace":
                code = self._parse_character(self.char_mgmt_var.get())
                if not glyph_codes:
                    raise ValueError("Glyph has no characters to replace. Use 'add' to add a character first.")
                old_code = self._resolve_old_code(glyph_codes)
                self.font.remap_glyph_character(glyph_index, old_code, code)
                messagebox.showinfo(
                    "Character Replaced",
                    f"Character '{self._char_display(old_code)}' (U+{old_code:04X}) replaced with "
                    f"'{self._char_display(code)}' (U+{code:04X}) in glyph #{glyph_index}"
                )
                status = f"U+{old_code:04X} replaced with U+{code:04X} in glyph #{glyph_index}"
            elif action == "clear_glyph":
                chars_display = (", ".join(self._char_display(c) for c in glyph_codes)
                                 if glyph_codes else "(no character mapped)")
                confirmed = messagebox.askyesno(
                    "Clear Glyph",
                    f"Clear glyph #{glyph_index} ({chars_display})?\n\n"
                    "This clears every charmap entry pointing to it (same as "
                    "an empty FFFF FFFF slot) and zeroes out all of the "
                    "glyph's own data (metrics and rectangle). This cannot "
                    "be undone once saved.",
                    icon="warning"
                )
                if not confirmed:
                    return
                self.font.clear_glyph(glyph_index)
                messagebox.showinfo("Glyph Cleared", f"Glyph #{glyph_index} successfully cleared.")
                status = f"Glyph #{glyph_index} cleared"
        except ValueError as e:
            messagebox.showerror("Invalid Character", str(e))
            return
        if status is None:
            return

        self._mark_modified(status)
        self.char_mgmt_var.set("")
        self._refresh_glyph_list()

        if action in ("remove", "clear_glyph"):
            remaining = self.font.codes_for_glyph(glyph_index)
            if self.selected_code not in remaining:
                self.selected_code = remaining[0] if remaining else None
        elif action == "replace":
            self.selected_code = code

        self.select_glyph(glyph_index, preferred_code=self.selected_code)

    # ------------------------------------------------------------- canvas

    def _redraw_canvas(self):
        self.canvas.delete("all")
        if self.tex_image is None:
            return

        z = self.zoom
        w, h = self.tex_image.width, self.tex_image.height
        if self._tex_photo_zoom != z:      # rescale only when zoom or texture changed
            disp = self.tex_image.resize((w * z, h * z), Image.NEAREST)
            self.tex_photo = ImageTk.PhotoImage(disp)
            self._tex_photo_zoom = z
        self.canvas.create_image(0, 0, anchor="nw", image=self.tex_photo)
        self.canvas.config(scrollregion=(0, 0, w * z, h * z))

        if self.font is None:
            return

        for g in self.font.glyphs:
            pixels = self.font.to_pixels(g)
            if pixels is None:
                continue
            x0, y0, x1, y1 = pixels
            color = "#00e0ff" if g.index == self.selected_index else "#ff3030"
            width = 2 if g.index == self.selected_index else 1
            self.canvas.create_rectangle(
                x0 * z, y0 * z, x1 * z, y1 * z,
                outline=color, width=width, tags=(f"glyph_{g.index}",)
            )

        if self.selected_index is not None and self.selected_index < len(self.font.glyphs):
            g = self.font.glyphs[self.selected_index]
            pixels = self.font.to_pixels(g)
            if pixels:
                self._draw_handles(*pixels, z)

    def _redraw_selected_glyph(self, x0, y0, x1, y1):
        """Optimized redraw for only the selected glyph during drag operations."""
        z = self.zoom
        self.canvas.delete(f"glyph_{self.selected_index}")
        self.canvas.delete("handle")

        # Draw selected glyph rectangle
        self.canvas.create_rectangle(
            x0 * z, y0 * z, x1 * z, y1 * z,
            outline="#00e0ff", width=2, tags=(f"glyph_{self.selected_index}",)
        )

        # Draw handles
        self._draw_handles(x0, y0, x1, y1, z)

    def _draw_handles(self, x0, y0, x1, y1, z):
        hs = HANDLE_SIZE
        pts = {
            "x0y0": (x0 * z, y0 * z), "x1y0": (x1 * z, y0 * z),
            "x0y1": (x0 * z, y1 * z), "x1y1": (x1 * z, y1 * z),
        }
        for tag, (px, py) in pts.items():
            self.canvas.create_rectangle(
                px - hs, py - hs, px + hs, py + hs,
                fill="#00e0ff", outline="black", tags=("handle", tag)
            )

    def _canvas_to_texpx(self, event):
        x = self.canvas.canvasx(event.x) / self.zoom
        y = self.canvas.canvasy(event.y) / self.zoom
        return x, y

    def on_canvas_click(self, event):
        if self.font is None:
            return
        x, y = self._canvas_to_texpx(event)

        # First check if clicking on a resize handle for the selected glyph
        if self.selected_index is not None:
            g = self.font.glyphs[self.selected_index]
            pixels = self.font.to_pixels(g)
            if pixels:
                x0, y0, x1, y1 = pixels
                tol = HANDLE_SIZE / self.zoom + 1
                if abs(x - x0) < tol and abs(y - y0) < tol:
                    self.drag_mode = "x0y0"
                elif abs(x - x1) < tol and abs(y - y0) < tol:
                    self.drag_mode = "x1y0"
                elif abs(x - x0) < tol and abs(y - y1) < tol:
                    self.drag_mode = "x0y1"
                elif abs(x - x1) < tol and abs(y - y1) < tol:
                    self.drag_mode = "x1y1"
                elif x0 < x < x1 and y0 < y < y1:
                    self.drag_mode = "move"
                else:
                    self.drag_mode = None

                if self.drag_mode:
                    self.drag_start = (x, y)
                    self.drag_orig_rect = (x0, y0, x1, y1)
                    return

        # Otherwise try to select the glyph under cursor (smallest area matching)
        best = None
        best_area = None
        for g in self.font.glyphs:
            pixels = self.font.to_pixels(g)
            if pixels is None:
                continue
            x0, y0, x1, y1 = pixels
            if x0 <= x <= x1 and y0 <= y <= y1:
                area = (x1 - x0) * (y1 - y0)
                if best_area is None or area < best_area:
                    best = g.index
                    best_area = area
        if best is not None:
            self.select_glyph(best)
        self.drag_mode = None

    def _compute_dragged_rect(self, event):
        """Compute the new glyph rectangle for the in-progress drag operation.

        Shared by on_canvas_drag (live preview) and on_canvas_release (final
        commit) so the move/resize/clamp math only lives in one place.
        Returns (x0, y0, x1, y1) as ints, normalized so x0<=x1 and y0<=y1.
        """
        x, y = self._canvas_to_texpx(event)
        dx = x - self.drag_start[0]
        dy = y - self.drag_start[1]
        x0, y0, x1, y1 = self.drag_orig_rect

        # Store original dimensions for move mode
        orig_width = x1 - x0
        orig_height = y1 - y0

        if self.drag_mode == "move":
            x0, x1 = x0 + dx, x1 + dx
            y0, y1 = y0 + dy, y1 + dy
        elif self.drag_mode == "x0y0":
            x0, y0 = x0 + dx, y0 + dy
        elif self.drag_mode == "x1y0":
            x1, y0 = x1 + dx, y0 + dy
        elif self.drag_mode == "x0y1":
            x0, y1 = x0 + dx, y1 + dy
        elif self.drag_mode == "x1y1":
            x1, y1 = x1 + dx, y1 + dy

        # Clamp to the texture. Left/top are always clamped at 0 (a negative value
        # cannot be stored as a raw uint16 coordinate); without a texture there is
        # no right/bottom limit and the range check in set_from_pixels() applies.
        tex_w, tex_h = self.tex_image.size if self.tex_image else (float("inf"), float("inf"))
        x0 = max(0, min(x0, tex_w))
        x1 = max(0, min(x1, tex_w))
        y0 = max(0, min(y0, tex_h))
        y1 = max(0, min(y1, tex_h))

        # In move mode, preserve original dimensions after clamping
        if self.drag_mode == "move":
            # If x0 was clamped, adjust x1 to maintain width
            if x0 != self.drag_orig_rect[0] + dx:
                x1 = x0 + orig_width
            # If x1 was clamped, adjust x0 to maintain width
            elif x1 != self.drag_orig_rect[2] + dx:
                x0 = x1 - orig_width
            # Same for y coordinates
            if y0 != self.drag_orig_rect[1] + dy:
                y1 = y0 + orig_height
            elif y1 != self.drag_orig_rect[3] + dy:
                y0 = y1 - orig_height

        # Truncate coordinates to integers to avoid decimal values
        x0_rounded = int(min(x0, x1))
        y0_rounded = int(min(y0, y1))
        x1_rounded = int(max(x0, x1))
        y1_rounded = int(max(y0, y1))
        return x0_rounded, y0_rounded, x1_rounded, y1_rounded

    def on_canvas_drag(self, event):
        if self.font is None or self.selected_index is None or self.drag_mode is None:
            return
        x0_rounded, y0_rounded, x1_rounded, y1_rounded = self._compute_dragged_rect(event)

        # Update UI fields without saving to data during drag
        self.prop_vars["x0"].set(f"{x0_rounded}")
        self.prop_vars["y0"].set(f"{y0_rounded}")
        self.prop_vars["x1"].set(f"{x1_rounded}")
        self.prop_vars["y1"].set(f"{y1_rounded}")

        # Optimized redraw - only update selected glyph
        self._redraw_selected_glyph(x0_rounded, y0_rounded, x1_rounded, y1_rounded)

    def on_canvas_release(self, event):
        if self.drag_mode:
            rect = self._compute_dragged_rect(event)
            new_glyph = None
            if rect != self.drag_orig_rect:
                candidate = self.font.glyphs[self.selected_index].clone()
                try:
                    self.font.set_pixels(candidate, *rect)
                    new_glyph = candidate
                except ValueError as e:
                    messagebox.showerror("Invalid Rectangle", str(e))
            if new_glyph is not None:
                # Only the rectangle changed: the glyph list rows stay valid.
                self._commit_glyph_edit(new_glyph, refresh_list=False)
            else:
                # Plain click, drag back to the start, or rejected: write nothing and
                # restore the fields/canvas the live drag preview changed.
                self.select_glyph(self.selected_index, preferred_code=self.selected_code)
        self.drag_mode = None
        self.drag_start = None
        self.drag_orig_rect = None


def main():
    root = tk.Tk()
    app = GlyphEditorApp(root)
    if len(sys.argv) >= 2:          # optional: <default.bin> [texture]
        root.after_idle(app.load_paths, *sys.argv[1:3])
    root.mainloop()


if __name__ == "__main__":
    main()
