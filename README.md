[badge-en]: https://img.shields.io/badge/lang-English%20%F0%9F%87%AC%F0%9F%87%A7-white
[badge-ru]: https://img.shields.io/badge/%D1%8F%D0%B7%D1%8B%D0%BA-%D0%A0%D1%83%D1%81%D1%81%D0%BA%D0%B8%D0%B9%20%F0%9F%87%B7%F0%9F%87%BA-white
[vot-readme-en]: README.md
[vot-readme-ru]: README_RU.md
[![en][badge-en]][vot-readme-en]
[![ru][badge-ru]][vot-readme-ru]

<table>
<tr>
<td width="170">

<p>
  <img src="resources/icon.ico">
</p>

</td>
<td>

# Conker Glyph Editor

### A visual glyph editor for Conker: Live & Reloaded fonts (CAFF format).

</td>
</tr>
</table>

## Files

- `conker_glyph_format.py` — library for reading and writing the font format and managing open-addressed character maps (required by the editor)
- `conker_texture.py` — reads and replaces the Gray8 texture embedded in the .bin (used by the library and the editor)
- `conker_clipboard.py` — image clipboard support (copy / paste the texture)
- `conker_glyph_editor.py` — the main graphical editor (tkinter)

All files must be located in the same folder.

## Screenshot

<p align>
  <img src="images/screenshot.png">
</p>

## Installation (Windows)

### Option 1 — Download the executable

Download the latest `.exe` version from the [Releases](https://github.com/AS97GitHub/Conker-Glyph-Editor/releases) page.

> No `python` installation or additional dependencies are required.

### Option 2 — Run from source

1. Make sure Python 3.8 or later is installed (tkinter is included with the standard Windows Python distribution, so no separate installation is required).
2. Install Pillow:

   ```
   pip install Pillow
   ```

> On Linux, copying / pasting the texture with `Ctrl+C` / `Ctrl+V` additionally needs `xclip` (X11) or `wl-clipboard` (Wayland) installed.

## Running

> ⚠️ On Windows, you can use either `python` or `py` to run the script, depending on your Python installation.

> ⚠️ On Linux you may need to use `python3` instead of `python`.

```bash
python conker_glyph_editor.py
```

Or launch it directly with the font file:

```bash
python conker_glyph_editor.py path\to\default.bin
```

## Usage

1. **"Open .bin..."** — select a font file (for example, `default.bin` from `ConkerFont`). The **profile** (`ConkerFont` / `ConkerFontJapanese` / `FrontendTitle` / `FrontendTitleJapanese`) is **detected automatically** from a byte signature in the file and the drop-down updates to match. The texture is read out of the .bin itself and shown on the canvas right away.
2. **Texture:** the texture lives inside the .bin (linear Gray8).
   - **"Export texture..."** — save it as a PNG (Gray8).
   - **"Import texture..."** — replace it with a PNG/BMP. Width must be a multiple of 64 (up to 4096). If the size changes, you are asked whether to **keep** glyph rectangles on the same pixels or **scale** them with the texture. The change is written when you save the .bin.
   - **Ctrl+C** copies the texture to the clipboard, **Ctrl+V** imports the clipboard image as the new texture (the shortcuts do not apply while you type in a text field).
3. **Left tabs — Glyphs / Charmap / Help:**
   - **Glyphs** — list of glyphs as `index  char=… (U+XXXX)`. Select by index; the selected glyph is highlighted on the texture with a light blue rectangle and corner handles.
   - **Charmap** — assigned characters and empty slots of the fixed 2048-slot open-addressed hash table.
   - **Help** — short controls reference.
4. **Visual & Property Editing:**
   - Drag a corner handle to resize the glyph rectangle (`Start X/Y`, `End X/Y`). **Glyph Width / Height** are recalculated from the new rectangle size.
   - Drag the center of the rectangle to move it.
   - Edit fields on the right panel and click **"Apply Changes"**:
     - **X Offset / Y Offset** — character offset (signed -128…127).
     - **Glyph Width / Glyph Height** — physical on-screen size (also scales the atlas sample). UI shows stored value -1.
     - **Advance** — horizontal step to the next character (lo byte of the glyph’s advance (int16)). For U+0020 the game uses **Space Width** from Font Metrics instead.
     - **Kerning** — always -1 in shipped fonts.
     - **Ascent** — at the end of the glyph record (often tracks glyph height; no confirmed on-screen effect alone).
5. **Font Metrics** (right panel, font-wide):
   - **Line Height**, **Space Width**, **Fallback** code point (e.g. `U+25A1`).
   - Atlas size and max hash probes are shown read-only.
6. **Character Management:** select an action, fill the fields, click **"Apply"**:
   - **add** — map an extra character / `U+XXXX` to the selected glyph. Duplicate codes are rejected.
   - **remove** — unmap the character chosen in **Old Char** (a glyph must keep at least one).
   - **replace** — reassign **Old Char** to **New Char**.
   - **new_glyph** — append a copy of the selected glyph; optional **New Char**. The file grows; sizes, offsets and checksum update on save.
   - **delete_glyph** — remove the glyph, shift later indices, shrink the file (confirmation required; irreversible after save).
7. **Saving:**
   - **"Save As..."** — write a new file.
   - **"Save (overwrite)"** — overwrite the open file.

The editor patches glyph records in place and rebuilds the 2048-slot charmap without breaking probe chains. Adding or deleting glyphs re-lays out the container (charmap, 32-byte padding, texture resource, metadata, section sizes). On save, the header checksum and charmap probe limit are refreshed so the game accepts the file.

## Important Note

Atlas coordinates are stored in a fixed 14-bit grid (`UV_RANGE = 16384`):

```
pixel = raw / DIV + OFFSET
DIV   = 16384 / actual_texture_size_in_pixels
OFFSET = -0.5   (all shipped profiles)
```

Glyph record layout (18 bytes, little-endian): `x_offset (int8)`, `y_offset (int8)`, `width (uint8)`, `height (uint8)`, `u0,u1,v0,v1 (uint16)`, `advance (int16)`, `kerning (int16)`, `ascent (int16)`.

**X/Y Offset**, **Glyph Width/Height**, and **Advance** are **confirmed in-game via XEMU**. **Kerning** is always -1 in shipped data; **Ascent** has no confirmed isolated on-screen effect. Verify large-scale changes in-game.

---

Not affiliated with Microsoft / Rare. All trademarks are property of their
respective owners. Intended for non-commercial use (translations, modding,
format research).