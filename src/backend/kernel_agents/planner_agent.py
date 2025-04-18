import logging
import uuid
import json
import re
from typing import Dict, List, Optional, Any, Tuple

import semantic_kernel as sk
from semantic_kernel.functions import KernelFunction
from semantic_kernel.functions.kernel_arguments import KernelArguments

from kernel_agents.agent_base import BaseAgent
from context.cosmos_memory_kernel import CosmosMemoryContext
from models.messages_kernel import (
    AgentMessage,
    InputTask,
    Plan,
    Step,
    StepStatus,
    PlanStatus,
    HumanFeedbackStatus,
)
from event_utils import track_event_if_configured

class PlannerAgent(BaseAgent):
    """Planner agent implementation using Semantic Kernel.
    
    This agent creates and manages plans based on user tasks, breaking them down into steps
    that can be executed by specialized agents to achieve the user's goal.
    """

    def __init__(
        self,
        kernel: sk.Kernel,
        session_id: str,
        user_id: str,
        memory_store: CosmosMemoryContext,
        tools: Optional[List[KernelFunction]] = None,
        system_message: Optional[str] = None,
        agent_name: str = "PlannerAgent",
        config_path: Optional[str] = None,
        client=None,
        definition=None,
        available_agents: List[str] = None,
        agent_tools_list: List[str] = None
    ) -> None:
        """Initialize the Planner Agent.
        
        Args:
            kernel: The semantic kernel instance
            session_id: The current session identifier
            user_id: The user identifier
            memory_store: The Cosmos memory context
            tools: List of tools available to this agent (optional)
            system_message: Optional system message for the agent
            agent_name: Optional name for the agent (defaults to "PlannerAgent")
            config_path: Optional path to the Planner tools configuration file
            client: Optional client instance
            definition: Optional definition instance
            available_agents: List of available agent names for creating steps
            agent_tools_list: List of available tools across all agents
        """
        # Store the available agents and their tools
        self._available_agents = available_agents or ["HumanAgent", "HrAgent", "MarketingAgent", 
                                                    "ProductAgent", "ProcurementAgent", 
                                                    "TechSupportAgent", "GenericAgent"]
        self._agent_tools_list = agent_tools_list or []
        
        # Load configuration if tools not provided
        if tools is None:
            config = self.load_tools_config("planner", config_path)
            tools = self.get_tools_from_config(kernel, "planner", config_path)
            if not system_message:
                system_message = config.get(
                    "system_message", 
                    "You are a Planner agent responsible for creating and managing plans. You analyze tasks, break them down into steps, and assign them to the appropriate specialized agents."
                )
            agent_name = config.get("agent_name", agent_name)
        
        super().__init__(
            agent_name=agent_name,
            kernel=kernel,
            session_id=session_id,
            user_id=user_id,
            memory_store=memory_store,
            tools=tools,
            system_message=system_message,
            client=client,
            definition=definition
        )
        
    async def handle_input_task(self, kernel_arguments: KernelArguments) -> str:
        """Handle the initial input task from the user.
        
        Args:
            kernel_arguments: Contains the input_task_json string
            
        Returns:
            Status message
        """
        # Parse the input task
        input_task_json = kernel_arguments["input_task_json"]
        input_task = InputTask.parse_raw(input_task_json)
        
        # Generate a structured plan with steps
        plan, steps = await self._create_structured_plan(input_task)
        
        if steps:
            # Add a message about the created plan
            await self._memory_store.add_item(
                AgentMessage(
                    session_id=input_task.session_id,
                    user_id=self._user_id,
                    plan_id=plan.id,
                    content=f"Generated a plan with {len(steps)} steps. Click the blue check box beside each step to complete it, click the x to remove this step.",
                    source="PlannerAgent",
                    step_id="",
                )
            )
            
            track_event_if_configured(
                f"Planner - Generated a plan with {len(steps)} steps and added plan into the cosmos",
                {
                    "session_id": input_task.session_id,
                    "user_id": self._user_id,
                    "plan_id": plan.id,
                    "content": f"Generated a plan with {len(steps)} steps. Click the blue check box beside each step to complete it, click the x to remove this step.",
                    "source": "PlannerAgent",
                },
            )
            
            # If human clarification is needed, add a message requesting it
            if hasattr(plan, 'human_clarification_request') and plan.human_clarification_request:
                await self._memory_store.add_item(
                    AgentMessage(
                        session_id=input_task.session_id,
                        user_id=self._user_id,
                        plan_id=plan.id,
                        content=f"I require additional information before we can proceed: {plan.human_clarification_request}",
                        source="PlannerAgent",
                        step_id="",
                    )
                )
                
                track_event_if_configured(
                    "Planner - Additional information requested and added into the cosmos",
                    {
                        "session_id": input_task.session_id,
                        "user_id": self._user_id,
                        "plan_id": plan.id,
                        "content": f"I require additional information before we can proceed: {plan.human_clarification_request}",
                        "source": "PlannerAgent",
                    },
                )
        
        return f"Plan '{plan.id}' created successfully with {len(steps)} steps"
        
    async def handle_plan_clarification(self, kernel_arguments: KernelArguments) -> str:
        """Handle human clarification for a plan.
        
        Args:
            kernel_arguments: Contains session_id and human_clarification
            
        Returns:
            Status message
        """
        session_id = kernel_arguments["session_id"]
        human_clarification = kernel_arguments["human_clarification"]
        
        # Retrieve and update the plan
        plan = await self._memory_store.get_plan_by_session(session_id)
        if not plan:
            return f"No plan found for session {session_id}"
            
        plan.human_clarification_response = human_clarification
        await self._memory_store.update_plan(plan)
        
        # Add a record of the clarification
        await self._memory_store.add_item(
            AgentMessage(
                session_id=session_id,
                user_id=self._user_id,
                plan_id="",
                content=f"{human_clarification}",
                source="HumanAgent",
                step_id="",
            )
        )
        
        track_event_if_configured(
            "Planner - Store HumanAgent clarification and added into the cosmos",
            {
                "session_id": session_id,
                "user_id": self._user_id,
                "content": f"{human_clarification}",
                "source": "HumanAgent",
            },
        )
        
        # Add a confirmation message
        await self._memory_store.add_item(
            AgentMessage(
                session_id=session_id,
                user_id=self._user_id,
                plan_id="",
                content="Thanks. The plan has been updated.",
                source="PlannerAgent",
                step_id="",
            )
        )
        
        track_event_if_configured(
            "Planner - Updated with HumanClarification and added into the cosmos",
            {
                "session_id": session_id,
                "user_id": self._user_id,
                "content": "Thanks. The plan has been updated.",
                "source": "PlannerAgent",
            },
        )
        
        return "Plan updated with human clarification"
        
    async def _create_structured_plan(self, input_task: InputTask) -> Tuple[Plan, List[Step]]:
        """Create a structured plan with steps based on the input task.
        
        Args:
            input_task: The input task from the user
            
        Returns:
            Tuple containing the created plan and list of steps
        """
        try:
            # Generate the instruction for the LLM
            instruction = self._generate_instruction(input_task.description)
            
            # Ask the LLM to generate a structured plan
            messages = [{
                "role": "user", 
                "content": instruction
            }]
            
            result = await self._agent.invoke_async(messages=messages)
            response_content = result.value.strip()
            
            # Parse the JSON response
            try:
                parsed_result = json.loads(response_content)
                
                # Extract plan details
                initial_goal = parsed_result.get("initial_goal", input_task.description)
                steps_data = parsed_result.get("steps", [])
                summary = parsed_result.get("summary_plan_and_steps", "Plan created based on task description")
                human_clarification_request = parsed_result.get("human_clarification_request")
                
                # Create the Plan instance
                plan = Plan(
                    id=str(uuid.uuid4()),
                    session_id=input_task.session_id,
                    user_id=input_task.user_id,
                    initial_goal=initial_goal,
                    overall_status=PlanStatus.in_progress,
                    summary=summary,
                    human_clarification_request=human_clarification_request
                )
                
                # Store the plan
                await self._memory_store.add_plan(plan)
                
                track_event_if_configured(
                    "Planner - Initial plan and added into the cosmos",
                    {
                        "session_id": input_task.session_id,
                        "user_id": input_task.user_id,
                        "initial_goal": initial_goal,
                        "overall_status": PlanStatus.in_progress,
                        "source": "PlannerAgent",
                        "summary": summary,
                        "human_clarification_request": human_clarification_request,
                    },
                )
                
                # Create steps from the parsed data
                steps = []
                for step_data in steps_data:
                    action = step_data.get("action", "")
                    agent_name = step_data.get("agent", "GenericAgent")
                    
                    # Create the step
                    step = Step(
                        id=str(uuid.uuid4()),
                        plan_id=plan.id,
                        session_id=input_task.session_id,
                        action=action,
                        agent=agent_name,
                        status=StepStatus.planned,
                        human_approval_status=HumanFeedbackStatus.requested
                    )
                    
                    # Store the step
                    await self._memory_store.add_step(step)
                    steps.append(step)
                    
                    track_event_if_configured(
                        "Planner - Added planned individual step into the cosmos",
                        {
                            "plan_id": plan.id,
                            "action": action,
                            "agent": agent_name,
                            "status": StepStatus.planned,
                            "session_id": input_task.session_id,
                            "user_id": input_task.user_id,
                            "human_approval_status": HumanFeedbackStatus.requested,
                        },
                    )
                
                return plan, steps
                
            except json.JSONDecodeError:
                # If JSON parsing fails, use regex to extract steps
                return await self._create_plan_from_text(input_task, response_content)
                
        except Exception as e:
            logging.exception(f"Error creating structured plan: {e}")
            
            track_event_if_configured(
                f"Planner - Error in create_structured_plan: {e} into the cosmos",
                {
                    "session_id": input_task.session_id,
                    "user_id": input_task.user_id,
                    "initial_goal": "Error generating plan",
                    "overall_status": PlanStatus.failed,
                    "source": "PlannerAgent",
                    "summary": f"Error generating plan: {e}",
                },
            )
            
            # Create an error plan
            error_plan = Plan(
                id=str(uuid.uuid4()),
                session_id=input_task.session_id,
                user_id=input_task.user_id,
                initial_goal="Error generating plan",
                overall_status=PlanStatus.failed,
                summary=f"Error generating plan: {str(e)}"
            )
            
            await self._memory_store.add_plan(error_plan)
            return error_plan, []
    
    async def _create_plan_from_text(self, input_task: InputTask, text_content: str) -> Tuple[Plan, List[Step]]:
        """Create a plan from unstructured text when JSON parsing fails.
        
        Args:
            input_task: The input task
            text_content: The text content from the LLM
            
        Returns:
            Tuple containing the created plan and list of steps
        """
        # Extract goal from the text (first line or use input task description)
        goal_match = re.search(r"(?:Goal|Initial Goal|Plan):\s*(.+?)(?:\n|$)", text_content)
        goal = goal_match.group(1).strip() if goal_match else input_task.description
        
        # Create the plan
        plan = Plan(
            id=str(uuid.uuid4()),
            session_id=input_task.session_id,
            user_id=input_task.user_id,
            initial_goal=goal,
            overall_status=PlanStatus.in_progress
        )
        
        # Store the plan
        await self._memory_store.add_plan(plan)
        
        # Parse steps using regex
        step_pattern = re.compile(r'(?:Step|)\s*(\d+)[:.]\s*\*?\*?(?:Agent|):\s*\*?([^:*\n]+)\*?[:\s]*(.+?)(?=(?:Step|)\s*\d+[:.]\s*|$)', re.DOTALL)
        matches = step_pattern.findall(text_content)
        
        if not matches:
            # Fallback to simpler pattern
            step_pattern = re.compile(r'(\d+)[.:\)]\s*([^:]*?):\s*(.*?)(?=\d+[.:\)]|$)', re.DOTALL)
            matches = step_pattern.findall(text_content)
        
        steps = []
        for match in matches:
            number = match[0].strip()
            agent_text = match[1].strip()
            action = match[2].strip()
            
            # Clean up agent name
            agent = re.sub(r'\s+', '', agent_text)
            if not agent or agent not in self._available_agents:
                agent = "GenericAgent"  # Default to GenericAgent if not recognized
                
            # Create and store the step
            step = Step(
                id=str(uuid.uuid4()),
                plan_id=plan.id,
                session_id=input_task.session_id,
                action=action,
                agent=agent,
                status=StepStatus.planned,
                human_approval_status=HumanFeedbackStatus.requested
            )
            
            await self._memory_store.add_step(step)
            steps.append(step)
            
        return plan, steps
    
    def _generate_instruction(self, objective: str) -> str:
        """Generate instruction for the LLM to create a plan.
        
        Args:
            objective: The user's objective
            
        Returns:
            Instruction string for the LLM
        """
        # Create a list of available agents
        agents_str = ", ".join(self._available_agents)
        
        # Create list of available tools
        tools_str = "\n".join(self._agent_tools_list) if self._agent_tools_list else "Various specialized tools"
        
        return f"""
        You are the Planner, an AI orchestrator that manages a group of AI agents to accomplish tasks.

        For the given objective, come up with a simple step-by-step plan.
        This plan should involve individual tasks that, if executed correctly, will yield the correct answer. Do not add any superfluous steps.
        The result of the final step should be the final answer. Make sure that each step has all the information needed - do not skip steps.

        These actions are passed to the specific agent. Make sure the action contains all the information required for the agent to execute the task.

        Your objective is:
        {objective}

        The agents you have access to are:
        {agents_str}

        These agents have access to the following functions:
        {tools_str}

        The first step of your plan should be to ask the user for any additional information required to progress the rest of steps planned.

        Only use the functions provided as part of your plan. If the task is not possible with the agents and tools provided, create a step with the agent of type Exception and mark the overall status as completed.

        Do not add superfluous steps - only take the most direct path to the solution, with the minimum number of steps. Only do the minimum necessary to complete the goal.

        If there is a single function call that can directly solve the task, only generate a plan with a single step. For example, if someone asks to be granted access to a database, generate a plan with only one step involving the grant_database_access function, with no additional steps.

        When generating the action in the plan, frame the action as an instruction you are passing to the agent to execute. It should be a short, single sentence. Include the function to use. For example, "Set up an Office 365 Account for Jessica Smith. Function: set_up_office_365_account"

        Ensure the summary of the plan and the overall steps is less than 50 words.

        Identify any additional information that might be required to complete the task. Include this information in the plan in the human_clarification_request field of the plan. If it is not required, leave it as null. Do not include information that you are waiting for clarification on in the string of the action field, as this otherwise won't get updated.

        You must prioritise using the provided functions to accomplish each step. First evaluate each and every function the agents have access too. Only if you cannot find a function needed to complete the task, and you have reviewed each and every function, and determined why each are not suitable, there are two options you can take when generating the plan.
        First evaluate whether the step could be handled by a typical large language model, without any specialised functions. For example, tasks such as "add 32 to 54", or "convert this SQL code to a python script", or "write a 200 word story about a fictional product strategy".
        If a general Large Language Model CAN handle the step/required action, add a step to the plan with the action you believe would be needed, and add "EXCEPTION: No suitable function found. A generic LLM model is being used for this step." to the end of the action. Assign these steps to the GenericAgent. For example, if the task is to convert the following SQL into python code (SELECT * FROM employees;), and there is no function to convert SQL to python, write a step with the action "convert the following SQL into python code (SELECT * FROM employees;) EXCEPTION: No suitable function found. A generic LLM model is being used for this step." and assign it to the GenericAgent.
        Alternatively, if a general Large Language Model CAN NOT handle the step/required action, add a step to the plan with the action you believe would be needed, and add "EXCEPTION: Human support required to do this step, no suitable function found." to the end of the action. Assign these steps to the HumanAgent. For example, if the task is to find the best way to get from A to B, and there is no function to calculate the best route, write a step with the action "Calculate the best route from A to B. EXCEPTION: Human support required, no suitable function found." and assign it to the HumanAgent.

        Limit the plan to 6 steps or less.

        Choose from {agents_str} ONLY for planning your steps.
        
        Return your response as a JSON object with the following structure:
        {
          "initial_goal": "The goal of the plan",
          "steps": [
            {
              "action": "Detailed description of the step action",
              "agent": "AgentName"
            }
          ],
          "summary_plan_and_steps": "Brief summary of the plan and steps",
          "human_clarification_request": "Any additional information needed from the human" 
        }
        """