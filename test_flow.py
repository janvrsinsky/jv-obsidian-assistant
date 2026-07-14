"""Self-test for the Celestia routing and filesystem core.

Runs against a throwaway copy of the demo vault so the checked-in fixtures are
never mutated. Verifies the three claims the README makes:

  1. A project-shaped fact is routed to and written into the correct project
     note, under its `## Log` section.
  2. The write appends: every prior log line is still present afterward.
  3. The sandbox path guard blocks directory-traversal and absolute-path escapes.

Pure standard library. Run:  python test_flow.py
Exit code 0 = all checks passed; non-zero = a check failed.
"""

from __future__ import annotations

import shutil
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent


class Check:
    def __init__(self) -> None:
        self.passed = 0
        self.failed = 0

    def ok(self, cond: bool, label: str) -> None:
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {label}")
        if cond:
            self.passed += 1
        else:
            self.failed += 1

    def summary(self) -> int:
        total = self.passed + self.failed
        print(f"\n{self.passed}/{total} checks passed.")
        return 0 if self.failed == 0 else 1


def main() -> int:
    check = Check()

    # Work on an isolated copy so the demo vault fixtures stay pristine.
    tmp = Path(tempfile.mkdtemp(prefix="celestia-test-"))
    sandbox_vault = tmp / "demo_vault"
    shutil.copytree(REPO_ROOT / "demo_vault", sandbox_vault)

    # Point the server module at the sandbox copy BEFORE importing routing, so
    # both modules share the same VAULT_ROOT.
    import os

    os.environ["CELESTIA_VAULT"] = str(sandbox_vault)

    import importlib
    import mcp_server

    importlib.reload(mcp_server)
    import routing

    importlib.reload(routing)

    from mcp_server import SandboxError, read_file, list_directory, search_files
    from routing import load_notes, route_fact, capture_fact

    notes = load_notes()

    print("1. Ownership routing")
    fact = "Supplier confirmed the two spindle motors ship Friday for the Line 3 retrofit."
    decision = route_fact(fact, notes)
    check.ok(
        decision.target_path == "projects/cnc-retrofit-line-3.md",
        f"project-shaped fact routes to the project card (got {decision.target_path})",
    )
    check.ok(decision.section == "## Log", "routed under the ## Log section")
    check.ok(decision.mode == "append", "routing mode is append, not overwrite")

    print("\n2. Append, not overwrite")
    target_rel = "projects/cnc-retrofit-line-3.md"
    before = read_file(target_rel)
    prior_log_lines = [
        ln for ln in before.splitlines() if ln.startswith("- 2026-")
    ]
    receipt = capture_fact(fact, notes)
    after = read_file(target_rel)

    check.ok(receipt["path"] == target_rel, "receipt names the file it touched")
    check.ok(fact.split(".")[0] in after, "new fact is present in the note")
    for prior in prior_log_lines:
        if not check_line_present(after, prior):
            check.ok(False, f"prior log line preserved: {prior[:48]}...")
            break
    else:
        check.ok(True, "every prior log line is still present (nothing overwritten)")
    # The note must have grown, never shrunk.
    check.ok(len(after) > len(before), "note grew (content appended, not replaced)")
    # Exactly one new log-shaped line was added.
    new_log_lines = [ln for ln in after.splitlines() if ln.startswith("- 2026-")]
    check.ok(
        len(new_log_lines) == len(prior_log_lines) + 1,
        f"exactly one log line added ({len(prior_log_lines)} -> {len(new_log_lines)})",
    )

    print("\n3. Unowned fact falls back to the inbox")
    orphan = "Random note about repainting the break room next quarter."
    orphan_decision = route_fact(orphan, notes)
    check.ok(
        orphan_decision.target_path.endswith("inbox.md"),
        f"fact with no owner routes to inbox (got {orphan_decision.target_path})",
    )

    print("\n4. Sandbox path guard")
    for bad in [
        "../secrets.md",
        "../../etc/passwd",
        "projects/../../escape.md",
        "/etc/hosts",
    ]:
        blocked = False
        try:
            read_file(bad)
        except SandboxError:
            blocked = True
        except FileNotFoundError:
            blocked = False  # reached the filesystem: guard did not stop it
        except Exception:
            blocked = False
        check.ok(blocked, f"traversal blocked: {bad!r}")

    # A legitimate in-vault read still works (guard is not over-broad).
    ok_read = False
    try:
        _ = read_file("dashboard.md")
        ok_read = True
    except Exception:
        ok_read = False
    check.ok(ok_read, "legitimate in-vault read still succeeds")

    print("\n5. Typed tools behave")
    listing = list_directory("projects")
    check.ok(
        any(e["name"] == "cnc-retrofit-line-3.md" for e in listing),
        "list_directory returns the project note",
    )
    hits = search_files("spindle")
    check.ok(len(hits) > 0, "search_files finds a known term across the vault")

    # Cleanup.
    shutil.rmtree(tmp, ignore_errors=True)

    return check.summary()


def check_line_present(haystack: str, line: str) -> bool:
    return line in haystack


if __name__ == "__main__":
    sys.exit(main())
