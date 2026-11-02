import logging
from collections.abc import Generator

from dotenv import load_dotenv
from sqlmodel import Session, create_engine, select

# Redacted logging for processes that import this module directly (DB scripts): the
# same bootstrap as every app module, defined once in runtime/logger.py (and replaced
# by the full setup when the API or worker starts).
import transcribe_api.runtime.logger  # noqa: F401
from transcribe_api.runtime.settings_dictation import get_settings

# Load environment variables from .env file
load_dotenv()

logger = logging.getLogger(__name__)

# Database URL from environment variable
DATABASE_URL = get_settings().DATABASE_CONNECTION_STRING

if not DATABASE_URL:
    msg = "DATABASE_CONNECTION_STRING is not set"
    raise ValueError(msg)

# Create engine with connection pool configuration
# Configured for GP_Standard_D2s_v3: 199 user connections (214 total - 15 reserved)
# Configuration: 20 pool + 30 overflow = 50 connections per instance
# This allows 4 backend instances (~200 connections) with headroom for admin/monitoring
engine = create_engine(
    DATABASE_URL,
    echo=False,
    pool_size=20,  # Base connections for steady state
    max_overflow=30,  # Additional connections for burst traffic
    pool_timeout=30,  # Wait for available connection
    pool_recycle=3600,  # Recycle connections after 1 hour
    pool_pre_ping=True,  # Verify connections before using
)


def get_session() -> Generator[Session]:
    """Database session dependency"""
    with Session(engine) as session:
        yield session


# def init_db():
#     """Initialize database - create tables"""
#     try:
#         create_db_and_tables()
#         logger.info("Database initialized successfully")
#     except Exception as e:
#         logger.error(f"Error initializing database: {e}")
#         raise


def test_db_connection():
    """Test database connection"""
    try:
        with Session(engine) as session:
            # Simple query to test connection
            session.execute(select(1))
            logger.info("Database connection test successful")
            return True
    except Exception:
        logger.exception("Database connection test failed")
        return False
