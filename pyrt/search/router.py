"""The simple search page: ``/search?q=`` (plan §8, FP S01-S05).

One route. The box that feeds it is the shell's, at the top of every page
(``base.html``), so a search is always one field away; this module only
answers it. A bare ticket number is not a search at all: it is the ticket,
and the answer is a redirect to it.
"""

from __future__ import annotations

from typing import Annotated, Final

from fastapi import APIRouter, Query, Request
from starlette.responses import RedirectResponse, Response

from pyrt.search import service
from pyrt.web.deps import ActorPrincipals, Config, Db, SignedIn
from pyrt.web.errors import SEE_OTHER
from pyrt.web.templating import absolute_url, render

router = APIRouter()

#: Plan §8's heading for the results page; the walk expects it verbatim.
RESULTS_HEADING: Final = "Search results"

#: The empty state (FP S05).
NO_RESULTS: Final = "No tickets found"

#: The columns of the results table, in order (FP S04).
COLUMNS: Final[tuple[str, ...]] = ("Id", "Subject", "Status", "Queue", "Owner", "Created")


@router.get("/search", include_in_schema=False)
def search(
    request: Request,
    db: Db,
    settings: Config,
    actor: SignedIn,
    held: ActorPrincipals,
    q: Annotated[str, Query()] = "",
) -> Response:
    """FP S01-S05: the results table, or the ticket a bare number names."""
    query = service.parse(q)
    if query.ticket_id is not None:
        return RedirectResponse(
            absolute_url(settings, f"/ticket/{query.ticket_id}"), status_code=SEE_OTHER
        )
    rows = service.results(db, held, query, actor.id)
    return render(
        request,
        "search/results.html",
        {
            "page_title": RESULTS_HEADING,
            "query": q,
            "columns": COLUMNS,
            "rows": rows,
            "no_results": NO_RESULTS,
        },
    )
