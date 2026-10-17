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

# Three stretches of real Ework mails (2026-09-15, 2026-10-08, 2026-09-25) where a
# tag sits between two cards.
EWORK_RATE = (
    "full details. Senior Logistics Developer – Warehouse Automation Ework Group AB • Vasteras (SWE) "
    "Integration Developer Ework Group AB • Solna (SWE) 850.00 SEK/h "
    "AI Platform Engineer / Architect Ework Group AB • Gothenburg (SWE) "
    "Apputvecklare Polismyndigheten • Stockholm (SWE) ")

EWORK_ALSO = (
    "full details. Process- och metodhandledare Ework Group AB • Malmo (SWE) "
    "Specialized Lending Consultant Ework Group Oy • Stockholm (SWE) or Helsinki (FIN) + more "
    "Tech Lead Ework Group AB • Stockholm (SWE) Data Engineer Ework Group AB • Malmo (SWE) ")

EWORK_REMOTE = (
    "full details. Transformationsledare för AI driven produktutveckling Ework Group AB • Stockholm (SWE) "
    "900.00 SEK/h Identity & Access Management Developer Ework Group Oy • Stockholm (SWE) "
    "or Warsaw (POL) + more Remote Technical Lead – Microsoft Entra / Global Secure Access "
    "Ework Group AB • Södertälje (SWE) ")


class ParserTests(unittest.TestCase):
    def test_deploja_titles_and_links(self):
        ads = ia.parse_deploja(DEPLOJA_MAIL)
        self.assertEqual([t for t, _ in ads],
                         ["Krav- och testledare", "Senior testautomatiserare .NET",
                          "Systemutvecklare BI/DW"])
        self.assertTrue(all(u.startswith("https://career.deploja.se/jobs/") for _, u in ads))

    def test_ework_lines(self):
        self.assertEqual([a["ad"] for a in ia.parse_ework(EWORK_MAIL)], [
            "Föreläsning-Hög Arbetsbelastning Ework Group AB • Malmo",
            "Systemutvecklare Polismyndigheten • Stockholm",
            "Lösningsarkitekt Polismyndigheten • Stockholm",
            "Senior Function Developer Aurobay Sweden AB • Gothenburg"])

    def test_ework_tags_belong_to_the_card_before_them(self):
        ads = {a["ad"]: a for a in ia.parse_ework(EWORK_RATE)}
        self.assertEqual(ads["Integration Developer Ework Group AB • Solna"]["rate"], "850.00 SEK/h")
        self.assertEqual(
            ads["AI Platform Engineer / Architect Ework Group AB • Gothenburg"]["rate"], "")

        ads = {a["ad"]: a for a in ia.parse_ework(EWORK_ALSO)}
        self.assertEqual(
            ads["Specialized Lending Consultant Ework Group Oy • Stockholm"]["also"],
            "Helsinki (FIN) + more")
        self.assertEqual(ads["Tech Lead Ework Group AB • Stockholm"]["also"], "")

        ads = {a["ad"]: a for a in ia.parse_ework(EWORK_REMOTE)}
        lead = "Transformationsledare för AI driven produktutveckling Ework Group AB • Stockholm"
        iam = "Identity & Access Management Developer Ework Group Oy • Stockholm"
        entra = ("Technical Lead – Microsoft Entra / Global Secure Access "
                 "Ework Group AB • Södertälje")
        self.assertEqual(ads[lead]["rate"], "900.00 SEK/h")
        self.assertTrue(ads[iam]["remote"])
        self.assertEqual(ads[iam]["also"], "Warsaw (POL) + more")
        self.assertFalse(ads[entra]["remote"])

    def test_a_removed_ad_reads_as_closed_not_unreachable(self):
        import email.message
        import urllib.error
        from unittest import mock
        url = "https://career.deploja.se/jobs/8499884-tech-lead"   # answered 410 on 2026-10-17
        gone = urllib.error.HTTPError(url, 410, "Gone", email.message.Message(), None)
        with mock.patch("urllib.request.urlopen", side_effect=gone):
            self.assertEqual(ia.check_deploja(url), (False, "", ""))
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.HTTPError(url, 503, "x", email.message.Message(), None)):
            self.assertIsNone(ia.check_deploja(url))
        with mock.patch("urllib.request.urlopen", side_effect=TimeoutError()):
            self.assertIsNone(ia.check_deploja(url))

    def test_chunks_are_joined_without_the_repeated_overlap(self):
        whole = "".join(chr(97 + i % 26) for i in range(1500))
        step = 750 - ia.CHUNK_OVERLAP
        parts = [whole[i:i + 750] for i in range(0, len(whole), step)]
        self.assertEqual(ia.join_chunks(parts), whole)


if __name__ == "__main__":
    unittest.main()
