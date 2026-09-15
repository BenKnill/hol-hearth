(* Development-only, memory-first HOL edit session.

   A restored profile starts one server. Each request carries source bytes over
   its socket, forks a disposable child, and returns a compact result. No
   per-request source, transcript, lifecycle, or receipt file is created. *)

external hot_exit : int -> 'a = "caml_unix_exit"
external descriptor_number : Unix.file_descr -> int = "%identity"
external close_descriptor_number : int -> unit = "caml_unix_close"

let marker nonce kind attempt payload =
  Printf.sprintf "__HOL_WORKBENCH_HOT__:%s:%s:%s:%s" nonce kind attempt payload

let write_all descriptor payload =
  let rec loop offset =
    if offset < String.length payload then
      let written =
        Unix.write_substring descriptor payload offset
          (String.length payload - offset)
      in
      if written = 0 then raise End_of_file else loop (offset + written)
  in
  loop 0

let send_line descriptor line = write_all descriptor (line ^ "\n")

let read_exact channel size =
  if size < 0 then invalid_arg "negative hot-session request size";
  let payload = Bytes.create size in
  really_input channel payload 0 size;
  Bytes.unsafe_to_string payload

let close_descriptor descriptor =
  try close_descriptor_number descriptor with
  | Unix.Unix_error (Unix.EBADF, _, _) -> ()

let close_fds_except keep =
  let keep = Array.map descriptor_number keep in
  let should_keep descriptor =
    descriptor >= 0
    && (descriptor <= 2 || Array.exists (( = ) descriptor) keep)
  in
  Sys.readdir "/proc/self/fd"
  |> Array.iter (fun entry ->
         match int_of_string_opt entry with
         | Some descriptor when not (should_keep descriptor) ->
             close_descriptor descriptor
         | _ -> ())

let redirect_stdin () =
  let devnull = Unix.openfile "/dev/null" [ Unix.O_RDONLY ] 0 in
  Unix.dup2 devnull Unix.stdin;
  if devnull <> Unix.stdin then Unix.close devnull

let redirect_outputs descriptor =
  Unix.dup2 descriptor Unix.stdout;
  Unix.dup2 descriptor Unix.stderr;
  if descriptor <> Unix.stdout && descriptor <> Unix.stderr then
    Unix.close descriptor

let wait_for_byte descriptor timeout =
  let ready, _, _ = Unix.select [ descriptor ] [] [] timeout in
  if ready = [] then false
  else
    let byte = Bytes.create 1 in
    Unix.read descriptor byte 0 1 = 1

let wait_for_input descriptor timeout =
  let rec loop deadline =
    let remaining = deadline -. Unix.gettimeofday () in
    if remaining <= 0.0 then false
    else
      match Unix.select [ descriptor ] [] [] remaining with
      | [], _, _ -> false
      | _ -> true
      | exception Unix.Unix_error (Unix.EINTR, _, _) -> loop deadline
  in
  loop (Unix.gettimeofday () +. timeout)

type child_status =
  | Exited of int
  | Signaled of int
  | Stopped of int

(* Python supplies POSIX numbers from the running host. OCaml's portable signal
   constants differ from POSIX numbers; the conversion API appeared in 5.4. *)
let signal_table = ref []
let configure_signals entries = signal_table := entries
let signal_details signal =
  match List.assoc_opt signal !signal_table with
  | Some details -> details
  | None when signal >= 0 -> (signal, Printf.sprintf "SIG%d" signal)
  | None -> invalid_arg "unconfigured portable signal"

let signal_to_int signal = fst (signal_details signal)
let signal_to_string signal = snd (signal_details signal)

let child_status = function
  | Unix.WEXITED code -> Exited code
  | Unix.WSIGNALED signal -> Signaled signal
  | Unix.WSTOPPED signal -> Stopped signal

let child_status_code = function
  | Exited code -> code
  | Signaled signal -> 128 + signal_to_int signal
  | Stopped _ -> invalid_arg "stopped child has no completion code"

let child_status_payload = function
  | Exited code -> Printf.sprintf "exited:%d" code
  | Signaled signal ->
      Printf.sprintf "signaled:%d:%s" (signal_to_int signal)
        (signal_to_string signal)
  | Stopped signal ->
      Printf.sprintf "stopped:%d:%s" (signal_to_int signal)
        (signal_to_string signal)

let rec wait_until child deadline =
  match Unix.waitpid [ Unix.WNOHANG ] child with
  | 0, _ when Unix.gettimeofday () < deadline ->
      Unix.sleepf 0.005;
      wait_until child deadline
  | 0, _ -> None
  | _, status -> Some (child_status status)
  | exception Unix.Unix_error (Unix.EINTR, _, _) -> wait_until child deadline

let signal_owned_child child signal =
  try Unix.kill (-child) signal with
  | Unix.Unix_error (Unix.ESRCH, _, _) ->
      (* Before [setsid] there is no child-owned process group yet.  The PID is
         exact and cannot be reused while this parent owns it. *)
      (try Unix.kill child signal with
      | Unix.Unix_error (Unix.ESRCH, _, _) -> ())

let rec wait_reaped child =
  match Unix.waitpid [] child with
  | _, status -> child_status status
  | exception Unix.Unix_error (Unix.EINTR, _, _) -> wait_reaped child

let terminate child =
  signal_owned_child child Sys.sigterm;
  match wait_until child (Unix.gettimeofday () +. 0.25) with
  | Some (Exited _ as status) | Some (Signaled _ as status) -> status
  | Some (Stopped _) | None ->
      signal_owned_child child Sys.sigkill;
      wait_reaped child

type child_outcome =
  | Child_exited of child_status
  | Child_timed_out
  | Client_disconnected

let rec wait_for_child connection child deadline =
  match Unix.waitpid [ Unix.WNOHANG; Unix.WUNTRACED ] child with
  | 0, _ when Unix.gettimeofday () >= deadline -> Child_timed_out
  | 0, _ ->
      let remaining = deadline -. Unix.gettimeofday () in
      let ready, _, _ = Unix.select [ connection ] [] [] (min 0.01 remaining) in
      if ready = [] then wait_for_child connection child deadline
      else
        let byte = Bytes.create 1 in
        (match Unix.recv connection byte 0 1 [ Unix.MSG_PEEK ] with
        | 0 -> Client_disconnected
        | _ ->
            (* No post-ACK client data belongs to the source-bytes protocol. *)
            Client_disconnected
        | exception Unix.Unix_error (Unix.EINTR, _, _) ->
            wait_for_child connection child deadline
        | exception Unix.Unix_error
                      ((Unix.ECONNRESET | Unix.ENOTCONN), _, _) ->
            Client_disconnected)
  | _, status -> Child_exited (child_status status)
  | exception Unix.Unix_error (Unix.EINTR, _, _) ->
      wait_for_child connection child deadline

exception Stop_server

let serve_request connection nonce fields input =
  match fields with
  | [ "eval"; request_nonce; attempt; timeout_text; size_text ] ->
      if request_nonce <> nonce then
        send_line connection (marker nonce "error" attempt "bad_nonce")
      else
        let timeout = max 1 (int_of_string timeout_text) in
        let size = int_of_string size_text in
        let source = read_exact input size in
        let gate_read, gate_write = Unix.pipe ~cloexec:true () in
        let ready_read, ready_write = Unix.pipe ~cloexec:true () in
        let child = Unix.fork () in
        if child = 0 then
          (Unix.close gate_write;
           Unix.close ready_read;
           (* Signal after session ownership is established. *)
           ignore (Unix.setsid ());
           ignore (Unix.write_substring ready_write "1" 0 1);
           Unix.close ready_write;
           Sys.set_signal Sys.sigchld Sys.Signal_default;
           redirect_stdin ();
           close_fds_except [| connection; gate_read |];
           if not (wait_for_byte gate_read 5.0) then hot_exit 126;
           Unix.close gate_read;
           redirect_outputs connection;
           let card = Eval.card (Eval.evaluate ~timeout source) in
           print_endline (marker nonce "result" attempt card);
           flush stdout;
           flush stderr;
           hot_exit 0)
        else
          (Unix.close gate_read;
           Unix.close ready_write;
           let gate_open = ref true in
           let ready_open = ref true in
           let child_reaped = ref false in
           let close_gate () =
             if !gate_open then
               (gate_open := false;
                Unix.close gate_write)
           in
           let close_ready () =
             if !ready_open then
               (ready_open := false;
                Unix.close ready_read)
           in
           let terminate_owned () =
             if not !child_reaped then
               (child_reaped := true;
                ignore (terminate child))
           in
           Fun.protect
             ~finally:(fun () ->
               close_ready ();
               close_gate ();
               terminate_owned ())
             (fun () ->
               if not (wait_for_byte ready_read 2.0) then
                 (close_ready ();
                  terminate_owned ();
                  send_line connection
                    (marker nonce "error" attempt "child_not_ready"))
               else
                 (close_ready ();
                  send_line connection
                    (marker nonce "spawned" attempt (string_of_int child));
                  if not (wait_for_input connection 5.0) then
                    (close_gate ();
                     terminate_owned ();
                     send_line connection
                       (marker nonce "error" attempt "registration_timeout"))
                  else
                    let acknowledgement =
                      try Some (input_line input) with End_of_file -> None
                    in
                    match acknowledgement with
                    | None ->
                        close_gate ();
                        terminate_owned ()
                    | Some acknowledgement
                      when acknowledgement <> "ack\t" ^ nonce ^ "\t" ^ attempt ->
                        close_gate ();
                        terminate_owned ();
                        send_line connection
                          (marker nonce "error" attempt "registration_refused")
                    | Some _ ->
                        ignore (Unix.write_substring gate_write "1" 0 1);
                        close_gate ();
                        (match
                           wait_for_child connection child
                             (Unix.gettimeofday () +. float_of_int timeout +. 1.0)
                         with
                        | Client_disconnected -> terminate_owned ()
                        | Child_timed_out ->
                            let status = terminate child in
                            child_reaped := true;
                            send_line connection
                              (marker nonce "result" attempt
                                 (Printf.sprintf "TIMEOUT seconds=%d" timeout));
                            send_line connection
                              (marker nonce "status" attempt
                                 (child_status_payload status));
                            send_line connection
                              (marker nonce "completed" attempt
                                 (string_of_int (child_status_code status)))
                        | Child_exited (Stopped signal) ->
                            ignore (terminate child);
                            child_reaped := true;
                            send_line connection
                              (marker nonce "error" attempt
                                 (Printf.sprintf "child_stopped:%d:%s"
                                    (signal_to_int signal)
                                    (signal_to_string signal)))
                        | Child_exited status ->
                            child_reaped := true;
                            send_line connection
                              (marker nonce "status" attempt
                                 (child_status_payload status));
                            send_line connection
                              (marker nonce "completed" attempt
                                 (string_of_int (child_status_code status)))))))
  | [ "status"; request_nonce ] when request_nonce = nonce ->
      send_line connection
        (marker nonce "ready" "server" (string_of_int (Unix.getpid ())))
  | [ "stop"; request_nonce ] when request_nonce = nonce ->
      raise Stop_server
  | _ -> send_line connection (marker nonce "error" "unknown" "bad_request")

let process_start_ticks pid =
  try
    let channel = open_in (Printf.sprintf "/proc/%d/stat" pid) in
    let line = input_line channel in
    close_in_noerr channel;
    let close = String.rindex line ')' in
    let suffix =
      String.sub line (close + 2) (String.length line - close - 2)
    in
    match String.split_on_char ' ' suffix |> List.filter (( <> ) "") with
    | fields when List.length fields > 19 ->
        Int64.of_string_opt (List.nth fields 19)
    | _ -> None
  with _ -> None

let owner_is_alive pid start_ticks =
  process_start_ticks pid = Some start_ticks

let serve listener socket_path nonce owner_pid owner_start_ticks =
  let running = ref true in
  let listener_open = ref true in
  let close_listener () =
    if !listener_open then
      (listener_open := false;
       Unix.close listener;
       (try Unix.unlink socket_path with
       | Unix.Unix_error (Unix.ENOENT, _, _) -> ()))
  in
  Fun.protect
    ~finally:close_listener
    (fun () ->
      while !running && owner_is_alive owner_pid owner_start_ticks do
        let ready, _, _ = Unix.select [ listener ] [] [] 0.25 in
        if ready <> [] then
          match Unix.accept ~cloexec:true listener with
          | exception Unix.Unix_error (Unix.EINTR, _, _) -> ()
          | connection, _ ->
            let input = Unix.in_channel_of_descr connection in
            let stop_requested =
              try
                let header = input_line input in
                let fields = String.split_on_char '\t' header in
                serve_request connection nonce fields input;
                false
              with
              | Stop_server -> true
              | End_of_file -> false
              | error ->
                  (try
                     send_line connection
                       (marker nonce "error" "server" (Printexc.to_string error))
                   with _ -> ());
                  false
            in
            if stop_requested then
              (running := false;
               (* Stop accepting before acknowledging.  Synchronous request
                  handling guarantees that no evaluation child remains. *)
               close_listener ();
               (try
                  send_line connection (marker nonce "stopping" "server" "0")
                with _ -> ()));
            close_in_noerr input
      done)

let start socket_path nonce owner_pid owner_start_ticks =
  if socket_path = "" || nonce = "" then invalid_arg "empty hot-session identity";
  if owner_pid <= 1 || not (owner_is_alive owner_pid owner_start_ticks) then
    invalid_arg "invalid hot-session owner identity";
  (try Unix.unlink socket_path with
  | Unix.Unix_error (Unix.ENOENT, _, _) -> ());
  let listener = Unix.socket ~cloexec:true Unix.PF_UNIX Unix.SOCK_STREAM 0 in
  Unix.bind listener (Unix.ADDR_UNIX socket_path);
  Unix.listen listener 4;
  (* The restored basis is already warm.  Forcing a whole-heap collection here
     made server startup scale with profile size and could fail on probability;
     it is an optional COW optimization, not part of evaluator correctness. *)
  let child = Unix.fork () in
  if child = 0 then
    (ignore (Unix.setsid ());
     Sys.set_signal Sys.sigchld Sys.Signal_default;
     redirect_stdin ();
     let devnull = Unix.openfile "/dev/null" [ Unix.O_WRONLY ] 0 in
     Unix.dup2 devnull Unix.stdout;
     Unix.dup2 devnull Unix.stderr;
     if devnull <> Unix.stdout && devnull <> Unix.stderr then Unix.close devnull;
     close_fds_except [| listener |];
     (try serve listener socket_path nonce owner_pid owner_start_ticks with _ -> ());
     hot_exit 0)
  else
    (Unix.close listener;
     child)
