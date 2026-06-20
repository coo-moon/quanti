"""qmt-bridge: standalone localhost HTTP gateway to QMT/xtquant.

Not part of the installable ``quanti`` package (see pyproject packages.find,
which only includes ``quanti*``). Runs on the QMT-bundled Python; quanti talks
to it over HTTP. Present as a package only so the test-suite can import it.
"""
