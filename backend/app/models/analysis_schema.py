"""Analysis output schema + closed tagging vocabularies.

One definition serves three roles: Gemini's response_schema (structured
output), the post-response validator, and the documented shape of the
calls.analysis JSONB. The enums ARE the vocabularies — the single source
of truth for what the model may emit.
"""

from enum import StrEnum

from pydantic import BaseModel, Field


class Outcome(StrEnum):
    MEETING_SCHEDULED = "meeting_scheduled"
    INFO_REQUESTED = "info_requested"
    NOT_INTERESTED = "not_interested"
    NOT_QUALIFIED = "not_qualified"
    CLOSED_WON = "closed_won"
    NO_CLEAR_OUTCOME = "no_clear_outcome"


class Objection(StrEnum):
    PRICE = "price"
    TIMING = "timing"
    AUTHORITY = "authority"
    NEED = "need"
    TRUST = "trust"
    COMPETITOR = "competitor"


class LeadTemperature(StrEnum):
    COLD = "cold"
    WARM = "warm"
    HOT = "hot"


class Intent(StrEnum):
    READY_TO_BUY = "ready_to_buy"
    EVALUATING = "evaluating"
    GATHERING_INFO = "gathering_info"
    PRICE_SHOPPING = "price_shopping"
    NOT_INTERESTED = "not_interested"


class Mood(StrEnum):
    POSITIVE = "positive"
    NEUTRAL = "neutral"
    NEGATIVE = "negative"
    FRUSTRATED = "frustrated"


class Tags(BaseModel):
    outcome: Outcome
    objections: list[Objection] = Field(default_factory=list)
    lead_temperature: LeadTemperature


class SpeakerMood(BaseModel):
    agent: Mood
    customer: Mood


class AnalysisResult(BaseModel):
    summary: str
    tags: Tags
    intent: Intent
    mood: SpeakerMood
