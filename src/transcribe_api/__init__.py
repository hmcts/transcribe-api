"""HMCTS Transcribe backend."""

# Must run before any settings object is built, including the auth library's,
# which caches the first values it reads. Package import is the one point that
# precedes everything: the web app, the worker and Alembic all import through
# here. See runtime/secret_files.py.
from transcribe_api.runtime.secret_files import load_secret_files_into_environ

load_secret_files_into_environ()
