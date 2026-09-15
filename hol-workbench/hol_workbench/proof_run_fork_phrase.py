"""Pure OCaml phrase generation for one disposable fork evaluation."""

from __future__ import annotations

from pathlib import Path

from hol_workbench import fork_child_ocaml
from hol_workbench.foundation_delta import foundation_wrapped_load

FORK_CHILD_FORMAT_FLUSH = fork_child_ocaml.FORK_CHILD_FORMAT_FLUSH
FORK_CHILD_SIGNAL_RESET = fork_child_ocaml.FORK_CHILD_SIGNAL_RESET
FORK_CHILD_UNIX_EXIT = fork_child_ocaml.FORK_CHILD_UNIX_EXIT
FORK_CHILD_UNIX_EXIT_DECLARATION = fork_child_ocaml.FORK_CHILD_UNIX_EXIT_DECLARATION
fork_child_ownership_transfer = fork_child_ocaml.fork_child_ownership_transfer
ocaml_string_literal = fork_child_ocaml.ocaml_string_literal


def fork_eval_phrase(
    *,
    attempt_id: str,
    source: str,
    transcript: str,
    load_wrapper: Path,
    done_token: str,
    spawn_token: str,
    ack_path: Path,
    ownership_path: Path,
    refusal_token: str,
    foundation_marker_prefix: str | None = None,
) -> str:
    """Build the one OCaml phrase that forks and runs a disposable evaluation."""

    wrapped_load = foundation_wrapped_load(
        load_wrapper=load_wrapper,
        done_token=done_token,
        marker_prefix=foundation_marker_prefix,
    )
    return f"""
{FORK_CHILD_UNIX_EXIT_DECLARATION}
let _ =
  {FORK_CHILD_FORMAT_FLUSH};
  flush stdout;
  flush stderr;
  let ready_r,ready_w = Unix.pipe () in
  let child =
    try Unix.fork () with e ->
      (Printf.printf "%s%s\\n" {ocaml_string_literal(refusal_token)} (Printexc.to_string e);
       flush stdout;
       -1) in
  if child = -1 then (Unix.close ready_r; Unix.close ready_w) else
  if child = 0 then
    ({FORK_CHILD_SIGNAL_RESET};
     Unix.close ready_r;
     ignore (Unix.write_substring ready_w "1" 0 1);
     Unix.close ready_w;
     {fork_child_ownership_transfer(ack_path, ownership_path)}
     let oc = open_out_bin {ocaml_string_literal(transcript)} in
     let fd = Unix.descr_of_out_channel oc in
     Unix.dup2 fd Unix.stdout;
     Unix.dup2 fd Unix.stderr;
     {FORK_CHILD_FORMAT_FLUSH};
     Printf.printf "[fork-worker] attempt %s begin source=%s\\n" {ocaml_string_literal(attempt_id)} {ocaml_string_literal(source)};
     flush stdout;
{wrapped_load}     Printf.printf "[fork-worker] attempt %s end\\n" {ocaml_string_literal(attempt_id)};
     {FORK_CHILD_FORMAT_FLUSH};
     flush stdout;
     flush stderr;
     close_out_noerr oc;
     {FORK_CHILD_UNIX_EXIT} 0)
  else
    (Unix.close ready_w;
     let ready = Bytes.create 1 in
     ignore (Unix.read ready_r ready 0 1);
     Unix.close ready_r;
     Printf.printf "%s%d\\n" {ocaml_string_literal(spawn_token)} child;
     flush stdout);;
""".lstrip()
