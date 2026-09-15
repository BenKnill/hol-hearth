from __future__ import annotations

import argparse
import json
import os
import random
import re
import signal
import tempfile
import textwrap
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

THEOREM_PATTERNS = [
    re.compile(r"\blet\s+([A-Za-z][A-Za-z0-9_']*)\s*=\s*(?:time\s+)?prove\s*\(\s*`", re.S),
    re.compile(
        r"\blet\s+([A-Za-z][A-Za-z0-9_']*)\s*=\s*(ARITH_RULE|INT_ARITH|TAUT|REAL_ARITH|REAL_RING|RING_RULE)\s*`", re.S
    ),
]
DIRECTIVE_RE = re.compile(r"\(\*\s*FAKE_HOL:\s*(.*?)\s*\*\)", re.S)
USE_RE = re.compile(r'(?:^|\n)\s*(?:#use|loadt)\s+("(?:\\.|[^"\\])*")\s*$', re.S)
NEEDS_RE = re.compile(r'(?:^|\n)\s*needs\s+("(?:\\.|[^"\\])*")\s*$', re.S)
EXPECTED_TERM_RE = re.compile(r"\blet\s+harness_expected_term\s*=\s*`", re.S)
CLAIM_THM_RE = re.compile(r"\blet\s+harness_claim_thm\s*=\s*([A-Za-z][A-Za-z0-9_']*)\s*$", re.S)
PROBE_CALL_RE = re.compile(r"\bharness_probe_claim\s+(\"(?:\\.|[^\"\\])*\")\s*`", re.S)
DOCTOR_MARKER_RE = re.compile(r"__HOL_DOCTOR_OK__:([A-Za-z][A-Za-z0-9_']*)")
CLAIM_OBSERVED_RE = re.compile(r"__HOL_CLAIM_OBSERVED__:([A-Za-z][A-Za-z0-9_']*)")
PRINT_ENDLINE_RE = re.compile(r"\bprint_endline\s+(\"(?:\\.|[^\"\\])*\")", re.S)
CHECKED_SOURCE_LOAD_RE = re.compile(
    r'\bif\s+!\(Hol_loader\.file_loader\)\s+("(?:\\.|[^"\\])*")',
    re.S,
)
BINDING_STATEMENT_RE = re.compile(
    r"__HOL_BINDING_STATEMENT_BEGIN__:([A-Za-z_][A-Za-z0-9_']*).*?"
    r"\bconcl\s+([A-Za-z_][A-Za-z0-9_']*).*?"
    r"__HOL_BINDING_STATEMENT_END__:\1",
    re.S,
)
FORK_OPEN_OUT_RE = re.compile(r"\bopen_out_bin\s+(\"(?:\\.|[^\"\\])*\")", re.S)
FORK_LOADT_RE = re.compile(r"\bloadt\s+(\"(?:\\.|[^\"\\])*\")", re.S)
FORK_DONE_RE = re.compile(r"\bprint_endline\s+(\"__PROOF_RUN_FORK_CHILD_DONE__:(?:\\.|[^\"\\])*\")", re.S)
FORK_SPAWN_RE = re.compile(r'Printf\.printf\s+"%s%d\\n"\s+("(?:\\.|[^\"\\])*")\s+child', re.S)
TYPEVAR_TOKEN_RE = re.compile(r"(?:(?<=:)|(?<=->)|(?<=#))([A-Z][A-Za-z0-9_']*)")
ROLEPLAY_PROFILES = {"none", "off", "light", "realistic", "noisy", "unstable"}
VIRTUAL_HOL_LIBRARY_PREFIXES = (
    "Library/",
    "Multivariate/",
    "Examples/",
    "Formal_ineqs/",
    "Help/",
    "100/",
)
VIRTUAL_HOL_LIBRARY_FILES = {
    "calc_rat.ml",
}

STARTUP_ROLEPLAY_LINES = [
    "/fake/hol-light/_opam/lib/ocaml/compiler-libs: added to search path",
    "/fake/hol-light/_opam/lib/camlp5: added to search path",
    "/fake/hol-light/_opam/lib/camlp5/camlp5.cma: loaded",
    "Warning: inventing type variables",
]

FAILURE_ROLEPLAY_LINES = [
    'Exception: Failure "ASM_REWRITE_TAC".',
    'Exception: Failure "MATCH_MP_TAC".',
    'Exception: Failure "types do not agree".',
    "Error: Syntax error",
    'Fatal error: exception Failure("fake HOL roleplay crash")',
]


def parse_seed(value: str | None) -> int | None:
    if value is None or value == "":
        return 0
    if value.lower() in {"none", "random"}:
        return None
    return int(value)


def _empty_string_map() -> dict[str, str]:
    return {}


def _empty_float_map() -> dict[str, float]:
    return {}


def _empty_string_list() -> list[str]:
    return []


@dataclass
class FakeHolConfig:
    startup: bool = True
    echo_prompts: bool = False
    emit_proved_lines: bool = True
    emit_val_thm_lines: bool = True
    fail_on_token: dict[str, str] = field(default_factory=_empty_string_map)
    sleep_on_token: dict[str, float] = field(default_factory=_empty_float_map)
    exit_status: int = 0
    false_green: bool = False
    source_echo_failure_lines: bool = False
    noise_level: int = 0
    stats: bool = False
    color: bool = False
    roleplay_profile: str = "none"
    random_seed: int | None = 0
    failure_rate: float = 0.0
    emit_timing: bool = False
    emit_goalstack: bool = False
    multiline_theorem_output: bool = False

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> FakeHolConfig:
        if env is None:
            env = os.environ
        mode = env.get("FAKE_HOL_MODE", "success").strip().lower()
        profile = env.get("FAKE_HOL_PROFILE")
        if not profile and env.get("FAKE_HOL_ROLEPLAY", "0") == "1":
            profile = "realistic"
        profile = (profile or "none").strip().lower()
        config = cls(
            startup=env.get("FAKE_HOL_STARTUP", "1") != "0",
            echo_prompts=env.get("FAKE_HOL_PROMPTS", "0") == "1",
            emit_proved_lines=env.get("FAKE_HOL_PROVED_LINES", "1") != "0",
            emit_val_thm_lines=env.get("FAKE_HOL_VAL_LINES", "1") != "0",
            exit_status=int(env.get("FAKE_HOL_EXIT", "0")),
            false_green=env.get("FAKE_HOL_FALSE_GREEN", "0") == "1",
            source_echo_failure_lines=env.get("FAKE_HOL_SOURCE_ECHO", "0") == "1",
            noise_level=int(env.get("FAKE_HOL_NOISE", "0")),
            stats=env.get("FAKE_HOL_STATS", "0") == "1",
            color=env.get("FAKE_HOL_COLOR", "0") == "1",
            roleplay_profile=profile,
            random_seed=parse_seed(env.get("FAKE_HOL_SEED", "0")),
            failure_rate=float(env.get("FAKE_HOL_FAILURE_RATE", "0")),
            emit_timing=env.get("FAKE_HOL_TIMING", "0") == "1",
            emit_goalstack=env.get("FAKE_HOL_GOALSTACK", "0") == "1",
            multiline_theorem_output=env.get("FAKE_HOL_MULTILINE", "0") == "1"
            or env.get("FAKE_HOL_MULTILINE_THM_LINES", "0") == "1",
        )
        if mode == "quiet":
            config.startup = False
            config.emit_proved_lines = False
            config.emit_val_thm_lines = False
        elif mode in {"roleplay", "realistic", "noisy", "unstable"}:
            config.roleplay_profile = "realistic" if mode == "roleplay" else mode
            if mode == "noisy":
                config.noise_level = max(config.noise_level, 2)
            if mode == "unstable" and "FAKE_HOL_FAILURE_RATE" not in env:
                config.failure_rate = 0.25
        elif mode == "exception":
            config.fail_on_token["prove"] = env.get("FAKE_HOL_FAIL_MESSAGE", 'Exception: Failure "fake HOL exception".')
        elif mode == "false_green":
            config.false_green = True
            config.fail_on_token["prove"] = env.get(
                "FAKE_HOL_FAIL_MESSAGE", 'Exception: Failure "fake HOL false green".'
            )
        elif mode == "source_echo":
            config.source_echo_failure_lines = True
        elif mode == "fatal":
            config.fail_on_token["prove"] = env.get("FAKE_HOL_FAIL_MESSAGE", "Fatal error: fake HOL fatal error")
        elif mode == "error":
            config.fail_on_token["prove"] = env.get("FAKE_HOL_FAIL_MESSAGE", "Error: fake HOL error")
        elif mode == "hang":
            config.sleep_on_token["__FAKE_HOL_HANG__"] = float(env.get("FAKE_HOL_SLEEP_MS", "60000")) / 1000.0
        elif mode not in {"", "success"}:
            config.fail_on_token[mode] = env.get("FAKE_HOL_FAIL_MESSAGE", f'Exception: Failure "fake HOL mode {mode}".')
        fail_on = env.get("FAKE_HOL_FAIL_ON")
        if fail_on:
            message = env.get("FAKE_HOL_FAIL_MESSAGE", f'Exception: Failure "FAKE_HOL_FAIL_ON:{fail_on}".')
            config.fail_on_token[fail_on] = message
        sleep_ms = env.get("FAKE_HOL_SLEEP_MS")
        sleep_on = env.get("FAKE_HOL_SLEEP_ON")
        if sleep_ms and sleep_on:
            once = -1 if env.get("FAKE_HOL_SLEEP_ONCE", "0") == "1" else 1
            config.sleep_on_token[sleep_on] = once * float(sleep_ms) / 1000.0
        if config.roleplay_profile not in ROLEPLAY_PROFILES:
            config.roleplay_profile = "realistic"
        return config


@dataclass(frozen=True)
class FakeTheorem:
    name: str
    statement: str
    source: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class FakeHolResult:
    stdout: str
    exit_status: int
    theorems: dict[str, FakeTheorem]
    events: list[dict[str, Any]]


@dataclass(frozen=True)
class FakeHolScenario:
    text: str = ""
    config: FakeHolConfig = field(default_factory=FakeHolConfig)
    startup_noise: bool = False
    theorem_events: list[str] = field(default_factory=_empty_string_list)
    fail_at: str | None = None
    false_green: bool = False
    source_echo_failure_lines: bool = False

    def to_runtime(self) -> FakeHolRuntime:
        config = FakeHolConfig(
            startup=self.config.startup,
            echo_prompts=self.config.echo_prompts,
            emit_proved_lines=self.config.emit_proved_lines,
            emit_val_thm_lines=self.config.emit_val_thm_lines,
            fail_on_token=dict(self.config.fail_on_token),
            sleep_on_token=dict(self.config.sleep_on_token),
            exit_status=self.config.exit_status,
            false_green=self.config.false_green or self.false_green,
            source_echo_failure_lines=self.config.source_echo_failure_lines or self.source_echo_failure_lines,
            noise_level=max(self.config.noise_level, 1 if self.startup_noise else 0),
            stats=self.config.stats,
            color=self.config.color,
            roleplay_profile=self.config.roleplay_profile,
            random_seed=self.config.random_seed,
            failure_rate=self.config.failure_rate,
            emit_timing=self.config.emit_timing,
            emit_goalstack=self.config.emit_goalstack,
            multiline_theorem_output=self.config.multiline_theorem_output,
        )
        if self.fail_at:
            config.fail_on_token[self.fail_at] = f'Exception: Failure "fake {self.fail_at}".'
        runtime = FakeHolRuntime(config)
        for name in self.theorem_events:
            runtime.theorems[name] = FakeTheorem(name=name, statement="T")
        return runtime


class FakeHolRuntime:
    def __init__(self, config: FakeHolConfig | None = None, *, emit_callback: Callable[[str], None] | None = None):
        self.config = config or FakeHolConfig()
        self.random = random.Random(self.config.random_seed) if self.config.random_seed is not None else random.Random()
        self.emit_callback = emit_callback
        self.theorems: dict[str, FakeTheorem] = {}
        self.output: list[str] = []
        self.events: list[dict[str, Any]] = []
        self._harness_expected_statement: str | None = None
        self._harness_claim_name: str | None = None
        self._uses = 0
        self._phrases = 0
        self._started = time.monotonic()

    def run_text(self, text: str, *, cwd: Path | str) -> FakeHolResult:
        cwd = Path(cwd)
        if self.config.startup:
            self.emit_startup()
        self._run_text(text, cwd=cwd, source=None, start_line=1)
        if self.config.stats:
            elapsed_ms = int((time.monotonic() - self._started) * 1000)
            self.emit(
                f"__FAKE_HOL_STATS__:phrases={self._phrases} "
                f"uses={self._uses} theorems={len(self.theorems)} elapsed_ms={elapsed_ms}"
            )
        stdout = "\n".join(self.output)
        if stdout:
            stdout += "\n"
        return FakeHolResult(
            stdout=stdout,
            exit_status=self.config.exit_status,
            theorems=dict(self.theorems),
            events=list(self.events),
        )

    def run_fragment(
        self,
        text: str,
        *,
        cwd: Path | str,
        source: Path | None = None,
        start_line: int = 1,
    ) -> None:
        """Process one persistent-shell fragment without replaying startup."""

        self._run_text(text, cwd=Path(cwd), source=source, start_line=start_line)

    def emit(self, line: str) -> None:
        if self.config.echo_prompts and not line.startswith("# "):
            line = "# " + line
        self.output.append(line)
        if self.emit_callback is not None:
            self.emit_callback(line)

    def fork_child_runtime(self) -> FakeHolRuntime:
        child_config = replace(
            self.config,
            startup=False,
            stats=False,
            exit_status=0,
            fail_on_token=dict(self.config.fail_on_token),
            sleep_on_token=dict(self.config.sleep_on_token),
        )
        child = FakeHolRuntime(child_config)
        child.theorems = dict(self.theorems)
        return child

    def run_fake_fork_child(
        self,
        *,
        transcript: Path,
        wrapper: Path,
        done_token: str,
        ack_path: Path,
        ownership_path: Path,
    ) -> int:
        os.setsid()
        deadline = time.monotonic() + 5.0
        while not ack_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        if not ack_path.is_file():
            return 127
        ack_path.unlink(missing_ok=True)
        ownership_path.write_text("owned", encoding="utf-8")
        deadline = time.monotonic() + 5.0
        while ownership_path.is_file() and time.monotonic() < deadline:
            time.sleep(0.01)
        if ownership_path.is_file():
            return 126

        child = self.fork_child_runtime()
        transcript.parent.mkdir(parents=True, exist_ok=True)
        transcript.write_text(f"[fake-fork-worker] attempt begin wrapper={wrapper}\n", encoding="utf-8")

        def stream_child_line(line: str) -> None:
            with transcript.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

        child.emit_callback = stream_child_line
        try:
            wrapper_text = wrapper.read_text(encoding="utf-8")
        except OSError as exc:
            child.emit(f'Exception: Sys_error "{wrapper}: {exc.strerror}".')
            child.config.exit_status = 1
        else:
            child._run_text(wrapper_text, cwd=wrapper.parent, source=wrapper, start_line=1)

        child_failed = child.config.exit_status != 0 or any(
            "Exception:" in item or "Error:" in item or "Fatal error:" in item for item in child.output
        )
        with transcript.open("a", encoding="utf-8") as handle:
            if not child_failed:
                handle.write(done_token + "\n")
            handle.write("[fake-fork-worker] attempt end\n")
        return 1 if child_failed else 0

    def handle_fake_fork_worker_phrase(self, phrase: str, *, cwd: Path, source: Path | None, line: int | None) -> bool:
        if "Unix.fork" not in phrase or "__PROOF_RUN_FORK_SPAWNED__" not in phrase:
            return False
        transcript_matches = FORK_OPEN_OUT_RE.findall(phrase)
        load_match = FORK_LOADT_RE.search(phrase)
        done_match = FORK_DONE_RE.search(phrase)
        spawn_match = FORK_SPAWN_RE.search(phrase)
        if not (transcript_matches and load_match and done_match and spawn_match):
            return False
        try:
            transcript = resolve_use_path(json.loads(transcript_matches[-1]), cwd=cwd)
            wrapper = resolve_use_path(json.loads(load_match.group(1)), cwd=cwd)
            done_token = json.loads(done_match.group(1))
            spawn_token = json.loads(spawn_match.group(1))
        except json.JSONDecodeError:
            return False

        ack_path = transcript.with_name(f"{transcript.stem}.registration-ack.json")
        ownership_path = transcript.with_name(f"{transcript.stem}.ownership-ready")
        signal.signal(signal.SIGCHLD, signal.SIG_IGN)
        child_pid = os.fork()
        if child_pid == 0:
            signal.signal(signal.SIGCHLD, signal.SIG_DFL)
            exit_status = self.run_fake_fork_child(
                transcript=transcript,
                wrapper=wrapper,
                done_token=done_token,
                ack_path=ack_path,
                ownership_path=ownership_path,
            )
            os._exit(exit_status)
        self.emit(f"{spawn_token}{child_pid}")
        self.events.append(
            {
                "kind": "fake-fork-child",
                "pid": child_pid,
                "wrapper": str(wrapper),
                "transcript": str(transcript),
                "source": str(source) if source else None,
                "line": line,
                "status": "spawned",
            }
        )
        return True

    def emit_startup(self) -> None:
        self.emit("OCaml version 5.4.0")
        self.emit("Enter #help;; for help.")
        self.emit("")
        self.emit("* HOL-Light syntax in effect *")
        self.emit("Camlp5 parsing version (HOL-Light) 8.03.01")
        if self.roleplay_enabled():
            for line in self.random.sample(STARTUP_ROLEPLAY_LINES, k=self.roleplay_startup_count()):
                self.emit(line)
        if self.config.noise_level:
            self.emit("Warning: inventing type variables")
            self.emit("Searching with limit 0")
            self.emit("Searching with limit 1")

    def _run_text(self, text: str, *, cwd: Path, source: Path | None, start_line: int) -> None:
        for phrase, line in split_ocaml_phrases(text, start_line=start_line):
            stripped = phrase.strip()
            if not stripped:
                continue
            self._phrases += 1
            checked_source_path = parse_checked_source_load(stripped)
            if checked_source_path is not None:
                output_start = len(self.output)
                loaded = self.load_source_file(
                    checked_source_path,
                    cwd=cwd,
                    source=source,
                    line=line,
                    kind="checked-source-load",
                )
                new_output = self.output[output_start:]
                failed = any(
                    item.startswith(("Exception:", "Error:", "Fatal error:", "Failure:")) for item in new_output
                )
                if loaded and not failed:
                    self.emit("__PROOF_RUN_SOURCE_LOAD_COMPLETED__")
                continue
            use_path = parse_use_directive(stripped)
            if use_path is not None:
                self.load_source_file(use_path, cwd=cwd, source=source, line=line, kind="use")
                continue
            need_path = parse_needs_directive(stripped)
            if need_path is not None:
                self.handle_needs_directive(need_path, cwd=cwd, source=source, line=line)
                continue
            self.run_phrase(stripped, cwd=cwd, source=source, line=line)

    def load_source_file(
        self,
        path: str,
        *,
        cwd: Path,
        source: Path | None,
        line: int | None,
        kind: str,
    ) -> bool:
        resolved = resolve_use_path(path, cwd=cwd)
        self._uses += 1
        self.events.append(
            {"kind": kind, "path": str(resolved), "source": str(source) if source else None, "line": line}
        )
        try:
            nested = resolved.read_text(encoding="utf-8")
        except OSError as exc:
            self.emit(f'Exception: Sys_error "{resolved}: {exc.strerror}".')
            if not self.config.false_green:
                self.config.exit_status = 1
            return False
        self._run_text(nested, cwd=resolved.parent, source=resolved, start_line=1)
        return True

    def handle_needs_directive(
        self,
        path: str,
        *,
        cwd: Path,
        source: Path | None,
        line: int | None,
    ) -> None:
        if should_virtualize_hol_library_need(path):
            self.events.append(
                {
                    "kind": "virtual-needs",
                    "path": path,
                    "source": str(source) if source else None,
                    "line": line,
                }
            )
            if self.config.noise_level or self.roleplay_enabled():
                self.emit(f'Loading "{path}"')
            return
        self.load_source_file(path, cwd=cwd, source=source, line=line, kind="needs")

    def run_phrase(self, phrase: str, *, cwd: Path, source: Path | None = None, line: int | None = None) -> None:
        directive_count = self._apply_directives(phrase)
        if directive_count and not mask_ocaml_comments_and_strings_except_backquotes(phrase).strip():
            return

        if self.config.source_echo_failure_lines and "with Failure" in phrase:
            self.emit("| Failure _ -> ()")
            self.events.append({"kind": "source-echo", "line": line, "source": str(source) if source else None})
            return
        for token, seconds in list(self.config.sleep_on_token.items()):
            if token in phrase:
                time.sleep(abs(seconds))
                if seconds < 0:
                    self.config.sleep_on_token.pop(token, None)
        for token, message in self.config.fail_on_token.items():
            if token in phrase:
                self.emit(message)
                self.events.append({"kind": "configured-failure", "token": token, "line": line})
                if not self.config.false_green:
                    self.config.exit_status = 1
                return

        if self.should_roleplay_fail(phrase):
            message = self.random.choice(FAILURE_ROLEPLAY_LINES)
            self.emit_roleplay_failure(message)
            return

        expected = parse_expected_assignment(phrase)
        if expected is not None:
            self._harness_expected_statement = expected
            self.events.append({"kind": "harness-expected-term", "statement": expected})
            self.emit_noise_for_phrase(phrase)
            return

        claim_name = parse_claim_assignment(phrase)
        if claim_name is not None:
            self._harness_claim_name = claim_name
            self.events.append({"kind": "harness-claim-thm", "name": claim_name})
            self.emit_noise_for_phrase(phrase)
            return

        probe_call = parse_probe_call(phrase)
        if probe_call is not None:
            self.handle_probe_call(*probe_call)
            return

        if "__HOL_DOCTOR_OK__:" in phrase:
            self.handle_doctor_probe(phrase)
            return

        # The harness shadows TAC_PROOF/prove with a branch that prints
        # unsolved-goal sentinels only when a tactic leaves subgoals. The fake
        # runtime is not an OCaml evaluator; without this guard its simple
        # print_endline recognizer would emit that diagnostic branch eagerly.
        if "__HOL_PROOF_FRONTIER_BEGIN__" in phrase and "harness_print_frontier" in phrase:
            self.emit_noise_for_phrase(phrase)
            return

        if self.handle_fake_fork_worker_phrase(phrase, cwd=cwd, source=source, line=line):
            return

        if CLAIM_OBSERVED_RE.search(phrase):
            self.handle_semantic_probe(phrase)
            return

        binding_statement = BINDING_STATEMENT_RE.search(phrase)
        if binding_statement:
            requested, binding = binding_statement.groups()
            theorem = self.theorems.get(binding)
            if requested != binding or theorem is None:
                self.emit(f'Exception: Failure "__HOL_BINDING_STATEMENT_UNBOUND__:{requested}".')
                self.events.append({"kind": "binding-statement", "status": "unbound", "name": requested})
                if not self.config.false_green:
                    self.config.exit_status = 1
                return
            self.emit(f"__HOL_BINDING_STATEMENT_BEGIN__:{requested}")
            self.emit(theorem.statement)
            self.emit(f"__HOL_BINDING_STATEMENT_END__:{requested}")
            self.events.append({"kind": "binding-statement", "status": "observed", "name": requested})
            return

        theorem = parse_theorem_definition(phrase, source=str(source) if source else None, line=line)
        if theorem:
            self.theorems[theorem.name] = theorem
            self.events.append(
                {
                    "kind": "theorem",
                    "name": theorem.name,
                    "statement": theorem.statement,
                    "source": theorem.source,
                    "line": theorem.line,
                }
            )
            self.emit_theorem(theorem)
            return

        print_match = PRINT_ENDLINE_RE.search(phrase)
        if print_match:
            try:
                self.emit(json.loads(print_match.group(1)))
            except json.JSONDecodeError:
                self.emit(print_match.group(1).strip('"'))
            return

        self.emit_noise_for_phrase(phrase)

    def emit_theorem(self, theorem: FakeTheorem) -> None:
        if self.config.noise_level or self.roleplay_enabled():
            self.emit_theorem_noise(theorem)
        statement = theorem.statement
        if self.config.color:
            statement = statement.replace("forall", "\x1b[35mforall\x1b[0m").replace("SUC", "\x1b[32mSUC\x1b[0m")
        if self.config.emit_val_thm_lines:
            if self.config.multiline_theorem_output:
                self.emit(f"val {theorem.name} : thm =")
                for index, line in enumerate(wrap_theorem_statement(statement)):
                    prefix = "|- " if index == 0 else "   "
                    self.emit(prefix + line)
            elif self.roleplay_enabled() and self.random.random() < 0.35:
                self.emit(f"val {theorem.name} : thm = |- {statement}")
            else:
                self.emit(f"val {theorem.name} : thm =")
                self.emit(f"|- {statement}")
        if self.config.emit_proved_lines:
            self.emit(f"proved {theorem.name}")
        if self.config.emit_timing or self.config.roleplay_profile in {"realistic", "noisy", "unstable"}:
            self.emit_timing_line()

    def emit_noise_for_phrase(self, phrase: str) -> None:
        if self.config.noise_level <= 0 and not self.roleplay_enabled():
            return
        if self.config.noise_level or self.random.random() < 0.2:
            self.emit("Warning: Benign redefinition")
        if self.config.noise_level >= 2:
            self.emit("3 basis elements and 0 critical pairs")
            self.emit("Generating HOL version of proof")
        if self.config.noise_level >= 3 or self.config.roleplay_profile == "noisy":
            self.emit_symbolic_steps(5 if self.config.noise_level < 3 else 8)

    def emit_theorem_noise(self, theorem: FakeTheorem) -> None:
        if self.config.emit_goalstack or (
            self.config.roleplay_profile in {"realistic", "noisy", "unstable"} and self.random.random() < 0.25
        ):
            self.emit_goalstack(theorem)
        search_count = 2
        if self.config.roleplay_profile == "noisy":
            search_count = self.random.randint(3, 8)
        elif self.roleplay_enabled():
            search_count = self.random.randint(0, 4)
        for i in range(search_count):
            self.emit(f"Searching with limit {i}")
        if search_count:
            solved_at = 2 + search_count * self.random.randint(2, 9)
            progress = "0.." + "..".join(str(i) for i in range(min(search_count, 5)))
            self.emit(f"{progress}..solved at {solved_at}")
        if self.config.noise_level >= 2 or self.config.roleplay_profile in {"noisy", "unstable"}:
            basis = self.random.randint(2, 7)
            critical = self.random.randint(0, 5)
            self.emit(f"{basis} basis elements and {critical} critical pairs")
            if self.random.random() < 0.65:
                self.emit("Generating HOL version of proof")

    def emit_goalstack(self, theorem: FakeTheorem) -> None:
        statement = theorem.statement or "T"
        self.emit("1 subgoal")
        self.emit("")
        self.emit(f"`{statement}`")

    def emit_symbolic_steps(self, count: int) -> None:
        for i in range(count):
            self.emit(f"Stepping to state s{i + 1}")

    def emit_timing_line(self) -> None:
        user_time = self.random.uniform(0.001, 0.25)
        start = self.random.uniform(1.0, 10.0)
        end = start + user_time
        self.emit(f"Running time: {user_time:.6f} sec, Start unixtime: {start:.6f}, End unixtime: {end:.6f}")

    def roleplay_enabled(self) -> bool:
        return self.config.roleplay_profile not in {"", "none", "off"}

    def roleplay_startup_count(self) -> int:
        if self.config.roleplay_profile == "light":
            return 1
        if self.config.roleplay_profile == "noisy":
            return min(len(STARTUP_ROLEPLAY_LINES), 4)
        return 3

    def should_roleplay_fail(self, phrase: str) -> bool:
        if self.config.failure_rate <= 0:
            return False
        if parse_theorem_definition(phrase) is None:
            return False
        return self.random.random() < self.config.failure_rate

    def emit_roleplay_failure(self, message: str, *, exit_status: int = 1) -> None:
        self.emit(message)
        self.events.append({"kind": "roleplay-failure", "message": message})
        if not self.config.false_green:
            self.config.exit_status = exit_status

    def handle_doctor_probe(self, phrase: str) -> None:
        match = DOCTOR_MARKER_RE.search(phrase)
        name = match.group(1) if match else self._harness_claim_name
        theorem = self.theorems.get(name or "")
        if theorem and statements_match(theorem.statement, "T"):
            self.emit(f"__HOL_DOCTOR_OK__:{name}")
            self.events.append({"kind": "doctor", "status": "observed", "name": name})
            return
        self.emit(f'Exception: Failure "__HOL_DOCTOR_CLAIM_MISMATCH__:{name or "UNKNOWN"}".')
        self.events.append({"kind": "doctor", "status": "mismatch", "name": name})
        if not self.config.false_green:
            self.config.exit_status = 1

    def handle_semantic_probe(self, phrase: str) -> None:
        marker_match = CLAIM_OBSERVED_RE.search(phrase)
        marker_name = marker_match.group(1) if marker_match else None
        theorem_name = self._harness_claim_name or marker_name
        expected = self._harness_expected_statement
        if not theorem_name:
            self.emit('Exception: Failure "__HOL_CLAIM_UNBOUND__:UNKNOWN".')
            self.events.append({"kind": "claim-probe", "status": "unbound", "name": None})
            if not self.config.false_green:
                self.config.exit_status = 1
            return
        theorem = self.theorems.get(theorem_name)
        if theorem is None:
            self.emit(f'Exception: Failure "__HOL_CLAIM_UNBOUND__:{theorem_name}".')
            self.events.append({"kind": "claim-probe", "status": "unbound", "name": theorem_name})
            if not self.config.false_green:
                self.config.exit_status = 1
            return
        if expected is not None and statements_match(theorem.statement, expected):
            self.emit(f"__HOL_CLAIM_OBSERVED__:{theorem_name}")
            self.events.append({"kind": "claim-probe", "status": "observed", "name": theorem_name})
            return
        self.emit(f'Exception: Failure "__HOL_CLAIM_MISMATCH__:{theorem_name}".')
        self.emit(f"__HOL_CLAIM_MISMATCH__:{theorem_name}")
        self.events.append(
            {
                "kind": "claim-probe",
                "status": "mismatch",
                "name": theorem_name,
                "expected": expected,
                "actual": theorem.statement,
            }
        )
        if not self.config.false_green:
            self.config.exit_status = 1

    def handle_probe_call(self, name: str, expected: str, theorem_name: str) -> None:
        theorem = self.theorems.get(theorem_name)
        if theorem is None:
            self.emit(f'Exception: Failure "__HOL_CLAIM_UNBOUND__:{theorem_name}".')
            self.events.append({"kind": "claim-probe", "status": "unbound", "name": theorem_name})
            if not self.config.false_green:
                self.config.exit_status = 1
            return
        if statements_match(theorem.statement, expected):
            self.emit(f"__HOL_CLAIM_OBSERVED__:{name}")
            self.events.append({"kind": "claim-probe", "status": "observed", "name": name})
            return
        self.emit(f'Exception: Failure "__HOL_CLAIM_MISMATCH__:{name}".')
        self.emit(f"__HOL_CLAIM_MISMATCH__:{name}")
        self.events.append(
            {
                "kind": "claim-probe",
                "status": "mismatch",
                "name": name,
                "expected": expected,
                "actual": theorem.statement,
            }
        )
        if not self.config.false_green:
            self.config.exit_status = 1

    def _apply_directives(self, text: str) -> int:
        count = 0
        for raw in DIRECTIVE_RE.findall(text):
            count += 1
            directive = " ".join(raw.split())
            if not directive:
                continue
            self.events.append({"kind": "directive", "directive": directive})
            parts = directive.split()
            cmd = parts[0]
            rest = parts[1:]
            if cmd == "fail":
                self.emit(" ".join(rest) if rest else 'Exception: Failure "fake directive".')
                if not self.config.false_green:
                    self.config.exit_status = 1
            elif cmd == "fail-on" and rest:
                token = rest[0]
                message = " ".join(rest[1:]) if len(rest) > 1 else f'Exception: Failure "FAKE_HOL_FAIL_ON:{token}".'
                self.config.fail_on_token[token] = message
            elif cmd == "parse-error":
                self.emit("Error: Syntax error")
                if not self.config.false_green:
                    self.config.exit_status = 2
            elif cmd == "type-error":
                self.emit('Exception: Failure "types do not agree".')
                if not self.config.false_green:
                    self.config.exit_status = 1
            elif cmd == "tactic-fail":
                tactic = rest[0] if rest else "ASM_REWRITE_TAC"
                self.emit(f'Exception: Failure "{tactic}".')
                if not self.config.false_green:
                    self.config.exit_status = 1
            elif cmd == "fatal":
                self.emit(" ".join(rest) if rest else 'Fatal error: exception Failure("fake fatal error")')
                if not self.config.false_green:
                    self.config.exit_status = 2
            elif cmd == "crash":
                self.emit('Fatal error: exception Failure("fake crash")')
                if not self.config.false_green:
                    self.config.exit_status = int(rest[0]) if rest else 2
            elif cmd == "partial-crash":
                self.emit("Searching with limit 0")
                self.emit("val PARTIAL_HELPER : thm =")
                self.emit("|- T")
                self.emit('Fatal error: exception Failure("fake partial crash")')
                if not self.config.false_green:
                    self.config.exit_status = int(rest[0]) if rest else 2
            elif cmd == "false-green":
                self.emit(" ".join(rest) if rest else 'Exception: Failure "fake false green".')
                self.config.exit_status = 0
            elif cmd == "noise":
                self.apply_noise_directive(rest)
            elif cmd == "goalstack":
                count = int(rest[0]) if rest else 1
                for i in range(count):
                    self.emit("1 subgoal")
                    self.emit("")
                    self.emit(f"`fake goal {i + 1}`")
            elif cmd == "warning":
                self.emit(" ".join(rest) if rest else "Warning: inventing type variables")
            elif cmd == "timing":
                self.emit_timing_line()
            elif cmd == "color":
                value = rest[0].lower() if rest else "on"
                self.config.color = value not in {"0", "off", "false", "no"}
            elif cmd == "multiline":
                value = rest[0].lower() if rest else "on"
                self.config.multiline_theorem_output = value not in {"0", "off", "false", "no"}
            elif cmd == "exit" and rest:
                self.config.exit_status = int(rest[0])
            elif (cmd == "sleep" and rest) or (cmd == "hang" and rest):
                delay = rest[0]
                seconds = float(delay[:-2]) / 1000.0 if delay.endswith("ms") else float(delay)
                time.sleep(seconds)
        return count

    def apply_noise_directive(self, rest: list[str]) -> None:
        kind = rest[0] if rest else "symbolic"
        count = int(rest[-1]) if rest and rest[-1].isdigit() else 10
        if kind == "search":
            for i in range(count):
                self.emit(f"Searching with limit {i % 7}")
        elif kind == "gbasis":
            for i in range(count):
                self.emit(f"{2 + i % 5} basis elements and {i % 6} critical pairs")
        elif kind == "bdd":
            for i in range(count):
                self.emit(f"BDD with {3 + i} variables, {7 + i * 2} nodes and {11 + i} cached results")
        elif kind == "mixed":
            for i in range(count):
                if i % 4 == 0:
                    self.emit(f"Searching with limit {i % 7}")
                elif i % 4 == 1:
                    self.emit("Warning: inventing type variables")
                elif i % 4 == 2:
                    self.emit(f"{3 + i % 4} basis elements and {i % 3} critical pairs")
                else:
                    self.emit(f"Stepping to state s{i + 1}")
        else:
            self.emit_symbolic_steps(count)


def generate_transcript(text: str, *, cwd: Path | str, config: FakeHolConfig | None = None) -> str:
    return FakeHolRuntime(config).run_text(text, cwd=Path(cwd)).stdout


def wrap_theorem_statement(statement: str, *, width: int = 72) -> list[str]:
    wrapped = textwrap.wrap(statement, width=width, break_long_words=False, break_on_hyphens=False)
    return wrapped or [""]


def self_test() -> None:
    basic_source = """
let PROVE_SPEC = prove(`!x. x = x`,MESON_TAC[]);;
let TIMED_SPEC = time prove(`T`,TRUTH_TAC);;
let ARITH_SPEC = ARITH_RULE `0 < x ==> 0 <= x`;;
let TAUT_SPEC = TAUT `p \\/ ~p`;;
"""
    basic = FakeHolRuntime(FakeHolConfig(startup=False)).run_text(basic_source, cwd=Path.cwd())
    assert basic.exit_status == 0, basic.stdout
    assert set(basic.theorems) == {"PROVE_SPEC", "TIMED_SPEC", "ARITH_SPEC", "TAUT_SPEC"}
    assert basic.theorems["PROVE_SPEC"].statement == "!x. x = x"
    assert "proved TAUT_SPEC" in basic.stdout

    probe_source = """
let TINY_SPEC = prove(`!x. x = x`,MESON_TAC[]);;
let harness_expected_term = `!x. x = x`;;
let harness_claim_thm = TINY_SPEC;;
let _ = print_endline "__HOL_CLAIM_OBSERVED__:TINY_SPEC";;
"""
    observed = FakeHolRuntime(FakeHolConfig(startup=False)).run_text(probe_source, cwd=Path.cwd())
    assert observed.exit_status == 0, observed.stdout
    assert "__HOL_CLAIM_OBSERVED__:TINY_SPEC" in observed.stdout

    binding_statement = FakeHolRuntime(FakeHolConfig(startup=False)).run_text(
        """
let DERIVED = prove(`!x. x = x`,MESON_TAC[]);;
let _ =
  print_endline "__HOL_BINDING_STATEMENT_BEGIN__:DERIVED";
  print_term (concl DERIVED); print_newline ();
  print_endline "__HOL_BINDING_STATEMENT_END__:DERIVED";;
""",
        cwd=Path.cwd(),
    )
    assert "__HOL_BINDING_STATEMENT_BEGIN__:DERIVED\n!x. x = x\n" in binding_statement.stdout
    assert "__HOL_BINDING_STATEMENT_END__:DERIVED" in binding_statement.stdout

    mismatch = FakeHolRuntime(FakeHolConfig(startup=False)).run_text(
        probe_source.replace("let harness_expected_term = `!x. x = x`", "let harness_expected_term = `F`"),
        cwd=Path.cwd(),
    )
    assert mismatch.exit_status == 1, mismatch.stdout
    assert "__HOL_CLAIM_MISMATCH__:TINY_SPEC" in mismatch.stdout

    with tempfile.TemporaryDirectory(prefix="fake-hol-runtime-self-test-") as tmp:
        tmp_path = Path(tmp)
        source_dir = tmp_path / "source dir"
        source_dir.mkdir()
        source = source_dir / 'attempt "quoted".ml'
        source.write_text("let USE_SPEC = prove(`!x:bool. x <=> x`,MESON_TAC[]);;\n", encoding="utf-8")
        used = FakeHolRuntime(FakeHolConfig(startup=False)).run_text(
            f"#use {json.dumps(str(source))};;\n",
            cwd=tmp_path,
        )
        assert used.exit_status == 0, used.stdout
        assert used.theorems["USE_SPEC"].source == str(source.resolve())

    roleplay_source = """
let HELPER_SPEC = prove(`T`,MESON_TAC[]);;
let TINY_SPEC = prove(`!x. x = x`,MESON_TAC[]);;
"""
    roleplay_config = FakeHolConfig(
        startup=True,
        roleplay_profile="noisy",
        random_seed=7,
        emit_timing=True,
        emit_goalstack=True,
    )
    first = FakeHolRuntime(roleplay_config).run_text(roleplay_source, cwd=Path.cwd())
    second = FakeHolRuntime(roleplay_config).run_text(roleplay_source, cwd=Path.cwd())
    assert first.stdout == second.stdout
    assert "1 subgoal" in first.stdout
    assert "Running time:" in first.stdout
    assert "Searching with limit" in first.stdout

    stats = FakeHolRuntime(FakeHolConfig(startup=False, stats=True)).run_text(
        "let STAT_SPEC = prove(`T`,MESON_TAC[]);;\n",
        cwd=Path.cwd(),
    )
    assert stats.exit_status == 0, stats.stdout
    assert "__FAKE_HOL_STATS__:phrases=1 uses=0 theorems=1 elapsed_ms=" in stats.stdout


def parse_theorem_definition(phrase: str, *, source: str | None = None, line: int | None = None) -> FakeTheorem | None:
    masked = mask_ocaml_comments_and_strings_except_backquotes(phrase)
    for pattern in THEOREM_PATTERNS:
        match = pattern.search(masked)
        if not match:
            continue
        name = match.group(1)
        backquote_pos = masked.find("`", match.end() - 1)
        if backquote_pos < 0:
            continue
        statement, _end = read_backquoted_term(phrase, backquote_pos)
        return FakeTheorem(name=name, statement=normalize_statement(statement), source=source, line=line)
    return None


def parse_expected_assignment(phrase: str) -> str | None:
    masked = mask_ocaml_comments_and_strings_except_backquotes(phrase)
    match = EXPECTED_TERM_RE.search(masked)
    if not match:
        return None
    backquote_pos = masked.find("`", match.end() - 1)
    if backquote_pos < 0:
        return None
    statement, _end = read_backquoted_term(phrase, backquote_pos)
    return normalize_statement(statement)


def parse_claim_assignment(phrase: str) -> str | None:
    masked = mask_ocaml_comments_and_strings_except_backquotes(phrase)
    match = CLAIM_THM_RE.search(masked.strip())
    return match.group(1) if match else None


def parse_probe_call(phrase: str) -> tuple[str, str, str] | None:
    masked = mask_ocaml_comments_and_strings_except_backquotes(phrase)
    match = PROBE_CALL_RE.search(phrase)
    if not match:
        return None
    try:
        name = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    backquote_pos = masked.find("`", match.end() - 1)
    if backquote_pos < 0:
        return None
    expected, term_end = read_backquoted_term(phrase, backquote_pos)
    theorem_match = re.search(r"\s+([A-Za-z][A-Za-z0-9_']*)\s*(?:;;?)?\s*$", masked[term_end:])
    if not theorem_match:
        return None
    return name, normalize_statement(expected), theorem_match.group(1)


def parse_use_directive(phrase: str) -> str | None:
    match = USE_RE.search(phrase.strip())
    if not match:
        return None
    return parse_ocaml_string_literal(match.group(1))


def parse_checked_source_load(phrase: str) -> str | None:
    match = CHECKED_SOURCE_LOAD_RE.search(phrase)
    if not match:
        return None
    return parse_ocaml_string_literal(match.group(1))


def parse_needs_directive(phrase: str) -> str | None:
    match = NEEDS_RE.search(phrase.strip())
    if not match:
        return None
    return parse_ocaml_string_literal(match.group(1))


def parse_ocaml_string_literal(literal: str) -> str:
    try:
        return json.loads(literal)
    except json.JSONDecodeError:
        return bytes(literal[1:-1], "utf-8").decode("unicode_escape")


def should_virtualize_hol_library_need(path: str) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    return not Path(path).is_absolute() and (
        normalized.startswith(VIRTUAL_HOL_LIBRARY_PREFIXES) or normalized in VIRTUAL_HOL_LIBRARY_FILES
    )


def resolve_use_path(path: str, *, cwd: Path) -> Path:
    resolved = Path(path)
    if not resolved.is_absolute():
        resolved = cwd / resolved
    return resolved.resolve()


def normalize_statement(statement: str | None) -> str:
    if statement is None:
        return ""
    return " ".join(statement.strip().split())


def canonicalize_visible_typevars(statement: str | None) -> str:
    normalized = normalize_statement(statement)
    names: dict[str, str] = {}

    def replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in names:
            names[name] = f"HARNESS_TY_{len(names)}"
        return names[name]

    return TYPEVAR_TOKEN_RE.sub(replace, normalized)


def statements_match(actual: str | None, expected: str | None) -> bool:
    if normalize_statement(actual) == normalize_statement(expected):
        return True
    return canonicalize_visible_typevars(actual) == canonicalize_visible_typevars(expected)


def read_backquoted_term(text: str, start: int) -> tuple[str, int]:
    if start < 0 or start >= len(text) or text[start] != "`":
        raise ValueError("read_backquoted_term requires an opening backquote")
    out: list[str] = []
    i = start + 1
    while i < len(text):
        ch = text[i]
        if ch == "`":
            return "".join(out), i + 1
        out.append(ch)
        i += 1
    raise ValueError("unterminated HOL backquoted term")


def mask_ocaml_comments_and_strings_except_backquotes(text: str) -> str:
    chars = list(text)
    i = 0
    comment_depth = 0
    in_string = False
    in_backquote = False
    while i < len(chars):
        ch = chars[i]
        nxt = chars[i + 1] if i + 1 < len(chars) else ""
        if in_backquote:
            if ch == "`":
                in_backquote = False
            i += 1
            continue
        if in_string:
            if ch == "\\":
                chars[i] = " "
                if i + 1 < len(chars):
                    chars[i + 1] = " "
                i += 2
                continue
            chars[i] = " "
            if ch == '"':
                in_string = False
            i += 1
            continue
        if comment_depth:
            if ch == "(" and nxt == "*":
                chars[i] = chars[i + 1] = " "
                comment_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == ")":
                chars[i] = chars[i + 1] = " "
                comment_depth -= 1
                i += 2
                continue
            chars[i] = " "
            i += 1
            continue
        if ch == "`":
            in_backquote = True
            i += 1
            continue
        if ch == '"':
            in_string = True
            chars[i] = " "
            i += 1
            continue
        if ch == "(" and nxt == "*":
            chars[i] = chars[i + 1] = " "
            comment_depth = 1
            i += 2
            continue
        i += 1
    return "".join(chars)


def split_ocaml_phrases(text: str, *, start_line: int = 1) -> list[tuple[str, int]]:
    phrases: list[tuple[str, int]] = []
    phrase_start = 0
    phrase_line = start_line
    line = start_line
    i = 0
    comment_depth = 0
    in_string = False
    in_backquote = False
    while i < len(text):
        ch = text[i]
        nxt = text[i + 1] if i + 1 < len(text) else ""
        if ch == "\n":
            line += 1
        if in_backquote:
            if ch == "`":
                in_backquote = False
            i += 1
            continue
        if in_string:
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                in_string = False
            i += 1
            continue
        if comment_depth:
            if ch == "(" and nxt == "*":
                comment_depth += 1
                i += 2
                continue
            if ch == "*" and nxt == ")":
                comment_depth -= 1
                i += 2
                continue
            i += 1
            continue
        if ch == "`":
            in_backquote = True
            i += 1
            continue
        if ch == '"':
            in_string = True
            i += 1
            continue
        if ch == "(" and nxt == "*":
            comment_depth = 1
            i += 2
            continue
        if ch == ";" and nxt == ";":
            phrase = text[phrase_start:i]
            phrases.append((phrase, phrase_line))
            i += 2
            phrase_start = i
            phrase_line = line
            continue
        i += 1
    tail = text[phrase_start:]
    if tail.strip():
        phrases.append((tail, phrase_line))
    return phrases


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fake HOL runtime helper.")
    parser.add_argument("--self-test", action="store_true", help="Run deterministic in-process runtime checks")
    args = parser.parse_args(argv)
    if args.self_test:
        self_test()
        print("fake HOL runtime self-test passed")
        return 0
    parser.print_help()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
