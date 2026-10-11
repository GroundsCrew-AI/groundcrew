"""Build and container output for the timeline and for Builder's prompt: redacted, scrubbed of
known secret values, and cut to the tail, where the error is."""

import re

from app.security.redact import REDACTED, redact

# BuildKit (--progress=plain): `ERROR: failed to solve: process "/bin/sh -c pip install ..."
# did not complete successfully: exit code: 1`. Older output says `executor failed running`.
_FAILED_PROCESS = re.compile(
    r'(?:process|executor failed running) "?(?:/bin/sh -c )?(?P<cmd>.+?)"? '
    r"(?:did not complete successfully|: exit code)",
)
_FAILED_STEP = re.compile(r"^#\d+ \[[^\]]*\] (?P<step>(?:RUN|COPY|ADD|FROM) .+)$", re.MULTILINE)
_ERROR_LINE = re.compile(
    r"(?i)\b(error|fatal|not found|no such file|cannot|could not|failed|"
    r"no matching distribution|modulenotfounderror|exit code)\b"
)
# BuildKit's step and timestamp prefixes: "#8 55.74 ", "55.74 ", "#8 ".
_PREFIX = re.compile(r"^(?:#\d+ )?(?:\d+\.\d+ )?")
# pip's generic footer, repeated on every failure; it never names the cause.
_BOILERPLATE = re.compile(
    r"(?i)(this error originates from a subprocess|see above for output|"
    r"a new release of pip|to update, run)"
)
# Lines that only repeat the step header or BuildKit's own wrapper.
_NOISE = re.compile(r"^(#\d+ (DONE|CACHED|\d+\.\d+ \^+)|-{3,}|\s*>{3}|\s*\d+ \|)")


class Scrubber:
    """Replaces exact secret values (registry passwords, secret env values) before redact()
    runs its patterns, so a value no pattern knows still never leaves the sandbox."""

    def __init__(self, values: list[str] | None = None) -> None:
        self._values: set[str] = set()
        for value in values or []:
            self.add(value)

    def add(self, value: str) -> None:
        # Very short values would blank out ordinary words; they are rejected upstream instead.
        if len(value) >= 4:
            self._values.add(value)

    def __call__(self, text: str) -> str:
        for value in sorted(self._values, key=len, reverse=True):
            text = text.replace(value, REDACTED)
        return redact(text)


def tail(text: str, limit: int, scrub: Scrubber | None = None) -> str:
    """Scrub, then keep the last `limit` characters, starting at a line boundary."""
    clean = (scrub or Scrubber())(text)
    if len(clean) <= limit:
        return clean
    cut = clean[-limit:]
    newline = cut.find("\n")
    if 0 <= newline < 200:
        cut = cut[newline + 1 :]
    return f"[... {len(clean) - len(cut)} earlier characters cut]\n{cut}"


def failing_step(log: str) -> str | None:
    """The Dockerfile instruction that failed, from BuildKit's plain output."""
    found = _FAILED_PROCESS.findall(log)
    if found:
        return f"RUN {found[-1].strip()}"
    errored = [m for m in _FAILED_STEP.finditer(log) if "ERROR" in log[m.end() : m.end() + 2000]]
    return errored[-1].group("step").strip() if errored else None


def error_lines(log: str, limit: int = 12) -> list[str]:
    """Lines that look like the cause, in log order, deduplicated; the last `limit` of them."""
    picked: list[str] = []
    for raw in log.splitlines():
        line = _PREFIX.sub("", raw).strip()
        if not line or _NOISE.match(raw) or _BOILERPLATE.search(line):
            continue
        if not _ERROR_LINE.search(line):
            continue
        if line not in picked:
            picked.append(line[:300])
    return picked[-limit:]
