"""Natural language interpreter.

The model turns intent into a spec. It never emits a command, a script or a build
instruction, and its output is rejected by the schema before anything else looks at
it.
"""
