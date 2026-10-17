"""
conker_clipboard.py - copy / paste a PIL image to / from the system clipboard.

tkinter cannot put images on the clipboard, so this uses what each platform
offers: ctypes (CF_DIB) on Windows, osascript on macOS, and xclip (X11) or
wl-copy (Wayland) on Linux.  Pasting goes through Pillow's ImageGrab, which on
Linux also needs xclip or wl-paste.  Problems are reported as ClipboardError
with a hint, never as a crash.
"""

import io
import os
import shutil
import subprocess
import sys
import tempfile
import time

from PIL import Image, ImageGrab, UnidentifiedImageError


class ClipboardError(OSError):
    """The clipboard could not be read or written (missing tool, busy, ...)."""


def copy_image(image, owner=None):
    """Put ``image`` on the clipboard. ``owner`` is a Tk widget (Windows needs a
    window handle to own the clipboard)."""
    rgb = image.convert("RGB")           # every program understands plain RGB
    if sys.platform.startswith("win"):
        _copy_windows(rgb, owner)
    elif sys.platform == "darwin":
        _copy_macos(rgb)
    else:
        _copy_linux(rgb)


def paste_image():
    """Return the image on the clipboard as a PIL image, or None if there is none."""
    try:
        data = ImageGrab.grabclipboard()
    except NotImplementedError as e:     # Linux without xclip / wl-paste
        raise ClipboardError(str(e)) from e
    except UnidentifiedImageError:       # the clipboard holds something that is not an image (text, ...)
        return None
    except OSError as e:
        raise ClipboardError(f"could not read the clipboard: {e}") from e
    if isinstance(data, list):           # files copied in Explorer / Finder
        for path in data:
            try:
                with Image.open(path) as img:
                    return img.convert("RGB")
            except OSError:
                continue
        return None
    return data


def _png_bytes(image):
    buffer = io.BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue()


def _copy_windows(rgb, owner):
    import ctypes
    from ctypes import wintypes

    buffer = io.BytesIO()
    rgb.save(buffer, format="BMP")
    dib = buffer.getvalue()[14:]         # CF_DIB = a BMP without its 14-byte file header

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    user32.OpenClipboard.argtypes = [wintypes.HWND]
    user32.OpenClipboard.restype = wintypes.BOOL
    user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
    user32.SetClipboardData.restype = wintypes.HANDLE
    kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
    kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]

    hwnd = owner.winfo_id() if owner is not None else None
    for _ in range(10):                  # another program may hold the clipboard briefly
        if user32.OpenClipboard(hwnd):
            break
        time.sleep(0.05)
    else:
        raise ClipboardError("the clipboard is in use by another program")
    try:
        user32.EmptyClipboard()
        handle = kernel32.GlobalAlloc(0x0002, len(dib))          # GMEM_MOVEABLE
        if not handle:
            raise ClipboardError("not enough memory for the clipboard")
        pointer = kernel32.GlobalLock(handle)
        ctypes.memmove(pointer, dib, len(dib))
        kernel32.GlobalUnlock(handle)
        if not user32.SetClipboardData(8, handle):               # CF_DIB
            kernel32.GlobalFree(handle)
            raise ClipboardError("could not put the image on the clipboard")
    finally:
        user32.CloseClipboard()


def _copy_macos(rgb):
    fd, path = tempfile.mkstemp(suffix=".png")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(_png_bytes(rgb))
        script = f'set the clipboard to (read (POSIX file "{path}") as \u00abclass PNGf\u00bb)'
        result = subprocess.run(["osascript", "-e", script], capture_output=True, text=True)
        if result.returncode != 0:
            raise ClipboardError(f"could not copy the image: {result.stderr.strip()}")
    except FileNotFoundError as e:
        raise ClipboardError("osascript was not found") from e
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _copy_linux(rgb):
    if os.environ.get("WAYLAND_DISPLAY") and shutil.which("wl-copy"):
        command = ["wl-copy", "--type", "image/png"]
    elif shutil.which("xclip"):
        command = ["xclip", "-selection", "clipboard", "-t", "image/png", "-i"]
    else:
        raise ClipboardError(
            "copying an image needs xclip (X11) or wl-clipboard (Wayland) to be installed"
        )
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, start_new_session=True)
    process.stdin.write(_png_bytes(rgb))
    process.stdin.close()
    try:                                 # the tool keeps running to serve the clipboard
        code = process.wait(timeout=0.5)
    except subprocess.TimeoutExpired:
        return
    if code != 0:
        raise ClipboardError(f"{command[0]} failed (exit code {code})")
