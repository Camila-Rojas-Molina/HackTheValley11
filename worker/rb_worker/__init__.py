"""RainbowBone job worker (S4): a separate process that claims jobs from the `jobs` table with a lease and runs
them outside any transaction (contracts/jobs.md). No job code exists before Galit's gate (G1)."""
