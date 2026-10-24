"""His prompts cut at his own paragraphs, and one copy of a text in the results (2026-10-25).

The prompt is copied from a real message of his (a reply to a quoted line of Claude's answer).
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mannaminne as m  # noqa: E402

REPLY = (
    "<!-- reply 1 -->\n"
    "> It’s fixable two ways. On my side, I should search with a question in your words. On the "
    "tool’s side, mannaminne could give the keyword half less weight when a query is long, and drop "
    "the duplicate copies.\n"
    "\n"
    "Do the mannaminne improvements pls. Btw could the prompts indexed by it be chunked in a more "
    "constructive manner perhaps?"
)


class ChunkSaid(unittest.TestCase):
    def test_quoted_lines_and_reply_markers_are_not_his_words(self):
        chunks = [c for _, c in m.chunk_said(REPLY)]
        self.assertEqual(chunks, ["Do the mannaminne improvements pls. Btw could the prompts indexed by it "
                                  "be chunked in a more constructive manner perhaps?"])

    def test_paragraphs_stay_whole_and_short_ones_join(self):
        long_a = "A" * 300
        long_b = "B " * 200
        text = f"{long_a}\n\nshort one\n\nshort two\n\n{long_b}"
        chunks = [c for _, c in m.chunk_said(text, size=750)]
        self.assertEqual(chunks[0], long_a)
        self.assertTrue(chunks[1].startswith("short one\n\nshort two"))
        self.assertNotIn("A", chunks[1])

    def test_a_paragraph_longer_than_a_chunk_falls_back_to_windows(self):
        text = "word " * 400
        chunks = [c for _, c in m.chunk_said(text, size=750, overlap=80)]
        self.assertGreater(len(chunks), 1)
        self.assertTrue(all(len(c) <= 750 for c in chunks))

    def test_attached_file_paths_are_dropped(self):
        text = '@"/Users/someone/Downloads/IMG_1.HEIC" Excellent work. Open the map when you are done.'
        self.assertEqual([c for _, c in m.chunk_said(text)], ["Excellent work. Open the map when you are done."])


class OneCopyOfAText(unittest.TestCase):
    def test_the_same_text_from_two_sessions_shows_once(self):
        def hit(rid, score):
            return {"r": (rid, "said", "p", "t", "s", "2026-10-20", "2026-10-20"), "kw": False,
                    "sem": score, "kwscore": 0.0, "sem_rank": 1 if score > 0.6 else 2}
        results = {"said:a#0": hit("said:a#0", 0.7), "said:b#3": hit("said:b#3", 0.5)}
        ranked = m._fuse_ranked(results, 10, {"said:a#0": "same", "said:b#3": "same"})
        self.assertEqual([x["r"][0] for x in ranked], ["said:a#0"])
        ranked = m._fuse_ranked(results, 10, {})
        self.assertEqual(len(ranked), 2)


if __name__ == "__main__":
    unittest.main()
