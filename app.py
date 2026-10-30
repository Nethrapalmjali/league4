"""GM League Season 4 - Event Management System.

Flask application powering player registration, franchise (team owner)
registration, the live auction, the admin console and the SMTP email
automation that confirms every one of those actions.

Configuration lives in ``.env`` (see ``.env.example``); nothing secret is
hardcoded here.
"""

from __future__ import annotations

from collections import Counter
import os
import re
import secrets
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timedelta
from functools import wraps
from io import BytesIO
from urllib.parse import quote

# xlsxwriter is imported lazily inside excel_response() to keep cold start fast
from dotenv import load_dotenv
from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    url_for,
)
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import create_engine, inspect as sa_inspect, text
from sqlalchemy.pool import NullPool
from werkzeug.security import check_password_hash
from werkzeug.utils import secure_filename

from mailer import Mailer

load_dotenv()


# --------------------------------------------------------------------------- #
# Small env helpers
# --------------------------------------------------------------------------- #
def env_str(key, default=""):
    value = os.getenv(key)
    return default if value is None else value.strip()


def env_bool(key, default=False):
    value = os.getenv(key)
    if value is None or not value.strip():
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(key, default):
    try:
        return int(os.getenv(key, "").strip() or default)
    except ValueError:
        return default


# True on Vercel (and comparable serverless hosts), where the deployment bundle
# is mounted read-only, only /tmp is writable, and the instance is frozen the
# moment a response is returned. Everything that writes to disk or relies on a
# background thread has to behave differently there.
SERVERLESS = bool(os.getenv("VERCEL") or os.getenv("AWS_LAMBDA_FUNCTION_NAME"))

# Supabase's direct connection host publishes an AAAA record and no A record.
# Serverless platforms (Vercel included) have no IPv6 egress, so connecting
# there fails with "Cannot assign requested address" no matter how correct the
# credentials are. The Supavisor pooler is the IPv4 route and is what a
# serverless app should use anyway.
SUPABASE_DIRECT_HOST_RE = re.compile(r"@db\.([a-z0-9]+)\.supabase\.co(?::\d+)?/", re.I)


def normalise_database_url(url):
    """Make a database URL usable from SQLAlchemy 2 on a serverless host."""
    if not url:
        return url
    if SERVERLESS and url.startswith("sqlite:///") and not url.startswith("sqlite:////tmp/"):
        return "sqlite:////tmp/league.db"
    # SQLAlchemy 2 dropped the legacy "postgres://" scheme that several
    # dashboards still hand out.
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    if url.startswith("postgresql://"):
        if "sslmode=" not in url:
            url += ("&" if "?" in url else "?") + "sslmode=require"
        match = SUPABASE_DIRECT_HOST_RE.search(url)
        if match and SERVERLESS:
            app.logger.critical(
                "DATABASE_URL points at the Supabase direct connection host "
                "db.%s.supabase.co, which is IPv6-only. This platform has no "
                "IPv6 egress, so every connection will fail with 'Cannot assign "
                "requested address'. Use the Supavisor pooler instead: user "
                "postgres.%s at aws-<n>-<region>.pooler.supabase.com:6543.",
                match.group(1), match.group(1),
            )
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]
    return url


# --------------------------------------------------------------------------- #
# App + configuration
# --------------------------------------------------------------------------- #
if SERVERLESS:
    import tempfile
    _inst_path = os.path.abspath("/tmp") if os.name != "nt" else tempfile.gettempdir()
    app = Flask(__name__, instance_path=_inst_path)
else:
    app = Flask(__name__)

# A per-process random key would be regenerated on every cold start, silently
# invalidating every session and CSRF token across serverless instances — so
# logins and form posts fail at random. Random is fine for local dev only.
_secret_key = env_str("SECRET_KEY", "gml_s4_master_secret_2026_super_safe_token_9827341289")
if not _secret_key:
    _secret_key = "gml_s4_master_secret_2026_super_safe_token_9827341289"


def resolve_database_uri():
    """Determine database URI with automatic, resilient fallback to SQLite."""
    default_supabase = (
        "postgresql://postgres.bqoszyelbcxrqojvutva:Rachana%401237@aws-0-ap-southeast-2.pooler.supabase.com:5432/postgres?sslmode=require"
    )
    raw_url = env_str("DATABASE_URL", default_supabase)
    if raw_url:
        normalized = normalise_database_url(raw_url)
        if normalized.startswith("postgresql"):
            try:
                test_engine = create_engine(
                    normalized,
                    connect_args={"connect_timeout": 5},
                    poolclass=NullPool,
                )
                with test_engine.connect() as conn:
                    conn.execute(text("SELECT 1"))
                app.logger.info("Database: connected successfully to PostgreSQL.")
                return normalized
            except Exception as exc:
                app.logger.warning(
                    "Database: PostgreSQL connection failed (%s). "
                    "Falling back to SQLite to maintain zero downtime.",
                    exc,
                )
        else:
            return normalized

    if SERVERLESS:
        fallback = "sqlite:////tmp/league.db"
    else:
        instance_dir = os.path.join(app.root_path, "instance")
        os.makedirs(instance_dir, exist_ok=True)
        fallback = f"sqlite:///{os.path.join(instance_dir, 'league.db').replace(os.sep, '/')}"
    app.logger.info("Database: using SQLite (%s).", fallback)
    return fallback


_db_uri = resolve_database_uri()

app.config.update(
    SECRET_KEY=_secret_key,
    SQLALCHEMY_DATABASE_URI=_db_uri,
    SQLALCHEMY_TRACK_MODIFICATIONS=False,
    UPLOAD_FOLDER=os.path.join(app.root_path, "static", "player_photos"),
    TEAM_LOGO_FOLDER=os.path.join(app.root_path, "static", "team_logos"),
    OWNER_PHOTO_FOLDER=os.path.join(app.root_path, "static", "owner_photos"),
    MAX_CONTENT_LENGTH=env_int("MAX_UPLOAD_MB", 8) * 1024 * 1024,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_bool("SESSION_COOKIE_SECURE", False),
    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),
    DEBUG=env_bool("FLASK_DEBUG", False),
    PROPAGATE_EXCEPTIONS=False,
)

if _db_uri.startswith("postgresql"):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": NullPool,
        "connect_args": {"connect_timeout": 5 if SERVERLESS else 10},
    }

if SERVERLESS:
    app.config.update(
        DEBUG=False,
        PROPAGATE_EXCEPTIONS=False,
        SESSION_COOKIE_SECURE=True,
    )

# ---- Email automation -----------------------------------------------------
app.config.update(
    MAIL_ENABLED=env_bool("MAIL_ENABLED", True),
    MAIL_SUPPRESS_SEND=env_bool("MAIL_SUPPRESS_SEND", False),
    RESEND_API_KEY=env_str("RESEND_API_KEY"),
    RESEND_FROM=env_str("RESEND_FROM"),
    BREVO_API_KEY=env_str("BREVO_API_KEY"),
    GMAIL_WEBHOOK_URL=env_str("GMAIL_WEBHOOK_URL"),
    MAIL_SERVER=env_str("MAIL_SERVER", "smtp.gmail.com"),
    MAIL_PORT=env_int("MAIL_PORT", 587),
    MAIL_USE_TLS=env_bool("MAIL_USE_TLS", True),
    MAIL_USE_SSL=env_bool("MAIL_USE_SSL", False),
    MAIL_USERNAME=env_str("MAIL_USERNAME", "nethrapalmjali21@gmail.com"),
    MAIL_PASSWORD=env_str("MAIL_PASSWORD", "wzaziqufonusdtmu"),
    MAIL_SENDER_NAME=env_str("MAIL_SENDER_NAME", "GM League Season 4"),
    MAIL_SENDER_EMAIL=env_str("MAIL_SENDER_EMAIL", "nethrapalmjali21@gmail.com") or env_str("MAIL_USERNAME"),
    MAIL_REPLY_TO=env_str("MAIL_REPLY_TO"),
    MAIL_ADMIN_BCC=env_str("MAIL_ADMIN_BCC"),
    MAIL_TIMEOUT=env_int("MAIL_TIMEOUT", 20),
    MAIL_MAX_RETRIES=env_int("MAIL_MAX_RETRIES", 2),
    MAIL_WORKERS=env_int("MAIL_WORKERS", 2),
    MAIL_LOGO_PATH=os.path.join(app.root_path, "static", "logos", env_str("MAIL_LOGO_FILE", "gmulogo1.png")),
)


# ---- Branding (drives every page title, header and email) -----------------
BRAND = {
    "name": env_str("LEAGUE_NAME", "GM League Season 4"),
    "short": env_str("LEAGUE_SHORT", "GML S4"),
    "season": env_str("LEAGUE_SEASON", "Season 4"),
    "university": env_str("LEAGUE_UNIVERSITY", "GM University"),
    "tagline": env_str("LEAGUE_TAGLINE", "Season 4 - Where Legends Are Born"),
    "site_url": env_str("SITE_URL", "https://gmlweb.onrender.com"),
    "support_phone": env_str("SUPPORT_PHONE", "9066777171"),
    "support_phone_alt": env_str("SUPPORT_PHONE_ALT", "7483412359"),
    "support_email": env_str("SUPPORT_EMAIL"),
    "whatsapp_number": env_str("WHATSAPP_NUMBER", "8618139789"),
    "whatsapp_country_code": env_str("WHATSAPP_COUNTRY_CODE", "91"),
    "whatsapp_community_url": env_str("WHATSAPP_COMMUNITY_URL", "https://chat.whatsapp.com/B5lehw1CZpO9xww2fQ4V5h"),
    # Good-luck message signed off at the end of every automated email.
    "director_name": env_str("DIRECTOR_NAME", "Ajjaiah G B"),
    "director_title": env_str("DIRECTOR_TITLE", "Director - PE"),
    "director_message": env_str(
        "DIRECTOR_MESSAGE",
        "Play hard, play fair, and enjoy every minute of it. "
        "The whole Physical Education department is behind you this season - go make it count.",
    ),
}

# The header wordmark: the league name without the season suffix ("GM League"),
# so the season can sit beside it as its own badge.
BRAND["wordmark"] = env_str("LEAGUE_WORDMARK") or (
    BRAND["name"].replace(BRAND["season"], "").strip(" -") or BRAND["name"]
)


def resolve_site_url():
    """Return the live public website URL so email buttons always link to the real site."""
    explicit = env_str("SITE_URL")
    if explicit and not explicit.startswith("http://127.0.0.1") and not explicit.startswith("http://localhost"):
        return explicit.rstrip("/")
    try:
        if request and request.host_url:
            host = request.host_url.rstrip("/")
            if host.startswith("http://") and ("onrender.com" in host or "vercel.app" in host):
                host = "https://" + host[len("http://"):]
            if not host.startswith("http://127.0.0.1") and not host.startswith("http://localhost"):
                return host
    except Exception:
        pass
    render_url = env_str("RENDER_EXTERNAL_URL")
    if render_url:
        return render_url.rstrip("/")
    vercel_url = env_str("VERCEL_URL")
    if vercel_url:
        if not vercel_url.startswith("http"):
            vercel_url = "https://" + vercel_url
        return vercel_url.rstrip("/")
    return "https://gmlweb.onrender.com"


def get_brand():
    b = dict(BRAND)
    b["site_url"] = resolve_site_url()
    return b


# Both helpline numbers, in one list so no template has to know there are two.
BRAND["support_phones"] = [p for p in (BRAND["support_phone"], BRAND["support_phone_alt"]) if p]
BRAND["support_phones_text"] = " or ".join(BRAND["support_phones"])
# wa.me needs the full international number with no "+" or spaces.
BRAND["whatsapp_url"] = (
    f"https://wa.me/{BRAND['whatsapp_country_code']}{BRAND['whatsapp_number']}"
    f"?text={quote('Hi! I have a question about ' + BRAND['name'] + '.')}"
    if BRAND["whatsapp_number"]
    else ""
)

ADMIN_USERNAME = env_str("ADMIN_USERNAME", "nandu")
ADMIN_PASSWORD = env_str("ADMIN_PASSWORD", "Mkan@6767")
ADMIN_PASSWORD_HASH = env_str("ADMIN_PASSWORD_HASH")

ALLOWED_EXTENSIONS = {"png", "jpg", "jpeg", "gif", "webp"}
IMAGE_MAGIC = (b"\x89PNG", b"\xff\xd8\xff", b"GIF87a", b"GIF89a", b"RIFF")

SPORTS = ["Football", "Basketball", "Kabaddi", "Badminton"]
SPORT_CODES = {
    "Football": "FB",
    "Basketball": "BB",
    "Kabaddi": "KB",
    "Badminton": "BD",
}
POSITIONS = {
    "Football": ["Goalkeeper", "Defender", "Midfielder", "Forward"],
    "Basketball": ["Point Guard", "Shooting Guard", "Small Forward", "Power Forward", "Center"],
    "Kabaddi": ["Raider", "Defender", "All-Rounder"],
    "Badminton": ["Singles", "Doubles"],
}
# Football and Kabaddi run only Men's category; Basketball and Badminton feature Men's and Women's
MENS_ONLY_SPORTS = {"Football", "Kabaddi"}
OWNER_SPORT_OPTIONS = [
    "Football – Men",
    "Basketball – Men",
    "Basketball – Women",
    "Kabaddi – Men",
    "Badminton – Men",
    "Badminton – Women",
]
DEFAULT_BUDGET = env_int("DEFAULT_TEAM_BUDGET", 1000000)

db = SQLAlchemy(app)
migrate = Migrate(app, db)
mailer = Mailer(app)


def format_inr(number, prefix="₹"):
    """Format any integer or float into the Indian numbering system (e.g. ₹10,00,000)."""
    if number is None:
        return f"{prefix}0" if prefix else "0"
    try:
        val = int(round(float(number)))
    except (ValueError, TypeError):
        return f"{prefix}0" if prefix else "0"

    is_negative = val < 0
    s = str(abs(val))
    if len(s) <= 3:
        res = s
    else:
        last3 = s[-3:]
        remaining = s[:-3]
        groups = []
        while remaining:
            groups.append(remaining[-2:])
            remaining = remaining[:-2]
        groups.reverse()
        res = ",".join(groups) + "," + last3

    if is_negative:
        res = "-" + res
    return f"{prefix}{res}" if prefix else res


def rupees(amount):
    return format_inr(amount, prefix="₹")


app.jinja_env.filters["inr"] = lambda v: format_inr(v, prefix="₹")
app.jinja_env.filters["inr_num"] = lambda v: format_inr(v, prefix="")
app.jinja_env.globals["rupees"] = rupees
app.jinja_env.globals["format_inr"] = format_inr


def parse_sport_gender(val: str | None) -> tuple[str, str] | tuple[None, None]:
    """Parse 'Basketball – Men' or 'Basketball(Women)' or 'Basketball' into ('Basketball', 'Men')."""
    if not val:
        return None, None
    val_clean = val.replace("–", "-").replace("—", "-").strip()
    m = re.match(r"^([a-zA-Z\s]+)(?:[\-\(]\s*(Men|Women|Male|Female)\)?)?$", val_clean, re.IGNORECASE)
    if m:
        sport = m.group(1).strip().capitalize()
        if sport not in SPORTS:
            return None, None
        gender_raw = m.group(2)
        gender = ("Women" if gender_raw.lower() in ("women", "female") else "Men") if gender_raw else "Men"
        return sport, gender
    return None, None


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class Player(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    sport = db.Column(db.String(50), nullable=False)
    branch = db.Column(db.String(50))
    usn = db.Column(db.String(20))
    email = db.Column(db.String(120))
    college_name = db.Column(db.String(100))
    position = db.Column(db.String(50))
    contact = db.Column(db.String(20))
    photo = db.Column(db.String(500))
    achievements = db.Column(db.Text)
    experience = db.Column(db.String(50))
    gender = db.Column(db.String(10))
    sold = db.Column(db.Boolean, default=False)
    unsold = db.Column(db.Boolean, default=False)
    bid_amount = db.Column(db.Integer)
    sold_to = db.Column(db.String(100))
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @property
    def reg_code(self):
        return f"{BRAND['short'].replace(' ', '')}-{SPORT_CODES.get(self.sport, 'GM')}-{self.id:04d}"


class TeamOwner(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    team_name = db.Column(db.String(100), nullable=False, unique=True)
    sports = db.Column(db.String(255), nullable=False)
    budget = db.Column(db.Integer, default=DEFAULT_BUDGET)
    contact = db.Column(db.String(20))
    team_logo = db.Column(db.String(500))
    designation = db.Column(db.String(100))
    email = db.Column(db.String(120))
    usn = db.Column(db.String(50))  # stores Employee ID
    manager_name = db.Column(db.String(100))
    manager_contact_number = db.Column(db.String(20))
    team_owner_photo = db.Column(db.String(500))
    registered_at = db.Column(db.DateTime, default=datetime.utcnow)

    wallets = db.relationship(
        "TeamWallet",
        backref="owner",
        cascade="all, delete-orphan",
        lazy="joined",
        order_by="TeamWallet.sport, TeamWallet.gender",
    )

    def __init__(self, **kwargs):
        super().__init__(**kwargs)

    @property
    def employee_id(self):
        return self.usn

    @employee_id.setter
    def employee_id(self, val):
        self.usn = val

    @property
    def reg_code(self):
        return f"{BRAND['short'].replace(' ', '')}-TM-{self.id:04d}"

    def get_wallet(self, sport, gender):
        if not sport or not gender:
            return None
        norm_sport = sport.strip().capitalize()
        norm_gender = "Women" if gender.strip().lower() in ("women", "female") else "Men"
        for w in self.wallets:
            if w.sport == norm_sport and w.gender == norm_gender:
                return w
        return None

    @property
    def total_allocated_budget(self):
        if self.wallets:
            return sum(w.allocated_budget for w in self.wallets)
        return self.budget or DEFAULT_BUDGET

    @property
    def total_spent(self):
        if self.wallets:
            return sum(w.spent for w in self.wallets)
        return 0

    @property
    def total_balance(self):
        if self.wallets:
            return sum(w.balance for w in self.wallets)
        return self.budget or 0

    @property
    def categories_display(self):
        """Return a structured list of registered sport-gender categories and wallet details."""
        if self.wallets:
            return [
                {
                    "label": w.label,
                    "sport": w.sport,
                    "gender": w.gender,
                    "allocated": w.allocated_budget,
                    "spent": w.spent,
                    "balance": w.balance,
                    "allocated_display": format_inr(w.allocated_budget),
                    "spent_display": format_inr(w.spent),
                    "balance_display": format_inr(w.balance),
                }
                for w in self.wallets
            ]
        result = []
        for s in (self.sports or "").split(","):
            s = s.strip()
            if not s:
                continue
            sp, gn = parse_sport_gender(s)
            if sp:
                lbl = f"{sp} – {gn}"
                result.append({
                    "label": lbl,
                    "sport": sp,
                    "gender": gn,
                    "allocated": DEFAULT_BUDGET,
                    "spent": 0,
                    "balance": DEFAULT_BUDGET,
                    "allocated_display": format_inr(DEFAULT_BUDGET),
                    "spent_display": format_inr(0),
                    "balance_display": format_inr(DEFAULT_BUDGET),
                })
        return result

    def sync_budget_from_wallets(self):
        if self.wallets:
            self.budget = sum(w.balance for w in self.wallets)


class TeamWallet(db.Model):
    __tablename__ = "team_wallet"
    id = db.Column(db.Integer, primary_key=True)
    team_owner_id = db.Column(db.Integer, db.ForeignKey("team_owner.id", ondelete="CASCADE"), nullable=False)
    sport = db.Column(db.String(50), nullable=False)
    gender = db.Column(db.String(20), nullable=False)  # "Men" or "Women"
    allocated_budget = db.Column(db.Integer, default=DEFAULT_BUDGET, nullable=False)
    spent = db.Column(db.Integer, default=0, nullable=False)
    balance = db.Column(db.Integer, default=DEFAULT_BUDGET, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint("team_owner_id", "sport", "gender", name="uq_team_wallet_sport_gender"),
    )

    @property
    def label(self):
        return f"{self.sport} – {self.gender}"

    @property
    def balance_display(self):
        return format_inr(self.balance)

    @property
    def spent_display(self):
        return format_inr(self.spent)

    @property
    def allocated_display(self):
        return format_inr(self.allocated_budget)


class WalletTransaction(db.Model):
    __tablename__ = "wallet_transaction"
    id = db.Column(db.Integer, primary_key=True)
    wallet_id = db.Column(db.Integer, db.ForeignKey("team_wallet.id", ondelete="CASCADE"), nullable=False)
    team_owner_id = db.Column(db.Integer, db.ForeignKey("team_owner.id", ondelete="CASCADE"), nullable=False)
    player_id = db.Column(db.Integer, db.ForeignKey("player.id", ondelete="SET NULL"), nullable=True)
    sport = db.Column(db.String(50), nullable=False)
    gender = db.Column(db.String(20), nullable=False)
    action = db.Column(db.String(50), nullable=False)  # "INITIAL_ALLOCATION", "BID_PURCHASE", "BID_REFUND", "BUDGET_ADJUSTMENT"
    amount = db.Column(db.Integer, nullable=False)
    balance_before = db.Column(db.Integer, nullable=False)
    balance_after = db.Column(db.Integer, nullable=False)
    description = db.Column(db.String(255))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


def ensure_schema():
    """Ensure database tables, columns, and wallet records are up to date."""
    db.create_all()

    inspector = sa_inspect(db.engine)
    existing_tables = set(inspector.get_table_names())
    for model in (Player, TeamOwner, TeamWallet, WalletTransaction):
        table = model.__table__
        if table.name not in existing_tables:
            continue
        present = {col["name"] for col in inspector.get_columns(table.name)}
        for column in table.columns:
            if column.name in present:
                continue
            ddl = column.type.compile(dialect=db.engine.dialect)
            app.logger.info("Schema upgrade: adding %s.%s", table.name, column.name)
            with db.engine.begin() as conn:
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{column.name}" {ddl}'))

    try:
        with db.engine.begin() as conn:
            conn.execute(text('UPDATE player SET unsold = 0 WHERE unsold IS NULL'))
    except Exception:
        pass

    # Migrate / bootstrap wallets for all existing team owners
    try:
        owners = TeamOwner.query.all()
        for owner in owners:
            if not owner.wallets:
                sports_tokens = [s.strip() for s in (owner.sports or "").split(",") if s.strip()]
                seen_counts = {}
                canonical_categories = []
                for tok in sports_tokens:
                    sp, gn = parse_sport_gender(tok)
                    if not sp:
                        continue
                    count = seen_counts.get(sp, 0)
                    if tok.lower() == sp.lower():
                        gn = "Men" if count == 0 else "Women"
                    seen_counts[sp] = count + 1

                    cat_label = f"{sp} – {gn}"
                    if cat_label not in canonical_categories:
                        canonical_categories.append(cat_label)

                    w = TeamWallet.query.filter_by(team_owner_id=owner.id, sport=sp, gender=gn).first()
                    if not w:
                        w = TeamWallet(
                            team_owner_id=owner.id,
                            sport=sp,
                            gender=gn,
                            allocated_budget=DEFAULT_BUDGET,
                            spent=0,
                            balance=DEFAULT_BUDGET,
                        )
                        db.session.add(w)
                        db.session.flush()
                        tx = WalletTransaction(
                            wallet_id=w.id,
                            team_owner_id=owner.id,
                            sport=sp,
                            gender=gn,
                            action="INITIAL_ALLOCATION",
                            amount=DEFAULT_BUDGET,
                            balance_before=0,
                            balance_after=DEFAULT_BUDGET,
                            description=f"Initial budget allocation for {cat_label}",
                        )
                        db.session.add(tx)

                # Deduct any sold players already signed to this franchise
                sold_players = Player.query.filter_by(sold_to=owner.team_name, sold=True).all()
                for p in sold_players:
                    p_gender = "Women" if p.gender == "Female" else "Men"
                    w = TeamWallet.query.filter_by(team_owner_id=owner.id, sport=p.sport, gender=p_gender).first()
                    if w:
                        w.spent += (p.bid_amount or 0)
                        w.balance = max(0, w.allocated_budget - w.spent)

                if canonical_categories:
                    owner.sports = ", ".join(canonical_categories)
                owner.sync_budget_from_wallets()
        db.session.commit()
    except Exception as exc:
        app.logger.warning("Wallet migration warning: %s", exc)
        db.session.rollback()


def _prepare_upload_folders():
    """Create the upload directories, tolerating a read-only deployment."""
    for folder in ("UPLOAD_FOLDER", "TEAM_LOGO_FOLDER", "OWNER_PHOTO_FOLDER"):
        try:
            os.makedirs(app.config[folder], exist_ok=True)
        except OSError as exc:  # read-only bundle on Vercel - uploads degrade, see save_upload
            app.logger.warning("Upload folder %s unavailable: %s", folder, exc)


_schema_ready = False


def init_database():
    """Bring the schema up to date once per process, without ever raising.

    This used to run at import time. On Vercel that made the whole module
    un-importable whenever the database was unreachable - SQLAlchemy raised
    while ``api/index.py`` was still executing ``from app import app``, so the
    function crashed with FUNCTION_INVOCATION_FAILED before a single request
    was handled and no traceback ever reached a browser. Doing it on the first
    request instead means a database problem degrades to a normal 500 with a
    logged traceback, and every non-database page keeps working.
    """
    global _schema_ready
    if _schema_ready:
        return
    _schema_ready = True  # set first: a failure must not retry on every request
    try:
        with app.app_context():
            db.create_all()
            ensure_schema()
    except Exception as exc:  # noqa: BLE001 - startup must never take the app down
        app.logger.exception("Database initialisation failed: %s", exc)


@app.before_request
def _ensure_database_ready():
    init_database()


_prepare_upload_folders()

# Outside serverless the app owns its filesystem and database, so do the work up
# front and surface problems immediately rather than on the first page view.
if not SERVERLESS:
    init_database()


# --------------------------------------------------------------------------- #
# CSRF protection (session token, no extra dependency)
# --------------------------------------------------------------------------- #
def csrf_token():
    token = session.get("_csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["_csrf_token"] = token
    return token


@app.before_request
def protect_against_csrf():
    if request.method not in {"POST", "PUT", "PATCH", "DELETE"}:
        return None
    sent = request.form.get("_csrf_token") or request.headers.get("X-CSRF-Token", "")
    expected = session.get("_csrf_token", "")

    # For admin & bidding operations, enforce strict CSRF protection
    is_protected_admin = (
        request.path.startswith("/admin")
        or request.path.startswith("/bidding")
        or request.path.startswith("/api/admin")
    )

    if not expected or not secrets.compare_digest(str(sent), str(expected)):
        if is_protected_admin:
            if request.is_json or request.headers.get("X-CSRF-Token") is not None:
                return jsonify({"error": "Session expired. Reload the page and try again."}), 400
            flash("Your session expired. Please log in again.", "error")
            return redirect(request.referrer or url_for("admin_login"))

        # For public player and franchise registrations:
        # Mobile in-app browsers (WhatsApp/Instagram webviews) often drop or block session cookies.
        # If the form submitted a token, accept it so mobile users aren't blocked by session drops.
        if sent:
            return None

        flash("Your session expired. Please fill the form again.", "error")
        return redirect(request.referrer or url_for("index"))
    return None


@app.after_request
def set_security_headers(response):
    response.headers["X-Frame-Options"] = "SAMEORIGIN"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    return response


def media_url(path):
    if not path:
        return ""
    if path.startswith("http://") or path.startswith("https://"):
        return path
    return url_for("static", filename=path)


def parse_highlights(text):
    """Clean and split player achievements into formatted list of points."""
    if not text:
        return []
    raw_str = str(text).strip()
    if not raw_str or raw_str.lower() in {"none", "n/a", "nil", "verified registrant"}:
        return []
    parts = re.split(r"[\r\n]+|[;ΓÇó]+|(?<=[a-zA-Z0-9\)])\.\s+", raw_str)
    results = []
    for item in parts:
        cleaned = item.strip().strip(".-*ΓÇó \t")
        if cleaned and len(cleaned) >= 2:
            results.append(cleaned)
    return results or ([raw_str] if len(raw_str) > 2 else [])


app.jinja_env.filters["media_url"] = media_url
app.jinja_env.filters["parse_highlights"] = parse_highlights


@app.context_processor
def inject_globals():
    return {
        "csrf_token": csrf_token,
        "brand": get_brand(),
        "sports": SPORTS,
        "mens_only_sports": MENS_ONLY_SPORTS,
        "now_year": datetime.now().year,
        "media_url": media_url,
        "parse_highlights": parse_highlights,
    }


# --------------------------------------------------------------------------- #
# Validation + upload helpers (Supabase Storage Cloud + Local Fallback)
# --------------------------------------------------------------------------- #
SUPABASE_URL = env_str("SUPABASE_URL", "https://bqoszyelbcxrqojvutva.supabase.co")
SUPABASE_KEY = env_str("SUPABASE_KEY", "sb_publishable_zujftvHxzH1EuxAXwc9RRA_62_6kRUC")
SUPABASE_BUCKET = env_str("SUPABASE_BUCKET", "gml-media")

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[a-zA-Z]{2,}$")
PHONE_RE = re.compile(r"^[6-9]\d{9}$")


def clean(value, limit=200):
    return (value or "").strip()[:limit]


def normalise_phone(value):
    """Strip +91 / spaces / dashes so 10-digit validation is fair to users."""
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 12 and digits.startswith("91"):
        digits = digits[2:]
    elif len(digits) == 11 and digits.startswith("0"):
        digits = digits[1:]
    return digits


def looks_like_image(storage):
    """Cheap magic-byte sniff so a renamed .exe cannot land in static/."""
    head = storage.stream.read(12)
    storage.stream.seek(0)
    return head.startswith(IMAGE_MAGIC)


def allowed_file(filename):
    return "." in filename and filename.rsplit(".", 1)[1].lower() in ALLOWED_EXTENSIONS


def save_upload(storage, folder_key, subdir, label):
    """Validate and store an uploaded image.

    Uploads directly to Supabase Storage bucket so photos and logos remain
    100% intact across server restarts and deployments. Falls back to local
    disk storage if cloud upload is unreachable.
    Returns ``(public_url_or_relative_path, error)``.
    """
    if not storage or not storage.filename:
        return None, None
    if not allowed_file(storage.filename):
        return None, f"{label} must be a PNG, JPG, GIF or WEBP image."
    if not looks_like_image(storage):
        return None, f"{label} does not look like a real image file."

    extension = storage.filename.rsplit(".", 1)[1].lower()
    stem = secure_filename(label.replace(" ", "_")) or "upload"
    filename = f"{stem}_{datetime.now():%Y%m%d%H%M%S}_{uuid.uuid4().hex[:8]}.{extension}"

    file_bytes = storage.read()
    storage.seek(0)

    # 1. Upload to Supabase Storage for permanent persistence
    if SUPABASE_URL and SUPABASE_KEY:
        try:
            mime_map = {
                "png": "image/png",
                "jpg": "image/jpeg",
                "jpeg": "image/jpeg",
                "gif": "image/gif",
                "webp": "image/webp",
            }
            content_type = mime_map.get(extension, "application/octet-stream")
            storage_path = f"{subdir}/{filename}"
            upload_url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{SUPABASE_BUCKET}/{storage_path}"
            req = urllib.request.Request(
                upload_url,
                data=file_bytes,
                headers={
                    "apikey": SUPABASE_KEY,
                    "Authorization": f"Bearer {SUPABASE_KEY}",
                    "Content-Type": content_type,
                    "x-upsert": "true",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=12) as resp:
                if resp.status in (200, 201):
                    public_url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/public/{SUPABASE_BUCKET}/{storage_path}"
                    # Keep local cache if filesystem allows
                    try:
                        destination = os.path.join(app.config[folder_key], filename)
                        os.makedirs(os.path.dirname(destination), exist_ok=True)
                        with open(destination, "wb") as f:
                            f.write(file_bytes)
                    except Exception:
                        pass
                    return public_url, None
        except Exception as exc:
            app.logger.warning("Supabase Storage upload failed for %s, falling back to local: %s", label, exc)

    # 2. Local disk fallback
    destination = os.path.join(app.config[folder_key], filename)
    try:
        os.makedirs(os.path.dirname(destination), exist_ok=True)
        with open(destination, "wb") as f:
            f.write(file_bytes)
    except OSError as exc:
        app.logger.warning("Could not store %s upload: %s", label, exc)
        return None, None
    return f"{subdir}/{filename}", None


def delete_upload(relative_path, folder_key):
    if not relative_path:
        return
    if relative_path.startswith("http://") or relative_path.startswith("https://"):
        if SUPABASE_BUCKET in relative_path and SUPABASE_URL and SUPABASE_KEY:
            try:
                obj_path = relative_path.split(f"/{SUPABASE_BUCKET}/")[-1]
                del_url = f"{SUPABASE_URL.rstrip('/')}/storage/v1/object/{SUPABASE_BUCKET}/{obj_path}"
                req = urllib.request.Request(
                    del_url,
                    headers={
                        "apikey": SUPABASE_KEY,
                        "Authorization": f"Bearer {SUPABASE_KEY}",
                    },
                    method="DELETE",
                )
                urllib.request.urlopen(req, timeout=5)
            except Exception as exc:
                app.logger.warning("Could not remove Supabase object %s: %s", relative_path, exc)
        return

    path = os.path.join(app.config[folder_key], os.path.basename(relative_path))
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError as exc:
        app.logger.warning("Could not remove %s: %s", path, exc)


def rupees(amount):
    return f"Γé╣ {int(amount or 0):,}"


def category_of(player):
    """The league talks in Men/Women, not the raw Male/Female stored value."""
    return "Women" if player.gender == "Female" else "Men"


# --------------------------------------------------------------------------- #
# Email dispatch
# --------------------------------------------------------------------------- #
def has_logo():
    return os.path.isfile(app.config["MAIL_LOGO_PATH"])


def email_player_registered(player):
    return mailer.send_template(
        to=player.email,
        subject=f"You're registered - {player.sport} | {BRAND['name']}",
        template="player_registered",
        context={
            "player": player,
            "sport": player.sport,
            "reg_code": player.reg_code,
            "category": category_of(player),
            "brand": get_brand(),
            "logo_cid": has_logo(),
        },
    )


def email_owner_registered(owner):
    return mailer.send_template(
        to=owner.email,
        subject=f"{owner.team_name} is registered - {BRAND['name']}",
        template="owner_registered",
        context={"owner": owner, "reg_code": owner.reg_code, "brand": get_brand(), "logo_cid": has_logo()},
    )


def email_player_sold(player, owner, amount):
    cat_gender = "Women" if getattr(player, "gender", None) == "Female" else "Men"
    wallet = owner.get_wallet(player.sport, cat_gender)
    cat_budget = wallet.balance if wallet else (owner.budget or 0)

    mailer.send_template(
        to=player.email,
        subject=f"SOLD! {owner.team_name} signed you for {rupees(amount)}",
        template="player_sold",
        context={
            "player": player,
            "sport": player.sport,
            "team_name": owner.team_name,
            "owner_name": owner.name,
            "category": category_of(player),
            "amount_display": rupees(amount),
            "brand": get_brand(),
            "logo_cid": has_logo(),
        },
    )
    mailer.send_template(
        to=owner.email,
        subject=f"Signed: {player.name} for {rupees(amount)} - {owner.team_name}",
        template="owner_signing",
        context={
            "player": player,
            "owner": owner,
            "sport": player.sport,
            "team_name": owner.team_name,
            "category": category_of(player),
            "amount_display": rupees(amount),
            "budget_display": rupees(cat_budget),
            "brand": get_brand(),
            "logo_cid": has_logo(),
        },
    )


# --------------------------------------------------------------------------- #
# Access control
# --------------------------------------------------------------------------- #
# Defined here rather than beside the admin views: the auction room sits among
# the public routes below and needs this decorator, and a decorator has to
# exist before the route that uses it is read.
def admin_required(view):
    @wraps(view)
    def wrapper(*args, **kwargs):
        if not session.get("admin_logged_in"):
            is_ajax = (
                request.is_json
                or request.headers.get("X-Requested-With") == "XMLHttpRequest"
                or "application/json" in request.headers.get("Accept", "")
            )
            if is_ajax:
                return jsonify({"status": "error", "message": "Admin session expired. Please reload or log in again."}), 401
            flash("Admin login required.", "error")
            return redirect(url_for("admin_login", next=request.path))
        return view(*args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- #
# Public routes
# --------------------------------------------------------------------------- #
@app.route("/ping")
@app.route("/health")
def ping():
    return jsonify({
        "status": "ok",
        "service": BRAND["name"],
        "timestamp": datetime.utcnow().isoformat() + "Z",
    }), 200


@app.route("/")
def index():
    stats = {
        "players": Player.query.count(),
        "teams": TeamOwner.query.count(),
        "sports": len(SPORTS),
        "sold": Player.query.filter_by(sold=True).count(),
    }
    return render_template("index.html", stats=stats)


@app.route("/register_player/<sport>", methods=["GET", "POST"])
def register_player(sport):
    sport = sport.capitalize()
    if sport not in SPORTS:
        flash("That sport is not part of this season.", "error")
        return redirect(url_for("index"))

    positions = POSITIONS[sport]
    form = {}

    if request.method == "POST":
        form = {
            "name": clean(request.form.get("name"), 100),
            "branch": clean(request.form.get("branch"), 50),
            "usn": clean(request.form.get("usn"), 20).upper(),
            "email": clean(request.form.get("email"), 120).lower(),
            "college_name": clean(request.form.get("college_name"), 100),
            "position": clean(request.form.get("position"), 50),
            "contact": normalise_phone(request.form.get("contact")),
            "achievements": clean(request.form.get("achievements"), 2000),
            "experience": clean(request.form.get("experience"), 50),
            "gender": clean(request.form.get("gender"), 10),
        }

        errors = []
        if len(form["name"]) < 3:
            errors.append("Please enter your full name.")
        if not form["usn"]:
            errors.append("USN is required.")
        if not EMAIL_RE.match(form["email"]):
            errors.append("Please enter a valid email address - your confirmation is sent there.")
        if not PHONE_RE.match(form["contact"]):
            errors.append("Contact must be a valid 10-digit mobile number.")
        if form["position"] not in positions:
            errors.append(f"Please choose a valid {sport} position.")
        if form["gender"] not in {"Male", "Female"}:
            errors.append("Please select a category.")
        if form["experience"] not in {"Beginner", "Intermediate", "Pro"}:
            errors.append("Please select your experience level.")
        if sport in MENS_ONLY_SPORTS and form["gender"] == "Female":
            errors.append(f"{sport} runs as a men's event this season. Please pick another sport.")
        if not form["branch"]:
            errors.append("Branch is required.")
        if not form["college_name"]:
            errors.append("College name is required.")

        # Prevent dual/duplicate entries across USN, Email, and Mobile number
        duplicate_usn = Player.query.filter(
            db.func.upper(Player.usn) == form["usn"], Player.sport == sport
        ).first() if form["usn"] else None

        duplicate_email = Player.query.filter(
            db.func.lower(Player.email) == form["email"], Player.sport == sport
        ).first() if form["email"] else None

        duplicate_contact = Player.query.filter(
            Player.contact == form["contact"], Player.sport == sport
        ).first() if form["contact"] else None

        if duplicate_usn:
            errors.append(f"USN '{form['usn']}' is already registered for {sport} (Entry ID: {duplicate_usn.reg_code}).")
        elif duplicate_email:
            errors.append(f"Email '{form['email']}' is already registered for {sport} (Entry ID: {duplicate_email.reg_code}). You do not need to register again.")
        elif duplicate_contact:
            errors.append(f"Mobile number '{form['contact']}' is already registered for {sport} (Entry ID: {duplicate_contact.reg_code}). You are already registered.")

        photo_file = request.files.get("photo")
        if not photo_file or not photo_file.filename:
            errors.append("Player photo is required. Please upload a clear photo of yourself.")
            photo_path = None
        else:
            photo_path, photo_error = save_upload(
                photo_file, "UPLOAD_FOLDER", "player_photos", form["name"] or "player"
            )
            if photo_error:
                errors.append(photo_error)

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "register_player.html",
                sport=sport,
                positions=positions,
                form=form,
                mens_only=sport in MENS_ONLY_SPORTS,
            )

        player = Player(sport=sport, photo=photo_path, **form)
        try:
            db.session.add(player)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.exception("Player registration failed")
            flash(f"We could not save your registration: {exc}", "error")
            return render_template(
                "register_player.html",
                sport=sport,
                positions=positions,
                form=form,
                mens_only=sport in MENS_ONLY_SPORTS,
            )

        queued = email_player_registered(player)
        flash(
            f"You're registered for {sport}! Entry ID {player.reg_code}."
            + (f" A confirmation is on its way to {player.email}." if queued else ""),
            "success",
        )
        return redirect(url_for("registration_success", kind="player", code=player.reg_code))

    if not form and sport in MENS_ONLY_SPORTS:
        form = {"gender": "Male"}
    return render_template(
        "register_player.html",
        sport=sport,
        positions=positions,
        form=form,
        mens_only=sport in MENS_ONLY_SPORTS,
    )


@app.route("/team_owner_registration", methods=["GET", "POST"])
def team_owner_registration():
    form = {}
    selected_sports = []

    if request.method == "POST":
        usn_val = clean(request.form.get("employee_id") or request.form.get("usn"), 50).upper()
        form = {
            "name": clean(request.form.get("name"), 100),
            "team_name": clean(request.form.get("team_name"), 100),
            "usn": usn_val,
            "email": clean(request.form.get("email"), 120).lower(),
            "designation": clean(request.form.get("designation"), 100),
            "contact": normalise_phone(request.form.get("contact")),
            "manager_name": clean(request.form.get("manager_name"), 100),
            "manager_contact_number": normalise_phone(request.form.get("manager_contact_number")),
        }
        selected_sports = [s for s in request.form.getlist("sports") if s in OWNER_SPORT_OPTIONS]

        errors = []
        if len(form["name"]) < 3:
            errors.append("Please enter the owner's full name.")
        if not form["team_name"]:
            errors.append("Team name is required.")
        if not form["usn"]:
            errors.append("Employee ID is required.")
        if not EMAIL_RE.match(form["email"]):
            errors.append("Please enter a valid email address - your confirmation is sent there.")
        if not PHONE_RE.match(form["contact"]):
            errors.append("Contact must be a valid 10-digit mobile number.")
        if form["manager_contact_number"] and not PHONE_RE.match(form["manager_contact_number"]):
            errors.append("Manager contact must be a valid 10-digit mobile number.")
        if not selected_sports:
            errors.append("Select at least one sport your team will compete in.")
        if TeamOwner.query.filter(db.func.lower(TeamOwner.team_name) == form["team_name"].lower()).first():
            errors.append(f"The team name ΓÇ£{form['team_name']}ΓÇ¥ is already taken.")

        logo_path, logo_error = save_upload(
            request.files.get("team_logo"), "TEAM_LOGO_FOLDER", "team_logos", form["team_name"] or "team"
        )
        photo_path, photo_error = save_upload(
            request.files.get("team_owner_photo"), "OWNER_PHOTO_FOLDER", "owner_photos", form["name"] or "owner"
        )
        errors.extend(e for e in (logo_error, photo_error) if e)

        if errors:
            for message in errors:
                flash(message, "error")
            return render_template(
                "team_owner_registration.html",
                sport_options=OWNER_SPORT_OPTIONS,
                form=form,
                selected_sports=selected_sports,
            )

        # Parse selected categories into canonical (sport, gender) pairs
        valid_categories = []
        for s in selected_sports:
            sp, gn = parse_sport_gender(s)
            if sp and (sp, gn) not in valid_categories:
                valid_categories.append((sp, gn))

        canonical_labels = [f"{sp} – {gn}" for sp, gn in valid_categories]

        owner = TeamOwner(
            sports=", ".join(canonical_labels),
            budget=len(valid_categories) * DEFAULT_BUDGET,
            team_logo=logo_path,
            team_owner_photo=photo_path,
            **form,
        )
        try:
            db.session.add(owner)
            db.session.flush()

            # Create independent wallets of ₹10,00,000 for each selected category
            for sp, gn in valid_categories:
                w = TeamWallet(
                    team_owner_id=owner.id,
                    sport=sp,
                    gender=gn,
                    allocated_budget=DEFAULT_BUDGET,
                    spent=0,
                    balance=DEFAULT_BUDGET,
                )
                db.session.add(w)
                db.session.flush()
                tx = WalletTransaction(
                    wallet_id=w.id,
                    team_owner_id=owner.id,
                    sport=sp,
                    gender=gn,
                    action="INITIAL_ALLOCATION",
                    amount=DEFAULT_BUDGET,
                    balance_before=0,
                    balance_after=DEFAULT_BUDGET,
                    description=f"Initial purse allocation of ₹10,00,000 for {sp} – {gn}",
                )
                db.session.add(tx)

            owner.sync_budget_from_wallets()
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            app.logger.exception("Team owner registration failed")
            flash(f"We could not save your registration: {exc}", "error")
            return render_template(
                "team_owner_registration.html",
                sport_options=OWNER_SPORT_OPTIONS,
                form=form,
                selected_sports=selected_sports,
            )

        queued = email_owner_registered(owner)
        flash(
            f"{owner.team_name} is registered! Franchise ID {owner.reg_code}."
            + (f" A confirmation is on its way to {owner.email}." if queued else ""),
            "success",
        )
        return redirect(url_for("registration_success", kind="owner", code=owner.reg_code))

    return render_template(
        "team_owner_registration.html",
        sport_options=OWNER_SPORT_OPTIONS,
        form=form,
        selected_sports=selected_sports,
    )


@app.route("/registered/<kind>/<code>")
def registration_success(kind, code):
    if kind not in {"player", "owner"}:
        abort(404)
    return render_template("registration_success.html", kind=kind, code=code)


@app.route("/bidding/<sport>/<gender>", methods=["GET", "POST"])
@admin_required
def bidding(sport, gender):
    sport = sport.capitalize()
    if sport not in SPORTS:
        flash("That sport is not part of this season.", "error")
        return redirect(url_for("admin_dashboard"))

    g_raw = (gender or "").strip().lower()
    if g_raw in ("female", "women", "w"):
        if sport in MENS_ONLY_SPORTS:
            flash(f"{sport} features only Men's category this season.", "error")
            return redirect(url_for("admin_dashboard"))
        category_gender = "Women"
        player_gender = "Female"
        url_gender = "female"
    elif g_raw in ("male", "men", "m"):
        category_gender = "Men"
        player_gender = "Male"
        url_gender = "male"
    else:
        flash("Invalid category specified. Please select Men or Women.", "error")
        return redirect(url_for("admin_dashboard"))

    if request.method == "POST":
        action = request.form.get("action", "sell")
        is_ajax = request.headers.get("X-Requested-With") == "XMLHttpRequest" or request.is_json

        if action == "sell":
            player = db.session.get(Player, request.form.get("player_id", type=int))
            owner = db.session.get(TeamOwner, request.form.get("owner_id", type=int))
            bid_amount = request.form.get("bid_amount", type=int)

            err = None
            wallet = None
            if not player or not owner:
                err = "Invalid player or team franchise selected."
            elif player.sold:
                err = f"{player.name} has already been sold to {player.sold_to}."
            elif bid_amount is None or bid_amount <= 0:
                err = "Enter a bid amount greater than zero."
            else:
                wallet = owner.get_wallet(sport, category_gender)
                if not wallet:
                    err = f"{owner.team_name} is not registered for {sport} – {category_gender}."
                elif bid_amount > wallet.balance:
                    err = f"{owner.team_name} only has {format_inr(wallet.balance)} available in their {sport} – {category_gender} purse."

            if err:
                if is_ajax:
                    return jsonify({"status": "error", "message": err}), 400
                flash(err, "error")
            else:
                bal_before = wallet.balance
                wallet.spent += bid_amount
                wallet.balance = max(0, wallet.balance - bid_amount)
                owner.sync_budget_from_wallets()

                player.sold = True
                player.unsold = False
                player.bid_amount = bid_amount
                player.sold_to = owner.team_name

                tx = WalletTransaction(
                    wallet_id=wallet.id,
                    team_owner_id=owner.id,
                    player_id=player.id,
                    sport=sport,
                    gender=category_gender,
                    action="BID_PURCHASE",
                    amount=bid_amount,
                    balance_before=bal_before,
                    balance_after=wallet.balance,
                    description=f"Purchased {player.name} ({player.position or 'Player'}) in {sport} – {category_gender} auction",
                )
                db.session.add(tx)

                try:
                    db.session.commit()
                    
                    # Run post-bid hooks (email notification and scorecard sync) in a background
                    # daemon thread so the auction operator's screen responds instantly without delay.
                    def _async_post_bid(p_id, o_id, amt):
                        try:
                            with app.app_context():
                                async_p = db.session.get(Player, p_id)
                                async_o = db.session.get(TeamOwner, o_id)
                                if async_p and async_o:
                                    try:
                                        email_player_sold(async_p, async_o, amt)
                                    except Exception as mail_err:
                                        app.logger.warning("Async bid email error: %s", mail_err)
                                    try:
                                        from scorecard.sync import sync_sold_player
                                        sync_sold_player(async_p, async_o)
                                    except Exception as sync_exc:
                                        app.logger.warning("Auto-sync player to scorecard failed: %s", sync_exc)
                        except Exception as e:
                            app.logger.warning("Post-bid background worker error: %s", e)

                    t_post = threading.Thread(target=_async_post_bid, args=(player.id, owner.id, bid_amount), daemon=True)
                    t_post.start()

                    msg = f"🔨 SOLD! {player.name} goes to {owner.team_name} for {format_inr(bid_amount)}!"
                    if is_ajax:
                        return jsonify({
                            "status": "success",
                            "message": msg,
                            "player_id": player.id,
                            "player_name": player.name,
                            "owner_id": owner.id,
                            "team_name": owner.team_name,
                            "bid_amount": bid_amount,
                            "bid_amount_display": format_inr(bid_amount),
                            "remaining_budget": wallet.balance,
                            "remaining_budget_display": format_inr(wallet.balance),
                            "wallet_balance": wallet.balance,
                            "wallet_balance_display": format_inr(wallet.balance),
                        })
                    flash(msg, "success")
                except Exception as exc:
                    db.session.rollback()
                    app.logger.exception("Bid failed")
                    if is_ajax:
                        return jsonify({"status": "error", "message": f"Could not record bid: {exc}"}), 500
                    flash(f"Could not record that bid: {exc}", "error")

        elif action == "unsold":
            player_id_raw = request.form.get("player_id")
            player = db.session.get(Player, int(player_id_raw)) if player_id_raw and str(player_id_raw).isdigit() else None
            if not player:
                app.logger.warning("Unsold failed: Player ID '%s' not found.", player_id_raw)
                if is_ajax:
                    return jsonify({"status": "error", "message": "Player not found or already processed."}), 400
                flash("Player not found.", "error")
            elif player.sold:
                app.logger.warning("Unsold failed: Player %s (ID %s) is already sold.", player.name, player.id)
                if is_ajax:
                    return jsonify({"status": "error", "message": f"{player.name} is already sold to {player.sold_to}."}), 400
                flash(f"{player.name} is already sold.", "error")
            else:
                try:
                    player.unsold = True
                    db.session.commit()
                    msg = f"{player.name} passed as Unsold."
                    if is_ajax:
                        return jsonify({"status": "success", "message": msg, "player_id": player.id})
                    flash(msg, "info")
                except Exception as exc:
                    db.session.rollback()
                    app.logger.exception("Failed to mark player unsold: %s", exc)
                    if is_ajax:
                        return jsonify({"status": "error", "message": f"Could not mark unsold: {exc}"}), 500
                    flash(f"Could not mark unsold: {exc}", "error")

        elif action == "reopen":
            player = db.session.get(Player, request.form.get("player_id", type=int))
            if player:
                if player.sold and player.sold_to and player.bid_amount:
                    old_owner = TeamOwner.query.filter_by(team_name=player.sold_to).first()
                    if old_owner:
                        old_wallet = old_owner.get_wallet(player.sport, category_gender)
                        if old_wallet:
                            bal_before = old_wallet.balance
                            old_wallet.spent = max(0, old_wallet.spent - player.bid_amount)
                            old_wallet.balance = min(old_wallet.allocated_budget, old_wallet.balance + player.bid_amount)
                            old_owner.sync_budget_from_wallets()
                            tx = WalletTransaction(
                                wallet_id=old_wallet.id,
                                team_owner_id=old_owner.id,
                                player_id=player.id,
                                sport=player.sport,
                                gender=category_gender,
                                action="BID_REFUND",
                                amount=player.bid_amount,
                                balance_before=bal_before,
                                balance_after=old_wallet.balance,
                                description=f"Auction reopen refund for {player.name}",
                            )
                            db.session.add(tx)
                        else:
                            old_owner.budget = (old_owner.budget or 0) + player.bid_amount
                player.sold = False
                player.unsold = False
                player.sold_to = None
                player.bid_amount = None
                db.session.commit()
                try:
                    from scorecard.sync import sync_reopened_player
                    sync_reopened_player(player)
                except Exception as sync_exc:
                    app.logger.warning("Auto-remove reopened player from scorecard failed: %s", sync_exc)
                msg = f"{player.name} re-opened into live auction pool."
                if is_ajax:
                    return jsonify({"status": "success", "message": msg, "player_id": player.id})
                flash(msg, "success")

        elif action == "recall_unsold":
            count = Player.query.filter_by(sport=sport, gender=player_gender, sold=False, unsold=True).count()
            Player.query.filter_by(sport=sport, gender=player_gender, sold=False, unsold=True).update({'unsold': False})
            db.session.commit()
            flash(f"Recalled {count} unsold players back to live auction for Round 2!", "success")

        return redirect(url_for("bidding", sport=sport.lower(), gender=url_gender))

    players = (
        Player.query.filter(
            Player.sport == sport,
            Player.gender == player_gender,
            Player.sold.is_(False),
            db.or_(Player.unsold.is_(False), Player.unsold.is_(None)),
        )
        .order_by(Player.id)
        .all()
    )
    unsold_players = (
        Player.query.filter_by(sport=sport, gender=player_gender, sold=False, unsold=True)
        .order_by(Player.name)
        .all()
    )
    sold_here = (
        Player.query.filter_by(sport=sport, gender=player_gender, sold=True)
        .order_by(Player.bid_amount.desc())
        .all()
    )

    # Franchise eligibility: franchises with a wallet for this sport & category
    eligible_wallets = TeamWallet.query.filter_by(sport=sport, gender=category_gender).all()
    eligible_owner_ids = {w.team_owner_id for w in eligible_wallets}
    all_owners = TeamOwner.query.order_by(TeamOwner.team_name).all()
    owners = [o for o in all_owners if o.id in eligible_owner_ids]
    if not owners:
        owners = all_owners

    owner_stats = {}
    for o in owners:
        w = o.get_wallet(sport, category_gender)
        squad = Player.query.filter_by(sold_to=o.team_name, sport=sport, gender=player_gender).all()
        spent = w.spent if w else sum(p.bid_amount or 0 for p in squad)
        budget = w.balance if w else (o.budget or 0)
        allocated = w.allocated_budget if w else DEFAULT_BUDGET
        pct = max(0, min(100, int((budget / allocated) * 100))) if allocated > 0 else 0
        owner_stats[o.id] = {
            "squad_count": len(squad),
            "spent": spent,
            "budget": budget,
            "allocated": allocated,
            "budget_pct": pct,
        }

    total_spent = sum(p.bid_amount or 0 for p in sold_here)
    highest_bid = max([p.bid_amount or 0 for p in sold_here], default=0)

    return render_template(
        "bidding.html",
        sport=sport,
        gender=category_gender,
        url_gender=url_gender,
        player_gender=player_gender,
        players=players,
        unsold_players=unsold_players,
        sold_players=sold_here,
        owners=owners,
        owner_stats=owner_stats,
        total_spent=total_spent,
        highest_bid=highest_bid,
        mens_only=sport in MENS_ONLY_SPORTS,
    )


@app.route("/terms")
def terms():
    return render_template("terms.html")


@app.route("/auction")
@app.route("/squads")
def auction_squads():
    selected_sport = request.args.get("sport", "").capitalize()
    if selected_sport and selected_sport not in SPORTS:
        selected_sport = ""

    owners = TeamOwner.query.order_by(TeamOwner.team_name).all()
    all_sold = Player.query.filter_by(sold=True).order_by(Player.bid_amount.desc()).all()

    total_spent = sum(p.bid_amount or 0 for p in all_sold)
    total_sold = len(all_sold)
    highest_bid = all_sold[0].bid_amount if all_sold else 0
    top_signings = all_sold[:6]

    franchise_squads = []
    for o in owners:
        if selected_sport:
            squad = [p for p in all_sold if p.sold_to == o.team_name and p.sport == selected_sport]
        else:
            squad = [p for p in all_sold if p.sold_to == o.team_name]
        spent = sum(p.bid_amount or 0 for p in squad)
        franchise_squads.append({
            "owner": o,
            "squad": squad,
            "squad_count": len(squad),
            "spent": spent,
            "budget": o.budget or 0,
        })

    return render_template(
        "auction_squads.html",
        franchise_squads=franchise_squads,
        selected_sport=selected_sport,
        sports=SPORTS,
        total_spent=total_spent,
        total_sold=total_sold,
        highest_bid=highest_bid,
        top_signings=top_signings,
    )


# --------------------------------------------------------------------------- #
# Admin
# --------------------------------------------------------------------------- #
def admin_password_ok(candidate):
    if ADMIN_PASSWORD_HASH:
        return check_password_hash(ADMIN_PASSWORD_HASH, candidate)
    return secrets.compare_digest(candidate, ADMIN_PASSWORD)


@app.route("/admin/login", methods=["GET", "POST"])
def admin_login():
    if request.method == "POST":
        attempts = session.get("_login_attempts", 0)
        if attempts >= 8:
            flash("Too many failed attempts. Close the browser tab and try again.", "error")
            return render_template("admin_login.html")

        username = clean(request.form.get("username"), 60)
        password = request.form.get("password") or ""
        if secrets.compare_digest(username, ADMIN_USERNAME) and admin_password_ok(password):
            session.clear()
            session.permanent = True
            session["admin_logged_in"] = True
            flash("Welcome back. You're logged in.", "success")
            target = request.args.get("next") or request.form.get("next") or ""
            return redirect(target if target.startswith("/admin") else url_for("admin_dashboard"))

        session["_login_attempts"] = attempts + 1
        flash("Invalid username or password.", "error")
    return render_template("admin_login.html")


@app.route("/admin/logout")
def admin_logout():
    session.pop("admin_logged_in", None)
    flash("You have been logged out.", "success")
    return redirect(url_for("admin_login"))


INSTITUTION_LABELS = {
    "gmu": "GM University",
    "gmit": "GMIT",
    "other": "Others / Foreign",
}


def classify_college(college_name: str | None) -> str:
    """Classify a player's registered college into 'gmu', 'gmit', or 'other'."""
    if not college_name:
        return "other"
    raw = college_name.strip()
    c = raw.lower()
    c_clean = re.sub(r"[^a-z0-9]", "", c)

    # 1. GM University variations
    if (
        "gmu" in c_clean
        or "gmuniversity" in c_clean
        or "gmunvirsity" in c_clean
        or "gumuniversity" in c_clean
        or "umunivarsity" in c_clean
    ):
        return "gmu"
    if "gm" in c_clean and ("univ" in c_clean or "vercity" in c_clean or "unv" in c_clean):
        return "gmu"
    if "gmbs" in c_clean:
        return "gmu"

    # 2. GMIT variations (excluding separate sister institutes like GMIPSR or GM Science)
    if "pharm" not in c and "science" not in c:
        if (
            "gmit" in c_clean
            or "gminstituteoftech" in c_clean
            or "gminstitueoftech" in c_clean
            or c_clean in ("gminstitute", "gminstitue")
        ):
            return "gmit"
        if "gm" in c_clean and "tech" in c_clean:
            return "gmit"
    elif "gmit" in c_clean:
        return "gmit"

    return "other"


@app.route("/admin/dashboard")
@admin_required
def admin_dashboard():
    players_by_sport = {s: Player.query.filter_by(sport=s).order_by(Player.name).all() for s in SPORTS}
    owners = TeamOwner.query.order_by(TeamOwner.team_name).all()
    sold_players = Player.query.filter_by(sold=True).order_by(Player.bid_amount.desc()).all()
    all_players = Player.query.order_by(Player.name).all()

    for p in all_players:
        cat = classify_college(p.college_name)
        p.institution_category = cat
        p.institution_label = INSTITUTION_LABELS.get(cat, "Others / Foreign")

    inst_counts = {
        "all": len(all_players),
        "gmu": sum(1 for p in all_players if p.institution_category == "gmu"),
        "gmit": sum(1 for p in all_players if p.institution_category == "gmit"),
        "other": sum(1 for p in all_players if p.institution_category == "other"),
    }

    other_players = [p for p in all_players if p.institution_category == "other"]
    other_colleges_summary = Counter(p.college_name.strip() for p in other_players if p.college_name).most_common(14)

    return render_template(
        "admin_dashboard.html",
        players_by_sport=players_by_sport,
        owners=owners,
        sold_players=sold_players,
        all_players=all_players,
        total_players=len(all_players),
        inst_counts=inst_counts,
        other_colleges_summary=other_colleges_summary,
        missing_email=sum(1 for p in all_players if not p.email),
        spent=sum(p.bid_amount or 0 for p in sold_players),
        remaining=sum(o.budget or 0 for o in owners),
        mail_status=mailer.status(),
    )


@app.route("/admin/institutions")
@admin_required
def admin_institutions():
    return redirect(url_for("admin_dashboard") + "#institutions")



@app.route("/admin/mail_test", methods=["POST"])
@admin_required
def admin_mail_test():
    recipient = clean(request.form.get("recipient"), 120).lower()
    if not EMAIL_RE.match(recipient):
        flash("Enter a valid email address to send the test to.", "error")
        return redirect(url_for("admin_dashboard"))

    status = mailer.status()
    ok, detail = mailer.send_now(
        to=recipient,
        subject=f"SMTP test - {BRAND['name']} event management system",
        template="test_mail",
        context={
            "brand": get_brand(),
            "logo_cid": has_logo(),
            "sent_at": datetime.now().strftime("%d %b %Y, %I:%M %p"),
            "smtp_server": status["server"],
            "smtp_security": status["security"],
            "smtp_username": status["username"],
            "smtp_sender": status["sender"],
        },
    )
    flash(
        f"Test mail to {recipient}: {detail}" if ok else f"Test mail failed - {detail}",
        "success" if ok else "error",
    )
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/resend/<kind>/<int:record_id>", methods=["POST"])
@admin_required
def admin_resend_confirmation(kind, record_id):
    if kind == "player":
        player = db.session.get(Player, record_id) or abort(404)
        if not player.email:
            flash(f"{player.name} has no email address on record.", "error")
        elif email_player_registered(player):
            flash(f"Confirmation re-sent to {player.email}.", "success")
        else:
            flash("Email automation is switched off.", "error")
    elif kind == "owner":
        owner = db.session.get(TeamOwner, record_id) or abort(404)
        if not owner.email:
            flash(f"{owner.team_name} has no email address on record.", "error")
        elif email_owner_registered(owner):
            flash(f"Confirmation re-sent to {owner.email}.", "success")
        else:
            flash("Email automation is switched off.", "error")
    else:
        abort(404)
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/reset_bid/<int:player_id>", methods=["POST"])
@admin_required
def admin_reset_bid(player_id):
    player = db.session.get(Player, player_id) or abort(404)
    if player.sold and player.sold_to and player.bid_amount:
        owner = TeamOwner.query.filter_by(team_name=player.sold_to).first()
        cat_gender = "Women" if player.gender == "Female" else "Men"
        if owner:
            wallet = owner.get_wallet(player.sport, cat_gender)
            if wallet:
                bal_before = wallet.balance
                wallet.spent = max(0, wallet.spent - player.bid_amount)
                wallet.balance = min(wallet.allocated_budget, wallet.balance + player.bid_amount)
                owner.sync_budget_from_wallets()
                tx = WalletTransaction(
                    wallet_id=wallet.id,
                    team_owner_id=owner.id,
                    player_id=player.id,
                    sport=player.sport,
                    gender=cat_gender,
                    action="BID_REFUND",
                    amount=player.bid_amount,
                    balance_before=bal_before,
                    balance_after=wallet.balance,
                    description=f"Admin reset bid refund for {player.name}",
                )
                db.session.add(tx)
            else:
                owner.budget = (owner.budget or 0) + player.bid_amount
    player.sold = False
    player.bid_amount = None
    player.sold_to = None
    try:
        db.session.commit()
        try:
            from scorecard.sync import sync_reopened_player
            sync_reopened_player(player)
        except Exception as sync_exc:
            app.logger.warning("Could not auto-remove player from scorecard: %s", sync_exc)
        flash(f"Bid reset for {player.name}. The purse has been refunded to franchise wallet.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error resetting bid: {exc}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_player/<int:id>", methods=["POST"])
@admin_required
def admin_delete_player(id):
    player = db.session.get(Player, id) or abort(404)
    name = player.name
    try:
        delete_upload(player.photo, "UPLOAD_FOLDER")
        db.session.delete(player)
        db.session.commit()
        flash(f"Player {name} deleted.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error deleting player: {exc}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_owner/<int:id>", methods=["POST"])
@admin_required
def admin_delete_owner(id):
    owner = db.session.get(TeamOwner, id) or abort(404)
    team = owner.team_name
    try:
        delete_upload(owner.team_logo, "TEAM_LOGO_FOLDER")
        delete_upload(owner.team_owner_photo, "OWNER_PHOTO_FOLDER")
        db.session.delete(owner)
        db.session.commit()
        flash(f"Team owner {team} deleted.", "success")
    except Exception as exc:
        db.session.rollback()
        flash(f"Error deleting team owner: {exc}", "error")
    return redirect(url_for("admin_dashboard"))


@app.route("/admin/delete_players", methods=["POST"])
@admin_required
def admin_delete_players():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    if not ids:
        return jsonify({"error": "No players selected"}), 400
    try:
        for player in Player.query.filter(Player.id.in_(ids)).all():
            delete_upload(player.photo, "UPLOAD_FOLDER")
            db.session.delete(player)
        db.session.commit()
        return jsonify({"message": f"{len(ids)} player(s) deleted"}), 200
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": f"Error deleting players: {exc}"}), 500


@app.route("/admin/delete_owners", methods=["POST"])
@admin_required
def admin_delete_owners():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    if not ids:
        return jsonify({"error": "No owners selected"}), 400
    try:
        for owner in TeamOwner.query.filter(TeamOwner.id.in_(ids)).all():
            delete_upload(owner.team_logo, "TEAM_LOGO_FOLDER")
            delete_upload(owner.team_owner_photo, "OWNER_PHOTO_FOLDER")
            db.session.delete(owner)
        db.session.commit()
        return jsonify({"message": f"{len(ids)} owner(s) deleted"}), 200
    except Exception as exc:
        db.session.rollback()
        return jsonify({"error": f"Error deleting owners: {exc}"}), 500


@app.route("/admin/edit_player/<int:id>", methods=["GET", "POST"])
@admin_required
def admin_edit_player(id):
    player = db.session.get(Player, id) or abort(404)
    if request.method == "POST":
        sport = clean(request.form.get("sport"), 50)
        if sport not in SPORTS:
            flash("Invalid sport.", "error")
            return redirect(url_for("admin_edit_player", id=id))

        position = clean(request.form.get("position"), 50)
        try:
            player.name = clean(request.form.get("name"), 100) or player.name
            player.sport = sport
            player.position = position if position in POSITIONS[sport] else None
            player.branch = clean(request.form.get("branch"), 50) or None
            player.usn = clean(request.form.get("usn"), 20).upper() or None
            player.email = clean(request.form.get("email"), 120).lower() or None
            player.college_name = clean(request.form.get("college_name"), 100) or None
            player.contact = normalise_phone(request.form.get("contact")) or None
            player.achievements = clean(request.form.get("achievements"), 2000) or None
            player.experience = clean(request.form.get("experience"), 50) or player.experience
            player.gender = clean(request.form.get("gender"), 10) or player.gender

            new_photo, photo_error = save_upload(
                request.files.get("photo"), "UPLOAD_FOLDER", "player_photos", player.name
            )
            if photo_error:
                flash(photo_error, "error")
                return redirect(url_for("admin_edit_player", id=id))
            if new_photo:
                delete_upload(player.photo, "UPLOAD_FOLDER")
                player.photo = new_photo

            db.session.commit()
            flash(f"Player {player.name} updated.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"Error updating player: {exc}", "error")
        return redirect(url_for("admin_dashboard"))

    return render_template(
        "edit_player.html", player=player, positions=POSITIONS.get(player.sport, []), all_positions=POSITIONS
    )


@app.route("/admin/edit_team_owner/<int:id>", methods=["GET", "POST"])
@admin_required
def admin_edit_team_owner(id):
    owner = db.session.get(TeamOwner, id) or abort(404)
    if request.method == "POST":
        selected = [s for s in request.form.getlist("sports") if s in OWNER_SPORT_OPTIONS]
        if not selected:
            flash("Select at least one sport and category.", "error")
            return redirect(url_for("admin_edit_team_owner", id=id))
        try:
            owner.name = clean(request.form.get("name"), 100) or owner.name
            owner.team_name = clean(request.form.get("team_name"), 100) or owner.team_name
            owner.contact = normalise_phone(request.form.get("contact")) or None
            owner.usn = clean(request.form.get("employee_id") or request.form.get("usn"), 50).upper() or None
            owner.designation = clean(request.form.get("designation"), 100) or None
            owner.email = clean(request.form.get("email"), 120).lower() or None
            owner.manager_name = clean(request.form.get("manager_name"), 100) or None
            owner.manager_contact_number = normalise_phone(request.form.get("manager_contact_number")) or None

            # Manage category wallets
            new_categories = []
            for s in selected:
                sp, gn = parse_sport_gender(s)
                if sp and (sp, gn) not in new_categories:
                    new_categories.append((sp, gn))

            existing_wallets = {(w.sport, w.gender): w for w in owner.wallets}

            # 1. Add wallets for newly selected categories
            for sp, gn in new_categories:
                if (sp, gn) not in existing_wallets:
                    w = TeamWallet(
                        team_owner_id=owner.id,
                        sport=sp,
                        gender=gn,
                        allocated_budget=DEFAULT_BUDGET,
                        spent=0,
                        balance=DEFAULT_BUDGET,
                    )
                    db.session.add(w)
                    db.session.flush()
                    tx = WalletTransaction(
                        wallet_id=w.id,
                        team_owner_id=owner.id,
                        sport=sp,
                        gender=gn,
                        action="INITIAL_ALLOCATION",
                        amount=DEFAULT_BUDGET,
                        balance_before=0,
                        balance_after=DEFAULT_BUDGET,
                        description=f"Initial purse allocation of ₹10,00,000 for {sp} – {gn}",
                    )
                    db.session.add(tx)

            # 2. Check unselected categories: preserve if squad has signed players
            new_cat_set = set(new_categories)
            for (sp, gn), w in list(existing_wallets.items()):
                if (sp, gn) not in new_cat_set:
                    p_gender = "Female" if gn == "Women" else "Male"
                    has_players = Player.query.filter_by(sold_to=owner.team_name, sport=sp, gender=p_gender).count()
                    if has_players:
                        flash(f"Preserved {sp} – {gn} wallet because {owner.team_name} has {has_players} signed players in this category.", "warning")
                        if (sp, gn) not in new_categories:
                            new_categories.append((sp, gn))
                    else:
                        db.session.delete(w)

            canonical_labels = [f"{sp} – {gn}" for sp, gn in new_categories]
            owner.sports = ", ".join(canonical_labels)
            owner.sync_budget_from_wallets()

            new_logo, logo_error = save_upload(
                request.files.get("team_logo"), "TEAM_LOGO_FOLDER", "team_logos", owner.team_name
            )
            new_photo, photo_error = save_upload(
                request.files.get("team_owner_photo"), "OWNER_PHOTO_FOLDER", "owner_photos", owner.name
            )
            for message in (logo_error, photo_error):
                if message:
                    flash(message, "error")
                    return redirect(url_for("admin_edit_team_owner", id=id))
            if new_logo:
                delete_upload(owner.team_logo, "TEAM_LOGO_FOLDER")
                owner.team_logo = new_logo
            if new_photo:
                delete_upload(owner.team_owner_photo, "OWNER_PHOTO_FOLDER")
                owner.team_owner_photo = new_photo

            db.session.commit()
            flash(f"Franchise {owner.team_name} updated successfully.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"Error updating team owner: {exc}", "error")
        return redirect(url_for("admin_dashboard"))

    # Build normalized set of owned categories
    owned = {f"{w.sport} – {w.gender}".lower() for w in owner.wallets}
    if not owned:
        for s in (owner.sports or "").split(","):
            sp, gn = parse_sport_gender(s)
            if sp:
                owned.add(f"{sp} – {gn}".lower())

    return render_template(
        "edit_team_owner.html", owner=owner, sport_options=OWNER_SPORT_OPTIONS, owned=owned
    )


# --------------------------------------------------------------------------- #
# Excel exports
# --------------------------------------------------------------------------- #
def player_rows(players):
    return [
        {
            "Entry ID": p.reg_code,
            "Name": p.name,
            "Institution": INSTITUTION_LABELS.get(classify_college(p.college_name), "Others / Foreign"),
            "College": p.college_name or "N/A",
            "USN": p.usn or "N/A",
            "Email": p.email or "N/A",
            "Contact": p.contact or "N/A",
            "Branch": p.branch or "N/A",
            "Sport": p.sport,
            "Position": p.position or "N/A",
            "Category": p.gender or "N/A",
            "Experience": p.experience or "N/A",
            "Achievements": p.achievements or "None",
            "Sold To": p.sold_to or "Not Sold",
            "Bid Amount": p.bid_amount or 0,
            "Registered On": p.registered_at.strftime("%Y-%m-%d %H:%M") if p.registered_at else "N/A",
            "Photo": p.photo or "N/A",
        }
        for p in players
    ]


def owner_rows(owners):
    return [
        {
            "Franchise ID": o.reg_code,
            "Team Name": o.team_name,
            "Owner": o.name,
            "Employee ID": o.usn or "N/A",
            "Email": o.email or "N/A",
            "Contact": o.contact or "N/A",
            "Designation": o.designation or "N/A",
            "Categories": ", ".join(f"{w.sport} – {w.gender}" for w in o.wallets) if o.wallets else o.sports,
            "Total Allocated": format_inr(o.total_allocated_budget),
            "Total Spent": format_inr(o.total_spent),
            "Total Remaining": format_inr(o.total_balance),
            "Wallets Breakdown": "; ".join(
                f"{w.sport} ({w.gender}): Bal {format_inr(w.balance)} / Alloc {format_inr(w.allocated_budget)}"
                for w in o.wallets
            ),
            "Manager Name": o.manager_name or "N/A",
            "Manager Contact": o.manager_contact_number or "N/A",
            "Registered On": o.registered_at.strftime("%Y-%m-%d %H:%M") if o.registered_at else "N/A",
        }
        for o in owners
    ]


def excel_response(sheets, filename):
    """``sheets`` is a list of ``(sheet_name, list-of-dict-rows)``."""
    import xlsxwriter  # lazy import - keeps cold start fast

    output = BytesIO()
    workbook = xlsxwriter.Workbook(output, {"in_memory": True})
    header_style = workbook.add_format({"bold": True})
    for sheet_name, rows in sheets:
        rows = rows or [{"Info": "No records"}]
        # Union of keys in first-seen order: the column layout the row builders
        # above already imply, without needing pandas to infer it.
        columns = list(dict.fromkeys(key for row in rows for key in row))
        clean_name = re.sub(r"[\[\]:*?/\\]", "_", sheet_name)[:31]
        worksheet = workbook.add_worksheet(clean_name)
        worksheet.write_row(0, 0, columns, header_style)
        for row_index, row in enumerate(rows, start=1):
            for column_index, column in enumerate(columns):
                worksheet.write(row_index, column_index, row.get(column))
        for column_index, column in enumerate(columns):
            width = max([len(str(column))] + [len(str(row.get(column, ""))) for row in rows])
            worksheet.set_column(column_index, column_index, min(max(width + 2, 10), 42))
    workbook.close()
    output.seek(0)
    return send_file(
        output,
        download_name=filename,
        as_attachment=True,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    )


@app.route("/export_summary/<sport>")
@admin_required
def export_summary(sport):
    sport = sport.capitalize()
    if sport not in SPORTS:
        flash("Invalid sport.", "error")
        return redirect(url_for("admin_dashboard"))
    players = Player.query.filter_by(sport=sport).order_by(Player.name).all()
    return excel_response([(sport, player_rows(players))], f"{BRAND['short']}_{sport}_Players.xlsx")


@app.route("/export_all_summaries")
@admin_required
def export_all_summaries():
    sheets = [(s, player_rows(Player.query.filter_by(sport=s).order_by(Player.name).all())) for s in SPORTS]
    sheets.append(("Team Owners", owner_rows(TeamOwner.query.order_by(TeamOwner.team_name).all())))
    return excel_response(sheets, f"{BRAND['short']}_All_Summaries.xlsx")


@app.route("/export_owners")
@admin_required
def export_owners():
    owners = TeamOwner.query.order_by(TeamOwner.team_name).all()
    return excel_response([("Team Owners", owner_rows(owners))], f"{BRAND['short']}_Team_Owners.xlsx")


@app.route("/export_institution/<category>")
@admin_required
def export_institution(category):
    category = (category or "").lower()
    all_players = Player.query.order_by(Player.name).all()

    if category == "all":
        sheets = [
            ("GM University", player_rows([p for p in all_players if classify_college(p.college_name) == "gmu"])),
            ("GMIT", player_rows([p for p in all_players if classify_college(p.college_name) == "gmit"])),
            ("Others & Foreign", player_rows([p for p in all_players if classify_college(p.college_name) == "other"])),
        ]
        return excel_response(sheets, f"{BRAND['short']}_Institutions_Breakdown.xlsx")

    if category not in ("gmu", "gmit", "other"):
        flash("Invalid institution category.", "error")
        return redirect(url_for("admin_dashboard") + "#institutions")

    label = INSTITUTION_LABELS[category].replace(" / ", "_").replace(" ", "_")
    sheet_title = INSTITUTION_LABELS[category].replace("/", "&")
    filtered = [p for p in all_players if classify_college(p.college_name) == category]
    return excel_response([(sheet_title, player_rows(filtered))], f"{BRAND['short']}_{label}_Players.xlsx")



# --------------------------------------------------------------------------- #
# Error handlers
# --------------------------------------------------------------------------- #
@app.errorhandler(404)
def not_found(_error):
    return render_template("error.html", code=404, title="Page not found",
                           message="That page isn't part of the league."), 404


@app.errorhandler(413)
def too_large(_error):
    limit = app.config["MAX_CONTENT_LENGTH"] // (1024 * 1024)
    return render_template("error.html", code=413, title="File too large",
                           message=f"Photos and logos must be under {limit} MB. Please compress and try again."), 413


@app.errorhandler(500)
def server_error(error):
    app.logger.exception("Unhandled error: %s", error)
    try:
        db.session.rollback()
    except Exception:
        pass
    return render_template("error.html", code=500, title="Something broke",
                           message="Our fault, not yours. Please try again in a moment."), 500


# --------------------------------------------------------------------------- #
# Live scorecard
# --------------------------------------------------------------------------- #
# Self-contained package under /scores with its own sc_ tables. Imported last so
# every name it borrows (db, admin_required, SPORTS) already exists. Nothing
# above this line knows the scorecard is here.
from scorecard import scorecard_bp  # noqa: E402

app.register_blueprint(scorecard_bp)


# --------------------------------------------------------------------------- #
# Keep-Alive Pinger (for Render Free Tier 24/7 uptime)
# --------------------------------------------------------------------------- #
def _start_keep_alive_worker():
    """Background daemon thread to ping Render web service every 15 seconds.

    Render puts free-tier web services to sleep after 15 minutes of inactivity.
    By pinging the public URL through Render's public router, the instance
    registers incoming traffic and stays awake 24/7.
    """
    if SERVERLESS:
        return

    ping_url = os.getenv("RENDER_EXTERNAL_URL") or os.getenv("PING_URL")
    if not ping_url and not env_bool("KEEP_ALIVE_ENABLED"):
        return

    if not ping_url:
        ping_url = f"http://127.0.0.1:{env_int('PORT', 5000)}/ping"

    ping_url = ping_url.rstrip("/")
    if not ping_url.endswith("/ping") and not ping_url.endswith("/health"):
        ping_url = f"{ping_url}/ping"

    interval = max(5, env_int("PING_INTERVAL", 15))

    def _loop():
        time.sleep(12)  # Allow WSGI / Gunicorn to bind port
        app.logger.info("Keep-alive pinger started. Target: %s, interval: %ds", ping_url, interval)
        while True:
            try:
                req = urllib.request.Request(
                    ping_url,
                    headers={"User-Agent": "GM-League-KeepAlive-Worker/1.0"},
                )
                with urllib.request.urlopen(req, timeout=10):
                    pass
            except Exception as e:
                app.logger.debug("Keep-alive ping exception: %s", e)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True, name="keep-alive-pinger")
    t.start()


_start_keep_alive_worker()


if __name__ == "__main__":
    app.run(debug=env_bool("FLASK_DEBUG", True), host=env_str("HOST", "127.0.0.1"), port=env_int("PORT", 5000))
