"""
conker_glyph_format.py - reader/patcher for the glyph table and character map of
Conker: Live & Reloaded CAFF font files (default.bin).

Field edits are applied in place to the in-memory file image; everything outside
the touched records stays byte-identical.

Deleting a glyph never changes the file layout: the records behind it move down
inside the glyph array, the glyph count drops and the freed bytes stay as a gap in
front of the charmap (which the font object reaches through a pointer, so it does
not have to follow the array directly).  Adding a glyph reuses such a gap.

Only when there is no gap can the array be grown (allow_growth=True): the charmap
and everything behind it move, and the counts, pointers, sizes and descriptor
entries that depend on that are rewritten the way the original fonts have them
(checked against all four shipped files).  Whatever changes the header (the
section sizes, the metadata offset) also needs the header checksum redone - the
game refuses a file whose checksum is stale; save() takes care of that and of the
charmap's probe limit, so a saved file is always consistent.
"""

import math
import os
import shutil
import struct
import tempfile

# The glyph array is a plain array of 18-byte records (see Glyph for the layout).
# Its base is what the font object's array pointer (at 0x4F0) targets.
GLYPH_TABLE_OFFSET = 0x508
GLYPH_REC_SIZE = 18
GLYPH_COUNT_HEADER_OFFSET = 0x4d4

# The glyph array is followed immediately by a fixed-size open addressing table
# of (uint16 Unicode code point, uint16 glyph index) pairs.
CHARMAP_SLOT_COUNT = 0x800
CHARMAP_SLOT_SIZE = 4
CHARMAP_SIZE = CHARMAP_SLOT_COUNT * CHARMAP_SLOT_SIZE

SENTINEL = 0xFFFF  # empty charmap slot / reserved value
MAX_GLYPHS = SENTINEL - 1   # glyph indices are uint16 in the charmap; 0xFFFF is reserved

# --- Container layout --------------------------------------------------------
CAFF_HEADER_SIZE = 0x180
CAFF_CHECKSUM_FIELD = 0x14               # checksum of the first 0x180 bytes
# Derived from, and checked against, all four shipped fonts.
# CAFF header (file offsets, little-endian uint32):
SECTION_DATA_OFFSET_FIELD = 0x38         # file offset of the .data section
SECTION_DATA_SIZE_FIELDS = (0x50, 0x64)  # .data size (stored twice)
SECTION_GPU_SIZE_FIELDS = (0x78, 0x8C)   # .gpu size = texture pixels (stored twice)
INFO_BLOCK_FIELD = 0x34                  # .data-relative start of the metadata block
# Font object inside .data (pointers are relative to the start of .data):
CHARMAP_PTR_FIELD = 0x4C4                # -> charmap; it does not have to follow the array
GLYPH_ARRAY_PTR_FIELD = 0x4F0            # -> glyph array
# The game limits a charmap lookup to this many probes.  In every shipped font it
# is exactly the longest probe sequence any entry needs (1 for the collision-free
# ConkerFont and FrontendTitle, 34 for ConkerFontJapanese), so an entry placed
# deeper than this is never found.  save() keeps it large enough.
CHARMAP_MAX_PROBES_FIELD = 0x4E4
# Behind the 2048 charmap slots: FFFFFFFF, zero padding up to a 32-byte boundary
# (relative to .data), the 'texture' resource (0x58 bytes), the metadata strings and
# a table of resource descriptors:  [1][0][E] | 010502000000 [T][0x58] |
# 010502000000 [0][gpu size] | ...   with E = size of the font resource (up to and
# including the FFFFFFFF) and T = offset of the texture resource.
CHARMAP_TRAILER_SIZE = 4
RESOURCE_ALIGNMENT = 32
TEXTURE_RESOURCE_SIZE = 0x58
DESCRIPTOR_HEADER = bytes.fromhex("010502000000")

# The font object also stores one Unicode code point here: U+25A1 (the white
# square) in all four shipped fonts - it is not the highest code (the Japanese fonts
# map kanji above it).  Its purpose is not known; it may be the character shown for
# unknown symbols, so the editor warns before the glyph for it is removed.
HEADER_CODE_FIELD = 0x4D8

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
    # The fixed offsets above only hold for files as shipped: adding or deleting
    # glyphs moves the metadata block. The font's source path inside it still names
    # the font as ".../<name>/...", so look for that (the slashes keep
    # "ConkerFont" apart from "ConkerFontJapanese").
    for profile_name in _PROFILE_SIGNATURES:
        if data.find(b"/" + profile_name.encode("ascii") + b"/") >= 0:
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


def caff_checksum(data):
    """The CAFF header checksum (stored at CAFF_CHECKSUM_FIELD): a nibble-shifting
    hash (PJW-style) over the first 0x180 bytes with the checksum field zeroed.
    The game refuses a file whose stored value does not match, so it must be
    redone after any change to the header (section sizes, metadata offset).
    Algorithm from the CAFF text tool; it reproduces the value of all four shipped
    fonts, and files with a stale value are exactly the ones the game rejects."""
    header = bytearray(bytes(data[:CAFF_HEADER_SIZE]).ljust(CAFF_HEADER_SIZE, b"\x00"))
    struct.pack_into("<I", header, CAFF_CHECKSUM_FIELD, 0)
    checksum = 0
    for byte in header:
        signed = byte - 256 if byte > 127 else byte        # the bytes are added as int8
        checksum = ((checksum << 4) + signed) & 0xFFFFFFFF
        top = checksum & 0xF0000000
        if top:
            checksum ^= top | (top >> 24)
    return checksum


# field1 field2 x0 x1 y0 y1 byte14 byte15 reserved(0xFFFF) unknown_field
_GLYPH_REC_FORMAT = "<HHHHHHBBHH"


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

    Record layout (18 bytes): field1, field2, x0, x1, y0, y1 (uint16 each),
    byte14, byte15 (uint8 each), 0xFFFF (reserved - constant in every shipped
    glyph), unknown_field (uint16).  unknown_field is the LAST two bytes of its
    own record.  Earlier versions of this tool read it from the two bytes in
    front of the record, i.e. every glyph showed its predecessor's value and the
    last glyph's value was mistaken for a table "sentinel".  Evidence for the
    current reading: the font object's array pointer targets the array start
    (0x508) and the array then ends exactly where the charmap begins; and the
    value tracks the glyph's own height (equal to the Glyph Height byte for two
    thirds of ConkerFont's glyphs; 14 for the 14px-high U+25A1 square that used
    to be the "sentinel").

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


class NoFreeSlotError(ValueError):
    """The glyph table has no free slot; adding a glyph would have to grow the file."""


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

        self._reparse()

    # ---------- Reading ----------

    def _reparse(self):
        """(Re)build glyphs, charmap and display characters from self.data."""
        self.glyph_count = struct.unpack_from("<I", self.data, GLYPH_COUNT_HEADER_OFFSET)[0]
        self.glyphs = self._read_glyphs()
        self.charmap = self._read_charmap()  # code -> glyph_index
        self._apply_charmap_to_glyphs()

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
            g = Glyph(i)
            (g.field1, g.field2, g.x0_raw, g.x1_raw, g.y0_raw, g.y1_raw,
             g.byte14, g.byte15, _reserved, g.unknown_field) = struct.unpack_from(
                _GLYPH_REC_FORMAT, self.data, off)
            g.is_special = max(
                g.x0_raw, g.x1_raw, g.y0_raw, g.y1_raw
            ) > SPECIAL_GLYPH_RAW_THRESHOLD
            glyphs.append(g)
        return glyphs

    def _read_charmap(self):
        """Read the fixed 2048-slot character-to-glyph hash table.

        The table follows the 18-byte glyph records directly.  Each little-endian slot is ``(Unicode code point,
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

    def _array_end(self):
        """File offset right behind the last glyph record."""
        return GLYPH_TABLE_OFFSET + self.glyph_count * GLYPH_REC_SIZE

    def _charmap_start(self):
        """Return the byte offset of slot zero in the fixed charmap.

        Normally this is the end of the glyph array.  After glyphs were deleted
        the charmap stays where it was and the font object's pointer says so; a
        pointer that is not a whole number of records behind the array (or leaves
        the file) is ignored and the charmap is assumed to follow the array.
        """
        array_end = self._array_end()
        start = self._u32(SECTION_DATA_OFFSET_FIELD) + self._u32(CHARMAP_PTR_FIELD)
        if (start >= array_end and (start - array_end) % GLYPH_REC_SIZE == 0
                and start + CHARMAP_SIZE <= len(self.data)):
            return start
        return array_end

    def free_glyph_slots(self):
        """How many glyph records fit into the gap in front of the charmap."""
        return (self._charmap_start() - self._array_end()) // GLYPH_REC_SIZE

    @property
    def checksum_ok(self):
        """True if the stored header checksum matches the header."""
        return self._u32(CAFF_CHECKSUM_FIELD) == caff_checksum(self.data)

    @property
    def header_code(self):
        """The code point stored in the font header (see HEADER_CODE_FIELD)."""
        return self._u32(HEADER_CODE_FIELD)

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

    def _probes_needed(self, code):
        """How many probes a lookup of ``code`` takes with the entry where it sits."""
        slot = self._slot_of(code)
        for probes, candidate in enumerate(self._probe_slots(code), 1):
            if candidate == slot:
                return probes
        raise ValueError(f"U+{code:04X} sits outside its own probe sequence")

    def _update_max_probes(self):
        """Raise the header's probe limit if some entry needs more probes than it
        allows (it is never lowered - a larger limit only costs a lookup nothing)."""
        needed = max((self._probes_needed(code) for code in self.charmap), default=1)
        if needed > self._u32(CHARMAP_MAX_PROBES_FIELD):
            struct.pack_into("<I", self.data, CHARMAP_MAX_PROBES_FIELD, needed)

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

        The glyph keeps its index (use delete_glyph to remove the record and
        renumber the following glyphs):

        - Every charmap slot currently mapped to this glyph is cleared to
          the same FFFF FFFF "empty slot" pattern used elsewhere for unused
          charmap entries (see remove_glyph_character_alias).
        - The glyph's own fields (metrics, rectangle, unknown_field) are zeroed,
          leaving an empty/inert glyph at that index.

        glyph_count and every other glyph's index are left untouched.
        """
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")

        codes_to_clear = self.codes_for_glyph(glyph_index)
        if codes_to_clear:
            self._remove_charmap_codes(codes_to_clear, glyph_index)

        self.write_glyph(Glyph(glyph_index))
        self._apply_charmap_to_glyphs()

    # ---------- Adding / deleting glyphs ----------

    def _u32(self, offset):
        return struct.unpack_from("<I", self.data, offset)[0]

    def _set_glyph_count(self, count):
        struct.pack_into("<I", self.data, GLYPH_COUNT_HEADER_OFFSET, count)

    def _plan_growth(self):
        """Validate the container layout and work out what growing the glyph array
        by one record changes (only used when there is no free slot).

        Everything behind the glyph array (charmap, padding, the 'texture'
        resource, metadata, and the whole .gpu section) moves, so these follow: the
        glyph count, the charmap pointer, both copies of the .data size, the
        metadata-block offset, the sizes/offsets in the resource descriptor table
        and the zero padding that keeps the texture resource 32-byte aligned.

        Raises ValueError for a layout that does not match the shipped fonts, so
        that nothing gets corrupted by guessing.
        """
        d = self.data

        def refuse(why):
            return ValueError(f"unsupported file layout, cannot grow the glyph table: {why}")

        data_off = self._u32(SECTION_DATA_OFFSET_FIELD)
        data_sizes = {self._u32(o) for o in SECTION_DATA_SIZE_FIELDS}
        gpu_sizes = {self._u32(o) for o in SECTION_GPU_SIZE_FIELDS}
        if len(data_sizes) != 1 or len(gpu_sizes) != 1:
            raise refuse("section size fields disagree")
        data_size, gpu_size = data_sizes.pop(), gpu_sizes.pop()
        if data_off + data_size + gpu_size != len(d):
            raise refuse("section sizes do not add up to the file size")

        charmap_start = self._charmap_start()
        if charmap_start != self._array_end():
            raise refuse("the charmap does not directly follow the glyph array")
        if self._u32(CHARMAP_PTR_FIELD) != charmap_start - data_off:
            raise refuse("charmap pointer does not point at the end of the glyph array")
        if self._u32(GLYPH_ARRAY_PTR_FIELD) != GLYPH_TABLE_OFFSET - data_off:
            raise refuse("glyph array pointer does not point at the array")

        slots_end = charmap_start + CHARMAP_SIZE
        block_end = slots_end + CHARMAP_TRAILER_SIZE
        if d[slots_end:block_end] != b"\xff" * CHARMAP_TRAILER_SIZE:
            raise refuse("unexpected bytes behind the charmap")
        res_start = data_off + self._align_up(block_end - data_off, RESOURCE_ALIGNMENT)
        data_end = data_off + data_size
        if res_start + TEXTURE_RESOURCE_SIZE > data_end:
            raise refuse("no resource data behind the charmap")
        if any(d[block_end:res_start]):
            raise refuse("padding behind the charmap is not zero")
        if d[res_start:res_start + 8] != b"texture\x00":
            raise refuse("the texture resource is not where it is expected")
        if self._u32(INFO_BLOCK_FIELD) != res_start - data_off + TEXTURE_RESOURCE_SIZE:
            raise refuse("metadata block does not follow the texture resource")

        # Resource descriptor table: [1][0][E] | hdr [T][0x58] | hdr [0][gpu size]
        e_pos = d.find(struct.pack("<I", block_end - data_off), res_start, data_end)
        if (e_pos < 8
                or d[e_pos - 8:e_pos] != struct.pack("<II", 1, 0)
                or d[e_pos + 4:e_pos + 10] != DESCRIPTOR_HEADER
                or struct.unpack_from("<II", d, e_pos + 10) != (res_start - data_off, TEXTURE_RESOURCE_SIZE)
                or d[e_pos + 18:e_pos + 24] != DESCRIPTOR_HEADER
                or struct.unpack_from("<II", d, e_pos + 24) != (0, gpu_size)):
            raise refuse("resource descriptor table not recognised")

        new_block_end = block_end + GLYPH_REC_SIZE
        new_res_start = data_off + self._align_up(new_block_end - data_off, RESOURCE_ALIGNMENT)
        return {
            "data_off": data_off, "data_size": data_size, "info": self._u32(INFO_BLOCK_FIELD),
            "block_end": block_end, "res_start": res_start, "refs": [e_pos, e_pos + 10],
            "new_block_end": new_block_end, "new_res_start": new_res_start,
        }

    def _grow_glyph_array(self):
        """Append one blank record behind the last glyph by moving everything
        after the array and rewriting the bookkeeping that depends on it."""
        plan = self._plan_growth()
        d = self.data
        data_off = plan["data_off"]
        array_end = self._array_end()

        blank = bytes(14) + b"\xff\xff" + bytes(2)   # reserved field is always FFFF
        new = d[:array_end] + blank + d[array_end:plan["block_end"]]
        new += bytes(plan["new_res_start"] - plan["new_block_end"])    # alignment padding
        new += d[plan["res_start"]:]
        shift = plan["new_res_start"] - plan["res_start"]

        def put(offset, value):
            struct.pack_into("<I", new, offset, value)

        put(GLYPH_COUNT_HEADER_OFFSET, self.glyph_count + 1)
        put(CHARMAP_PTR_FIELD, array_end + GLYPH_REC_SIZE - data_off)
        for field in SECTION_DATA_SIZE_FIELDS:
            put(field, plan["data_size"] + shift)
        put(INFO_BLOCK_FIELD, plan["info"] + shift)
        for pos, value in zip(plan["refs"], (plan["new_block_end"], plan["new_res_start"])):
            put(pos + shift, value - data_off)

        self.data = new
        self._reparse()

    @staticmethod
    def _align_up(value, alignment):
        return -(-value // alignment) * alignment

    def _restore(self, snapshot):
        self.data = bytearray(snapshot)
        self._reparse()

    def add_glyph(self, template_index=None, code=None, allow_growth=False):
        """Append a new glyph at the end of the glyph array and return its index.

        The new glyph is a copy of glyph ``template_index`` (rectangle and
        metrics - so it is visible and editable right away) or an empty glyph if
        that is None.  If ``code`` is given it is mapped to the new glyph.

        The record goes into the free gap left by earlier deletions, which keeps the
        file layout untouched.  Without a free slot NoFreeSlotError is raised unless
        ``allow_growth`` is true, which grows the file (see _grow_glyph_array).
        All-or-nothing: on any error the font is left exactly as it was.
        """
        if self.glyph_count >= MAX_GLYPHS:
            raise ValueError(f"the font already has the maximum of {MAX_GLYPHS} glyphs")
        if template_index is not None and not (0 <= template_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if code is not None:
            if not (0 <= code < SENTINEL):
                raise ValueError("character must be a BMP code point other than U+FFFF")
            if code in self.charmap:
                raise ValueError(f"U+{code:04X} is already assigned to glyph #{self.charmap[code]}")

        has_slot = self.free_glyph_slots() > 0
        if not has_slot:
            if not allow_growth:
                raise NoFreeSlotError(
                    "The glyph table has no free slot. Deleting a glyph frees one; "
                    "otherwise the file has to be grown."
                )
            self._plan_growth()                    # refuse unknown layouts up front

        new_index = self.glyph_count
        glyph = Glyph(new_index) if template_index is None else self.glyphs[template_index].clone()
        glyph.index = new_index
        glyph.char = ""                            # no character mapped yet
        snapshot = bytes(self.data)
        try:
            if has_slot:
                end = self._array_end()
                self.data[end:end + GLYPH_REC_SIZE] = bytes(14) + b"\xff\xff" + bytes(2)   # reserved = FFFF
                self._set_glyph_count(new_index + 1)
                self._reparse()
            else:
                self._grow_glyph_array()
            self.write_glyph(glyph)
            if code is not None:
                self.add_glyph_character_alias(new_index, code)
        except Exception:
            self._restore(snapshot)
            raise
        return new_index

    def delete_glyph(self, glyph_index):
        """Remove a glyph without touching the file layout.

        Its characters are unmapped, the glyphs behind it move down by one index
        (records and charmap entries), and the glyph count drops.  The freed bytes
        stay as a zeroed gap in front of the charmap, so the file keeps its size
        and every offset, pointer and size in it stays valid.
        All-or-nothing: on any error the font is left exactly as it was."""
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if self.glyph_count <= 1:
            raise ValueError("cannot delete the last remaining glyph")

        snapshot = bytes(self.data)
        try:
            codes = self.codes_for_glyph(glyph_index)
            if codes:
                self._remove_charmap_codes(codes, glyph_index)
            for code, mapped_index in self.charmap.items():
                if mapped_index > glyph_index:
                    struct.pack_into("<H", self.data, self._charmap_offsets[code] + 2, mapped_index - 1)

            start = GLYPH_TABLE_OFFSET + glyph_index * GLYPH_REC_SIZE
            end = self._array_end()
            self.data[start:end - GLYPH_REC_SIZE] = self.data[start + GLYPH_REC_SIZE:end]
            self.data[end - GLYPH_REC_SIZE:end] = bytes(GLYPH_REC_SIZE)
            self._set_glyph_count(self.glyph_count - 1)
            self._reparse()
        except Exception:
            self._restore(snapshot)
            raise

    # ---------- Writing ----------

    def write_glyph(self, glyph):
        """Writes modified Glyph back into self.data (in-memory, not saved to disk yet).
        The reserved 0xFFFF field of the record is left untouched."""
        i = glyph.index
        if not (0 <= i < len(self.glyphs)):
            raise ValueError(f"glyph index must be between 0 and {len(self.glyphs) - 1}")
        off = GLYPH_TABLE_OFFSET + i * GLYPH_REC_SIZE
        struct.pack_into(
            "<HHHHHHBB", self.data, off,
            glyph.field1, glyph.field2,
            glyph.x0_raw, glyph.x1_raw, glyph.y0_raw, glyph.y1_raw,
            glyph.byte14, glyph.byte15,
        )
        struct.pack_into("<H", self.data, off + 16, glyph.unknown_field)
        self.glyphs[i] = glyph

    def save(self, out_path=None):
        """Saves the entire file (with applied edits) to the target path.
        If out_path is None, overwrites the source file (self.path).

        The data goes to a temporary file in the same directory which then
        replaces the target atomically, so a failed write cannot leave a
        half-written (corrupt) file behind.  The charmap's probe limit and the header
        checksum are brought up to date first, so a saved file is always one the
        game accepts and finds all its characters in."""
        self._update_max_probes()
        struct.pack_into("<I", self.data, CAFF_CHECKSUM_FIELD, caff_checksum(self.data))
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
