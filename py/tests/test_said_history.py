"""`said` from Claude Code's prompt history, for sessions whose transcript is gone. The lines
are copied from the real history file (2025-09-29 to 2025-11-21)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import mannaminne as m  # noqa: E402

LINES = [
    '{"display":"/mcp ","pastedContents":{},"timestamp":1759150548116,"project":"/Users/fredrikbranstrom/Projects/kimonokittens"}',
    '{"display":"Can you pull and push to get up, and then maybe explain the Graphiti trust model fix a bit more so I can decide about that.","pastedContents":{},"timestamp":1759319465137,"project":"/Users/fredrikbranstrom/dotfiles"}',
    '{"display":"Running \\u001b[1mPostToolUse:mcp__zen__consensus\\u001b[22m...","pastedContents":{},"timestamp":1759322776014,"project":"/Users/fredrikbranstrom/dotfiles"}',
    '{"display":"Don\'t believe we\'re using the Responses API in this context but check the Graphiti code? Ultrathink","pastedContents":{},"timestamp":1763727874228,"project":"/Users/fredrikbranstrom/dotfiles","sessionId":"26bfefff-2c3f-471f-898a-46ffe8cde538"}',
]


class FromHistory(unittest.TestCase):
    def rows(self, seen=()):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "history.jsonl"
            path.write_text("\n".join(LINES) + "\n")
            return list(m._said_from_history(set(seen), path=str(path)))

    def test_his_words_are_kept_and_commands_and_terminal_output_are_not(self):
        texts = [r[6] for r in self.rows()]
        self.assertEqual(len(texts), 2)
        self.assertTrue(texts[0].startswith("Can you pull and push"))
        self.assertTrue(texts[1].startswith("Don't believe"))

    def test_a_line_without_a_session_is_grouped_by_project_and_day(self):
        r = self.rows()[0]
        self.assertEqual((r[1], r[2], r[4]), ("said", "said:h-dotfiles-2025-10-01", "dotfiles"))
        self.assertEqual(r[7], "2025-10-01")

    def test_a_session_whose_transcript_exists_is_skipped(self):
        rows = self.rows(seen={"26bfefff-2c3f-471f-898a-46ffe8cde538"})
        self.assertEqual([r[2] for r in rows], ["said:h-dotfiles-2025-10-01"])

    def test_a_missing_history_file_yields_nothing(self):
        self.assertEqual(list(m._said_from_history(set(), path="/nonexistent/history.jsonl")), [])


if __name__ == "__main__":
    unittest.main()
