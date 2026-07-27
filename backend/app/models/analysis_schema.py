"""Analysis output schema + closed tagging vocabularies.

One definition serves three roles: Gemini's response_schema (structured
output), the post-response validator, and the documented shape of the
calls.analysis JSONB. The enums ARE the vocabularies — the single source
of truth for what the model may emit.

Field order matters: `reasoning` is defined first so the model produces it
before the tags (structured output is generated in property order), which
forces the classifications to be grounded rather than guessed field-by-field.
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


class ObjectionType(StrEnum):
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


class ObjectionItem(BaseModel):
    type: ObjectionType
    quote: str = Field(description="Short verbatim line from the transcript showing this objection")


class Tags(BaseModel):
    outcome: Outcome
    objections: list[ObjectionItem] = Field(default_factory=list)
    lead_temperature: LeadTemperature


class SpeakerMood(BaseModel):
    label: Mood
    note: str = Field(description="One sentence: the speaker's mood and how it evolved during the call")


class SpeakerMoods(BaseModel):
    agent: SpeakerMood
    customer: SpeakerMood


class AnalysisResult(BaseModel):
    reasoning: str = Field(description="2-4 sentences tying transcript evidence to the tags below; produced first")
    summary: str
    tags: Tags
    intent: Intent
    mood: SpeakerMoods
    next_step: str = Field(description="The single most appropriate follow-up action")
