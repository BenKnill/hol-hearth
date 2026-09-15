from __future__ import annotations

import argparse
import os
import shutil
from pathlib import Path


def fake_ocaml_hol_launcher_text(
    *,
    import_root: Path,
    mode: str = "success",
    profile: str = "none",
    seed: int | None = 0,
    standalone: bool = False,
) -> str:
    root = import_root.resolve()
    seed_value = "random" if seed is None else str(seed)
    return "\n".join(
        [
            "#!/usr/bin/env python3",
            "import os",
            "import pathlib",
            "import sys",
            f"os.environ.setdefault('FAKE_HOL_MODE', {mode!r})",
            f"os.environ.setdefault('FAKE_HOL_PROFILE', {profile!r})",
            f"os.environ.setdefault('FAKE_HOL_SEED', {seed_value!r})",
            f"sys.path.insert(0, {str(root)!r})",
            "from fake_hol.cli_ocaml_hol import main",
            "raise SystemExit(main(sys.argv[1:]))",
            "",
        ]
    )


def install_fake_holdir(
    path: Path | str,
    *,
    mode: str = "success",
    profile: str = "none",
    seed: int | None = 0,
    standalone: bool = False,
) -> Path:
    holdir = Path(path)
    holdir.mkdir(parents=True, exist_ok=True)

    stublibs = holdir / "_opam" / "lib" / "stublibs"
    stublibs.mkdir(parents=True, exist_ok=True)
    (holdir / "_opam" / "bin").mkdir(parents=True, exist_ok=True)
    (stublibs / "dllzarith.so").write_text("fake\n", encoding="utf-8")
    (holdir / "hol.ml").write_text("(* fake HOL Light init file *)\n", encoding="utf-8")

    package_dir = Path(__file__).resolve().parent
    if standalone:
        installed_package = holdir / "fake_hol"
        if installed_package.exists():
            shutil.rmtree(installed_package)
        shutil.copytree(package_dir, installed_package, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
        import_root = holdir
    else:
        import_root = package_dir.parent

    ocaml_hol = holdir / "ocaml-hol"
    ocaml_hol.write_text(
        fake_ocaml_hol_launcher_text(
            import_root=import_root,
            mode=mode,
            profile=profile,
            seed=seed,
            standalone=standalone,
        ),
        encoding="utf-8",
    )
    ocaml_hol.chmod(0o755)

    hol_sh = holdir / "hol.sh"
    hol_sh.write_text('#!/usr/bin/env sh\nexec "$(dirname "$0")/ocaml-hol" "$@"\n', encoding="utf-8")
    hol_sh.chmod(0o755)
    return holdir


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Install a fake HOL Light HOLDIR for harness tests.")
    parser.add_argument("path", help="Directory to create or update as the fake HOLDIR")
    parser.add_argument("--mode", default=os.environ.get("FAKE_HOL_MODE", "success"))
    parser.add_argument("--profile", default=os.environ.get("FAKE_HOL_PROFILE", "none"))
    parser.add_argument("--seed", default=os.environ.get("FAKE_HOL_SEED", "0"))
    parser.add_argument("--standalone", action="store_true", help="Copy fake_hol into the fake HOLDIR")
    args = parser.parse_args(argv)
    seed = None if str(args.seed).lower() in {"none", "random"} else int(args.seed)
    holdir = install_fake_holdir(
        Path(args.path),
        mode=args.mode,
        profile=args.profile,
        seed=seed,
        standalone=args.standalone,
    )
    print(holdir.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
