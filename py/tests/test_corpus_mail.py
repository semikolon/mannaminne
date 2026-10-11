import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import corpus_mail as cm

# Two lines as a weekly newsletter stores them (2026-10-04): every link is a tracking
# address in parentheses, longer than the sentence around it. The shape and the lengths
# are real; the subscriber's own token in the address is replaced with zeros.
LINKED = (
    "Oct 4th, 2026 - #701 - read online ( https://00000000.click.convertkit-mail4.com/"
    "00000000000000000000000000000000000/7qh7h8h9xqwwmdazh6/"
    "aHR0cHM6Ly9mcy5ibG9nL2JyYWluLWZvb2Qvb2N0b2Jlci00LTIwMjYv ) -\n"
    "Listen and Learn: YouTube ( https://00000000.click.convertkit-mail4.com/"
    "00000000000000000000000000000000000/owhkhqhw0pgg7pbvhr/"
    "aHR0cHM6Ly93d3cueW91dHViZS5jb20vd2F0Y2g_dj1DcHE4eTVIcGNGNCZmZWF0dXJlPXlvdXR1LmJl )"
    " | Spotify ( https://00000000.click.convertkit-mail4.com/"
    "00000000000000000000000000000000000/z2hghnhemnpp86sph0/aHR0cHM6Ly9vcGVuLnNwb3RpZnkuY29t )\n"
)


class StripLinksTests(unittest.TestCase):
    def test_tracking_links_go_and_the_words_stay(self):
        out = cm.strip_links(LINKED)
        self.assertNotIn("http", out)
        self.assertNotIn("( )", out)
        self.assertIn("Oct 4th, 2026 - #701 - read online -", out)
        self.assertIn("Listen and Learn: YouTube | Spotify", out)

    def test_most_of_a_linked_line_was_address(self):
        self.assertLess(len(cm.strip_links(LINKED)), len(LINKED) * 0.2)

    def test_a_bare_address_goes_too(self):
        self.assertEqual(cm.strip_links("see https://example.org/a/b?c=d for more"), "see for more")

    def test_text_without_links_is_untouched(self):
        text = "Options can be more dangerous to a competent person than failure.\n\nNext line."
        self.assertEqual(cm.strip_links(text), text)


class JoinChunksTests(unittest.TestCase):
    def test_the_overlap_is_not_doubled(self):
        whole = "".join(chr(97 + i % 26) for i in range(2000))
        step = 700
        parts = [whole[:750]]
        pos = 750
        while pos < len(whole):
            parts.append(whole[pos - cm.CHUNK_OVERLAP:pos + step])
            pos += step
        self.assertEqual(cm.join_chunks(parts), whole)


if __name__ == "__main__":
    unittest.main()
