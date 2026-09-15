#!/usr/bin/env python3
"""Create the first local light profile from pinned HOL sources on Linux."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
WB = ROOT / "hol-workbench"
sys.path.insert(0, str(WB))

from hol_workbench.cli.published_profile import resolve_published_warm_profile
from hol_workbench.criu_publication_provenance import committed_source_identity
from hol_workbench.runtime_config import load_runtime_config, runtime_config_path, write_runtime_config

APT = ("sudo apt-get install --no-install-recommends python3 git make gcc ocaml-nox "
       "ocaml-findlib camlp5 libzarith-ocaml-dev libcamlp-streams-ocaml-dev criu sudo")


class SetupError(RuntimeError):
    pass


def capture(argv: list[str], cwd: Path | None = None) -> str:
    result = subprocess.run(argv, cwd=cwd, text=True, capture_output=True, timeout=30)
    if result.returncode:
        raise SetupError(f"{shlex.join(argv)}: {(result.stderr or result.stdout).strip()}")
    return (result.stdout or result.stderr).strip()


def prerequisites(criu: Path, mode: str) -> dict[str, str]:
    missing = [tool for tool in ("git", "make", "gcc", "ocamlc", "ocamlfind", "camlp5")
               if shutil.which(tool) is None]
    if not criu.is_file() or not os.access(criu, os.X_OK):
        missing.append(str(criu))
    if missing:
        raise SetupError(f"Missing tools: {', '.join(missing)}\nDebian/Ubuntu packages:\n  {APT}")
    version = capture(["ocamlc", "-version"])
    if tuple(int(part) for part in version.split(".")[:2]) < (4, 14):
        raise SetupError(f"OCaml {version} is too old; this source build requires OCaml 4.14 or newer.")
    for package in ("zarith", "camlp-streams"):
        try:
            capture(["ocamlfind", "query", package])
        except SetupError as exc:
            raise SetupError(f"Missing OCaml package {package}.\n  {APT}") from exc
    if capture(["camlp5", "-pmode"]) != "strict":
        raise SetupError("HOL requires Camlp5 built in strict mode (camlp5 -pmode).")
    prefix = ["sudo", "-n"] if mode == "sudo" else []
    try:
        criu_version = capture([*prefix, str(criu), "--version"])
    except SetupError as exc:
        raise SetupError(f"CRIU is not authorized. Run sudo -v in this shell, then retry.\n{exc}") from exc
    if mode == "capability":
        from hol_workbench.criu_maintenance_preflight import validate_capability_executable

        validate_capability_executable(criu)
    with tempfile.TemporaryDirectory(prefix="hearth-kernel-check-") as temporary:
        options = ["--unprivileged"] if mode == "capability" else []
        capture([*prefix, str(criu), "check", "--no-default-config", *options], Path(temporary))
    return {"ocaml": version, "camlp5": capture(["camlp5", "-v"]), "criu": criu_version}


def run_phase(name: str, argv: list[str], *, logs: Path, cwd: Path, env: dict[str, str]) -> None:
    log = logs / f"{name}.log"
    print(f"SETUP: {name}\nLOG: {log}", flush=True)
    started = time.monotonic()
    with log.open("w") as output:
        output.write(shlex.join(argv) + "\n")
        output.flush()
        process = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                                   stdout=output, stderr=subprocess.STDOUT)
        try:
            while True:
                try:
                    code = process.wait(timeout=20)
                    break
                except subprocess.TimeoutExpired:
                    print(f"SETUP: {name} still running ({int(time.monotonic() - started)}s); see {log}", flush=True)
        except KeyboardInterrupt:
            process.terminate()
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            raise
    if code:
        tail = "\n".join(log.read_text(errors="replace").splitlines()[-18:])
        raise SetupError(f"{name} failed (exit {code}).\n{tail}\nFull log: {log}")
    print(f"SETUP: {name} completed in {time.monotonic() - started:.1f}s", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, epilog=(
        "Install distribution packages first; see docs/setup.md. This command fetches HOL, "
        "compiles its module, builds the evaluator, and creates light only when missing. "
        "It does not rebuild an existing profile or overwrite another runtime configuration."))
    parser.add_argument("--data-dir", type=Path, help="Local source and snapshot directory (default: XDG data/hol-hearth)")
    parser.add_argument("--criu-bin", type=Path, help="CRIU executable; also searches /usr/sbin/criu")
    parser.add_argument("--criu-mode", choices=("sudo", "capability"), default="sudo")
    parser.add_argument("--check", action="store_true", help="Check prerequisites without fetching, compiling or loading HOL")
    args = parser.parse_args()
    if sys.platform != "linux" or sys.version_info < (3, 11):
        raise SetupError("Linux and Python 3.11+ are required.")
    conflicts = [name for name in ("HOL_WORKBENCH_ORB_HOLDIR", "HOL_WORKBENCH_CRIU_SHELF_ROOT",
                 "HOL_WORKBENCH_CRIU_BIN", "HOL_WORKBENCH_CRIU_MODE", "CRIU_EXTRA_PRELOADS") if os.environ.get(name)]
    if conflicts:
        raise SetupError(f"Unset setup-conflicting runtime overrides first: {', '.join(conflicts)}")
    lock = json.loads((ROOT / "dev/setup-lock.json").read_text())
    data = (args.data_dir or Path(os.environ.get("XDG_DATA_HOME", str(Path.home() / ".local/share"))) / "hol-hearth").expanduser().resolve()
    holdir, shelves = data / "hol-light", data / "shelves"
    if data == ROOT or ROOT in data.parents:
        raise SetupError("Keep --data-dir outside the Hearth checkout so runtime artifacts do not enter source history.")
    criu = (args.criu_bin or Path(shutil.which("criu") or "/usr/sbin/criu")).expanduser().resolve()
    config_path = runtime_config_path()
    if config_path.exists():
        config = load_runtime_config()
        if (config.hol_light_dir, config.criu_shelf_root, config.criu_bin, config.criu_mode.value) != (holdir, shelves, criu, args.criu_mode):
            raise SetupError(f"Existing configuration preserved: {config_path}.\n"
                             "Use ./hearth doctor --profile light for that runtime. For a separate setup, "
                             "set HOL_WORKBENCH_RUNTIME_CONFIG to a new absolute file and choose --data-dir.")
    versions = prerequisites(criu, args.criu_mode)
    print("TOOLS: " + json.dumps(versions, sort_keys=True), flush=True)
    # Fail before downloads or cold loading if this is not a committed checkout.
    source_identity = committed_source_identity(ROOT)
    print(f"HOL SOURCE: {lock['hol_light_revision']}\nDATA: {data}\nCONFIG: {config_path}", flush=True)
    if args.check:
        print("SETUP: prerequisites ready; run ./hearth setup with the same options to build light")
        return 0
    data.mkdir(parents=True, exist_ok=True)
    with (data / "setup.lock").open("a") as guard:
        try:
            fcntl.flock(guard, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise SetupError(f"Another setup owns {data}; wait for it to finish.") from exc
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        logs = data / "setup-logs" / f"{stamp}-{os.getpid()}"
        logs.mkdir(parents=True)
        env = {**os.environ, "PYTHONUNBUFFERED": "1", "CRIU_PROFILES": "light", "HOLLIGHT_USE_MODULE": "1"}
        def phase(name: str, argv: list[str], cwd: Path = ROOT) -> None:
            run_phase(name, argv, logs=logs, cwd=cwd, env=env)
        profile_ready = False
        if config_path.exists():
            try:
                resolve_published_warm_profile(WB / "bin", "light")
                profile_ready = True
            except (OSError, RuntimeError, ValueError, SystemExit):
                pass
        if not profile_ready:
            # A failed/incompatible shelf needs diagnosis, not an automatic cold rebuild.
            if shelves.exists() and any(shelves.glob("criu-warm-profiles-*")):
                raise SetupError(f"Existing profile build found under {shelves}; inspect its build-results.json and logs before retrying. No profile rebuilt.")
            if not (holdir / ".git").is_dir():
                if holdir.exists() and any(holdir.iterdir()):
                    raise SetupError(f"Refusing to replace nonempty source directory: {holdir}")
                phase("source-init", ["git", "init", str(holdir)])
                phase("source-origin", ["git", "remote", "add", "origin", lock["hol_light_remote"]], holdir)
            if capture(["git", "remote", "get-url", "origin"], holdir) != lock["hol_light_remote"]:
                raise SetupError(f"Unexpected HOL source remote in {holdir}; directory preserved.")
            head = subprocess.run(["git", "rev-parse", "HEAD"], cwd=holdir, text=True, capture_output=True)
            if head.returncode:
                phase("source-fetch", ["git", "fetch", "--depth=1", "origin", lock["hol_light_revision"]], holdir)
                phase("source-checkout", ["git", "checkout", "--detach", "FETCH_HEAD"], holdir)
            elif head.stdout.strip() != lock["hol_light_revision"]:
                raise SetupError(f"HOL source revision differs from setup lock: {holdir}; directory preserved.")
            if capture(["git", "status", "--porcelain", "--untracked-files=no"], holdir):
                raise SetupError(f"HOL has modified tracked sources: {holdir}; directory preserved.")
            phase("hol-build", ["make", "-j2", "HOLLIGHT_USE_MODULE=1", "hol.sh"], holdir)
            if not all((holdir / name).is_file() for name in ("ocaml-hol", "hol_lib.cmo", "pa_j.cmo")):
                raise SetupError("HOL build did not produce its required runtime files.")
            if not config_path.exists():
                write_runtime_config(hol_light_dir=holdir, criu_shelf_root=shelves, criu_bin=criu, criu_mode=args.criu_mode)
        phase("native-build", [str(ROOT / "hearth"), "build-native", "--ocamlc", str(Path(shutil.which("ocamlc")).resolve())])
        if not profile_ready:
            phase("light-profile", [sys.executable, "-I", "-B", "-c",
                  "import runpy,sys; sys.path.insert(0,sys.argv.pop(1)); runpy.run_module('hol_workbench.cli.orbstack_criu_build',run_name='__main__')", str(WB)])
        profile = resolve_published_warm_profile(WB / "bin", "light")
        phase("doctor", [str(ROOT / "hearth"), "doctor", "--profile", "light"])
        receipt = {"schema": "hol-hearth.setup.v1", "status": "ready", "profile": "light",
                   "profile_reused": profile_ready, "hol_revision": lock["hol_light_revision"],
                   "hearth_revision": source_identity["revision"], "tools": versions,
                   "config": str(config_path), "profile_root": str(profile.root), "logs": str(logs)}
        (logs / "setup.json").write_text(json.dumps(receipt, indent=2) + "\n")
        print(f"SETUP: ready (light {'reused' if profile_ready else 'built and restored'})\n"
              f"RECEIPT: {logs / 'setup.json'}\nNEXT: ./hearth demo cat-map", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (SetupError, OSError, RuntimeError, ValueError, subprocess.SubprocessError) as exc:
        print(f"SETUP: blocked\n{exc}", file=sys.stderr)
        raise SystemExit(2)
    except KeyboardInterrupt:
        print("SETUP: interrupted; inspect the printed logs before retrying", file=sys.stderr)
        raise SystemExit(130)
