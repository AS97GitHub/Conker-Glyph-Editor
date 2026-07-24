"""
conker_glyph_editor_(calibration).py
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
        self.drag_mode = None     # None | "move" | "x0" | "y0" | "x1" | "y1"
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
            values=list(FONT_PROFILES.keys()), state="readonly", width=14
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

        # Second toolbar row - live calibration of X_DIV/Y_DIV/offsets.
        # This does NOT change the raw file data, only how it is displayed.
        # You can adjust values freely and see the result without corrupting
        # anything until the "Apply" button is pressed.
        calib = ttk.Frame(self.root)
        calib.pack(side=tk.TOP, fill=tk.X, padx=4, pady=(0, 4))

        ttk.Label(calib, text="Calibration (affects display only until 'Apply' is clicked):").pack(
            side=tk.LEFT, padx=(0, 8)
        )

        self.calib_vars = {}
        for label in ["X_DIV", "Y_DIV", "X_OFFSET", "Y_OFFSET"]:
            ttk.Label(calib, text=label + ":").pack(side=tk.LEFT, padx=(6, 2))
            var = tk.StringVar(value="")
            # Wider width to fit values like 68.2734 without clipping
            entry = ttk.Entry(calib, textvariable=var, width=10)
            entry.pack(side=tk.LEFT)
            entry.bind("<Return>", lambda e: self.preview_calibration())
            self.calib_vars[label] = var

        ttk.Button(calib, text="Preview", command=self.preview_calibration).pack(side=tk.LEFT, padx=(10, 2))
        ttk.Button(calib, text="Apply to all glyphs (overwrite raw)",
                   command=self.apply_calibration_to_all).pack(side=tk.LEFT, padx=2)

        self.status_var = tk.StringVar(value="Open default.bin and texture to start.")
        status_bar = ttk.Label(self.root, textvariable=self.status_var, anchor="w", relief=tk.SUNKEN)
        status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        main = ttk.Frame(self.root)
        main.pack(side=tk.TOP, fill=tk.BOTH, expand=True)

        # Left panel: glyph list
        left = ttk.Frame(main, width=260)
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
        right = ttk.Frame(main, width=300)
        right.pack(side=tk.RIGHT, fill=tk.Y)
        right.pack_propagate(False)

        props = ttk.LabelFrame(right, text="Glyph Properties")
        props.pack(fill=tk.X, padx=6, pady=6)

        self.prop_vars = {}
        for i, label in enumerate(["index", "char", "advance", "x0", "y0", "x1", "y1"]):
            row = ttk.Frame(props)
            row.pack(fill=tk.X, pady=2)
            ttk.Label(row, text=label + ":", width=10).pack(side=tk.LEFT)
            var = tk.StringVar(value="")
            entry = ttk.Entry(row, textvariable=var, width=16)
            entry.pack(side=tk.LEFT, fill=tk.X, expand=True)
            self.prop_vars[label] = var
            if label in ("index", "char"):
                entry.config(state="readonly")

        ttk.Button(props, text="Apply Changes", command=self.apply_property_edits).pack(
            fill=tk.X, pady=(8, 2)
        )

        raw_frame = ttk.LabelFrame(right, text="Raw Entry Data (Reference)")
        raw_frame.pack(fill=tk.X, padx=6, pady=6)
        self.raw_text = tk.Text(raw_frame, height=8, width=32, font=("Consolas", 9), state="disabled")
        self.raw_text.pack(fill=tk.BOTH, expand=True)

        help_frame = ttk.LabelFrame(right, text="Help / Info")
        help_frame.pack(fill=tk.BOTH, expand=True, padx=6, pady=6)
        help_text = (
            "LMB on glyph on texture - select.\n\n"
            "Drag corner handle - resize rectangle.\n"
            "Drag center - move rectangle.\n\n"
            "You can also manually enter x0/y0/x1/y1\n"
            "(in texture pixels) and click\n"
            "'Apply Changes'.\n\n"
            "Y formula found empirically, not\n"
            "100% verified by disassembly."
        )
        ttk.Label(help_frame, text=help_text, justify="left", wraplength=270).pack(
            anchor="nw", padx=4, pady=4
        )

    # ------------------------------------------------------------- actions

    def open_bin(self):
        path = filedialog.askopenfilename(
            title="Open default.bin",
            filetypes=[("BIN files", "*.bin"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.font = ConkerFont(path, profile_name=self.profile_var.get())
        except Exception as e:
            messagebox.showerror("Loading Error", str(e))
            return
        self.unsaved_changes = False
        self._refresh_glyph_list()
        self._sync_calibration_fields_from_profile()
        self._redraw_canvas()
        self.status_var.set(
            f"Loaded {os.path.basename(path)}: {self.font.glyph_count} glyphs, "
            f"profile {self.profile_var.get()}"
        )

    def open_texture(self):
        path = filedialog.askopenfilename(
            title="Open Texture",
            filetypes=[("Images", "*.png *.bmp"), ("All files", "*.*")],
        )
        if not path:
            return
        try:
            self.tex_image = Image.open(path).convert("RGB")
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
            self._sync_calibration_fields_from_profile()
            self._redraw_canvas()

    def _sync_calibration_fields_from_profile(self):
        """Populates calibration input fields with current values from the active profile."""
        if self.font is None:
            return
        p = self.font.profile
        self.calib_vars["X_DIV"].set(str(p["X_DIV"]))
        self.calib_vars["Y_DIV"].set(str(p["Y_DIV"]))
        self.calib_vars["X_OFFSET"].set(str(p["X_OFFSET"]))
        self.calib_vars["Y_OFFSET"].set(str(p["Y_OFFSET"]))

    def _read_calibration_fields(self):
        """Parses calibration fields into floats. Raises ValueError on invalid input."""
        return {
            "X_DIV": float(self.calib_vars["X_DIV"].get()),
            "Y_DIV": float(self.calib_vars["Y_DIV"].get()),
            "X_OFFSET": float(self.calib_vars["X_OFFSET"].get()),
            "Y_OFFSET": float(self.calib_vars["Y_OFFSET"].get()),
        }

    def preview_calibration(self):
        """Applies entered X_DIV/Y_DIV/offsets to display only (self.font.profile),
        WITHOUT touching the raw bytes of glyphs. Can be adjusted freely without risk of
        corrupting data - saving to disk uses these live parameters, but raw data is not
        recalculated until 'Apply to all glyphs' is clicked."""
        if self.font is None:
            messagebox.showinfo("No Data", "Please open default.bin first")
            return
        try:
            values = self._read_calibration_fields()
        except ValueError:
            messagebox.showerror("Invalid Input", "X_DIV/Y_DIV/X_OFFSET/Y_OFFSET must be numbers.")
            return

        self.font.profile = dict(values)  # Temporary copy, does not mutate FONT_PROFILES
        self._redraw_canvas()
        if self.selected_index is not None:
            self.select_glyph(self.selected_index)
        self.status_var.set(
            f"Preview: X_DIV={values['X_DIV']} Y_DIV={values['Y_DIV']} "
            f"X_OFFSET={values['X_OFFSET']} Y_OFFSET={values['Y_OFFSET']} "
            "(raw data not modified)"
        )

    def apply_calibration_to_all(self):
        """Recalculates and WRITES raw x0/x1/y0/y1 for all non-special glyphs using
        the currently entered calibration parameters. Useful when the correct formula
        has been found via 'Preview' and needs to be permanently baked into data."""
        if self.font is None:
            messagebox.showinfo("No Data", "Please open default.bin first")
            return
        try:
            values = self._read_calibration_fields()
        except ValueError:
            messagebox.showerror("Invalid Input", "X_DIV/Y_DIV/X_OFFSET/Y_OFFSET must be numbers.")
            return

        old_profile = dict(self.font.profile)
        if not messagebox.askyesno(
            "Confirmation",
            "This will recalculate coordinates of ALL glyphs under the new calibration and\n"
            "write them as new raw data (old raw data will be lost unless backed up).\n\n"
            f"Old calibration: {old_profile}\n"
            f"New calibration: {values}\n\n"
            "Continue?"
        ):
            return

        # First obtain pixel coordinates using OLD calibration (the one currently
        # active in self.font.profile - usually the one used in preview), then
        # switch profile to the new one and overwrite raw values via set_pixels
        old_pixels = {}
        for g in self.font.glyphs:
            old_pixels[g.index] = self.font.to_pixels(g)

        self.font.profile = dict(values)

        count = 0
        for g in self.font.glyphs:
            pixels = old_pixels[g.index]
            if pixels is None:
                continue
            x0, y0, x1, y1 = pixels
            g2 = g.clone()
            self.font.set_pixels(g2, x0, y0, x1, y1)
            self.font.write_glyph(g2)
            count += 1

        self.unsaved_changes = True
        self._refresh_glyph_list()
        self._redraw_canvas()
        self.status_var.set(
            f"Calibration applied and saved to raw data for {count} glyphs. "
            "Don't forget to save the file!"
        )

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
            special = " [special]" if g.is_special else ""
            self.glyph_listbox.insert(
                tk.END, f"{g.index:3d}  {char_disp!r:>5s}  adv={g.advance:3d}{special}"
            )

    def on_listbox_select(self, event):
        sel = self.glyph_listbox.curselection()
        if not sel:
            return
        self.select_glyph(sel[0])

    def select_glyph(self, index):
        if self.font is None or index is None or index >= len(self.font.glyphs):
            return
        self.selected_index = index
        g = self.font.glyphs[index]

        self.prop_vars["index"].set(str(g.index))
        self.prop_vars["char"].set(repr(g.char))
        self.prop_vars["advance"].set(str(g.advance))

        pixels = self.font.to_pixels(g)
        if pixels:
            x0, y0, x1, y1 = pixels
            self.prop_vars["x0"].set(f"{x0:.1f}")
            self.prop_vars["y0"].set(f"{y0:.1f}")
            self.prop_vars["x1"].set(f"{x1:.1f}")
            self.prop_vars["y1"].set(f"{y1:.1f}")
        else:
            for k in ("x0", "y0", "x1", "y1"):
                self.prop_vars[k].set("(special glyph)")

        self._update_raw_text(g)
        self._redraw_canvas()

        # Synchronize listbox selection
        self.glyph_listbox.selection_clear(0, tk.END)
        self.glyph_listbox.selection_set(index)
        self.glyph_listbox.see(index)

    def _update_raw_text(self, g):
        self.raw_text.config(state="normal")
        self.raw_text.delete("1.0", tk.END)
        info = (
            f"advance : {g.advance}\n"
            f"field1  : {g.field1} (0x{g.field1:04x})\n"
            f"field2  : {g.field2} (0x{g.field2:04x})\n"
            f"x0_raw  : {g.x0_raw}\n"
            f"x1_raw  : {g.x1_raw}\n"
            f"y0_raw  : {g.y0_raw}\n"
            f"y1_raw  : {g.y1_raw}\n"
            f"byte14  : {g.byte14}\n"
            f"byte15  : {g.byte15}\n"
            f"special : {g.is_special}\n"
        )
        self.raw_text.insert("1.0", info)
        self.raw_text.config(state="disabled")

    def apply_property_edits(self):
        if self.font is None or self.selected_index is None:
            return
        g = self.font.glyphs[self.selected_index].clone()
        try:
            g.advance = int(self.prop_vars["advance"].get())
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

        if self.selected_index is not None:
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

        g = self.font.glyphs[self.selected_index].clone()
        self.font.set_pixels(g, min(x0, x1), min(y0, y1), max(x0, x1), max(y0, y1))
        self.font.write_glyph(g)
        self.unsaved_changes = True

        self.prop_vars["x0"].set(f"{min(x0,x1):.1f}")
        self.prop_vars["y0"].set(f"{min(y0,y1):.1f}")
        self.prop_vars["x1"].set(f"{max(x0,x1):.1f}")
        self.prop_vars["y1"].set(f"{max(y0,y1):.1f}")

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
            app.tex_image = Image.open(tex_path).convert("RGB")
    if len(sys.argv) >= 2:
        app._redraw_canvas()

    root.mainloop()


if __name__ == "__main__":
    main()
