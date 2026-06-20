"""Claude intelligence core: local prefilter, prompts, and the analyzer.

The pipeline is cost-aware by design: a cheap local prefilter discards noise
before any token is spent, results are cached by content (no re-analysis), and
a per-run budget guard caps how many items reach the model.
"""
