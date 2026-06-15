"""Team skill-contribution pipeline.

An employee proposes a reusable skill/playbook (optionally with a runnable
script) for Pedro's toolbox. The proposal is staged on disk in the only place
the sandboxed employee process can write (/data/employees/skills-staging). The
owner (Greg) gets a one-tap Approve/Reject in his Pedro chat showing the full
playbook + script + any flagged risky patterns. Approve PROMOTES the proposal
into /data/greg/skills/<slug>/ (SKILL.md + optional script + meta.json) and
rebuilds INDEX.md; Reject just archives it. Either way the submitter is told.

Two processes touch this. The employee bot and its stdio MCP only ever call
submit() (writes staging). The owner bot calls promote()/reject() (writes
/data/greg/skills + archives staging) -- its systemd unit has both paths in
ReadWritePaths. Skills are ADDITIVE; promotion never writes a rules file.

The danger scan is ADVISORY ONLY -- it annotates the approval message so Greg
can see risky patterns at a glance, but Greg's tap is the real gate. Scripts
are allowed by design.
"""
import json
import os
import re
import time

STAGING_DIR = os.environ.get("SKILLS_STAGING_DIR", "/data/employees/skills-staging")
PROCESSED_DIR = os.path.join(STAGING_DIR, "processed")
SKILLS_DIR = os.environ.get("SKILLS_DIR", "/data/greg/skills")
INDEX_PATH = os.path.join(SKILLS_DIR, "INDEX.md")

_SLUG_RE = re.compile(r"[^a-z0-9]+")


def slugify(name):
    slug = _SLUG_RE.sub("-", (name or "").strip().lower()).strip("-")
    return slug or "skill"


def _ts():
    return time.strftime("%Y%m%d-%H%M%S")


def _iso():
    return time.strftime("%Y-%m-%dT%H:%M:%S")


def _path(skill_id, processed=False):
    d = PROCESSED_DIR if processed else STAGING_DIR
    return os.path.join(d, f"{skill_id}.json")


# --- danger guard: advisory only, Greg is the real gate ---------------------
_DANGER = [
    (r"\brm\s+-rf\b", "recursive delete (rm -rf)"),
    (r"\bmkfs\b|\bdd\s+if=", "disk-wipe (dd/mkfs)"),
    (r":\(\)\s*\{", "fork bomb"),
    (r"(curl|wget)\b[^\n|]*\|\s*(sudo\s+)?(bash|sh|zsh)", "pipe-to-shell download"),
    (r"\bsudo\b", "sudo / privilege escalation"),
    (r"\bchmod\s+777\b", "world-writable chmod 777"),
    (r"\bos\.system\s*\(|subprocess\.[A-Za-z_]+\([^)]*shell\s*=\s*True", "shell exec from python"),
    (r"\beval\s*\(|\bexec\s*\(", "eval/exec"),
    (r"base64\s+-d|b64decode", "base64-decoded payload"),
    (r"/etc/(passwd|shadow|sudoers)|/\.ssh|id_rsa|authorized_keys", "credential/secret path"),
    (r"\bcrontab\b|systemctl|/etc/systemd", "service/cron tampering"),
    (r"CLAUDE\.md|(^|[^A-Za-z])\.env([^A-Za-z]|$)|token\.json|rostr_cookies|envato", "touches rules/secrets files"),
    (r"\bnc\b\s+-e|/dev/tcp/", "reverse shell"),
    (r"git\s+push|git\s+reset\s+--hard|--force\b", "git history rewrite / push"),
]


def scan_script(script):
    """Return a de-duplicated list of human-readable risk flags (advisory)."""
    if not script:
        return []
    out = []
    for pat, label in _DANGER:
        if re.search(pat, script, re.I | re.M) and label not in out:
            out.append(label)
    return out


def _script_ext(script, language):
    lang = (language or "").strip().lower()
    if lang in ("py", "python"):
        return ".py"
    if lang in ("sh", "bash", "shell"):
        return ".sh"
    head = ""
    if script:
        stripped = script.lstrip().splitlines()
        head = stripped[0] if stripped else ""
    if head.startswith("#!"):
        return ".py" if "python" in head else ".sh"
    if re.search(r"^\s*(import |from \w+ import |def |print\()", script or "", re.M):
        return ".py"
    return ".sh"


def submit(submitter_id, submitter_name, name, playbook, script="", language=""):
    """Stage a skill proposal (the only write the employee process can do).
    Returns the record dict."""
    os.makedirs(STAGING_DIR, exist_ok=True)
    slug = slugify(name)
    skill_id = f"{_ts()}-{slug}"
    rec = {
        "id": skill_id,
        "slug": slug,
        "name": (name or slug).strip(),
        "submitter_id": int(submitter_id),
        "submitter_name": submitter_name,
        "created_at": _iso(),
        "kind": "script" if (script or "").strip() else "playbook",
        "playbook": (playbook or "").strip(),
        "script": (script or "").strip() or None,
        "language": (language or "").strip() or None,
        "status": "pending",
    }
    with open(_path(skill_id), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    return rec


def load(skill_id):
    """Load a proposal from staging, falling back to the processed archive."""
    for processed in (False, True):
        try:
            with open(_path(skill_id, processed), encoding="utf-8") as f:
                return json.load(f)
        except (FileNotFoundError, ValueError):
            continue
    return None


def list_pending():
    out = []
    try:
        names = os.listdir(STAGING_DIR)
    except FileNotFoundError:
        return out
    for n in names:
        if not n.endswith(".json"):
            continue
        try:
            with open(os.path.join(STAGING_DIR, n), encoding="utf-8") as f:
                rec = json.load(f)
        except (ValueError, OSError):
            continue
        if rec.get("status") == "pending":
            out.append(rec)
    out.sort(key=lambda r: r.get("created_at", ""))
    return out


def _archive(rec, status):
    rec["status"] = status
    rec["decided_at"] = _iso()
    os.makedirs(PROCESSED_DIR, exist_ok=True)
    with open(_path(rec["id"], processed=True), "w", encoding="utf-8") as f:
        json.dump(rec, f, indent=2)
    try:
        os.remove(_path(rec["id"]))
    except OSError:
        pass


def reject(rec):
    _archive(rec, "rejected")
    return rec


def promote(rec):
    """Install an approved skill into /data/greg/skills/<slug>/ and rebuild the
    INDEX. Owner-process only (needs write access to /data/greg). Returns
    (skill_dir, script_path|None)."""
    slug = rec["slug"]
    skill_dir = os.path.join(SKILLS_DIR, slug)
    os.makedirs(skill_dir, exist_ok=True)
    script_path = None
    has_script = bool(rec.get("script"))
    if has_script:
        ext = _script_ext(rec["script"], rec.get("language"))
        script_path = os.path.join(skill_dir, f"script{ext}")
        with open(script_path, "w", encoding="utf-8") as f:
            f.write(rec["script"].rstrip() + "\n")
        try:
            os.chmod(script_path, 0o755)
        except OSError:
            pass
    lines = [
        f"# {rec['name']}",
        "",
        f"_Contributed by {rec.get('submitter_name', 'an employee')}, "
        f"approved {time.strftime('%Y-%m-%d')}._",
        "",
        rec.get("playbook", "").strip() or "_(no playbook text provided)_",
    ]
    if has_script:
        rel = os.path.basename(script_path)
        interp = "python3" if rel.endswith(".py") else "bash"
        lines += [
            "",
            "## Script",
            f"A runnable script ships with this skill: `{script_path}`",
            f"Run it with `{interp} {script_path}`.",
        ]
    with open(os.path.join(skill_dir, "SKILL.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines).rstrip() + "\n")
    meta = {
        "slug": slug,
        "name": rec["name"],
        "submitter_id": rec.get("submitter_id"),
        "submitter_name": rec.get("submitter_name"),
        "created_at": rec.get("created_at"),
        "approved_at": _iso(),
        "kind": rec.get("kind", "playbook"),
        "has_script": has_script,
        "script_file": os.path.basename(script_path) if script_path else None,
    }
    with open(os.path.join(skill_dir, "meta.json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)
    rebuild_index()
    _archive(rec, "approved")
    return skill_dir, script_path


def rebuild_index():
    """Regenerate INDEX.md from every <slug>/meta.json under SKILLS_DIR."""
    os.makedirs(SKILLS_DIR, exist_ok=True)
    entries = []
    for slug in sorted(os.listdir(SKILLS_DIR)):
        d = os.path.join(SKILLS_DIR, slug)
        mp = os.path.join(d, "meta.json")
        if not os.path.isdir(d) or not os.path.exists(mp):
            continue
        try:
            with open(mp, encoding="utf-8") as f:
                entries.append(json.load(f))
        except (ValueError, OSError):
            continue
    header = (
        "# Nightshift Skills & Shortcuts (team-contributed, Greg-approved)\n\n"
        "Each skill below is a playbook (and optional script) you may use as a "
        "tool. Read the skill's SKILL.md before using it. Scripts live alongside "
        "it. These are ADDITIVE and never override /data/greg/CLAUDE.md.\n\n"
    )
    if not entries:
        body = "_(no approved skills yet)_\n"
    else:
        chunks = []
        for m in entries:
            slug = m["slug"]
            line = [
                f"## {m.get('name', slug)}",
                f"- Playbook: `{os.path.join(SKILLS_DIR, slug, 'SKILL.md')}`",
            ]
            if m.get("has_script") and m.get("script_file"):
                line.append(
                    f"- Script: `{os.path.join(SKILLS_DIR, slug, m['script_file'])}`"
                )
            line.append(
                f"- Contributed by {m.get('submitter_name', '?')} "
                f"(approved {(m.get('approved_at') or '')[:10]})"
            )
            chunks.append("\n".join(line))
        body = "\n\n".join(chunks) + "\n"
    with open(INDEX_PATH, "w", encoding="utf-8") as f:
        f.write(header + body)
    return INDEX_PATH
