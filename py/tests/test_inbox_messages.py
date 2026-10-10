import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inbox_messages as im


def row(**over):
    """A table row as build() writes it: an unread promotion that nothing protects."""
    r = {"t": "t1", "cat": "promotions", "band": 50_000, "before": 2024,
         "unread": True, "starred": False, "important": False, "inbox": True,
         "sent": False, "convo": False, "attach": False, "userlabel": False,
         "purchase": False, "transaction": False, "authority": False,
         "human": False, "labels": [], "size": 73_000, "size_exact": False}
    r.update(over)
    return r


BULK = ["Reklam", "ZD/*"]


class SweepRuleTests(unittest.TestCase):
    def test_unopened_unprotected_bulk_is_swept(self):
        for cat in ("updates", "promotions", "social"):
            self.assertTrue(im.in_sweep(row(cat=cat), BULK), cat)

    def test_opened_mail_is_never_swept(self):
        self.assertFalse(im.in_sweep(row(unread=False), BULK))

    def test_people_and_discussion_lists_are_never_swept(self):
        for cat in ("personal", "forums", "none"):
            self.assertFalse(im.in_sweep(row(cat=cat), BULK), cat)

    def test_every_protecting_signal_spares_the_message_on_its_own(self):
        signals = {"sent": "conversation", "convo": "conversation", "starred": "starred",
                   "important": "important", "attach": "attachment",
                   "authority": "money/authority", "transaction": "receipt/booking",
                   "purchase": "receipt/booking", "human": "personal sender"}
        for flag, reason in signals.items():
            r = row(**{flag: True})
            self.assertEqual(im.protect_reasons(r, BULK), [reason], flag)
            self.assertFalse(im.in_sweep(r, BULK), flag)

    def test_his_own_label_protects_but_a_filter_label_does_not(self):
        self.assertFalse(im.in_sweep(row(labels=["Invoice"]), BULK))
        self.assertTrue(im.in_sweep(row(labels=["Reklam"]), BULK))
        self.assertTrue(im.in_sweep(row(labels=["ZD/20130316"]), BULK))
        # One real label among bulk ones is enough.
        self.assertFalse(im.in_sweep(row(labels=["Reklam", "Bostad"]), BULK))

    def test_without_a_bulk_list_every_label_protects(self):
        self.assertFalse(im.in_sweep(row(labels=["Reklam"]), []))

    def test_prefix_pattern_needs_the_star(self):
        self.assertTrue(im.is_bulk_label("ZD/20130316", ["ZD/*"]))
        self.assertFalse(im.is_bulk_label("ZD/20130316", ["ZD/"]))
        self.assertFalse(im.is_bulk_label("Reklamation", ["Reklam"]))


class ProtectListTests(unittest.TestCase):
    """The money-and-authority list is personal and lives outside the repo."""

    def setUp(self):
        import tempfile
        import inbox_inventory
        self.inv = inbox_inventory
        self.dir = tempfile.mkdtemp()

    def write(self, text):
        path = str(Path(self.dir) / "labels.txt")
        Path(path).write_text(text, encoding="utf-8")
        return path

    def test_labels_are_read_one_per_line_without_comments(self):
        path = self.write("# authorities\nSkatteverket\n\n  lowell  \n# banks\nswedbank\n")
        self.assertEqual(self.inv.money_authority_labels(path), {"skatteverket", "lowell", "swedbank"})

    def test_a_missing_list_is_refused_not_read_as_nothing_to_protect(self):
        with self.assertRaises(SystemExit):
            self.inv.money_authority_labels(str(Path(self.dir) / "absent.txt"))

    def test_an_empty_list_is_refused_too(self):
        with self.assertRaises(SystemExit):
            self.inv.money_authority_labels(self.write("# nothing but a comment\n"))

    def test_a_label_matches_whole_domain_parts_only(self):
        self.assertEqual(self.inv.money_hits("mail.lowell.se", {"lowell", "eon"}), ["lowell"])
        self.assertEqual(self.inv.money_hits("moveon.org", {"lowell", "eon"}), [])

    def test_importing_the_modules_needs_no_personal_file(self):
        # A fresh clone has none. The authority query is built when a table is built.
        self.assertNotIn("authority", im.QUERIES)
        self.assertTrue(callable(im.queries))

    def test_the_source_names_no_sender_of_its_own(self):
        # The list this file replaced was derived from one person's mail. It must not come back.
        src = Path(self.inv.__file__).read_text(encoding="utf-8")
        self.assertNotIn("MONEY_AUTHORITY_LABELS = {", src)


if __name__ == "__main__":
    unittest.main()
