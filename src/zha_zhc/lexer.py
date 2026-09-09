"""Small non-executing lexer for the declarative TypeScript subset."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    offset: int
    line: int
    column: int


def tokenize(text: str) -> list[Token]:
    tokens: list[Token] = []
    i = 0
    line = 1
    column = 1
    length = len(text)

    def advance(value: str) -> None:
        nonlocal line, column
        newlines = value.count("\n")
        if newlines:
            line += newlines
            column = len(value.rsplit("\n", 1)[1]) + 1
        else:
            column += len(value)

    while i < length:
        char = text[i]
        if char.isspace():
            advance(char)
            i += 1
            continue
        if text.startswith("//", i):
            end = text.find("\n", i)
            end = length if end < 0 else end
            advance(text[i:end])
            i = end
            continue
        if text.startswith("/*", i):
            end = text.find("*/", i + 2)
            end = length if end < 0 else end + 2
            advance(text[i:end])
            i = end
            continue
        if char in "'\"`":
            quote = char
            start = i
            start_line, start_column = line, column
            i += 1
            escaped = False
            while i < length:
                current = text[i]
                if not escaped and current == quote:
                    i += 1
                    break
                escaped = current == "\\" and not escaped
                if current != "\\":
                    escaped = False
                i += 1
            value = text[start:i]
            advance(value)
            tokens.append(Token("string", value, start, start_line, start_column))
            continue
        if char.isdigit() or (char == "." and i + 1 < length and text[i + 1].isdigit()):
            start = i
            while i < length and (text[i].isalnum() or text[i] in ".xX_+-"):
                if text[i] in "+-" and i > start and text[i - 1] not in "eE":
                    break
                i += 1
            value = text[start:i]
            tokens.append(Token("number", value, start, line, column))
            advance(value)
            continue
        if char.isalpha() or char in "_$":
            start = i
            while i < length and (text[i].isalnum() or text[i] in "_$"):
                i += 1
            value = text[start:i]
            tokens.append(Token("identifier", value, start, line, column))
            advance(value)
            continue
        start_line, start_column = line, column
        two = text[i : i + 2]
        three = text[i : i + 3]
        if three in ("===", "!=="):
            tokens.append(Token("operator", three, i, line, column))
            i += 3
            advance(three)
        elif two in ("=>", "==", "!=", "&&", "||", "?."):
            tokens.append(Token("operator", two, i, line, column))
            i += 2
            advance(two)
        else:
            tokens.append(Token("punct", char, i, start_line, start_column))
            i += 1
            advance(char)
    tokens.append(Token("eof", "", length, line, column))
    return tokens
