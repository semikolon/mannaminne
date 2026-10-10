import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import inbox_valued_senders as v

# Counts exactly as Gmail returned them on 2026-10-10 for seven real senders (starred, mails the
# owner sent there, mails since mid-2024, of them opened). The numbers are real; the sender names
# are replaced, because which senders a person reads is that person's own business.
REAL = {
    "jobboard.example": {"starred": 20, "wrote_to": 27, "recent": 197, "recent_read": 7},
    "emails.travel.example": {"starred": 0, "wrote_to": 0, "recent": 233, "recent_read": 3},
    "ainews.example": {"starred": 1, "wrote_to": 1, "recent": 223, "recent_read": 24},
    "news.essayist.example": {"starred": 0, "wrote_to": 0, "recent": 81, "recent_read": 0},
    "essayist.example": {"starred": 5, "wrote_to": 0, "recent": 81, "recent_read": 0},
    "kund.grocer.example": {"starred": 0, "wrote_to": 0, "recent": 266, "recent_read": 43},
    "wisdomblog.example": {"starred": 0, "wrote_to": 0, "recent": 123, "recent_read": 0},
}


class SignTests(unittest.TestCase):
    def test_stars_and_own_mail_are_signs(self):
        self.assertEqual(v.signs(REAL["jobboard.example"]), ["20 stjärnmärkta", "27 egna mejl dit"])

    def test_opened_counts_only_as_a_real_share_of_recent_mail(self):
        self.assertEqual(v.signs(REAL["ainews.example"]), ["öppnat 24 av 223 sedan mitten av 2024"])
        # seven opened of 197 is a habit of none: under a tenth
        self.assertNotIn("öppnat", " ".join(v.signs(REAL["jobboard.example"])))

    def test_a_sender_he_never_touched_shows_no_sign(self):
        self.assertEqual(v.signs(REAL["emails.travel.example"]), [])
        self.assertEqual(v.signs(REAL["wisdomblog.example"]), [])
        self.assertEqual(v.signs({}), [])

    def test_missing_counts_are_no_sign_and_no_crash(self):
        self.assertEqual(v.signs({"starred": None, "wrote_to": None, "recent": None, "recent_read": None}), [])


class RuleDomainTests(unittest.TestCase):
    def test_a_publishers_subdomains_are_one_publisher(self):
        self.assertEqual(v.rule_domain("news.essayist.example"), "essayist.example")
        self.assertEqual(v.rule_domain("kund.grocer.example"), "grocer.example")

    def test_a_two_part_suffix_keeps_three_parts(self):
        self.assertEqual(v.rule_domain("mail.society.org.uk"), "society.org.uk")

    def test_a_shared_sending_platform_is_never_widened(self):
        self.assertEqual(v.rule_domain("mail.beehiiv.com"), "mail.beehiiv.com")
        self.assertEqual(v.rule_domain("smtp.mailgun.org"), "smtp.mailgun.org")


class ProposeTests(unittest.TestCase):
    SENDERS = {"news.essayist.example": (5399, 226, "An Essayist"), "essayist.example": (510, 90, "An Essayist"),
               "emails.travel.example": (17183, 510, "Travel"), "ainews.example": (2620, 202, "AI News")}

    def test_a_sign_on_one_domain_keeps_the_whole_publisher(self):
        kept, dropped = v.propose(self.SENDERS, REAL)
        by_rule = {r["rule"]: r for r in kept}
        self.assertEqual(by_rule["essayist.example"]["pieces"], 5909)
        self.assertEqual(by_rule["essayist.example"]["signs"], ["5 stjärnmärkta"])
        self.assertIn("ainews.example", by_rule)
        self.assertEqual([r["rule"] for r in dropped], ["travel.example"])

    def test_largest_first(self):
        kept, _ = v.propose(self.SENDERS, REAL)
        self.assertEqual([r["rule"] for r in kept], ["essayist.example", "ainews.example"])


if __name__ == "__main__":
    unittest.main()
