"""Revision registration service for immutable rate observations.

Corrections never overwrite an observation: they append a new revision at
the same effective point, linked to the previous revision.
"""

from django.utils import timezone

from apps.currencies.models import RateObservation


def register_correction(
    observation: RateObservation,
    *,
    new_rate,
    reason: str,
    corrected_by=None,
    fetched_at=None,
    derivation=None,
) -> RateObservation:
    """Append a correcting revision for ``observation``.

    The new row keeps the original effective point and provider, links to
    the corrected observation via ``revision_of`` and carries revision
    number ``observation.revision + 1``.
    """
    if not reason:
        raise ValueError("A revision reason is required.")

    # The passed observation is the immediate predecessor, whether or not
    # it is itself a revision.
    predecessor = observation

    correction_derivation = derivation or {
        "kind": "correction",
        "corrected_observation_id": predecessor.id,
        "corrected_by": corrected_by,
    }

    return RateObservation.objects.create(
        from_currency=predecessor.from_currency,
        to_currency=predecessor.to_currency,
        rate=new_rate,
        provider=predecessor.provider,
        service=predecessor.service,
        batch=None,
        fetched_at=fetched_at or timezone.now(),
        effective_at=predecessor.effective_at,
        effective_date=predecessor.effective_date,
        temporal_kind=predecessor.temporal_kind,
        source=RateObservation.Source.CORRECTION,
        revision_of=predecessor,
        revision=predecessor.revision + 1,
        revision_reason=reason,
        derivation=correction_derivation,
    )
