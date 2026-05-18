#!/usr/bin/env bash
# Install this package as a Claude Code skill: symlink chennai into
# ~/.claude/skills/test-validity-evaluator so the SKILL.md is discoverable
# from any repo. Update with `git pull` in the chennai checkout — no
# reinstall needed.
set -euo pipefail

SKILL_NAME="test-validity-evaluator"
PKG_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET="${HOME}/.claude/skills/${SKILL_NAME}"

mkdir -p "${HOME}/.claude/skills"

if [[ -L "$TARGET" ]]; then
  current="$(readlink "$TARGET")"
  if [[ "$current" == "$PKG_DIR" ]]; then
    echo "✓ symlink already points at $PKG_DIR"
  else
    echo "→ updating existing symlink: $current → $PKG_DIR"
    ln -sfn "$PKG_DIR" "$TARGET"
  fi
elif [[ -e "$TARGET" ]]; then
  echo "✗ $TARGET exists and is not a symlink. Remove it manually first." >&2
  exit 1
else
  ln -s "$PKG_DIR" "$TARGET"
  echo "✓ created symlink $TARGET → $PKG_DIR"
fi

# Verify the SKILL.md is reachable from the install path.
if [[ ! -f "$TARGET/SKILL.md" ]]; then
  echo "✗ $TARGET/SKILL.md not found" >&2
  exit 1
fi
echo "✓ $TARGET/SKILL.md is present"

# PyYAML is recommended but optional — the minimal fallback parser handles
# the same adapter-manifest subset (with the bug fix in classify.py).
if python3 -c "import yaml" >/dev/null 2>&1; then
  echo "✓ PyYAML already installed"
else
  echo "→ installing PyYAML via pip3 --user"
  pip3 install --user pyyaml >/dev/null
  echo "✓ PyYAML installed"
fi

# Smoke check: classify on a tiny inline fixture so the user sees a green run.
python3 - <<'PY'
import json, subprocess, sys, tempfile, pathlib
import os
skill = pathlib.Path(os.path.expanduser("~/.claude/skills/test-validity-evaluator"))
adapter = skill / "contracts" / "adapters" / "python.pytest.yaml"
with tempfile.TemporaryDirectory() as tmp:
    p = pathlib.Path(tmp) / "test_smoke.py"
    p.write_text("def test_smoke():\n    assert 1 == 1\n")
    out = pathlib.Path(tmp) / "tier.json"
    cp = subprocess.run(
        [sys.executable, str(skill / "scripts" / "classify.py"),
         "--root", tmp, "--adapter", str(adapter), "--out", str(out)],
        capture_output=True, text=True, check=False,
    )
    if cp.returncode != 0:
        print("✗ smoke classify failed:", cp.stderr)
        sys.exit(1)
    data = json.loads(out.read_text())
    if not data:
        print("✗ smoke classify returned empty result")
        sys.exit(1)
    print(f"✓ smoke check: classified {len(data)} test ({data[0]['decision']})")
PY

echo
echo "Done. From any repo:"
echo "  python3 ~/.claude/skills/test-validity-evaluator/scripts/orchestrate.py \\"
echo "      --root . \\"
echo "      --adapter ~/.claude/skills/test-validity-evaluator/contracts/adapters/python.pytest.yaml \\"
echo "      --policy ./evaluation_policy.json \\"
echo "      --session-id \"evs_\$(date +%Y%m%d_%H%M%S)\""
