"""
conker_glyph_format.py
"""

import struct
import copy

GLYPH_TABLE_OFFSET = 0x506
GLYPH_REC_SIZE = 18
GLYPH_COUNT_HEADER_OFFSET = 0x4d4

SENTINEL = 0xFFFF  # "No rectangle" marker (space, system glyphs)

# Calibration parameters per font
FONT_PROFILES = {
    "ConkerFont": {
        "X_DIV": 16384 / 256, "Y_DIV": 16384 / 240, "X_OFFSET": -0.5, "Y_OFFSET": -0.5,
    },
    "ConkerFontJapanese": {
        "X_DIV": 16384 / 1024, "Y_DIV": 16384 / 772, "X_OFFSET": -1.5, "Y_OFFSET": -0.5,
    },
    "FrontendTitle": {
        "X_DIV": 16384 / 512, "Y_DIV": 16384 / 203, "X_OFFSET": 0.5, "Y_OFFSET": -0.5,
    },
    "FrontendTitleJapanese": {
        "X_DIV": 16384 / 1024, "Y_DIV": 16384 / 335, "X_OFFSET": 0.5, "Y_OFFSET": -0.5,
    },
}

# Character-to-glyph mapping table: block ranges inside .data
CHARMAP_BLOCKS = [
    (0x1392, 0x1412), (0x1416, 0x1496), (0x149a, 0x151a),
    (0x15a2, 0x1622), (0x1626, 0x16a6), (0x16aa, 0x1726),
]
CHARMAP_EXTRA = {
    0x176e: '18209c0019205600', 0x17aa: '26209b00', 0x191a: '78018100',
    0x1986: '92015000', 0x19d2: 'ac204000', 0x2e46: 'a125c600',
}


class Glyph:
    """A single entry in the glyph table, containing raw and unpacked fields."""

    __slots__ = (
        "index", "advance", "field1", "field2",
        "x0_raw", "x1_raw", "y0_raw", "y1_raw",
        "byte14", "byte15", "is_special", "char",
    )

    def __init__(self, index):
        self.index = index
        self.advance = 0
        self.field1 = 0
        self.field2 = 0
        self.x0_raw = 0
        self.x1_raw = 0
        self.y0_raw = 0
        self.y1_raw = 0
        self.byte14 = 0
        self.byte15 = 0
        self.is_special = False
        self.char = ""  # Populated externally from charmap, can be empty

    def to_pixels(self, x_div, y_div, x_off, y_off):
        """Returns (x0, y0, x1, y1) in texture pixels, or None if is_special."""
        if self.is_special:
            return None
        x0 = self.x0_raw / x_div + x_off
        x1 = self.x1_raw / x_div + x_off
        y0 = self.y0_raw / y_div + y_off
        y1 = self.y1_raw / y_div + y_off
        return (x0, y0, x1, y1)

    def set_from_pixels(self, x0, y0, x1, y1, x_div, y_div, x_off, y_off):
        """Inverse conversion: from pixel coordinates back to raw values."""
        self.x0_raw = round((x0 - x_off) * x_div)
        self.x1_raw = round((x1 - x_off) * x_div)
        self.y0_raw = round((y0 - y_off) * y_div)
        self.y1_raw = round((y1 - y_off) * y_div)
        self.is_special = False

    def clone(self):
        g = Glyph(self.index)
        for slot in self.__slots__:
            setattr(g, slot, getattr(self, slot))
        return g


class ConkerFont:
    """Loads default.bin fully into memory, provides access to glyphs, and allows
    writing changes back (in-place patch, leaving the rest of the file completely intact)."""

    def __init__(self, path, profile_name="ConkerFont"):
        self.path = path
        self.profile_name = profile_name
        self.profile = FONT_PROFILES[profile_name]
        with open(path, "rb") as f:
            self.data = bytearray(f.read())

        if self.data[0:4] != b"CAFF":
            raise ValueError(f"{path}: does not look like a CAFF container (magic={self.data[0:4]!r})")

        self.glyph_count = struct.unpack(
            "<I", self.data[GLYPH_COUNT_HEADER_OFFSET:GLYPH_COUNT_HEADER_OFFSET + 4]
        )[0]

        self.glyphs = self._read_glyphs()
        self.charmap = self._read_charmap()  # code -> glyph_index
        self._apply_charmap_to_glyphs()

    # ---------- Reading ----------

    def _read_glyphs(self):
        glyphs = []
        for i in range(self.glyph_count):
            off = GLYPH_TABLE_OFFSET + i * GLYPH_REC_SIZE
            rec = self.data[off:off + 16]
            g = Glyph(i)
            g.advance = struct.unpack("<H", rec[0:2])[0]
            g.field1 = struct.unpack("<H", rec[2:4])[0]
            g.field2 = struct.unpack("<H", rec[4:6])[0]
            g.x0_raw = struct.unpack("<H", rec[6:8])[0]
            g.x1_raw = struct.unpack("<H", rec[8:10])[0]
            g.y0_raw = struct.unpack("<H", rec[10:12])[0]
            g.y1_raw = struct.unpack("<H", rec[12:14])[0]
            g.byte14 = rec[14]
            g.byte15 = rec[15]
            g.is_special = (
                g.x0_raw > 60000 or g.x1_raw > 60000 or
                g.y0_raw > 60000 or g.y1_raw > 60000
            )
            glyphs.append(g)
        return glyphs

    def _read_charmap(self):
        charmap = {}
        for start, end in CHARMAP_BLOCKS:
            block = self.data[start:end]
            for i in range(0, len(block) - 3, 4):
                code, idx = struct.unpack("<HH", block[i:i + 4])
                charmap[code] = idx
        for off, hexstr in CHARMAP_EXTRA.items():
            raw = bytes.fromhex(hexstr)
            for i in range(0, len(raw) - 3, 4):
                code, idx = struct.unpack("<HH", raw[i:i + 4])
                charmap[code] = idx
        return charmap

    def _apply_charmap_to_glyphs(self):
        idx_to_char = {}
        for code, idx in self.charmap.items():
            if 0x20 <= code < 0x2100:
                idx_to_char.setdefault(idx, chr(code))
        for g in self.glyphs:
            g.char = idx_to_char.get(g.index, "")

    # ---------- Writing ----------

    def write_glyph(self, glyph):
        """Writes modified Glyph back into self.data (in-memory, not saved to disk yet)."""
        i = glyph.index
        off = GLYPH_TABLE_OFFSET + i * GLYPH_REC_SIZE
        rec = struct.pack(
            "<HHHHHHHBB",
            glyph.advance,
            glyph.field1,
            glyph.field2,
            glyph.x0_raw,
            glyph.x1_raw,
            glyph.y0_raw,
            glyph.y1_raw,
            glyph.byte14,
            glyph.byte15,
        )
        assert len(rec) == 16, f"internal error: record must be 16 bytes, got {len(rec)}"
        self.data[off:off + 16] = rec
        self.glyphs[i] = glyph

    def save(self, out_path=None):
        """Saves the entire file (with applied edits) to the target path.
        If out_path is None, overwrites the source file (self.path)."""
        target = out_path or self.path
        with open(target, "wb") as f:
            f.write(self.data)
        return target

    # ---------- High-level helper functions ----------

    def get_glyph_by_char(self, ch):
        idx = self.charmap.get(ord(ch))
        if idx is None:
            return None
        return self.glyphs[idx]

    def to_pixels(self, glyph):
        p = self.profile
        return glyph.to_pixels(p["X_DIV"], p["Y_DIV"], p["X_OFFSET"], p["Y_OFFSET"])

    def set_pixels(self, glyph, x0, y0, x1, y1):
        p = self.profile
        glyph.set_from_pixels(x0, y0, x1, y1, p["X_DIV"], p["Y_DIV"], p["X_OFFSET"], p["Y_OFFSET"])
