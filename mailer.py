import os
import smtplib
import threading
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from email.mime.image import MIMEImage
from flask import render_template

class Mailer:
    def __init__(self, app=None):
        self.app = app
        if app:
            self.init_app(app)

    def init_app(self, app):
        self.app = app

    def status(self):
        if not self.app:
            return {"server": "unknown", "security": "unknown", "username": "unknown", "sender": "unknown"}
        config = self.app.config
        security = "None"
        if config.get("MAIL_USE_TLS"):
            security = "TLS"
        elif config.get("MAIL_USE_SSL"):
            security = "SSL"
        return {
            "server": config.get("MAIL_SERVER", "smtp.gmail.com"),
            "security": security,
            "username": config.get("MAIL_USERNAME") or "Not configured",
            "sender": config.get("MAIL_SENDER_EMAIL") or "Not configured"
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
        # (especially during live auction bidding) return instantly without hitting timeouts.
        serverless = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))
        if serverless:
            try:
                self._send_raw(to, subject, html_body, txt_body)
                return True
            except Exception as e:
                self.app.logger.error(f"Failed to send email to {to}: {e}")
                return False
        else:
            def _async_worker():
                try:
                    self._send_raw(to, subject, html_body, txt_body)
                except Exception as e:
                    self.app.logger.error(f"Async email to {to} failed: {e}")

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
            self._send_raw(to, subject, html_body, txt_body)
            return True, "Email sent successfully"
        except Exception as e:
            return False, str(e)

    def _send_raw(self, to, subject, html_body, txt_body):
        if not to or "@" not in str(to):
            self.app.logger.warning(f"Skipping email send: invalid recipient '{to}'")
            return

        config = self.app.config
        server_addr = config.get("MAIL_SERVER", "smtp.gmail.com")
        port = config.get("MAIL_PORT", 587)
        username = config.get("MAIL_USERNAME")
        password = config.get("MAIL_PASSWORD")
        sender = config.get("MAIL_SENDER_EMAIL") or username

        if not username or not password:
            raise ValueError("MAIL_USERNAME and MAIL_PASSWORD must be configured in environment.")

        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = f"{config.get('MAIL_SENDER_NAME', 'GM League')} <{sender}>"
        msg["To"] = to

        if config.get("MAIL_ADMIN_BCC"):
            msg["Bcc"] = config.get("MAIL_ADMIN_BCC")

        msg.attach(MIMEText(txt_body, "plain"))
        msg.attach(MIMEText(html_body, "html"))

        # Attach logo if requested and exists
        logo_path = config.get("MAIL_LOGO_PATH")
        if logo_path and os.path.exists(logo_path):
            try:
                with open(logo_path, "rb") as f:
                    logo_img = MIMEImage(f.read())
                logo_img.add_header("Content-ID", "<brandlogo>")
                logo_img.add_header("X-Attachment-Id", "brandlogo")
                logo_img.add_header("Content-Disposition", "inline", filename=os.path.basename(logo_path))
                msg.attach(logo_img)
            except Exception as e:
                self.app.logger.warning(f"Could not attach logo to mail: {e}")

        # Connect to SMTP Server
        if config.get("MAIL_USE_SSL"):
            server = smtplib.SMTP_SSL(server_addr, port, timeout=config.get("MAIL_TIMEOUT", 20))
        else:
            server = smtplib.SMTP(server_addr, port, timeout=config.get("MAIL_TIMEOUT", 20))
            if config.get("MAIL_USE_TLS"):
                server.starttls()

        server.login(username, password)
        recipients = [to]
        if config.get("MAIL_ADMIN_BCC"):
            recipients.append(config.get("MAIL_ADMIN_BCC"))
        server.sendmail(sender, recipients, msg.as_string())
        server.quit()
