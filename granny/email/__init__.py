"""Email management — Mailjet + AWS WorkMail."""

from granny.email.mailjet import MailjetClient
from granny.email.mailjet_contacts import MailjetContactClient
from granny.email.workmail import WorkMailManager

__all__ = ["MailjetClient", "MailjetContactClient", "WorkMailManager"]
