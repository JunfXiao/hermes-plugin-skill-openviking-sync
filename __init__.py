"""skill-openviking-sync — auto-sync agent-managed skills to OpenViking.

Listens to post_tool_call; whenever skill_manage performs a create/patch/write_file
operation successfully, the affected skill directory is re-uploaded to the user's
OpenViking skills registry via `ov add-skill` (which upserts by skill name).

Deletes are NOT propagated (no reliable CLI verb verified) — remove stale skills
manually in OpenViking if needed.
"""

import hashlib
import logging
import os
import subprocess
import threading
from pathlib import Path

logger = logging.getLogger(__name__)

_SYNC_ACTIONS = {"create", "patch", "write_file"}
_uploaded_hashes = {}  # skill_name -> sha256 of skill dir contents


def _hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes"))


def _find_skill_dir(name: str):
    skills_root = _hermes_home() / "skills"
    if not skills_root.is_dir():
        return None
    for skill_md in skills_root.rglob("SKILL.md"):
        if skill_md.parent.name == name:
            return skill_md.parent
        try:
            head = skill_md.read_text(encoding="utf-8", errors="ignore")[:2000]
        except OSError:
            continue
        if f"name: {name}" in head:
            return skill_md.parent
    return None


def _skill_hash(skill_dir: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(skill_dir)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _upload(name: str, skill_dir: Path, digest: str) -> None:
    try:
        proc = subprocess.run(
            ["ov", "add-skill", str(skill_dir), "--wait", "--timeout", "120"],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if proc.returncode == 0:
            _uploaded_hashes[name] = digest
            logger.info("skill-openviking-sync: uploaded '%s' to OpenViking", name)
        else:
            logger.warning(
                "skill-openviking-sync: upload failed for '%s': %s",
                name,
                (proc.stderr or proc.stdout or "").strip()[:500],
            )
    except Exception as e:  # never break the agent loop
        logger.warning("skill-openviking-sync: upload error for '%s': %s", name, e)


def _on_post_tool_call(tool_name=None, args=None, result=None, status=None, **kwargs):
    if tool_name != "skill_manage" or status == "error":
        return
    if isinstance(result, str) and '"success": false' in result:
        return
    ops = (args or {}).get("operations") or []
    names = {
        op.get("name")
        for op in ops
        if isinstance(op, dict) and op.get("action") in _SYNC_ACTIONS and op.get("name")
    }
    for name in names:
        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            logger.warning("skill-openviking-sync: skill dir not found for '%s'", name)
            continue
        digest = _skill_hash(skill_dir)
        if _uploaded_hashes.get(name) == digest:
            continue  # already synced this exact content
        threading.Thread(
            target=_upload, args=(name, skill_dir, digest), daemon=True
        ).start()


def register(ctx):
    ctx.register_hook("post_tool_call", _on_post_tool_call)
