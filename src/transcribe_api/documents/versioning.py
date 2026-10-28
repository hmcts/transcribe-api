"""Which version of the decision template produced a document (DIAAT-279).

A decision document is produced by the structured renderer (the code in this
package and in ``lib``, and the crest images) from document content that admins
can edit in the database. Neither has a version field, so both are fingerprinted:

* the renderer: SHA-256 of this package's source files, the ``documents/lib`` modules the
  structured renderer builds the document with (frontmatter, blocks, typography)
  and the crest images, computed once at import (a failure is logged and retried,
  never cached);
* the content: SHA-256 of the content the loader handed to the renderer
  (``content_loader.last_used_content_digest``).

``template_version()`` combines the two into 12 hex characters, so it changes when
either does. It may also change on an edit that does not alter the output, which
is the safe direction.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from transcribe_api.documents.content_loader import last_used_content_digest

logger = logging.getLogger(__name__)

# Telemetry name of the template (document.template).
DECISION_TEMPLATE = "decision"

_PACKAGE_DIR = Path(__file__).resolve().parent  # transcribe_api/documents
_APP_DIR = _PACKAGE_DIR.parent  # transcribe_api
_LIB_DIR = _PACKAGE_DIR / "lib"
_DATA_DIR = _APP_DIR / "data"
_ASSETS = (
    _DATA_DIR / "english-judicial-decision-crest.png",
    _DATA_DIR / "scottish-judicial-decision-crest.png",
)


def _compute_renderer_digest() -> str | None:
    try:
        digest = hashlib.sha256()
        for path in [*sorted(_PACKAGE_DIR.glob("*.py")), *sorted(_LIB_DIR.glob("*.py")), *_ASSETS]:
            digest.update(str(path.relative_to(_APP_DIR)).encode())
            digest.update(path.read_bytes())
        return digest.hexdigest()
    except OSError as exc:
        logger.warning("Document renderer version could not be computed (%s)", type(exc).__name__)
        return None


# At import, i.e. at startup, not on a request's event loop.
_renderer_digest: str | None = _compute_renderer_digest()


def template_version() -> str:
    """12 hex characters identifying the renderer and content in use, or "unknown"."""
    global _renderer_digest  # noqa: PLW0603 - computed once per process
    if _renderer_digest is None:
        _renderer_digest = _compute_renderer_digest()  # retried, not cached as failed
    if _renderer_digest is None:
        return "unknown"
    content = last_used_content_digest() or "-"
    return hashlib.sha256(f"{_renderer_digest}:{content}".encode()).hexdigest()[:12]
