"""Neutralize untrusted text before it reaches a terminal or the ledger.

Task, agent, team and model names come from files agentfin did not write. A
name containing ESC / OSC sequences can rewrite what a terminal shows (clear the
screen, retitle the window); bidi overrides (U+202E) can make `fdp.exe` look like
`exe.pdf`; zero-width characters make two different tags look identical in a
report. `clean` removes all of these.
"""

from __future__ import annotations

import re
import unicodedata

# C0/C1 controls (incl. ESC, BEL, DEL), bidi embeddings/overrides/isolates and
# marks, zero-width characters, BOM, line/paragraph separators.
_BAD = re.compile(
    "[\x00-\x1f\x7f-\x9f؜​-‏ -‮⁠-⁯﻿]"
)
_OSC = re.compile("\x1b\\][^\x07\x1b]*(?:\x07|\x1b\\\\)")  # OSC ... BEL / ST
_CSI = re.compile("\x1b\\[[0-?]*[ -/]*[@-~]")  # CSI ... final byte


def clean(value: object, limit: int = 256) -> str:
    """Return `value` as printable text: control, bidi and zero-width characters
    are dropped (whole ANSI escape sequences, not just their ESC byte), other
    whitespace runs collapse to one space (edges are kept), and the result is NFC-normalized and
    capped at `limit` characters."""
    text = value if type(value) is str else str(value)
    if text.isascii() and text.isprintable() and "  " not in text:
        return text[:limit]  # fast path: plain printable ASCII needs no work
    text = _OSC.sub("", text)
    text = _CSI.sub("", text)
    text = text.replace("\t", " ").replace("\n", " ").replace("\r", " ")
    text = _BAD.sub("", text)
    text = unicodedata.normalize("NFC", text)
    return re.sub(r"\s+", " ", text)[:limit]  # edges kept: export still neutralizes " =1+1"
