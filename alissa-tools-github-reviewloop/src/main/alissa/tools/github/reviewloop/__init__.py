"""reviewloop — a GitHub watcher that drives the alissa-code-review
adversarial review loop (CR1–CR9) to convergence."""

from .config import Config
from .loop import Action, Decision, ReviewWatcher

__all__ = ["Config", "ReviewWatcher", "Decision", "Action"]
__version__ = "0.1.0"
