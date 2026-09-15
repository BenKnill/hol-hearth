let read_all channel =
  let buffer = Buffer.create 4096 in
  (try
     while true do
       Buffer.add_channel buffer channel 4096
     done
   with End_of_file -> ());
  Buffer.contents buffer

let () =
  Toploop.initialize_toplevel_env ();
  let source = read_all stdin in
  print_endline (Eval.card (Eval.evaluate source))
