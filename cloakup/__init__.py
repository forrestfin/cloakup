"""cloakup — AI-driven, human-paced browser agent on top of CloakBrowser."""

from .agent import AgentResult, WebAgent
from .config import AgentConfig, BrowserConfig, PacingConfig

__all__ = ["WebAgent", "AgentResult", "AgentConfig", "BrowserConfig", "PacingConfig"]
__version__ = "0.1.0"
