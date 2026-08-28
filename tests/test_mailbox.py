from __future__ import annotations

import os
import tempfile
import unittest
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path
from unittest.mock import patch

from procurement.config import Settings
from procurement.db import Database
from procurement.mailbox import SmtpMailbox, parse_inbound_mail
from procurement.models import (
    CampaignCreate,
    LotCreate,
    LotItemCreate,
    MailDraftCreate,
    MailLinkUpdate,
    ProjectCreate,
    SupplierCreate,
)
from procurement.service import ConflictError, ProcurementService


class MailboxWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = str(Path(self.tempdir.name) / "procurement.db")
        self.db = Database(self.db_path)
        self.db.initialize()
        self.service = ProcurementService(self.db)
        self.project = self.service.create_project(
            ProjectCreate(
                name="Склад М4",
                region="Воронежская область",
                delivery_address="г Воронеж промзона 4",
            )
        )
        self.lot = self.service.create_lot(
            LotCreate(
                project_id=self.project["id"],
                title="Кровельные материалы",
                region="Воронежская область",
                delivery_address="г Воронеж промзона 4",
                response_deadline=date.today() + timedelta(days=5),
                items=[
                    LotItemCreate(
                        name="Профлист",
                        quantity=100,
                        unit="м2",
                    )
                ],
            )
        )
        self.supplier = self.service.create_supplier(
            SupplierCreate(
                name="Поставщик Кровля",
                region="Воронежская область",
                email="sales@supplier.example",
            )
        )

    def tearDown(self):
        self.tempdir.cleanup()

    def _incoming(self):
        message = EmailMessage()
        message["From"] = "Поставщик <sales@supplier.example>"
        message["To"] = "snab@stroydnepr.ru"
        message["Subject"] = f"Re: [RFQ-{self.lot['id']}] Коммерческое предложение"
        message["Message-ID"] = "<supplier-reply-1@example>"
        message.set_content("Добрый день направляем коммерческое предложение")
        message.add_attachment(
            b"%PDF-1.4\n% test quote",
            maintype="application",
            subtype="pdf",
            filename="КП поставщика.pdf",
        )
        return parse_inbound_mail(message.as_bytes())

    def test_parser_and_ingest_link_supplier_lot_and_attachment(self):
        incoming = self._incoming()
        self.assertEqual(incoming.sender, "sales@supplier.example")
        self.assertEqual(len(incoming.attachments), 1)

        stored = self.service.ingest_inbound_mail(
            incoming,
            mailbox_address="snab@stroydnepr.ru",
            imap_uid=41,
            imap_uidvalidity="777",
        )
        duplicate = self.service.ingest_inbound_mail(
            incoming,
            mailbox_address="snab@stroydnepr.ru",
            imap_uid=41,
            imap_uidvalidity="777",
        )

        self.assertEqual(stored["id"], duplicate["id"])
        self.assertEqual(stored["supplier_id"], self.supplier["id"])
        self.assertEqual(stored["lot_id"], self.lot["id"])
        self.assertEqual(stored["project_id"], self.project["id"])
        self.assertEqual(len(duplicate["attachments"]), 1)
        attachment = duplicate["attachments"][0]
        self.assertEqual(attachment["blocked_reason"], "")
        self.assertIsNotNone(attachment["source_document_id"])
        self.assertTrue(
            Path(self.service.get_mail_attachment(attachment["id"])["storage_path"]).is_file()
        )
        self.assertEqual(len(self.service.list_mail_messages(direction="inbound")), 1)

    def test_draft_requires_approval_before_delivery(self):
        document = self.service.register_source_document(
            filename="запрос.docx",
            content=b"PK\x03\x04 test docx",
            document_type="mail_attachment",
            content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            project_id=self.project["id"],
        )
        draft = self.service.create_mail_draft(
            MailDraftCreate(
                recipient="sales@supplier.example",
                subject="Запрос коммерческого предложения",
                body="Просим направить коммерческое предложение",
                supplier_id=self.supplier["id"],
                lot_id=self.lot["id"],
                source_document_ids=[document["id"]],
            ),
            mailbox_address="snab@stroydnepr.ru",
            actor="Снабженец",
        )
        self.assertEqual(draft["status"], "draft")
        self.assertTrue(draft["subject"].startswith(f"[RFQ-{self.lot['id']}]"))
        with self.assertRaisesRegex(ConflictError, "approved"):
            self.service.mail_delivery_payload(draft["id"])

        approved = self.service.approve_mail_draft(
            draft["id"], approved_by="Руководитель снабжения"
        )
        payload = self.service.mail_delivery_payload(approved["id"])
        self.assertEqual(payload["recipients"], ["sales@supplier.example"])
        self.assertEqual(len(payload["delivery_attachments"]), 1)
        sent = self.service.mark_mail_sent(
            approved["id"], actor="Руководитель снабжения"
        )
        self.assertEqual(sent["status"], "sent")
        with self.assertRaisesRegex(ConflictError, "approved"):
            self.service.mark_mail_sent(sent["id"], actor="Руководитель снабжения")

    def test_draft_rejects_missing_attachment_file(self):
        document = self.service.register_source_document(
            filename="запрос.pdf",
            content=b"%PDF-1.4\n% attachment",
            document_type="mail_attachment",
            content_type="application/pdf",
            project_id=self.project["id"],
        )
        Path(document["storage_path"]).unlink()

        with self.assertRaisesRegex(ConflictError, "unavailable"):
            self.service.create_mail_draft(
                MailDraftCreate(
                    recipient="sales@supplier.example",
                    subject="Запрос цены",
                    body="Просим направить коммерческое предложение",
                    source_document_ids=[document["id"]],
                ),
                mailbox_address="snab@stroydnepr.ru",
                actor="Снабженец",
            )

    def test_manual_link_rejects_project_from_another_lot(self):
        message = EmailMessage()
        message["From"] = "unknown@example.org"
        message["To"] = "snab@stroydnepr.ru"
        message["Subject"] = "КП без номера"
        message["Message-ID"] = "<unlinked@example.org>"
        message.set_content("Предложение")
        stored = self.service.ingest_inbound_mail(
            parse_inbound_mail(message.as_bytes()),
            mailbox_address="snab@stroydnepr.ru",
            imap_uid=42,
            imap_uidvalidity="777",
        )
        other = self.service.create_project(
            ProjectCreate(
                name="Другой проект",
                region="Липецкая область",
                delivery_address="г Липецк",
            )
        )
        with self.assertRaisesRegex(ValueError, "does not match"):
            self.service.link_mail_message(
                stored["id"],
                MailLinkUpdate(project_id=other["id"], lot_id=self.lot["id"]),
                actor="Снабженец",
            )

    def test_approved_rfq_outbox_becomes_one_approved_mail_message(self):
        campaign = self.service.create_campaign(
            self.lot["id"],
            CampaignCreate(supplier_ids=[self.supplier["id"]], channel="email"),
        )
        outbox = campaign["messages"][0]
        self.service.approve_message(outbox["id"], "Руководитель снабжения")

        first = self.service.create_mail_from_outbox(
            outbox["id"],
            mailbox_address="snab@stroydnepr.ru",
            actor="Руководитель снабжения",
        )
        second = self.service.create_mail_from_outbox(
            outbox["id"],
            mailbox_address="snab@stroydnepr.ru",
            actor="Руководитель снабжения",
        )

        self.assertEqual(first["id"], second["id"])
        self.assertEqual(first["status"], "approved")
        self.assertEqual(first["outbox_message_id"], outbox["id"])
        self.assertTrue(first["subject"].startswith(f"[RFQ-{self.lot['id']}]"))


class MailSettingsTests(unittest.TestCase):
    def test_mail_secret_file_and_timeweb_defaults(self):
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8") as secret:
            secret.write("mail-password\n")
            secret.flush()
            with patch.dict(
                os.environ,
                {
                    "PROCUREMENT_MAIL_ADDRESS": "snab@stroydnepr.ru",
                    "PROCUREMENT_MAIL_PASSWORD_FILE": secret.name,
                    "PROCUREMENT_MAIL_RECEIVE_ENABLED": "true",
                    "PROCUREMENT_MAIL_SEND_ENABLED": "false",
                },
                clear=True,
            ):
                settings = Settings.from_env()
        self.assertEqual(settings.mail_username, "snab@stroydnepr.ru")
        self.assertEqual(settings.mail_password, "mail-password")
        self.assertEqual(settings.mail_imap_host, "imap.timeweb.ru")
        self.assertEqual(settings.mail_imap_port, 993)
        self.assertEqual(settings.mail_smtp_host, "smtp.timeweb.ru")
        self.assertEqual(settings.mail_smtp_port, 465)
        self.assertTrue(settings.mail_receive_enabled)
        self.assertFalse(settings.mail_send_enabled)


class MailTransportTests(unittest.TestCase):
    @patch("procurement.mailbox.smtplib.SMTP_SSL")
    def test_smtp_uses_tls_login_thread_headers_and_attachment(self, smtp_ssl):
        client = smtp_ssl.return_value.__enter__.return_value
        mailbox = SmtpMailbox(
            host="smtp.timeweb.ru",
            port=465,
            username="snab@stroydnepr.ru",
            password="test-only-password",
        )

        mailbox.send(
            sender="snab@stroydnepr.ru",
            recipient="sales@supplier.example",
            subject="Re: [RFQ-7] Запрос цены",
            body="Добрый день",
            message_id="<outbound-7@stroydnepr.ru>",
            in_reply_to="<supplier-7@example>",
            references=("<root-7@stroydnepr.ru>", "<supplier-7@example>"),
            attachments=(
                {
                    "filename": "запрос.pdf",
                    "content_type": "application/pdf",
                    "content": b"%PDF-1.4\n",
                },
            ),
        )

        client.login.assert_called_once_with(
            "snab@stroydnepr.ru", "test-only-password"
        )
        sent_message = client.send_message.call_args.args[0]
        self.assertEqual(sent_message["In-Reply-To"], "<supplier-7@example>")
        self.assertIn("<root-7@stroydnepr.ru>", sent_message["References"])
        self.assertEqual(sent_message.get_payload()[-1].get_filename(), "запрос.pdf")


if __name__ == "__main__":
    unittest.main()
