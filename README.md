# Conker Glyph Editor

🇺🇸 English | 🇷🇺 [Русский](README_RU.md)

⚠ Early development source code

A visual glyph editor for Conker: Live & Reloaded fonts (CAFF format).

## Files

- `conker_glyph_format.py` — library for reading and writing the font format (required by the editor)
- `conker_glyph_editor.py` — the main graphical editor (tkinter)

All files must be located in the same folder.

## Screenshot
<p align="center">
  <img src="images/Screenshot.png" width="768">
</p>

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
2. **"Open Texture..."** — select the extracted texture for the same font (BMP or PNG, for example one extracted with CrystalTile2 or your own export).
3. Select the correct **profile** from the drop-down list (`ConkerFont` / `FrontendTitle`). Each profile uses different coordinate calibration coefficients.
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

The Y-coordinate decoding formula (`Y_DIV`) for each font was determined **empirically** by maximizing the match between the glyph rectangles and the actual character outlines on the texture. It has **not** been fully reconstructed from the game's disassembled code. This means:

- Within the editor, everything is internally consistent: what you see on screen (the glyph rectangle over the texture) exactly matches what will be written to the file.
- However, there is no guarantee that the game interprets these bytes in exactly the same way at runtime. Minor differences are possible, especially along the Y axis. The X coordinate is considered much more reliable due to the discovered relationship: `X_DIV × texture_width = 16384`.

If possible, verify your changes in the actual game (for example, using XEMU). It is recommended to make small edits and test them before relying on the editor for large-scale modifications.
