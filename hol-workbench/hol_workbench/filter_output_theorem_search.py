"""Bounded retention for explicit HOL theorem-search results."""

from __future__ import annotations

import re
from collections.abc import Callable

SEARCH_RESULT_HEADER = "val it : (string * thm) list ="
TRUNCATION_NOTICE = "[theorem search results truncated; complete results remain in the raw log]"
SEARCH_RESULT_HEADER_RE = re.compile(
    r"^val (?:it|HOL_WORKBENCH_SIGNATURE_RESULT)\s*:\s*\(string\s*\*\s*thm\)\s+list\s*=\s*(.*)$"
)
SEARCH_RESULT_ITEM_RE = re.compile(r'^\[?\("([A-Za-z0-9_\']+)",')
DECISIVE_FAILURE_RE = re.compile(r"^(?:Fatal error|Exception|Error|Failure)\b")


class TheoremSearchCapture:
    """Retain a bounded theorem-search or reserved signature-result block."""

    def __init__(self, *, max_lines: int = 24, max_chars: int = 4000) -> None:
        self.max_lines = max_lines
        self.max_chars = max_chars
        self.active = False
        self.truncated = False
        self.lines = 0
        self.chars = 0
        self.items_seen = 0
        self.pending_close = False

    def consume(self, line: str) -> tuple[bool, str | None]:
        if self.pending_close:
            self.pending_close = False
            if not SEARCH_RESULT_ITEM_RE.match(line):
                self.active = False
        if not self.active:
            header = SEARCH_RESULT_HEADER_RE.match(line)
            if not header:
                return False, None
            self.active = True
            self.truncated = False
            self.lines = 0
            self.chars = 0
            self.items_seen = 0
            self.pending_close = False
            single_line_result = header.group(1)
        elif line.startswith("val ") or DECISIVE_FAILURE_RE.match(line):
            self.active = False
            return False, None
        else:
            single_line_result = ""

        item = SEARCH_RESULT_ITEM_RE.match(line)
        self.items_seen += bool(item)
        closes_result = line == "[]" or bool(single_line_result and single_line_result.endswith("]"))
        closes_result = closes_result or bool(self.items_seen and line.endswith(")]"))
        if self.truncated:
            self.active = line != "[]"
            self.pending_close = closes_result and self.active
            return True, None
        if self.lines >= self.max_lines or self.chars + len(line) > self.max_chars:
            self.truncated = True
            self.active = line != "[]"
            self.pending_close = closes_result and self.active
            return True, TRUNCATION_NOTICE

        self.lines += 1
        self.chars += len(line)
        self.active = line != "[]"
        self.pending_close = closes_result and self.active
        return True, line or None

    def handle(
        self, line: str, emit: Callable[[str], None], record_event: Callable[[dict], None] | None = None
    ) -> bool:
        handled, captured = self.consume(line)
        if captured:
            emit(captured)
            item = SEARCH_RESULT_ITEM_RE.match(line)
            if item and record_event:
                record_event({"kind": "theorem-search-result", "name": item.group(1), "line": line})
        return handled
