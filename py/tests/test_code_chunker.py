import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import code_chunker as cc


PY_SRC = '''\
import os


def alpha(x):
    return x + 1


def beta(y):
    return y * 2


class Gamma:
    def method_one(self):
        return "one"

    def method_two(self):
        return "two"
'''


class GuessLanguageTests(unittest.TestCase):
    def test_extensions(self):
        self.assertEqual(cc.guess_language("a/b/foo.py"), "python")
        self.assertEqual(cc.guess_language("x.rs"), "rust")
        self.assertEqual(cc.guess_language("x.tsx"), "tsx")
        self.assertIsNone(cc.guess_language("README"))


class CastChunkingTests(unittest.TestCase):
    def test_python_symbols_and_line_ranges(self):
        chunks = cc.chunk_code(PY_SRC, "pkg/mod.py", max_chars=120)
        # every chunk carries a metadata header referencing the path
        for c in chunks:
            self.assertTrue(c.text.startswith("[pkg/mod.py]"))
            self.assertEqual(c.language, "python")
            self.assertGreaterEqual(c.start_line, 1)
            self.assertGreaterEqual(c.end_line, c.start_line)
        # the function + class symbols are discovered somewhere in the output
        symbols = {c.symbol for c in chunks}
        self.assertTrue(any("alpha" in s for s in symbols))
        self.assertTrue(any("beta" in s for s in symbols))
        # nested method carries the dotted class-path
        self.assertTrue(any("Gamma.method_one" in s for s in symbols),
                        f"symbols were: {symbols}")

    def test_oversized_function_is_split(self):
        big = "def huge():\n" + "\n".join(f"    a{i} = {i}" for i in range(400))
        chunks = cc.chunk_code(big, "big.py", max_chars=300)
        self.assertGreater(len(chunks), 1)  # a 400-line body must split
        for c in chunks:
            self.assertEqual(c.language, "python")

    def test_tiny_siblings_merge_not_one_per_symbol(self):
        # two tiny functions under a generous budget → they should NOT each be
        # their own chunk (cAST greedy-merge); one-symbol-per-chunk is the WORST option.
        src = "def a():\n    return 1\n\n\ndef b():\n    return 2\n"
        chunks = cc.chunk_code(src, "m.py", max_chars=1000)
        self.assertEqual(len(chunks), 1)

    def test_metadata_header_has_symbol_kind_and_lines(self):
        chunks = cc.chunk_code(PY_SRC, "mod.py", max_chars=120)
        alpha = next(c for c in chunks if "alpha" in c.symbol)
        self.assertIn("function_definition", alpha.text)
        self.assertIn("L", alpha.text)

    def test_unknown_language_line_fallback(self):
        src = "\n".join(f"line {i}" for i in range(50))
        chunks = cc.chunk_code(src, "notes.unknownext", max_chars=100)
        self.assertGreater(len(chunks), 0)
        for c in chunks:
            self.assertEqual(c.language, "text")
            self.assertTrue(c.text.startswith("[notes.unknownext]"))

    def test_body_reconstructs_source_without_loss(self):
        # conservation: concatenating chunk bodies in order recovers every
        # non-whitespace char of the source (no code silently dropped).
        chunks = cc.chunk_code(PY_SRC, "mod.py", max_chars=120)
        joined = "".join(c.body for c in chunks)
        self.assertEqual(cc._nonws_len(joined), cc._nonws_len(PY_SRC))


if __name__ == "__main__":
    unittest.main()
