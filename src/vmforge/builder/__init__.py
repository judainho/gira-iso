"""Deterministic builder: spec in, image plus manifest out.

Nothing in this package talks to a model. Every input is either the validated spec
or a pinned, checksummed artifact, so a rebuild is a function of those two.
"""
