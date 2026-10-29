"""DIAAT-287 AC3: scan everything a test exported for prohibited content (one engine for
both areas).

Independent of the service's redaction rules: if a rule is removed or a new code path
logs something it should not, the scan still fails. Two kinds of check:

* seeded values: each area's realistic case data (tests/unit/dictation/unit/validation/
  pii_scan.py, tests/unit/recording/unit/validation/prohibited_content.py). None of
  them may appear anywhere;
* pattern classes (``PATTERNS``): emails, UK case references, SAS signatures, bearer
  tokens and JWTs, UK postcodes and National Insurance numbers, whatever the value.
  Each class is the union of both areas' expressions. ``RE2_PATTERNS`` are the
  lookaround-free ones, the same as the deployed sample scan (prohibited_content_scan.kql
  in transcribe-shared-infrastructure) can run.

Findings name the seed or pattern and where it was found, never the matched text, so
the evidence file (``test-results/telemetry-pii-scan.json``, one for the whole session:
tests/unit/conftest.py writes it) can be attached to the release evidence as it is.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from dataclasses import asdict, dataclass, field
from typing import Any

from hmcts_otel import conventions as c
from hmcts_otel.testing import TelemetryCapture

# Pattern classes --------------------------------------------------------------------------

# Dictation's: RE2 syntax (no lookarounds), as the deployed sample scan uses.
RE2_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}"
    ),
    # First-tier Tribunal (IAC) appeal numbers (PA/50123/2024, HU-01234-2023) and
    # Upper Tribunal references (UI-2024-001234).
    "uk_case_reference": re.compile(r"\b(?:[A-Z]{2}[/-]\d{5}[/-]\d{4}|UI-\d{4}-\d{6})\b"),
    # Azure SAS signature: sig= and a value (sig=[REDACTED] is fine).
    "sas_signature": re.compile(r"(?i)(?:^|[^a-z0-9_])sig=[A-Za-z0-9%+/=]{8,}"),
    "bearer_token": re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+/-]{8,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]*"),
    "uk_postcode": re.compile(r"\b(?:[A-PR-UWYZ][A-HK-Y]?\d[A-HJKPSTUW\d]?|GIR) ?\d[ABD-HJLNP-UW-Z]{2}\b"),
    "ni_number": re.compile(r"\b[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z] ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"),
}

# Recording's, which also cover case references in file names (PA_50123_2024), Crown
# Court and county/family court case numbers, and exclude invalid NI prefixes.
_RECORDING_PATTERNS: dict[str, re.Pattern[str]] = {
    "email": re.compile(r"[A-Za-z0-9._%+'-]{1,64}@[A-Za-z0-9-]{1,63}(?:\.[A-Za-z0-9-]{1,63})*\.[A-Za-z]{2,24}"),
    # IAC appeal references (PA/50123/2024, PA_50123_2024 in file names), Crown Court
    # case numbers (T20247001) and county/family court case numbers (ZW24C50012).
    "uk_case_reference": re.compile(
        r"(?i:(?<![A-Za-z0-9])[A-Z]{2}[/_ ]\d{5}[/_ ]\d{4}(?![0-9]))"
        r"|\bT\d{8}\b"
        r"|\b[A-Z]{2}\d{2}[A-Z]\d{5}\b"
    ),
    # A SAS token's signature value; "sig=[REDACTED]" is the redacted form, allowed.
    "sas_signature": re.compile(r"(?i)\bsig=[A-Za-z0-9%+/]{8,}"),
    "bearer_token": re.compile(r"(?i)\bbearer\s{1,8}[A-Za-z0-9._~+/-]{16,}"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_-]{2,}\.[A-Za-z0-9_-]{2,}\."),
    "uk_postcode": re.compile(r"\b(?:[A-PR-UWYZ][A-HK-Y]?\d[A-Z\d]?) ?\d[ABD-HJLNP-UW-Z]{2}\b"),
    "ni_number": re.compile(
        r"\b(?!BG|GB|NK|KN|TN|NT|ZZ)[A-CEGHJ-PR-TW-Z][A-CEGHJ-NPR-TW-Z]"
        r" ?\d{2} ?\d{2} ?\d{2} ?[A-D]\b"
    ),
}


def _scoped(pattern: str) -> str:
    """A leading global (?i) flag as a scoped group, so the pattern can sit in a union."""
    return f"(?i:{pattern[4:]})" if pattern.startswith("(?i)") else pattern


# One expression per class: either area's matches it.
PATTERNS: dict[str, re.Pattern[str]] = {
    name: re.compile(f"(?:{_scoped(RE2_PATTERNS[name].pattern)})|(?:{_scoped(_RECORDING_PATTERNS[name].pattern)})")
    for name in RE2_PATTERNS
}

# Collection -------------------------------------------------------------------------------


@dataclass(frozen=True)
class Exported:
    """One exported string and where it came from (e.g. "log attribute")."""

    where: str
    text: str


@dataclass
class Counts:
    spans: int = 0
    span_events: int = 0
    log_records: int = 0
    custom_events: int = 0
    metric_points: int = 0
    attributes: int = 0

    def add(self, other: Counts) -> None:
        for name in self.__dataclass_fields__:
            setattr(self, name, getattr(self, name) + getattr(other, name))


def _attribute_items(where: str, attributes: Any, counts: Counts) -> Iterator[Exported]:
    for key, value in dict(attributes or {}).items():
        counts.attributes += 1
        yield Exported(f"{where} key", str(key))
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        yield Exported(where, text)


def exported_items(tel: TelemetryCapture) -> tuple[list[Exported], Counts]:
    """Every string the capture would export: spans (name, attributes, status, events and
    their attributes, links' attributes), log records (body, attributes, so custom events
    too), metric data point attributes, and the resource attributes exported with each
    of the three signals (service name, environment, anything from
    OTEL_RESOURCE_ATTRIBUTES). Each distinct resource is scanned once per signal."""
    counts = Counts()
    items: list[Exported] = []
    seen_resources: set[tuple[str, int]] = set()

    def resource_items(signal: str, resource: Any) -> None:
        if resource is None or (signal, id(resource)) in seen_resources:
            return
        seen_resources.add((signal, id(resource)))
        items.extend(_attribute_items(f"{signal} resource attribute", resource.attributes, counts))

    for span in tel.finished_spans():
        counts.spans += 1
        items.append(Exported("span name", span.name))
        items.extend(_attribute_items("span attribute", span.attributes, counts))
        if span.status.description:
            items.append(Exported("span status", span.status.description))
        for event in span.events:
            counts.span_events += 1
            items.append(Exported("span event name", event.name))
            items.extend(_attribute_items("span event attribute", event.attributes, counts))
        for link in span.links:
            items.extend(_attribute_items("span link attribute", link.attributes, counts))
        resource_items("span", span.resource)
    for readable in tel.logs.get_finished_logs():
        resource_items("log", getattr(readable, "resource", None))
    for record in tel.log_records():
        counts.log_records += 1
        attributes = dict(record.attributes or {})
        if attributes.get(c.CUSTOM_EVENT_NAME) is not None:
            counts.custom_events += 1
        items.append(Exported("log body", str(record.body)))
        items.extend(_attribute_items("log attribute", attributes, counts))
    data = tel.metrics.get_metrics_data()
    if data is not None:
        for resource_metrics in data.resource_metrics:
            resource_items("metric", resource_metrics.resource)
            for scope_metrics in resource_metrics.scope_metrics:
                for metric in scope_metrics.metrics:
                    for point in metric.data.data_points:
                        counts.metric_points += 1
                        items.extend(_attribute_items("metric attribute", point.attributes, counts))
    return items, counts


@dataclass(frozen=True)
class Finding:
    kind: str  # "seeded" or "pattern"
    name: str  # the seed label or pattern name: never the matched text
    where: str

    def as_dict(self) -> dict[str, str]:
        return {"kind": self.kind, "name": self.name, "where": self.where}


@dataclass
class ScanResult:
    counts: Counts
    findings: list[Finding] = field(default_factory=list)

    @property
    def passed(self) -> bool:
        return not self.findings

    def summary(self) -> str:
        return ", ".join(sorted({f"{f.kind} '{f.name}' in {f.where}" for f in self.findings}))


def scan_items(
    items: list[Exported],
    seeded: Mapping[str, str],
    patterns: Mapping[str, re.Pattern[str]] | None = None,
) -> list[Finding]:
    """Every seeded value and pattern class over the given strings."""
    patterns = PATTERNS if patterns is None else patterns
    findings: set[Finding] = set()
    for item in items:
        for label, value in seeded.items():
            if value and value in item.text:
                findings.add(Finding("seeded", label, item.where))
        for name, pattern in patterns.items():
            if pattern.search(item.text):
                findings.add(Finding("pattern", name, item.where))
    return sorted(findings, key=lambda f: (f.kind, f.name, f.where))


def scan(
    tel: TelemetryCapture,
    seeded: Mapping[str, str],
    patterns: Mapping[str, re.Pattern[str]] | None = None,
) -> ScanResult:
    """Every seeded value and pattern class, over everything the capture exported."""
    items, counts = exported_items(tel)
    return ScanResult(counts, scan_items(items, seeded, patterns))


# Evidence (one file per session, written by tests/unit/conftest.py) ------------------------

SCENARIOS: list[dict[str, Any]] = []
SEEDED_LABELS: dict[str, list[str]] = {}  # area -> seed labels (never the values)


def record_scenario(
    area: str,
    nodeid: str,
    result: ScanResult,
    seeded: Mapping[str, str],
    notes: Mapping[str, Any] | None = None,
) -> None:
    """Add one scanned scenario to the session's evidence."""
    SEEDED_LABELS[area] = sorted(seeded)
    SCENARIOS.append(
        {
            "area": area,
            "scenario": nodeid,
            "counts": asdict(result.counts),
            "findings": [finding.as_dict() for finding in result.findings],
            "result": "pass" if result.passed else "fail",
            **(notes or {}),
        }
    )


def evidence(**metadata: Any) -> dict[str, Any]:
    """The session's evidence: every scenario, totals, the checks (labels and patterns)."""
    totals = Counts()
    for scenario in SCENARIOS:
        totals.add(Counts(**scenario["counts"]))
    failed = [s["scenario"] for s in SCENARIOS if s["result"] != "pass"]
    return {
        "check": "DIAAT-287 automated prohibited-content (PII) scan of exported telemetry",
        "epic": "DIAAT-205",
        "ticket": "DIAAT-287",
        **metadata,
        "result": "fail" if failed else "pass",
        "scenarios_scanned": len(SCENARIOS),
        "scenarios_failed": failed,
        "totals": asdict(totals),
        "seeded_values": SEEDED_LABELS,
        "patterns": {name: pattern.pattern for name, pattern in PATTERNS.items()},
        "scenarios": SCENARIOS,
    }


def write_evidence(path: Any, **metadata: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(evidence(**metadata), indent=2) + "\n", encoding="utf-8")
