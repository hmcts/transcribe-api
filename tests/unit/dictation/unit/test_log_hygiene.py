"""Static analysis: logger calls in the backend must not dump raw payloads.

DIAAT-298 was caused by a logger.info() call that passed json.dumps(payload, indent=2),
dumping the full submission payload (transcripts, notes, PII) to Application Insights.
This module walks the AST of every non-test Python file in the backend and fails if it
finds any of the unsafe patterns that caused or could repeat the incident.
"""

import ast
from pathlib import Path

# File is at tests/unit/dictation/unit/test_log_hygiene.py — four parents to reach the
# repository root; the application source is src/transcribe_api.
_BACKEND_ROOT = Path(__file__).parents[4] / "src" / "transcribe_api"
_LOG_METHODS = {"debug", "info", "warning", "error", "critical", "exception"}

# Variable names whose values are known to contain PII and must never be logged directly.
# blob_name / blob_path / user_upload_blob_path / s3_file_key encode the user's email in
# legacy user-uploads/{email}/{filename} paths; always pass through _redact_blob_path.
# eval represents a full evaluation object whose input.dialogue_entries field contains
# transcript text (PII); log only safe fields (index, exception type) instead.
_PII_VARIABLE_NAMES = {
    "payload",
    "download_filename",
    "blob_name",
    "blob_path",
    "user_upload_blob_path",
    "user_identifier",
    "s3_file_key",
    "eval",
}

# Subset of _PII_VARIABLE_NAMES that represent blob/S3 path strings in the
# user-uploads/{email}/{filename} format.  _redact_blob_path correctly strips the
# email segment from these; it returns all other strings unchanged, so passing a
# non-blob-path PII variable to _redact_blob_path is still a violation.
_BLOB_PATH_VARIABLE_NAMES: frozenset[str] = frozenset({
    "blob_name",
    "blob_path",
    "user_upload_blob_path",
    "s3_file_key",
})

_EXCLUDE_DIRS = {"tests", "migrations", "alembic", "__pycache__", ".venv", "vendor", "scripts"}

# DIAAT-298 covered the dictation service. These modules came from the recording
# service (batch-audio-transcription, see scripts-import.sh) and are not yet held to
# these rules; every other module, including code written in this repo, is.
_RECORDING_SERVICE_FILES = {
    "api/deps_recording.py",
    "api/routes_recording.py",
    "domain/auth/approles_recording.py",
    "domain/auth/auth_models_recording.py",
    "domain/auth/dependencies_recording.py",
    "domain/interface_recording.py",
    "domain/models_recording.py",
    "domain/webhook.py",
    "runtime/blob_recording.py",
    "runtime/db_recording.py",
    "runtime/security.py",
    "runtime/settings_recording.py",
    "stt/accuracy.py",
    "stt/batch_client.py",
    "stt/local_storage.py",
    "stt/polling_service.py",
    "stt/preprocessing.py",
    "stt/speakers_recording.py",
    "stt/submission.py",
    "stt/wer.py",
}


def _backend_source_files():
    """Yield every non-test, non-vendor, dictation-service .py file under the source root."""
    for path in sorted(_BACKEND_ROOT.rglob("*.py")):
        rel = path.relative_to(_BACKEND_ROOT)
        if any(p in _EXCLUDE_DIRS for p in rel.parts) or rel.as_posix() in _RECORDING_SERVICE_FILES:
            continue
        yield path


def test_scans_the_application_source():
    # Guards the root above: a wrong path would make every check pass vacuously.
    files = {p.relative_to(_BACKEND_ROOT).as_posix() for p in _backend_source_files()}
    assert {"api/routes_dictation.py", "stt/work_poller.py", "runtime/blob_dictation.py"} <= files
    assert all((_BACKEND_ROOT / f).is_file() for f in _RECORDING_SERVICE_FILES)


def _collect_logger_calls(tree: ast.AST) -> list[ast.Call]:
    return [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr in _LOG_METHODS
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "logger"
    ]


def _is_json_dumps_call(node: ast.expr) -> bool:
    """True for json.dumps(...)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "json"
    )


def _is_pii_name(node: ast.expr) -> bool:
    """True for a bare reference to a known PII variable."""
    return isinstance(node, ast.Name) and node.id in _PII_VARIABLE_NAMES


def _contains_email_named_var(node: ast.expr) -> bool:
    """True if any Name node in the subtree is named 'email' or ends with '_email'.

    Catches bare email variables (e.g. email, admin_email, target_owner_email)
    anywhere in the argument subtree, including inside sanitize_for_log() wrappers.
    """
    return any(
        isinstance(n, ast.Name) and (n.id == "email" or n.id.endswith("_email"))
        for n in ast.walk(node)
    )


def _contains_email_attribute(node: ast.expr) -> bool:
    """True if any node in the expression subtree is an email-bearing attribute access.

    Matches both `.email` and any attribute ending in `_email` (e.g. request.user_email,
    record.admin_email) — those hold the same PII as a bare `.email` and must not be
    logged.  Uses ast.walk so it catches every form: bare `x.email`, wrapped in any
    function call (positional or keyword), inside a list/tuple, inside an f-string, etc.
    """
    return any(
        isinstance(n, ast.Attribute) and (n.attr == "email" or n.attr.endswith("_email"))
        for n in ast.walk(node)
    )


def _is_str_of_pii(node: ast.expr) -> bool:
    """True for str(<pii_var>) or repr(<pii_var>)."""
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id in ("str", "repr")
        and len(node.args) == 1
        and (_is_pii_name(node.args[0]) or _contains_email_attribute(node.args[0]))
    )


# The only function call that is permitted to receive a _BLOB_PATH_VARIABLE_NAMES member
# as a direct argument in a logger call.  Any other wrapper (e.g. _sanitize_for_log,
# str, json.dumps) does not redact the email path segment and must be rejected.
# Crucially, the exemption is restricted to blob-path variables: _redact_blob_path
# returns non-blob-path strings unchanged, so _redact_blob_path(payload) is still a
# violation even though the callee name matches.
_APPROVED_REDACTORS: frozenset[str] = frozenset({"_redact_blob_path"})


def _is_pii_in_unapproved_call(node: ast.expr) -> bool:
    """True when a function call receives a PII variable without proper redaction.

    Catches _sanitize_for_log(blob_path) and similar wrappers that do not
    redact the user-email path segment from legacy blob paths.

    Also catches _redact_blob_path(payload) / _redact_blob_path(eval) — passing
    a non-blob-path PII variable to the approved redactor is still a violation
    because _redact_blob_path returns those strings unchanged.
    """
    if not isinstance(node, ast.Call):
        return False
    pii_args = [a for a in node.args if isinstance(a, ast.Name) and _is_pii_name(a)]
    if not pii_args:
        return False
    func = node.func
    if isinstance(func, ast.Name):
        called_name = func.id
    elif isinstance(func, ast.Attribute):
        called_name = func.attr
    else:
        return True
    if called_name not in _APPROVED_REDACTORS:
        return True
    # Approved redactor: only valid for blob-path variables; it returns all other
    # PII variables unchanged, so _redact_blob_path(payload) is still a violation.
    return any(a.id not in _BLOB_PATH_VARIABLE_NAMES for a in pii_args)


def _fstring_contains_pii(node: ast.expr) -> bool:
    """True if an f-string (JoinedStr) interpolates a known PII variable or .email attribute."""
    if not isinstance(node, ast.JoinedStr):
        return False
    for value in node.values:
        if isinstance(value, ast.FormattedValue) and (
            _is_pii_name(value.value) or _contains_email_attribute(value.value)
        ):
            return True
    return False


def _contains_pii_name_outside_redactor(node: ast.expr) -> bool:
    """True if any _PII_VARIABLE_NAMES member appears anywhere in the expression tree,
    except within a _redact_blob_path(...) subtree applied to a blob-path variable.

    Catches nested patterns that evade the top-level checks:
        f"{sanitize_for_log(blob_path)}"   — wrapper inside an f-string slot
        {"key": blob_path}                 — PII inside a container literal

    Attribute accesses (e.g. payload.role) are NOT descended into: accessing a
    specific field of a PII variable is a different risk level from using the
    variable itself, and .email accesses are already caught by _contains_email_attribute.

    _redact_blob_path is treated as a safe sink ONLY when its direct args are
    blob-path variables.  Passing payload, eval, or download_filename to the
    redactor is still a violation because those are returned unchanged.
    """
    if isinstance(node, ast.Name):
        return _is_pii_name(node)
    if isinstance(node, ast.Attribute):
        # payload.role, blob_path.split etc. — specific field, not the whole object.
        return False
    if isinstance(node, ast.Call):
        func = node.func
        called_name = (
            func.id if isinstance(func, ast.Name)
            else func.attr if isinstance(func, ast.Attribute)
            else None
        )
        if called_name in _APPROVED_REDACTORS:
            # For each positional arg:
            #   - direct blob-path Name → safe (that is what _redact_blob_path handles)
            #   - direct non-blob-path PII Name → not safe, return True
            #   - any other expression (Call, Subscript, …) → recurse to detect nested PII
            # Same logic for keyword values so _redact_blob_path(blob_path=payload) is caught.
            for a in node.args:
                if isinstance(a, ast.Name):
                    if _is_pii_name(a) and a.id not in _BLOB_PATH_VARIABLE_NAMES:
                        return True
                elif _contains_pii_name_outside_redactor(a):
                    return True
            for kw in node.keywords:
                kv = kw.value
                if isinstance(kv, ast.Name):
                    if _is_pii_name(kv) and kv.id not in _BLOB_PATH_VARIABLE_NAMES:
                        return True
                elif _contains_pii_name_outside_redactor(kv):
                    return True
            return False  # safe: all args are blob-path variables or non-PII expressions
        return (
            (
                isinstance(func, ast.Attribute)
                and _contains_pii_name_outside_redactor(func.value)
            )
            or any(_contains_pii_name_outside_redactor(a) for a in node.args)
            or any(_contains_pii_name_outside_redactor(kw.value) for kw in node.keywords)
        )
    return any(
        _contains_pii_name_outside_redactor(child)
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.expr)
    )


def _all_logger_args(call: ast.Call):
    """Yield every argument value (positional and keyword) passed to a logger call."""
    yield from call.args
    for kw in call.keywords:
        yield kw.value


def test_no_json_dumps_in_logger_calls():
    """No logger call in the backend may pass json.dumps(...) as an argument."""
    violations = []
    for path in _backend_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(_BACKEND_ROOT)
        for call in _collect_logger_calls(tree):
            for arg in _all_logger_args(call):
                if any(_is_json_dumps_call(n) for n in ast.walk(arg)):
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(..., json.dumps(...), ...)"
                    )

    assert not violations, (
        "DIAAT-298 regression: logger calls must not pass json.dumps() — "
        "this dumps raw PII to Application Insights.\n" + "\n".join(violations)
    )


# Function calls whose return value is a safe, non-PII identifier even when a PII
# argument is passed in.  _redact_blob_path strips the email segment from a legacy
# user-uploads/{email}/{filename} path; extract_transcription_id_from_blob_path
# returns only the transcription UUID.
_SAFE_PII_TRANSFORMS: frozenset[str] = frozenset({
    "_redact_blob_path",
    "extract_transcription_id_from_blob_path",
})

# Function calls whose return value STILL embeds any PII passed in: string
# conversions and the log-sanitizer family escape/normalise text (str/repr render
# it; sanitize_for_log strips control characters to stop log injection) but none of
# them remove the PII itself.  So `blob_safe = sanitize_for_log(blob_name)` still
# embeds the email path segment — the exact pattern this PR removed from
# azure_utils.py — and taint must propagate through these calls.
#
# Every OTHER call is treated as OPAQUE: its result is NOT assumed to embed its
# arguments.  Assuming otherwise is unsound and produces false positives, because a
# call that merely receives PII routinely returns something derived-but-safe — a
# count (admin_list_users -> total), an unrelated identifier (submit_batch_job ->
# an Azure job URL), or a domain object whose safe fields are what get logged.
_PII_PRESERVING_CALLS: frozenset[str] = frozenset({
    "str",
    "repr",
    "sanitize_for_log",
    "_sanitize_for_log",
    # String-building methods that embed their positional arguments verbatim in the result.
    # "user={}".format(email) and ", ".join([email]) both produce a string that contains
    # the PII value, so the taint must propagate through them.
    "format",
    "join",
})


def _safe_transform_name(node: ast.Call) -> str | None:
    """Return the callee name of a call, or None if it is not a simple name/attribute call."""
    return (
        node.func.id if isinstance(node.func, ast.Name)
        else node.func.attr if isinstance(node.func, ast.Attribute)
        else None
    )


def _carries_pii(node: ast.expr, tainted: frozenset[str] = frozenset()) -> bool:
    """True when an expression's VALUE embeds PII: a known PII variable, an
    email attribute/variable, or a currently-tainted alias.

    The recursion mirrors the direct-argument guard so it does not over-flag:
      * a non-email attribute projection (user.id, payload.role) is SAFE — an
        Attribute node is judged only by _contains_email_attribute and is never
        descended into, so tainting the object 'user' does not taint 'user.id';
      * a safe transform (_SAFE_PII_TRANSFORMS) neutralises its arguments, so
        _redact_blob_path(blob_path) and the transcription-id extractor are clean;
      * a PII-preserving call (_PII_PRESERVING_CALLS: str/repr/sanitize_for_log) is
        descended, so sanitize_for_log(blob_name) still carries the email segment;
      * any OTHER call is OPAQUE — its result is not assumed to embed its arguments
        (avoids false positives on counts, ids and objects derived from PII args);
      * f-strings, string concatenation, containers and other wrappers ARE
        descended, so PII nested inside them is still caught.
    """
    if isinstance(node, ast.Name):
        return _is_pii_name(node) or node.id in tainted or _contains_email_named_var(node)
    if isinstance(node, ast.Attribute):
        return _contains_email_attribute(node)
    if isinstance(node, ast.Call):
        name = _safe_transform_name(node)
        if name in _SAFE_PII_TRANSFORMS or name not in _PII_PRESERVING_CALLS:
            return False
        return any(_carries_pii(a, tainted) for a in node.args) or any(
            _carries_pii(kw.value, tainted) for kw in node.keywords
        )
    if isinstance(node, ast.JoinedStr):
        return any(
            isinstance(part, ast.FormattedValue) and _carries_pii(part.value, tainted)
            for part in node.values
        )
    return any(
        _carries_pii(child, tainted)
        for child in ast.iter_child_nodes(node)
        if isinstance(child, ast.expr)
    )


def _tainted_names_used(node: ast.expr, tainted: frozenset[str]) -> frozenset[str]:
    """Return the tainted alias names used in a VALUE position within an expression.

    Same traversal as _carries_pii, but collects only names already in `tainted`
    (direct PII names and email accesses are the domain of the other guards).  A
    non-email attribute projection of a tainted object (user.id) yields nothing;
    safe transforms neutralise their arguments; and only PII-preserving calls
    (str/repr/sanitize_for_log) are descended, so an opaque call around a tainted
    value — logger.info("n=%d", len(entries)) — reports nothing.  This yields
    exactly the tainted-alias leaks the indirection guard is responsible for.
    """
    if isinstance(node, ast.Name):
        return frozenset({node.id}) if node.id in tainted else frozenset()
    if isinstance(node, ast.Attribute):
        return frozenset()
    if isinstance(node, ast.Call):
        name = _safe_transform_name(node)
        if name in _SAFE_PII_TRANSFORMS or name not in _PII_PRESERVING_CALLS:
            return frozenset()  # safe transform, or opaque call — arguments do not escape
        children: list[ast.expr] = [*node.args, *(kw.value for kw in node.keywords)]
    elif isinstance(node, ast.JoinedStr):
        children = [p.value for p in node.values if isinstance(p, ast.FormattedValue)]
    else:
        children = [c for c in ast.iter_child_nodes(node) if isinstance(c, ast.expr)]
    found: set[str] = set()
    for child in children:
        found |= _tainted_names_used(child, tainted)
    return frozenset(found)


def _is_pii_string_expression(node: ast.expr) -> bool:
    """True when an expression produces a value embedding PII, with no aliases tainted.

    Thin wrapper over _carries_pii with an empty taint set — used to decide whether a
    single assignment's right-hand side embeds PII directly (a bare PII name, an email
    attribute/variable, or an unsafe call such as sanitize_for_log(blob_name)).  The
    full indirection guard uses _carries_pii with the accumulated taint set so multi-hop
    aliases propagate; see _tainted_logger_arg_refs.
    """
    return _carries_pii(node, frozenset())


def _assignment_targets(node: ast.stmt) -> list[str]:
    """Yield the simple `x = ...`, `x: T = ...`, `x += ...` Name targets of an assignment.

    Tuple/list unpacking targets (a, b = f(...)) are deliberately NOT tainted: a
    function that merely receives PII does not make every element of a returned
    tuple sensitive (e.g. users, total = admin_list_users(search_email=...) — `total`
    is a count), and tainting them produces false positives.
    """
    targets: list[ast.expr] = []
    if isinstance(node, ast.Assign):
        targets = list(node.targets)
    elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
        targets = [node.target]
    return [t.id for t in targets if isinstance(t, ast.Name)]


_Violation = tuple[ast.Call, frozenset[str]]


def _apply_assignment(stmt: ast.stmt, taint: set[str]) -> None:
    """Update `taint` in place for an assignment (unconditional within its own block).

    The right-hand side is tainted if it embeds PII directly or references an
    already-tainted name; targets are then tainted, or CLEARED when the RHS is clean
    (a straight-line reassignment to a safe value untaints the variable).
    """
    rhs_tainted = _carries_pii(stmt.value, frozenset(taint))
    for name in _assignment_targets(stmt):
        if rhs_tainted or (isinstance(stmt, ast.AugAssign) and name in taint):
            taint.add(name)
        else:
            taint.discard(name)


def _header_logger_violations(stmt: ast.stmt, taint: set[str]) -> list[_Violation]:
    """Report logger calls in a statement's OWN expressions (never its nested blocks).

    iter_child_nodes yields a compound statement's header expressions (If.test,
    For.iter, an assignment value, an Expr's call) but its body/orelse are ast.stmt
    and are excluded here — they are handled by the block recursion so each logger
    call is evaluated against the taint state that actually reaches it.
    """
    frozen = frozenset(taint)
    out: list[_Violation] = []
    for child in ast.iter_child_nodes(stmt):
        if not isinstance(child, ast.expr):
            continue
        for call in _collect_logger_calls(child):
            refs: set[str] = set()
            for arg in _all_logger_args(call):
                refs |= _tainted_names_used(arg, frozen)
            if refs:
                out.append((call, frozenset(refs)))
    return out


def _flow_taint_loop(stmt: ast.stmt, taint: set[str]) -> tuple[set[str], list[_Violation]]:
    """For/AsyncFor/While: the body may run 0+ times and taint can carry across
    iterations, so iterate to a bounded fixed point, then merge the zero-iteration
    path (incoming taint) and any else-clause."""
    entry = set(taint)
    if isinstance(stmt, (ast.For, ast.AsyncFor)) and _carries_pii(stmt.iter, frozenset(taint)):
        entry |= {n.id for n in ast.walk(stmt.target) if isinstance(n, ast.Name)}
    cur = set(entry)
    for _ in range(8):
        body_taint, _ = _flow_taint(stmt.body, cur)
        if body_taint <= cur:
            break
        cur |= body_taint
    _, violations = _flow_taint(stmt.body, cur)
    out = set(taint) | cur
    else_taint, else_v = _flow_taint(stmt.orelse, out)
    return out | else_taint, violations + else_v


def _flow_taint_stmt(stmt: ast.stmt, taint_in: set[str]) -> tuple[set[str], list[_Violation]]:
    """Analyse one statement, returning (taint_out, violations)."""
    taint = set(taint_in)
    violations = _header_logger_violations(stmt, taint)
    if isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return taint, violations  # nested scope: analysed independently by the caller loop
    if isinstance(stmt, (ast.Assign, ast.AnnAssign, ast.AugAssign)) and getattr(stmt, "value", None):
        _apply_assignment(stmt, taint)
        return taint, violations
    if isinstance(stmt, ast.If):
        t1, v1 = _flow_taint(stmt.body, taint)
        t2, v2 = _flow_taint(stmt.orelse, taint)
        return t1 | t2, violations + v1 + v2  # merge branches: tainted on EITHER path stays tainted
    if isinstance(stmt, (ast.For, ast.AsyncFor, ast.While)):
        loop_taint, loop_v = _flow_taint_loop(stmt, taint)
        return loop_taint, violations + loop_v
    if isinstance(stmt, ast.Try):
        body_taint, body_v = _flow_taint(stmt.body, taint)
        out = set(body_taint)
        handler_seed = set(taint) | body_taint
        for handler in stmt.handlers:
            h_taint, h_v = _flow_taint(handler.body, handler_seed)
            out |= h_taint
            body_v += h_v
        else_taint, else_v = _flow_taint(stmt.orelse, body_taint)
        out |= else_taint
        final_taint, final_v = _flow_taint(stmt.finalbody, out)
        return final_taint, violations + body_v + else_v + final_v
    if isinstance(stmt, (ast.With, ast.AsyncWith)):
        with_taint, with_v = _flow_taint(stmt.body, taint)
        return with_taint, violations + with_v
    return taint, violations


def _flow_taint(stmts: list[ast.stmt], taint_in: set[str]) -> tuple[set[str], list[_Violation]]:
    """Analyse a block of statements in order, threading taint through each."""
    taint = set(taint_in)
    violations: list[_Violation] = []
    for stmt in stmts:
        taint, v = _flow_taint_stmt(stmt, taint)
        violations += v
    return taint, violations


def _tainted_logger_arg_refs(func: ast.AST) -> list[_Violation]:
    """Flow-sensitive taint analysis over a function body.

    A name becomes tainted when it is assigned an expression that embeds PII directly
    (a bare PII name, an email attribute/variable, an unsafe call such as
    sanitize_for_log(blob_name)) or references an already-tainted name — so multi-hop
    aliases propagate:

        identity = current_user.email          # tainted: .email attribute
        message  = f"User {identity}"           # tainted: references 'identity'
        logger.error(message)                   # flagged: 'message' reaches logger

    Analysis is flow-sensitive over the statement tree, NOT a flat source-order pass:
      * a straight-line clean reassignment clears taint (msg = "generic" untaints);
      * at an if/else join a name tainted on EITHER branch stays tainted, so
        `if c: msg = user.email else: msg = "x"; logger.info(msg)` is still flagged;
      * loops iterate to a fixed point so loop-carried taint is caught.

    A logger call is reported when a tainted name appears in one of its arguments —
    bare, in an f-string, in a container, or wrapped in a PII-preserving call —
    following _tainted_names_used, so a non-email projection of a tainted object
    (user.id) and opaque-call results (len(entries)) are NOT flagged.
    """
    _, violations = _flow_taint(func.body, frozenset())
    flagged: dict[int, _Violation] = {}
    for call, refs in violations:
        prev = flagged.get(id(call))
        flagged[id(call)] = (call, (prev[1] | refs) if prev else refs)
    return list(flagged.values())


def test_no_pii_in_logger_variable_indirection():
    """Catch PII embedded in an intermediate variable that is then passed to logger.

    The argument-level guard cannot see through one-hop assignment chains:
        error_msg = f"User not found: {user_email}"  # PII embedded here
        logger.error(error_msg)                       # guard sees only 'error_msg'

    String-building expressions (f-strings, concatenation) taint their target, and
    so do function calls that consume PII — e.g. blob_safe = sanitize_for_log(blob_name)
    — UNLESS the callee is an explicit safe transform (_SAFE_PII_TRANSFORMS).  Without
    the call case an alias could launder PII through sanitize_for_log, the exact
    unsafe pattern this PR replaced in azure_utils.py, without failing this guard.

    Taint is transitive across aliases and is detected wherever a tainted name
    appears in a logger argument (bare, in an f-string, a container, or a wrapper) —
    see _tainted_logger_arg_refs for the full analysis.
    """
    violations = []
    for path in _backend_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(_BACKEND_ROOT)
        for func in ast.walk(tree):
            if not isinstance(func, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            for call, refs in _tainted_logger_arg_refs(func):
                names = ", ".join(sorted(refs))
                violations.append(
                    f"  {rel}:{call.lineno}: logger.{call.func.attr}(...) references "
                    f"tainted name(s) {{{names}}} assigned an expression embedding PII"
                )

    assert not violations, (
        "DIAAT-298 regression: logger calls must not pass variables whose assigned string embeds PII.\n"
        + "\n".join(violations)
    )


def test_no_raw_pii_variable_in_logger_calls():
    """No logger call may pass a known PII variable directly, via str(), or in an f-string."""
    violations = []
    for path in _backend_source_files():
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source, filename=str(path))
        rel = path.relative_to(_BACKEND_ROOT)
        for call in _collect_logger_calls(tree):
            for arg in _all_logger_args(call):
                if _is_pii_name(arg):
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(..., {arg.id}, ...)"
                    )
                elif _contains_email_attribute(arg):
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(..., <obj>.email, ...)"
                    )
                elif _contains_email_named_var(arg):
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(..., <email_var>, ...)"
                    )
                elif _is_str_of_pii(arg):
                    inner = arg.args[0]
                    label = inner.attr if isinstance(inner, ast.Attribute) else inner.id
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(..., str({label}), ...)"
                    )
                elif _fstring_contains_pii(arg):
                    violations.append(
                        f'  {rel}:{call.lineno}: logger.{call.func.attr}(..., f"...{{pii_var}}...", ...)'
                    )
                elif _is_pii_in_unapproved_call(arg):
                    func = arg.func
                    func_name = func.id if isinstance(func, ast.Name) else func.attr
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(..., {func_name}(pii_var), ...) "
                        f"— use _redact_blob_path instead of {func_name}"
                    )
                elif _contains_pii_name_outside_redactor(arg):
                    violations.append(
                        f"  {rel}:{call.lineno}: logger.{call.func.attr}(...) contains PII variable "
                        f"nested in a wrapper or container — use _redact_blob_path"
                    )

    assert not violations, (
        "DIAAT-298 regression: known PII variables must not be passed directly to logger calls "
        "(use _redact_blob_path for blob paths, _sanitize_for_log on a safe identifier otherwise).\n"
        + "\n".join(violations)
    )


def test_pii_guard_catches_nested_pii():
    """_contains_pii_name_outside_redactor must detect PII nested in wrappers/containers
    and treat _redact_blob_path(...) as the explicit safe boundary only for blob paths.

    Regression cases for the reviewer comment: the guard previously missed
        f"{sanitize_for_log(blob_path)}"   — f-string wrapping an unapproved call
        {"key": blob_path}                 — PII inside a container literal
    and treated _redact_blob_path as a safe sink even for non-blob-path PII.
    """

    def _expr(source: str) -> ast.expr:
        return ast.parse(source, mode="eval").body

    # PII nested inside an unapproved wrapper inside an f-string — must be caught
    assert _contains_pii_name_outside_redactor(
        _expr('f"path={sanitize_for_log(blob_path)}"')
    ), "f-string wrapping unapproved sanitizer with PII arg must be flagged"

    # PII as a dict value — must be caught
    assert _contains_pii_name_outside_redactor(
        _expr('{"key": blob_path}')
    ), "dict literal containing PII variable must be flagged"

    # Approved redactor on a blob-path variable — must NOT be caught (safe)
    assert not _contains_pii_name_outside_redactor(
        _expr("_redact_blob_path(blob_path)")
    ), "_redact_blob_path(blob_path) must be treated as safe"

    # Approved redactor inside an f-string on a blob-path variable — must NOT be caught
    assert not _contains_pii_name_outside_redactor(
        _expr('f"path={_redact_blob_path(blob_path)}"')
    ), "f-string wrapping _redact_blob_path(blob_path) must be treated as safe"

    # Bare PII name — must be caught (basic sanity check)
    assert _contains_pii_name_outside_redactor(
        _expr("blob_path")
    ), "bare PII name must be flagged"

    # Non-PII variable — must NOT be caught
    assert not _contains_pii_name_outside_redactor(
        _expr("transcription_id")
    ), "non-PII variable must not be flagged"

    # Attribute access on a PII object (e.g. payload.role) — safe field, must NOT be caught;
    # .email accesses are handled separately by _contains_email_attribute
    assert not _contains_pii_name_outside_redactor(
        _expr("payload.role")
    ), "attribute access on PII variable (payload.role) must not be flagged"

    # Unapproved wrapper around a safe attribute access — must NOT be caught
    assert not _contains_pii_name_outside_redactor(
        _expr("sanitize_for_log(payload.role)")
    ), "sanitize_for_log(payload.role) must not be flagged (role is a safe field)"

    # Non-blob-path PII passed to approved redactor — MUST be caught.
    # _redact_blob_path returns payload/eval/download_filename unchanged because
    # they are not user-uploads/... paths; wrapping them in the redactor is a no-op.
    assert _contains_pii_name_outside_redactor(
        _expr("_redact_blob_path(payload)")
    ), "_redact_blob_path(payload) must be flagged — payload is not a blob path"

    assert _contains_pii_name_outside_redactor(
        _expr("_redact_blob_path(eval)")
    ), "_redact_blob_path(eval) must be flagged — eval is not a blob path"

    assert _contains_pii_name_outside_redactor(
        _expr("_redact_blob_path(download_filename)")
    ), "_redact_blob_path(download_filename) must be flagged — not a blob path"

    # Nested PII inside the approved redactor's positional arg — must be caught.
    # _redact_blob_path(sanitize_for_log(blob_path)) is wrong; the correct form is
    # _redact_blob_path(blob_path).  Nesting a non-approved wrapper inside it bypasses
    # the path-segment email redaction so the guard must reject it.
    assert _contains_pii_name_outside_redactor(
        _expr("_redact_blob_path(sanitize_for_log(blob_path))")
    ), "_redact_blob_path(sanitize_for_log(blob_path)) must be flagged — PII nested in positional arg"

    # PII passed as a keyword argument to the approved redactor — must be caught.
    assert _contains_pii_name_outside_redactor(
        _expr("_redact_blob_path(blob_path=payload)")
    ), "_redact_blob_path(blob_path=payload) must be flagged — non-blob PII in keyword arg"


def test_indirection_guard_catches_email_attribute_alias():
    """Regression: identity = current_user.email; logger.info(identity) must be caught.

    _is_pii_string_expression previously returned False for Attribute nodes, so
    'identity = current_user.email' did not taint 'identity' and the subsequent
    logger call bypassed the indirection guard entirely.
    """

    def _expr(src: str) -> ast.expr:
        return ast.parse(src, mode="eval").body

    # Direct .email attribute access — must taint the alias
    assert _is_pii_string_expression(
        _expr("current_user.email")
    ), "current_user.email must be treated as a PII expression"

    assert _is_pii_string_expression(
        _expr("user.email")
    ), "user.email must be treated as a PII expression"

    # Non-email attributes on the same object — must NOT taint
    assert not _is_pii_string_expression(
        _expr("current_user.id")
    ), "current_user.id must not be treated as PII"

    assert not _is_pii_string_expression(
        _expr("current_user.role")
    ), "current_user.role must not be treated as PII"


def test_indirection_guard_catches_sanitized_pii_alias():
    """Regression: blob_safe = sanitize_for_log(blob_name); logger.info(blob_safe) must be caught.

    _is_pii_string_expression previously returned False for every ast.Call, so a
    call that only escapes control characters (sanitize_for_log) could launder a
    PII blob path into an alias that then passed the indirection guard — the exact
    unsafe pattern this PR replaced in azure_utils.py.  Calls consuming PII now
    taint their target unless the callee is an explicit safe transform.
    """

    def _expr(src: str) -> ast.expr:
        return ast.parse(src, mode="eval").body

    # Unsafe wrapper consuming a PII blob path — must taint the alias
    assert _is_pii_string_expression(
        _expr("sanitize_for_log(blob_name)")
    ), "sanitize_for_log(blob_name) must taint the alias — it leaves the email segment intact"

    assert _is_pii_string_expression(
        _expr("_sanitize_for_log(blob_path)")
    ), "_sanitize_for_log(blob_path) must taint the alias"

    # Unsafe wrapper consuming an email argument — must taint (positional and keyword)
    assert _is_pii_string_expression(
        _expr("sanitize_for_log(user_email)")
    ), "sanitize_for_log(user_email) must taint the alias"

    # String conversions preserve their argument's PII — must taint
    assert _is_pii_string_expression(
        _expr("str(blob_path)")
    ), "str(blob_path) must taint the alias — string conversion preserves the PII"

    # An arbitrary call that merely RECEIVES PII is opaque: its result is not assumed
    # to embed the argument.  Assuming otherwise false-positives on counts / ids /
    # objects derived from a PII arg (admin_list_users -> total, submit_batch_job ->
    # url, get_or_create_user_by_email -> user).  Direct logging of such a call is
    # still caught by the argument-level guard's _contains_email_attribute.
    assert not _is_pii_string_expression(
        _expr("build_msg(email=current_user.email)")
    ), "an unknown call receiving PII is opaque and must not taint its result"

    assert not _is_pii_string_expression(
        _expr("submit_batch_job(sas_url)")
    ), "an opaque call result must not be tainted by a non-preserving argument"

    # Approved safe transforms — must NOT taint their result
    assert not _is_pii_string_expression(
        _expr("_redact_blob_path(blob_name)")
    ), "_redact_blob_path(blob_name) is a safe transform and must not taint the alias"

    assert not _is_pii_string_expression(
        _expr("extract_transcription_id_from_blob_path(blob_path, str(user.id))")
    ), "the transcription-id extractor is a safe transform and must not taint the alias"

    # Call with no PII arguments — must NOT taint
    assert not _is_pii_string_expression(
        _expr("compute_checksum(container_name)")
    ), "a call with no PII arguments must not taint the alias"


def test_email_attribute_guard_catches_suffixed_attributes():
    """Regression: request.user_email / record.admin_email are PII too.

    _contains_email_attribute previously matched only n.attr == "email", so
    attributes ending in _email passed every guard and could be logged.
    """

    def _expr(src: str) -> ast.expr:
        return ast.parse(src, mode="eval").body

    # Bare .email — still caught
    assert _contains_email_attribute(_expr("current_user.email"))

    # _email-suffixed attributes — must now be caught, in every wrapping form
    assert _contains_email_attribute(_expr("request.user_email")), "request.user_email must be flagged"
    assert _contains_email_attribute(_expr("record.admin_email")), "record.admin_email must be flagged"
    assert _contains_email_attribute(
        _expr('f"owner={obj.target_owner_email}"')
    ), "_email attribute inside an f-string must be flagged"
    assert _contains_email_attribute(
        _expr("sanitize_for_log(request.user_email)")
    ), "_email attribute wrapped in a call must be flagged"

    # And they taint an alias through _is_pii_string_expression
    assert _is_pii_string_expression(_expr("request.user_email"))

    # Non-email attributes — must NOT be flagged
    assert not _contains_email_attribute(_expr("current_user.id"))
    assert not _contains_email_attribute(_expr("obj.email_count")), (
        "email_count does not hold an address and must not be flagged"
    )
    assert not _contains_email_attribute(_expr("obj.emailer"))


def test_indirection_guard_follows_multihop_and_nested_refs():
    """Regression for the two indirection-guard gaps:

    1. Tainted alias nested in an f-string / container / wrapper (not a bare Name):
           identity = current_user.email
           logger.info(f"user={identity}")        # must be caught
    2. Multi-hop taint propagated through an already-tainted alias:
           identity = current_user.email
           message = f"User {identity}"
           logger.error(message)                   # must be caught

    Also asserts clean reassignment still clears taint (no false positive).
    """

    def _func(src: str) -> ast.AST:
        return ast.parse(src).body[0]

    def _flagged_lines(src: str) -> set[int]:
        return {call.lineno for call, _ in _tainted_logger_arg_refs(_func(src))}

    # (1) tainted alias nested inside an f-string argument
    nested_fstring = (
        "def f():\n"
        "    identity = current_user.email\n"
        "    logger.info(f'user={identity}')\n"
    )
    assert _flagged_lines(nested_fstring) == {3}, "tainted alias inside an f-string arg must be caught"

    # tainted alias nested inside a container literal
    nested_container = (
        "def f():\n"
        "    identity = current_user.email\n"
        "    logger.info('ctx', {'user': identity})\n"
    )
    assert _flagged_lines(nested_container) == {3}, "tainted alias inside a dict arg must be caught"

    # tainted alias wrapped in a call
    nested_wrapper = (
        "def f():\n"
        "    identity = current_user.email\n"
        "    logger.info(sanitize_for_log(identity))\n"
    )
    assert _flagged_lines(nested_wrapper) == {3}, "tainted alias wrapped in a call must be caught"

    # (2) multi-hop alias chain
    multihop = (
        "def f():\n"
        "    identity = current_user.email\n"
        "    message = f'User {identity}'\n"
        "    logger.error(message)\n"
    )
    assert _flagged_lines(multihop) == {4}, "multi-hop tainted alias must be caught"

    # three-hop chain, tainted through a plain concatenation alias
    three_hop = (
        "def f():\n"
        "    a = payload\n"
        "    b = a\n"
        "    c = 'prefix ' + b\n"
        "    logger.info(c)\n"
    )
    assert _flagged_lines(three_hop) == {5}, "three-hop tainted alias must be caught"

    # clean reassignment clears taint — must NOT be flagged
    reassigned_clean = (
        "def f():\n"
        "    msg = f'error for {user_email}'\n"
        "    msg = 'generic error'\n"
        "    logger.info(msg)\n"
    )
    assert _flagged_lines(reassigned_clean) == set(), "clean reassignment must clear taint"

    # alias of a safe value — must NOT be flagged
    safe_alias = (
        "def f():\n"
        "    safe = _redact_blob_path(blob_name)\n"
        "    logger.info(f'blob={safe}')\n"
    )
    assert _flagged_lines(safe_alias) == set(), "alias of a safe-transform result must not be flagged"

    # count/aggregate of a tainted collection — must NOT be flagged (real-code idiom)
    logged_count = (
        "def f():\n"
        "    entries = payload\n"
        "    logger.info('retrieved %d entries', len(entries))\n"
    )
    assert _flagged_lines(logged_count) == set(), "len() of a tainted value is a safe count, not a leak"

    # opaque call result (function that merely receives PII) — must NOT be tainted
    opaque_result = (
        "def f():\n"
        "    job_url = submit_batch_job(blob_path)\n"
        "    logger.info('job=%s', job_url)\n"
    )
    assert _flagged_lines(opaque_result) == set(), "result of an opaque call must not be treated as tainted"

    # safe attribute projection of a tainted object — must NOT be flagged
    safe_attribute = (
        "def f():\n"
        "    user = get_or_create_user_by_email(user_email)\n"
        "    logger.info('user_id=%s', user.id)\n"
    )
    assert _flagged_lines(safe_attribute) == set(), "non-email attribute of a tainted object must not be flagged"

    # ...but the email attribute of that same object still taints a downstream alias
    email_projection = (
        "def f():\n"
        "    addr = current_user.email\n"
        "    logger.info('addr=%s', addr)\n"
    )
    assert _flagged_lines(email_projection) == {3}, "aliased .email attribute must still be caught"

    # flow-sensitive branch merge: a clean reassignment on ONE branch must not clear
    # taint set on the other branch — the tainted path still reaches the logger call.
    branch_taint = (
        "def f(cond):\n"
        "    if cond:\n"
        "        msg = current_user.email\n"
        "    else:\n"
        "        msg = 'generic'\n"
        "    logger.info(msg)\n"
    )
    assert _flagged_lines(branch_taint) == {6}, (
        "taint from one branch must survive the join even if the other branch is clean"
    )

    # straight-line clean reassignment (both at the same block level) still clears taint
    straightline_clean = (
        "def f():\n"
        "    msg = current_user.email\n"
        "    msg = 'generic'\n"
        "    logger.info(msg)\n"
    )
    assert _flagged_lines(straightline_clean) == set(), (
        "an unconditional clean reassignment must clear taint"
    )


def test_string_format_and_join_carry_pii():
    """Regression: format() and join() are PII-preserving — taint must propagate through them.

    Previously every call not explicitly listed in _PII_PRESERVING_CALLS was treated as
    opaque, so 'msg = "user={}".format(current_user.email)' left msg untainted and
    'logger.info(msg)' produced no violation.  Adding format/join to _PII_PRESERVING_CALLS
    makes _carries_pii descend into their positional arguments.
    """

    def _expr(src: str) -> ast.expr:
        return ast.parse(src, mode="eval").body

    def _func(src: str) -> ast.AST:
        return ast.parse(src).body[0]

    def _flagged_lines(src: str) -> set[int]:
        return {call.lineno for call, _ in _tainted_logger_arg_refs(_func(src))}

    # _carries_pii: format() embedding an email attribute — must be True
    assert _carries_pii(
        _expr('"user={}".format(current_user.email)'), frozenset()
    ), '"user={}".format(current_user.email) must carry PII — format embeds its args'

    # _carries_pii: join() embedding an email-named variable — must be True
    assert _carries_pii(
        _expr('"sep".join([user_email])'), frozenset()
    ), '"sep".join([user_email]) must carry PII — join embeds its args'

    # _is_pii_string_expression: single-call shorthand used by the assignment taint step
    assert _is_pii_string_expression(
        _expr('"user={}".format(current_user.email)')
    ), '_is_pii_string_expression must flag "user={}".format(current_user.email)'

    # Full indirection chain — the exact bypass reported by the reviewer
    format_chain = (
        "def f():\n"
        "    msg = 'user={}'.format(current_user.email)\n"
        "    logger.info(msg)\n"
    )
    assert _flagged_lines(format_chain) == {3}, (
        "logger.info(msg) where msg = 'user={}'.format(current_user.email) must be caught"
    )

    # join() equivalent
    join_chain = (
        "def f():\n"
        "    msg = ', '.join([user_email])\n"
        "    logger.info(msg)\n"
    )
    assert _flagged_lines(join_chain) == {3}, (
        "logger.info(msg) where msg = ', '.join([user_email]) must be caught"
    )

    # Safe case: format with no PII args — must NOT be flagged
    assert not _carries_pii(
        _expr('"count={}".format(total)'), frozenset()
    ), '"count={}".format(total) must not carry PII — total is not a PII variable'
