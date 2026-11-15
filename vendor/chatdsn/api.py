"""Data provider abstraction for fetching agent positions."""

import logging
import time
from abc import ABC, abstractmethod
from typing import Optional, List
from .types import AgentPositions, Position

logger = logging.getLogger(__name__)


class DataProvider(ABC):
    """Abstract data provider interface."""

    @abstractmethod
    def get_agent_positions(self, agent_name: str) -> Optional[AgentPositions]:
        """Fetch positions for a specific agent."""
        pass

    @abstractmethod
    def list_available_agents(self) -> List[str]:
        """List all available agents."""
        pass


class Nof1DataProvider(DataProvider):
    """Data provider for nof1.ai API."""

    def __init__(self, base_url: str = "https://nof1.ai/api"):
        """Initialize with base URL."""
        self.base_url = base_url.rstrip("/")
        self._cache: dict = {}
        self._cache_ttl = 60  # 60 seconds

        # Try to import requests
        try:
            import requests
            self.requests = requests
        except ImportError:
            raise ImportError("requests library required. Install with: pip install requests")

    def get_agent_positions(self, agent_name: str) -> Optional[AgentPositions]:
        """Fetch positions for a specific agent with caching."""
        cache_key = f"positions_{agent_name}"
        now = time.time()

        # Check cache
        if cache_key in self._cache:
            cached_data, cached_time = self._cache[cache_key]
            if now - cached_time < self._cache_ttl:
                logger.debug(f"Cache hit for {agent_name}")
                return cached_data

        # Fetch from API
        try:
            url = f"{self.base_url}/agents/{agent_name}/positions"
            response = self.requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            agent_positions = self._parse_positions(agent_name, data)

            # Update cache
            self._cache[cache_key] = (agent_positions, now)

            return agent_positions

        except self.requests.exceptions.RequestException as e:
            logger.error(f"Failed to fetch positions for {agent_name}: {e}")
            return None

    def list_available_agents(self) -> List[str]:
        """List all available agents."""
        try:
            url = f"{self.base_url}/agents"
            response = self.requests.get(url, timeout=10)
            response.raise_for_status()

            data = response.json()
            if isinstance(data, dict) and "agents" in data:
                return [agent.get("name", agent.get("id", "")) for agent in data["agents"]]
            elif isinstance(data, list):
                return [agent.get("name", agent.get("id", "")) for agent in data]

            return []

        except self.requests.exceptions.RequestException as e:
            logger.error(f"Failed to list agents: {e}")
            return []

    def _parse_positions(self, agent_name: str, data: dict) -> AgentPositions:
        """Parse API response into AgentPositions."""
        positions = []

        # Parse positions array
        positions_data = data.get("positions", [])
        for pos_data in positions_data:
            try:
                position = Position(
                    coin=pos_data["coin"],
                    side=pos_data["side"],
                    entry_price=float(pos_data["entryPrice"]),
                    quantity=float(pos_data["quantity"]),
                    leverage=int(pos_data.get("leverage", 10)),
                    margin=float(pos_data.get("margin", 0)),
                    entry_oid=pos_data.get("entryOid", pos_data.get("orderId", "")),
                    tp_price=float(pos_data["tpPrice"]) if pos_data.get("tpPrice") else None,
                    sl_price=float(pos_data["slPrice"]) if pos_data.get("slPrice") else None,
                    unrealized_pnl=float(pos_data.get("unrealizedPnl", 0)),
                )
                positions.append(position)
            except (KeyError, ValueError, TypeError) as e:
                logger.warning(f"Failed to parse position: {e}")
                continue

        return AgentPositions(
            agent_name=agent_name,
            positions=positions,
            total_balance=float(data.get("totalBalance", 0)),
            total_margin=float(data.get("totalMargin", 0)),
            unrealized_pnl=float(data.get("unrealizedPnl", 0)),
        )


class ApiClient:
    """API client with pluggable data provider."""

    def __init__(self, provider: Optional[DataProvider] = None):
        """Initialize with a data provider."""
        if provider is None:
            from .config import config
            provider = Nof1DataProvider(config.nof1_api_base)

        self.provider = provider

    def get_agent_positions(self, agent_name: str) -> Optional[AgentPositions]:
        """Get positions for an agent."""
        return self.provider.get_agent_positions(agent_name)

    def list_agents(self) -> List[str]:
        """List all available agents."""
        return self.provider.list_available_agents()
