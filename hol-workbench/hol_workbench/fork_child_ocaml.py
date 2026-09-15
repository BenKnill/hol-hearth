"""Pure OCaml fragments shared by broker-owned fork executors."""

from __future__ import annotations

from pathlib import Path

FORK_CHILD_SESSION_SETUP = "ignore (Unix.setsid ())"
FORK_CHILD_SIGNAL_RESET = "Sys.set_signal Sys.sigchld Sys.Signal_default"
FORK_CHILD_FORMAT_FLUSH = "Format.pp_print_flush Format.std_formatter (); Format.pp_print_flush Format.err_formatter ()"
# HOL Light's Camlp5 syntax misparses the `_exit` field name. Bind the same
# no-finalization primitive used by Unix._exit under a parser-safe identifier.
FORK_CHILD_UNIX_EXIT = "proof_run_unix_exit"
FORK_CHILD_UNIX_EXIT_DECLARATION = 'external proof_run_unix_exit : int -> \'a = "caml_unix_exit";;'
FORK_REGISTRATION_ACK_TIMEOUT_SECONDS = 10.0


def ocaml_string_literal(value: str) -> str:
    """Encode exact UTF-8 bytes as an OCaml string literal.

    JSON's ``\\uXXXX`` escapes are not OCaml string escapes. Decimal byte
    escapes keep paths and control tokens exact even when they contain
    non-ASCII characters.
    """

    encoded: list[str] = []
    for byte in value.encode("utf-8"):
        if byte == 0x22:
            encoded.append('\\"')
        elif byte == 0x5C:
            encoded.append("\\\\")
        elif 0x20 <= byte <= 0x7E:
            encoded.append(chr(byte))
        else:
            encoded.append(f"\\{byte:03d}")
    return '"' + "".join(encoded) + '"'


def fork_child_registration_wait(ack_path: Path) -> str:
    encoded = ocaml_string_literal(str(ack_path))
    return "\n".join(
        [
            f"let ack_deadline = Unix.gettimeofday () +. {FORK_REGISTRATION_ACK_TIMEOUT_SECONDS} in",
            f"while not (Sys.file_exists {encoded}) && Unix.gettimeofday () < ack_deadline do",
            "  ignore (Unix.select [] [] [] 0.01)",
            "done;",
            f"if not (Sys.file_exists {encoded}) then {FORK_CHILD_UNIX_EXIT} 127;",
            f"(try Sys.remove {encoded} with _ -> ());",
            f"if Sys.file_exists {encoded} then {FORK_CHILD_UNIX_EXIT} 126;",
        ]
    )


def fork_child_ownership_transfer(ack_path: Path, ownership_path: Path) -> str:
    encoded_ownership = ocaml_string_literal(str(ownership_path))
    return "\n".join(
        [
            fork_child_registration_wait(ack_path),
            f"(try {FORK_CHILD_SESSION_SETUP} with _ -> {FORK_CHILD_UNIX_EXIT} 126);",
            f"let ownership = open_out_bin {encoded_ownership} in",
            'output_string ownership "owned";',
            "close_out ownership;",
            f"let ownership_deadline = Unix.gettimeofday () +. {FORK_REGISTRATION_ACK_TIMEOUT_SECONDS} in",
            f"while Sys.file_exists {encoded_ownership} && Unix.gettimeofday () < ownership_deadline do",
            "  ignore (Unix.select [] [] [] 0.01)",
            "done;",
            f"if Sys.file_exists {encoded_ownership} then {FORK_CHILD_UNIX_EXIT} 126;",
        ]
    )
