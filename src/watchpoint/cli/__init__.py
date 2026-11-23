"""Command line interface. Each group of commands is its own module; importing one registers its
commands on `app`, so the order below is the order `watchpoint --help` lists them in."""

# isort: off
from . import watching  # noqa: F401 - init, list, check, ack
from . import looking_back  # noqa: F401 - status, diff, show, history, log
from . import allowing  # noqa: F401 - allow
from . import trying  # noqa: F401 - preview, doctor, open, clean
from . import page  # noqa: F401 - gui
from . import alerts  # noqa: F401 - fields, rule, expect
from . import agent  # noqa: F401 - skill
from . import secret  # noqa: F401 - secret
from . import unattended  # noqa: F401 - schedule

# isort: on
from .common import app

__all__ = ["app"]
