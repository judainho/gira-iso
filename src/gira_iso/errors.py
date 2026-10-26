"""One root for every error the tool raises on purpose, and what each one means to a caller.

Before this module each command picked its own exit code and the menu caught its own tuple
of exception types; an error nobody remembered to catch ended in a traceback. Now an error
carries its exit code, so any front end can turn it into the same outcome.

    0  success
    1  rejected: the policy or a verification said no, or the model needs more detail
    2  invalid input: a bad name, a missing file, an unreadable manifest
    3  environment: a tool, the hypervisor, the disk or the network is not usable
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_REJECTED = 1
EXIT_INVALID = 2
EXIT_ENVIRONMENT = 3
EXIT_INTERRUPTED = 130  # the shell convention for SIGINT


class GiraError(RuntimeError):
    """Base of every expected failure. Subclasses set `exit_code`.

    It extends RuntimeError on purpose so code that still catches RuntimeError keeps working.
    """

    exit_code: int = EXIT_INVALID

    @property
    def user_message(self) -> str:
        return str(self)
