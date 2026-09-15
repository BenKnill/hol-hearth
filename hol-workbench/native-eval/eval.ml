(* Development-only OCaml phrase evaluation.

   This deliberately reads [Toploop.parse_toplevel_phrase].  A HOL-loaded
   toplevel installs its camlp5 parser there, which keeps backquoted terms,
   nested comments, and all other HOL syntax under HOL Light's parser.
   A successful result is an edit-loop signal, never proof or audit evidence. *)

type outcome =
  | Ok of int
  | Failed of int * string
  | Timed_out of int

exception Timeout

let timed_out = ref false

let line_of source offset =
  let limit = String.length source in
  let position = ref (min offset limit) in
  let whitespace = function ' ' | '\n' | '\t' | '\r' -> true | _ -> false in
  while !position < limit && whitespace source.[!position] do
    incr position
  done;
  let line = ref 1 in
  for index = 0 to !position - 1 do
    if source.[index] = '\n' then incr line
  done;
  !line

let compact message =
  let one_line = String.concat " " (String.split_on_char '\n' message) |> String.trim in
  if String.length one_line > 400 then String.sub one_line 0 400 ^ " ..." else one_line

let evaluate ?timeout source =
  let lexbuf = Lexing.from_string source in
  let count = ref 0 in
  let phrase_start = ref 0 in
  let buffer = Buffer.create 4096 in
  let formatter = Format.formatter_of_buffer buffer in
  let previous =
    Sys.signal Sys.sigalrm
      (Sys.Signal_handle (fun _ -> timed_out := true; raise Timeout))
  in
  timed_out := false;
  Option.iter (fun seconds -> if seconds > 0 then ignore (Unix.alarm seconds)) timeout;
  let restore_alarm () =
    ignore (Unix.alarm 0);
    Sys.set_signal Sys.sigalrm previous
  in
  let rec loop () =
    phrase_start := lexbuf.Lexing.lex_curr_pos;
    match (try Some (!Toploop.parse_toplevel_phrase lexbuf) with End_of_file -> None) with
    | None -> Ok !count
    | Some phrase ->
        Buffer.clear buffer;
        (match Toploop.execute_phrase false formatter phrase with
        | true ->
            incr count;
            loop ()
        | false ->
            Format.pp_print_flush formatter ();
            Failed (line_of source !phrase_start, Buffer.contents buffer)
        | exception _ when !timed_out -> raise Timeout
        | exception error ->
            Format.pp_print_flush formatter ();
            Buffer.clear buffer;
            (try Location.report_exception formatter error
             with _ -> Format.fprintf formatter "%s" (Printexc.to_string error));
            Format.pp_print_flush formatter ();
            Failed (line_of source !phrase_start, Buffer.contents buffer))
  in
  let outcome =
    try loop () with
    | Timeout -> Timed_out (Option.value timeout ~default: 0)
    | Exit -> Failed (line_of source !phrase_start,
                      "Syntax error: HOL parser could not read this phrase")
    | error ->
        Buffer.clear buffer;
        (try Location.report_exception formatter error
         with _ -> Format.fprintf formatter "%s" (Printexc.to_string error));
        Format.pp_print_flush formatter ();
        Failed (line_of source !phrase_start, Buffer.contents buffer)
  in
  restore_alarm ();
  if !timed_out then Timed_out (Option.value timeout ~default: 0) else outcome

let card = function
  | Ok phrases -> Printf.sprintf "OK phrases=%d hol=accepted scope=complete-source" phrases
  | Failed (line, message) -> Printf.sprintf "FAIL line=%d %s" line (compact message)
  | Timed_out seconds -> Printf.sprintf "TIMEOUT seconds=%d" seconds
