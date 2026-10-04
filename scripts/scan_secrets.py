#!/usr/bin/env python3
"""Check reachable Git history and publishable working files without printing secrets."""
import argparse
import base64
from collections import Counter, defaultdict
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import sys
from urllib.parse import parse_qsl, unquote, urlsplit


MAX_BYTES = 16 * 1024 * 1024
TOKEN_PATTERNS = (
    ("private_key", re.compile(r"-----BEGIN (?:RSA |EC |DSA |OPENSSH |ENCRYPTED )?PRIVATE KEY-----")),
    ("openai_token", re.compile(r"\bsk-(?:proj-|svcacct-)?[A-Za-z0-9_-]{20,512}\b")),
    ("github_token", re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,255}|github_pat_[A-Za-z0-9_]{22,255})\b")),
    ("webhook_secret", re.compile(r"\bwhsec_[A-Za-z0-9+/=_-]{20,512}")),
)
LITERAL = re.compile(
    r"\b(?P<name>(?:[A-Za-z][A-Za-z0-9_]*_)?(?:token|secret|api_key|runtime_key|aes_key|aeskey))"
    r"[\"']?\s*[:=]\s*(?P<quote>[\"']?)(?P<value>[A-Za-z0-9_+/=.:%$<>@{}-]{8,512})",
    re.IGNORECASE,
)
URL = re.compile(r"https?://[^\s\"'<>\[\]{}]+", re.IGNORECASE)
SENSITIVE_QUERY = re.compile(
    r"(?:^|[_-])(?:(?:access|api|bot|callback|signing)[_-]?)?(?:token|secret|key|password|signature|auth|credential)$",
    re.IGNORECASE,
)
FIXTURE_MARKER = re.compile(
    r"(?:^|[-_\s])(?:fixture|synthetic|example|placeholder|not[-_ ]?(?:a[-_ ]?)?real|not[-_ ]?a[-_ ]?token)(?:$|[-_\s])",
    re.IGNORECASE,
)
PLACEHOLDERS = {"token", "secret", "password", "pass", "redacted", "changeme", "private_token", "private_context"}


def placeholder(value):
    """Only explicit dummy values are exempt, regardless of the containing directory."""
    return (value.lower() in PLACEHOLDERS or bool(FIXTURE_MARKER.search(value))
            or value.startswith(("${", "<", "file:")))


def entropy(value):
    counts = Counter(value)
    return -sum((count / len(value)) * math.log2(count / len(value)) for count in counts.values())


def encoded_fixture(value):
    try:
        decoded = base64.b64decode(value, validate=True)
    except (ValueError, base64.binascii.Error):
        return False
    text = decoded.decode("ascii", errors="replace")
    return placeholder(text) or (len(decoded) >= 16 and len(set(decoded)) == 1)


def secret_matches(text):
    found = set()
    for category, pattern in TOKEN_PATTERNS:
        for match in pattern.finditer(text):
            value = match.group()
            if category != "private_key" and placeholder(value):
                continue
            if category == "webhook_secret" and encoded_fixture(value[6:]):
                continue
            found.add((category, text.count("\n", 0, match.start()) + 1))
    for match in LITERAL.finditer(text):
        value = match.group("value")
        if placeholder(value) or len(value) < 20 or entropy(value) < 3.0:
            continue
        if value.startswith("whsec_") and encoded_fixture(value[6:]):
            continue
        found.add(("credential_literal", text.count("\n", 0, match.start()) + 1))
    for match in URL.finditer(text):
        try:
            parts = urlsplit(match.group().rstrip(").,;"))
            credentials = parts.password is not None and not placeholder(unquote(parts.password))
            query = any(SENSITIVE_QUERY.search(name) and value and not placeholder(value)
                        for name, value in parse_qsl(parts.query) + parse_qsl(parts.fragment))
            callback_context = (re.search(r"(?:callback|webhook|hooks)", parts.hostname or "", re.IGNORECASE)
                                or re.search(r"(?:callback_url|webhook_url|delivery)[\s\S]{0,120}$",
                                             text[max(0, match.start() - 120):match.start()], re.IGNORECASE))
            route = bool(callback_context and any(
                len(value) >= 20 and not placeholder(value) and entropy(value) >= 3.0
                for value in (unquote(segment) for segment in parts.path.split("/"))))
        except ValueError:
            continue
        if credentials or query or route:
            found.add(("secret_url", text.count("\n", 0, match.start()) + 1))
    return sorted(found)


def runtime_path(path):
    parts = PurePosixPath(path).parts
    name = parts[-1].lower() if parts else ""
    if any(part.lower() in {"runtime", "state", "secrets", "credentials"} for part in parts[:-1]):
        return "runtime_file"
    if re.search(r"\.(?:sqlite(?:3)?|db)(?:[.-].*)?$", name):
        return "runtime_database"
    if name.endswith((".key", ".pem", ".p12", ".pfx")):
        return "private_file"
    if name == ".env" or (name.startswith(".env.") and name not in {".env.example", ".env.sample", ".env.template"}):
        return "auth_config"
    if (re.fullmatch(r"(?:private[^/]*|auth|binding|credentials|runtime)\.(?:json|ya?ml|toml)", name)
            or path in {"config/bridge.json", "config/tunnel.yaml"}):
        return "auth_config"
    if name.endswith((".zip", ".tar", ".tgz", ".gz", ".bz2", ".xz", ".7z", ".rar", ".whl")):
        return "archive_file"
    return None


def display_path(path):
    # A filename can itself contain a token. Never echo that token.
    for _, pattern in TOKEN_PATTERNS:
        path = pattern.sub("[redacted]", path)
    return "".join(character if character.isprintable() else "?" for character in path)


def git(repo, *arguments, allowed=(0,)):
    process = subprocess.run(["git", "--no-replace-objects", "-C", str(repo), "-c", "core.fsmonitor=false", *arguments],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30, check=False)
    if process.returncode not in allowed:
        raise ValueError("git_failed")
    return process.stdout, process.returncode


def scan(repo):
    root_raw, _ = git(repo, "rev-parse", "--show-toplevel")
    root = Path(os.fsdecode(root_raw.rstrip(b"\n")))
    _, head_status = git(root, "rev-parse", "--verify", "--quiet", "HEAD", allowed=(0, 1))
    revisions = ("--all", "HEAD") if head_status == 0 else ("--all",)
    raw, _ = git(root, "rev-list", "--objects", "--no-object-names", *revisions)
    object_ids = sorted(set(raw.decode("ascii").splitlines()))
    raw, _ = git(root, "rev-list", *revisions)
    commits = raw.decode("ascii").splitlines()
    paths = defaultdict(set)
    findings = []
    seen = set()

    def report(scope, category, path=None, line=None, object_id=None):
        result = {"scope": scope, "category": category}
        if path is not None:
            result["file"] = display_path(path)
        if line is not None:
            result["line"] = line
        if object_id is not None:
            result["object"] = object_id
        key = tuple(sorted(result.items()))
        if key not in seen:
            seen.add(key)
            findings.append(result)

    raw, _ = git(root, "rev-parse", "--is-shallow-repository")
    history_complete = raw.strip() == b"false"
    if not history_complete:
        report("history", "shallow_history")
    for commit in commits:
        raw, _ = git(root, "ls-tree", "-r", "-z", commit)
        for entry in raw.split(b"\0"):
            if not entry:
                continue
            metadata, path_raw = entry.split(b"\t", 1)
            _, kind, object_raw = metadata.split()
            if kind == b"blob":
                path = os.fsdecode(path_raw)
                object_id = object_raw.decode("ascii")
                paths[object_id].add(path)
                for category, _ in secret_matches(path):
                    report("history", category, path, object_id=object_id)
                category = runtime_path(path)
                if category:
                    report("history", category, path, object_id=object_id)

    history_texts = 0
    for object_id in object_ids:
        raw, _ = git(root, "cat-file", "-t", object_id)
        kind = raw.strip()
        if kind not in {b"blob", b"commit", b"tag"}:
            continue
        object_paths = paths.get(object_id, set())
        if object_paths and all(runtime_path(path) for path in object_paths):
            continue
        raw, _ = git(root, "cat-file", "-s", object_id)
        if int(raw) > MAX_BYTES:
            report("history", "scan_limit", object_id=object_id)
            continue
        data, _ = git(root, "cat-file", "-p", object_id)
        history_texts += 1
        if data.startswith(b"SQLite format 3\0"):
            report("history", "runtime_database", object_id=object_id)
            continue
        if b"\0" in data:
            continue
        for category, line in secret_matches(data.decode("utf-8", errors="replace")):
            if kind == b"blob" and object_paths:
                for path in sorted(object_paths):
                    report("history", category, path, line, object_id)
            else:
                report("history", category, line=line, object_id=object_id)

    raw, _ = git(root, "ls-files", "--cached", "--others", "--exclude-standard", "-z")
    worktree_files = 0
    for path in sorted(set(os.fsdecode(item) for item in raw.split(b"\0") if item)):
        for category, _ in secret_matches(path):
            report("worktree", category, path)
        category = runtime_path(path)
        if category:
            report("worktree", category, path)
            continue
        target = root / path
        if any(component.is_symlink() for component in (target, *target.parents)):
            report("worktree", "symlink_path", path)
            continue
        try:
            fd = os.open(target, os.O_RDONLY | os.O_NOFOLLOW)
        except FileNotFoundError:
            # Deletions are still covered by the reachable history scan.
            continue
        with os.fdopen(fd, "rb") as file:
            info = os.fstat(file.fileno())
            if not stat.S_ISREG(info.st_mode):
                report("worktree", "unsupported_file", path)
                continue
            if info.st_size > MAX_BYTES:
                report("worktree", "scan_limit", path)
                continue
            data = file.read(MAX_BYTES + 1)
        worktree_files += 1
        if len(data) > MAX_BYTES:
            report("worktree", "scan_limit", path)
        elif data.startswith(b"SQLite format 3\0"):
            report("worktree", "runtime_database", path)
        elif b"\0" not in data:
            for category, line in secret_matches(data.decode("utf-8", errors="replace")):
                report("worktree", category, path, line)
    findings.sort(key=lambda item: (item["scope"], item.get("file", ""), item.get("object", ""),
                                    item.get("line", 0), item["category"]))
    return {"ok": not findings, "findings": findings,
            "scanned": {"history_objects": len(object_ids), "history_texts": history_texts,
                        "worktree_files": worktree_files, "complete_local_history": history_complete}}


class FixedErrorParser(argparse.ArgumentParser):
    def error(self, message):
        raise ValueError("arguments_failed")


def main(argv=None):
    parser = FixedErrorParser(description=__doc__)
    parser.add_argument("--repo", type=Path, default=Path.cwd())
    try:
        arguments = parser.parse_args(argv)
        result = scan(arguments.repo)
    except Exception:
        print(json.dumps({"error": "secret_scan_failed"}))
        return 2
    print(json.dumps(result, ensure_ascii=True, sort_keys=True))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
