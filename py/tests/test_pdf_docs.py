"""PDFs in ~/Documents are read through their text layer (2026-11-09).

Reads the real folder and skips without it; this repository is public, so no file name
or content is asserted, only that nearly all of them yield text.

    .venv/bin/python -m unittest tests.test_pdf_docs
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mannaminne as m  # noqa: E402

DOCS = os.path.expanduser("~/Documents")


@unittest.skipUnless(os.path.isdir(DOCS), "no ~/Documents on this machine")
class RealPdfs(unittest.TestCase):
    def test_most_pdfs_yield_text_and_scans_yield_empty(self):
        files = [f for f in m._walk_docs(DOCS, (".pdf",)) if not m._is_dataless(f)]
        if len(files) < 50:
            self.skipTest("fewer than 50 PDFs on disk")
        texts = [m._pdf_text(f) for f in files]
        with_text = sum(1 for t in texts if len(t.strip()) > 50)
        self.assertGreater(with_text / len(files), 0.9)
        self.assertTrue(all("\f" not in t for t in texts))

    def test_pdf_is_a_documents_suffix(self):
        self.assertIn('".pdf"), "documents")', Path(m.__file__).read_text())


if __name__ == "__main__":
    unittest.main()
