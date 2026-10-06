# --------------------------------------------------------------------------- #
# Database configuration
# --------------------------------------------------------------------------- #

def normalise_database_url(url):
    """Prepare DATABASE_URL for SQLAlchemy + Supabase."""

    if not url:
        return url

    url = url.strip()

    # SQLAlchemy expects postgresql:// rather than legacy postgres://
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]

    # Force psycopg2 driver
    if url.startswith("postgresql://"):
        url = "postgresql+psycopg2://" + url[len("postgresql://"):]

    # Supabase requires SSL
    if "sslmode=" not in url:
        url += ("&" if "?" in url else "?") + "sslmode=require"

    return url


# IMPORTANT:
# DATABASE_URL must be configured in Render Environment Variables.
# Do NOT put the real database password inside this source file.
DATABASE_URL = env_str("DATABASE_URL")

if not DATABASE_URL:
    raise RuntimeError(
        "DATABASE_URL is not configured. "
        "Please add DATABASE_URL in Render Environment Variables."
    )

app.config.update(
    SECRET_KEY=_secret_key,

    SQLALCHEMY_DATABASE_URI=normalise_database_url(DATABASE_URL),

    SQLALCHEMY_TRACK_MODIFICATIONS=False,

    UPLOAD_FOLDER=os.path.join(
        app.root_path,
        "static",
        "player_photos"
    ),

    TEAM_LOGO_FOLDER=os.path.join(
        app.root_path,
        "static",
        "team_logos"
    ),

    OWNER_PHOTO_FOLDER=os.path.join(
        app.root_path,
        "static",
        "owner_photos"
    ),

    MAX_CONTENT_LENGTH=env_int(
        "MAX_UPLOAD_MB",
        8
    ) * 1024 * 1024,

    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env_bool(
        "SESSION_COOKIE_SECURE",
        False
    ),

    PERMANENT_SESSION_LIFETIME=timedelta(hours=8),

    DEBUG=False,
    PROPAGATE_EXCEPTIONS=False,
)


# --------------------------------------------------------------------------- #
# SQLAlchemy connection options
# --------------------------------------------------------------------------- #

if DATABASE_URL.startswith(("postgres://", "postgresql://")):
    app.config["SQLALCHEMY_ENGINE_OPTIONS"] = {
        "poolclass": NullPool,
        "connect_args": {
            "connect_timeout": 10,
            "sslmode": "require",
        },
    }
