"""
conker_glyph_format.py
"""

import struct
import copy

GLYPH_TABLE_OFFSET = 0x506
GLYPH_REC_SIZE = 18
GLYPH_COUNT_HEADER_OFFSET = 0x4d4

# The glyph table is followed by one uint16 sentinel, then a fixed-size open
# addressing table of (uint16 Unicode code point, uint16 glyph index) pairs.
CHARMAP_LEADING_SENTINEL_SIZE = 2
CHARMAP_SLOT_COUNT = 0x800
CHARMAP_SLOT_SIZE = 4
CHARMAP_SIZE = CHARMAP_SLOT_COUNT * CHARMAP_SLOT_SIZE

SENTINEL = 0xFFFF  # "No rectangle" marker (space, system glyphs)

# Calibration parameters per font
FONT_PROFILES = {
    "ConkerFont": {
        "X_DIV": 16384 / 256, "Y_DIV": 16384 / 240, "X_OFFSET": -0.5, "Y_OFFSET": -0.5,
    },
    "ConkerFontJapanese": {
        "X_DIV": 16384 / 1024, "Y_DIV": 16384 / 772, "X_OFFSET": -0.5, "Y_OFFSET": -0.5,
    },
    "FrontendTitle": {
        "X_DIV": 16384 / 512, "Y_DIV": 16384 / 203, "X_OFFSET": -0.5, "Y_OFFSET": -0.5,
    },
    "FrontendTitleJapanese": {
        "X_DIV": 16384 / 1024, "Y_DIV": 16384 / 335, "X_OFFSET": -0.5, "Y_OFFSET": -0.5,
    },
}

# Profile detection signatures: byte patterns at specific offsets
_PROFILE_SIGNATURES = {
    "ConkerFont": {
        "offset": 0x33A2,
        "bytes": b"ConkerFont",
    },
    "ConkerFontJapanese": {
        "offset": 0x7922,
        "bytes": b"ConkerFontJapanese",
    },
    "FrontendTitle": {
        "offset": 0x2E42,
        "bytes": b"FrontendTitle",
    },
    "FrontendTitleJapanese": {
        "offset": 0x3FA2,
        "bytes": b"FrontendTitleJapanese",
    },
}


def detect_profile(data):
    """Auto-detect font profile from byte signatures in the file.
    
    Args:
        data: bytearray or bytes of the file contents
        
    Returns:
        str: Profile name if detected, None if no match found
    """
    for profile_name, sig in _PROFILE_SIGNATURES.items():
        offset = sig["offset"]
        expected_bytes = sig["bytes"]
        if offset + len(expected_bytes) <= len(data):
            actual_bytes = data[offset:offset + len(expected_bytes)]
            if actual_bytes == expected_bytes:
                return profile_name
    return None


class Glyph:
    """A single entry in the glyph table, containing raw and unpacked fields.

    Field meanings, CONFIRMED IN-GAME (via XEMU, by editing one field at a time
    and comparing screenshots against an unmodified baseline). Internal attribute
    names below are kept as-is for backward compatibility with existing code/data;
    the "display name" column is what the editor UI now shows the user.

    attribute         | display name     | meaning
    --------------------------------------------------------------------------------
    unknown_field     | Unknown          | CONFIRMED IN-GAME: no visible effect even
                      |                  | at extreme test values (0 and 255), not
                      |                  | just small changes. High byte is always
                      |                  | 0x00 across the whole glyph table (not a
                      |                  | second independent byte - the "two
                      |                  | independent parameters" idea was tested
                      |                  | and does not hold at the byte-split
                      |                  | level). Low byte holds a plausible but
                      |                  | apparently inert number (0-55 range).
                      |                  | Most likely explanation: not read by the
                      |                  | text renderer at all - possibly legacy/
                      |                  | tooling data, or used by some other game
                      |                  | system unrelated to on-screen glyph
                      |                  | drawing. Not fully ruled out.
    --------------------------------------------------------------------------------
    x0_raw            | Start X          | Texture-atlas rectangle, left edge.
    y0_raw            | Start Y          | Texture-atlas rectangle, top edge.
    x1_raw            | End X            | Texture-atlas rectangle, right edge.
    y1_raw            | End Y            | Texture-atlas rectangle, bottom edge.
    --------------------------------------------------------------------------------
    field1 (hi byte)  | Y Bearing        | Vertical offset of the glyph relative to
                      |                  | the baseline. Positive = glyph sits
                      |                  | lower (below baseline); negative = glyph
                      |                  | sits higher (above baseline). Signed int8.
    field1 (lo byte)  | X Bearing        | Horizontal offset of the glyph relative
                      |                  | to the baseline. Negative = shifted left;
                      |                  | positive = shifted right. Signed int8.
    --------------------------------------------------------------------------------
    field2 (hi byte)  | Glyph Height     | Physical glyph height. Also rescales
                      |                  | (stretches/squashes) the glyph on the Y
                      |                  | axis when changed. Unsigned.
    field2 (lo byte)  | Glyph Width      | Physical glyph width. Also rescales
                      |                  | (stretches/squashes) the glyph on the X
                      |                  | axis when changed. Unsigned.
    --------------------------------------------------------------------------------
    byte14            | Advance Width    | Horizontal step after this character -
                      |                  | determines where the next character
                      |                  | starts. This is what `unknown_field` (above)
                      |                  | was originally assumed to do. Unsigned.
    --------------------------------------------------------------------------------
    byte15            | -                | Always observed as 0x00; purpose unknown.

    x0_raw..y1_raw use the FONT_PROFILES pixel-conversion formula (raw / DIV +
    OFFSET). field1/field2 are stored as one uint16 each in the file but behave
    as two INDEPENDENT single bytes - not as one combined 16-bit number.
    """

    __slots__ = (
        "index", "unknown_field", "field1", "field2",
        "x0_raw", "x1_raw", "y0_raw", "y1_raw",
        "byte14", "byte15", "is_special", "char",
    )

    def __init__(self, index):
        self.index = index
        self.unknown_field = 0
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
        """Returns (x0, y0, x1, y1) in texture pixels, or None if is_special.

        Rounded to the nearest whole pixel: raw values in the file are always
        integers, and x_off/y_off are non-integer per profile (e.g. -0.5), so
        raw/DIV + OFFSET lands a fraction of a pixel off an integer purely from
        that arithmetic (observed max deviation ~0.01-0.05px across all four
        profiles) - not a meaningful sub-pixel value. Rounding here keeps the
        displayed/drawn coordinate consistent with what set_from_pixels() will
        write back, so re-saving an untouched glyph reproduces the original
        raw value exactly.
        """
        if self.is_special:
            return None
        x0 = round(self.x0_raw / x_div + x_off)
        x1 = round(self.x1_raw / x_div + x_off)
        y0 = round(self.y0_raw / y_div + y_off)
        y1 = round(self.y1_raw / y_div + y_off)
        return (x0, y0, x1, y1)

    def set_from_pixels(self, x0, y0, x1, y1, x_div, y_div, x_off, y_off):
        """Inverse conversion: from pixel coordinates back to raw values."""
        self.x0_raw = int((x0 - x_off) * x_div)
        self.x1_raw = int((x1 - x_off) * x_div)
        self.y0_raw = int((y0 - y_off) * y_div)
        self.y1_raw = int((y1 - y_off) * y_div)
        self.is_special = False

    def clone(self):
        g = Glyph(self.index)
        for slot in self.__slots__:
            setattr(g, slot, getattr(self, slot))
        return g


class ConkerFont:
    """Loads default.bin fully into memory, provides access to glyphs, and allows
    writing changes back (in-place patch, leaving the rest of the file completely intact)."""

    def __init__(self, path, profile_name=None):
        self.path = path
        with open(path, "rb") as f:
            self.data = bytearray(f.read())
        
        # Auto-detect profile if not specified
        if profile_name is None:
            profile_name = detect_profile(self.data)
            if profile_name is None:
                profile_name = "ConkerFont"  # Fallback to default
        
        self.profile_name = profile_name
        self.profile = FONT_PROFILES[profile_name]

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
            g.unknown_field = struct.unpack("<H", rec[0:2])[0]
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
        """Read the fixed 2048-slot character-to-glyph hash table.

        The table follows the 18-byte glyph records and a single uint16
        sentinel.  Each little-endian slot is ``(Unicode code point,
        glyph_index)``; ``FFFF FFFF`` denotes an unused slot.  Entries are
        placed with double hashing, but reading every slot directly avoids
        depending on the probing algorithm or on heuristics about code ranges.
        """
        charmap_start = self._charmap_start()
        charmap_end = charmap_start + CHARMAP_SIZE
        if charmap_end > len(self.data):
            raise ValueError(
                f"{self.path}: truncated charmap "
                f"(needs bytes 0x{charmap_start:X}..0x{charmap_end:X})"
            )

        charmap = {}
        charmap_offsets = {}
        for slot in range(CHARMAP_SLOT_COUNT):
            offset = charmap_start + slot * CHARMAP_SLOT_SIZE
            code, glyph_index = struct.unpack_from("<HH", self.data, offset)
            if code == SENTINEL and glyph_index == SENTINEL:
                continue
            if code == SENTINEL or glyph_index == SENTINEL:
                raise ValueError(
                    f"{self.path}: malformed charmap slot {slot} at 0x{offset:X}"
                )
            if glyph_index >= self.glyph_count:
                raise ValueError(
                    f"{self.path}: charmap slot {slot} at 0x{offset:X} references "
                    f"glyph {glyph_index}, but the font has {self.glyph_count} glyphs"
                )
            if code in charmap:
                raise ValueError(
                    f"{self.path}: duplicate U+{code:04X} in charmap"
                )
            charmap[code] = glyph_index
            charmap_offsets[code] = offset

        self._charmap_offsets = charmap_offsets
        return charmap

    def _charmap_start(self):
        """Return the byte offset of slot zero in the fixed charmap."""
        return (
            GLYPH_TABLE_OFFSET
            + self.glyph_count * GLYPH_REC_SIZE
            + CHARMAP_LEADING_SENTINEL_SIZE
        )

    def count_empty_charmap_slots(self):
        """Return how many of the CHARMAP_SLOT_COUNT fixed slots are unused
        (raw bytes FFFF FFFF), i.e. free/empty entries in the hash table."""
        charmap_start = self._charmap_start()
        count = 0
        for slot in range(CHARMAP_SLOT_COUNT):
            offset = charmap_start + slot * CHARMAP_SLOT_SIZE
            code, glyph_index = struct.unpack_from("<HH", self.data, offset)
            if code == SENTINEL and glyph_index == SENTINEL:
                count += 1
        return count

    def _apply_charmap_to_glyphs(self):
        """Fills in Glyph.char (the human-readable character shown in the UI)
        from the charmap. Uses the full BMP range (0x20-0xFFFF), matching
        _read_charmap - the previous narrower 0x20-0x2100 cutoff hid every CJK
        character (Hiragana/Katakana/Kanji all start at 0x3040+), leaving
        `char` empty for them even though the charmap itself had the right
        entry.

        When several codes point at the same glyph (can legitimately happen -
        e.g. full-width and half-width variants of the same character), picks
        the "most plausible" one to display: Latin/Cyrillic/common punctuation
        and CJK ranges are preferred over obscure/rare Unicode blocks, since a
        code landing in one of those rare blocks is more likely to be charmap
        detection noise than an intentional mapping.
        """
        def plausibility(code):
            if 0x20 <= code < 0x7F: return 0          # ASCII
            if 0x80 <= code < 0x250: return 1          # Latin-1 / Latin Extended
            if 0x400 <= code < 0x500: return 1          # Cyrillic
            if 0x2000 <= code < 0x2100: return 1          # general punctuation
            if 0x3040 <= code < 0xA000: return 1          # Hiragana/Katakana/CJK
            if 0xFF00 <= code < 0xFFF0: return 1          # fullwidth forms
            return 2                                        # anything else: least preferred

        idx_to_char = {}
        idx_to_rank = {}
        for code, idx in self.charmap.items():
            if not (0x20 <= code < 0xFFFF):
                continue
            rank = plausibility(code)
            if idx not in idx_to_rank or rank < idx_to_rank[idx]:
                idx_to_rank[idx] = rank
                idx_to_char[idx] = chr(code)
        for g in self.glyphs:
            g.char = idx_to_char.get(g.index, "")

    def remap_glyph_character(self, glyph_index, old_code, new_code):
        """Replace one glyph's Unicode code point without changing file size.

        The charmap is open-addressed, so changing a code in place would make
        it unreachable.  Rebuild its fixed 2048-slot table instead, preserving
        every mapping except ``old_code -> glyph_index``.  The rebuilt table
        uses the game's double-hash probe sequence.
        """
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if not (0 <= old_code < SENTINEL):
            raise ValueError("the current character code is invalid")
        if not (0 <= new_code < SENTINEL):
            raise ValueError("character must be a BMP code point other than U+FFFF")
        if self.charmap.get(old_code) != glyph_index:
            raise ValueError(
                f"U+{old_code:04X} is not assigned to glyph #{glyph_index}"
            )
        if new_code == old_code:
            return
        if new_code in self.charmap:
            owner = self.charmap[new_code]
            raise ValueError(
                f"U+{new_code:04X} is already assigned to glyph #{owner}"
            )

        # _charmap_offsets is ordered by its physical slot, which provides a
        # stable insertion order while rebuilding the table.
        entries = [
            (new_code if code == old_code else code, mapped_glyph_index)
            for code, mapped_glyph_index in sorted(
                self.charmap.items(), key=lambda item: self._charmap_offsets[item[0]]
            )
        ]
        rebuilt = bytearray(b"\xFF" * CHARMAP_SIZE)
        occupied = [False] * CHARMAP_SLOT_COUNT
        for code, mapped_glyph_index in entries:
            step = (code >> 5) + 2
            slot = (code + step) & (CHARMAP_SLOT_COUNT - 1)
            for _ in range(CHARMAP_SLOT_COUNT):
                if not occupied[slot]:
                    struct.pack_into(
                        "<HH", rebuilt, slot * CHARMAP_SLOT_SIZE, code, mapped_glyph_index
                    )
                    occupied[slot] = True
                    break
                slot = (slot + step) & (CHARMAP_SLOT_COUNT - 1)
            else:
                raise ValueError(f"no free charmap slot is reachable for U+{code:04X}")

        charmap_start = self._charmap_start()
        self.data[charmap_start:charmap_start + CHARMAP_SIZE] = rebuilt
        self.charmap = self._read_charmap()
        self._apply_charmap_to_glyphs()

    def remove_glyph_character_alias(self, glyph_index, code):
        """Remove a character mapping from a glyph.

        Only allowed if the glyph has more than one character mapping.
        This removes the specific code->glyph_index mapping from the charmap.
        """
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if not (0 <= code < SENTINEL):
            raise ValueError("character must be a BMP code point other than U+FFFF")
        if self.charmap.get(code) != glyph_index:
            raise ValueError(
                f"U+{code:04X} is not assigned to glyph #{glyph_index}"
            )
        
        # Check if glyph has more than one character
        glyph_codes = [c for c, idx in self.charmap.items() if idx == glyph_index]
        if len(glyph_codes) <= 1:
            raise ValueError(
                f"Cannot remove the only character from glyph #{glyph_index}. "
                "Use 'Add Character Alias' to add another character first."
            )

        # Find the physical slot for this code
        charmap_start = self._charmap_start()
        step = (code >> 5) + 2
        slot = (code + step) & (CHARMAP_SLOT_COUNT - 1)
        
        # Search for the slot containing this code
        found = False
        for _ in range(CHARMAP_SLOT_COUNT):
            offset = charmap_start + slot * CHARMAP_SLOT_SIZE
            existing_code, existing_glyph_index = struct.unpack_from("<HH", self.data, offset)
            if existing_code == code and existing_glyph_index == glyph_index:
                # Mark this slot as empty (FFFF FFFF)
                struct.pack_into("<HH", self.data, offset, SENTINEL, SENTINEL)
                found = True
                break
            slot = (slot + step) & (CHARMAP_SLOT_COUNT - 1)
        
        if not found:
            raise ValueError(f"Could not find charmap slot for U+{code:04X}")

        # Remove from charmap dictionary
        del self.charmap[code]
        if code in self._charmap_offsets:
            del self._charmap_offsets[code]
        
        self._apply_charmap_to_glyphs()

    def add_glyph_character_alias(self, glyph_index, code):
        """Map an additional Unicode code point to an existing glyph.

        This fills one unused ``FFFF FFFF`` charmap slot.  The glyph's existing
        mappings remain intact, so both the old and new characters render the
        same atlas rectangle and metrics.
        """
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if not (0 <= code < SENTINEL):
            raise ValueError("character must be a BMP code point other than U+FFFF")
        if code in self.charmap:
            owner = self.charmap[code]
            raise ValueError(
                f"U+{code:04X} is already assigned to glyph #{owner}"
            )

        charmap_start = self._charmap_start()
        step = (code >> 5) + 2
        slot = (code + step) & (CHARMAP_SLOT_COUNT - 1)
        for _ in range(CHARMAP_SLOT_COUNT):
            offset = charmap_start + slot * CHARMAP_SLOT_SIZE
            existing_code, existing_glyph_index = struct.unpack_from("<HH", self.data, offset)
            if existing_code == SENTINEL and existing_glyph_index == SENTINEL:
                struct.pack_into("<HH", self.data, offset, code, glyph_index)
                self.charmap[code] = glyph_index
                self._charmap_offsets[code] = offset
                self._apply_charmap_to_glyphs()
                return
            slot = (slot + step) & (CHARMAP_SLOT_COUNT - 1)

        raise ValueError(f"no free charmap slot is reachable for U+{code:04X}")

    # ---------- Writing ----------

    def write_glyph(self, glyph):
        """Writes modified Glyph back into self.data (in-memory, not saved to disk yet)."""
        i = glyph.index
        off = GLYPH_TABLE_OFFSET + i * GLYPH_REC_SIZE
        rec = struct.pack(
            "<HHHHHHHBB",
            glyph.unknown_field,
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

    def to_pixels(self, glyph):
        p = self.profile
        return glyph.to_pixels(p["X_DIV"], p["Y_DIV"], p["X_OFFSET"], p["Y_OFFSET"])

    def set_pixels(self, glyph, x0, y0, x1, y1):
        p = self.profile
        glyph.set_from_pixels(x0, y0, x1, y1, p["X_DIV"], p["Y_DIV"], p["X_OFFSET"], p["Y_OFFSET"])
