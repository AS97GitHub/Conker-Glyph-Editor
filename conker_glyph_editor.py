"""
conker_glyph_editor.py
"""

import os
import sys
import struct
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from PIL import Image, ImageTk

from conker_glyph_format import ConkerFont, FONT_PROFILES


HANDLE_SIZE = 6          # Size of corner handle box for dragging (in screen pixels)
DEFAULT_ZOOM = 4


class GlyphEditorApp:
    def __init__(self, root):
        self.root = root
        self.root.title("Conker Glyph Editor")
        self.root.geometry("1200x760")

        self.font = None          # ConkerFont instance
        self.tex_image = None     # PIL.Image of the original texture
        self.tex_photo = None     # ImageTk.PhotoImage for display
        self.zoom = DEFAULT_ZOOM
        self.selected_index = None
        self.current_file_path = None  # Track current file path
        self.drag_mode = None     # None | "move" | "x0y0" | "x1y0" | "x0y1" | "x1y1"
        self.drag_start = None
        self.drag_orig_rect = None
        self.unsaved_changes = False

        self._build_ui()

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        toolbar = ttk.Frame(self.root)
        toolbar.pack(side=tk.TOP, fill=tk.X, padx=4, pady=4)

        ttk.Button(toolbar, text="Open .bin...", command=self.open_bin).pack(side=tk.LEFT, padx=2)
        ttk.Button(toolbar, text="Open texture...", command=self.open_texture).pack(side=tk.LEFT, padx=2)

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
        zoom_spin = ttk.Spinbox(toolbar, from_=1, to=16, textvariable=self.zoom_var,
                                 width=4, command=self.on_zoom_changed)
        zoom_spin.pack(side=tk.LEFT)

        ttk.Button(toolbar, text="Save As...", command=self.save_as).pack(side=tk.RIGHT, padx=2)
        ttk.Button(toolbar, text="Save (overwrite)", command=self.save_overwrite).pack(side=tk.RIGHT, padx=2)

        self.status_var = tk.StringVar(value="Open default.bin and texture to start.")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        main = ttk.Frame(self.root)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Left panel: glyph list
        left = ttk.Frame(main, width=225)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)

        ttk.Label(left, text="Glyphs:").pack(anchor="w", padx=4)
        list_frame = ttk.Frame(left)
        list_frame.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        scrollbar = ttk.Scrollbar(list_frame)
        scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.glyph_listbox = tk.Listbox(list_frame, yscrollcommand=scrollbar.set, font=("Consolas", 10))
        self.glyph_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        scrollbar.config(command=self.glyph_listbox.yview)
        self.glyph_listbox.bind("<<ListboxSelect>>", self.on_listbox_select)

        # Center panel: texture canvas
        center = ttk.Frame(main)
        center.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)

        canvas_frame = ttk.Frame(center)
        canvas_frame.pack(fill=tk.BOTH, expand=True)

        hbar = ttk.Scrollbar(canvas_frame, orient=tk.HORIZONTAL)
        vbar = ttk.Scrollbar(canvas_frame, orient=tk.VERTICAL)
        self.canvas = tk.Canvas(canvas_frame, bg="#222222",
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
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        props = ttk.LabelFrame(right, text="Glyph Properties")
        props.pack(fill=tk.X, padx=6, pady=6)

        self.prop_vars = {}
        prop_fields = [
            ("index", "Index"),
            ("char", "Char"),
            ("unknown_hi", "Unknown hi (?)"),
            ("unknown_lo", "Unknown lo (?)"),
            ("x0", "Start X"),
            ("y0", "Start Y"),
            ("x1", "End X"),
            ("y1", "End Y"),
            ("field1_hi", "Y Bearing (-↑/+↓)"),
            ("field1_lo", "X Bearing (-←/+→)"),
            ("field2_hi", "Glyph Height (↕)"),
            ("field2_lo", "Glyph Width (↔)"),
            ("byte14", "Advance Width"),
        ]
        for key, label in prop_fields:
            row = ttk.Frame(props)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label + ":", width=17, anchor="e").pack(side=tk.LEFT, padx=(0, 2))
            var = tk.StringVar(value="")
            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.prop_vars[key] = var
            if key in ("index", "char"):
                entry.config(state="readonly")

        ttk.Button(props, text="Apply Changes", command=self.apply_property_edits).pack(
            fill=tk.X, pady=(8, 2)
        )

        help_frame = ttk.LabelFrame(right, text="Help / Info")
        help_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        
        # Create scrollable text widget for help
        help_scrollbar = ttk.Scrollbar(help_frame)
        help_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)
        
        help_text = tk.Text(help_frame, wrap=tk.WORD, width=30, height=10,
                            yscrollcommand=help_scrollbar.set,
                            font=("Tahoma", 9), state=tk.DISABLED,
                            relief=tk.FLAT, highlightthickness=0,
                            background="#f0f0f0")
        help_text.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        help_scrollbar.config(command=help_text.yview)
        
        help_content = (
            "LMB on glyph on texture - select.\n\n"
            "Drag corner handle - resize rectangle.\n"
            "Drag center - move rectangle.\n\n"
            "You can also manually enter Start/End\n"
            "X/Y (in texture pixels) and click\n"
            "'Apply Changes'.\n\n"
            "VERIFIED IN-GAME (via XEMU):\n"
            "- Glyph Height/Width (f2_hi/f2_lo):\n"
            "  physical glyph size. Changing these\n"
            "  visibly stretches/squashes the glyph\n"
            "  on screen along Y/X. Stored as TWO\n"
            "  independent bytes, not one number.\n"
            "- Advance Width (byte14): horizontal\n"
            "  step after this character - where\n"
            "  the next one starts.\n"
            "- Y/X Bearing (f1_hi/f1_lo): offset of\n"
            "  the glyph from the baseline. Positive\n"
            "  Y = lower, negative Y = higher.\n"
            "  Negative X = left, positive X =\n"
            "  right. Shown/entered as SIGNED bytes\n"
            "  (-128..127).\n"
            "- 'Unknown' (hi/lo): NO visible\n"
            "  effect even at extreme test values\n"
            "  (0 and 255), not just small changes.\n"
            "  High byte always 0x00 - the 'two\n"
            "  independent bytes' idea was tested\n"
            "  and doesn't hold. Likely not read by\n"
            "  the text renderer at all.\n\n"
            "Y-axis pixel-conversion formula found\n"
            "empirically, not 100% verified by\n"
            "disassembly."
        )
        help_text.config(state=tk.NORMAL)
        help_text.insert(tk.END, help_content)
        help_text.config(state=tk.DISABLED)

    # ------------------------------------------------------------- actions

    def open_bin(self):
        path = filedialog.askopenfilename(
            title="Open default.bin",
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.font = ConkerFont(path, profile_name=None)  # Auto-detect profile
            # Update profile dropdown to match detected profile
            self.profile_var.set(self.font.profile_name)
        except Exception as e:
            messagebox.showerror("Loading Error", str(e))
            return
        
        # Check if opening the same file
        same_file = (self.current_file_path == path)
        self.current_file_path = path
        
        self.unsaved_changes = False
        
        if same_file and self.selected_index is not None and self.selected_index < len(self.font.glyphs):
            # Keep the selection if it's still valid
            saved_index = self.selected_index
        else:
            # Reset selection for new file or invalid index
            self.selected_index = None
            saved_index = None
            # Clear property fields
            for var in self.prop_vars.values():
                var.set("")
        
        self._refresh_glyph_list()
        
        # Restore UI state if keeping selection
        if saved_index is not None:
            self.select_glyph(saved_index)
        
        self._redraw_canvas()
        self.status_var.set(
            f"Loaded {os.path.basename(path)}: {self.font.glyph_count} glyphs, "
            f"profile {self.profile_var.get()} (auto-detected)"
        )

    def open_texture(self):
        path = filedialog.askopenfilename(
            title="Open Texture",
            filetypes=[("Images", "*.png *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            img = Image.open(path)
            # Validate image format
            if img.format not in ("PNG", "BMP"):
                messagebox.showwarning(
                    "Texture Format Warning",
                    f"Image format '{img.format}' may not be supported. PNG or BMP recommended."
                )
            # Validate image dimensions
            if img.width < 1 or img.height < 1:
                raise ValueError("Image must have positive dimensions")
            if img.width > 1024 or img.height > 1024:
                messagebox.showwarning(
                    "Texture Size Warning",
                    f"Image dimensions ({img.width}x{img.height}) are very large. This may cause performance issues."
                )
            self.tex_image = img.convert("RGB")
        except Exception as e:
            messagebox.showerror("Texture Loading Error", str(e))
            return
        self._redraw_canvas()
        self.status_var.set(
            f"Texture loaded: {os.path.basename(path)} ({self.tex_image.width}x{self.tex_image.height})"
        )

    def on_profile_changed(self):
        if self.font is not None:
            self.font.profile_name = self.profile_var.get()
            self.font.profile = FONT_PROFILES[self.profile_var.get()]
            # Check if selected_index is still valid after profile change
            if self.selected_index is not None and self.selected_index >= len(self.font.glyphs):
                self.selected_index = None
                # Clear selection in listbox
                self.glyph_listbox.selection_clear(0, tk.END)
                # Clear property fields
                for var in self.prop_vars.values():
                    var.set("")
            elif self.selected_index is not None:
                # Even if index is valid, clear listbox selection to avoid inconsistencies
                self.glyph_listbox.selection_clear(0, tk.END)
            self._redraw_canvas()

    def on_zoom_changed(self):
        self.zoom = max(1, int(self.zoom_var.get()))
        self._redraw_canvas()

    def save_as(self):
        if self.font is None:
            messagebox.showinfo("No Data", "Please open default.bin first")
            return
        path = filedialog.asksaveasfilename(
            title="Save As",
            defaultextension=".bin",
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        self.font.save(path)
        self.unsaved_changes = False
        self.status_var.set(f"Saved: {path}")

    def save_overwrite(self):
        if self.font is None:
            messagebox.showinfo("No Data", "Please open default.bin first")
            return
        if not messagebox.askyesno(
            "Confirmation",
            f"Overwrite original file?\n{self.font.path}\n\n"
            "Making a backup copy first is recommended."
        ):
            return
        self.font.save()
        self.unsaved_changes = False
        self.status_var.set(f"File overwritten: {self.font.path}")

    # --------------------------------------------------------------- list

    def _refresh_glyph_list(self):
        self.glyph_listbox.delete(0, tk.END)
        if self.font is None:
            return
        for g in self.font.glyphs:
            char_disp = g.char if g.char.strip() else "·"
            special = " [spec]" if g.is_special else ""
            self.glyph_listbox.insert(
                tk.END, f"{g.index:3d}  char={char_disp}  adv={g.byte14:3d}{special}"
            )

    def on_listbox_select(self, event):
        sel = self.glyph_listbox.curselection()
        if not sel:
            return
        self.select_glyph(sel[0])

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

    def select_glyph(self, index):
        if self.font is None or index is None or index >= len(self.font.glyphs):
            return
        self.selected_index = index
        g = self.font.glyphs[index]

        self.prop_vars["index"].set(str(g.index))
        self.prop_vars["char"].set(g.char)
        # This field (internal attribute name: unknown_field, kept for backward
        # compatibility) is stored as one uint16 in the file. CONFIRMED IN-GAME:
        # no visible effect even at extreme test values (0 and 255). High byte is
        # always 0x00 across the whole glyph table - shown here split into hi/lo
        # bytes for consistency with field1/field2, so any future finding about
        # either byte specifically is easy to test in isolation.
        unk_hi, unk_lo = g.unknown_field >> 8, g.unknown_field & 0xFF
        self.prop_vars["unknown_hi"].set(str(unk_hi))
        self.prop_vars["unknown_lo"].set(str(unk_lo))
        # field1 is stored as one uint16 in the file, but behaves as two independent
        # bytes. CONFIRMED IN-GAME: hi byte = Y Bearing (vertical offset from the
        # baseline - positive moves the glyph down, negative moves it up), lo byte =
        # X Bearing (horizontal offset from the baseline - negative shifts left,
        # positive shifts right). Displayed/edited here as SIGNED int8 (-128..127),
        # since the raw unsigned readings (e.g. 254/255) only made sense once
        # reinterpreted as small negative numbers (-2/-1).
        f1_hi, f1_lo = g.field1 >> 8, g.field1 & 0xFF
        self.prop_vars["field1_hi"].set(str(self._byte_to_signed(f1_hi)))
        self.prop_vars["field1_lo"].set(str(self._byte_to_signed(f1_lo)))
        # field2 is stored as one uint16 in the file, but behaves as two INDEPENDENT
        # single-byte values. CONFIRMED IN-GAME: hi byte = Glyph Height, lo byte =
        # Glyph Width - changing either one visibly stretches/squashes the glyph on
        # screen along that axis (not just the atlas rectangle size).
        # Displayed with -1 offset for user convenience.
        f2_hi, f2_lo = g.field2 >> 8, g.field2 & 0xFF
        self.prop_vars["field2_hi"].set(str(f2_hi - 1))
        self.prop_vars["field2_lo"].set(str(f2_lo - 1))
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

        # Synchronize listbox selection
        self.glyph_listbox.selection_clear(0, tk.END)
        self.glyph_listbox.selection_set(index)
        self.glyph_listbox.see(index)

    def apply_property_edits(self):
        if self.font is None or self.selected_index is None:
            return
        g = self.font.glyphs[self.selected_index].clone()
        try:
            unk_hi = int(self.prop_vars["unknown_hi"].get())
            unk_lo = int(self.prop_vars["unknown_lo"].get())
            if not (0 <= unk_hi <= 255):
                raise ValueError("Unknown hi must be an integer between 0 and 255 (it's a single byte)")
            if not (0 <= unk_lo <= 255):
                raise ValueError("Unknown lo must be an integer between 0 and 255 (it's a single byte)")
            g.unknown_field = (unk_hi << 8) | unk_lo

            # field1_hi and field1_lo are treated as two INDEPENDENT single bytes,
            # entered/displayed as SIGNED int8 (-128..127) - see select_glyph for why.
            f1_hi_signed = int(self.prop_vars["field1_hi"].get())
            f1_lo_signed = int(self.prop_vars["field1_lo"].get())
            f1_hi = self._signed_to_byte(f1_hi_signed)
            f1_lo = self._signed_to_byte(f1_lo_signed)
            g.field1 = (f1_hi << 8) | f1_lo

            # field2_hi and field2_lo are treated as two INDEPENDENT single bytes
            # (not as one combined 16-bit number - see select_glyph for why), so each
            # is validated and packed separately.
            # User enters values with -1 offset, so we add 1 back when storing.
            f2_hi = int(self.prop_vars["field2_hi"].get()) + 1
            f2_lo = int(self.prop_vars["field2_lo"].get()) + 1
            if not (0 <= f2_hi <= 255):
                raise ValueError("Glyph Height must be an integer between -1 and 254 (displayed with -1 offset)")
            if not (0 <= f2_lo <= 255):
                raise ValueError("Glyph Width must be an integer between -1 and 254 (displayed with -1 offset)")
            g.field2 = (f2_hi << 8) | f2_lo

            byte14_val = int(self.prop_vars["byte14"].get())
            if not (0 <= byte14_val <= 255):
                raise ValueError("Advance Width must be an integer between 0 and 255 (it's a single byte in the file)")
            g.byte14 = byte14_val
            if not g.is_special:
                x0 = float(self.prop_vars["x0"].get())
                y0 = float(self.prop_vars["y0"].get())
                x1 = float(self.prop_vars["x1"].get())
                y1 = float(self.prop_vars["y1"].get())
                self.font.set_pixels(g, x0, y0, x1, y1)
        except ValueError as e:
            messagebox.showerror("Invalid Input", f"Please check numeric fields.\n{e}")
            return

        self.font.write_glyph(g)
        self.unsaved_changes = True
        self._refresh_glyph_list()
        self.select_glyph(self.selected_index)
        self.status_var.set(f"Glyph #{g.index} updated (changes not saved to disk)")

    # ------------------------------------------------------------- canvas

    def _redraw_canvas(self):
        self.canvas.delete("all")
        if self.tex_image is None:
            return

        z = self.zoom
        w, h = self.tex_image.width, self.tex_image.height
        disp = self.tex_image.resize((w * z, h * z), Image.NEAREST)
        self.tex_photo = ImageTk.PhotoImage(disp)
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

    def on_canvas_drag(self, event):
        if self.font is None or self.selected_index is None or self.drag_mode is None:
            return
        x, y = self._canvas_to_texpx(event)
        dx = x - self.drag_start[0]
        dy = y - self.drag_start[1]
        x0, y0, x1, y1 = self.drag_orig_rect

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

        # Round coordinates to integers to avoid decimal values
        x0_rounded = round(min(x0, x1))
        y0_rounded = round(min(y0, y1))
        x1_rounded = round(max(x0, x1))
        y1_rounded = round(max(y0, y1))

        g = self.font.glyphs[self.selected_index].clone()
        self.font.set_pixels(g, x0_rounded, y0_rounded, x1_rounded, y1_rounded)
        self.font.write_glyph(g)
        self.unsaved_changes = True

        self.prop_vars["x0"].set(f"{x0_rounded}")
        self.prop_vars["y0"].set(f"{y0_rounded}")
        self.prop_vars["x1"].set(f"{x1_rounded}")
        self.prop_vars["y1"].set(f"{y1_rounded}")

        self._redraw_canvas()

    def on_canvas_release(self, event):
        if self.drag_mode:
            self._refresh_glyph_list()
            self.select_glyph(self.selected_index)
            self.status_var.set(f"Glyph #{self.selected_index} updated (not saved to disk)")
        self.drag_mode = None
        self.drag_start = None
        self.drag_orig_rect = None


def main():
    root = tk.Tk()
    try:
        style = ttk.Style()
        if "vista" in style.theme_names():
            style.theme_use("vista")
    except Exception:
        pass
    app = GlyphEditorApp(root)

    # Open immediately if paths are passed via CLI arguments
    if len(sys.argv) >= 2:
        bin_path = sys.argv[1]
        if os.path.exists(bin_path):
            app.font = ConkerFont(bin_path, profile_name=app.profile_var.get())
            app._refresh_glyph_list()
    if len(sys.argv) >= 3:
        tex_path = sys.argv[2]
        if os.path.exists(tex_path):
            try:
                img = Image.open(tex_path)
                if img.format not in ("PNG", "BMP"):
                    print(f"Warning: Image format '{img.format}' may not be supported. PNG or BMP recommended.")
                if img.width < 1 or img.height < 1:
                    raise ValueError("Image must have positive dimensions")
                app.tex_image = img.convert("RGB")
            except Exception as e:
                print(f"Error loading texture: {e}")
                app.tex_image = None
    if len(sys.argv) >= 2:
        app._redraw_canvas()

    root.mainloop()


if __name__ == "__main__":
    main()
