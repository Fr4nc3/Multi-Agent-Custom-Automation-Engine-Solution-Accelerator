from typing import List, Optional

import semantic_kernel as sk
from semantic_kernel.functions import KernelFunction

from kernel_agents.agent_base import BaseAgent
from context.cosmos_memory_kernel import CosmosMemoryContext

class ProcurementAgent(BaseAgent):
    """Procurement agent implementation using Semantic Kernel."""

    def __init__(
        self,
        kernel: sk.Kernel,
        session_id: str,
        user_id: str,
        memory_store: CosmosMemoryContext,
        tools: List[KernelFunction] = None,
        system_message: Optional[str] = None,
        agent_name: str = "ProcurementAgent",
        config_path: Optional[str] = None
    ) -> None:
        """Initialize the Procurement Agent.
        
        Args:
            kernel: The semantic kernel instance
            session_id: The current session identifier
            user_id: The user identifier
            memory_store: The Cosmos memory context
            tools: List of tools available to this agent (optional)
            system_message: Optional system message for the agent
            agent_name: Optional name for the agent (defaults to "ProcurementAgent")
            config_path: Optional path to the Procurement tools configuration file
        """
        # Load configuration if tools not provided
        if tools is None:
            config = self.load_tools_config("procurement", config_path)
            tools = self.get_tools_from_config(kernel, "procurement", config_path)
            if not system_message:
                system_message = config.get("system_message", "You are a Procurement agent. You have knowledge about purchasing processes, supplier management, and contract negotiations.")
            agent_name = config.get("agent_name", agent_name)
        
        super().__init__(
            agent_name=agent_name,
            kernel=kernel,
            session_id=session_id,
            user_id=user_id,
            memory_store=memory_store,
            tools=tools,
            system_message=system_message
        )