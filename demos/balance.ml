(* Transfer a nonnegative amount between two nonnegative balances. *)
let HEARTH_TRANSFER_CONSERVES = prove
 (`!a b t:real. (a - t) + (b + t) = a + b`,
  REAL_ARITH_TAC);;

let HEARTH_TRANSFER_NONNEGATIVE = prove
 (`!a b t:real. &0 <= b /\ &0 <= t /\ t <= a
                ==> &0 <= a - t /\ &0 <= b + t`,
  REAL_ARITH_TAC);;
