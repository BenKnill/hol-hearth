needs "arm/proofs/base.ml";;
needs "common/mlkem_mldsa.ml";;
let HOL_WORKBENCH_S2N_ARM_MLKEM_WARMUP = prove(`forward_ntt = reorder bitreverse_pairs o pure_forward_ntt`,ACCEPT_TAC FORWARD_NTT);;
