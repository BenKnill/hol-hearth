"""Source loading phrases used by warm fork transports."""

from __future__ import annotations

import os
from pathlib import Path

from hol_workbench.fork_child_ocaml import ocaml_string_literal


def source_local_needs_prelude(
    source: Path, *, source_context: Path | None = None,
    source_root_loads: tuple[tuple[Path, str, Path], ...] = (),
) -> list[str]:
    configured_context = source_context
    if configured_context is None:
        configured = os.environ.get("HOL_WORKBENCH_SOURCE_CONTEXT")
        configured_context = Path(configured) if configured else source
    source_dir = configured_context.resolve().parent
    root_paths = "; ".join(
        f"(({ocaml_string_literal(str(directory))},{ocaml_string_literal(literal)}),"
        f"{ocaml_string_literal(str(packaged))})"
        for directory, literal, packaged in source_root_loads
    )
    return [
        (
            '(* Harness wrapper: resolve needs/loadt/loads "sibling.ml" relative to each '
            "declaring file when such a file exists. *)"
        ),
        f"let proof_run_source_dir_stack = ref [{ocaml_string_literal(str(source_dir))}];;",
        f"let proof_run_source_root_paths = [{root_paths}];;",
        "let proof_run_normalized_source_dir directory =",
        "  let rec components result = function",
        "    [] -> List.rev result",
        '  | ("" | ".")::rest -> components result rest',
        '  | ".."::rest -> components (match result with [] -> [] | _::tail -> tail) rest',
        "  | part::rest -> components (part::result) rest in",
        '  "/" ^ String.concat "/" (components [] (String.split_on_char \'/\' directory));;',
        "let proof_run_current_source_dir () =",
        "  match !proof_run_source_dir_stack with",
        "    source_dir::_ -> source_dir",
        '  | [] -> failwith "proof-run source directory stack underflow";;',
        "let proof_run_source_local_path path =",
        "  if Filename.is_relative path then",
        "    (try Some (List.assoc (proof_run_current_source_dir (),path) proof_run_source_root_paths)",
        "     with Not_found ->",
        "       let local_path = Filename.concat (proof_run_current_source_dir ()) path in",
        "       if Sys.file_exists local_path then Some local_path else None)",
        "  else if Sys.file_exists path then Some path else None;;",
        "let proof_run_with_source_local_declaring_dir loader path =",
        "  match proof_run_source_local_path path with",
        "    None -> loader path",
        "  | Some local_path ->",
        "      let previous_stack = !proof_run_source_dir_stack in",
        "      proof_run_source_dir_stack := proof_run_normalized_source_dir (Filename.dirname local_path) :: previous_stack;",
        "      try",
        "        let result = loader local_path in",
        "        proof_run_source_dir_stack := previous_stack;",
        "        result",
        "      with exn ->",
        "        proof_run_source_dir_stack := previous_stack;",
        "        raise exn;;",
        "let proof_run_original_needs = needs;;",
        "let needs path =",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_STARTED__:needs:" ^ path ^ "\\n");',
        "  Format.print_flush ();",
        "  proof_run_with_source_local_declaring_dir proof_run_original_needs path;",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_COMPLETED__:needs:" ^ path ^ "\\n");',
        "  Format.print_flush ();;",
        "let proof_run_original_loadt = loadt;;",
        "let loadt path =",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_STARTED__:loadt:" ^ path ^ "\\n");',
        "  Format.print_flush ();",
        "  proof_run_with_source_local_declaring_dir proof_run_original_loadt path;",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_COMPLETED__:loadt:" ^ path ^ "\\n");',
        "  Format.print_flush ();;",
        "let proof_run_original_loads = loads;;",
        "let loads path =",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_STARTED__:loads:" ^ path ^ "\\n");',
        "  Format.print_flush ();",
        "  proof_run_with_source_local_declaring_dir proof_run_original_loads path;",
        '  Format.print_string ("__PROOF_RUN_LITERAL_LOAD_COMPLETED__:loads:" ^ path ^ "\\n");',
        "  Format.print_flush ();;",
        "",
    ]


def source_package_runtime_prelude(runtime_cwd: str | Path | None) -> list[str]:
    """Enter an immutable artifact package without losing the profile fallback path."""
    if not runtime_cwd:
        return []
    cwd = str(Path(runtime_cwd).expanduser().resolve())
    return [
        "(* Harness wrapper: make identity-bound literal ELF artifacts visible in this disposable child. *)",
        "let proof_run_original_cwd = Sys.getcwd ();;",
        "load_path := proof_run_original_cwd :: !load_path;;",
        f"Sys.chdir {ocaml_string_literal(cwd)};;",
        "",
    ]


def source_load_phrase(source: Path, *, use_loadt: bool = False) -> str:
    loader = "loadt" if use_loadt else "#use"
    return "\n".join([*source_local_needs_prelude(source), f"{loader} {ocaml_string_literal(str(source))};;"])
