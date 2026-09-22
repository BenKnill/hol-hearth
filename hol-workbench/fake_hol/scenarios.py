from __future__ import annotations

import argparse
import json
import shlex
import tempfile
import textwrap
from pathlib import Path
from typing import cast

from .install import install_fake_holdir
from .runtime import FakeHolConfig, FakeHolRuntime, parse_seed

SCENARIOS = {
    "tiny-success": """
let TINY_SPEC = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "tutorial-basic": """
let TAUT_SPEC = TAUT `p \\/ ~p`;;
let ARITH_SPEC = ARITH_RULE `0 < x /\\ x < 7 ==> 1 <= x /\\ x <= 6`;;
let TINY_SPEC = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "false-green": """
let HELPER = prove(`T`,MESON_TAC[]);;
(* FAKE_HOL: false-green Exception: Failure "fake false green". *)
""".lstrip(),
    "source-echo": """
let f x =
  try x with Failure _ -> x;;
let TINY_SPEC = prove(`T`,MESON_TAC[]);;
""".lstrip(),
    "wrong-theorem": """
let OTHER_SPEC = prove(`T`,MESON_TAC[]);;
""".lstrip(),
    "claim-mismatch": """
let TINY_SPEC = prove(`F`,MESON_TAC[]);;
""".lstrip(),
    "roleplay-mixed": """
(* FAKE_HOL: warning Warning: inventing type variables *)
(* FAKE_HOL: goalstack 2 *)
(* FAKE_HOL: noise mixed 12 *)
let HELPER_SPEC = prove(`T`,MESON_TAC[]);;
let ARITH_SPEC = ARITH_RULE `0 < x ==> 0 <= x`;;
(* FAKE_HOL: timing *)
let TINY_SPEC = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "type-error": """
let HELPER_SPEC = prove(`T`,MESON_TAC[]);;
(* FAKE_HOL: type-error *)
""".lstrip(),
    "parse-error": """
let HELPER_SPEC = prove(`T`,MESON_TAC[]);;
(* FAKE_HOL: parse-error *)
""".lstrip(),
    "partial-crash": """
let HELPER_SPEC = prove(`T`,MESON_TAC[]);;
(* FAKE_HOL: partial-crash 2 *)
""".lstrip(),
    "multi-claim-success": """
let MULTI_ONE = prove(`T`,MESON_TAC[]);;
let MULTI_TWO = ARITH_RULE `0 < x ==> 0 <= x`;;
let MULTI_THREE = TAUT `p \\/ ~p`;;
""".lstrip(),
    "dependency-noise": """
#use "deps/fake_dependency.ml";;
(* FAKE_HOL: noise mixed 16 *)
let TARGET_AFTER_DEPS = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "local-needs-chain": """
needs "deps/child.ml";;
let TARGET_AFTER_LOCAL_NEEDS = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "missing-local-needs": """
needs "deps/missing_dependency.ml";;
let TARGET_AFTER_MISSING_NEEDS = prove(`T`,MESON_TAC[]);;
""".lstrip(),
    "gnarly-race-pack": """
needs "deps/race_prelude.ml";;
(* FAKE_HOL: warning Warning: replaying fake race pack with stale-looking scratch state *)
(* FAKE_HOL: goalstack 2 *)
(* FAKE_HOL: noise mixed 40 *)
let GNARLY_PREFIX_1 = prove(`!x. x = x`,MESON_TAC[]);;
(* FAKE_HOL: sleep 20ms *)
let GNARLY_PREFIX_2 = ARITH_RULE `0 < n ==> 0 <= n`;;
(* FAKE_HOL: multiline on *)
let GNARLY_PREFIX_3 = prove(`
  !a b c d e f.
    a = b /\\ b = c /\\ c = d /\\ d = e /\\ e = f
    ==> a = f
`,MESON_TAC[]);;
(* FAKE_HOL: fail-on GNARLY_FINAL Exception: Failure "late fake race target failed after useful prefix". *)
let GNARLY_FINAL = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "multiline-theorem": """
(* FAKE_HOL: multiline on *)
let MULTILINE_SPEC = prove(`
  !a b c d e f g h.
    a = b /\\
    b = c /\\
    c = d /\\
    d = e /\\
    e = f /\\
    f = g /\\
    g = h
    ==> a = h
`,MESON_TAC[]);;
""".lstrip(),
    "ansi-noisy": """
(* FAKE_HOL: color on *)
(* FAKE_HOL: noise bdd 8 *)
let ANSI_NOISY_SPEC = prove(`forall n. SUC n = n + 1`,MESON_TAC[]);;
""".lstrip(),
    "slow-success": """
(* FAKE_HOL: sleep 25ms *)
let SLOW_TINY_SPEC = prove(`T`,MESON_TAC[]);;
""".lstrip(),
    "prefix-then-failure": """
let PREFIX_OK = prove(`T`,MESON_TAC[]);;
(* FAKE_HOL: false-green Exception: Failure "fake failure after useful prefix". *)
let AFTER_FAILURE = prove(`!x. x = x`,MESON_TAC[]);;
""".lstrip(),
    "type-drift-after-prefix": """
let PREFIX_OK = prove(`T`,MESON_TAC[]);;
let det2 a b c d = a * d - b * c;;
(* FAKE_HOL: fail-on DRIFT_SPEC Exception: Failure "types do not agree: under-annotated real arguments". *)
let DRIFT_SPEC = prove(`!a b c d. det2 a b c d = det2 a b c d`,MESON_TAC[]);;
""".lstrip(),
    "ready-with-definitions": """
let det2 = new_definition
 `det2 (a:real) b c d = a * d - b * c`;;

let mul00 = new_definition
 `mul00 (a:real) b c d e f g h = a * e + b * g`;;

let READY_WARNING_SPEC = prove
 (`!a b c d:real. mul00 a b c d d (--b) (--c) a = det2 a b c d`,
  REWRITE_TAC[mul00; det2] THEN REAL_ARITH_TAC);;
""".lstrip(),
}

SCENARIO_EXTRA_FILES = {
    "dependency-noise": {
        "deps/fake_dependency.ml": """
(* FAKE_HOL: warning Warning: loading fake dependency *)
(* FAKE_HOL: noise search 10 *)
let DEP_HELPER = prove(`T`,MESON_TAC[]);;
""".lstrip(),
    },
    "local-needs-chain": {
        "deps/child.ml": """
needs "grandchild.ml";;
let NEEDS_CHILD_HELPER = prove(`T`,MESON_TAC[]);;
""".lstrip(),
        "deps/grandchild.ml": """
needs "Library/ringtheory.ml";;
let NEEDS_GRANDCHILD_HELPER = prove(`T`,MESON_TAC[]);;
""".lstrip(),
    },
    "gnarly-race-pack": {
        "deps/race_prelude.ml": """
needs "Library/ringtheory.ml";;
needs "local_helper.ml";;
(* FAKE_HOL: noise search 14 *)
let GNARLY_PRELUDE_HELPER = prove(`T`,MESON_TAC[]);;
""".lstrip(),
        "deps/local_helper.ml": """
needs "calc_rat.ml";;
let GNARLY_LOCAL_HELPER = REAL_ARITH `!x:real. x = x`;;
""".lstrip(),
    },
}

SCENARIO_EXPECTATIONS = {
    "tiny-success": {"theorem": "TINY_SPEC", "expected": "proved"},
    "tutorial-basic": {"theorem": "TINY_SPEC", "expected": "proved"},
    "false-green": {"theorem": "HELPER", "expected": "filter_failure"},
    "source-echo": {"theorem": "TINY_SPEC", "expected": "proved_with_source_echo_warning"},
    "wrong-theorem": {"theorem": "OTHER_SPEC", "expected": "succeeded"},
    "claim-mismatch": {"theorem": "TINY_SPEC", "expected": "claim_mismatch"},
    "roleplay-mixed": {"theorem": "TINY_SPEC", "expected": "proved_with_noise"},
    "type-error": {"theorem": "HELPER_SPEC", "expected": "filter_failure"},
    "parse-error": {"theorem": "HELPER_SPEC", "expected": "filter_failure"},
    "partial-crash": {"theorem": "HELPER_SPEC", "expected": "child_exit_nonzero"},
    "multi-claim-success": {"theorem": "MULTI_ONE", "expected": "proved"},
    "dependency-noise": {"theorem": "TARGET_AFTER_DEPS", "expected": "proved_with_dependency_noise"},
    "local-needs-chain": {"theorem": "TARGET_AFTER_LOCAL_NEEDS", "expected": "proved_with_transitive_local_needs"},
    "missing-local-needs": {"theorem": "TARGET_AFTER_MISSING_NEEDS", "expected": "filter_failure_from_missing_needs"},
    "gnarly-race-pack": {"theorem": "GNARLY_FINAL", "expected": "late_target_failure_after_noisy_prefix"},
    "multiline-theorem": {"theorem": "MULTILINE_SPEC", "expected": "proved_with_multiline_theorem_output"},
    "ansi-noisy": {"theorem": "ANSI_NOISY_SPEC", "expected": "proved_with_ansi_and_noise"},
    "slow-success": {"theorem": "SLOW_TINY_SPEC", "expected": "proved_after_small_delay"},
    "prefix-then-failure": {"theorem": "PREFIX_OK", "expected": "filter_failure_after_prefix"},
    "type-drift-after-prefix": {"theorem": "DRIFT_SPEC", "expected": "type_failure_after_prefix"},
    "ready-with-definitions": {"theorem": "READY_WARNING_SPEC", "expected": "proved_with_source_readiness_warnings"},
}

VM_FEEDBACK_SUITE = [
    "tiny-success",
    "tutorial-basic",
    "source-echo",
    "dependency-noise",
    "local-needs-chain",
    "missing-local-needs",
    "gnarly-race-pack",
    "multiline-theorem",
    "ansi-noisy",
    "claim-mismatch",
    "wrong-theorem",
    "false-green",
    "prefix-then-failure",
    "type-drift-after-prefix",
    "ready-with-definitions",
    "slow-success",
]

SCENARIO_DESCRIPTIONS = {
    "tiny-success": "One small theorem; cheapest transcript sanity check.",
    "tutorial-basic": "Three common theorem constructors: TAUT, ARITH_RULE, prove.",
    "false-green": "Prints an exception while the fake process exits 0; harness should fail it.",
    "source-echo": "Contains source text with Failure; harness should not treat source echo as proof failure.",
    "wrong-theorem": "Contains OTHER_SPEC; the ordinary source command discovers and checks the actual claim.",
    "claim-mismatch": "Binds the expected theorem name to a different statement.",
    "roleplay-mixed": "Deterministic noisy transcript with warnings, goalstack-ish output, and timing.",
    "type-error": "Emits a HOL/OCaml type-failure style line.",
    "parse-error": "Emits a syntax-error style line.",
    "partial-crash": "Prints a helper theorem, then crashes.",
    "multi-claim-success": "Several successful claims for --all dashboards and warm multi-claim tests.",
    "dependency-noise": "Loads a fake dependency file before the target theorem.",
    "local-needs-chain": "Loads a transitive local needs chain while virtualizing a HOL library need.",
    "missing-local-needs": "Emits a missing local needs exception even if a later target-shaped theorem appears.",
    "gnarly-race-pack": "Noisy multi-claim race pack with local deps, sleeps, multiline output, and a late failure.",
    "multiline-theorem": "Forces multiline theorem output around one target claim.",
    "ansi-noisy": "Adds ANSI-colored theorem text plus BDD-like noise.",
    "slow-success": "Sleeps briefly before proving a tiny theorem.",
    "prefix-then-failure": "A useful prefix theorem followed by a false-green failure line.",
    "type-drift-after-prefix": "A useful prefix theorem followed by an under-annotated real/type-drift failure.",
    "ready-with-definitions": "Local definitions plus a theorem; useful for source-readiness warnings.",
}


def scenario_text(name: str) -> str:
    try:
        return SCENARIOS[name]
    except KeyError as exc:
        known = ", ".join(sorted(SCENARIOS))
        raise SystemExit(f"unknown fake HOL scenario {name!r}; known scenarios: {known}") from exc


def scenario_extra_files(name: str) -> dict[str, str]:
    scenario_text(name)
    return dict(SCENARIO_EXTRA_FILES.get(name, {}))


def transcript_for_scenario(name: str, *, cwd: Path | str | None = None, config: FakeHolConfig | None = None) -> str:
    cwd = Path.cwd() if cwd is None else Path(cwd)
    extras = scenario_extra_files(name)
    if not extras:
        return FakeHolRuntime(config or FakeHolConfig()).run_text(scenario_text(name), cwd=cwd).stdout
    with tempfile.TemporaryDirectory(prefix=f"fake-hol-{name}-") as tmp:
        tmp_path = Path(tmp)
        write_scenario_files(name, tmp_path)
        source = tmp_path / "source.ml"
        return (
            FakeHolRuntime(config or FakeHolConfig()).run_text(source.read_text(encoding="utf-8"), cwd=tmp_path).stdout
        )


def scenario_catalog() -> list[dict[str, object]]:
    return [
        {
            "name": name,
            "description": SCENARIO_DESCRIPTIONS.get(name, ""),
            "theorem": SCENARIO_EXPECTATIONS.get(name, {}).get("theorem"),
            "expected": SCENARIO_EXPECTATIONS.get(name, {}).get("expected"),
            "extra_files": sorted(SCENARIO_EXTRA_FILES.get(name, {})),
        }
        for name in sorted(SCENARIOS)
    ]


def suite_names(name: str) -> list[str]:
    if name == "vm-feedback":
        return list(VM_FEEDBACK_SUITE)
    if name == "all":
        return sorted(SCENARIOS)
    raise SystemExit(f"unknown fake HOL scenario suite {name!r}; known suites: vm-feedback, all")


def write_scenario_files(name: str, directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    source = directory / "source.ml"
    source.write_text(scenario_text(name), encoding="utf-8")
    for rel, text in scenario_extra_files(name).items():
        path = directory / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    return source


def default_workbench_root() -> Path:
    return Path(__file__).resolve().parents[2]


def materialize_lab(
    root: Path,
    names: list[str],
    *,
    workbench_root: Path | None = None,
    standalone_fake_hol: bool = False,
) -> dict[str, object]:
    root = root.resolve()
    workbench_root = (workbench_root or default_workbench_root()).resolve()
    root.mkdir(parents=True, exist_ok=True)
    holdir = install_fake_holdir(root / "fake-hol", standalone=standalone_fake_hol)
    scenarios_dir = root / "scenarios"
    runs_dir = root / "runs"
    runs_dir.mkdir(exist_ok=True)

    entries: list[dict[str, object]] = []
    for name in names:
        scenario_dir = scenarios_dir / name
        source = write_scenario_files(name, scenario_dir)
        expectation = dict(SCENARIO_EXPECTATIONS.get(name, {}))
        theorem = str(expectation.get("theorem") or "")
        run_root = runs_dir / name
        run_root.mkdir(exist_ok=True)
        proof_run = workbench_root / "hol-workbench" / "bin" / "proof-run"
        status_cmd = [
            str(proof_run),
            "doctor",
            "--holdir",
            str(holdir),
            "--timeout",
            "5",
        ]
        run_cmd = [
            str(proof_run),
            "run",
            "--source",
            str(source),
            "--theorem",
            theorem,
            "--holdir",
            str(holdir),
            "--run-root",
            str(run_root),
            "--doctor-timeout",
            "5",
        ]
        run_script = scenario_dir / "run.sh"
        run_script.write_text(
            scenario_run_script(
                workbench_root=workbench_root,
                holdir=holdir,
                source=source,
                scenario_dir=scenario_dir,
                theorem=theorem,
                run_root=run_root,
            ),
            encoding="utf-8",
        )
        run_script.chmod(0o755)
        expected: dict[str, object] = {
            "schema": "fake-hol.scenario.expected.v1",
            "name": name,
            "description": SCENARIO_DESCRIPTIONS.get(name, ""),
            "source": str(source),
            "theorem": theorem,
            "expected": expectation.get("expected"),
            "fake_hol_only": True,
            "not_proof_evidence": True,
            "status_command": shell_join(status_cmd),
            "run_command": shell_join(run_cmd),
        }
        (scenario_dir / "expected.json").write_text(
            json.dumps(expected, indent=2, sort_keys=True) + "\n", encoding="utf-8"
        )
        entries.append(expected)

    manifest: dict[str, object] = {
        "schema": "fake-hol.feedback-lab.v1",
        "root": str(root),
        "workbench_root": str(workbench_root),
        "holdir": str(holdir),
        "scenario_count": len(entries),
        "scenarios": entries,
    }
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    (root / "README.md").write_text(feedback_lab_readme(manifest), encoding="utf-8")
    return manifest


def scenario_run_script(
    *,
    workbench_root: Path,
    holdir: Path,
    source: Path,
    scenario_dir: Path,
    theorem: str,
    run_root: Path,
) -> str:
    return textwrap.dedent(
        f"""\
        #!/usr/bin/env sh
        set -eu

        WORKBENCH_ROOT=${{WORKBENCH_ROOT:-{shlex.quote(str(workbench_root))}}}
        FAKE_HOL_LAB_HOLDIR=${{FAKE_HOL_LAB_HOLDIR:-{shlex.quote(str(holdir))}}}
        RUN_ROOT=${{FAKE_HOL_LAB_RUN_ROOT:-{shlex.quote(str(run_root))}}}
        PROOF_RUN="$WORKBENCH_ROOT/hol-workbench/bin/proof-run"
        SCENARIO_DIR={shlex.quote(str(scenario_dir))}
        SOURCE={shlex.quote(str(source))}
        THEOREM={shlex.quote(theorem)}

        case "${{1:-run}}" in
          status)
            exec "$PROOF_RUN" doctor --holdir "$FAKE_HOL_LAB_HOLDIR" --timeout 5
            ;;
          run)
            exec "$PROOF_RUN" run --source "$SOURCE" --theorem "$THEOREM" \
              --holdir "$FAKE_HOL_LAB_HOLDIR" --run-root "$RUN_ROOT" --doctor-timeout 5
            ;;
          explain)
            exec "$PROOF_RUN" explain --run-root "$RUN_ROOT" --source "$SOURCE"
            ;;
          view)
            exec python3 - "$SCENARIO_DIR" "$SOURCE" "$RUN_ROOT" "$WORKBENCH_ROOT" <<'PY'
import json
import sys
from pathlib import Path

scenario_dir = Path(sys.argv[1])
source = Path(sys.argv[2])
run_root = Path(sys.argv[3])
workbench_root = Path(sys.argv[4])


def section(title: str) -> None:
    print()
    print(f"## {{title}}")


def show_text(path: Path, *, max_lines: int = 80) -> None:
    if not path.exists():
        print(f"(missing: {{path}})")
        return
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    if len(lines) > max_lines:
        print(f"--- {{path}} (last {{max_lines}} of {{len(lines)}} lines) ---")
        lines = lines[-max_lines:]
    else:
        print(f"--- {{path}} ---")
    for line in lines:
        print(line)


def latest_child(
    root: Path,
    *,
    exclude: set[str] | None = None,
    require_any: set[str] | None = None,
) -> Path | None:
    exclude = exclude or set()
    if not root.exists():
        return None
    children = [path for path in root.iterdir() if path.is_dir() and path.name not in exclude]
    if require_any:
        children = [path for path in children if any((path / name).exists() for name in require_any)]
    return max(children, key=lambda path: path.stat().st_mtime, default=None)


print("# Fake HOL Replay View")
print("boundary: fake HOL exercises harness behavior only; it is not proof evidence")
print(f"scenario: {{scenario_dir.name}}")
print(f"run_root: {{run_root}}")

section("Expected Contract")
show_text(scenario_dir / "expected.json", max_lines=80)

section("Source")
show_text(source, max_lines=120)

deps = sorted(path for path in scenario_dir.rglob("*.ml") if path != source)
if deps:
    section("Local Dependency Sources")
    for dep in deps:
        show_text(dep, max_lines=80)

latest_run = latest_child(
    run_root,
    exclude={{"context"}},
    require_any={{"batch-summary.json", "proof-receipt.md", "run-card.txt", "targets", "sessions"}},
)
section("Latest Run")
if latest_run is None:
    print("(no workbench run found yet; run this first: ./run.sh run)")
    raise SystemExit(0)
print(latest_run)

for rel in ("proof-receipt.md", "run-card.txt"):
    path = latest_run / rel
    if path.exists():
        section(rel)
        show_text(path, max_lines=120)

artifact_root = latest_run / "targets"
if not artifact_root.exists():
    artifact_root = latest_run / "sessions"
target_dirs = sorted(artifact_root.iterdir()) if artifact_root.exists() else []
for target_dir in target_dirs:
    if not target_dir.is_dir():
        continue
    section(f"Target {{target_dir.name}}")
    for rel in (
        "agent-card.txt",
        "preflight-card.txt",
        "doctor-card.txt",
        "failure-card.txt",
        "failure-context.txt",
        "proof-summary.json",
        "failure.json",
        "target.json",
    ):
        path = target_dir / rel
        if path.exists():
            show_text(path, max_lines=80)
    raw_log = target_dir / "proof.raw.log"
    if raw_log.exists():
        section(f"Raw Fake HOL Transcript Tail: {{target_dir.name}}")
        show_text(raw_log, max_lines=120)

context_root = latest_child(run_root / "context")
if context_root is not None:
    section("Gated Context Transcript Tail")
    context_run = workbench_root / "hol-workbench" / "bin" / "context-run"
    print(f"manual peek: {{context_run}} peek {{context_root}} --tail 80")
    show_text(context_root / "combined.log", max_lines=120)
PY
            ;;
          *)
            echo "usage: $0 [status|run|explain|view]" >&2
            exit 2
            ;;
        esac
        """
    ).lstrip()


def feedback_lab_readme(manifest: dict[str, object]) -> str:
    scenarios = manifest["scenarios"]
    rows: list[str] = []
    scenario_items = cast(list[dict[str, object]], scenarios) if isinstance(scenarios, list) else []
    for item in scenario_items:
        rows.append(f"| `{item['name']}` | `{item['theorem']}` | `{item['expected']}` | {item['description']} |")
    table = "\n".join(rows)
    return (
        "# Fake HOL VM Feedback Lab\n\n"
        "This lab is for reviewing HOL Hearth UX without real HOL Light.\n"
        "It uses the explicit developer-only `proof-run` integration surface with\n"
        "a fake `HOLDIR`; it never invokes the public warm-only `prove` route.\n"
        "Results here exercise harness behavior only and are not theorem evidence.\n\n"
        "Start with:\n\n"
        "```sh\n"
        f"cd {shlex.quote(str(manifest['root']))}\n"
        "./scenarios/tiny-success/run.sh status\n"
        "./scenarios/tiny-success/run.sh run\n"
        "./scenarios/tiny-success/run.sh explain\n"
        "./scenarios/tiny-success/run.sh view\n"
        "```\n\n"
        "Then try failure and noisy cases:\n\n"
        "```sh\n"
        "./scenarios/source-echo/run.sh run\n"
        "./scenarios/dependency-noise/run.sh run\n"
        "./scenarios/multiline-theorem/run.sh run\n"
        "./scenarios/false-green/run.sh run || true\n"
        "./scenarios/type-drift-after-prefix/run.sh run || true\n"
        "./scenarios/gnarly-race-pack/run.sh run || true\n"
        "./scenarios/gnarly-race-pack/run.sh view\n"
        "./scenarios/ready-with-definitions/run.sh status\n"
        "./scenarios/ready-with-definitions/run.sh run\n"
        "./scenarios/wrong-theorem/run.sh run || true\n"
        "```\n\n"
        "`run.sh view` is the fake replay seat: it prints the scenario contract,\n"
        "source/dependency files, latest receipt/cards, failure JSON, raw fake-HOL\n"
        "transcript tail, and gated context tail. Use it after `run` when reviewing\n"
        "longer fake race behavior or a failure report.\n\n"
        "Override `WORKBENCH_ROOT`, `FAKE_HOL_LAB_HOLDIR`, or\n"
        "`FAKE_HOL_LAB_RUN_ROOT` if you move this lab. The wrappers intentionally\n"
        "ignore ambient `HOLDIR`, and keep fake runtime controls off the public\n"
        "Linux warm-profile route. The generated fake HOL checkout is:\n\n"
        "```text\n"
        f"{manifest['holdir']}\n"
        "```\n\n"
        "## Scenarios\n\n"
        "| scenario | theorem | expected | purpose |\n"
        "| --- | --- | --- | --- |\n"
        f"{table}\n"
    )


def shell_join(argv: list[str]) -> str:
    return " ".join(shlex.quote(item) for item in argv)


def self_test() -> None:
    tutorial = transcript_for_scenario("tutorial-basic", config=FakeHolConfig(startup=False))
    assert "val TAUT_SPEC : thm =" in tutorial
    assert "proved ARITH_SPEC" in tutorial
    assert "proved TINY_SPEC" in tutorial
    assert "Exception:" not in tutorial

    roleplay = transcript_for_scenario(
        "roleplay-mixed",
        config=FakeHolConfig(
            roleplay_profile="realistic",
            random_seed=7,
            emit_timing=True,
            emit_goalstack=True,
        ),
    )
    assert "1 subgoal" in roleplay
    assert "Running time:" in roleplay
    assert "proved TINY_SPEC" in roleplay

    stats = transcript_for_scenario("tiny-success", config=FakeHolConfig(startup=False, stats=True))
    assert "__FAKE_HOL_STATS__:phrases=1 uses=0 theorems=1 elapsed_ms=" in stats

    multi = transcript_for_scenario("multi-claim-success", config=FakeHolConfig(startup=False))
    assert "proved MULTI_ONE" in multi
    assert "proved MULTI_TWO" in multi
    assert "proved MULTI_THREE" in multi

    deps = transcript_for_scenario("dependency-noise", config=FakeHolConfig(startup=False))
    assert "proved DEP_HELPER" in deps
    assert "proved TARGET_AFTER_DEPS" in deps

    multiline = transcript_for_scenario("multiline-theorem", config=FakeHolConfig(startup=False))
    assert "val MULTILINE_SPEC : thm =" in multiline
    assert "|- !a b c. a = b /\\ b = c ==> a = c" in multiline or "\n   " in multiline

    type_drift = transcript_for_scenario("type-drift-after-prefix", config=FakeHolConfig(startup=False))
    assert "proved PREFIX_OK" in type_drift
    assert "under-annotated real arguments" in type_drift
    assert "proved DRIFT_SPEC" not in type_drift

    ready = transcript_for_scenario("ready-with-definitions", config=FakeHolConfig(startup=False))
    assert "proved READY_WARNING_SPEC" in ready

    catalog = scenario_catalog()
    assert catalog and {item["name"] for item in catalog} == set(SCENARIOS)

    with tempfile.TemporaryDirectory(prefix="fake-hol-route-self-test-") as tmp:
        lab_root = Path(tmp) / "lab"
        manifest = materialize_lab(lab_root, suite_names("all"))
        scenario_rows = manifest.get("scenarios")
        assert isinstance(scenario_rows, list)
        typed_rows = cast(list[object], scenario_rows)
        assert len(typed_rows) == len(SCENARIOS)
        for raw_row in typed_rows:
            assert isinstance(raw_row, dict)
            row = cast(dict[str, object], raw_row)
            name = row.get("name")
            assert isinstance(name, str) and name in SCENARIOS
            for command_name in ("status_command", "run_command"):
                command = row.get(command_name)
                assert isinstance(command, str)
                assert "/bin/proof-run" in command
                assert "/bin/prove" not in command
                assert "--cwd" not in command
            run_script = (lab_root / "scenarios" / name / "run.sh").read_text(encoding="utf-8")
            assert "/bin/proof-run" in run_script
            assert "/bin/prove" not in run_script
            assert "--cwd" not in run_script


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Write fake HOL scenario sources or transcripts.")
    parser.add_argument("scenario", nargs="?", help="Scenario name")
    parser.add_argument("--list", action="store_true", help="List scenario names")
    parser.add_argument("--long", action="store_true", help="With --list, include one-line descriptions")
    parser.add_argument("--json", action="store_true", help="With --list, emit a JSON scenario catalog")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic in-process scenario checks")
    parser.add_argument("--suite", default="vm-feedback", help="Scenario suite for --materialize: vm-feedback or all")
    parser.add_argument("--materialize", help="Create a runnable fake-HOL feedback lab under this directory")
    parser.add_argument("--workbench-root", help="Hearth root for generated run scripts; defaults to this checkout")
    parser.add_argument("--standalone-fake-hol", action="store_true", help="Copy fake_hol into generated fake HOLDIR")
    parser.add_argument("--out", help="Write scenario source to this path")
    parser.add_argument("--transcript", help="Write fake HOL transcript to this path")
    parser.add_argument("--noise", type=int, default=0, help="Fake HOL noise level for transcript output")
    parser.add_argument("--profile", default="none", help="Roleplay profile: none, light, realistic, noisy, unstable")
    parser.add_argument("--seed", default="0", help="Deterministic seed, or 'random'")
    parser.add_argument("--failure-rate", type=float, default=0.0, help="Seeded per-theorem roleplay failure rate")
    parser.add_argument("--timing", action="store_true", help="Emit fake running-time lines in transcripts")
    parser.add_argument("--goalstack", action="store_true", help="Emit goalstack-ish output in transcripts")
    args = parser.parse_args(argv)

    if args.list:
        if args.json:
            print(json.dumps({"scenarios": scenario_catalog()}, indent=2, sort_keys=True))
        elif args.long:
            for item in scenario_catalog():
                print(f"{item['name']}: {item['description']}")
        else:
            for name in sorted(SCENARIOS):
                print(name)
        return 0
    if args.self_test:
        self_test()
        print("fake HOL scenario self-test passed")
        return 0
    if args.materialize:
        names = [args.scenario] if args.scenario else suite_names(args.suite)
        manifest = materialize_lab(
            Path(args.materialize),
            names,
            workbench_root=Path(args.workbench_root) if args.workbench_root else None,
            standalone_fake_hol=args.standalone_fake_hol,
        )
        print(manifest["root"])
        print(f"fake HOL feedback lab scenarios: {manifest['scenario_count']}")
        print(f"read: {Path(str(manifest['root'])) / 'README.md'}")
        return 0
    if not args.scenario:
        parser.error("missing scenario name")
    text = scenario_text(args.scenario)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    if args.transcript:
        transcript = transcript_for_scenario(
            args.scenario,
            config=FakeHolConfig(
                noise_level=args.noise,
                roleplay_profile=args.profile,
                random_seed=parse_seed(args.seed),
                failure_rate=args.failure_rate,
                emit_timing=args.timing,
                emit_goalstack=args.goalstack,
            ),
        )
        Path(args.transcript).write_text(transcript, encoding="utf-8")
    if not args.out and not args.transcript:
        print(text, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
