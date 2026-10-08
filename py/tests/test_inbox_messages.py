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


if __name__ == "__main__":
    unittest.main()
