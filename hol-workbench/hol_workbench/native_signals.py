"""Map OCaml's portable signal constants to this Linux host's POSIX values."""

import signal

SIGNALS = (
    "ABRT", "ALRM", "FPE", "HUP", "ILL", "INT", "KILL", "PIPE", "QUIT",
    "SEGV", "TERM", "USR1", "USR2", "CHLD", "CONT", "STOP", "TSTP", "TTIN",
    "TTOU", "VTALRM", "PROF", "BUS", "POLL", "SYS", "TRAP", "URG", "XCPU",
    "XFSZ",
)


def ocaml_signal_configuration(ocaml_version: str) -> str:
    entries = []
    version = tuple(int(part) for part in ocaml_version.split(".")[:2])
    names = SIGNALS + (("IO", "WINCH") if version >= (5, 4) else ())
    for name in names:
        value = getattr(signal, "SIG" + name, None)
        if value is not None:
            entries.append(f'(Sys.sig{name.lower()}, ({int(value)}, "SIG{name}"))')
    return "Hot_session.configure_signals [" + "; ".join(entries) + "];;"
