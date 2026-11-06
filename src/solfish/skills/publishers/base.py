"""What a publisher gives back, shared by the content skill and the platform modules."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Delivery:
    """Where a post went (a URL or a path). final=False means the user still has to publish it."""

    location: str
    final: bool = True
    next_step: str = ""  # what the user should do, when it isn't just "upload from this folder"


class MaybePublished(Exception):
    """The platform may have taken the post (say, a timeout on the final request). Retrying could
    post it twice, so this leaves the post for a person to check rather than marking it failed."""
