"""RainbowBone AI layer (stream S5; contract C4, contracts/ai-interface.md).

AI never computes a stiffness, strain or risk number. It words numbers our code computed, and
contract.check() proves every number it used.

Modules: facts (the only model input), contract (labels, checker, reading grade), template (the
deterministic last tier), chain (provider order and fall-through), provenance (ai_generation rows),
providers/ (one Provider interface), copilot and tools (M8).

Until M6 every model provider is a stub that reports `unavailable`, and AI_CHAIN defaults to
`template` (Galit's gate): no code path here can reach the network.
"""
