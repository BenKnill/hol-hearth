"""Strict, bounded lexical contract for supported OCaml loader calls.

The HOL-loaded OCaml parser remains the execution authority.  This module only
recognizes a deliberately small loader-call grammar over exact source bytes.
It validates the complete lexical input before returning any occurrence, so a
late malformed construct can never leave an earlier partial dependency edge.
"""

from __future__ import annotations

import bisect
import hashlib
import unicodedata
from dataclasses import dataclass
from typing import Literal

SOURCE_LOADERS = frozenset({"needs", "loadt", "loads", "load", "#use"})
ARTIFACT_LOADERS = frozenset({"define_from_elf", "define_assert_from_elf"})
SUPPORTED_LOADERS = SOURCE_LOADERS | ARTIFACT_LOADERS

MAX_SOURCE_BYTES = 64 * 1024 * 1024
MAX_TOKENS = 2_000_000
MAX_NESTING = 4096
MAX_QUOTED_DELIMITER_CHARS = 4096

LoaderFamily = Literal["source", "artifact"]
LoaderOutcome = Literal["literal", "dynamic"]
ScanStatus = Literal["ok", "refused"]

_BLANKS = frozenset(" \t\r\n\f")
_OPERATOR_CHARS = frozenset("!$%&*+-./:<=>?@^|~#")
_OPEN_TO_CLOSE = {"LPAREN": "RPAREN", "LBRACK": "RBRACK", "LBRACE": "RBRACE"}
_CLOSE_KINDS = frozenset(_OPEN_TO_CLOSE.values())
_CONTROL_PREDECESSORS = frozenset(
    {
        "begin",
        "do",
        "else",
        "if",
        "in",
        "initializer",
        "match",
        "object",
        "struct",
        "then",
        "try",
        "when",
        "while",
        "with",
    }
)
_BOUNDARY_IDENTIFIERS = frozenset(
    {"and", "as", "do", "done", "downto", "else", "end", "exception", "in", "then", "to", "when", "with"}
)
_BOUNDARY_OPERATORS = frozenset({"|"})
_CALL_PREDECESSOR_OPERATORS = frozenset({"=", "->", "|", ":=", "<-", "&&", "||"})
_INCOMPLETE_ATOMIC_IDENTIFIERS = frozenset(
    {
        "_", "and", "as", "assert", "begin", "class", "constraint", "do", "done",
        "downto", "else", "end", "exception", "external", "for", "fun", "function",
        "functor", "if", "in", "include", "inherit", "initializer", "lazy", "let",
        "match", "method", "module", "mutable", "new", "nonrec", "object", "of", "open",
        "or", "private", "rec", "sig", "struct", "then", "to", "try", "type", "val",
        "virtual", "when", "while", "with",
    }
)


@dataclass(frozen=True)
class ByteSpan:
    """Half-open byte span into the exact source passed to the scanner."""

    start: int
    end: int

    def bytes_from(self, source: bytes) -> bytes:
        return source[self.start : self.end]


@dataclass(frozen=True)
class OcamlStringLiteral:
    """One decoded OCaml string plus its exact raw-token identity."""

    span: ByteSpan
    value_bytes: bytes
    raw_sha256: str
    quoted: bool

    def utf8_text(self) -> str | None:
        try:
            return self.value_bytes.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return None


@dataclass(frozen=True)
class LoaderOccurrence:
    """One supported loader name classified without interpreting HOL."""

    loader: str
    family: LoaderFamily
    outcome: LoaderOutcome
    reason: str | None
    loader_span: ByteSpan
    call_span: ByteSpan
    source_line: int
    path_literal: OcamlStringLiteral | None = None
    name_literal: OcamlStringLiteral | None = None

    @property
    def path(self) -> str | None:
        return self.path_literal.utf8_text() if self.path_literal is not None else None

    @property
    def name(self) -> str | None:
        return self.name_literal.utf8_text() if self.name_literal is not None else None


@dataclass(frozen=True)
class LoaderScanRefusal:
    kind: str
    reason: str
    span: ByteSpan
    source_line: int


@dataclass(frozen=True)
class LoaderScanResult:
    status: ScanStatus
    source_sha256: str
    size_bytes: int
    occurrences: tuple[LoaderOccurrence, ...]
    refusal: LoaderScanRefusal | None = None

    @property
    def literal_occurrences(self) -> tuple[LoaderOccurrence, ...]:
        return tuple(item for item in self.occurrences if item.outcome == "literal")

    @property
    def dynamic_occurrences(self) -> tuple[LoaderOccurrence, ...]:
        return tuple(item for item in self.occurrences if item.outcome == "dynamic")


@dataclass(frozen=True)
class _Token:
    kind: str
    value: str
    span: ByteSpan
    literal: OcamlStringLiteral | None = None


class _LexicalFailure(Exception):
    def __init__(self, kind: str, reason: str, start: int, end: int | None = None) -> None:
        super().__init__(reason)
        self.kind = kind
        self.reason = reason
        self.start = start
        self.end = start + 1 if end is None else end


def _identifier_start(char: str) -> bool:
    if char == "_":
        return True
    return unicodedata.category(char).startswith("L")


def _identifier_continue(char: str) -> bool:
    if char in {"_", "'"} or char.isdigit():
        return True
    return unicodedata.category(char)[0] in {"L", "M"}


def _line_number_for_offset(source: bytes, offset: int) -> int:
    line = 1
    index = 0
    end = min(max(offset, 0), len(source))
    while index < end:
        byte = source[index]
        if byte == 0x0D:
            if index + 1 < end and source[index + 1] == 0x0A:
                index += 1
            line += 1
        elif byte == 0x0A:
            line += 1
        index += 1
    return line


class _Lexer:
    def __init__(self, source: bytes, text: str) -> None:
        self.source = source
        self.text = text
        self.offsets = [0]
        for char in text:
            self.offsets.append(self.offsets[-1] + len(char.encode("utf-8")))
        self.line_starts = [0]
        index = 0
        while index < len(source):
            byte = source[index]
            if byte == 0x0D:
                if index + 1 < len(source) and source[index + 1] == 0x0A:
                    index += 1
                self.line_starts.append(index + 1)
            elif byte == 0x0A:
                self.line_starts.append(index + 1)
            index += 1

    def _span(self, start: int, end: int) -> ByteSpan:
        return ByteSpan(self.offsets[start], self.offsets[end])

    def line_for_byte(self, offset: int) -> int:
        return bisect.bisect_right(self.line_starts, offset)

    def _literal(self, start: int, end: int, value: bytes, *, quoted: bool) -> OcamlStringLiteral:
        span = self._span(start, end)
        return OcamlStringLiteral(
            span=span,
            value_bytes=value,
            raw_sha256=hashlib.sha256(span.bytes_from(self.source)).hexdigest(),
            quoted=quoted,
        )

    def _decode_escape(self, slash: int) -> tuple[bytes, int]:
        index = slash + 1
        if index >= len(self.text):
            raise _LexicalFailure("unterminated_string", "trailing backslash in string literal", slash, index)
        char = self.text[index]
        named = {
            "\\": b"\\",
            '"': b'"',
            "'": b"'",
            "n": b"\n",
            "t": b"\t",
            "b": b"\b",
            "r": b"\r",
            " ": b" ",
        }
        if char in named:
            return named[char], index + 1
        if char == "\n":
            index += 1
            while index < len(self.text) and self.text[index] in {" ", "\t"}:
                index += 1
            return b"", index
        if char == "\r":
            newline_end = index
            while newline_end < len(self.text) and self.text[newline_end] == "\r":
                newline_end += 1
            if newline_end < len(self.text) and self.text[newline_end] == "\n":
                index = newline_end + 1
                while index < len(self.text) and self.text[index] in {" ", "\t"}:
                    index += 1
                return b"", index
            return b"\\\r", index + 1
        if char.isascii() and char.isdigit():
            digits = self.text[index : index + 3]
            if len(digits) != 3 or not all(item.isascii() and item.isdigit() for item in digits):
                raise _LexicalFailure(
                    "invalid_escape",
                    "decimal OCaml string escape must contain exactly three digits",
                    slash,
                    min(len(self.text), index + 3),
                )
            value = int(digits, 10)
            if value > 255:
                raise _LexicalFailure(
                    "invalid_escape",
                    "decimal OCaml string escape exceeds one byte",
                    slash,
                    index + 3,
                )
            return bytes([value]), index + 3
        if char == "x":
            digits = self.text[index + 1 : index + 3]
            if len(digits) == 2 and all(item in "0123456789abcdefABCDEF" for item in digits):
                return bytes([int(digits, 16)]), index + 3
            return b"\\x", index + 1
        if char == "o":
            digits = self.text[index + 1 : index + 4]
            if (
                len(digits) != 3
                or digits[0] not in "0123"
                or any(item not in "01234567" for item in digits[1:])
            ):
                raise _LexicalFailure(
                    "invalid_escape",
                    "octal OCaml string escape must be in the range \\o000 through \\o377",
                    slash,
                    min(len(self.text), index + 4),
                )
            return bytes([int(digits, 8)]), index + 4
        if char == "u":
            if index + 1 >= len(self.text) or self.text[index + 1] != "{":
                raise _LexicalFailure(
                    "invalid_escape",
                    "Unicode OCaml string escape requires braces",
                    slash,
                    min(len(self.text), index + 2),
                )
            close = self.text.find("}", index + 2)
            if close < 0:
                raise _LexicalFailure(
                    "invalid_escape",
                    "unterminated Unicode OCaml string escape",
                    slash,
                    len(self.text),
                )
            digits = self.text[index + 2 : close]
            if not 1 <= len(digits) <= 6 or any(item not in "0123456789abcdefABCDEF" for item in digits):
                raise _LexicalFailure(
                    "invalid_escape",
                    "Unicode OCaml string escape requires one through six hex digits",
                    slash,
                    close + 1,
                )
            scalar = int(digits, 16)
            if scalar > 0x10FFFF or 0xD800 <= scalar <= 0xDFFF:
                raise _LexicalFailure(
                    "invalid_escape",
                    "Unicode OCaml string escape is not a scalar value",
                    slash,
                    close + 1,
                )
            return chr(scalar).encode("utf-8"), close + 1
        return b"\\" + char.encode("utf-8"), index + 1

    def _ordinary_string(self, start: int) -> tuple[OcamlStringLiteral, int]:
        value = bytearray()
        index = start + 1
        while index < len(self.text):
            char = self.text[index]
            if char == '"':
                end = index + 1
                return self._literal(start, end, bytes(value), quoted=False), end
            if char == "\\":
                decoded, index = self._decode_escape(index)
                value.extend(decoded)
                continue
            if char == "\r":
                if index + 1 < len(self.text) and self.text[index + 1] == "\n":
                    index += 1
                    value.append(0x0A)
                else:
                    value.append(0x0D)
                index += 1
                continue
            if char == "\n":
                value.append(0x0A)
                index += 1
                continue
            value.extend(char.encode("utf-8"))
            index += 1
        raise _LexicalFailure(
            "unterminated_string",
            "unterminated ordinary OCaml string literal",
            start,
            len(self.text),
        )

    def _quoted_opener(self, start: int) -> tuple[str, int] | None:
        index = start + 1
        while (
            index < len(self.text)
            and index - start - 1 < MAX_QUOTED_DELIMITER_CHARS
            and ("a" <= self.text[index] <= "z" or self.text[index] == "_")
        ):
            index += 1
        if (
            index - start - 1 == MAX_QUOTED_DELIMITER_CHARS
            and index < len(self.text)
            and ("a" <= self.text[index] <= "z" or self.text[index] == "_")
        ):
            raise _LexicalFailure(
                "quoted_delimiter_limit",
                "OCaml quoted-string delimiter exceeds the lexical bound",
                start,
                index,
            )
        if index < len(self.text) and self.text[index] == "|":
            return self.text[start + 1 : index], index + 1
        return None

    def _looks_like_invalid_quoted_opener(self, start: int) -> bool:
        pipe = self.text.find(
            "|",
            start + 1,
            min(len(self.text), start + MAX_QUOTED_DELIMITER_CHARS + 2),
        )
        if pipe < 0:
            return False
        candidate = self.text[start + 1 : pipe]
        if not candidate or any(char in "{}()[];:=\r\n\"'`" for char in candidate):
            return False
        return _identifier_start(candidate[0]) or (
            candidate[0].isascii() and candidate[0].isdigit()
        )

    def _quoted_string(self, start: int, delimiter: str, content_start: int) -> tuple[OcamlStringLiteral, int]:
        terminator = f"|{delimiter}}}"
        close = self.text.find(terminator, content_start)
        if close < 0:
            raise _LexicalFailure(
                "unterminated_quoted_string",
                "unterminated quoted OCaml string literal",
                start,
                len(self.text),
            )
        value = bytearray()
        index = content_start
        while index < close:
            char = self.text[index]
            if char == "\r":
                if index + 1 < close and self.text[index + 1] == "\n":
                    index += 1
                    value.append(0x0A)
                else:
                    value.append(0x0D)
            elif char == "\n":
                value.append(0x0A)
            else:
                value.extend(char.encode("utf-8"))
            index += 1
        end = close + len(terminator)
        return self._literal(start, end, bytes(value), quoted=True), end

    def _quoted_extension_end(self, start: int) -> int:
        prefix = 3 if self.text.startswith("{%%", start) else 2
        index = start + prefix
        if index >= len(self.text) or not _identifier_start(self.text[index]):
            raise _LexicalFailure(
                "malformed_quoted_extension",
                "quoted OCaml extension requires an extension identifier",
                start,
                min(len(self.text), index + 1),
            )
        while index < len(self.text):
            if _identifier_continue(self.text[index]):
                index += 1
                continue
            if self.text[index] == ".":
                index += 1
                if index >= len(self.text) or not _identifier_start(self.text[index]):
                    raise _LexicalFailure(
                        "malformed_quoted_extension",
                        "quoted OCaml extension has an incomplete dotted identifier",
                        start,
                        min(len(self.text), index + 1),
                    )
                continue
            break
        while index < len(self.text) and self.text[index] in _BLANKS:
            index += 1
        delimiter_start = index
        while (
            index < len(self.text)
            and index - delimiter_start < MAX_QUOTED_DELIMITER_CHARS
            and ("a" <= self.text[index] <= "z" or self.text[index] == "_")
        ):
            index += 1
        if (
            index - delimiter_start == MAX_QUOTED_DELIMITER_CHARS
            and index < len(self.text)
            and ("a" <= self.text[index] <= "z" or self.text[index] == "_")
        ):
            raise _LexicalFailure(
                "quoted_delimiter_limit",
                "OCaml quoted-extension delimiter exceeds the lexical bound",
                start,
                index,
            )
        if index >= len(self.text) or self.text[index] != "|":
            raise _LexicalFailure(
                "malformed_quoted_extension",
                "quoted OCaml extension requires a valid raw-string delimiter",
                start,
                min(len(self.text), index + 1),
            )
        delimiter = self.text[delimiter_start:index]
        terminator = f"|{delimiter}}}"
        close = self.text.find(terminator, index + 1)
        if close < 0:
            raise _LexicalFailure(
                "unterminated_quoted_extension",
                "unterminated quoted OCaml extension",
                start,
                len(self.text),
            )
        return close + len(terminator)

    def _comment_string_end(self, start: int) -> int:
        index = start + 1
        while index < len(self.text):
            if self.text[index] == "\\":
                index = min(len(self.text), index + 2)
                continue
            if self.text[index] == '"':
                return index + 1
            index += 1
        raise _LexicalFailure(
            "unterminated_comment_string",
            "unterminated string literal inside OCaml comment",
            start,
            len(self.text),
        )

    def _char_or_typevar(self, start: int) -> tuple[_Token, int]:
        if start + 1 >= len(self.text):
            raise _LexicalFailure("malformed_character", "dangling apostrophe", start, len(self.text))
        next_char = self.text[start + 1]
        if next_char == "\\":
            if start + 2 < len(self.text) and self.text[start + 2] == "u":
                raise _LexicalFailure(
                    "malformed_character",
                    "Unicode escapes are not supported in OCaml character literals",
                    start,
                    min(len(self.text), start + 4),
                )
            decoded, close = self._decode_escape(start + 1)
            if close >= len(self.text) or self.text[close] != "'" or len(decoded) != 1:
                raise _LexicalFailure(
                    "malformed_character",
                    "malformed OCaml character literal",
                    start,
                    min(len(self.text), close + 1),
                )
            end = close + 1
            return _Token("CHAR", "", self._span(start, end)), end
        if next_char != "'" and start + 2 < len(self.text) and self.text[start + 2] == "'":
            if len(next_char.encode("utf-8")) != 1:
                raise _LexicalFailure(
                    "malformed_character",
                    "direct OCaml character literal is not one byte",
                    start,
                    start + 3,
                )
            end = start + 3
            return _Token("CHAR", "", self._span(start, end)), end
        if _identifier_start(next_char):
            end = start + 2
            while end < len(self.text) and _identifier_continue(self.text[end]):
                end += 1
            return _Token("TYPEVAR", self.text[start:end], self._span(start, end)), end
        raise _LexicalFailure(
            "malformed_character",
            "malformed OCaml character literal or type variable",
            start,
            min(len(self.text), start + 2),
        )

    def _comment(self, start: int) -> int:
        depth = 1
        index = start + 2
        while index < len(self.text):
            if self.text[index] == "'":
                # Camlp5's comment lexer consumes ``'*)`` and ``'*`` before
                # reconsidering a comment terminator; a general apostrophe
                # also shields the following byte inside comments.
                if self.text.startswith("'*)", index):
                    index += 3
                else:
                    index = min(len(self.text), index + 2)
                continue
            if self.text.startswith("(*", index):
                depth += 1
                if depth > MAX_NESTING:
                    raise _LexicalFailure(
                        "nesting_limit",
                        "nested OCaml comment limit exceeded",
                        start,
                        index + 2,
                    )
                index += 2
                continue
            if self.text.startswith("*)", index):
                depth -= 1
                index += 2
                if depth == 0:
                    return index
                continue
            if self.text[index] == '"':
                index = self._comment_string_end(index)
                continue
            if self.text[index] == "{":
                if self.text.startswith("{%", index):
                    index = self._quoted_extension_end(index)
                    continue
                opener = self._quoted_opener(index)
                if opener is not None:
                    delimiter, content_start = opener
                    _literal, index = self._quoted_string(index, delimiter, content_start)
                    continue
            index += 1
        raise _LexicalFailure(
            "unterminated_comment",
            "unterminated nested OCaml comment",
            start,
            len(self.text),
        )

    def _hol_backquote(self, start: int) -> int:
        close = self.text.find("`", start + 1)
        if close < 0:
            raise _LexicalFailure(
                "unterminated_hol_backquote",
                "unterminated HOL backquote",
                start,
                len(self.text),
            )
        return close + 1

    def _line_directive_end(self, start: int) -> int | None:
        if start != 0 and self.text[start - 1] not in {"\r", "\n"}:
            return None
        index = start + 1
        while index < len(self.text) and self.text[index] in {" ", "\t"}:
            index += 1
        digit_start = index
        while index < len(self.text) and self.text[index].isascii() and self.text[index].isdigit():
            index += 1
        if index == digit_start:
            return None
        while index < len(self.text) and self.text[index] in {" ", "\t"}:
            index += 1
        if index >= len(self.text) or self.text[index] != '"':
            raise _LexicalFailure(
                "malformed_line_directive",
                "OCaml line directive requires a quoted source name",
                start,
                min(len(self.text), index + 1),
            )
        index += 1
        while index < len(self.text) and self.text[index] not in {'"', "\r", "\n"}:
            index += 1
        if index >= len(self.text) or self.text[index] != '"':
            raise _LexicalFailure(
                "malformed_line_directive",
                "OCaml line directive source name is unterminated",
                start,
                min(len(self.text), index + 1),
            )
        line_end = index + 1
        while line_end < len(self.text) and self.text[line_end] not in {"\r", "\n"}:
            line_end += 1
        if line_end >= len(self.text):
            return len(self.text)
        if self.text[line_end] == "\r" and line_end + 1 < len(self.text) and self.text[line_end + 1] == "\n":
            return line_end + 2
        return line_end + 1

    def tokenize(self) -> list[_Token]:
        tokens: list[_Token] = []
        delimiters: list[tuple[str, int]] = []
        index = 0

        def append(token: _Token) -> None:
            tokens.append(token)
            if len(tokens) > MAX_TOKENS:
                raise _LexicalFailure(
                    "token_limit",
                    "OCaml loader lexical token limit exceeded",
                    index,
                    min(len(self.text), index + 1),
                )

        while index < len(self.text):
            char = self.text[index]
            if char in _BLANKS:
                index += 1
                continue
            if char == "#":
                directive_end = self._line_directive_end(index)
                if directive_end is not None:
                    index = directive_end
                    continue
            if self.text.startswith("(*", index):
                index = self._comment(index)
                continue
            if char == '"':
                literal, end = self._ordinary_string(index)
                append(_Token("STRING", "", literal.span, literal))
                index = end
                continue
            if char == "{":
                if self.text.startswith("{%", index):
                    end = self._quoted_extension_end(index)
                    append(_Token("OPAQUE", "", self._span(index, end)))
                    index = end
                    continue
                opener = self._quoted_opener(index)
                if opener is not None:
                    delimiter, content_start = opener
                    literal, end = self._quoted_string(index, delimiter, content_start)
                    append(_Token("STRING", "", literal.span, literal))
                    index = end
                    continue
                if self._looks_like_invalid_quoted_opener(index):
                    raise _LexicalFailure(
                        "invalid_quoted_delimiter",
                        "OCaml quoted-string delimiters contain only lowercase ASCII letters and underscores",
                        index,
                        min(len(self.text), self.text.find("|", index + 1) + 1),
                    )
            if char == "'":
                token, index = self._char_or_typevar(index)
                append(token)
                continue
            if char == "`":
                end = self._hol_backquote(index)
                append(_Token("HOL", "", self._span(index, end)))
                index = end
                continue
            if self.text.startswith(";;", index):
                if delimiters:
                    raise _LexicalFailure(
                        "phrase_boundary_inside_delimiter",
                        "OCaml phrase boundary occurs inside an open delimiter",
                        index,
                        index + 2,
                    )
                append(_Token("SEMISEMI", ";;", self._span(index, index + 2)))
                index += 2
                continue
            if char == ";":
                append(_Token("SEMI", ";", self._span(index, index + 1)))
                index += 1
                continue
            if char == ",":
                append(_Token("COMMA", ",", self._span(index, index + 1)))
                index += 1
                continue
            delimiter_kinds = {
                "(": "LPAREN",
                ")": "RPAREN",
                "[": "LBRACK",
                "]": "RBRACK",
                "{": "LBRACE",
                "}": "RBRACE",
            }
            if char in delimiter_kinds:
                kind = delimiter_kinds[char]
                if kind in _OPEN_TO_CLOSE:
                    delimiters.append((_OPEN_TO_CLOSE[kind], index))
                    if len(delimiters) > MAX_NESTING:
                        raise _LexicalFailure(
                            "nesting_limit",
                            "OCaml delimiter nesting limit exceeded",
                            index,
                            index + 1,
                        )
                elif not delimiters or delimiters[-1][0] != kind:
                    raise _LexicalFailure(
                        "mismatched_delimiter",
                        f"mismatched OCaml delimiter {char}",
                        index,
                        index + 1,
                    )
                else:
                    delimiters.pop()
                append(_Token(kind, char, self._span(index, index + 1)))
                index += 1
                continue
            if char == "#" and self.text.startswith("#use", index):
                end = index + 4
                if end == len(self.text) or not _identifier_continue(self.text[end]):
                    append(_Token("LOADER", "#use", self._span(index, end)))
                    index = end
                    continue
            if char == "\\":
                raw_start = index
                index += 1
                if index >= len(self.text) or self.text[index] != "#":
                    raise _LexicalFailure(
                        "malformed_raw_identifier",
                        "HOL raw identifiers require a \\# prefix",
                        raw_start,
                        min(len(self.text), index + 1),
                    )
                index += 1
                if index >= len(self.text) or not _identifier_start(self.text[index]):
                    raise _LexicalFailure(
                        "illegal_character",
                        "backslash outside an OCaml literal must introduce a raw identifier",
                        raw_start,
                        min(len(self.text), index + 1),
                    )
                end = index + 1
                while end < len(self.text) and _identifier_continue(self.text[end]):
                    end += 1
                value = self.text[index:end]
                kind = "LOADER" if value in SUPPORTED_LOADERS else "RAW_IDENT"
                append(_Token(kind, value, self._span(raw_start, end)))
                index = end
                continue
            if _identifier_start(char):
                end = index + 1
                while end < len(self.text) and _identifier_continue(self.text[end]):
                    end += 1
                value = self.text[index:end]
                kind = "LOADER" if value in SUPPORTED_LOADERS else "IDENT"
                append(_Token(kind, value, self._span(index, end)))
                index = end
                continue
            unicode_operators = {"→": "->", "≤": "<=", "≥": ">="}
            if char in unicode_operators:
                append(_Token("OP", unicode_operators[char], self._span(index, index + 1)))
                index += 1
                continue
            if char in _OPERATOR_CHARS:
                end = index + 1
                while end < len(self.text) and self.text[end] in _OPERATOR_CHARS:
                    end += 1
                append(_Token("OP", self.text[index:end], self._span(index, end)))
                index = end
                continue
            if char.isascii() and char.isdigit():
                end = index + 1
                while end < len(self.text) and self.text[end].isascii() and self.text[end].isalnum():
                    end += 1
                append(_Token("ATOM", self.text[index:end], self._span(index, end)))
                index = end
                continue
            if ord(char) < 0x20 or ord(char) == 0x7F:
                raise _LexicalFailure(
                    "illegal_character",
                    "illegal control character in OCaml source",
                    index,
                    index + 1,
                )
            raise _LexicalFailure(
                "illegal_character",
                f"illegal character {char!r} in OCaml source",
                index,
                index + 1,
            )

        if delimiters:
            expected, start = delimiters[-1]
            raise _LexicalFailure(
                "unterminated_delimiter",
                f"unterminated OCaml delimiter; expected {expected}",
                start,
                len(self.text),
            )
        return tokens


def _direct_binding(tokens: list[_Token], index: int) -> bool:
    if index == 0:
        return False
    previous = tokens[index - 1]
    if previous.kind == "IDENT" and previous.value in {"and", "let"}:
        return True
    return (
        index >= 2
        and previous.kind == "IDENT"
        and previous.value == "rec"
        and tokens[index - 2].kind == "IDENT"
        and tokens[index - 2].value in {"and", "let"}
    )


def _qualified_or_hash_prefixed(tokens: list[_Token], index: int) -> bool:
    if index == 0:
        return False
    previous = tokens[index - 1]
    if previous.kind == "OP" and previous.value in {".", "#"}:
        return True
    return (
        index >= 2
        and previous.kind == "LPAREN"
        and tokens[index - 2].kind == "OP"
        and tokens[index - 2].value == "."
    )


def _call_position(tokens: list[_Token], index: int, loader: str) -> bool:
    if index == 0:
        return True
    previous = tokens[index - 1]
    if loader == "#use":
        return previous.kind == "SEMISEMI"
    if previous.kind in {"SEMISEMI", "SEMI", "COMMA", "LPAREN", "LBRACK", "LBRACE"}:
        return True
    if previous.kind == "OP" and previous.value in _CALL_PREDECESSOR_OPERATORS:
        return True
    return previous.kind == "IDENT" and previous.value in _CONTROL_PREDECESSORS


def _exact_string_argument(
    tokens: list[_Token],
    start: int,
) -> tuple[OcamlStringLiteral, int, int] | None:
    index = start
    opens = 0
    while index < len(tokens) and tokens[index].kind == "LPAREN":
        opens += 1
        index += 1
    if index >= len(tokens):
        return None
    token = tokens[index]
    if token.kind != "STRING" or token.literal is None:
        return None
    literal = token.literal
    end_byte = literal.span.end
    index += 1
    for _ in range(opens):
        if index >= len(tokens) or tokens[index].kind != "RPAREN":
            return None
        end_byte = tokens[index].span.end
        index += 1
    return literal, index, end_byte


def _call_boundary(tokens: list[_Token], index: int) -> bool:
    if index >= len(tokens):
        return True
    token = tokens[index]
    if token.kind in {"SEMISEMI", "SEMI", "COMMA", *_CLOSE_KINDS}:
        return True
    if token.kind == "IDENT" and token.value in _BOUNDARY_IDENTIFIERS:
        return True
    return token.kind == "OP" and token.value in _BOUNDARY_OPERATORS


def _starts_expression(tokens: list[_Token], index: int) -> bool:
    if index >= len(tokens):
        return False
    token = tokens[index]
    if token.kind == "IDENT" and token.value in _BOUNDARY_IDENTIFIERS:
        return False
    return token.kind in {
        "ATOM",
        "CHAR",
        "HOL",
        "IDENT",
        "LBRACE",
        "LBRACK",
        "LOADER",
        "OPAQUE",
        "LPAREN",
        "RAW_IDENT",
        "STRING",
        "TYPEVAR",
    }


def _single_expression_end(tokens: list[_Token], start: int) -> int | None:
    if not _starts_expression(tokens, start):
        return None
    token = tokens[start]
    if token.kind == "TYPEVAR" or (
        token.kind == "IDENT" and token.value in _INCOMPLETE_ATOMIC_IDENTIFIERS
    ):
        return None
    if token.kind not in _OPEN_TO_CLOSE:
        index = start + 1
        while (
            index + 1 < len(tokens)
            and tokens[index].kind == "OP"
            and tokens[index].value == "."
            and tokens[index + 1].kind in {"IDENT", "LOADER", "RAW_IDENT"}
        ):
            index += 2
        return index
    expected: list[str] = [_OPEN_TO_CLOSE[token.kind]]
    index = start + 1
    while index < len(tokens) and expected:
        current = tokens[index]
        if current.kind in _OPEN_TO_CLOSE:
            expected.append(_OPEN_TO_CLOSE[current.kind])
        elif current.kind in _CLOSE_KINDS:
            if current.kind != expected[-1]:
                return None
            expected.pop()
        index += 1
    return index if not expected else None


def _dynamic(
    lexer: _Lexer,
    token: _Token,
    *,
    family: LoaderFamily,
    reason: str,
    end: int | None = None,
    path_literal: OcamlStringLiteral | None = None,
    name_literal: OcamlStringLiteral | None = None,
) -> LoaderOccurrence:
    call_end = token.span.end if end is None else max(token.span.end, end)
    return LoaderOccurrence(
        loader=token.value,
        family=family,
        outcome="dynamic",
        reason=reason,
        loader_span=token.span,
        call_span=ByteSpan(token.span.start, call_end),
        source_line=lexer.line_for_byte(token.span.start),
        path_literal=path_literal,
        name_literal=name_literal,
    )


def _classify_tokens(lexer: _Lexer, tokens: list[_Token]) -> tuple[LoaderOccurrence, ...]:
    occurrences: list[LoaderOccurrence] = []
    tainted_loaders: set[str] = set()
    for index, token in enumerate(tokens):
        if token.kind != "LOADER":
            continue
        loader = token.value
        family: LoaderFamily = "source" if loader in SOURCE_LOADERS else "artifact"
        if _direct_binding(tokens, index):
            if family == "source":
                tainted_loaders.add(loader)
                occurrences.append(
                    _dynamic(lexer, token, family=family, reason="binding_position")
                )
            else:
                # The architecture decoder files define these helpers before
                # other source files call them.  The definition is not an
                # artifact reference, but a same-file use is shadowed.
                tainted_loaders.add(loader)
            continue
        if _qualified_or_hash_prefixed(tokens, index):
            continue
        if loader in tainted_loaders:
            occurrences.append(
                _dynamic(lexer, token, family=family, reason="shadowed_loader_binding")
            )
            continue
        if not _call_position(tokens, index, loader):
            occurrences.append(
                _dynamic(lexer, token, family=family, reason="not_in_call_position")
            )
            if family == "source":
                tainted_loaders.add(loader)
            else:
                tainted_loaders.add(loader)
            continue

        if family == "source":
            parsed = _exact_string_argument(tokens, index + 1)
            if parsed is None:
                occurrences.append(
                    _dynamic(lexer, token, family=family, reason="argument_is_not_an_exact_string_literal")
                )
                tainted_loaders.add(loader)
                continue
            path_literal, after_path, call_end = parsed
            if path_literal.utf8_text() is None or b"\x00" in path_literal.value_bytes:
                occurrences.append(
                    _dynamic(
                        lexer,
                        token,
                        family=family,
                        reason="path_literal_is_not_a_supported_filesystem_string",
                        end=call_end,
                        path_literal=path_literal,
                    )
                )
                tainted_loaders.add(loader)
                continue
            if not _call_boundary(tokens, after_path):
                occurrences.append(
                    _dynamic(
                        lexer,
                        token,
                        family=family,
                        reason="extra_expression_after_path_literal",
                        end=call_end,
                        path_literal=path_literal,
                    )
                )
                tainted_loaders.add(loader)
                continue
            occurrences.append(
                LoaderOccurrence(
                    loader=loader,
                    family=family,
                    outcome="literal",
                    reason=None,
                    loader_span=token.span,
                    call_span=ByteSpan(token.span.start, call_end),
                    source_line=lexer.line_for_byte(token.span.start),
                    path_literal=path_literal,
                )
            )
            continue

        name_parsed = _exact_string_argument(tokens, index + 1)
        if name_parsed is None:
            occurrences.append(
                _dynamic(lexer, token, family=family, reason="name_is_not_an_exact_string_literal")
            )
            continue
        name_literal, after_name, name_end = name_parsed
        path_parsed = _exact_string_argument(tokens, after_name)
        if path_parsed is None:
            occurrences.append(
                _dynamic(
                    lexer,
                    token,
                    family=family,
                    reason="path_is_not_an_exact_string_literal",
                    end=name_end,
                    name_literal=name_literal,
                )
            )
            continue
        path_literal, after_path, call_end = path_parsed
        if (
            name_literal.utf8_text() is None
            or path_literal.utf8_text() is None
            or b"\x00" in name_literal.value_bytes
            or b"\x00" in path_literal.value_bytes
        ):
            occurrences.append(
                _dynamic(
                    lexer,
                    token,
                    family=family,
                    reason="name_or_path_literal_is_not_a_supported_filesystem_string",
                    end=call_end,
                    path_literal=path_literal,
                    name_literal=name_literal,
                )
            )
            continue
        if loader == "define_from_elf":
            complete = _call_boundary(tokens, after_path)
            reason = "extra_expression_after_path_literal"
            complete_end = call_end
        else:
            expression_end = _single_expression_end(tokens, after_path)
            complete = expression_end is not None and _call_boundary(tokens, expression_end)
            reason = (
                "missing_instruction_expression_after_path_literal"
                if expression_end is None
                else "extra_expression_after_instruction_expression"
            )
            complete_end = (
                call_end
                if expression_end is None
                else tokens[expression_end - 1].span.end
            )
        if not complete:
            occurrences.append(
                _dynamic(
                    lexer,
                    token,
                    family=family,
                    reason=reason,
                    end=complete_end,
                    path_literal=path_literal,
                    name_literal=name_literal,
                )
            )
            continue
        occurrences.append(
            LoaderOccurrence(
                loader=loader,
                family=family,
                outcome="literal",
                reason=None,
                loader_span=token.span,
                call_span=ByteSpan(token.span.start, complete_end),
                source_line=lexer.line_for_byte(token.span.start),
                path_literal=path_literal,
                name_literal=name_literal,
            )
        )
    return tuple(occurrences)


def scan_ocaml_loaders(source: bytes) -> LoaderScanResult:
    """Return one atomic classification of supported loaders in ``source``."""

    source_sha256 = hashlib.sha256(source).hexdigest()
    if len(source) > MAX_SOURCE_BYTES:
        refusal = LoaderScanRefusal(
            kind="source_size_limit",
            reason=f"source exceeds the {MAX_SOURCE_BYTES}-byte loader scan bound",
            span=ByteSpan(0, len(source)),
            source_line=1,
        )
        return LoaderScanResult("refused", source_sha256, len(source), (), refusal)
    try:
        text = source.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        refusal = LoaderScanRefusal(
            kind="invalid_utf8",
            reason="source is not strict UTF-8",
            span=ByteSpan(exc.start, exc.end),
            source_line=_line_number_for_offset(source, exc.start),
        )
        return LoaderScanResult("refused", source_sha256, len(source), (), refusal)

    lexer = _Lexer(source, text)
    try:
        tokens = lexer.tokenize()
    except _LexicalFailure as exc:
        start = lexer.offsets[min(max(exc.start, 0), len(text))]
        end = lexer.offsets[min(max(exc.end, exc.start), len(text))]
        refusal = LoaderScanRefusal(
            kind=exc.kind,
            reason=exc.reason,
            span=ByteSpan(start, end),
            source_line=lexer.line_for_byte(start),
        )
        return LoaderScanResult("refused", source_sha256, len(source), (), refusal)

    return LoaderScanResult(
        status="ok",
        source_sha256=source_sha256,
        size_bytes=len(source),
        occurrences=_classify_tokens(lexer, tokens),
    )
