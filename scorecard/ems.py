"""The one place this package reaches into the event-management app.

Why this is not a plain ``from app import db``: running the site with
``python app.py`` makes that module ``__main__``. A plain import of ``app``
from inside this package would not find it under that name, so Python would
execute app.py a *second* time as a separate module — registering this
blueprint twice and raising "the setup method 'route' can no longer be called".

Looking the module up in ``sys.modules`` binds to whichever instance is
actually running, so the package behaves the same under ``python app.py``,
``flask run`` and the Vercel entry point.

Keeping every borrowed name here also means the coupling is one short file
long: this is the complete list of what the scorecard uses from the EMS.
"""

import sys


def _host_module():
    # "app" first: under WSGI and `flask run` that is the live module. Falls
    # back to __main__, which is what `python app.py` produces.
    for name in ("app", "__main__"):
        module = sys.modules.get(name)
        if module is not None and hasattr(module, "db"):
            return module
    import app as module  # nothing running yet — a test importing us directly

    return module


_ems = _host_module()

db = _ems.db
admin_required = _ems.admin_required
SPORTS = _ems.SPORTS


def team_owner_model():
    """Fetched on demand — only the optional linking screen needs it."""
    return _ems.TeamOwner
