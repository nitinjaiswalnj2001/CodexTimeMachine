"""Conservative rule-based classification and deterministic redaction."""

from __future__ import annotations

import re

from .models import CommandTag


# A shell token may begin only at the command start or after an observable shell
# separator.  Deliberately do not use arbitrary whitespace as a boundary: it
# would classify data in commands such as ``echo pytest``.
_BOUNDARY = r"(?:^|[;&|()])\s*"
_TEST = re.compile(_BOUNDARY + r"(?:pytest|py\.test|python(?:3)?\s+-m\s+(?:pytest|unittest)|unittest)(?:\s|$)", re.I)
_COMPILE = re.compile(_BOUNDARY + r"python(?:3)?\s+-m\s+compileall(?:\s|$)", re.I)
_GIT = re.compile(_BOUNDARY + r"git\s+(?:status|log)(?:\s|$)", re.I)
_FILE_INSPECTION = re.compile(_BOUNDARY + r"(?:cat|sed|rg|grep|head|tail)\b", re.I)
_REPOSITORY_INSPECTION = re.compile(_BOUNDARY + r"(?:find|fd|tree|ls|dir)\b", re.I)
_EVALUATION = re.compile(_BOUNDARY + r"(?:benchmark|bench|eval|evaluate)(?:\s|$)", re.I)
_SHELL_WRAPPER = re.compile(
    r"(?:^|[;&|()]\s*)(?:/bin/)?(?:bash|sh)\s+-c\s+(?P<quote>['\"])(?P<body>.*?)(?P=quote)",
    re.IGNORECASE | re.DOTALL,
)

_SECRET_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(?i)(authorization\s*:\s*)(?:bearer\s+)?[^\s\r\n]+"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]+"),
    re.compile(r"(?i)\b((?:OPENAI_API_KEY|CODEX_ACCESS_TOKEN|GITHUB_TOKEN)\s*[=:]\s*)[^\s\r\n]+"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b"),
    re.compile(r"TM_(?:FUTURE|OUTSIDE|HOME)_[A-Z0-9_]+"),
)


def _command_candidates(command: str) -> list[str]:
    """Return observable shell bodies without trying to fully parse shell syntax."""
    candidates = [command]
    frontier = [command]
    # A small bounded pass supports a wrapper nested inside a shell body without
    # treating arbitrary quoted command data as executable syntax.
    for _ in range(4):
        discovered: list[str] = []
        for value in frontier:
            discovered.extend(match.group("body") for match in _SHELL_WRAPPER.finditer(value))
        if not discovered:
            break
        candidates.extend(discovered)
        frontier = discovered
    return candidates


def classify_command(command: str) -> list[CommandTag]:
    tags: set[CommandTag] = set()
    candidates = _command_candidates(command)
    if any(_TEST.search(candidate) for candidate in candidates):
        tags.add(CommandTag.TEST_EXECUTION)
    if any(_COMPILE.search(candidate) for candidate in candidates):
        tags.add(CommandTag.COMPILATION)
    if any(_GIT.search(candidate) for candidate in candidates):
        tags.add(CommandTag.GIT_INSPECTION)
    if any(_FILE_INSPECTION.search(candidate) for candidate in candidates):
        tags.add(CommandTag.FILE_INSPECTION)
    if any(_REPOSITORY_INSPECTION.search(candidate) for candidate in candidates):
        tags.add(CommandTag.REPOSITORY_INSPECTION)
    if any(_EVALUATION.search(candidate) for candidate in candidates):
        tags.add(CommandTag.EVALUATION)
    if not tags:
        tags.add(CommandTag.OTHER)
    return sorted(tags, key=lambda tag: tag.value)


def redact_text(value: str) -> tuple[str, bool]:
    redacted = value
    for pattern in _SECRET_PATTERNS:
        if pattern.groups:
            redacted = pattern.sub(lambda match: f"{match.group(1)}[REDACTED]", redacted)
        else:
            redacted = pattern.sub("[REDACTED]", redacted)
    return redacted, redacted != value
