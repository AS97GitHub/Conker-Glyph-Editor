"""
conker_glyph_format.py - reader/patcher for the glyph table and character map of
Conker: Live & Reloaded CAFF font files (default.bin).

Edits are applied in place to the in-memory file image; everything outside the
touched records stays byte-identical.
"""

import math
import os
import shutil
import struct
import tempfile

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

# Glyph rectangle coordinates (x0/x1/y0/y1_raw) are uint16 values in a
# 0..16384 space: raw = (pixel - offset) * 16384 / texture_size (see
# FONT_PROFILES).  A real rectangle therefore never exceeds ~16384 (largest
# value seen in the four shipped fonts: 16376).
# "Special" glyphs (e.g. space) store a mirrored, negative-looking pair
# instead: x1 = 65536 - x0 and y1 = 65536 - y0 as uint16, e.g. (32, 65504,
# 34, 65502).  Every value found in them is >= 65496.
# A glyph is treated as "special" (no drawable rectangle) when any of its four
# coordinates is above this threshold.  The value is an empirically chosen cut
# inside the empty gap between 16384 and 65496; it is NOT known to be a
# constant taken from the game engine itself.
SPECIAL_GLYPH_RAW_THRESHOLD = 60000

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


def _code_plausibility(code):
    """Rank a code point for display (lower = more plausible to show).

    Used when several codes point at the same glyph: Latin/Cyrillic/common
    punctuation and CJK ranges are preferred over obscure Unicode blocks.
    """
    if 0x20 <= code < 0x7F: return 0          # ASCII
    if 0x80 <= code < 0x250: return 1          # Latin-1 / Latin Extended
    if 0x400 <= code < 0x500: return 1          # Cyrillic
    if 0x2000 <= code < 0x2100: return 1          # general punctuation
    if 0x3040 <= code < 0xA000: return 1          # Hiragana/Katakana/CJK
    if 0xFF00 <= code < 0xFFF0: return 1          # fullwidth forms
    return 2                                        # anything else: least preferred


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
        profiles) - not a meaningful sub-pixel value.

        The round trip raw -> pixels -> raw is lossy (a pixel spans dozens of raw
        units), so callers should only call set_from_pixels() for a rectangle
        that was actually edited.
        """
        if self.is_special:
            return None
        x0 = round(self.x0_raw / x_div + x_off)
        x1 = round(self.x1_raw / x_div + x_off)
        y0 = round(self.y0_raw / y_div + y_off)
        y1 = round(self.y1_raw / y_div + y_off)
        return (x0, y0, x1, y1)

    def set_from_pixels(self, x0, y0, x1, y1, x_div, y_div, x_off, y_off):
        """Inverse conversion: from pixel coordinates back to raw values.

        Raises ValueError, leaving the glyph untouched, if a coordinate is not a
        finite number or its raw value would not be a drawable rectangle
        (0..SPECIAL_GLYPH_RAW_THRESHOLD; larger values are what the format
        reserves for "special" glyphs, and uint16 cannot hold negatives).
        """
        raws = []
        for pixel, div, off in ((x0, x_div, x_off), (x1, x_div, x_off),
                                (y0, y_div, y_off), (y1, y_div, y_off)):
            if not math.isfinite(pixel):
                raise ValueError("Coordinates must be finite numbers")
            raw = int((pixel - off) * div)
            if not 0 <= raw <= SPECIAL_GLYPH_RAW_THRESHOLD:
                raise ValueError(
                    f"Coordinate {pixel:g} is outside the texture range of this profile"
                )
            raws.append(raw)
        self.x0_raw, self.x1_raw, self.y0_raw, self.y1_raw = raws
        self.is_special = False

    def clone(self):
        g = Glyph(self.index)
        for slot in self.__slots__:
            setattr(g, slot, getattr(self, slot))
        return g

    def same_data(self, other):
        """True if every field (including index and char) equals ``other``'s."""
        return all(getattr(self, slot) == getattr(other, slot) for slot in self.__slots__)


class ConkerFont:
    """Loads default.bin fully into memory, provides access to glyphs, and allows
    writing changes back (in-place patch, leaving the rest of the file completely intact)."""

    def __init__(self, path, profile_name=None):
        self.path = path
        with open(path, "rb") as f:
            self.data = bytearray(f.read())

        if self.data[0:4] != b"CAFF":
            raise ValueError(f"{path}: does not look like a CAFF container (magic={self.data[0:4]!r})")
        if len(self.data) < GLYPH_COUNT_HEADER_OFFSET + 4:
            raise ValueError(f"{path}: truncated file (no glyph count in header)")

        detected = None
        if profile_name is None:
            detected = detect_profile(self.data)
            profile_name = detected or "ConkerFont"  # fall back to the default profile
        if profile_name not in FONT_PROFILES:
            raise ValueError(f"unknown font profile {profile_name!r}")

        self.profile_name = profile_name
        self.profile = FONT_PROFILES[profile_name]
        self.profile_autodetected = detected is not None

        self.glyph_count = struct.unpack(
            "<I", self.data[GLYPH_COUNT_HEADER_OFFSET:GLYPH_COUNT_HEADER_OFFSET + 4]
        )[0]

        self.glyphs = self._read_glyphs()
        self.charmap = self._read_charmap()  # code -> glyph_index
        self._apply_charmap_to_glyphs()

    # ---------- Reading ----------

    def _read_glyphs(self):
        table_end = GLYPH_TABLE_OFFSET + self.glyph_count * GLYPH_REC_SIZE
        if table_end > len(self.data):
            raise ValueError(
                f"{self.path}: truncated glyph table "
                f"(needs bytes up to 0x{table_end:X}, file has 0x{len(self.data):X})"
            )
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
            g.is_special = max(
                g.x0_raw, g.x1_raw, g.y0_raw, g.y1_raw
            ) > SPECIAL_GLYPH_RAW_THRESHOLD
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

    def codes_for_glyph(self, glyph_index):
        """Sorted list of every Unicode code point mapped to ``glyph_index``."""
        return sorted(code for code, idx in self.charmap.items() if idx == glyph_index)

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
        from the charmap, using the BMP range 0x20-0xFFFE (CJK included).

        When several codes point at the same glyph (can legitimately happen -
        e.g. full-width and half-width variants of the same character), picks
        the "most plausible" one to display: Latin/Cyrillic/common punctuation
        and CJK ranges are preferred over obscure/rare Unicode blocks, since a
        code landing in one of those rare blocks is more likely to be charmap
        detection noise than an intentional mapping.
        """
        idx_to_char = {}
        idx_to_rank = {}
        for code, idx in self.charmap.items():
            if not (0x20 <= code < 0xFFFF):
                continue
            rank = _code_plausibility(code)
            if idx not in idx_to_rank or rank < idx_to_rank[idx]:
                idx_to_rank[idx] = rank
                idx_to_char[idx] = chr(code)
        for g in self.glyphs:
            g.char = idx_to_char.get(g.index, "")

    # ---------- Charmap probing helpers ----------

    @staticmethod
    def _charmap_step(code):
        """Probe step used by the game's open-addressing charmap."""
        return (code >> 5) + 2

    @classmethod
    def _probe_chain_length(cls, code):
        """Number of distinct slots the probe sequence of ``code`` can reach.

        The table size is a power of two, so an even step only visits
        ``CHARMAP_SLOT_COUNT / gcd(step, CHARMAP_SLOT_COUNT)`` slots.  For a few
        codes (step divisible by 1024) that is only 2 slots, or even 1.  The
        step itself is dictated by the game's lookup and must not be changed.
        """
        return CHARMAP_SLOT_COUNT // math.gcd(cls._charmap_step(code), CHARMAP_SLOT_COUNT)

    @classmethod
    def _probe_slots(cls, code):
        """Yield every distinct slot reachable for ``code``, in probe order."""
        step = cls._charmap_step(code)
        slot = (code + step) & (CHARMAP_SLOT_COUNT - 1)
        for _ in range(cls._probe_chain_length(code)):
            yield slot
            slot = (slot + step) & (CHARMAP_SLOT_COUNT - 1)

    @staticmethod
    def _slots_word(n):
        return f"{n} slot{'s' if n != 1 else ''}"

    def _no_slot_message(self, code, free_slots=None):
        if free_slots is None:
            free_slots = self.count_empty_charmap_slots()
        return (
            f"code U+{code:04X} cannot be placed: its probe chain "
            f"({self._slots_word(self._probe_chain_length(code))}) is completely full "
            f"(free slots in entire table: {free_slots} of {CHARMAP_SLOT_COUNT})"
        )

    def _slot_of(self, code):
        return (self._charmap_offsets[code] - self._charmap_start()) // CHARMAP_SLOT_SIZE

    def _entries_in_slot_order(self):
        """Charmap entries ordered by physical slot (stable rebuild order)."""
        return sorted(self.charmap.items(), key=lambda item: self._charmap_offsets[item[0]])

    def _find_charmap_slot(self, code, glyph_index):
        """Physical slot holding ``code -> glyph_index`` (searched along its probe chain)."""
        charmap_start = self._charmap_start()
        for slot in self._probe_slots(code):
            existing_code, existing_glyph_index = struct.unpack_from(
                "<HH", self.data, charmap_start + slot * CHARMAP_SLOT_SIZE
            )
            if existing_code == code and existing_glyph_index == glyph_index:
                return slot
        raise ValueError(f"Could not find charmap slot for U+{code:04X}")

    def _chain_dependents(self, removed_slots, excluded_codes):
        """Codes whose probe chain passes through one of ``removed_slots``
        *before* reaching their own slot.  Emptying such a slot (FFFF FFFF)
        would cut their chain, so a lookup that stops at the first empty slot
        could no longer find them."""
        removed_slots = set(removed_slots)
        dependents = []
        for code in self.charmap:
            if code in excluded_codes:
                continue
            own_slot = self._slot_of(code)
            for slot in self._probe_slots(code):
                if slot == own_slot:
                    break
                if slot in removed_slots:
                    dependents.append(code)
                    break
        return dependents

    def _rebuild_charmap(self, entries):
        """Rebuild the fixed table from ``(code, glyph_index)`` pairs.

        Nothing is modified unless every entry could be placed; otherwise a
        ValueError is raised and the font stays untouched.
        """
        entries = list(entries)
        rebuilt = bytearray(b"\xFF" * CHARMAP_SIZE)
        occupied = [False] * CHARMAP_SLOT_COUNT
        for code, mapped_glyph_index in entries:
            for slot in self._probe_slots(code):
                if not occupied[slot]:
                    struct.pack_into(
                        "<HH", rebuilt, slot * CHARMAP_SLOT_SIZE, code, mapped_glyph_index
                    )
                    occupied[slot] = True
                    break
            else:
                raise ValueError(
                    self._no_slot_message(code, CHARMAP_SLOT_COUNT - len(entries))
                )

        charmap_start = self._charmap_start()
        self.data[charmap_start:charmap_start + CHARMAP_SIZE] = rebuilt
        self.charmap = self._read_charmap()
        self._apply_charmap_to_glyphs()

    def _remove_charmap_codes(self, codes, glyph_index):
        """Remove ``codes`` (all mapped to ``glyph_index``) from the charmap.

        If emptying their slots would cut the probe chain of some other entry,
        the table is rebuilt without them instead of leaving holes.
        """
        codes = list(codes)
        slots = {code: self._find_charmap_slot(code, glyph_index) for code in codes}
        dependents = self._chain_dependents(slots.values(), set(codes))

        if dependents:
            removed = set(codes)
            remaining = [
                (code, idx) for code, idx in self._entries_in_slot_order()
                if code not in removed
            ]
            try:
                self._rebuild_charmap(remaining)
            except ValueError as e:
                names = ", ".join(f"U+{c:04X}" for c in sorted(dependents)[:5])
                raise ValueError(
                    f"removal would break probe chains of other characters "
                    f"({names}{'...' if len(dependents) > 5 else ''}), but rebuilding "
                    f"the table failed: {e}"
                ) from e
            return

        charmap_start = self._charmap_start()
        for code, slot in slots.items():
            struct.pack_into(
                "<HH", self.data, charmap_start + slot * CHARMAP_SLOT_SIZE, SENTINEL, SENTINEL
            )
            del self.charmap[code]
            self._charmap_offsets.pop(code, None)
        self._apply_charmap_to_glyphs()

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

        # Rebuild in physical-slot order, which gives a stable insertion order.
        entries = [
            (new_code if code == old_code else code, mapped_glyph_index)
            for code, mapped_glyph_index in self._entries_in_slot_order()
        ]
        self._rebuild_charmap(entries)

    def remove_glyph_character_alias(self, glyph_index, code):
        """Remove a character mapping from a glyph.

        Only allowed if the glyph has more than one character mapping.
        This removes the specific code->glyph_index mapping from the charmap.
        If the freed slot lies inside another entry's probe chain, the table
        is rebuilt so no lookup chain is cut.
        """
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if not (0 <= code < SENTINEL):
            raise ValueError("character must be a BMP code point other than U+FFFF")
        if self.charmap.get(code) != glyph_index:
            raise ValueError(
                f"U+{code:04X} is not assigned to glyph #{glyph_index}"
            )

        if len(self.codes_for_glyph(glyph_index)) <= 1:
            raise ValueError(
                f"Cannot remove the only character from glyph #{glyph_index}. "
                "Use 'Add Character Alias' to add another character first."
            )

        self._remove_charmap_codes([code], glyph_index)

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
        for slot in self._probe_slots(code):
            offset = charmap_start + slot * CHARMAP_SLOT_SIZE
            existing_code, existing_glyph_index = struct.unpack_from("<HH", self.data, offset)
            if existing_code == SENTINEL and existing_glyph_index == SENTINEL:
                struct.pack_into("<HH", self.data, offset, code, glyph_index)
                self.charmap[code] = glyph_index
                self._charmap_offsets[code] = offset
                self._apply_charmap_to_glyphs()
                return

        raise ValueError(self._no_slot_message(code))

    def clear_glyph(self, glyph_index):
        """Clear a glyph in place: wipe its charmap entries and its record.

        This does not physically delete anything - the glyph table is a
        fixed array indexed by position, so a glyph can't be removed without
        shifting every later glyph's index (and every charmap entry that
        points to one) - too invasive and risky for what is meant to be a
        simple "clear this slot" operation. Instead:

        - Every charmap slot currently mapped to this glyph is cleared to
          the same FFFF FFFF "empty slot" pattern used elsewhere for unused
          charmap entries (see remove_glyph_character_alias).
        - The glyph's own 16-byte record is zeroed out completely (all
          fields, including field1/field2/byte14/byte15 and the rectangle),
          leaving an empty/inert glyph at that index rather than removing
          the slot itself.

        glyph_count and every other glyph's index are left untouched, so
        nothing else in the file needs to be renumbered.
        """
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")

        # Clear every charmap entry pointing at this glyph, the same way
        # remove_glyph_character_alias clears a single one.
        codes_to_clear = self.codes_for_glyph(glyph_index)
        if codes_to_clear:
            self._remove_charmap_codes(codes_to_clear, glyph_index)

        # Zero out the glyph's own record (all 16 bytes: unknown_field,
        # field1, field2, the rectangle, byte14, byte15).
        off = GLYPH_TABLE_OFFSET + glyph_index * GLYPH_REC_SIZE
        self.data[off:off + 16] = b"\x00" * 16

        g = Glyph(glyph_index)
        self.glyphs[glyph_index] = g

        self._apply_charmap_to_glyphs()

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
        self.data[off:off + 16] = rec
        self.glyphs[i] = glyph

    def save(self, out_path=None):
        """Saves the entire file (with applied edits) to the target path.
        If out_path is None, overwrites the source file (self.path).

        The data goes to a temporary file in the same directory which then
        replaces the target atomically, so a failed write cannot leave a
        half-written (corrupt) file behind."""
        result = out_path or self.path
        target = os.path.realpath(result)          # write through symlinks
        fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(target),
                                        prefix=".conker_", suffix=".tmp")
        try:
            with os.fdopen(fd, "wb") as f:
                f.write(self.data)
            if os.path.exists(target):
                shutil.copymode(target, tmp_path)  # keep the original permissions
            os.replace(tmp_path, target)
        except BaseException:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        return result

    # ---------- High-level helper functions ----------

    def to_pixels(self, glyph):
        p = self.profile
        return glyph.to_pixels(p["X_DIV"], p["Y_DIV"], p["X_OFFSET"], p["Y_OFFSET"])

    def set_pixels(self, glyph, x0, y0, x1, y1):
        p = self.profile
        glyph.set_from_pixels(x0, y0, x1, y1, p["X_DIV"], p["Y_DIV"], p["X_OFFSET"], p["Y_OFFSET"])
