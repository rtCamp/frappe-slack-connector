import json

import frappe
from frappe.tests import IntegrationTestCase

from frappe_slack_connector.gmail_mirror.messages import (
    HEADER_LIMIT,
    parent_blocks,
    parent_text,
    reply_blocks,
    reply_text,
    resumed_blocks,
    sorted_emails,
    stopped_blocks,
)

# The default test email is 30 minutes older than this
NOW = frappe.utils.get_datetime("2026-01-05 10:30:00")


def _email(**fields):
    base = dict(
        name="m1",
        idx=1,
        sender="ann@example.com",
        sender_full_name="Ann Lee",
        recipients="bob@example.com",
        plain_content="Hello there",
        date_and_time="2026-01-05 10:00:00",
        attachments_data=None,
    )
    return frappe._dict(base | fields)


def _thread(emails=None, **fields):
    base = dict(
        name="t1",
        subject_of_first_mail="Quarterly plan",
        reference_doctype=None,
        reference_name=None,
        emails=emails if emails is not None else [_email()],
    )
    return frappe._dict(base | fields)


def _objects(blocks):
    """Every text object of the blocks: section and header texts, context elements."""
    for block in blocks:
        for obj in [block.get("text"), *block.get("elements", [])]:
            if obj:
                yield obj


def _texts(blocks, kind):
    return [obj["text"] for obj in _objects(blocks) if obj.get("type") == kind]


class TestParentBlocks(IntegrationTestCase):
    def test_subject_participants_and_link(self):
        blocks = parent_blocks(_thread())
        self.assertEqual(blocks[0]["type"], "header")
        self.assertEqual(blocks[0]["text"]["text"], "Quarterly plan")
        self.assertIn("Participants: ann@example.com, bob@example.com", _texts(blocks, "plain_text"))
        self.assertIn("|Open thread in ERP>", _texts(blocks, "mrkdwn")[0])

    def test_empty_and_long_subject(self):
        self.assertEqual(parent_blocks(_thread(subject_of_first_mail="  "))[0]["text"]["text"], "(no subject)")
        self.assertEqual(len(parent_blocks(_thread(subject_of_first_mail="x" * 400))[0]["text"]["text"]), HEADER_LIMIT)

    def test_participants_are_capped(self):
        emails = [_email(name=f"m{i}", sender=f"p{i}@example.com", recipients="") for i in range(25)]
        texts = _texts(parent_blocks(_thread(emails=emails)), "plain_text")
        participants = next(t for t in texts if t.startswith("Participants"))
        self.assertTrue(participants.endswith(" and 15 more"))

    def test_thread_without_emails(self):
        self.assertIn("Participants: unknown", _texts(parent_blocks(_thread(emails=[])), "plain_text"))

    def test_linked_record_gets_a_link(self):
        blocks = parent_blocks(_thread(reference_doctype="ToDo", reference_name="TD-1"))
        self.assertIn("|ToDo: TD-1>", _texts(blocks, "mrkdwn")[0])

    def test_hostile_subject_stays_literal(self):
        hostile = "<!channel> *urgent* <https://evil.example|click>"
        blocks = parent_blocks(_thread(subject_of_first_mail=hostile))
        self.assertEqual(blocks[0]["text"], {"type": "plain_text", "text": hostile, "emoji": False})
        self.assertNotIn("<!channel>", parent_text(_thread(subject_of_first_mail=hostile)))


class TestReplyBlocks(IntegrationTestCase):
    def test_fresh_email_shows_the_name_alone(self):
        blocks = reply_blocks(_email(), now=NOW)
        self.assertEqual(_texts(blocks, "mrkdwn"), ["*Ann Lee*"])
        self.assertIn("Hello there", _texts(blocks, "plain_text"))

    def test_email_older_than_an_hour_shows_its_date(self):
        blocks = reply_blocks(_email(date_and_time="2026-01-03 17:58:00"), now=NOW)
        self.assertEqual(_texts(blocks, "mrkdwn"), ["*Ann Lee*  ·  3 Jan 2026, 17:58"])

    def test_no_link_on_a_reply(self):
        self.assertNotIn("http", json.dumps(reply_blocks(_email(), now=NOW)))

    def test_sender_falls_back_to_the_address(self):
        self.assertEqual(_texts(reply_blocks(_email(sender_full_name=None), now=NOW), "mrkdwn"), ["*ann@example.com*"])

    def test_hostile_text_cannot_mention_or_format(self):
        hostile = "<!channel> <@U123> *bold* <https://evil.example|click>"
        blocks = reply_blocks(_email(sender_full_name=hostile, plain_content=hostile), now=NOW)
        header = _texts(blocks, "mrkdwn")[0]
        self.assertNotIn("<!channel>", header)
        self.assertNotIn("<@U123>", header)
        self.assertNotIn("*bold*", header)
        # the body is shown literally, in an object Slack does not parse
        self.assertIn(hostile, _texts(blocks, "plain_text"))
        for obj in _objects(blocks):
            if obj["type"] == "mrkdwn":
                self.assertIs(obj["verbatim"], True)
        self.assertNotIn("<!channel>", reply_text(_email(sender_full_name=hostile, plain_content=hostile)))

    def test_empty_body(self):
        """An HTML-only email has no plain text; Slack rejects an empty text object."""
        for empty in (None, "", "   \n"):
            with self.subTest(body=empty):
                self.assertIn("(no text)", _texts(reply_blocks(_email(plain_content=empty), now=NOW), "plain_text"))

    def test_missing_sender_and_date(self):
        blocks = reply_blocks(_email(sender=None, sender_full_name=None, date_and_time=None), now=NOW)
        self.assertEqual(_texts(blocks, "mrkdwn")[0], "*Unknown sender*")

    def test_long_body_is_cut(self):
        body = _texts(reply_blocks(_email(plain_content="x" * 5000), now=NOW), "plain_text")[0]
        self.assertTrue(body.endswith("… (continued in the ERP)"))
        self.assertLessEqual(len(body), 3000)

    def test_attachment_names(self):
        data = json.dumps(
            [{"file_name": "plan.pdf", "file_doc_name": "F1"}, {"file_name": "<!here>.png", "file_doc_name": "F2"}]
        )
        texts = _texts(reply_blocks(_email(attachments_data=data), now=NOW), "plain_text")
        self.assertIn("Attachments: plan.pdf, <!here>.png", texts)

    def test_unreadable_attachment_data_counts_as_none(self):
        for data in ("not json", json.dumps({"file_name": "x"}), []):
            with self.subTest(data=data):
                texts = _texts(reply_blocks(_email(attachments_data=data), now=NOW), "plain_text")
                self.assertFalse([t for t in texts if t.startswith("Attachments")])


class TestOrderAndNotes(IntegrationTestCase):
    def test_sorted_emails_puts_undated_last(self):
        emails = [
            _email(name="b", idx=1, date_and_time=None),
            _email(name="a", idx=2, date_and_time="2026-01-01 09:00:00"),
        ]
        self.assertEqual([e.name for e in sorted_emails(_thread(emails=emails))], ["a", "b"])

    def test_stop_and_resume_notes_name_the_user_literally(self):
        self.assertEqual(
            _texts(stopped_blocks("<!here> Ann"), "plain_text"),
            ["Mirroring of this email thread was stopped by <!here> Ann. New emails will not appear here."],
        )
        self.assertEqual(
            _texts(resumed_blocks("Ann"), "plain_text"), ["Mirroring of this email thread was resumed by Ann."]
        )
