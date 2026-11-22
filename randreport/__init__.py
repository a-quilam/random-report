"""RanDom public reporting domain.

Public data layer rules (see docs/DOMAIN.md and docs/REQUIREMENTS.md):
* every money amount is an integer number of kopecks, never a float;
* "unknown" is ``None`` and never silently becomes zero;
* raw source entities (private accounts, transactions, closed calendar records)
  never leave ``randreport.domain``; only allowlisted DTOs from
  ``randreport.domain.dto`` cross into the projection and the web layer.
"""

__all__ = ["domain", "projection"]
