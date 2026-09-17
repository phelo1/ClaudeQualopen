"""Live context for trade candidates: news, social chatter, options flow,
events and fundamentals - fetched only for names that already pass the
price-based setup, scored, cached and folded into the plan checklist and
justification.
"""

from .base import ContextReport, Headline, ContextCache
from .gather import ContextGatherer

__all__ = ["ContextReport", "Headline", "ContextCache", "ContextGatherer"]
