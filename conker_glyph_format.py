"""
conker_glyph_format.py - reader/patcher for the glyph table and character map of
Conker: Live & Reloaded CAFF font files (default.bin).

Field edits are applied in place to the in-memory file image; everything outside
the touched records stays byte-identical.

Adding or deleting a glyph resizes the glyph array, which moves everything behind
it (charmap, padding, texture resource, metadata, the .gpu texture pixels).  The
file is laid out again the way the original fonts have it, and the bookkeeping that
depends on it is rewritten: glyph count, charmap pointer, both copies of the .data
size, the metadata offset, the resource descriptors and the padding that keeps the
texture resource 32-byte aligned.  The header checksum and the charmap's probe
limit are brought up to date by save(), so a saved file is always one the game
accepts.  Files saved by an earlier version may carry a free gap in front of the
charmap; add_glyph reuses it and delete_glyph closes it.
"""

import math
import os
import shutil
import struct
import tempfile

from conker_texture import read_gray8, write_gray8

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
# Texture dimensions are stored in three places: the font object (two uint16 here),
# the 'texture' resource (the same pair at +0x2C) and the .gpu size (W*H, in the header
# twice and in the resource descriptor table).  Nothing else depends on them.
TEXTURE_DIMS_FIELD = 0x500
TEXTURE_RES_DIMS_OFFSET = 0x2C
# The textures are linear Gray8 (all four shipped heights are not powers of two, so
# they cannot be swizzled); linear Xbox textures need a pitch (= width) that is a
# multiple of 64 bytes.  4096 is the GPU's largest texture side.
TEXTURE_WIDTH_ALIGNMENT = 64
MAX_TEXTURE_SIDE = 4096
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
# Two more font-wide values sit in the font object (uint32 each):
#   0x4FC - the width of the space character, in the same on-screen units as a glyph's
#           advance.  CONFIRMED IN-GAME: +20 widens the space, -5 narrows it, while the
#           advance (byte14) of the U+0020 glyph itself has NO effect - the game takes
#           the width of the space only from this field.  In all four shipped fonts the
#           two are equal (9, 9, 9, 8).
#   0x4F8 - 37 / 47 / 35 / 45 in the shipped fonts, with no simple relation to the glyph
#           metrics.  Changing it showed NO effect in game so far (it may be a line
#           spacing, which a single line of text cannot show); purpose still unknown.
SPACE_WIDTH_FIELD = 0x4FC
SPACE_CODE = 0x20

# Glyph rectangle coordinates (x0/x1/y0/y1_raw) are uint16 values in a
# 0..16384 space: raw = (pixel - offset) * 16384 / texture_size (see
# UV_RANGE).  A real rectangle therefore never exceeds ~16384 (largest
# value seen in the four shipped fonts: 16376).
# "Special" glyphs (e.g. space) store a mirrored, negative-looking pair
# instead: x1 = 65536 - x0 and y1 = 65536 - y0 as uint16, e.g. (32, 65504,
# 34, 65502).  Every value found in them is >= 65496.
# A glyph is treated as "special" (no drawable rectangle) when any of its four
# coordinates is above this threshold.  The value is an empirically chosen cut
# inside the empty gap between 16384 and 65496; it is NOT known to be a
# constant taken from the game engine itself.
SPECIAL_GLYPH_RAW_THRESHOLD = 60000

# Rectangle coordinates are normalised: raw = (pixel - offset) * UV_RANGE / texture
# size.  The texture size is therefore NOT a property of the font profile - it is read
# from the file (a font may carry a larger texture than the original, see
# ConkerFont.replace_texture).  The profile only carries the pixel offsets.
UV_RANGE = 16384
FONT_PROFILES = {
    name: {"X_OFFSET": -0.5, "Y_OFFSET": -0.5}
    for name in ("ConkerFont", "ConkerFontJapanese", "FrontendTitle", "FrontendTitleJapanese")
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


class ConkerFont:
    """Loads default.bin fully into memory, provides access to glyphs, and allows
    writing changes back (in-place patch, leaving the rest of the file completely intact)."""

    def __init__(self, path, profile_name=None):
        self.path = path
        with open(path, "rb") as f:
            self.data = bytearray(f.read())

        if self.data[0:4] != b"CAFF":
            raise ValueError(f"{path}: does not look like a CAFF container (magic={self.data[0:4]!r})")
        if len(self.data) < GLYPH_TABLE_OFFSET:
            raise ValueError(f"{path}: truncated file (no font object in the header)")

        detected = None
        if profile_name is None:
            detected = detect_profile(self.data)
            profile_name = detected or "ConkerFont"  # fall back to the default profile
        if profile_name not in FONT_PROFILES:
            raise ValueError(f"unknown font profile {profile_name!r}")

        self.profile_name = profile_name
        self.profile = FONT_PROFILES[profile_name]
        self.profile_autodetected = detected is not None
        if not all(self.texture_size):
            raise ValueError(f"{path}: the texture size in the header is zero")

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
    def texture_offset(self):
        """File offset of the texture pixels (start of the .gpu section) - where
        the texture is extracted from / written back to."""
        return self._u32(SECTION_DATA_OFFSET_FIELD) + self._u32(SECTION_DATA_SIZE_FIELDS[0])

    @property
    def texture_size(self):
        """(width, height) of the texture in pixels, read from the font object.
        The texture is a single linear Gray8 plane: W*H bytes."""
        return struct.unpack_from("<HH", self.data, TEXTURE_DIMS_FIELD)

    @property
    def gpu_size(self):
        """Size in bytes of the .gpu section (the texture pixels), from the header."""
        return self._u32(SECTION_GPU_SIZE_FIELDS[0])

    def _check_texture_layout(self):
        """Refuse to touch the texture if the places that describe its size
        disagree with each other (a file this tool does not understand)."""
        w, h = self.texture_size
        if self.gpu_size != w * h:
            raise ValueError(
                f"the .gpu section is {self.gpu_size} bytes, but the font header says the "
                f"texture is {w}x{h} ({w * h} bytes)"
            )
        if self.gpu_size != self._u32(SECTION_GPU_SIZE_FIELDS[1]):
            raise ValueError("the two .gpu size fields disagree")
        offset = self.texture_offset
        if offset + w * h > len(self.data):
            raise ValueError(
                f"truncated file: texture would need bytes 0x{offset:X}..0x{offset + w * h:X}"
            )

    def read_texture(self):
        """Extract the texture as a PIL image (mode 'L', Gray8).

        The pixels are the whole .gpu section: W*H consecutive grayscale bytes,
        no header, no padding.  The section starts right behind .data
        (texture_offset), which is not 32-byte aligned in the file.
        Implemented in conker_texture.read_gray8()."""
        self._check_texture_layout()
        return read_gray8(self.data, self.texture_offset, *self.texture_size)

    @staticmethod
    def check_texture_size(width, height):
        """Raise ValueError unless a texture of this size is possible (see
        replace_texture)."""
        if width % TEXTURE_WIDTH_ALIGNMENT or not (0 < width <= MAX_TEXTURE_SIDE
                                                   and 0 < height <= MAX_TEXTURE_SIDE):
            raise ValueError(
                f"a texture of {width}x{height} is not possible: the width must be a multiple of "
                f"{TEXTURE_WIDTH_ALIGNMENT} and both sides at most {MAX_TEXTURE_SIDE}"
            )

    def replace_texture(self, image, keep_pixels=False):
        """Replace the texture with ``image`` in memory (anything that is not
        grayscale is converted); call save() to write the file.

        An image of the current size just replaces the pixels.  Any other size
        resizes the .gpu section (it is the last part of the file, so nothing else
        moves) and updates the size in the font object, the texture resource, the
        header and the descriptor table.  Larger textures, also with other
        proportions, are accepted by the game (confirmed in game).

        What happens to the glyph rectangles is up to ``keep_pixels``: they are
        normalised (raw = pixel * 16384 / texture size), so
        - False: they stay as they are and scale with the texture - right for an
          enlarged version of the same atlas;
        - True: they are recomputed so they cover the same pixels of the picture -
          right when the glyphs keep their size and place and the picture just has
          more room (raises ValueError if some rectangle would fall outside a smaller
          texture).
        Glyph Width/Height, bearings and advance are on-screen units and are never
        touched, so text keeps its size either way.

        Width must be a multiple of TEXTURE_WIDTH_ALIGNMENT, both sides at most
        MAX_TEXTURE_SIDE.  The header checksum covers the size fields; save()
        redoes it."""
        self._check_texture_layout()
        w, h = image.size
        if (w, h) == self.texture_size:
            write_gray8(self.data, self.texture_offset, image, w, h)
            return
        self.check_texture_size(w, h)
        plan = self._plan_layout()                 # refuses a layout it does not know
        dims_in_resource = plan["res_start"] + TEXTURE_RES_DIMS_OFFSET
        old_w, old_h = self.texture_size
        if struct.unpack_from("<HH", self.data, dims_in_resource) != (old_w, old_h):
            raise ValueError("unsupported file layout: the texture resource does not hold the texture size")

        data_end = plan["data_off"] + plan["data_size"]
        new = self.data[:data_end] + image.convert("L").tobytes()      # .gpu is the last section
        for field in SECTION_GPU_SIZE_FIELDS:
            struct.pack_into("<I", new, field, w * h)
        struct.pack_into("<I", new, plan["e_pos"] + 28, w * h)         # descriptor [0][gpu size]
        struct.pack_into("<HH", new, dims_in_resource, w, h)
        struct.pack_into("<HH", new, TEXTURE_DIMS_FIELD, w, h)
        if keep_pixels:
            self._rescale_rectangles(new, (old_w, old_h), (w, h))
        self.data = new
        if keep_pixels:
            self._reparse()

    def _rescale_rectangles(self, buf, old_size, new_size):
        """Rewrite the rectangle of every drawable glyph in ``buf`` so it covers the
        same pixels in a texture of ``new_size``: raw' = raw * old / new per axis (the
        half-pixel offset cancels out).  Raises ValueError, changing nothing, if a
        rectangle would lie outside the new texture."""
        (ow, oh), (nw, nh) = old_size, new_size
        edits, outside = [], 0
        for g in self.glyphs:
            if g.is_special:
                continue
            x0, x1 = (round(v * ow / nw) for v in (g.x0_raw, g.x1_raw))
            y0, y1 = (round(v * oh / nh) for v in (g.y0_raw, g.y1_raw))
            # pixel = raw * size / UV_RANGE + offset; the editor allows up to the texture size
            if (max(x0, x1) > UV_RANGE * (1 + 0.5 / nw) + 1
                    or max(y0, y1) > UV_RANGE * (1 + 0.5 / nh) + 1):
                outside += 1
            edits.append((g.index, x0, x1, y0, y1))
        if outside:
            raise ValueError(
                f"{outside} glyph rectangle(s) would lie outside the new {nw}x{nh} texture - "
                "the texture is smaller than the area they cover"
            )
        for index, x0, x1, y0, y1 in edits:
            struct.pack_into("<HHHH", buf, GLYPH_TABLE_OFFSET + index * GLYPH_REC_SIZE + 4, x0, x1, y0, y1)

    @property
    def checksum_ok(self):
        """True if the stored header checksum matches the header."""
        return self._u32(CAFF_CHECKSUM_FIELD) == caff_checksum(self.data)

    @property
    def space_width(self):
        """Width of the space character (the font-wide value, see SPACE_WIDTH_FIELD)."""
        return self._u32(SPACE_WIDTH_FIELD)

    def set_space_width(self, width):
        """Set the width of the space character (an integer from 0 to 255, like an
        advance).  Only the font-wide field is written; the space glyph's own advance is
        ignored by the game (see SPACE_WIDTH_FIELD)."""
        if not (isinstance(width, int) and 0 <= width <= 255):
            raise ValueError("Space Width must be an integer between 0 and 255")
        struct.pack_into("<I", self.data, SPACE_WIDTH_FIELD, width)

    def is_space_glyph(self, glyph_index):
        """True for the glyph U+0020 maps to - the one whose own advance the game ignores."""
        return self.charmap.get(SPACE_CODE) == glyph_index

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

    # ---------- Adding / deleting glyphs ----------

    def _u32(self, offset):
        return struct.unpack_from("<I", self.data, offset)[0]

    def _set_glyph_count(self, count):
        struct.pack_into("<I", self.data, GLYPH_COUNT_HEADER_OFFSET, count)

    @staticmethod
    def _align_up(value, alignment):
        return -(-value // alignment) * alignment

    def _plan_layout(self):
        """Validate the container layout (as found in all four shipped fonts) and
        collect what a resize of the glyph array has to move and rewrite.

        Raises ValueError for a layout that does not match, so that nothing gets
        corrupted by guessing.
        """
        d = self.data

        def refuse(why):
            return ValueError(f"unsupported file layout, cannot resize the glyph table: {why}")

        data_off = self._u32(SECTION_DATA_OFFSET_FIELD)
        data_sizes = {self._u32(o) for o in SECTION_DATA_SIZE_FIELDS}
        gpu_sizes = {self._u32(o) for o in SECTION_GPU_SIZE_FIELDS}
        if len(data_sizes) != 1 or len(gpu_sizes) != 1:
            raise refuse("section size fields disagree")
        data_size, gpu_size = data_sizes.pop(), gpu_sizes.pop()
        if data_off + data_size + gpu_size != len(d):
            raise refuse("section sizes do not add up to the file size")

        charmap_start = self._charmap_start()
        if self._u32(CHARMAP_PTR_FIELD) != charmap_start - data_off:
            raise refuse("charmap pointer is not consistent with the glyph array")
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

        return {
            "data_off": data_off, "data_size": data_size, "info": self._u32(INFO_BLOCK_FIELD),
            "charmap_start": charmap_start, "block_end": block_end, "res_start": res_start,
            "e_pos": e_pos,
        }

    def _relayout(self, array):
        """Replace the glyph array by ``array`` (the bytes of all records) and lay
        out everything behind it again: the charmap follows the array directly, the
        texture resource stays 32-byte aligned (the padding grows or shrinks), the
        rest of the file moves along, and the sizes/offsets that depend on it are
        rewritten.  The charmap must already use the new glyph numbering."""
        plan = self._plan_layout()
        d = self.data
        data_off = plan["data_off"]
        count = len(array) // GLYPH_REC_SIZE

        block = d[plan["charmap_start"]:plan["block_end"]]      # charmap slots + FFFFFFFF
        new_block_end = GLYPH_TABLE_OFFSET + len(array) + len(block)
        new_res_start = data_off + self._align_up(new_block_end - data_off, RESOURCE_ALIGNMENT)
        shift = new_res_start - plan["res_start"]

        new = d[:GLYPH_TABLE_OFFSET] + array + block
        new += bytes(new_res_start - new_block_end)             # alignment padding
        new += d[plan["res_start"]:]

        def put(offset, value):
            struct.pack_into("<I", new, offset, value)

        put(GLYPH_COUNT_HEADER_OFFSET, count)
        put(CHARMAP_PTR_FIELD, GLYPH_TABLE_OFFSET + len(array) - data_off)
        for field in SECTION_DATA_SIZE_FIELDS:
            put(field, plan["data_size"] + shift)
        put(INFO_BLOCK_FIELD, plan["info"] + shift)
        put(plan["e_pos"] + shift, new_block_end - data_off)          # font resource size E
        put(plan["e_pos"] + shift + 10, new_res_start - data_off)     # texture resource offset T

        self.data = new
        self._reparse()

    def _restore(self, snapshot):
        self.data = bytearray(snapshot)
        self._reparse()

    def add_glyph(self, template_index=None, code=None):
        """Append a new glyph at the end of the glyph array and return its index.

        The new glyph is a copy of glyph ``template_index`` (rectangle and
        metrics - so it is visible and editable right away) or an empty glyph if
        that is None.  If ``code`` is given it is mapped to the new glyph.  The file
        grows as needed.  All-or-nothing: on any error the font is left exactly as
        it was.
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
        has_gap = self.free_glyph_slots() > 0
        if not has_gap:
            self._plan_layout()                    # refuse unknown layouts up front

        new_index = self.glyph_count
        glyph = Glyph(new_index) if template_index is None else self.glyphs[template_index].clone()
        glyph.index = new_index
        glyph.char = ""                            # no character mapped yet
        blank = bytes(14) + b"\xff\xff" + bytes(2)  # reserved field is always FFFF
        snapshot = bytes(self.data)
        try:
            end = self._array_end()
            if has_gap:                            # gap left by an earlier version
                self.data[end:end + GLYPH_REC_SIZE] = blank
                self._set_glyph_count(new_index + 1)
                self._reparse()
            else:
                self._relayout(bytes(self.data[GLYPH_TABLE_OFFSET:end]) + blank)
            self.write_glyph(glyph)
            if code is not None:
                self.add_glyph_character_alias(new_index, code)
        except Exception:
            self._restore(snapshot)
            raise
        return new_index

    def delete_glyph(self, glyph_index):
        """Remove a glyph: its characters are unmapped, the record leaves the
        array, every later glyph moves down by one index (records and charmap
        entries), and everything behind the array moves up - the file shrinks.
        All-or-nothing: on any error the font is left exactly as it was."""
        if not (0 <= glyph_index < self.glyph_count):
            raise ValueError(f"glyph index must be between 0 and {self.glyph_count - 1}")
        if self.glyph_count <= 1:
            raise ValueError("cannot delete the last remaining glyph")
        self._plan_layout()                        # refuse unknown layouts up front

        snapshot = bytes(self.data)
        try:
            codes = self.codes_for_glyph(glyph_index)
            if codes:
                self._remove_charmap_codes(codes, glyph_index)
            for code, mapped_index in self.charmap.items():
                if mapped_index > glyph_index:
                    struct.pack_into("<H", self.data, self._charmap_offsets[code] + 2, mapped_index - 1)
            array = bytes(self.data[GLYPH_TABLE_OFFSET:self._array_end()])
            cut = glyph_index * GLYPH_REC_SIZE
            self._relayout(array[:cut] + array[cut + GLYPH_REC_SIZE:])
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

    def _pixel_scale(self):
        """(x_div, y_div, x_off, y_off) for the texture the file actually has."""
        w, h = self.texture_size
        p = self.profile
        return UV_RANGE / w, UV_RANGE / h, p["X_OFFSET"], p["Y_OFFSET"]

    def to_pixels(self, glyph):
        return glyph.to_pixels(*self._pixel_scale())

    def set_pixels(self, glyph, x0, y0, x1, y1):
        glyph.set_from_pixels(x0, y0, x1, y1, *self._pixel_scale())
