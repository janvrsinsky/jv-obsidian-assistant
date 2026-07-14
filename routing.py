"""Note-ownership routing and the append-vs-surface write discipline.

This is the policy layer that sits in front of the raw filesystem tools. Given a
plain-language fact ("supplier confirmed the spindle motors ship Friday"), it
decides:

  1. which note *owns* that piece of truth (a project card, an area note, or the
     inbox as fallback), and
  2. where inside that note the fact goes (a `## Log` section), always by
     appending, never by overwriting.

Ownership is scored, not hard-coded: each note advertises its identity through
frontmatter (title, aliases, keywords) and its first heading. A fact is matched
against those signals by shared significant tokens. If nothing scores, the fact
is a raw capture and lands in the inbox for the human to triage. That mirrors the
real rule the assistant follows: state lands where it can be found again, and the
human stays the editor.

Pure standard library so it is testable without the MCP framework.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from mcp_server import safe_resolve, read_file, write_file, VAULT_ROOT

# ---------------------------------------------------------------------------
# Tokenisation
# ---------------------------------------------------------------------------

# Small English stopword set; routing should key off content words, not glue.
_STOPWORDS = {
    "the", "and", "for", "with", "that", "this", "from", "into", "onto", "your",
    "our", "are", "was", "were", "has", "have", "had", "will", "would", "should",
    "about", "over", "under", "than", "then", "them", "they", "you", "not", "but",
    "all", "any", "can", "did", "does", "done", "get", "got", "how", "its", "let",
    "new", "now", "off", "out", "per", "put", "ran", "run", "set", "who", "why",
}

_WORD_RE = re.compile(r"[a-z0-9]+")


def _tokens(text: str) -> set[str]:
    """Lowercase content tokens of length >= 3 that are not stopwords."""
    return {
        t
        for t in _WORD_RE.findall((text or "").lower())
        if len(t) >= 3 and t not in _STOPWORDS
    }


# ---------------------------------------------------------------------------
# Note model
# ---------------------------------------------------------------------------

_FM_BLOCK_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.DOTALL)


@dataclass
class Note:
    rel_path: str
    ntype: str  # project | area | inbox | dashboard | meeting | reference
    title: str
    aliases: list[str] = field(default_factory=list)
    keywords: list[str] = field(default_factory=list)
    signal: set[str] = field(default_factory=set)  # cached identity tokens

    def build_signal(self, first_heading: str = "") -> None:
        self.signal = set()
        for chunk in [self.title, first_heading, *self.aliases, *self.keywords]:
            self.signal |= _tokens(chunk)


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (frontmatter dict, first-heading text). Deliberately tiny YAML
    reader: scalars and simple `[a, b]` inline lists only, which is all the demo
    vault uses. No external YAML dependency."""
    fm: dict = {}
    m = _FM_BLOCK_RE.match(text)
    body = text[m.end():] if m else text
    if m:
        for line in m.group(1).splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if ":" not in line:
                continue
            key, _, raw = line.partition(":")
            key = key.strip()
            raw = raw.strip()
            if raw.startswith("[") and raw.endswith("]"):
                items = [x.strip().strip("'\"") for x in raw[1:-1].split(",")]
                fm[key] = [x for x in items if x]
            else:
                fm[key] = raw.strip("'\"")
    first_heading = ""
    for line in body.splitlines():
        if line.startswith("#"):
            first_heading = line.lstrip("#").strip()
            break
    return fm, first_heading


def load_notes(vault_root: Path | None = None) -> list[Note]:
    """Scan the vault and build a Note for every Markdown file that carries a
    routable `type` in its frontmatter (project / area / inbox / ...)."""
    root = (vault_root or VAULT_ROOT).resolve()
    notes: list[Note] = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            if not name.lower().endswith((".md", ".markdown")):
                continue
            fp = Path(dirpath) / name
            try:
                text = fp.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            fm, first_heading = _parse_frontmatter(text)
            ntype = str(fm.get("type", "")).strip()
            if ntype not in {"project", "area", "inbox", "dashboard", "meeting", "reference"}:
                continue
            aliases = fm.get("aliases", []) or []
            keywords = fm.get("keywords", []) or []
            if isinstance(aliases, str):
                aliases = [aliases]
            if isinstance(keywords, str):
                keywords = [keywords]
            note = Note(
                rel_path=str(fp.relative_to(root)).replace("\\", "/"),
                ntype=ntype,
                title=str(fm.get("title", fp.stem)),
                aliases=list(aliases),
                keywords=list(keywords),
            )
            note.build_signal(first_heading)
            notes.append(note)
    return notes


# ---------------------------------------------------------------------------
# Routing decision
# ---------------------------------------------------------------------------


@dataclass
class RouteDecision:
    target_path: str
    section: str  # heading the fact is appended under, e.g. "## Log"
    mode: str  # always "append" for captured facts
    reason: str
    score: int
    candidates: list[tuple[str, int]] = field(default_factory=list)


# Only these note types can *own* a durable fact. Dashboards, meeting notes and
# reference notes are read surfaces, not write targets for captured state.
_OWNING_TYPES = {"project", "area"}

# Below this many shared significant tokens, the match is too weak to trust; the
# fact is a raw capture and goes to the inbox for human triage.
_MIN_SCORE = 1


def _find_inbox(notes: list[Note]) -> str:
    for n in notes:
        if n.ntype == "inbox":
            return n.rel_path
    # Sensible default if the demo vault has no explicit inbox note.
    return "+inbox/inbox.md"


def route_fact(fact: str, notes: list[Note]) -> RouteDecision:
    """Decide which note owns `fact` and how to write it.

    Scoring: count of significant tokens shared between the fact and each
    owning note's identity signal. Highest score wins; ties break toward the
    more specific note type (project over area) since a dated development is
    more often project-shaped than area-shaped. No confident owner -> inbox.
    """
    fact_tokens = _tokens(fact)
    scored: list[tuple[int, int, Note]] = []
    for n in notes:
        if n.ntype not in _OWNING_TYPES:
            continue
        overlap = len(fact_tokens & n.signal)
        # Type preference: project = 1, area = 0. Used only to break ties.
        type_pref = 1 if n.ntype == "project" else 0
        scored.append((overlap, type_pref, n))

    scored.sort(key=lambda t: (t[0], t[1]), reverse=True)
    candidates = [(n.rel_path, ov) for ov, _, n in scored[:3]]

    if scored and scored[0][0] >= _MIN_SCORE:
        best = scored[0][2]
        return RouteDecision(
            target_path=best.rel_path,
            section="## Log",
            mode="append",
            reason=(
                f"owned by {best.ntype} note '{best.title}' "
                f"({scored[0][0]} matched tokens)"
            ),
            score=scored[0][0],
            candidates=candidates,
        )

    return RouteDecision(
        target_path=_find_inbox(notes),
        section="## Captures",
        mode="append",
        reason="no confident owner; raw capture routed to inbox for triage",
        score=0,
        candidates=candidates,
    )


# ---------------------------------------------------------------------------
# Append-vs-surface: the write mechanics
# ---------------------------------------------------------------------------


def render_log_entry(fact: str, *, stamp: str | None = None) -> str:
    """Format a single dated log bullet. One fact, one line, human-readable."""
    when = stamp or datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    clean = " ".join((fact or "").split())
    return f"- {when} · {clean}"


def append_under_section(body: str, section_header: str, entry_line: str) -> str:
    """Return `body` with `entry_line` appended at the end of `section_header`.

    If the section exists, the entry is inserted just before the next heading of
    the same-or-higher level (or at end of file). If it does not exist, the
    section is created at the end of the note. Existing content is never
    replaced. This is the append-not-overwrite guarantee in code.
    """
    lines = body.splitlines()
    header_level = len(section_header) - len(section_header.lstrip("#"))

    # Locate the section header line.
    header_idx = None
    for i, line in enumerate(lines):
        if line.strip() == section_header.strip():
            header_idx = i
            break

    if header_idx is None:
        # Create the section at end of note.
        out = list(lines)
        if out and out[-1].strip() != "":
            out.append("")
        out.append(section_header)
        out.append("")
        out.append(entry_line)
        return "\n".join(out) + "\n"

    # Find where this section ends: next heading of same-or-higher level.
    insert_at = len(lines)
    for j in range(header_idx + 1, len(lines)):
        stripped = lines[j].lstrip()
        if stripped.startswith("#"):
            # Heading level is the count of leading '#', measured after any
            # indentation is stripped (not the width of that indentation).
            level = len(stripped) - len(stripped.lstrip("#"))
            if level <= header_level:
                insert_at = j
                break

    # Walk back over trailing blank lines so the entry sits tight under content.
    while insert_at - 1 > header_idx and lines[insert_at - 1].strip() == "":
        insert_at -= 1

    out = lines[:insert_at] + [entry_line] + lines[insert_at:]
    return "\n".join(out) + "\n"


def capture_fact(fact: str, notes: list[Note] | None = None) -> dict:
    """End-to-end: route `fact`, append it under the owning note's log section,
    and return a receipt describing exactly what was touched.

    Reads current note state (via the sandbox-guarded read), computes the new
    body in memory, then overwrites the single target file with the appended
    body. The overwrite here is safe because the new body is `old + entry`: no
    prior content is dropped, which the test suite verifies explicitly.
    """
    notes = notes if notes is not None else load_notes()
    decision = route_fact(fact, notes)

    target = safe_resolve(decision.target_path)
    if target.is_file():
        current = read_file(decision.target_path)
    else:
        # Fresh note (typically the inbox on first capture): seed a title.
        stem = target.stem.replace("-", " ").strip().title()
        current = f"# {stem}\n"

    entry = render_log_entry(fact)
    new_body = append_under_section(current, decision.section, entry)

    # Full-file write of the append-extended body. Not "append" mode because we
    # are inserting under a specific section, not at raw EOF.
    receipt = write_file(decision.target_path, new_body, mode="overwrite")
    receipt.update(
        {
            "section": decision.section,
            "entry": entry,
            "reason": decision.reason,
            "score": decision.score,
        }
    )
    return receipt


__all__ = [
    "Note",
    "RouteDecision",
    "load_notes",
    "route_fact",
    "render_log_entry",
    "append_under_section",
    "capture_fact",
]
