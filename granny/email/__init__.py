"""Email management — Mailjet, AWS WorkMail, Stalwart."""

from granny.email.mailjet import MailjetClient
from granny.email.mailjet_contacts import MailjetContactClient
from granny.email.stalwart import StalwartClient, StalwartError
from granny.email.workmail import WorkMailManager

__all__ = [
    "MailjetClient",
    "MailjetContactClient",
    "StalwartClient",
    "StalwartError",
    "WorkMailManager",
]
