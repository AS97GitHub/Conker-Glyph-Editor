"""
conker_texture.py - Gray8 font texture helpers for Conker: Live & Reloaded CAFF
font files (default.bin).

The texture is the whole .gpu section at the end of the file: W*H consecutive
grayscale bytes, no header and no padding (W*H is a multiple of 32 for every
shipped font).  The section starts right behind the .data section; that offset is
not 32-byte aligned in the file and it moves whenever glyphs are added or deleted,
so it is always taken from the file's own header (never hardcoded).  The size of
the texture is read from the file too: FontTexture.replace() accepts an image of
another size (width a multiple of 64, up to 4096) and resizes the .gpu section; the
glyph rectangles are normalised, so the text keeps its on-screen size and just gets
sharper.  (The raw-buffer helpers below only do same-size, in-place writes.)

Two ways to use it:

1. On a .bin file directly:

       from conker_texture import FontTexture

       tex = FontTexture.detect("default.bin")     # or FontTexture(path, "ConkerFont")
       tex.read().save("texture.png")              # extract
       tex.replace(Image.open("new.png"))          # swap pixels (in memory)
       tex.save()                                  # write back (atomic)

2. On a raw buffer (this is what conker_glyph_format.py does):

       from conker_texture import read_gray8, write_gray8

       img = read_gray8(data, texture_offset, 256, 240)
       write_gray8(data, texture_offset, img, 256, 240)
"""

from PIL import Image


def check_layout(data, offset, width, height):
    """Validate that a Gray8 plane of ``width`` x ``height`` fits into ``data``
    at ``offset``. Raises ValueError otherwise; returns nothing."""
    if width <= 0 or height <= 0:
        raise ValueError("texture dimensions must be positive")
    if offset < 0 or offset + width * height > len(data):
        raise ValueError(
            f"texture would need bytes 0x{offset:X}..0x{offset + width * height:X}, "
            f"file has 0x{len(data):X}"
        )


def read_gray8(data, offset, width, height):
    """Extract the texture from a bytes-like object as a PIL image (mode 'L')."""
    check_layout(data, offset, width, height)
    return Image.frombytes("L", (width, height), bytes(data[offset:offset + width * height]))


def write_gray8(data, offset, image, width, height):
    """Write ``image`` into a mutable bytes-like object as Gray8 pixels at
    ``offset``. The image must be exactly ``width`` x ``height``; anything else
    (including a different aspect) is rejected - the section size is fixed."""
    check_layout(data, offset, width, height)
    if image.size != (width, height):
        raise ValueError(
            f"texture must be exactly {width}x{height} pixels, "
            f"got {image.size[0]}x{image.size[1]}"
        )
    data[offset:offset + width * height] = image.convert("L").tobytes()


class FontTexture:
    """The Gray8 texture embedded in one CAFF font file.

    A thin wrapper around ConkerFont, so the texture offset and size always come
    from the file's own header and stay correct for files whose glyph table was
    resized.  The file is read fully into memory on open; replace() changes the
    in-memory copy and save() writes it back (via a temporary file, so a failed
    write cannot leave a corrupt file behind).
    """

    def __init__(self, path, font_name=None):
        """Open ``path``. ``font_name`` (a key of FONT_PROFILES) is only needed when
        the font cannot be recognised from the file itself."""
        from conker_glyph_format import ConkerFont, FONT_PROFILES   # lazy: it imports this module
        self.font = ConkerFont(path, profile_name=font_name)
        if font_name is None and not self.font.profile_autodetected:
            raise ValueError(
                f"{path}: font not recognised; pass font_name "
                f"({', '.join(sorted(FONT_PROFILES))})"
            )

    @classmethod
    def detect(cls, path):
        """Open ``path``, recognising the font from the file itself."""
        return cls(path)

    @property
    def path(self):
        return self.font.path

    @property
    def font_name(self):
        return self.font.profile_name

    @property
    def offset(self):
        """File offset of the texture pixels (start of the .gpu section)."""
        return self.font.texture_offset

    @property
    def size(self):
        """(width, height) of the texture in pixels."""
        return self.font.texture_size

    def read(self):
        """Extract the texture as a PIL image (mode 'L', Gray8)."""
        return self.font.read_texture()

    def replace(self, image, keep_pixels=False):
        """Replace the texture in memory with ``image`` (converted to grayscale; any
        size the format allows). ``keep_pixels`` decides what happens to the glyph
        rectangles when the size changes - see ConkerFont.replace_texture. Call save()
        to write."""
        self.font.replace_texture(image, keep_pixels=keep_pixels)

    def extract_png(self, out_path):
        """Convenience: extract the texture straight to a PNG file."""
        self.read().save(out_path, format="PNG")
        return out_path

    def save(self, out_path=None):
        """Write the (possibly modified) file back. Without ``out_path`` the
        opened file is overwritten; permissions are preserved."""
        return self.font.save(out_path)


if __name__ == "__main__":
    # Tiny CLI: extract the texture of a font file to a PNG
    import sys
    if len(sys.argv) < 3:
        print(__doc__)
        print("usage: python conker_texture.py <default.bin> <out.png> [font_name]")
        raise SystemExit(2)
    tex = FontTexture(sys.argv[1], sys.argv[3] if len(sys.argv) > 3 else None)
    tex.extract_png(sys.argv[2])
    print(f"extracted {tex.font_name} texture {tex.size[0]}x{tex.size[1]} -> {sys.argv[2]}")
