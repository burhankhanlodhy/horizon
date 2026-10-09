"""Bounded, non-executing compiler for repeated literal pricing rows.

This module never reads or writes a client file. A managed integration must
provide the complete source and enforce the source version at native execution.
"""

from __future__ import annotations

import ast
import copy
import difflib
import hashlib
import re
from dataclasses import dataclass
from pathlib import PurePosixPath


class CompactEditError(ValueError):
    """Reject an operation before publishing a native edit."""


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 512:
        raise CompactEditError("invalid workspace path")
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or any(part in {".", ".."} for part in value.split("/"))
        or any(char in value for char in "\\:\r\n\x00")
        or str(path) != value
    ):
        raise CompactEditError("path must be a canonical workspace-relative path")
    return value


@dataclass(frozen=True)
class SourceSnapshot:
    path: str
    native_path: str
    source: str

    def __post_init__(self) -> None:
        relative_path(self.path)
        if (
            not isinstance(self.native_path, str)
            or not self.native_path
            or len(self.native_path) > 1024
            or any(char in self.native_path for char in "\r\n\x00")
        ):
            raise CompactEditError("invalid native path")
        if not isinstance(self.source, str):
            raise CompactEditError("source must be text")
        try:
            encoded = self.source.encode("utf-8")
        except UnicodeError as exc:
            raise CompactEditError("source must be UTF-8") from exc
        if len(encoded) > 512_000 or "\r" in self.source or "\x00" in self.source:
            raise CompactEditError("source must be bounded LF text")
        if not self.source.endswith("\n"):
            raise CompactEditError("source must have a final newline")

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.source.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Candidate:
    """One requested operation, supplied by a trusted intent classifier."""

    snapshot: SourceSnapshot
    table: str
    template: str
    keys: tuple[str, ...]


@dataclass(frozen=True)
class CompiledEdit:
    snapshot: SourceSnapshot
    after: str
    patch: str


def _span(source: str, node: ast.AST) -> tuple[int, int]:
    lines = source.splitlines(keepends=True)
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))

    def position(line: int, byte_column: int) -> int:
        # AST columns are UTF-8 byte offsets, not Python character offsets.
        prefix = lines[line - 1].encode("utf-8")[:byte_column].decode("utf-8")
        return offsets[line - 1] + len(prefix)

    return (
        position(node.lineno, node.col_offset),
        position(node.end_lineno, node.end_col_offset),
    )


def _literal_row(value: ast.AST, key: str) -> None:
    if (
        not isinstance(value, ast.Call)
        or not isinstance(value.func, ast.Name)
        or value.func.id != "ModelPricing"
        or value.args
        or not value.keywords
    ):
        raise CompactEditError("only literal ModelPricing keyword rows are supported")
    names = [keyword.arg for keyword in value.keywords]
    if None in names or len(set(names)) != len(names):
        raise CompactEditError("unpacking or duplicate keywords are unsupported")
    for keyword in value.keywords:
        if not isinstance(keyword.value, ast.Constant) or type(keyword.value.value) not in {
            str,
            int,
            float,
            bool,
            type(None),
        }:
            raise CompactEditError("computed constructor values are unsupported")
    model = next((item.value for item in value.keywords if item.arg == "model"), None)
    if not isinstance(model, ast.Constant) or model.value != key:
        raise CompactEditError("row key must equal its model literal")


def compile_candidate(candidate: Candidate) -> CompiledEdit:
    """Clone 4–16 literal rows; change only dictionary keys and model fields.

    Constructor semantics/import identity still require a certified module
    contract. AST checks alone cannot establish arbitrary Python runtime safety.
    """
    source = candidate.snapshot.source
    keys = candidate.keys
    if not isinstance(keys, tuple) or not 4 <= len(keys) <= 16:
        raise CompactEditError("compact clone count must be between 4 and 16")
    if any(
        not isinstance(key, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,127}", key)
        for key in keys
    ):
        raise CompactEditError("invalid model key")
    if len(set(keys)) != len(keys):
        raise CompactEditError("duplicate requested keys")
    if not isinstance(candidate.table, str) or not candidate.table.isidentifier():
        raise CompactEditError("invalid table name")
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise CompactEditError("source is not a supported Python module") from exc
    bindings = [
        node
        for node in tree.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == candidate.table
    ]
    if len(bindings) != 1 or not isinstance(bindings[0].value, ast.Dict):
        raise CompactEditError("expected one top-level annotated dictionary")
    binding = bindings[0]
    if ast.unparse(binding.annotation) != "dict[str, ModelPricing]":
        raise CompactEditError("unexpected catalog annotation")
    # Reject a second binding or mutation anywhere, including inside functions.
    for node in ast.walk(tree):
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            if node.id == "ModelPricing" or (
                node.id == candidate.table and node is not binding.target
            ):
                raise CompactEditError("ambiguous catalog or constructor binding")
        if isinstance(node, (ast.Assign, ast.AugAssign, ast.Delete)):
            targets = node.targets if isinstance(node, (ast.Assign, ast.Delete)) else [node.target]
            if any(
                isinstance(target, (ast.Subscript, ast.Attribute))
                and any(
                    isinstance(part, ast.Name) and part.id == candidate.table
                    for part in ast.walk(target)
                )
                for target in targets
            ):
                raise CompactEditError("catalog mutation is unsupported")
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ) and node.name in {candidate.table, "ModelPricing"}:
            raise CompactEditError("catalog or constructor is rebound")
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for alias in node.names:
                bound = alias.asname or (
                    alias.name.split(".")[0] if isinstance(node, ast.Import) else alias.name
                )
                if bound == candidate.table or (
                    bound == "ModelPricing"
                    and not (
                        isinstance(node, ast.ImportFrom)
                        and alias.name == "ModelPricing"
                        and alias.asname is None
                        and (
                            (node.module == "registry" and node.level == 1)
                            or (node.module == "horizon.pricing.registry" and node.level == 0)
                        )
                    )
                ):
                    raise CompactEditError("catalog or constructor import is ambiguous")
                if alias.name == "*":
                    raise CompactEditError("wildcard imports are unsupported")
    imports = [
        node
        for node in tree.body
        if isinstance(node, ast.ImportFrom)
        and (
            (node.module == "registry" and node.level == 1)
            or (node.module == "horizon.pricing.registry" and node.level == 0)
        )
        and any(alias.name == "ModelPricing" and alias.asname is None for alias in node.names)
    ]
    if len(imports) != 1:
        raise CompactEditError("constructor import does not match the catalog contract")
    table = binding.value
    existing: list[str] = []
    for key_node, value in zip(table.keys, table.values):
        if not isinstance(key_node, ast.Constant) or not isinstance(key_node.value, str):
            raise CompactEditError("dictionary unpacking/computed keys are unsupported")
        existing.append(key_node.value)
        _literal_row(value, key_node.value)
    if len(set(existing)) != len(existing) or candidate.template not in existing:
        raise CompactEditError("ambiguous or missing template")
    if set(existing).intersection(keys):
        raise CompactEditError("requested key already exists")
    index = existing.index(candidate.template)
    key_node, value_node = table.keys[index], table.values[index]
    start, _ = _span(source, key_node)
    _, end = _span(source, value_node)
    line_start = source.rfind("\n", 0, start) + 1
    indent = source[line_start:start]
    if not indent or indent.strip() or "\t" in indent:
        raise CompactEditError("template must start on an indented line")
    line_end = source.find("\n", end)
    if line_end < 0 or source[end:line_end].strip() != ",":
        raise CompactEditError("template must end with a standalone trailing comma")
    closing = _span(source, table)[1] - 1
    close_line = source.rfind("\n", 0, closing) + 1
    if source[close_line:closing].strip() or source[closing : source.find("\n", closing)] != "}":
        raise CompactEditError("dictionary closing brace must be on its own line")
    # Appending requires the previous row to have a comma too.
    last_end = _span(source, table.values[-1])[1]
    if source[last_end : source.find("\n", last_end)].strip() != ",":
        raise CompactEditError("last dictionary row must have a trailing comma")
    model_node = next(item.value for item in value_node.keywords if item.arg == "model")
    replacements = [_span(source, key_node), _span(source, model_node)]
    original = source[start:end]
    if len(original.encode("utf-8")) > 16_384:
        raise CompactEditError("template row exceeds the supported expansion size")
    rows = []
    for key in keys:
        row = original
        for left, right in sorted(replacements, reverse=True):
            row = row[: left - start] + repr(key) + row[right - start :]
        rows.append(indent + row + ",\n")
    after = source[:close_line] + "".join(rows) + source[close_line:]
    try:
        actual = ast.parse(after)
    except (SyntaxError, ValueError, RecursionError) as exc:
        raise CompactEditError("expanded module is invalid") from exc
    expected = copy.deepcopy(tree)
    expected_table = expected.body[tree.body.index(binding)].value
    for key in keys:
        row = copy.deepcopy(value_node)
        next(item for item in row.keywords if item.arg == "model").value = ast.Constant(key)
        expected_table.keys.append(ast.Constant(key))
        expected_table.values.append(row)
    if ast.dump(actual, include_attributes=False) != ast.dump(expected, include_attributes=False):
        raise CompactEditError("expanded module differs from the permitted operation")
    diff = list(difflib.unified_diff(source.splitlines(True), after.splitlines(True), n=3))
    patch = "*** Begin Patch\n*** Update File: " + candidate.snapshot.path + "\n"
    patch += "".join("@@\n" if line.startswith("@@") else line for line in diff[2:])
    patch += "*** End Patch\n"
    return CompiledEdit(candidate.snapshot, after, patch)
