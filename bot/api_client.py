import logging
import os

import httpx

from bot import db

log = logging.getLogger("uzbekrentalsbot.api")

TIMEOUT = 5.0


def base_url() -> str | None:
    url = os.environ.get("BASE_API_URL", "").strip().rstrip("/")
    return url or None


async def get_health(url: str) -> dict:
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{url}/health")
        resp.raise_for_status()
        return resp.json()


async def check_health() -> bool:
    """Call GET /health, print the result, and log a clear error on failure."""
    url = base_url()
    if not url:
        log.error("BASE_API_URL is not set; cannot reach the Vatan Rentals API.")
        return False
    try:
        result = await get_health(url)
    except httpx.HTTPStatusError as e:
        log.error("API at %s answered /health with HTTP %s.", url, e.response.status_code)
        return False
    except httpx.HTTPError as e:
        log.error("API at %s is unreachable: %s", url, e.__class__.__name__ + f" {e}".rstrip())
        return False
    except ValueError:
        log.error("API at %s returned a non-JSON /health response.", url)
        return False
    print(f"API health ({url}/health): {result}", flush=True)
    return True


async def search_listings(
    listing_type: str, city: str | None, page: int = 1, limit: int = 3
) -> tuple[list[dict], int]:
    """GET /api/listings filtered by type (and city, if given).

    Returns (listings on this page, total matches). Raises httpx.HTTPError on failure.
    """
    url = base_url()
    if not url:
        raise httpx.InvalidURL("BASE_API_URL is not set")
    params: dict[str, str | int] = {"type": listing_type, "page": page, "page_size": limit}
    if city:
        params["city"] = city
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(f"{url}/api/listings", params=params)
        resp.raise_for_status()
        listings = resp.json()
        total = int(resp.headers.get("X-Total-Count", len(listings)))
        return listings, total


def listing_page_url(listing: dict) -> str | None:
    """Public page for a listing. SITE_URL overrides BASE_API_URL when the site lives elsewhere."""
    site = os.environ.get("SITE_URL", "").strip().rstrip("/") or base_url()
    if not site:
        return None
    kind = "car" if listing.get("listing_type") == "car" else "property"
    return f"{site}/listings/{kind}?id={listing['id']}"


class NotLinked(Exception):
    """This Telegram chat has no linked Vatan Rentals account."""


class LinkExpired(Exception):
    """The linked account's tokens no longer work; the user must link again."""


async def authed_get(chat_id: int, path: str):
    """GET an authenticated API path as the account linked to this chat.

    Refreshes the access token once on a 401. Raises NotLinked, LinkExpired
    or httpx.HTTPError.
    """
    link = db.get_link(chat_id)
    url = base_url()
    if link is None:
        raise NotLinked
    if not url:
        raise httpx.InvalidURL("BASE_API_URL is not set")
    async with httpx.AsyncClient(timeout=TIMEOUT) as client:
        resp = await client.get(
            f"{url}{path}", headers={"Authorization": f"Bearer {link['access_token']}"}
        )
        if resp.status_code == 401:
            refreshed = await client.post(f"{url}/api/auth/refresh", json={"refresh_token": link["refresh_token"]})
            if refreshed.status_code != 200:
                db.delete_link(chat_id)
                raise LinkExpired
            token = refreshed.json()["access_token"]
            db.update_access_token(chat_id, token)
            resp = await client.get(f"{url}{path}", headers={"Authorization": f"Bearer {token}"})
        resp.raise_for_status()
        return resp.json()
