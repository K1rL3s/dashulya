# ruff: file-ignore[ambiguous-unicode-character-string] -- Token fixtures intentionally cover multiple alphabets.

import pytest

from dashulya.memory._unicode61 import tokens


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("naïve CAFÉ cafe\u0301", ["naive", "cafe", "cafe"]),
        ("Straße STRASSE", ["straße", "strasse"]),
        ("snake_case JSON42 BB?", ["snake", "case", "json42", "bb"]),
        ("привет\u0301 ΙΣΟΣ ὈΔΥΣΣΕΎΣ", ["привет", "ισοσ", "ὀδυσσεύσ"]),
        ("İSTANBUL Æther Øresund Łódź", ["istanbul", "æther", "øresund", "łodz"]),
        ("\u0301start a\u036fb", ["start", "a", "b"]),
        ("a\u0378b", ["a\u0378b"]),
        ("project🤯 tools🤷 👩\u200d💻", ["project🤯", "tools🤷"]),
        ("edge\ud7ff edge\ue000 \U0010ffff", ["edge\ud7ff", "edge\ue000", "\U0010ffff"]),
        (" \n!?", []),
    ],
)
def test_unicode61_token_contract(text: str, expected: list[str]) -> None:
    # Preserve the selected Unicode 6.1 categories and folds independently of
    # newer Python Unicode tables, especially unassigned characters and marks.
    assert tokens(text) == expected
