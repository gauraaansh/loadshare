"""
ARIA — Agent Interface Contract
=================================
All 5 agents subclass BaseAgent and implement run().

Design rules (enforced here, not in subclasses):
  - Agents NEVER compute — algorithms/ and ML server do computation.
  - LLM is called only for synthesis / natural-language explanation.
  - Every agent writes its output to agent_memory via _log_to_db().
  - run() must always return a dict with at minimum:
      status        : 'success' | 'partial' | 'failed'
      summary_text  : one-liner string for the Supervisor's context window
      alert_count   : int
      severity      : 'normal' | 'warning' | 'critical'
"""

import json
import uuid
from abc import ABC, abstractmethod
from typing import Any

import asyncpg
import structlog


class BaseAgent(ABC):

    def __init__(self, conn: asyncpg.Connection, redis):
        self.conn  = conn
        self.redis = redis
        self.log   = structlog.get_logger(agent=self.__class__.__name__)

    @abstractmethod
    async def run(self, cycle_id: str, **kwargs) -> dict[str, Any]:
        """
        Run the agent for one 15-min cycle.

        Args:
            cycle_id: UUID string — correlates all DB rows for this cycle.

        Returns a dict with at minimum:
            status       : 'success' | 'partial' | 'failed'
            summary_text : one-liner for Supervisor context
            alert_count  : int
            severity     : 'normal' | 'warning' | 'critical'
        """
        ...

    async def _log_to_db(
        self,
        cycle_id:     str,
        output:       dict,
        summary:      str,
        execution_ms: int,
        status:       str = "success",
    ) -> None:
        """Write agent output to agent_memory table."""
        try:
            await self.conn.execute(
                """
                INSERT INTO agent_memory
                    (id, agent_name, cycle_id, output_json, summary_text, execution_ms, status)
                VALUES ($1, $2, $3::uuid, $4, $5, $6, $7)
                """,
                str(uuid.uuid4()),
                self.__class__.__name__,
                cycle_id,
                json.dumps(output, default=str),
                summary,
                execution_ms,
                status,
            )
        except Exception as e:
            self.log.warning("agent_memory write failed", error=str(e))
