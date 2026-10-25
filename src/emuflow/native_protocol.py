"""Checked small-request serialization shared by native phase adapters."""
from pathlib import Path
import struct
import zlib


class CheckedRequestWriter:
    def __init__(self, path):
        self.stream = Path(path).open("wb")
        self.crc = 0

    def bytes(self, value):
        chunks = getattr(value, "iter_bytes", None)
        if chunks is not None:
            for block in chunks():
                self.stream.write(block)
                self.crc = zlib.crc32(block, self.crc)
            return
        with memoryview(value).cast("B") as view:
            self.stream.write(view)
            self.crc = zlib.crc32(view, self.crc)

    def put(self, layout, *values):
        self.bytes(struct.pack("<" + layout, *values))

    def text(self, value):
        encoded = value.encode("utf-8")
        self.put("I", len(encoded))
        self.bytes(encoded)

    def catalog(self, values):
        self.put("I", len(values))
        for value in values:
            self.text(value)

    def finish(self):
        self.stream.write(struct.pack("<I", self.crc))
        self.stream.close()

    def close(self):
        self.stream.close()

