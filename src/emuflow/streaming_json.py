"""Small dependency-free streaming reader for object-oriented JSON.

The production Yosys interchange is one large object whose useful leaves are
independent port/cell/net records.  ``json.load`` needlessly materializes the
entire document.  This reader keeps a bounded text window, lets callers walk
object keys, and delegates each leaf decode to CPython's C JSON scanner.
"""

from __future__ import annotations

import json
from typing import Any, Iterator, TextIO


class StreamingJSONObjectReader:
    def __init__(
        self,
        stream: TextIO,
        *,
        chunk_size: int = 1 << 20,
        max_leaf_chars: int = 64 << 20,
    ):
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if max_leaf_chars <= 0:
            raise ValueError("max_leaf_chars must be positive")
        self._stream = stream
        self._chunk_size = chunk_size
        self._max_leaf_chars = max_leaf_chars
        self._buffer = ""
        self._position = 0
        self._eof = False
        self._decoder = json.JSONDecoder()

    def _compact(self) -> None:
        if self._position and (
            self._position >= self._chunk_size
            or self._position == len(self._buffer)
        ):
            self._buffer = self._buffer[self._position :]
            self._position = 0

    def _fill(self) -> bool:
        if self._eof:
            return False
        self._compact()
        block = self._stream.read(self._chunk_size)
        if block == "":
            self._eof = True
            return False
        self._buffer += block
        return True

    def _peek(self) -> str:
        while self._position >= len(self._buffer):
            if not self._fill():
                return ""
        return self._buffer[self._position]

    def _take(self) -> str:
        value = self._peek()
        if value:
            self._position += 1
        return value

    def skip_whitespace(self) -> None:
        while True:
            character = self._peek()
            if character and character.isspace():
                self._position += 1
                continue
            return

    def expect(self, token: str) -> None:
        self.skip_whitespace()
        found = self._take()
        if found != token:
            raise ValueError(
                f"malformed JSON stream: expected {token!r}, got {found!r}"
            )

    def read_value(self) -> Any:
        self.skip_whitespace()
        start = self._position
        while True:
            try:
                value, end = self._decoder.raw_decode(self._buffer, start)
            except json.JSONDecodeError as error:
                if len(self._buffer) - start > self._max_leaf_chars:
                    raise ValueError(
                        "JSON leaf record exceeds the bounded streaming limit"
                    ) from error
                if self._fill():
                    # Compaction may move the start offset to zero.
                    start = self._position
                    continue
                raise ValueError("malformed or truncated JSON value") from error
            if end - start > self._max_leaf_chars:
                raise ValueError(
                    "JSON leaf record exceeds the bounded streaming limit"
                )
            self._position = end
            return value

    def skip_value(self) -> None:
        """Skip one value without allocating it, including a giant object."""

        self.skip_whitespace()
        first = self._peek()
        if first == "":
            raise ValueError("truncated JSON value")
        if first not in "[{":
            self.read_value()
            return
        opening = self._take()
        closing = "}" if opening == "{" else "]"
        stack = [closing]
        in_string = False
        escaped = False
        while stack:
            character = self._take()
            if character == "":
                raise ValueError("truncated JSON container")
            if in_string:
                if escaped:
                    escaped = False
                elif character == "\\":
                    escaped = True
                elif character == '"':
                    in_string = False
                continue
            if character == '"':
                in_string = True
            elif character == "{":
                stack.append("}")
            elif character == "[":
                stack.append("]")
            elif character in "}]":
                if character != stack[-1]:
                    raise ValueError("mismatched JSON container")
                stack.pop()

    def object_keys(self) -> Iterator[str]:
        """Yield keys while the caller consumes each corresponding value."""

        self.expect("{")
        first = True
        while True:
            self.skip_whitespace()
            if self._peek() == "}":
                self._take()
                return
            if not first:
                self.expect(",")
            key = self.read_value()
            if not isinstance(key, str):
                raise ValueError("JSON object key is not a string")
            self.expect(":")
            yield key
            first = False

    def array_values(self) -> Iterator[Any]:
        """Read one bounded array member at a time without retaining siblings."""
        self.expect("[")
        first = True
        while True:
            self.skip_whitespace()
            if self._peek() == "]":
                self._take()
                return
            if not first:
                self.expect(",")
            yield self.read_value()
            first = False

    def finish(self) -> None:
        self.skip_whitespace()
        if self._peek() != "":
            raise ValueError("trailing data after JSON document")
