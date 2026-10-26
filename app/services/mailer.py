"""
mailer.py
─────────
Envío de correos con la API de Brevo (https://developers.brevo.com).
"""
import logging

import httpx

from app.core.config import settings

logger = logging.getLogger(__name__)

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"


def is_configured() -> bool:
    return bool(settings.BREVO_API_KEY and settings.MAIL_SENDER_EMAIL)


async def send_email(to: str, subject: str, html: str, text: str) -> None:
    """Envía un correo. Los errores se registran en el log, no se lanzan."""
    if not is_configured():
        logger.warning(f"Email to {to} not sent: BREVO_API_KEY / MAIL_SENDER_EMAIL missing")
        return
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                BREVO_SEND_URL,
                headers={"api-key": settings.BREVO_API_KEY, "accept": "application/json"},
                json={
                    "sender": {"name": settings.MAIL_SENDER_NAME, "email": settings.MAIL_SENDER_EMAIL},
                    "to": [{"email": to}],
                    "subject": subject,
                    "htmlContent": html,
                    "textContent": text,
                },
            )
            response.raise_for_status()
    except Exception as e:
        logger.error(f"Email to {to} failed: {e}")


async def send_password_reset_code(to: str, code: str) -> None:
    minutes = settings.PASSWORD_RESET_MINUTES
    if settings.DEBUG and not is_configured():
        logger.info(f"[DEBUG] Password reset code for {to}: {code}")
    await send_email(
        to,
        subject=f"{code} es tu código para restablecer la contraseña de Sellin",
        text=(
            f"Tu código para restablecer la contraseña de Sellin es {code}.\n\n"
            f"Caduca en {minutes} minutos. Si no lo has pedido tú, ignora este correo: "
            "tu contraseña no cambiará."
        ),
        html=(
            '<div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;color:#1a1a1a">'
            '<h2 style="margin-bottom:8px">Restablecer contraseña</h2>'
            "<p>Escribe este código en la app de Sellin:</p>"
            f'<p style="font-size:32px;font-weight:bold;letter-spacing:8px;margin:24px 0">{code}</p>'
            f"<p>Caduca en {minutes} minutos.</p>"
            '<p style="color:#666;font-size:13px">Si no lo has pedido tú, ignora este correo: '
            "tu contraseña no cambiará.</p></div>"
        ),
    )
