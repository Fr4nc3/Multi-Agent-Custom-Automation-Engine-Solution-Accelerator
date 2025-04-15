# cosmos_memory_kernel.py

import asyncio
import logging
import uuid
from typing import Any, Dict, List, Optional, Type, Tuple
import numpy as np

from azure.cosmos.partition_key import PartitionKey
from semantic_kernel.memory.memory_record import MemoryRecord
from semantic_kernel.memory.memory_store_base import MemoryStoreBase
from semantic_kernel.contents import ChatMessageContent, ChatHistory, AuthorRole

from config_kernel import Config
from models.messages_kernel import BaseDataModel, Plan, Session, Step, AgentMessage
from context.in_memory_context import InMemoryContext


class CosmosMemoryContext(MemoryStoreBase):
    """A buffered chat completion context that also saves messages and data models to Cosmos DB or in-memory fallback."""

    MODEL_CLASS_MAPPING = {
        "session": Session,
        "plan": Plan,
        "step": Step,
        "agent_message": AgentMessage,
        # Messages are handled separately
    }

    def __init__(
        self,
        session_id: str,
        user_id: str,
        buffer_size: int = 100,
        initial_messages: Optional[List[ChatMessageContent]] = None,
    ) -> None:
        self._buffer_size = buffer_size
        self._messages = initial_messages or []
        self._cosmos_container = Config.COSMOSDB_CONTAINER
        self._database = Config.GetCosmosDatabaseClient()
        self._container = None
        self.session_id = session_id
        self.user_id = user_id
        self._initialized = asyncio.Event()
        self._in_memory_context = None
        
        # Auto-initialize the container
        asyncio.create_task(self.initialize())

    async def initialize(self):
        """Initialize the memory context - either using CosmosDB or in-memory alternative."""
        try:
            if self._database is not None:
                # Try to use real CosmosDB
                self._container = await self._database.create_container_if_not_exists(
                    id=self._cosmos_container,
                    partition_key=PartitionKey(path="/session_id"),
                )
                logging.info("Successfully connected to CosmosDB")
            else:
                # Use in-memory alternative
                self._in_memory_context = InMemoryContext(
                    session_id=self.session_id,
                    user_id=self.user_id,
                    buffer_size=self._buffer_size,
                    initial_messages=self._messages
                )
                logging.info("Using InMemoryContext as fallback")
        except Exception as e:
            logging.warning(f"Failed to initialize CosmosDB container: {e}. Using InMemoryContext as fallback.")
            self._in_memory_context = InMemoryContext(
                session_id=self.session_id,
                user_id=self.user_id,
                buffer_size=self._buffer_size,
                initial_messages=self._messages
            )
        
        self._initialized.set()

    # Helper method to delegate to in-memory context if needed
    async def _delegate(self, method_name, *args, **kwargs):
        """Delegate a method call to in-memory context if CosmosDB is not available."""
        await self._initialized.wait()
        
        if self._in_memory_context is not None:
            method = getattr(self._in_memory_context, method_name)
            return await method(*args, **kwargs)
        
        # If we reach here, we're using CosmosDB
        return None

    async def add_item(self, item: BaseDataModel) -> None:
        """Add a data model item to Cosmos DB."""
        await self._initialized.wait()
        if self._in_memory_context:
            await self._delegate("add_item", item)
            return

        try:
            document = item.model_dump()
            await self._container.create_item(body=document)
            logging.info(f"Item added to Cosmos DB - {document['id']}")
        except Exception as e:
            logging.exception(f"Failed to add item to Cosmos DB: {e}")

    async def update_item(self, item: BaseDataModel) -> None:
        """Update an existing item in Cosmos DB."""
        await self._initialized.wait()
        if self._in_memory_context:
            await self._delegate("update_item", item)
            return

        try:
            document = item.model_dump()
            await self._container.upsert_item(body=document)
        except Exception as e:
            logging.exception(f"Failed to update item in Cosmos DB: {e}")

    async def get_item_by_id(
        self, item_id: str, partition_key: str, model_class: Type[BaseDataModel]
    ) -> Optional[BaseDataModel]:
        """Retrieve an item by its ID and partition key."""
        await self._initialized.wait()
        if self._in_memory_context:
            return await self._delegate("get_item_by_id", item_id, partition_key, model_class)

        try:
            item = await self._container.read_item(
                item=item_id, partition_key=partition_key
            )
            return model_class.model_validate(item)
        except Exception as e:
            logging.exception(f"Failed to retrieve item from Cosmos DB: {e}")
            return None

    async def query_items(
        self,
        query: str,
        parameters: List[Dict[str, Any]],
        model_class: Type[BaseDataModel],
    ) -> List[BaseDataModel]:
        """Query items from Cosmos DB and return a list of model instances."""
        await self._initialized.wait()
        if self._in_memory_context:
            return await self._delegate("query_items", query, parameters, model_class)

        try:
            items = self._container.query_items(query=query, parameters=parameters)
            result_list = []
            async for item in items:
                item["ts"] = item["_ts"]
                result_list.append(model_class.model_validate(item))
            return result_list
        except Exception as e:
            logging.exception(f"Failed to query items from Cosmos DB: {e}")
            return []

    # Methods to add and retrieve Sessions, Plans, and Steps

    async def add_session(self, session: Session) -> None:
        """Add a session to Cosmos DB."""
        await self.add_item(session)

    async def get_session(self, session_id: str) -> Optional[Session]:
        """Retrieve a session by session_id."""
        query = "SELECT * FROM c WHERE c.id=@id AND c.data_type=@data_type"
        parameters = [
            {"name": "@id", "value": session_id},
            {"name": "@data_type", "value": "session"},
        ]
        sessions = await self.query_items(query, parameters, Session)
        return sessions[0] if sessions else None

    async def get_all_sessions(self) -> List[Session]:
        """Retrieve all sessions."""
        query = "SELECT * FROM c WHERE c.data_type=@data_type"
        parameters = [
            {"name": "@data_type", "value": "session"},
        ]
        sessions = await self.query_items(query, parameters, Session)
        return sessions

    async def add_plan(self, plan: Plan) -> None:
        """Add a plan to Cosmos DB."""
        await self.add_item(plan)

    async def update_plan(self, plan: Plan) -> None:
        """Update an existing plan in Cosmos DB."""
        await self.update_item(plan)

    async def get_plan_by_session(self, session_id: str) -> Optional[Plan]:
        """Retrieve a plan associated with a session."""
        query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.user_id=@user_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@session_id", "value": session_id},
            {"name": "@data_type", "value": "plan"},
            {"name": "@user_id", "value": self.user_id},
        ]
        plans = await self.query_items(query, parameters, Plan)
        return plans[0] if plans else None

    async def get_plan(self, plan_id: str) -> Optional[Plan]:
        """Retrieve a plan by its ID."""
        query = "SELECT * FROM c WHERE c.id=@id AND c.data_type=@data_type"
        parameters = [
            {"name": "@id", "value": plan_id},
            {"name": "@data_type", "value": "plan"},
        ]
        plans = await self.query_items(query, parameters, Plan)
        return plans[0] if plans else None

    async def get_all_plans(self) -> List[Plan]:
        """Retrieve all plans."""
        query = "SELECT * FROM c WHERE c.user_id=@user_id AND c.data_type=@data_type ORDER BY c._ts DESC OFFSET 0 LIMIT 5"
        parameters = [
            {"name": "@data_type", "value": "plan"},
            {"name": "@user_id", "value": self.user_id},
        ]
        plans = await self.query_items(query, parameters, Plan)
        return plans

    async def add_step(self, step: Step) -> None:
        """Add a step to Cosmos DB."""
        await self.add_item(step)

    async def update_step(self, step: Step) -> None:
        """Update an existing step in Cosmos DB."""
        await self.update_item(step)

    async def get_steps_for_plan(self, plan_id: str, session_id: Optional[str] = None) -> List[Step]:
        """Retrieve all steps associated with a plan.
        
        Args:
            plan_id: The ID of the plan to retrieve steps for
            session_id: Optional session ID if known
            
        Returns:
            List of Step objects
        """
        query = "SELECT * FROM c WHERE c.plan_id=@plan_id AND c.user_id=@user_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@plan_id", "value": plan_id},
            {"name": "@data_type", "value": "step"},
            {"name": "@user_id", "value": self.user_id},
        ]
        steps = await self.query_items(query, parameters, Step)
        return steps

    async def get_step(self, step_id: str, session_id: str) -> Optional[Step]:
        """Retrieve a step by its ID.
        
        Args:
            step_id: The ID of the step to retrieve
            session_id: The session ID this step belongs to
            
        Returns:
            Step object if found, None otherwise
        """
        query = "SELECT * FROM c WHERE c.id=@id AND c.session_id=@session_id AND c.data_type=@data_type"
        parameters = [
            {"name": "@id", "value": step_id},
            {"name": "@session_id", "value": session_id},
            {"name": "@data_type", "value": "step"},
        ]
        steps = await self.query_items(query, parameters, Step)
        return steps[0] if steps else None

    async def add_agent_message(self, message: AgentMessage) -> None:
        """Add an agent message to Cosmos DB.
        
        Args:
            message: The AgentMessage to add
        """
        await self.add_item(message)

    async def get_agent_messages_by_session(self, session_id: str) -> List[AgentMessage]:
        """Retrieve agent messages for a specific session.
        
        Args:
            session_id: The session ID to get messages for
            
        Returns:
            List of AgentMessage objects
        """
        query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.data_type=@data_type ORDER BY c._ts ASC"
        parameters = [
            {"name": "@session_id", "value": session_id},
            {"name": "@data_type", "value": "agent_message"},
        ]
        messages = await self.query_items(query, parameters, AgentMessage)
        return messages

    # Methods for messages - adapted for Semantic Kernel

    async def add_message(self, message: ChatMessageContent) -> None:
        """Add a message to the memory and save to Cosmos DB."""
        await self._initialized.wait()
        if self._in_memory_context:
            await self._delegate("add_message", message)
            return

        if self._container is None:
            return

        try:
            self._messages.append(message)
            # Ensure buffer size is maintained
            while len(self._messages) > self._buffer_size:
                self._messages.pop(0)
                
            message_dict = {
                "id": str(uuid.uuid4()),
                "session_id": self.session_id,
                "user_id": self.user_id,
                "data_type": "message",
                "content": {
                    "role": message.role.value,
                    "content": message.content,
                    "metadata": message.metadata
                },
                "source": message.metadata.get("source", ""),
            }
            await self._container.create_item(body=message_dict)
        except Exception as e:
            logging.exception(f"Failed to add message to Cosmos DB: {e}")

    async def get_messages(self) -> List[ChatMessageContent]:
        """Get recent messages for the session."""
        await self._initialized.wait()
        if self._in_memory_context:
            return await self._delegate("get_messages")

        if self._container is None:
            return []

        try:
            query = """
                SELECT * FROM c
                WHERE c.session_id=@session_id AND c.data_type=@data_type
                ORDER BY c._ts ASC
                OFFSET 0 LIMIT @limit
            """
            parameters = [
                {"name": "@session_id", "value": self.session_id},
                {"name": "@data_type", "value": "message"},
                {"name": "@limit", "value": self._buffer_size},
            ]
            items = self._container.query_items(
                query=query,
                parameters=parameters,
            )
            messages = []
            async for item in items:
                content = item.get("content", {})
                role = content.get("role", "user")
                chat_role = AuthorRole.ASSISTANT
                if role == "user":
                    chat_role = AuthorRole.USER
                elif role == "system":
                    chat_role = AuthorRole.SYSTEM
                elif role == "tool":  # Equivalent to FunctionExecutionResultMessage
                    chat_role = AuthorRole.TOOL
                
                message = ChatMessageContent(
                    role=chat_role,
                    content=content.get("content", ""),
                    metadata=content.get("metadata", {})
                )
                messages.append(message)
            return messages
        except Exception as e:
            logging.exception(f"Failed to load messages from Cosmos DB: {e}")
            return []

    # ChatHistory compatibility methods
    
    def get_chat_history(self) -> ChatHistory:
        """Convert the buffered messages to a ChatHistory object."""
        history = ChatHistory()
        for message in self._messages:
            history.add_message(message)
        return history
    
    async def save_chat_history(self, history: ChatHistory) -> None:
        """Save a ChatHistory object to the store."""
        for message in history.messages:
            await self.add_message(message)
    
    # MemoryStore interface methods
    
    async def upsert_memory_record(self, collection: str, record: MemoryRecord) -> str:
        """Implement MemoryStore interface - store a memory record."""
        memory_dict = {
            "id": record.id or str(uuid.uuid4()),
            "session_id": self.session_id,
            "user_id": self.user_id,
            "data_type": "memory",
            "collection": collection,
            "text": record.text,
            "description": record.description,
            "external_source_name": record.external_source_name,
            "additional_metadata": record.additional_metadata,
            "embedding": record.embedding.tolist() if record.embedding is not None else None,
            "key": record.key
        }
        
        await self._container.upsert_item(body=memory_dict)
        return memory_dict["id"]
    
    async def get_memory_record(self, collection: str, key: str, with_embedding: bool = False) -> Optional[MemoryRecord]:
        """Implement MemoryStore interface - retrieve a memory record."""
        query = """
            SELECT * FROM c 
            WHERE c.collection=@collection AND c.key=@key AND c.session_id=@session_id AND c.data_type=@data_type
        """
        parameters = [
            {"name": "@collection", "value": collection},
            {"name": "@key", "value": key},
            {"name": "@session_id", "value": self.session_id},
            {"name": "@data_type", "value": "memory"}
        ]
        
        items = self._container.query_items(query=query, parameters=parameters)
        async for item in items:
            return MemoryRecord(
                id=item["id"],
                text=item["text"],
                description=item["description"],
                external_source_name=item["external_source_name"],
                additional_metadata=item["additional_metadata"],
                embedding=np.array(item["embedding"]) if with_embedding and "embedding" in item else None,
                key=item["key"]
            )
        return None
    
    async def remove_memory_record(self, collection: str, key: str) -> None:
        """Implement MemoryStore interface - remove a memory record."""
        query = """
            SELECT c.id FROM c 
            WHERE c.collection=@collection AND c.key=@key AND c.session_id=@session_id AND c.data_type=@data_type
        """
        parameters = [
            {"name": "@collection", "value": collection},
            {"name": "@key", "value": key},
            {"name": "@session_id", "value": self.session_id},
            {"name": "@data_type", "value": "memory"}
        ]
        
        items = self._container.query_items(query=query, parameters=parameters)
        async for item in items:
            await self._container.delete_item(item=item["id"], partition_key=self.session_id)

    # Generic method to get data by type

    async def get_data_by_type(self, data_type: str) -> List[BaseDataModel]:
        """Query the Cosmos DB for documents with the matching data_type, session_id and user_id."""
        await self._initialized.wait()
        if self._container is None:
            return []

        model_class = self.MODEL_CLASS_MAPPING.get(data_type, BaseDataModel)
        try:
            query = "SELECT * FROM c WHERE c.session_id=@session_id AND c.user_id=@user_id AND c.data_type=@data_type ORDER BY c._ts ASC"
            parameters = [
                {"name": "@session_id", "value": self.session_id},
                {"name": "@data_type", "value": data_type},
                {"name": "@user_id", "value": self.user_id},
            ]
            return await self.query_items(query, parameters, model_class)
        except Exception as e:
            logging.exception(f"Failed to query data by type from Cosmos DB: {e}")
            return []

    # Additional utility methods

    async def delete_item(self, item_id: str, partition_key: str) -> None:
        """Delete an item from Cosmos DB."""
        await self._initialized.wait()
        try:
            await self._container.delete_item(item=item_id, partition_key=partition_key)
        except Exception as e:
            logging.exception(f"Failed to delete item from Cosmos DB: {e}")

    async def delete_items_by_query(
        self, query: str, parameters: List[Dict[str, Any]]
    ) -> None:
        """Delete items matching the query."""
        await self._initialized.wait()
        try:
            items = self._container.query_items(query=query, parameters=parameters)
            async for item in items:
                item_id = item["id"]
                partition_key = item.get("session_id", None)
                await self._container.delete_item(
                    item=item_id, partition_key=partition_key
                )
        except Exception as e:
            logging.exception(f"Failed to delete items from Cosmos DB: {e}")

    async def delete_all_items(self, data_type) -> None:
        """Delete all items of a specific type from Cosmos DB."""
        query = "SELECT c.id, c.session_id FROM c WHERE c.data_type=@data_type AND c.user_id=@user_id"
        parameters = [
            {"name": "@data_type", "value": data_type},
            {"name": "@user_id", "value": self.user_id},
        ]
        await self.delete_items_by_query(query, parameters)

    async def get_all_items(self) -> List[Dict[str, Any]]:
        """Retrieve all items from Cosmos DB."""
        await self._initialized.wait()
        if self._container is None:
            return []

        try:
            messages_list = []
            query = "SELECT * FROM c WHERE c.user_id=@user_id OFFSET 0 LIMIT @limit"
            parameters = [
                {"name": "@user_id", "value": self.user_id},
                {"name": "@limit", "value": 100}
            ]
            items = self._container.query_items(query=query, parameters=parameters)
            async for item in items:
                messages_list.append(item)
            return messages_list
        except Exception as e:
            logging.exception(f"Failed to get items from Cosmos DB: {e}")
            return []

    async def close(self) -> None:
        """Close the Cosmos DB client."""
        pass  # Implement if needed for Semantic Kernel

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        await self.close()

    def __del__(self):
        asyncio.create_task(self.close())

    # Additional required MemoryStoreBase methods
    
    async def create_collection(self, collection_name: str) -> None:
        """Create a new collection. For CosmosDB, we don't need to create new collections
        as everything is stored in the same container with type identifiers."""
        await self._initialized.wait()
        # No-op for CosmosDB implementation - we use the data_type field instead
        pass

    async def get_collections(self) -> List[str]:
        """Get all collections."""
        await self._initialized.wait()
        
        try:
            query = """
                SELECT DISTINCT c.collection 
                FROM c 
                WHERE c.data_type = 'memory' AND c.session_id = @session_id
            """
            parameters = [{"name": "@session_id", "value": self.session_id}]
            
            items = self._container.query_items(query=query, parameters=parameters)
            collections = []
            async for item in items:
                if "collection" in item and item["collection"] not in collections:
                    collections.append(item["collection"])
            return collections
        except Exception as e:
            logging.exception(f"Failed to get collections from Cosmos DB: {e}")
            return []

    async def does_collection_exist(self, collection_name: str) -> bool:
        """Check if a collection exists."""
        collections = await self.get_collections()
        return collection_name in collections

    async def delete_collection(self, collection_name: str) -> None:
        """Delete a collection."""
        await self._initialized.wait()
        
        try:
            query = """
                SELECT c.id, c.session_id
                FROM c
                WHERE c.collection = @collection AND c.data_type = 'memory' AND c.session_id = @session_id
            """
            parameters = [
                {"name": "@collection", "value": collection_name},
                {"name": "@session_id", "value": self.session_id}
            ]
            
            items = self._container.query_items(query=query, parameters=parameters)
            async for item in items:
                await self._container.delete_item(
                    item=item["id"],
                    partition_key=item["session_id"]
                )
        except Exception as e:
            logging.exception(f"Failed to delete collection from Cosmos DB: {e}")

    async def upsert_async(self, collection_name: str, record: Dict[str, Any]) -> str:
        """Helper method to insert documents directly."""
        await self._initialized.wait()
        
        try:
            # Make sure record has the session_id for partitioning
            if "session_id" not in record:
                record["session_id"] = self.session_id
                
            # Ensure record has an ID
            if "id" not in record:
                record["id"] = str(uuid.uuid4())
                
            await self._container.upsert_item(body=record)
            return record["id"]
        except Exception as e:
            logging.exception(f"Failed to upsert item to Cosmos DB: {e}")
            return ""
            
    async def get_memory_records(
        self, collection: str, limit: int = 1000, with_embeddings: bool = False
    ) -> List[MemoryRecord]:
        """Get memory records from a collection."""
        await self._initialized.wait()
        
        try:
            query = """
                SELECT *
                FROM c
                WHERE c.collection = @collection 
                AND c.data_type = 'memory'
                AND c.session_id = @session_id
                ORDER BY c._ts DESC
                OFFSET 0 LIMIT @limit
            """
            parameters = [
                {"name": "@collection", "value": collection},
                {"name": "@session_id", "value": self.session_id},
                {"name": "@limit", "value": limit}
            ]
            
            items = self._container.query_items(query=query, parameters=parameters)
            records = []
            async for item in items:
                embedding = None
                if with_embeddings and "embedding" in item and item["embedding"]:
                    embedding = np.array(item["embedding"])
                    
                record = MemoryRecord(
                    id=item["id"],
                    key=item.get("key", ""),
                    text=item.get("text", ""),
                    embedding=embedding,
                    description=item.get("description", ""),
                    additional_metadata=item.get("additional_metadata", ""),
                    external_source_name=item.get("external_source_name", "")
                )
                records.append(record)
            return records
        except Exception as e:
            logging.exception(f"Failed to get memory records from Cosmos DB: {e}")
            return []

    # Required abstract methods from MemoryStoreBase

    async def upsert(self, collection_name: str, record: MemoryRecord) -> str:
        """Upsert a memory record into the store."""
        return await self.upsert_memory_record(collection_name, record)

    async def upsert_batch(self, collection_name: str, records: List[MemoryRecord]) -> List[str]:
        """Upsert a batch of memory records into the store."""
        result_ids = []
        for record in records:
            record_id = await self.upsert_memory_record(collection_name, record)
            result_ids.append(record_id)
        return result_ids

    async def get(self, collection_name: str, key: str, with_embedding: bool = False) -> MemoryRecord:
        """Get a memory record from the store."""
        return await self.get_memory_record(collection_name, key, with_embedding)

    async def get_batch(self, collection_name: str, keys: List[str], with_embeddings: bool = False) -> List[MemoryRecord]:
        """Get a batch of memory records from the store."""
        results = []
        for key in keys:
            record = await self.get_memory_record(collection_name, key, with_embeddings)
            if record:
                results.append(record)
        return results

    async def remove(self, collection_name: str, key: str) -> None:
        """Remove a memory record from the store."""
        await self.remove_memory_record(collection_name, key)

    async def remove_batch(self, collection_name: str, keys: List[str]) -> None:
        """Remove a batch of memory records from the store."""
        for key in keys:
            await self.remove_memory_record(collection_name, key)

    async def get_nearest_match(
        self, 
        collection_name: str, 
        embedding: np.ndarray, 
        limit: int = 1, 
        min_relevance_score: float = 0.0, 
        with_embeddings: bool = False
    ) -> Tuple[MemoryRecord, float]:
        """Get the nearest match to the given embedding."""
        matches = await self.get_nearest_matches(
            collection_name, 
            embedding, 
            limit, 
            min_relevance_score, 
            with_embeddings
        )
        return matches[0] if matches else (None, 0.0)

    async def get_nearest_matches(
        self, 
        collection_name: str, 
        embedding: np.ndarray, 
        limit: int = 1, 
        min_relevance_score: float = 0.0, 
        with_embeddings: bool = False
    ) -> List[Tuple[MemoryRecord, float]]:
        """Get the nearest matches to the given embedding."""
        await self._initialized.wait()
        
        try:
            # Get all memory records from the collection
            records = await self.get_memory_records(collection_name, limit=100, with_embeddings=True)
            
            # Compute cosine similarity with each record and sort
            results = []
            for record in records:
                if record.embedding is not None:
                    # Compute cosine similarity between the query and each record
                    similarity = np.dot(embedding, record.embedding) / (
                        np.linalg.norm(embedding) * np.linalg.norm(record.embedding)
                    )
                    
                    if similarity >= min_relevance_score:
                        # If we don't need the embeddings in the results, set them to None
                        if not with_embeddings:
                            record.embedding = None
                        results.append((record, float(similarity)))
            
            # Sort by similarity (descending) and limit the results
            results.sort(key=lambda x: x[1], reverse=True)
            return results[:limit]
        except Exception as e:
            logging.exception(f"Failed to get nearest matches from Cosmos DB: {e}")
            return []