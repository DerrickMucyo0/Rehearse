"""Pure pause-metrics-v1 facts from validated original-transcription timings.

A pause is an operational inter-word gap of at least 0.50 seconds, not verified
acoustic silence or hesitation. Endpoints use Decimal(str(timestamp)); the full
qualifying gap contributes to the total. No audio, events, content, provider or
database state is retained by the immutable scalar result.
"""

from collections.abc import Sequence
from decimal import Context, Decimal, Inexact, MAX_EMAX, MIN_EMIN, ROUND_HALF_EVEN, Rounded, localcontext
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.speaking_metrics import (
    RecognizedWordTiming, TimingUnavailableReason, validate_transcription_timings,
)

PAUSE_METRICS_VERSION = "pause-metrics-v1"
PAUSE_THRESHOLD_SECONDS = Decimal("0.50")


class DeliveryMetrics(BaseModel):
    """Measured zero and unavailable timing are distinct, closed result states."""

    model_config = ConfigDict(strict=True, extra="forbid", frozen=True, allow_inf_nan=False)

    version: Literal["pause-metrics-v1"] = PAUSE_METRICS_VERSION
    pause_count: Annotated[int, Field(ge=0)] | None
    total_pause_duration_seconds: Annotated[float, Field(ge=0)] | None
    longest_pause_seconds: Annotated[float, Field(ge=0)] | None
    unavailable_reason: TimingUnavailableReason | None

    @model_validator(mode="after")
    def consistent_availability(self):
        available = self.unavailable_reason is None
        values = (self.pause_count, self.total_pause_duration_seconds, self.longest_pause_seconds)
        if any((value is not None) != available for value in values):
            raise ValueError("Inconsistent pause availability.")
        if available:
            if self.pause_count == 0:
                if self.total_pause_duration_seconds != 0 or self.longest_pause_seconds != 0:
                    raise ValueError("Zero pauses require zero durations.")
            elif (self.total_pause_duration_seconds <= 0 or self.longest_pause_seconds <= 0 or
                  self.longest_pause_seconds > self.total_pause_duration_seconds):
                raise ValueError("Inconsistent pause durations.")
        return self


def _exact_decimal_context(words: Sequence[RecognizedWordTiming]) -> Context:
    # Cover every endpoint's integer/fractional places plus carry digits for a
    # sum of adjacent gaps. A fresh context excludes caller precision/traps.
    minimum_exponent = 0
    maximum_adjusted = 0
    for word in words:
        for timestamp in (word.start, word.end):
            endpoint = Decimal(str(timestamp))
            minimum_exponent = min(minimum_exponent, endpoint.as_tuple().exponent)
            maximum_adjusted = max(maximum_adjusted, endpoint.adjusted())
    return Context(
        prec=maximum_adjusted - minimum_exponent + 2 + len(str(len(words))),
        Emin=MIN_EMIN, Emax=MAX_EMAX, rounding=ROUND_HALF_EVEN,
        capitals=1, clamp=0, flags=[], traps=[Inexact, Rounded],
    )


def measure_delivery(text: str, words: Sequence[RecognizedWordTiming]) -> DeliveryMetrics:
    """Validate once, then examine adjacent lexical gaps in O(n), without repair.

    Speaking-v1 validation (including usable span/WPM) remains authoritative.
    Decimal arithmetic neither rounds nor quantizes endpoints, gaps or sums.
    Only final aggregate durations become floats for the application layer.
    """
    validated, unavailable = validate_transcription_timings(text, words)
    if validated is None:
        return DeliveryMetrics(
            pause_count=None, total_pause_duration_seconds=None,
            longest_pause_seconds=None, unavailable_reason=unavailable,
        )

    count = 0
    total = Decimal(0)
    longest = Decimal(0)
    with localcontext(_exact_decimal_context(validated.words)):
        lexical_words = iter(validated.words)
        previous_end = Decimal(str(next(lexical_words).end))
        for word in lexical_words:
            gap = Decimal(str(word.start)) - previous_end
            if gap >= PAUSE_THRESHOLD_SECONDS:
                count += 1
                total += gap
                longest = max(longest, gap)
            previous_end = Decimal(str(word.end))
    return DeliveryMetrics(
        pause_count=count, total_pause_duration_seconds=float(total),
        longest_pause_seconds=float(longest), unavailable_reason=None,
    )
