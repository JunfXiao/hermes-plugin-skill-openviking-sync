"""skill-openviking-sync — auto-sync agent-managed skills to OpenViking.

Listens to post_tool_call; whenever skill_manage performs a create/patch/write_file
operation successfully, the affected skill directory is re-uploaded to the user's
OpenViking skills registry via the OpenViking HTTP API (zip -> temp_upload ->
POST /api/v1/skills, which upserts by skill name). No `ov` CLI dependency.

Retries: transient failures (network errors, 5xx, 429) are retried with
exponential backoff (3 attempts). Definitive failures (4xx auth/validation)
fail fast and are NOT retried.

Temp file hygiene: on upload start, best-effort DELETE of the temp file is
attempted if a previous attempt for the same skill left one behind; after a
successful add_skill the temp file is deleted best-effort (server may GC it
anyway). Local zips are written to a tempfile and always removed in a finally
block.

Deletes are NOT propagated — remove stale skills manually in OpenViking if needed.

Path handling is runtime-aware: the skills root and ovcli config are resolved
for THIS process's view (contextvar home > HERMES_HOME / HERMES_REAL_HOME >
~/.hermes > /opt/data), because different frontends mount the same data dir at
different paths (webui container vs desktop). Skill matching is exact (dir or
frontmatter name), never substring.
"""

import hashlib
import io
import json
import logging
import os
import re
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from pathlib import Path

logger = logging.getLogger(__name__)

_SYNC_ACTIONS = {"create", "patch", "write_file"}
_uploaded_hashes = {}  # (skill_name, resolved skill dir) -> sha256 of contents
_last_temp_file_id = {}  # skill_name -> temp_file_id from the most recent attempt

_MAX_ATTEMPTS = 3
_BACKOFF_BASE = 2.0   # seconds; doubles each retry
_BACKOFF_INITIAL = 1.0

_FRONTMATTER_NAME_RE = re.compile(r"^name\s*:\s*([^#\n]+?)\s*$", re.M)


def _candidate_homes():
    """Hermes home candidates across runtimes/views (deduped, ordered)."""
    homes = []

    def _add(p):
        if not p:
            return
        try:
            p = Path(p).expanduser()
        except Exception:
            return
        if p not in homes:
            homes.append(p)

    try:
        # Same process as Hermes: respects the per-session profile override.
        from hermes_constants import get_hermes_home

        _add(get_hermes_home())
    except Exception:
        pass
    _add(os.environ.get("HERMES_HOME"))
    _add(os.environ.get("HERMES_REAL_HOME"))
    _add(Path.home() / ".hermes")
    _add("/opt/data")  # official docker layout fallback
    return homes


def _ov_config():
    """Return (base_url, api_key) from the ovcli config, resolved for THIS runtime's view.

    Different frontends mount the same data dir at different paths (webui
    container: /opt/data; desktop: ~/.hermes), so try all plausible locations.
    """
    candidates = []

    env_conf = os.environ.get("OVCLI_CONFIG_PATH")
    if env_conf:
        candidates.append(Path(env_conf))

    # Hermes config.yaml: openviking.ovcli_config_path (authoritative when set)
    try:
        from hermes_cli.config import load_config

        p = ((load_config() or {}).get("openviking") or {}).get("ovcli_config_path")
        if p:
            candidates.append(Path(p))
    except Exception:
        pass

    # .openviking/ovcli.conf(.default) next to each candidate home (walking up
    # a few levels covers profile dirs, profile roots and the data root).
    for home in _candidate_homes():
        base = home
        for _ in range(3):
            candidates.append(base / ".openviking" / "ovcli.conf.default")
            candidates.append(base / ".openviking" / "ovcli.conf")
            base = base.parent

    seen = set()
    for cand in candidates:
        try:
            cand = cand.expanduser()
        except Exception:
            continue
        seen_key = str(cand)
        if seen_key in seen:
            continue
        seen.add(seen_key)
        try:
            if not cand.is_file():
                continue
            cfg = json.loads(cand.read_text(encoding="utf-8"))
        except Exception:
            continue
        url = (cfg.get("url") or cfg.get("endpoint") or "").rstrip("/")
        api_key = cfg.get("api_key") or ""
        if url and api_key:
            logger.debug("skill-openviking-sync: using ovcli config %s", cand)
            return url, api_key
    return None


def _skills_roots():
    """Candidate skills roots for THIS runtime (deduped, ordered).

    Hermes' own resolution comes first (same function the skill tooling uses),
    then a sweep over home candidates and their profiles/* dirs so other mount
    views (container <-> desktop) are covered too.
    """
    roots = []

    def _add(p):
        try:
            p = Path(p)
        except Exception:
            return
        if p.is_dir() and p not in roots:
            roots.append(p)

    try:
        from agent.skill_utils import get_all_skills_dirs

        for d in get_all_skills_dirs():
            _add(d)
    except Exception:
        pass

    for home in _candidate_homes():
        for base in (home, home.parent):
            _add(base / "skills")
            profiles = base / "profiles"
            if profiles.is_dir():
                try:
                    for prof in sorted(profiles.iterdir()):
                        _add(prof / "skills")
                except OSError:
                    pass
    return roots


def _find_skill_dir(name):
    """Locate a skill dir by EXACT name for this process's view.

    Prefers the same resolver the skill_manage tool just used (same process),
    then sweeps candidate roots. Matching is exact (directory or frontmatter
    name) - never a substring, so 'hermes-agent' cannot match
    'hermes-agent-skill-authoring'.
    """
    try:
        from tools.skill_manager_tool import _find_skill as _tool_find_skill

        hit = _tool_find_skill(name)
        if isinstance(hit, dict):
            p = hit.get("path")
            if p and Path(p).is_dir():
                return Path(p)
    except Exception:
        pass

    roots = _skills_roots()
    # 1) directory-name match (also supports categorised "cat/name" forms)
    for root in roots:
        target = root / name
        if target.is_dir() and (target / "SKILL.md").is_file():
            return target
    for root in roots:
        for skill_md in root.rglob("SKILL.md"):
            if skill_md.parent.name == name:
                return skill_md.parent
    # 2) exact frontmatter-name match
    for root in roots:
        for skill_md in root.rglob("SKILL.md"):
            try:
                head = skill_md.read_text(encoding="utf-8", errors="ignore")[:2000]
            except OSError:
                continue
            m = _FRONTMATTER_NAME_RE.search(head)
            if m and m.group(1).strip().strip("\"'") == name:
                return skill_md.parent
    logger.warning(
        "skill-openviking-sync: skill dir not found for '%s' (roots scanned: %s)",
        name,
        ", ".join(str(r) for r in roots) or "<none>",
    )
    return None


def _skill_hash(skill_dir: Path) -> str:
    h = hashlib.sha256()
    for f in sorted(skill_dir.rglob("*")):
        if f.is_file():
            h.update(str(f.relative_to(skill_dir)).encode())
            h.update(f.read_bytes())
    return h.hexdigest()


def _zip_skill_dir(skill_dir: Path, name: str, dest: Path) -> None:
    """Write the zipped skill dir to dest (caller ensures cleanup)."""
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as zf:
        for f in sorted(skill_dir.rglob("*")):
            if f.is_file():
                zf.write(f, arcname=str(Path(name) / f.relative_to(skill_dir)))


def _is_retryable(exc: Exception) -> bool:
    """Transient = network-level errors, timeouts, 5xx, 429. 4xx = definitive."""
    if isinstance(exc, urllib.error.HTTPError):
        return exc.code >= 500 or exc.code == 429
    # URLError / timeouts / connection resets / JSON parse of bad response
    return not isinstance(exc, urllib.error.HTTPError)


def _http_multipart(url: str, key: str, filename: str, payload: bytes) -> dict:
    boundary = "----ovsync-boundary-8f3a"
    body = (
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
        f"Content-Type: application/octet-stream\r\n\r\n"
    ).encode() + payload + f"\r\n--{boundary}--\r\n".encode()
    req = urllib.request.Request(
        url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": f"multipart/form-data; boundary={boundary}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as r:
        data = r.read()
        return json.loads(data.decode("utf-8"))


def _http_json(url: str, key: str, payload: dict, timeout: float) -> dict:
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        return json.loads(data.decode("utf-8"))


def _http_delete(url: str, key: str, timeout: float = 15) -> None:
    req = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {key}"}, method="DELETE"
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        r.read()


def _cleanup_temp_file(base: str, key: str, temp_file_id: str) -> bool:
    """Best-effort DELETE of a leftover temp upload. Never raises."""
    if not temp_file_id:
        return False
    try:
        _http_delete(f"{base}/api/v1/resources/temp_uploads/{temp_file_id}", key)
        return True
    except Exception:
        # endpoint may not exist or file already GC'd — that's fine
        return False


def _attempt_upload(base: str, key: str, name: str, skill_dir: Path, digest: str) -> dict:
    """One upload attempt: temp_upload -> add_skill -> cleanup temp. Raises on failure."""
    # Zip to a temp file on disk; always removed in finally.
    fd, tmp_path = tempfile.mkstemp(suffix=f"-{name}.zip", prefix="ovsync-")
    tmp_zip = Path(tmp_path)
    try:
        with os.fdopen(fd, "wb") as fh:
            pass  # just reserve; zipfile writes its own
        _zip_skill_dir(skill_dir, name, tmp_zip)
        payload = tmp_zip.read_bytes()
    finally:
        try:
            tmp_zip.unlink(missing_ok=True)
        except OSError:
            pass

    # Pre-flight: best-effort cleanup of a leftover temp file from a previous
    # failed run for this skill (started upload but add_skill never completed).
    stale = _last_temp_file_id.pop(name, None)
    if stale:
        if _cleanup_temp_file(base, key, stale):
            logger.info("skill-openviking-sync: cleaned stale temp file %s", stale)

    tmp_resp = _http_multipart(
        f"{base}/api/v1/resources/temp_upload", key, f"{name}.zip", payload
    )
    temp_file_id = (tmp_resp.get("result") or {}).get("temp_file_id")
    if not temp_file_id:
        raise RuntimeError(f"temp_upload returned no temp_file_id: {tmp_resp}")

    _last_temp_file_id[name] = temp_file_id  # remember for post-failure cleanup
    try:
        result = _http_json(
            f"{base}/api/v1/skills",
            key,
            {"temp_file_id": temp_file_id, "wait": True, "timeout": 120},
            timeout=150,
        )
    except Exception:
        # add_skill failed — try to clean the temp file we just created so it
        # doesn't accumulate; remembered in _last_temp_file_id regardless.
        raise

    status = (result.get("result") or {}).get("status")
    if result.get("status") != "ok" or status != "success":
        raise RuntimeError(f"add_skill did not succeed: {str(result)[:300]}")

    _uploaded_hashes[(name, str(skill_dir))] = digest
    _last_temp_file_id.pop(name, None)
    uri = (result.get("result") or {}).get("uri", "?")
    logger.info("skill-openviking-sync: uploaded '%s' to %s", name, uri)
    return result


def _upload(name: str, skill_dir: Path, digest: str) -> None:
    try:
        cfg = _ov_config()
        if not cfg:
            raise RuntimeError("ovcli config not found (no OpenViking url/api_key)")
        base, key = cfg

        last_exc: Exception | None = None
        attempts_used = 0
        for attempt in range(1, _MAX_ATTEMPTS + 1):
            attempts_used = attempt
            try:
                _attempt_upload(base, key, name, skill_dir, digest)
                return  # success
            except Exception as exc:
                last_exc = exc
                if not _is_retryable(exc):
                    logger.warning(
                        "skill-openviking-sync: non-retryable error for '%s': %s",
                        name, exc,
                    )
                    break
                if attempt < _MAX_ATTEMPTS:
                    delay = _BACKOFF_INITIAL * (_BACKOFF_BASE ** (attempt - 1))
                    logger.info(
                        "skill-openviking-sync: retryable error for '%s' (attempt %d/%d), "
                        "retrying in %.1fs: %s",
                        name, attempt, _MAX_ATTEMPTS, delay, exc,
                    )
                    time.sleep(delay)
        logger.warning(
            "skill-openviking-sync: upload failed for '%s' after %d attempts: %s",
            name, attempts_used, last_exc,
        )
    except Exception as e:  # never break the agent loop
        logger.warning("skill-openviking-sync: upload error for '%s': %s", name, e)


def _on_post_tool_call(tool_name=None, args=None, result=None, status=None, **kwargs):
    if tool_name != "skill_manage" or status == "error":
        return
    if isinstance(result, str) and '"success": false' in result:
        return
    if isinstance(result, dict) and result.get("success") is False:
        return
    ops = (args or {}).get("operations") or []
    if not ops and isinstance(args, dict) and args.get("action") in _SYNC_ACTIONS and args.get("name"):
        ops = [args]  # legacy flat shape
    names = {
        op.get("name")
        for op in ops
        if isinstance(op, dict) and op.get("action") in _SYNC_ACTIONS and op.get("name")
    }
    for name in sorted(names):
        skill_dir = _find_skill_dir(name)
        if not skill_dir:
            logger.debug("skill-openviking-sync: skill dir not found for '%s'", name)
            continue
        digest = _skill_hash(skill_dir)
        if _uploaded_hashes.get((name, str(skill_dir))) == digest:
            continue  # already synced this exact content
        threading.Thread(
            target=_upload, args=(name, skill_dir, digest), daemon=True
        ).start()


def register(ctx):
    ctx.register_hook("post_tool_call", _on_post_tool_call)
