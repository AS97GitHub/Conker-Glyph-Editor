# Conker Glyph Editor

#### ⚠ Early development source code

### _A visual glyph editor for Conker: Live & Reloaded fonts (CAFF format)._

### 🇺🇸 English | 🇷🇺 [Русский](README_RU.md)

## Files

- `conker_glyph_format.py` — library for reading and writing the font format (required by the editor)
- `conker_glyph_editor.py` — the main graphical editor (tkinter)

All files must be located in the same folder.

## Installation (Windows)

1. Make sure Python 3.8 or later is installed (tkinter is included with the standard Windows Python distribution, so no separate installation is required).
2. Install Pillow:
   ```
   pip install Pillow
   ```

## Running

> ⚠️ On Windows, you can use either `python` or `py` to run the script, depending on your Python installation.

> ⚠️ On Linux you may need to use `python3` instead of `python`.

```bash
python conker_glyph_editor.py
```

Or launch it directly with the font and texture files:

```bash
python conker_glyph_editor.py path\to\default.bin path\to\texture.png
```

## Usage

1. **"Open .bin..."** — select a font file (for example, `default.bin` from `ConkerFont`).
2. **"Open Texture..."** — select the extracted texture for the same font (BMP or PNG, for example one extracted with CrystalTile2, ImageHeat, or your own export).
3. Select the correct **profile** from the drop-down list (`ConkerFont` / `ConkerFontJapanese` / `FrontendTitle` / `FrontendTitleJapanese`). Each profile is a fixed preset with the coordinate calibration coefficients already determined for that font (see "Important Note" below) — there is no interactive calibration step, just pick the profile matching the font you opened.
4. The glyph list on the left lets you select a glyph. The selected glyph is highlighted on the texture with a light blue rectangle and corner handles.
5. **Editing:**
   - Drag a corner handle to resize the glyph rectangle (`x0/y0/x1/y1`).
   - Drag the center of the rectangle to move it.
   - Alternatively, enter values manually in the panel on the right and click **"Apply Changes"**.
   - The `advance` field specifies the glyph advance width and can also be edited manually.
6. **Saving:**
   - **"Save As..."** — save to a new file (recommended to preserve the original).
   - **"Save (Overwrite)"** — overwrite the currently opened file (confirmation required).

The editor modifies **only the bytes of the selected glyph record** (18 bytes per glyph). The rest of the file (texture, other glyphs, headers, etc.) remains completely unchanged at the byte level.

## Important Note

The coordinate decoding formula for each font is:

```
pixel = raw / DIV + OFFSET
DIV   = 16384 / actual_used_texture_size_in_pixels
```

where 16384 = 2¹⁴ — coordinates are stored in a fixed 14-bit normalized grid. The
"actual used size" is the real width/height of the texture's content (for example what
ImageHeat produces after trimming empty padding: 256×240 for ConkerFont, 512×203 for
FrontendTitle), not the power-of-two file dimensions of the texture.

This formula has been validated via IoU against the real textures of the known fonts
(~0.85–0.87 average overlap between predicted and actual glyph outlines) and gives a
single, structurally consistent logic for both X and Y, rather than two independently
fitted numbers. However: it has **not** been fully verified through disassembly/debugging
of the game's own code — a discrepancy with what the actual engine uses is still
theoretically possible, especially for texture sizes not yet covered by the tested
samples.

If possible, verify your changes in the actual game (for example, using XEMU). It is
recommended to make small edits and test them before relying on the editor for
large-scale modifications.
