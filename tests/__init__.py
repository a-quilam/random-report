"""Test package for the RandReport stack.

Importing this package installs the automatic pre-exit resource check, so a
``python -m unittest discover -s tests`` run fails with exit code 1 when a SQLite
handle, a listening socket or an HTTP response is still open as the process ends.
That check exists because Python only *prints* a ``ResourceWarning`` for such a
handle (the run still ends "OK" with exit code 0, even under
``-W error::ResourceWarning``), so the defect would come back unnoticed
(docs/TEST_RESULTS_ACCEPTANCE.md section 2.6).
"""

from randreport import leakguard

# verbose=True prints one confirmation line when the process is clean
install = leakguard.install

install(verbose=True)
