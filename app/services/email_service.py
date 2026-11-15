import asyncio
import smtplib
import ssl
from email.message import EmailMessage

from app.core.config import settings


def _send_reset_email(recipient: str, token: str) -> None:
    message = EmailMessage()
    message["From"] = settings.SMTP_FROM
    message["To"] = recipient
    message["Subject"] = "Nexa Wallet - Reset your password"
    message.set_content(
        "Use this reset token in Nexa Wallet:\n\n"
        f"{token}\n\n"
        "It expires in 15 minutes and can only be used once.\n"
        "If you didn't request this, ignore this email."
    )
    with smtplib.SMTP(settings.SMTP_HOST, settings.SMTP_PORT, timeout=10) as smtp:
        if settings.SMTP_STARTTLS:
            smtp.starttls(context=ssl.create_default_context())
        if settings.SMTP_USERNAME:
            smtp.login(
                settings.SMTP_USERNAME, settings.SMTP_PASSWORD.get_secret_value()
            )
        smtp.send_message(message)


async def send_reset_email(recipient: str, token: str) -> None:
    await asyncio.to_thread(_send_reset_email, recipient, token)
