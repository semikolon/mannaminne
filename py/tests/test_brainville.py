"""Brainville's daily mail: listings read from its HTML. The fixture is two listings cut from a
real mail of 2026-10-17, tracking parameters removed."""

import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import inbox_assignments as ia  # noqa: E402

PAGE = (Path(__file__).parent / "fixtures" / "brainville_two_listings.html").read_text()


class Listings(unittest.TestCase):
    def test_each_listing_has_its_title_company_and_days_left(self):
        ads = ia.parse_brainville_html(PAGE, "2026-10-17")
        self.assertEqual([a["title"] for a in ads], ["Projektledare", "Senior GCP Data Engineer"])
        self.assertEqual(ads[0]["id"], "368296")
        self.assertEqual(ads[0]["about"], "Upgraded People AB Stockholm Heltid ca. 12 månader")
        self.assertEqual(ads[0]["deadline"], "2026-11-15")
        self.assertTrue(ads[0]["url"].endswith("/Details/368296"))

    def test_collect_keeps_matching_titles_once_and_keys_them_by_ad(self):
        listings = [("2026-10-16", a) for a in ia.parse_brainville_html(PAGE, "2026-10-16")]
        listings += [("2026-10-17", a) for a in ia.parse_brainville_html(PAGE, "2026-10-17")]
        cur = mock.MagicMock()
        cur.fetchall.return_value = []
        with mock.patch.object(ia, "mails", return_value=[]):
            out = ia.collect(cur, "2026-10-04", ia.re.compile("data engineer", ia.re.I),
                             brainville=lambda since: listings)
        self.assertEqual([(a["key"], a["posted"]) for a in out], [("brainville:366724", "2026-10-16")])

    def test_gmail_unreadable_falls_back_to_text_windows(self):
        def broken(since):
            raise RuntimeError("no network")
        with mock.patch.object(ia, "mails", side_effect=lambda cur, dom, since:
                               [("2026-10-17", "… Senior GCP Data Engineer Tingent AB Stockholm …")]
                               if dom == ia.BRAINVILLE else []):
            out = ia.collect(mock.MagicMock(), "2026-10-04", ia.re.compile("data engineer", ia.re.I),
                             brainville=broken)
        self.assertTrue(out and out[0]["title"].startswith("…"))


if __name__ == "__main__":
    unittest.main()
