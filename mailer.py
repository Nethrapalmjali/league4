import base64
import json
import os
import smtplib
import socket
import threading
import urllib.error
import urllib.request
from email.header import Header
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr

from flask import render_template


class Mailer:
    def __init__(self, app=None):
        self.app = app
        self.last_error = None
        if app:
            self.init_app(app)

    def init_app(self, app):
        self.app = app

    def status(self):
        if not self.app:
            return {
                "server": "unknown",
                "security": "unknown",
                "username": "unknown",
                "sender": "unknown",
                "mode": "unknown",
                "provider": "unknown",
            }
        config = self.app.config
        enabled = config.get("MAIL_ENABLED", True)
        if not enabled:
            mode = "disabled"
        elif config.get("RESEND_API_KEY"):
            mode = "live (Resend API)"
        elif config.get("BREVO_API_KEY"):
            mode = "live (Brevo API)"
        elif config.get("MAIL_USERNAME") and config.get("MAIL_PASSWORD"):
            mode = "live (SMTP)"
        else:
            mode = "not configured"

        port = int(config.get("MAIL_PORT", 587))
        security = "None"
        if config.get("MAIL_USE_SSL") or port == 465:
            security = "SSL"
        elif config.get("MAIL_USE_TLS") or port == 587:
            security = "TLS"

        provider = "SMTP"
        if config.get("RESEND_API_KEY"):
            provider = "Resend HTTP API"
        elif config.get("BREVO_API_KEY"):
            provider = "Brevo HTTP API"

        return {
            "mode": mode,
            "provider": provider,
            "server": config.get("MAIL_SERVER", "smtp.gmail.com"),
            "port": port,
            "security": security,
            "username": config.get("MAIL_USERNAME") or "Not configured",
            "sender": config.get("MAIL_SENDER_EMAIL") or config.get("MAIL_USERNAME") or "Not configured",
            "last_error": self.last_error,
        }

    def send_template(self, to, subject, template, context):
        if not self.app:
            return False
        # If mail is disabled, don't send
        if not self.app.config.get("MAIL_ENABLED", True):
            return False

        # Render html/txt templates if they exist, otherwise use fallback text
        try:
            html_body = render_template(f"emails/{template}.html", **context)
        except Exception:
            try:
                html_body = render_template(f"{template}.html", **context)
            except Exception:
                html_body = f"Hello, this is a notification for {subject}. Context: {context}"

        try:
            txt_body = render_template(f"emails/{template}.txt", **context)
        except Exception:
            try:
                txt_body = render_template(f"{template}.txt", **context)
            except Exception:
                txt_body = html_body

        if self.app.config.get("MAIL_SUPPRESS_SEND", False):
            self.app.logger.info(f"Suppressed sending email to {to}: {subject}")
            return True

        # Send email asynchronously in a background daemon thread so HTTP requests
        # return instantly without hitting timeouts.
        serverless = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
        if serverless:
            try:
                self._dispatch_send(to, subject, html_body, txt_body)
                self.last_error = None
                return True
            except Exception as e:
                self.last_error = str(e)
                self.app.logger.error(f"Failed to send email to {to}: {e}", exc_info=True)
                return False
        else:
            def _async_worker():
                try:
                    self._dispatch_send(to, subject, html_body, txt_body)
                    self.last_error = None
                except Exception as e:
                    self.last_error = str(e)
                    self.app.logger.error(f"Async email to {to} failed: {e}", exc_info=True)

            t = threading.Thread(target=_async_worker, daemon=True)
            t.start()
            return True

    def send_now(self, to, subject, template, context):
        # Synchronous send
        try:
            html_body = render_template(f"emails/{template}.html", **context)
        except Exception:
            try:
                html_body = render_template(f"{template}.html", **context)
            except Exception:
                html_body = f"Hello, this is a test email. Context: {context}"

        try:
            txt_body = render_template(f"emails/{template}.txt", **context)
        except Exception:
            try:
                txt_body = render_template(f"{template}.txt", **context)
            except Exception:
                txt_body = html_body

        try:
            detail = self._dispatch_send(to, subject, html_body, txt_body)
            self.last_error = None
            return True, detail
        except Exception as e:
            self.last_error = str(e)
            self.app.logger.error(f"Synchronous email to {to} failed: {e}", exc_info=True)
            return False, str(e)

    def _dispatch_send(self, to, subject, html_body, txt_body):
        clean_to = str(to).strip()
        if not clean_to or "@" not in clean_to:
            raise ValueError(f"Invalid recipient email address: '{to}'")

        config = self.app.config
        resend_key = (config.get("RESEND_API_KEY") or "").strip()
        brevo_key = (config.get("BREVO_API_KEY") or "").strip()

        if resend_key:
            return self._send_resend(clean_to, subject, html_body, txt_body, resend_key)
        elif brevo_key:
            return self._send_brevo(clean_to, subject, html_body, txt_body, brevo_key)
        else:
            return self._send_smtp(clean_to, subject, html_body, txt_body)

    def _send_resend(self, to, subject, html_body, txt_body, api_key):
        config = self.app.config
        sender_name = config.get("MAIL_SENDER_NAME", "GM League Season 4")
        sender_email = (config.get("MAIL_SENDER_EMAIL") or config.get("MAIL_USERNAME") or "onboarding@resend.dev").strip()

        # Resend requires a verified custom domain unless sending from onboarding@resend.dev
        from_header = config.get("RESEND_FROM") or f"{sender_name} <{sender_email}>"
        if ("@gmail.com" in from_header or "@yahoo." in from_header) and not config.get("RESEND_FROM"):
            from_header = f"{sender_name} <onboarding@resend.dev>"

        payload = {
            "from": from_header,
            "to": [to],
            "subject": subject,
            "html": html_body,
            "text": txt_body,
        }

        logo_path = config.get("MAIL_LOGO_PATH")
        if logo_path and os.path.exists(logo_path):
            try:
                with open(logo_path, "rb") as f:
                    payload["attachments"] = [{
                        "filename": os.path.basename(logo_path),
                        "content": base64.b64encode(f.read()).decode("utf-8"),
                    }]
            except Exception as e:
                self.app.logger.warning(f"Could not attach logo to Resend email: {e}")

        req = urllib.request.Request(
            "https://api.resend.com/emails",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return f"Sent successfully via Resend HTTP API (id: {data.get('id', 'ok')})"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Resend API error ({e.code}): {err_body}") from e

    def _send_brevo(self, to, subject, html_body, txt_body, api_key):
        config = self.app.config
        sender_name = config.get("MAIL_SENDER_NAME", "GM League Season 4")
        sender_email = (config.get("MAIL_SENDER_EMAIL") or config.get("MAIL_USERNAME")).strip()

        payload = {
            "sender": {"name": sender_name, "email": sender_email},
            "to": [{"email": to}],
            "subject": subject,
            "htmlContent": html_body,
            "textContent": txt_body,
        }

        logo_path = config.get("MAIL_LOGO_PATH")
        if logo_path and os.path.exists(logo_path):
            try:
                with open(logo_path, "rb") as f:
                    payload["attachment"] = [{
                        "name": os.path.basename(logo_path),
                        "content": base64.b64encode(f.read()).decode("utf-8"),
                    }]
            except Exception as e:
                self.app.logger.warning(f"Could not attach logo to Brevo email: {e}")

        req = urllib.request.Request(
            "https://api.brevo.com/v3/smtp/email",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "api-key": api_key,
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                return f"Sent successfully via Brevo HTTP API (id: {data.get('messageId', 'ok')})"
        except urllib.error.HTTPError as e:
            err_body = e.read().decode("utf-8", errors="ignore")
            raise RuntimeError(f"Brevo API error ({e.code}): {err_body}") from e

    def _send_smtp(self, to, subject, html_body, txt_body):
        config = self.app.config
        server_addr = config.get("MAIL_SERVER", "smtp.gmail.com")
        port = int(config.get("MAIL_PORT", 587))
        username = (config.get("MAIL_USERNAME") or "").strip()
        password = (config.get("MAIL_PASSWORD") or "").strip()
        sender = (config.get("MAIL_SENDER_EMAIL") or username).strip()
        sender_name = config.get("MAIL_SENDER_NAME", "GM League Season 4")

        if not username or not password:
            raise ValueError(
                "SMTP credentials missing: MAIL_USERNAME and MAIL_PASSWORD must be configured in environment (.env)."
            )

        # RFC 2387 compliant MIME structure for HTML with embedded inline image
        logo_path = config.get("MAIL_LOGO_PATH")
        has_logo = logo_path and os.path.exists(logo_path)

        if has_logo:
            msg = MIMEMultipart("related")
            alt = MIMEMultipart("alternative")
            alt.attach(MIMEText(txt_body, "plain", "utf-8"))
            alt.attach(MIMEText(html_body, "html", "utf-8"))
            msg.attach(alt)

            try:
                with open(logo_path, "rb") as f:
                    logo_img = MIMEImage(f.read())
                logo_img.add_header("Content-ID", "<brandlogo>")
                logo_img.add_header("X-Attachment-Id", "brandlogo")
                logo_img.add_header("Content-Disposition", "inline", filename=os.path.basename(logo_path))
                msg.attach(logo_img)
            except Exception as e:
                self.app.logger.warning(f"Could not attach logo to mail: {e}")
        else:
            msg = MIMEMultipart("alternative")
            msg.attach(MIMEText(txt_body, "plain", "utf-8"))
            msg.attach(MIMEText(html_body, "html", "utf-8"))

        msg["Subject"] = Header(subject, "utf-8")
        msg["From"] = formataddr((str(Header(sender_name, "utf-8")), sender))
        msg["To"] = to

        reply_to = config.get("MAIL_REPLY_TO")
        if reply_to:
            msg["Reply-To"] = reply_to

        admin_bcc = (config.get("MAIL_ADMIN_BCC") or "").strip()
        if admin_bcc:
            msg["Bcc"] = admin_bcc

        timeout = int(config.get("MAIL_TIMEOUT", 20))
        use_ssl = bool(config.get("MAIL_USE_SSL")) or port == 465
        use_tls = bool(config.get("MAIL_USE_TLS")) and not use_ssl

        try:
            if use_ssl:
                server = smtplib.SMTP_SSL(server_addr, port, timeout=timeout)
                server.ehlo()
            else:
                server = smtplib.SMTP(server_addr, port, timeout=timeout)
                server.ehlo()
                if use_tls:
                    server.starttls()
                    server.ehlo()

            server.login(username, password)
            recipients = [to]
            if admin_bcc and "@" in admin_bcc:
                recipients.append(admin_bcc)

            server.sendmail(sender, recipients, msg.as_string())
            try:
                server.quit()
            except Exception:
                pass
            return f"Sent successfully via SMTP ({server_addr}:{port})"

        except (socket.timeout, TimeoutError, smtplib.SMTPConnectError, OSError) as e:
            is_render = bool(os.getenv("RENDER") or os.getenv("RENDER_EXTERNAL_URL"))
            if is_render or "110" in str(e) or "timed out" in str(e).lower():
                raise RuntimeError(
                    f"SMTP connection to {server_addr}:{port} timed out. "
                    "Notice: Render Free Tier blocks outbound SMTP ports 25, 465, and 587. "
                    "To send emails on Render, add RESEND_API_KEY (from https://resend.com) or BREVO_API_KEY "
                    "in your Render Environment settings, or upgrade to a Render paid plan."
                ) from e
            raise RuntimeError(f"SMTP connection to {server_addr}:{port} failed: {e}") from e

        except smtplib.SMTPAuthenticationError as e:
            raise RuntimeError(
                f"SMTP Authentication failed for '{username}'. "
                "For Gmail, you MUST use a 16-character Google App Password (with 2-Step Verification enabled on the Google Account), "
                "not your personal account password."
            ) from e
