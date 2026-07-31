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
        "X_DIV": 16384 / 1024, "Y_DIV": 16384 / 335, "X_OFFSET": -0.5, "Y_OFFSET": -0.5,
    },
}

# Character-to-glyph mapping table.
#
# Previously this was a set of hardcoded byte offsets found by manually inspecting
# ConkerFont's default.bin. Those offsets turned out to be specific to that one
# file - other fonts (FrontendTitle, Japanese variants, etc.) place the charmap
# at different offsets, since it directly follows the glyph table and the glyph
# table's size varies per font (different glyph_count).
#
# Instead, the charmap is now found AUTOMATICALLY by scanning for its structural
# signature: it's a sequence of (uint16 code, uint16 glyph_index) pairs, where
# glyph_index is always a valid index into the glyph table. See
# ConkerFont._detect_charmap() below for the algorithm. The hardcoded values are
# kept only as a documented fallback/reference for ConkerFont specifically.
_LEGACY_CHARMAP_BLOCKS_CONKERFONT = [
    (0x1392, 0x1412), (0x1416, 0x1496), (0x149a, 0x151a),
    (0x15a2, 0x1622), (0x1626, 0x172a),  # NOTE: end corrected from 0x1726 to 0x172a -
                                          # the original hardcoded range was missing
                                          # one entry (0xFF 'ÿ' -> glyph 153), found
                                          # by the automatic detector.
]


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
    byte15            | -                | Always observed as 0x00; likely padding.

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
        """Automatically finds every (code, glyph_index) pair in the charmap
        region of the file, without relying on any hardcoded offsets.

        Two-pass approach:
          1. Scan for long runs of CONSECUTIVE character codes (e.g. 0x20, 0x21,
             0x22, ...) where each entry's glyph_index is a valid index into the
             glyph table. A run of several such entries in a row is extremely
             unlikely to happen by chance, so this reliably locates the real
             charmap blocks and - just as importantly - their 4-byte alignment
             within the file.
          2. Re-scan the whole region at that confirmed alignment, this time
             accepting ANY single valid (code, glyph_index) pair, not just ones
             that are part of a long run. This picks up the sparse/isolated
             entries (typographic quotes, €, Kanji that isn't laid out in
             sequential code-point order, etc.) that sit between the main runs
             without a long consecutive sequence of their own.

        Handles both small Latin-only fonts (ConkerFont, FrontendTitle - a few
        hundred bytes of charmap) and large CJK fonts (ConkerFontJapanese,
        FrontendTitleJapanese - well over a thousand glyphs, codes scattered
        across the Hiragana/Katakana/Kanji/fullwidth Unicode ranges up to
        0xFFFF) by sizing the search window and the accepted code range off the
        actual glyph_count rather than fixed constants tuned for one font.

        Falls back to the legacy hardcoded ConkerFont offsets if no candidate
        region can be found at all (e.g. a corrupted or very unusual file).
        """
        search_start = GLYPH_TABLE_OFFSET + self.glyph_count * GLYPH_REC_SIZE
        # Charmap entries are 4 bytes each. Budget generously (8x the minimum
        # possible size, plus a fixed safety margin) so large CJK charmaps -
        # which are not necessarily packed as tightly as one entry per glyph -
        # are fully covered instead of being cut off partway through.
        min_needed = self.glyph_count * 4
        search_end = min(search_start + max(0x2000, min_needed * 8), len(self.data))

        # Accept the full BMP range of character codes (0x20 up to 0xFFFF). This
        # covers everything from Latin-1 to Hiragana/Katakana/Kanji/fullwidth
        # forms used by the Japanese font variants. Using the same wide range
        # for every font (rather than guessing "is this CJK?" from glyph_count,
        # which turned out to be unreliable - some Japanese fonts have as few
        # as ~370 glyphs) does not introduce false positives for Latin-only
        # fonts either: tested against ConkerFont/FrontendTitle, the wide range
        # finds the same entries as a narrower one, plus a couple of previously
        # missed ones (e.g. the U+25A1 fallback glyph).
        max_code = 0xFFFF

        charmap = {}
        charmap_offsets = {}  # code -> file offset of the 4-byte (code, glyph_idx) entry
        alignments_seen = set()
        pos = search_start
        MIN_RUN = 8  # long enough that a chance match is effectively impossible

        while pos + 4 <= search_end:
            code, idx = struct.unpack("<HH", self.data[pos:pos + 4])
            if idx < self.glyph_count and 0x20 <= code < max_code:
                p = pos
                expected = code
                run = {}
                run_offsets = {}
                while p + 4 <= search_end:
                    c, gi = struct.unpack("<HH", self.data[p:p + 4])
                    if c != expected or gi >= self.glyph_count:
                        break
                    run[c] = gi
                    run_offsets[c] = p
                    expected += 1
                    p += 4
                if len(run) >= MIN_RUN:
                    charmap.update(run)
                    charmap_offsets.update(run_offsets)
                    alignments_seen.add(pos % 4)
                    pos = p
                    continue
            pos += 2

        if alignments_seen:
            for align in alignments_seen:
                pos2 = search_start + ((align - search_start) % 4)
                while pos2 + 4 <= search_end:
                    code, idx = struct.unpack("<HH", self.data[pos2:pos2 + 4])
                    if idx < self.glyph_count and 0x20 <= code < max_code:
                        charmap[code] = idx
                        charmap_offsets[code] = pos2
                    pos2 += 4

        if not charmap:
            # Fallback: nothing auto-detected (unexpected file layout) - use the
            # legacy hardcoded ConkerFont ranges as a last resort.
            for start, end in _LEGACY_CHARMAP_BLOCKS_CONKERFONT:
                block = self.data[start:end]
                for i in range(0, len(block) - 3, 4):
                    code, idx = struct.unpack("<HH", block[i:i + 4])
                    charmap[code] = idx
                    charmap_offsets[code] = start + i

        # The fallback glyph (shown for missing/unmapped characters) is recorded
        # separately in the font header (fallback CODE, not glyph index) rather
        # than living inside the main charmap blocks - and it can sit at a
        # different byte alignment than the rest of the charmap (observed one
        # entry off, i.e. %4 == 2 instead of %4 == 0), so it needs its own
        # dedicated search rather than being picked up by the aligned scan above.
        fallback_code = struct.unpack("<I", self.data[0x4d8:0x4dc])[0]
        if fallback_code and fallback_code not in charmap:
            pos3 = search_start
            while pos3 + 4 <= search_end:
                code, idx = struct.unpack("<HH", self.data[pos3:pos3 + 4])
                if code == fallback_code and idx < self.glyph_count:
                    charmap[code] = idx
                    charmap_offsets[code] = pos3
                    break
                pos3 += 2

        self._charmap_offsets = charmap_offsets
        return charmap

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

    # ---------- Charmap editing (repurposing existing glyph slots for new codes) ----------

    def remap_charmap_code(self, old_code, new_code):
        """Repurposes an EXISTING charmap entry: the glyph currently shown for
        `old_code` (e.g. a Japanese Hiragana character) will instead be shown
        for `new_code` (e.g. a Cyrillic letter). The glyph's geometry/texture
        rectangle is untouched - only which character code triggers it changes.

        This only works for codes that were found during charmap auto-detection
        (i.e. `old_code in self.charmap`), since it patches the (code, glyph_idx)
        pair in place at its known file offset rather than inserting new bytes -
        the file size and every other structure stays exactly the same size.

        Raises KeyError if old_code isn't a known charmap entry, or ValueError
        if new_code is already in use (to avoid silently creating an ambiguous
        charmap with two different glyphs claiming the same code - remap or
        delete the existing entry for new_code first if that's really what you
        want).
        """
        if old_code not in self.charmap:
            raise KeyError(f"code {hex(old_code)} is not a known charmap entry")
        if new_code in self.charmap:
            raise ValueError(
                f"code {hex(new_code)} is already mapped to glyph "
                f"{self.charmap[new_code]} - remap or remove that entry first"
            )

        glyph_idx = self.charmap[old_code]
        offset = self._charmap_offsets[old_code]

        # Patch the 4 bytes in place: same glyph_idx, new code.
        self.data[offset:offset + 2] = struct.pack("<H", new_code)

        del self.charmap[old_code]
        del self._charmap_offsets[old_code]
        self.charmap[new_code] = glyph_idx
        self._charmap_offsets[new_code] = offset

        self._apply_charmap_to_glyphs()

    def find_codes_in_range(self, first_code, last_code_inclusive):
        """Returns a sorted list of (code, glyph_index) for every currently
        mapped charmap entry whose code falls within [first_code, last_code].
        Handy for finding e.g. all Hiragana/Katakana/Kanji entries to free up:
            font.find_codes_in_range(0x3040, 0x9FFF)
        """
        return sorted(
            (code, idx) for code, idx in self.charmap.items()
            if first_code <= code <= last_code_inclusive
        )
