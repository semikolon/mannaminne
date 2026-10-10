import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inbox_assignments as ia

# Copied verbatim from broker mails in the corpus (2026-10-08 and 2026-10-09),
# greeting and address lines left out.
DEPLOJA_MAIL = (
    "vi har <strong>3 nya jobb</strong> som matchar din profil "
    "Krav- och testledare (https://career.deploja.se/jobs/8525438-krav-och-testledare) "
    "Senior testautomatiserare .NET (https://career.deploja.se/jobs/8525889-senior-testautomatiserare-net) "
    "Systemutvecklare BI/DW (https://career.deploja.se/jobs/8526457-systemutvecklare-bi-dw) "
    "Som Connectad med Deploja AB är du alltid uppdaterad")

EWORK_MAIL = (
    "24 new jobs from your country just appeared on Ework Verama. Click on the job "
    "to see the full details. Föreläsning-Hög Arbetsbelastning Ework Group AB • Malmo (SWE) "
    "Systemutvecklare Polismyndigheten • Stockholm (SWE) "
    "Lösningsarkitekt Polismyndigheten • Stockholm (SWE) "
    "Senior Function Developer Aurobay Sweden AB • Gothenburg (SWE) ")


class ParserTests(unittest.TestCase):
    def test_deploja_titles_and_links(self):
        ads = ia.parse_deploja(DEPLOJA_MAIL)
        self.assertEqual([t for t, _ in ads],
                         ["Krav- och testledare", "Senior testautomatiserare .NET",
                          "Systemutvecklare BI/DW"])
        self.assertTrue(all(u.startswith("https://career.deploja.se/jobs/") for _, u in ads))

    def test_ework_lines(self):
        self.assertEqual(ia.parse_ework(EWORK_MAIL), [
            "Föreläsning-Hög Arbetsbelastning Ework Group AB • Malmo",
            "Systemutvecklare Polismyndigheten • Stockholm",
            "Lösningsarkitekt Polismyndigheten • Stockholm",
            "Senior Function Developer Aurobay Sweden AB • Gothenburg"])

    def test_chunks_are_joined_without_the_repeated_overlap(self):
        whole = "".join(chr(97 + i % 26) for i in range(1500))
        step = 750 - ia.CHUNK_OVERLAP
        parts = [whole[i:i + 750] for i in range(0, len(whole), step)]
        self.assertEqual(ia.join_chunks(parts), whole)


if __name__ == "__main__":
    unittest.main()
