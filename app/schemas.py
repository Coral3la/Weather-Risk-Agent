"""Pydantic models: the arguments of each tool, and the agent's final answer (PLAN.md §7).

The same models do two jobs:
  1. They are turned into the strict JSON schemas sent to the LLM.
  2. They re-validate what the LLM sends back, before any code acts on it.

Optional arguments are written as "X | None" with no default, because OpenAI's
strict mode requires every field to be present (the LLM sends null to skip one).
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app import config
from app.scoring import load_hubs

# The allowed values come from hubs.json and config.py, so the LLM cannot ask
# for a hub, category, metric or year that doesn't exist.
HubId = Literal[tuple(hub["id"] for hub in load_hubs())]
Region = Literal[tuple(config.REGIONS)]
Category = Literal[tuple(config.CATEGORIES)]
CategoryOrOverall = Literal[tuple([*config.CATEGORIES, config.OVERALL])]
Metric = Literal[tuple(config.WEATHER_METRICS)]
Year = Literal[tuple(config.SCORE_YEARS)]
ToolName = Literal["list_hubs", "rank_hubs", "compare_hubs", "explain_hub_score", "weather_stat",
                   "current_alerts"]


class StrictModel(BaseModel):
    """Rejects fields that aren't in the model."""
    model_config = ConfigDict(extra="forbid")


# --- Tool arguments ----------------------------------------------------------------

class ListHubsArgs(StrictModel):
    region: Region | None = Field(description="Only hubs in this region. null = all hubs.")


class RankHubsArgs(StrictModel):
    category: CategoryOrOverall = Field(description="Hazard category to rank by, or 'overall'.")
    region: Region | None = Field(description="Only hubs in this region. null = all hubs.")
    top_n: int | None = Field(description="Return only the top N hubs. null = all of them.")

    @field_validator("top_n")
    @classmethod
    def top_n_is_positive(cls, value):
        if value is not None and value < 1:
            raise ValueError("top_n must be 1 or more")
        return value


class CompareHubsArgs(StrictModel):
    hub_ids: list[HubId] = Field(description="Two or more hubs to compare.")
    categories: list[Category] | None = Field(
        description="Categories to compare. null = all five categories."
    )

    @field_validator("hub_ids")
    @classmethod
    def at_least_two_hubs(cls, value):
        if len(set(value)) < 2:
            raise ValueError("compare_hubs needs at least two different hubs")
        return value


class ExplainHubScoreArgs(StrictModel):
    hub_id: HubId


class WeatherStatArgs(StrictModel):
    hub_id: HubId
    metric: Metric = Field(description="Which kind of day to count.")
    year: Year = Field(description="Calendar year.")


class CurrentAlertsArgs(StrictModel):
    hub_ids: list[HubId] | None = Field(description="Only these hubs. null = all hubs.")
    region: Region | None = Field(description="Only hubs in this region. null = all hubs.")


# --- The agent's final answer -------------------------------------------------------

class HubRef(StrictModel):
    hub_id: str
    name: str
    score: float | None = Field(
        description="null if the answer covers two or more categories, is about NWS alerts, "
                    "or has no score. "
                    "Otherwise the hub's score for the single category (or overall) the "
                    "answer is about, copied from a tool result."
    )


class KeyFigure(StrictModel):
    label: str
    value: float = Field(description="Copied exactly from a tool result.")
    unit: str = Field(description="e.g. 'score (0-100)', '% of days', 'days', 'days per year'.")
    source_tool: ToolName = Field(description="The tool whose result contains this value.")


class AgentAnswer(StrictModel):
    answer: str = Field(
        description="The text shown to the user. Describes scores and metrics only with what "
                    "the tool results state. For 'why is X high/low' questions, starts with "
                    "the hub's overall rank and score."
    )
    hubs: list[HubRef] = Field(description="The hubs this answer is about. Empty if none.")
    key_figures: list[KeyFigure] = Field(description="The numbers the answer relies on.")
    assumptions: list[str] = Field(description="Definitions and assumptions behind the answer.")
    confidence: Literal["high", "medium", "low"]
    confidence_reason: str
    out_of_scope: bool = Field(description="True if the question can't be answered from our hubs and data.")
