"""Bounded Boolean expressions for keyword search, without evaluating user code."""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

MAX_TERMS_PER_RULE = 20
MAX_EXPRESSION_DEPTH = 20


class KeywordInputError(ValueError):
    """Raised when a keyword list cannot be stored safely."""


@dataclass(frozen=True, slots=True)
class Expression:
    operator: Literal["term", "not", "and", "or"]
    term: str = ""
    children: tuple[Expression, ...] = ()

    def to_data(self) -> dict[str, Any]:
        if self.operator == "term":
            return {"op": "term", "value": self.term.casefold()}
        return {"op": self.operator, "args": [child.to_data() for child in self.children]}

    def matches(self, contains: Callable[[str], bool]) -> bool:
        if self.operator == "term":
            return contains(self.term)
        if self.operator == "not":
            return not self.children[0].matches(contains)
        if self.operator == "and":
            return all(child.matches(contains) for child in self.children)
        return any(child.matches(contains) for child in self.children)

    def literal_terms(self) -> tuple[str, ...] | None:
        """Keep the original flat representation for positive conjunctions."""
        if self.operator == "term":
            return (self.term.casefold(),)
        if self.operator == "and" and all(child.operator == "term" for child in self.children):
            return tuple(child.term.casefold() for child in self.children)
        return None

    def render(self, parent_precedence: int = 0) -> str:
        precedence = {"or": 1, "and": 2, "not": 3, "term": 4}[self.operator]
        if self.operator == "term":
            value = quote_term(self.term)
        elif self.operator == "not":
            value = "!" + self.children[0].render(precedence)
        else:
            separator = " & " if self.operator == "and" else " || "
            value = separator.join(child.render(precedence) for child in self.children)
        return f"({value})" if precedence < parent_precedence else value


def expression_from_data(data: object) -> Expression:
    """Validate stored trees before matching; never reinterpret legacy literal terms."""
    terms = 0

    def read(item: object, depth: int) -> Expression:
        nonlocal terms
        # Each grammar level can add an OR, an AND and a unary node.
        if depth > MAX_EXPRESSION_DEPTH * 3 + 3 or not isinstance(item, dict):
            raise KeywordInputError("Некорректное логическое условие")
        operator = item.get("op")
        if operator == "term":
            value = item.get("value")
            terms += 1
            if not isinstance(value, str) or not value or terms > MAX_TERMS_PER_RULE:
                raise KeywordInputError("Некорректная часть логического условия")
            return Expression("term", value)
        args = item.get("args")
        if (
            operator not in ("not", "and", "or")
            or not isinstance(args, list)
            or not 1 <= len(args) <= MAX_TERMS_PER_RULE
            or (operator == "not" and len(args) != 1)
        ):
            raise KeywordInputError("Некорректный оператор логического условия")
        return Expression(operator, children=tuple(read(arg, depth + 1) for arg in args))

    return read(data, 0)


def quote_term(term: str) -> str:
    if any(char in term for char in ',&"!|()'):
        return '"' + term.replace('"', '""') + '"'
    return term


@dataclass(frozen=True, slots=True)
class _Token:
    kind: str
    value: str = ""


def _tokenize(value: str) -> list[_Token]:
    tokens: list[_Token] = []
    buffer: list[str] = []
    in_quotes = False
    closed_quote = False
    index = 0

    def flush() -> None:
        nonlocal closed_quote
        term = re.sub(r"\s+", " ", unicodedata.normalize("NFKC", "".join(buffer))).strip()
        if term:
            tokens.append(_Token("term", term))
        elif closed_quote:
            raise KeywordInputError("Ключевая фраза не должна быть пустой")
        buffer.clear()
        closed_quote = False

    while index < len(value):
        char = value[index]
        if in_quotes:
            if char == '"':
                if index + 1 < len(value) and value[index + 1] == '"':
                    buffer.append('"')
                    index += 1
                else:
                    in_quotes = False
                    closed_quote = True
            else:
                buffer.append(char)
        elif char == '"':
            if "".join(buffer).strip() or closed_quote:
                raise KeywordInputError("Кавычки должны охватывать ключ целиком")
            buffer.clear()
            in_quotes = True
        elif char in ",&\n!()|":
            flush()
            if char == "|":
                if index + 1 >= len(value) or value[index + 1] != "|":
                    raise KeywordInputError("Для ИЛИ используйте ||, для буквального | — кавычки")
                index += 1
            tokens.append(_Token("sep" if char in ",\n" else char))
        else:
            if closed_quote and not char.isspace():
                raise KeywordInputError("После закрывающей кавычки нужен оператор")
            buffer.append(char)
        index += 1
    if in_quotes:
        raise KeywordInputError("Закройте кавычки в ключевой фразе")
    flush()
    tokens.append(_Token("end"))
    return tokens


class _Parser:
    def __init__(self, value: str) -> None:
        self.tokens = _tokenize(value)
        self.index = 0
        self.term_count = 0

    @property
    def current(self) -> _Token:
        return self.tokens[self.index]

    def rules(self) -> list[Expression]:
        result = []
        while True:
            self.term_count = 0
            result.append(self.parse_or(0, grouped=False))
            if self.current.kind == "end":
                return result
            if self.current.kind != "sep":
                raise KeywordInputError("Между ключами нужен оператор; проверьте скобки")
            self.index += 1

    def parse_or(self, depth: int, *, grouped: bool) -> Expression:
        children = [self.parse_and(depth)]
        while self.current.kind == "|" or (grouped and self.current.kind == "sep"):
            self.index += 1
            children.append(self.parse_and(depth))
        return self.combine("or", children)

    def parse_and(self, depth: int) -> Expression:
        children = [self.parse_unary(depth)]
        while self.current.kind == "&":
            self.index += 1
            children.append(self.parse_unary(depth))
        return self.combine("and", children)

    @staticmethod
    def combine(operator: Literal["and", "or"], children: list[Expression]) -> Expression:
        flattened = tuple(
            child
            for node in children
            for child in (node.children if node.operator == operator else (node,))
        )
        return flattened[0] if len(flattened) == 1 else Expression(operator, children=flattened)

    def parse_unary(self, depth: int) -> Expression:
        if depth > MAX_EXPRESSION_DEPTH:
            raise KeywordInputError("Слишком глубокая вложенность скобок или отрицаний")
        token = self.current
        self.index += 1
        if token.kind == "term":
            self.term_count += 1
            if self.term_count > MAX_TERMS_PER_RULE:
                raise KeywordInputError(
                    f"В одном условии допускается не более {MAX_TERMS_PER_RULE} частей"
                )
            return Expression("term", token.value)
        if token.kind == "!":
            return Expression("not", children=(self.parse_unary(depth + 1),))
        if token.kind == "(":
            node = self.parse_or(depth + 1, grouped=True)
            if self.current.kind != ")":
                raise KeywordInputError("Закройте скобки в условии поиска")
            self.index += 1
            return node
        raise KeywordInputError("Между операторами должно быть ключевое слово или фраза")


def parse_expressions(value: str) -> list[Expression]:
    return _Parser(value).rules()
