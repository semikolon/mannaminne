import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mail_judge as mj

# The first piece of one stored mail, as the index holds it (a mailing list's weekly mail,
# 2026-10-05). The shape is real: the subject is the first line, there is no Subject: line, and
# the headers come in this order. Sender and recipient are replaced.
STORED_FIRST_PIECE = '''37 new roles
From: "Example Job Board" <team@jobboard.example>
To: Owner <owner@example.com>
Date: Mon, 05 Oct 2026 12:04:07 +0000

View this email in your browser (https://lists.example/jobboard/37-new-roles?e=0000000000)


** Hi everybody! This week:
------------------------------------------------------------
* 37 new roles
* Remote roles
'''
STORED_TITLE = "37 new roles"

# Two answers exactly as the decision client returned them for mail headers on 2026-10-10:
# a newspaper's newsletter, and a shop mail it was less sure of.
REAL_SURE = {"choice": "bulk", "confidence": 1, "probabilities": {"person": 0, "machine": 0, "bulk": 1}}
REAL_UNSURE = {"choice": "bulk", "confidence": 0.61, "probabilities": {"person": 0.16, "machine": 0.1, "bulk": 0.74}}


class HeaderTests(unittest.TestCase):
    def test_the_subject_comes_from_the_title_when_the_text_has_no_subject_line(self):
        h = mj.headers_of(STORED_FIRST_PIECE, STORED_TITLE)
        self.assertEqual(h["subject"], "37 new roles")
        self.assertEqual(h["from"], '"Example Job Board" <team@jobboard.example>')
        self.assertEqual(h["date"], "Mon, 05 Oct 2026 12:04:07 +0000")

    def test_only_the_four_headers_are_sent(self):
        state = json.loads(mj.state_of(mj.headers_of(STORED_FIRST_PIECE, STORED_TITLE)))
        self.assertEqual(set(state), {"email"})
        self.assertEqual(set(state["email"]), {"subject", "from", "to", "date"})
        self.assertNotIn("View this email", json.dumps(state))

    def test_a_long_header_is_cut_not_sent_whole(self):
        h = mj.headers_of("From: " + "x" * 900 + "\n", "t" * 900)
        self.assertEqual(len(h["from"]), 200)
        self.assertEqual(len(h["subject"]), 200)


class VerdictTests(unittest.TestCase):
    def test_real_answers_are_read(self):
        self.assertEqual(mj.verdict(REAL_SURE), ("bulk", 1.0))
        choice, prob = mj.verdict(REAL_UNSURE)
        self.assertEqual(choice, "bulk")
        self.assertEqual(prob, REAL_UNSURE["probabilities"]["bulk"])

    def test_the_unsure_real_answer_earns_no_class(self):
        self.assertIsNone(mj.class_for(*mj.verdict(REAL_UNSURE)))
        self.assertEqual(mj.class_for(*mj.verdict(REAL_SURE)), "bulk")

    def test_no_answer_or_an_unknown_choice_is_no_verdict(self):
        for answer in (None, {}, {"choice": "spam", "probabilities": {"spam": 0.99}}, {"choice": "bulk"}):
            self.assertIsNone(mj.verdict(answer), answer)

    def test_below_the_line_a_guess_earns_no_class(self):
        self.assertEqual(mj.class_for("bulk", 0.80), "bulk")
        self.assertIsNone(mj.class_for("bulk", 0.79))

    def test_the_models_machine_becomes_account_which_is_not_skipped(self):
        import mannaminne as m
        self.assertEqual(mj.class_for("machine", 0.95), "account")
        self.assertNotIn("account", m.EMBED_SKIP_CLASSES)
        self.assertNotIn("person", m.EMBED_SKIP_CLASSES)
        self.assertIn("bulk", m.EMBED_SKIP_CLASSES)


class FakeClient:
    """Stands in for the decision client: answers from a table, counts its calls."""

    def __init__(self, answers):
        self.answers, self.calls = answers, []

    def decide(self, state, questions, timeout=20.0):
        self.calls.append(state)
        subject = json.loads(state)["email"]["subject"]
        if subject not in self.answers:
            return None
        return {"answers": {"kind": self.answers[subject]}, "cost_usd": 0.00003}


class AskTests(unittest.TestCase):
    def test_one_mail_per_request_and_no_answer_stays_no_answer(self):
        client = FakeClient({"37 new roles": REAL_SURE})
        states = [mj.state_of({"subject": "37 new roles", "from": "a"}), mj.state_of({"subject": "silence", "from": "b"})]
        out = mj.ask(client, states, workers=2)
        self.assertEqual(len(client.calls), 2)
        self.assertEqual(out[0][0], ("bulk", 1.0))
        self.assertIsNone(out[1][0])

    def test_without_a_client_file_nothing_is_asked(self):
        old = {k: os.environ.get(k) for k in (mj.ENV_CLIENT, mj.ENV_SWITCH)}
        try:
            os.environ[mj.ENV_CLIENT] = "/nonexistent/decision_client.py"
            self.assertIsNone(mj.load_client())
            os.environ.pop(mj.ENV_CLIENT)
            os.environ[mj.ENV_SWITCH] = "0"
            self.assertIsNone(mj.load_client())
        finally:
            for k, v in old.items():
                if v is None:
                    os.environ.pop(k, None)
                else:
                    os.environ[k] = v


class LiveTableTests(unittest.TestCase):
    """What must hold in the real class table, whoever wrote to it."""

    def setUp(self):
        import mannaminne as m
        self.m = m
        try:
            self.conn = m.load_conn()
            self.cur = self.conn.cursor()
            self.cur.execute("SET statement_timeout = '20s'")
            self.cur.execute("SELECT count(*) FROM information_schema.columns "
                             "WHERE table_name='email_class' AND column_name='jev_class'")
            if not self.cur.fetchone()[0]:
                self.skipTest("the class table has no judge columns yet")
        except unittest.SkipTest:
            raise
        except Exception as e:
            self.skipTest(f"live index unreachable or busy: {e}")

    def tearDown(self):
        try:
            self.conn.rollback()
        except Exception:
            pass

    def test_no_class_was_set_by_the_judge_below_the_line(self):
        self.cur.execute("SELECT count(*) FROM email_class WHERE set_by='jev' AND (jev_prob IS NULL OR jev_prob < %s)",
                         (mj.MIN_PROB,))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_the_judges_classes_carry_its_name(self):
        self.cur.execute("SELECT count(*) FROM email_class WHERE class = ANY(%s) AND set_by IS DISTINCT FROM 'jev'",
                         (list(mj.CLASS_OF.values()),))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_an_unsure_guess_left_the_mail_unplaced(self):
        self.cur.execute("SELECT count(*) FROM email_class WHERE jev_prob < %s AND class = ANY(%s)",
                         (mj.MIN_PROB, list(mj.CLASS_OF.values())))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_no_mail_from_a_ruled_sender_carries_the_judges_class(self):
        domains, _ = self.m._load_email_rules()
        if not domains:
            self.skipTest("no sender rules configured")
        names = list(domains)
        self.cur.execute("""SELECT count(*) FROM email_class
                            WHERE set_by='jev' AND (domain = ANY(%s) OR domain LIKE ANY(%s))""",
                         (names, ["%." + d for d in names]))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_no_mail_from_a_protected_sender_carries_the_judges_class(self):
        protected = mj.protected_labels()
        if not protected:
            self.skipTest("no protect list on this machine")
        self.cur.execute("SELECT count(*) FROM email_class e WHERE e.set_by='jev' AND " + mj.PROTECTED_SQL,
                         (protected,))
        self.assertEqual(self.cur.fetchone()[0], 0)

    def test_a_protected_sender_is_matched_by_whole_domain_parts(self):
        self.cur.execute("SELECT " + mj.PROTECTED_SQL.replace("e.domain", "%s::text"), ("mail.lowell.se", ["lowell"]))
        self.assertTrue(self.cur.fetchone()[0])
        self.cur.execute("SELECT " + mj.PROTECTED_SQL.replace("e.domain", "%s::text"), ("slowell.se", ["lowell"]))
        self.assertFalse(self.cur.fetchone()[0])

    def test_the_question_asked_is_the_question_that_was_measured(self):
        measured = Path.home() / ".claude" / "hooks" / "jev_eval" / "email_run.py"
        if not measured.is_file():
            self.skipTest("the measurement script is not on this machine")
        src = measured.read_text(encoding="utf-8")
        scope = {}
        exec(src[src.index("Q = {'type': 'choice'"):src.index("def ask(i):")], scope)
        self.assertEqual(mj.QUESTION, scope["Q"])


if __name__ == "__main__":
    unittest.main()
