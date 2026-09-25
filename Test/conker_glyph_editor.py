"""
conker_glyph_editor.py
"""

import os
import sys
import re
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
            self.root.iconbitmap(icon_path)

        self.font = None               # ConkerFont instance
        self.tex_image = None          # PIL.Image of the original texture
        self.tex_photo = None          # ImageTk.PhotoImage for display
        self.zoom = DEFAULT_ZOOM
        self.selected_index = None
        self.selected_code = None   # Current charmap code for the selected glyph
        self.current_file_path = None  # Track current file path
        self.drag_mode = None          # None | "move" | "x0y0" | "x1y0" | "x0y1" | "x1y1"
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

        # Left panel: glyph list / charmap, in tabs
        left = ttk.Frame(main, width=225)
        left.pack(side=tk.LEFT, fill=tk.Y)
        left.pack_propagate(False)

        self.left_notebook = ttk.Notebook(left)
        self.left_notebook.pack(fill=tk.BOTH, expand=True, padx=4, pady=2)

        # --- Tabs ---
        style = ttk.Style()
        style.configure("TNotebook.Tab", padding=(23, 1))

        # --- Tab 1: Glyphs ---
        glyphs_tab = ttk.Frame(self.left_notebook)
        self.left_notebook.add(glyphs_tab, text="Glyphs")

        glyph_list_frame = ttk.Frame(glyphs_tab)
        glyph_list_frame.pack(fill=tk.BOTH, expand=True, pady=2)

        glyph_scrollbar = ttk.Scrollbar(glyph_list_frame)
        glyph_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.glyph_listbox = tk.Listbox(glyph_list_frame, yscrollcommand=glyph_scrollbar.set,
                                         font=("Consolas", 10),
                                         selectmode=tk.SINGLE, exportselection=False)
        self.glyph_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        glyph_scrollbar.config(command=self.glyph_listbox.yview)
        self.glyph_listbox.bind("<<ListboxSelect>>", self.on_listbox_select)

        # --- Tab 2: Charmap ---
        charmap_tab = ttk.Frame(self.left_notebook)
        self.left_notebook.add(charmap_tab, text="Charmap")

        charmap_list_frame = ttk.Frame(charmap_tab)
        charmap_list_frame.pack(fill=tk.BOTH, expand=True, pady=2)

        charmap_scrollbar = ttk.Scrollbar(charmap_list_frame)
        charmap_scrollbar.pack(side=tk.RIGHT, fill=tk.Y)

        self.charmap_listbox = tk.Listbox(charmap_list_frame, yscrollcommand=charmap_scrollbar.set,
                                           font=("Consolas", 10),
                                           selectmode=tk.SINGLE, exportselection=False)
        self.charmap_listbox.pack(side=tk.LEFT, fill=tk.BOTH, expand=True)
        charmap_scrollbar.config(command=self.charmap_listbox.yview)
        self.charmap_listbox.bind("<<ListboxSelect>>", self.on_charmap_listbox_select)
        # Maps a row in self.charmap_listbox to (glyph_index, code_or_None)
        self._charmap_row_data = []
        # Maps glyph_index -> preferred row in charmap_listbox to highlight
        self._charmap_index_by_glyph = {}
        # Maps a row in self.glyph_listbox to (glyph_index, preferred_code_or_None)
        self._glyph_row_data = []

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
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label + ":", width=17, anchor="e").pack(side=tk.LEFT, padx=(0, 2))
            var = tk.StringVar(value="")
            entry = ttk.Entry(row, textvariable=var, width=12)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.prop_vars[key] = var
            if key == "index":
                entry.config(state="readonly")

        ttk.Button(props, text="Apply Changes", command=self.apply_property_edits).pack(
            fill=tk.X, pady=(8, 2)
        )

        # Character Management - separate frame
        char_mgmt_frame = ttk.LabelFrame(right, text="Character Management")
        char_mgmt_frame.pack(fill=tk.X, padx=6, pady=6)

        # First row for old char (used only for replace)
        replace_row = ttk.Frame(char_mgmt_frame)
        replace_row.pack(fill=tk.X, pady=(6, 2))
        
        ttk.Label(replace_row, text="Old Char:", width=17, anchor="e").pack(
            side=tk.LEFT, padx=(0, 2)
        )
        
        self.old_char_var = tk.StringVar(value="")
        self.old_char_combo = ttk.Combobox(replace_row, textvariable=self.old_char_var, width=11)
        self.old_char_combo.pack(side=tk.LEFT, fill=tk.X, expand=True)
        self.old_char_combo.config(state="disabled")  # Initially disabled
        
        # Second row for new char + action
        char_mgmt_row = ttk.Frame(char_mgmt_frame)
        char_mgmt_row.pack(fill=tk.X, pady=(2, 2))
        
        ttk.Label(char_mgmt_row, text="New Char:", width=17, anchor="e").pack(
            side=tk.LEFT, padx=(0, 2)
        )
        
        self.char_mgmt_var = tk.StringVar(value="")
        self.char_mgmt_entry = ttk.Entry(char_mgmt_row, textvariable=self.char_mgmt_var, width=11)
        self.char_mgmt_entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
        
        self.char_action_var = tk.StringVar(value="add")
        char_action_combo = ttk.Combobox(
            char_mgmt_row, textvariable=self.char_action_var,
            values=["add", "remove", "replace"], state="readonly", width=7
        )
        char_action_combo.pack(side=tk.LEFT, padx=(4, 0))
        char_action_combo.bind("<<ComboboxSelected>>", self.on_char_action_changed)
        
        ttk.Button(char_mgmt_frame, text="Apply", command=self.apply_char_action).pack(
            fill=tk.X, pady=(2, 2)
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
            "New Char / Old Char accept one BMP\n"
            "character or U+XXXX.\n\n"
            "add: maps New Char to the selected\n"
            "glyph (in addition to any existing\n"
            "characters). Duplicate codes are\n"
            "rejected.\n"
            "remove: deletes the character chosen\n"
            "in Old Char from the selected glyph.\n"
            "replace: reassigns the character\n"
            "chosen in Old Char to point to\n"
            "New Char instead.\n\n"
            "VERIFIED IN-GAME (via XEMU):\n"
            "- Glyph Width/Height: physical glyph\n"
            "  size. Changing these visibly stretches/\n"
            "  squashes the glyph on screen along\n"
            "  X/Y. Stored as TWO independent\n"
            "  bytes, not one number.\n"
            "- Advance Width: horizontal step after\n"
            "  this character - where the next one\n"
            "  starts.\n"
            "- X/Y Bearing: offset of the glyph from\n"
            "  the baseline. Negative X = left,\n"
            "  positive X = right. Positive Y = lower,\n"
            "  negative Y = higher. Shown/entered\n"
            "  as SIGNED bytes (-128..127).\n\n"
            "Pixel-conversion formula (both X and Y)\n"
            "found empirically."
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
            self.selected_code = None
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
                self.selected_code = None
                # Clear selection in listbox
                self.glyph_listbox.selection_clear(0, tk.END)
                # Clear property fields
                for var in self.prop_vars.values():
                    var.set("")
            self._redraw_canvas()

    def on_zoom_changed(self):
        new_zoom = max(1, int(self.zoom_var.get()))
        if new_zoom == self.zoom:
            return  # No change, skip redraw
        self.zoom = new_zoom
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
        self._glyph_row_data = []
        if self.font is None:
            self._refresh_charmap_list()
            return
        for g in self.font.glyphs:
            # Get all characters mapped to this glyph
            glyph_codes = [code for code, idx in self.font.charmap.items() if idx == g.index]
            if glyph_codes:
                # Display all characters, sorted for consistency
                glyph_chars = [chr(code) for code in glyph_codes]
                char_disp = "".join(sorted(glyph_chars))
                # Use the first character as preferred code
                preferred_code = sorted(glyph_codes)[0]
            else:
                char_disp = "·"
                preferred_code = None
            special = " [spec]" if g.is_special else ""
            self.glyph_listbox.insert(tk.END, f"{g.index:3d}  char={char_disp}  adv={g.byte14:3d}{special}")
            self._glyph_row_data.append((g.index, preferred_code))
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
        if self.font is None:
            return

        rows = []

        # Assigned charmap entries, sorted by code point for readability.
        for code, glyph_index in sorted(self.font.charmap.items()):
            char_disp = chr(code) if code >= 0x20 else "·"
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
        text = value.strip()
        if text.upper().startswith("U+"):
            try:
                code = int(text[2:], 16)
            except ValueError as e:
                raise ValueError("Character must be one symbol or U+XXXX") from e
        elif text.lower().startswith("0x"):
            try:
                code = int(text[2:], 16)
            except ValueError as e:
                raise ValueError("Character must be one symbol or U+XXXX") from e
        else:
            # Do not strip this path: a literal space is a valid character.
            if len(value) != 1:
                raise ValueError("Character must be one symbol or U+XXXX")
            code = ord(value)

        if not (0 <= code < 0xFFFF):
            raise ValueError("Character must be a BMP code point from U+0000 to U+FFFE")
        return code

    def select_glyph(self, index, preferred_code=None):
        if self.font is None or index is None or index >= len(self.font.glyphs):
            return
        self.selected_index = index
        g = self.font.glyphs[index]

        glyph_codes = [code for code, mapped_index in self.font.charmap.items()
                       if mapped_index == index]
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

        # Synchronize listbox selection immediately for glyph listbox (as in old version)
        self.glyph_listbox.selection_clear(0, tk.END)
        self.glyph_listbox.selection_set(index)
        self.glyph_listbox.see(index)

        # Update old char combo when glyph selection changes
        self._update_old_char_combo()

        # Defer charmap listbox sync to avoid reentrancy issues
        self.root.after_idle(self._sync_charmap_selection, index)

    def _sync_charmap_selection(self, index):
        self.charmap_listbox.selection_clear(0, tk.END)
        target_row = self._charmap_index_by_glyph.get(index)
        if target_row is not None:
            self.charmap_listbox.selection_set(target_row)
            self.charmap_listbox.see(target_row)

    def apply_property_edits(self):
        if self.font is None or self.selected_index is None:
            return
        g = self.font.glyphs[self.selected_index].clone()
        try:
            # field1_lo and field1_hi are treated as two INDEPENDENT single bytes,
            # entered/displayed as SIGNED int8 (-128..127) - see select_glyph for why.
            f1_lo_signed = int(self.prop_vars["field1_lo"].get())
            f1_hi_signed = int(self.prop_vars["field1_hi"].get())
            f1_lo = self._signed_to_byte(f1_lo_signed)
            f1_hi = self._signed_to_byte(f1_hi_signed)
            g.field1 = (f1_hi << 8) | f1_lo

            # field2_lo and field2_hi are treated as two INDEPENDENT single bytes
            # (not as one combined 16-bit number - see select_glyph for why), so each
            # is validated and packed separately.
            # User enters values with -1 offset, so we add 1 back when storing.
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
                x0 = float(self.prop_vars["x0"].get())
                y0 = float(self.prop_vars["y0"].get())
                x1 = float(self.prop_vars["x1"].get())
                y1 = float(self.prop_vars["y1"].get())
                self.font.set_pixels(g, x0, y0, x1, y1)
        except ValueError as e:
            messagebox.showerror("Invalid Input", str(e))
            return

        self.font.write_glyph(g)
        self.unsaved_changes = True
        self._refresh_glyph_list()
        self.select_glyph(self.selected_index, preferred_code=self.selected_code)
        self.status_var.set(f"Glyph #{g.index} updated (changes not saved to disk)")

    def _update_old_char_combo(self):
        """Update the old char combo with current glyph's characters."""
        if self.selected_index is not None and self.font is not None:
            glyph_codes = [c for c, idx in self.font.charmap.items() if idx == self.selected_index]
            if glyph_codes:
                # Create display values: character + U+XXXX for clarity
                char_values = []
                for code in sorted(glyph_codes):
                    char_display = chr(code) if code >= 0x20 else f"U+{code:04X}"
                    char_values.append(f"{char_display} (U+{code:04X})")
                self.old_char_combo['values'] = char_values
                if char_values:
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

        if action == "remove":
            # 'remove' only needs the character picked in "Old Char"
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
        try:
            if action == "add":
                code = self._parse_character(self.char_mgmt_var.get())
                self.font.add_glyph_character_alias(self.selected_index, code)
                char_display = chr(code) if code >= 0x20 else f"U+{code:04X}"
                messagebox.showinfo(
                    "Character Added",
                    f"Character '{char_display}' (U+{code:04X}) successfully added to glyph #{self.selected_index}"
                )
                self.status_var.set(
                    f"U+{code:04X} added to glyph #{self.selected_index} "
                    "(changes not saved to disk)"
                )
            elif action == "remove":
                # Determine which character to remove from the 'Old Char' selector
                glyph_codes = [c for c, idx in self.font.charmap.items() if idx == self.selected_index]
                if not glyph_codes:
                    raise ValueError("Glyph has no characters to remove.")

                code = self._resolve_old_code(glyph_codes)

                self.font.remove_glyph_character_alias(self.selected_index, code)
                char_display = chr(code) if code >= 0x20 else f"U+{code:04X}"
                messagebox.showinfo(
                    "Character Removed",
                    f"Character '{char_display}' (U+{code:04X}) successfully removed from glyph #{self.selected_index}"
                )
                self.status_var.set(
                    f"U+{code:04X} removed from glyph #{self.selected_index} "
                    "(changes not saved to disk)"
                )
            elif action == "replace":
                code = self._parse_character(self.char_mgmt_var.get())

                # Replace a specific character with the new one
                glyph_codes = [c for c, idx in self.font.charmap.items() if idx == self.selected_index]
                if not glyph_codes:
                    raise ValueError("Glyph has no characters to replace. Use 'add' to add a character first.")

                old_code = self._resolve_old_code(glyph_codes)

                self.font.remap_glyph_character(self.selected_index, old_code, code)
                
                old_display = chr(old_code) if old_code >= 0x20 else f"U+{old_code:04X}"
                new_display = chr(code) if code >= 0x20 else f"U+{code:04X}"
                messagebox.showinfo(
                    "Character Replaced",
                    f"Character '{old_display}' (U+{old_code:04X}) replaced with '{new_display}' (U+{code:04X}) in glyph #{self.selected_index}"
                )
                self.status_var.set(
                    f"U+{old_code:04X} replaced with U+{code:04X} in glyph #{self.selected_index} "
                    "(changes not saved to disk)"
                )
                
        except ValueError as e:
            messagebox.showerror("Invalid Character", str(e))
            return

        self.unsaved_changes = True
        self.char_mgmt_var.set("")
        self._refresh_glyph_list()
        
        # Update selected_code based on action
        if action == "remove":
            glyph_codes = [c for c, idx in self.font.charmap.items() if idx == self.selected_index]
            if self.selected_code not in glyph_codes:
                self.selected_code = glyph_codes[0] if glyph_codes else None
        elif action == "replace":
            self.selected_code = code
        
        self.select_glyph(self.selected_index, preferred_code=self.selected_code)

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

        # Clamp coordinates to texture bounds to prevent going off-screen
        if self.tex_image:
            tex_w, tex_h = self.tex_image.width, self.tex_image.height
            # Ensure coordinates stay within texture bounds
            x0 = max(0, min(x0, tex_w - 1))
            x1 = max(0, min(x1, tex_w - 1))
            y0 = max(0, min(y0, tex_h - 1))
            y1 = max(0, min(y1, tex_h - 1))

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
            x0_rounded, y0_rounded, x1_rounded, y1_rounded = self._compute_dragged_rect(event)

            g = self.font.glyphs[self.selected_index].clone()
            self.font.set_pixels(g, x0_rounded, y0_rounded, x1_rounded, y1_rounded)
            self.font.write_glyph(g)
            self.unsaved_changes = True

            self._refresh_glyph_list()
            self.select_glyph(self.selected_index)
            self.status_var.set(f"Glyph #{self.selected_index} updated (not saved to disk)")
            
            # Redraw all glyphs after release to show full context
            self._redraw_canvas()
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
