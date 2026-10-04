#!/usr/bin/env python3
"""Local release checks using synthetic state, Git history and temporary packages."""

import argparse
import importlib.metadata
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import sysconfig
import tarfile
import tempfile
import venv
import zipfile


def run(command, cwd, *, env=None):
    result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
    return result.returncode, result.stdout + result.stderr


def inspect_packages(directory):
    wheels = list(directory.glob("*.whl"))
    sources = list(directory.glob("*.tar.gz"))
    if len(wheels) != 1 or len(sources) != 1:
        raise ValueError("package_count")
    with zipfile.ZipFile(wheels[0]) as package:
        names = package.namelist()
        required = ("wechat_dot_bridge/migrations.py", "wechat_dot_bridge/maintenance.py",
                    "wechat_dot_bridge/sdk_adapter.py", "wechat_dot_bridge/net_transport.py",
                    "wechat_dot_bridge/tool_contract.py", "wechat_dot_bridge/private_paths.py",
                    "wechat_dot_bridge/lifecycle.py", "wechat_dot_bridge/webhook_signing.py",
                    "wechat_dot_bridge/codecs/markdown-filter.ts", "wechat_dot_bridge/codecs/audio.mjs")
        if any(name not in names for name in required):
            raise ValueError("package_resource_missing")
        for required_license in ("LICENSE", "NOTICE.txt", "Tencent-openclaw-weixin-MIT.txt", "silk-wasm-MIT.txt"):
            if not any(name.endswith("/" + required_license) for name in names):
                raise ValueError("package_license_missing")
        metadata = next(name for name in names if name.endswith(".dist-info/METADATA"))
        if b"Description-Content-Type: text/markdown" not in package.read(metadata):
            raise ValueError("package_readme_missing")
    with tarfile.open(sources[0]) as package:
        source_names = package.getnames()
        for document in ("README.md", "CONTRIBUTING.md", "SECURITY.md", "CHANGELOG.md", ".gitignore",
                         "scripts/check_release.py", "scripts/verify-upgrade.py", "scripts/scan_secrets.py",
                         "tests/network_fixtures.py", "tests/test_upgrade_script.py",
                         "docs/README.md", "docs/INSTALL.md", "docs/DOT_SETUP.md", "docs/RELEASING.md", "docs/OPERATIONS.md",
                         "docs/UPSTREAM_AUDIT.md", "docs/history/UPSTREAM_AUDIT.md", "docs/history/REAL_VALIDATION.md", "uv.lock"):
            if not any(name.endswith("/" + document) for name in source_names):
                raise ValueError("source_document_missing")
    forbidden = re.compile(r"(?:^|/)(?:node_modules|\.git|runtime|state|secrets)(?:/|$)|\.(?:sqlite\w*|db|key|pem)$|(?:^|/)\.env(?:\.|$)")
    if any(forbidden.search(name) for name in names + source_names):
        raise ValueError("package_private_file")
    return wheels[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--old-revision", help="Optional local Git revision for the synthetic upgrade exercise")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parent.parent
    checks = []
    # Do not inherit PYTHONPATH that could make the installed-wheel smoke test
    # accidentally import the checkout. No account, config or service is opened.
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    try:
        setuptools_version = importlib.metadata.version("setuptools")
        if int(setuptools_version.split(".")[0]) < 77:
            raise ValueError("build_dependency_missing")
        upgrade = [sys.executable, "scripts/verify-upgrade.py"]
        if args.old_revision is not None:
            upgrade.extend(["--old-revision", args.old_revision])
        commands = (
            ("lock", ["uv", "lock", "--check"]),
            ("lint", [sys.executable, "-m", "ruff", "check", "wechat_dot_bridge", "scripts", "tests"]),
            ("syntax", [sys.executable, "-m", "compileall", "-q", "wechat_dot_bridge", "scripts", "tests"]),
            ("tests", [sys.executable, "-m", "unittest", "discover", "-s", "tests", "-v"]),
            ("upgrade", upgrade),
            ("secrets", [sys.executable, "scripts/scan_secrets.py", "--repo", str(root)]),
            ("diff", ["git", "diff", "--check"]),
        )
        for name, command in commands:
            print("Checking " + name + "...", file=sys.stderr, flush=True)
            code, output = run(command, root, env=env)
            check = {"name": name, "passed": code == 0}
            if name == "tests":
                summary = re.search(r"Ran (\d+) tests", output)
                if summary:
                    check["count"] = int(summary[1])
                skipped = re.search(r"skipped=(\d+)", output)
                check["skipped"] = int(skipped[1]) if skipped else 0
            if name == "secrets" and output.strip():
                # Scanner output contains locations/categories only, never values.
                try:
                    check["report"] = json.loads(output)
                except ValueError:
                    check["error"] = "scanner_result_invalid"
            checks.append(check)
            if code:
                print(json.dumps({"passed": False, "checks": checks}), flush=True)
                return 1
        with tempfile.TemporaryDirectory(prefix="bridge-release-") as temporary:
            temp = Path(temporary)
            dist = temp / "dist"
            dist.mkdir()
            print("Checking packages...", file=sys.stderr, flush=True)
            # Call the configured PEP 517 backend directly. Its dependencies are
            # preinstalled by the operator; verification needs no network access.
            build = "from setuptools.build_meta import build_sdist,build_wheel; import sys; target=sys.argv[1]; build_sdist(target); build_wheel(target)"
            code, _ = run([sys.executable, "-c", build, str(dist)], root, env=env)
            if code:
                raise ValueError("package_build_failed")
            wheel = inspect_packages(dist)
            checks.append({"name": "packages", "passed": True})
            environment = temp / "wheel-env"
            # Reuse only the already installed runtime libraries, then install our
            # wheel without network/dependency resolution outside the checkout.
            venv.EnvBuilder(with_pip=True).create(environment)
            python = environment / "bin" / "python"
            code, purelib = run([str(python), "-c", "import sysconfig; print(sysconfig.get_path('purelib'))"], temp, env=env)
            if code:
                raise ValueError("wheel_install_failed")
            dependency_paths = {str(Path(value).absolute()) for value in sys.path
                                if value and Path(value).name in {"site-packages", "dist-packages"}}
            dependency_paths.update(sysconfig.get_path(name) for name in ("purelib", "platlib"))
            # Add package directories directly, without evaluating the parent's
            # editable-install .pth files or adding the checkout to sys.path.
            (Path(purelib.strip()) / "bridge-runtime-dependencies.pth").write_text(
                "\n".join(sorted(dependency_paths)) + "\n"
            )
            code, _ = run([str(python), "-m", "pip", "install", "--no-index", "--no-deps", "--no-cache-dir", str(wheel)], temp, env=env)
            if code:
                raise ValueError("wheel_install_failed")
            smoke = """
from importlib.resources import files
from pathlib import Path
import tempfile
import wechat_dot_bridge
from wechat_dot_bridge.mcp import tool_definitions
from wechat_dot_bridge.store import Store
from wechat_dot_bridge.migrations import SCHEMA_VERSION
assert str(Path(wechat_dot_bridge.__file__).resolve()).startswith(str(Path(__import__('sys').prefix).resolve()))
assert len(tool_definitions()) == 11
assert files('wechat_dot_bridge').joinpath('codecs/markdown-filter.ts').is_file()
with tempfile.TemporaryDirectory() as state:
    store=Store(Path(state)/'state')
    assert store.db.execute('PRAGMA user_version').fetchone()[0] == SCHEMA_VERSION
    assert not store.get_status()['bound']
    store.close()
"""
            code, _ = run([str(python), "-c", smoke], temp, env=env)
            if code:
                raise ValueError("wheel_smoke_failed")
            checks.append({"name": "installed_wheel", "passed": True,
                           "runtime_dependencies": "existing_environment"})
        print(json.dumps({"passed": True, "checks": checks}), flush=True)
        return 0
    except (OSError, ValueError, importlib.metadata.PackageNotFoundError) as error:
        known = {"build_dependency_missing", "package_build_failed", "wheel_install_failed", "wheel_smoke_failed",
                 "package_count", "package_resource_missing", "package_license_missing", "package_readme_missing",
                 "source_document_missing", "package_private_file"}
        code = str(error) if str(error) in known else "release_check_failed"
        print(json.dumps({"passed": False, "error": code, "checks": checks}), flush=True)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
