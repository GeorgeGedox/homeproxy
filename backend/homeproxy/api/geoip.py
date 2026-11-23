"""GeoIP endpoints: the global block list, the age and credit of the data, and a refresh job.

The block list is staged in the pending change set and takes effect on the next apply. Route block
lists are route fields, edited with the route. The MaxMind license key is never returned.
"""

from fastapi import APIRouter, Response
from pydantic import BaseModel

from homeproxy.api.apply import ServicesDep
from homeproxy.api.changes import edit_lock
from homeproxy.api.countries import check_added_countries
from homeproxy.api.settings import merge_section
from homeproxy.auth.routes import Runtime
from homeproxy.geoip.data import GeoipData, load_data_info
from homeproxy.geoip.sources import ATTRIBUTION, Attribution, Provider
from homeproxy.jobs import Job
from homeproxy.jobs.geoip import KIND, refresh
from homeproxy.models import StrictModel
from homeproxy.models.fields import CountryCode
from homeproxy.store import validate_config
from homeproxy.store.pending import CONFIG_FILE, PendingChanges

router = APIRouter(prefix="/api/v1", tags=["geoip"])


class GeoipState(BaseModel):
    """What the GeoIP page shows.

    `provider` and `block_countries` are the settings with pending edits applied. `data` describes
    the map in use, or is null before the first download. `attribution` credits the source of that
    map, or the configured provider before the first download, and must be shown with a link.
    """

    provider: Provider
    block_countries: list[CountryCode]
    data: GeoipData | None
    attribution: Attribution


class BlockList(StrictModel):
    """The countries blocked on every public route."""

    block_countries: list[CountryCode]


@router.get("/geoip", response_model=GeoipState)
def get_geoip(runtime: Runtime) -> GeoipState:
    """Return the global block list, the data age and the credit the data's license requires."""
    store = runtime.store
    with edit_lock(store) as held:
        geoip = validate_config(PendingChanges(store).read_tree(CONFIG_FILE, held)).geoip
    data = load_data_info(store.root)
    return GeoipState(
        provider=geoip.provider,
        block_countries=geoip.block_countries,
        data=data,
        attribution=ATTRIBUTION[data.provider if data else geoip.provider],
    )


@router.put(
    "/geoip",
    status_code=204,
    response_class=Response,
    responses={
        422: {
            "description": "A country code is not two letters, or the GeoIP data does not have "
            "it, or there is no GeoIP data"
        }
    },
)
def put_geoip(body: BlockList, runtime: Runtime, services: ServicesDep) -> Response:
    """Stage a new global block list. Nothing changes until the next apply."""
    store = runtime.store
    pending = PendingChanges(store)
    with edit_lock(store) as held:
        tree = pending.read_tree(CONFIG_FILE, held)
        current = validate_config(tree).geoip
        check_added_countries(
            services.jobs,
            store.root,
            "block_countries",
            current.block_countries,
            body.block_countries,
        )
        new = current.model_copy(update={"block_countries": body.block_countries})
        if merge_section(tree, "geoip", current, new):
            pending.stage_tree(CONFIG_FILE, tree, held)
    return Response(status_code=204)


@router.post("/geoip/refresh", status_code=202, response_model=Job)
def refresh_geoip(services: ServicesDep) -> Job:
    """Download the applied provider's data now, as a background job, and return the job.

    New data goes live at once when no changes are pending, and with the next apply otherwise.
    """
    return services.jobs.submit(KIND, lambda: refresh(services.pipeline))
