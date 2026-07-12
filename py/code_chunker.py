#!/usr/bin/env python3
"""cAST code chunker — structure-aware chunking over tree-sitter ASTs.

Implements the cAST split-then-merge algorithm (the 2026 SOTA for code
retrieval chunking, measurably better than one-symbol-per-chunk or blind char
windows): recursively descend the AST, GREEDILY MERGE adjacent sibling nodes up
to a size budget, and RECURSIVELY SPLIT any single node that overflows it. Size
is measured in NON-WHITESPACE chars (cAST's key metric — whitespace shouldn't
consume budget). Every emitted chunk carries line-range + symbol name/kind and a
prepended metadata header (deterministic Contextual Retrieval — filepath +
symbol-path so the vector "knows" its origin, at $0 LLM cost).

Design home: ~/dotfiles/docs/code_index_build_plan_2026-07-11.md § C2.
Standalone + DB-free by design: this is Phase 2's testable core; the mannaminne
ingest path (a `code_chunks` table + a discover_code() source) wraps it later.

Budget note: default max_chars targets the Darwin ctx-512 embedder (the STANDING
path — the Z4 ctx-8192 endpoint is a stopped/VPN-pending accelerator, never a
dependency). Raise MAX_CHARS when embedding on a larger-context endpoint.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

# ~1200 non-whitespace chars ≈ well under the ctx-512 (~1600 nw-char) ceiling
# with margin for the metadata header. Overridable via env for a larger endpoint.
DEFAULT_MAX_CHARS = int(os.environ.get("MANNAMINNE_CODE_MAX_CHARS", "1200"))
DEFAULT_MIN_CHARS = int(os.environ.get("MANNAMINNE_CODE_MIN_CHARS", "80"))

# tree-sitter node types treated as named definitions (for symbol name + kind).
# Deliberately broad + multi-language; unknown grammars just yield no symbol.
_DEF_TYPES = {
    "function_definition", "function_declaration", "function_item",
    "method_definition", "method_declaration", "class_definition",
    "class_declaration", "class", "impl_item", "trait_item",
    "struct_item", "enum_item", "interface_declaration", "constructor_declaration",
    "arrow_function", "singleton_method", "def", "type_alias", "namespace_definition",
}

_EXT_LANG = {
    ".py": "python", ".pyi": "python",
    ".js": "javascript", ".jsx": "javascript", ".mjs": "javascript", ".cjs": "javascript",
    ".ts": "typescript", ".tsx": "tsx",
    ".rb": "ruby", ".rs": "rust", ".go": "go", ".java": "java",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cxx": "cpp", ".hpp": "cpp",
    ".cs": "c_sharp", ".php": "php", ".swift": "swift", ".kt": "kotlin",
    ".scala": "scala", ".sh": "bash", ".bash": "bash", ".lua": "lua",
    ".ex": "elixir", ".exs": "elixir",
}


def guess_language(path: str) -> str | None:
    return _EXT_LANG.get(Path(path).suffix.lower())


@dataclass
class CodeChunk:
    path: str
    language: str
    symbol: str            # best-effort enclosing symbol name ("" if none)
    symbol_kind: str       # tree-sitter node type of the enclosing def ("" if none)
    start_line: int        # 1-based, inclusive
    end_line: int          # 1-based, inclusive
    body: str              # raw source slice
    text: str              # metadata-header + body (what gets embedded + FTS'd)


def _nonws_len(s: str) -> int:
    return sum(1 for c in s if not c.isspace())


def _slice(source: bytes, a: int, b: int) -> str:
    return source[a:b].decode("utf-8", "replace")


def _cast_spans(node, source: bytes, max_nonws: int) -> list[tuple[int, int]]:
    """cAST split-then-merge → list of (start_byte, end_byte) spans covering
    node, each ≤ max_nonws non-whitespace chars where structurally possible."""
    if _nonws_len(_slice(source, node.start_byte, node.end_byte)) <= max_nonws:
        return [(node.start_byte, node.end_byte)]
    children = [c for c in node.children if c.end_byte > c.start_byte]
    if not children:                       # oversized leaf — cannot split further
        return [(node.start_byte, node.end_byte)]
    spans: list[tuple[int, int]] = []
    cur: tuple[int, int] | None = None
    for child in children:
        child_nonws = _nonws_len(_slice(source, child.start_byte, child.end_byte))
        if child_nonws > max_nonws:
            if cur:
                spans.append(cur); cur = None
            spans.extend(_cast_spans(child, source, max_nonws))
        elif cur is None:
            cur = (child.start_byte, child.end_byte)
        elif _nonws_len(_slice(source, cur[0], child.end_byte)) <= max_nonws:
            cur = (cur[0], child.end_byte)          # greedy merge of small siblings
        else:
            spans.append(cur)
            cur = (child.start_byte, child.end_byte)
    if cur:
        spans.append(cur)
    return spans


def _defs_in_span(root, a: int, b: int) -> list[tuple[str, str]]:
    """(dotted-symbol, node-type) for every named definition whose START falls in
    [a,b). A merged multi-def chunk lists ALL its symbols (better recall than
    keying only on the chunk's start node). `is_named` excludes keyword tokens."""
    out: list[tuple[str, str]] = []

    def walk(n):
        if n.start_byte >= b or n.end_byte <= a:
            return
        if n.is_named and n.type in _DEF_TYPES and a <= n.start_byte < b:
            out.append((_symbol_path(n), n.type))
        for c in n.children:
            walk(c)

    walk(root)
    return out


def _symbol_path(def_node) -> str:
    """Dotted path of enclosing def names, e.g. 'ClassName.method'."""
    parts: list[str] = []
    n = def_node
    while n is not None:
        if n.is_named and n.type in _DEF_TYPES:
            name = n.child_by_field_name("name")
            if name is not None:
                parts.append(name.text.decode("utf-8", "replace"))
        n = n.parent
    return ".".join(reversed(parts))


def _line_fallback(source: bytes, path: str, language: str,
                   max_chars: int) -> list[CodeChunk]:
    """No tree-sitter grammar / parse failure → token-aware line windows
    (never blind char windows). Still carries line-ranges + a header."""
    text = source.decode("utf-8", "replace")
    lines = text.split("\n")
    out: list[CodeChunk] = []
    buf: list[str] = []
    start = 1
    for i, line in enumerate(lines, 1):
        buf.append(line)
        if _nonws_len("\n".join(buf)) >= max_chars:
            out.append(_mk(path, language, "", "", start, i, "\n".join(buf)))
            buf, start = [], i + 1
    if buf and "".join(buf).strip():
        out.append(_mk(path, language, "", "", start, len(lines), "\n".join(buf)))
    return out


def _mk(path, language, symbol, kind, start_line, end_line, body) -> CodeChunk:
    header = f"[{path}] {kind or 'code'} {symbol} (L{start_line}-{end_line})".strip()
    return CodeChunk(path=path, language=language, symbol=symbol, symbol_kind=kind,
                     start_line=start_line, end_line=end_line, body=body,
                     text=f"{header}\n{body}")


def chunk_code(source: str | bytes, path: str, language: str | None = None,
               max_chars: int = DEFAULT_MAX_CHARS,
               min_chars: int = DEFAULT_MIN_CHARS) -> list[CodeChunk]:
    """Chunk one source file into cAST symbol-aware chunks with metadata.

    `path` is the (relative) filepath used in the metadata header + returned on
    each chunk. `language` overrides extension detection. Falls back to line
    windows when no grammar is available or parsing fails."""
    if isinstance(source, str):
        source = source.encode("utf-8")
    language = language or guess_language(path)
    if not language:
        return _line_fallback(source, path, "text", max_chars)
    try:
        from tree_sitter_language_pack import get_parser
        parser = get_parser(language)
        tree = parser.parse(source)
    except Exception:
        return _line_fallback(source, path, language, max_chars)

    spans = _cast_spans(tree.root_node, source, max_chars)
    # Merge a trailing under-min chunk into its predecessor so tiny tail
    # fragments (a closing brace, a lone import) don't become standalone chunks.
    merged: list[tuple[int, int]] = []
    for span in spans:
        body = _slice(source, *span)
        if merged and _nonws_len(body) < min_chars:
            merged[-1] = (merged[-1][0], span[1])
        else:
            merged.append(span)

    chunks: list[CodeChunk] = []
    for a, b in merged:
        body = _slice(source, a, b)
        if not body.strip():
            continue
        start_line = source[:a].count(b"\n") + 1
        end_line = source[:b].count(b"\n") + 1
        defs = _defs_in_span(tree.root_node, a, b)
        # symbol = space-joined dotted names of every def the chunk contains
        # (dedup, order-preserving); kind = the first def's type, else 'module'.
        names = list(dict.fromkeys(name for name, _ in defs if name))
        symbol = " ".join(names)
        kind = defs[0][1] if defs else "module"
        chunks.append(_mk(path, language, symbol, kind, start_line, end_line, body))
    return chunks


if __name__ == "__main__":  # tiny CLI for eyeballing a file's chunks
    import sys
    for p in sys.argv[1:]:
        src = Path(p).read_bytes()
        for c in chunk_code(src, p):
            print(f"L{c.start_line}-{c.end_line} {c.symbol_kind} {c.symbol} "
                  f"({_nonws_len(c.body)} nw-chars)")
