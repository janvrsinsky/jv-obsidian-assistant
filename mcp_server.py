"""Celestia MCP filesystem server.

A narrow, typed Model Context Protocol filesystem server over a sandboxed
Obsidian Markdown vault. It exposes five operations only:

    read_file, write_file, list_directory, search_files, directory_tree

Every path is resolved through a single sandbox guard (`safe_resolve`) so the
agent can never read or write outside the vault root, and symlink or `..`
traversal is rejected before any filesystem call happens.

This module is import-safe without the MCP framework installed: the tool
registration is skipped when `mcp` is absent, so `test_flow.py` and `routing.py`
can import the pure functions with the Python standard library alone.

Run as an MCP server (requires the `mcp` package, see requirements.txt):

    CELESTIA_VAULT=./demo_vault python mcp_server.py
"""

from __future__ import annotations

import os
from pathlib import Path

# ---------------------------------------------------------------------------
# Sandbox configuration
# ---------------------------------------------------------------------------

# Default vault is the bundled demo vault sitting next to this file. Override
# with CELESTIA_VAULT to point at a real vault in production.
_DEFAULT_VAULT = Path(__file__).resolve().parent / "demo_vault"
VAULT_ROOT = Path(os.environ.get("CELESTIA_VAULT", str(_DEFAULT_VAULT))).resolve()

# Read/write is restricted to Markdown and a couple of plain-text companions.
# Anything else is refused so the agent cannot touch binaries or dot-config.
_ALLOWED_SUFFIXES = {".md", ".markdown", ".txt"}

# Cap the size of a single read/write so a runaway request cannot exhaust memory.
_MAX_BYTES = 512 * 1024


class SandboxError(ValueError):
    """Raised when a requested path escapes the vault or breaks a rule."""


def safe_resolve(rel_path: str, *, vault_root: Path | None = None) -> Path:
    """Resolve `rel_path` inside the vault and reject any escape.

    The guard is the single choke point every tool goes through. It:
      1. rejects absolute paths outright,
      2. resolves symlinks and `..` segments to a real path,
      3. asserts the real path stays under the (real) vault root.

    Returns the resolved absolute Path. Raises SandboxError on any violation.
    """
    root = (vault_root or VAULT_ROOT).resolve()

    if rel_path is None:
        raise SandboxError("path is required")

    # Normalise separators; treat a leading slash as vault-relative, not system
    # absolute, so "/projects/x.md" means "<vault>/projects/x.md".
    cleaned = str(rel_path).replace("\\", "/").lstrip("/")

    candidate = (root / cleaned)

    # Resolve fully. strict=False so we can resolve not-yet-existing write
    # targets, but their *parent* chain is still resolved through symlinks.
    resolved = candidate.resolve()

    # Containment check. Python 3.9+: is_relative_to is the clean form; fall
    # back to a prefix check on the resolved string for older interpreters.
    try:
        inside = resolved == root or resolved.is_relative_to(root)
    except AttributeError:  # pragma: no cover - very old Python
        inside = os.path.commonpath([str(resolved), str(root)]) == str(root)

    if not inside:
        raise SandboxError(f"path escapes vault sandbox: {rel_path!r}")

    return resolved


def _check_suffix(path: Path) -> None:
    if path.suffix.lower() not in _ALLOWED_SUFFIXES:
        raise SandboxError(
            f"refusing non-text file {path.name!r}; "
            f"allowed suffixes: {sorted(_ALLOWED_SUFFIXES)}"
        )


# ---------------------------------------------------------------------------
# Core operations (framework-agnostic, unit-testable)
# ---------------------------------------------------------------------------


def read_file(path: str) -> str:
    """Return the full text of a Markdown note inside the vault."""
    target = safe_resolve(path)
    _check_suffix(target)
    if not target.is_file():
        raise SandboxError(f"not a file: {path!r}")
    data = target.read_bytes()
    if len(data) > _MAX_BYTES:
        raise SandboxError(f"file too large to read ({len(data)} bytes)")
    return data.decode("utf-8")


def write_file(path: str, content: str, mode: str = "append") -> dict:
    """Write text to a note. Defaults to append so history is preserved.

    mode:
      "append"  add `content` to the end of the note (creating it if absent).
                This is the discipline the assistant uses for logged facts:
                writes accrete, they do not clobber.
      "create"  write only if the file does not yet exist (fresh notes).
      "overwrite" full replace. Deliberately named so a caller must opt in.
                The routing layer uses it only with an append-extended body
                (prior text + the new entry), so no prior content is dropped;
                it is never a blind clobber.

    Returns a small receipt so the agent can report exactly what it touched.
    """
    target = safe_resolve(path)
    _check_suffix(target)

    if content is None:
        content = ""
    encoded = content.encode("utf-8")
    if len(encoded) > _MAX_BYTES:
        raise SandboxError(f"content too large to write ({len(encoded)} bytes)")

    existed = target.is_file()

    if mode == "create" and existed:
        raise SandboxError(f"refusing to overwrite existing note: {path!r}")
    if mode == "overwrite" and not existed:
        # Overwrite of a missing file is just a create; allow it.
        mode = "create"
    if mode not in ("append", "create", "overwrite"):
        raise SandboxError(f"unknown write mode: {mode!r}")

    target.parent.mkdir(parents=True, exist_ok=True)

    if mode == "append" and existed:
        prior = target.read_text("utf-8")
        joiner = "" if prior.endswith("\n") or prior == "" else "\n"
        target.write_text(prior + joiner + content, "utf-8")
        action = "appended"
    else:
        target.write_text(content, "utf-8")
        action = "created" if not existed else "overwrote"

    return {
        "path": str(target.relative_to(VAULT_ROOT)),
        "action": action,
        "bytes": len(content.encode("utf-8")),
    }


def list_directory(path: str = ".") -> list[dict]:
    """List immediate children of a directory inside the vault."""
    target = safe_resolve(path)
    if not target.is_dir():
        raise SandboxError(f"not a directory: {path!r}")
    out: list[dict] = []
    for child in sorted(target.iterdir(), key=lambda p: (p.is_file(), p.name)):
        out.append(
            {
                "name": child.name,
                "type": "dir" if child.is_dir() else "file",
                "path": str(child.relative_to(VAULT_ROOT)),
            }
        )
    return out


def search_files(query: str, path: str = ".", *, max_results: int = 50) -> list[dict]:
    """Case-insensitive substring search across note bodies.

    This is retrieval over a live Markdown filesystem, not a vector index. It
    walks the vault, reads each text note, and returns matching lines with the
    file and line number so the agent can open and read the note next.
    """
    if not query:
        raise SandboxError("query is required")
    root = safe_resolve(path)
    if not root.is_dir():
        raise SandboxError(f"not a directory: {path!r}")

    needle = query.lower()
    hits: list[dict] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Skip Obsidian internals and any dotdir; keep the walk cheap.
        dirnames[:] = [d for d in dirnames if not d.startswith(".")]
        for name in sorted(filenames):
            fp = Path(dirpath) / name
            if fp.suffix.lower() not in _ALLOWED_SUFFIXES:
                continue
            try:
                text = fp.read_text("utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            for lineno, line in enumerate(text.splitlines(), start=1):
                if needle in line.lower():
                    hits.append(
                        {
                            "path": str(fp.relative_to(VAULT_ROOT)),
                            "line": lineno,
                            "text": line.strip()[:200],
                        }
                    )
                    if len(hits) >= max_results:
                        return hits
    return hits


def directory_tree(path: str = ".", *, max_depth: int = 6) -> dict:
    """Return a nested tree of the vault (or a subtree) for orientation."""
    root = safe_resolve(path)
    if not root.is_dir():
        raise SandboxError(f"not a directory: {path!r}")

    def build(node: Path, depth: int) -> dict:
        entry: dict = {"name": node.name, "type": "dir", "children": []}
        if depth >= max_depth:
            return entry
        for child in sorted(node.iterdir(), key=lambda p: (p.is_file(), p.name)):
            if child.name.startswith("."):
                continue
            if child.is_dir():
                entry["children"].append(build(child, depth + 1))
            else:
                entry["children"].append({"name": child.name, "type": "file"})
        return entry

    return build(root, 0)


# ---------------------------------------------------------------------------
# MCP tool registration (only when the framework is present)
# ---------------------------------------------------------------------------

try:
    from mcp.server.fastmcp import FastMCP  # type: ignore

    _HAS_FASTMCP = True
except Exception:  # ImportError in practice; broad to stay import-safe
    _HAS_FASTMCP = False


def build_server():  # pragma: no cover - exercised only with mcp installed
    """Create and return the FastMCP server with the five tools wired up."""
    if not _HAS_FASTMCP:
        raise RuntimeError(
            "the 'mcp' package is not installed; run `pip install -r requirements.txt`"
        )

    mcp = FastMCP("celestia")

    @mcp.tool()
    def read_file_tool(path: str) -> str:
        """Read a Markdown note from the vault."""
        return read_file(path)

    @mcp.tool()
    def write_file_tool(path: str, content: str, mode: str = "append") -> dict:
        """Write to a note. Appends by default (history preserved)."""
        return write_file(path, content, mode)

    @mcp.tool()
    def list_directory_tool(path: str = ".") -> list:
        """List the immediate children of a vault directory."""
        return list_directory(path)

    @mcp.tool()
    def search_files_tool(query: str, path: str = ".") -> list:
        """Substring search across note bodies; returns file + line hits."""
        return search_files(query, path)

    @mcp.tool()
    def directory_tree_tool(path: str = ".") -> dict:
        """Return a nested tree of the vault for orientation."""
        return directory_tree(path)

    return mcp


if __name__ == "__main__":  # pragma: no cover
    build_server().run()
