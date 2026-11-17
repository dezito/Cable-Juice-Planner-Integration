"""Notification about the HACS cards the sidebar dashboard needs but Home Assistant does not load.

The bundled dashboard uses custom Lovelace cards. Without them the cards show an error in the
browser, so when the dashboard option is on and a card cannot be found, one persistent
notification lists what is missing with links to the cards.

Which cards are loaded is read from Home Assistant itself (the Lovelace resources, which is where
HACS registers frontend cards, and the frontend's extra module URLs). Nothing is fetched from the
network. When the resources cannot be read, nothing is concluded: the notification is neither
created nor changed, so a broken lookup never reports every card as missing.
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components import persistent_notification
from homeassistant.components.lovelace.const import LOVELACE_DATA
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.core import HomeAssistant, callback

from ..const import DOMAIN

_LOGGER = logging.getLogger(__name__)

NOTIFICATION_ID = f"{DOMAIN}_dashboard_cards"
# What the last notification listed, so a change in the resources does not bring back a dismissed one
_LISTED_KEY = f"{DOMAIN}_dashboard_cards_listed"
# frontend.DATA_EXTRA_MODULE_URL (a plain key, so that older Home Assistant versions without the name still work)
_EXTRA_MODULE_URL_KEY = "frontend_extra_module_url"
TITLE = "Cable Juice Planner"


@dataclass(frozen=True)
class DashboardCard:
    name: str
    owner: str
    repository: str
    # lower-case parts of the resource URL (HACS serves /hacsfiles/<repository>/<file>.js)
    tokens: tuple[str, ...]
    optional: bool = False

    @property
    def github_url(self) -> str:
        return f"https://github.com/{self.owner}/{self.repository}"

    @property
    def hacs_url(self) -> str:
        return f"https://my.home-assistant.io/redirect/hacs_repository/?owner={self.owner}&repository={self.repository}"


# Every custom card type of dashboard/cable_juice_planner.yaml; card-mod only styles tables
DASHBOARD_CARDS: tuple[DashboardCard, ...] = (
    DashboardCard("Mushroom", "piitaya", "lovelace-mushroom", ("mushroom",)),
    DashboardCard("ApexCharts Card", "RomRider", "apexcharts-card", ("apexcharts-card",)),
    DashboardCard("Config Template Card", "iantrich", "config-template-card", ("config-template-card",)),
    DashboardCard("card-mod", "thomasloven", "lovelace-card-mod", ("card-mod",), optional=True),
)


async def async_loaded_card_urls(hass: HomeAssistant) -> list[str] | None:
    """URLs of the frontend modules Home Assistant loads, or None when they cannot be read."""
    resources = getattr(hass.data.get(LOVELACE_DATA), "resources", None)
    if resources is None:
        return None
    try:
        await resources.async_get_info()  # the storage collection loads lazily
        items = resources.async_items()
    except Exception as err:  # noqa: BLE001 - a failing lookup must never break the setup
        _LOGGER.debug("Could not read the Lovelace resources: %s", err)
        return None
    if items is None:
        return None
    urls = [str(item["url"]) for item in items if isinstance(item, dict) and item.get("url")]
    # frontend: extra_module_url (YAML) and integrations that add modules themselves
    urls.extend(str(url) for url in getattr(hass.data.get(_EXTRA_MODULE_URL_KEY), "urls", None) or ())
    return urls


def missing_cards(urls: Iterable[str], cards: Iterable[DashboardCard] = DASHBOARD_CARDS) -> list[DashboardCard]:
    lowered = [url.lower() for url in urls]
    return [card for card in cards if not any(token in url for token in card.tokens for url in lowered)]


def card_notification(missing: Iterable[DashboardCard], t: Callable[..., str]) -> tuple[str, str]:
    """Title and Markdown message listing the missing cards."""
    lines = []
    for card in missing:
        optional = f" ({t('ui.dashboard_cards.optional')})" if card.optional else ""
        lines.append(
            f"- **{card.name}**{optional}: [GitHub]({card.github_url}) · [HACS]({card.hacs_url})"
        )
    message = f"{t('ui.dashboard_cards.intro')}\n\n" + "\n".join(lines) + f"\n\n{t('ui.dashboard_cards.footer')}"
    return f"{TITLE}: {t('ui.dashboard_cards.title')}", message


async def async_update_card_notification(
    hass: HomeAssistant,
    t: Callable[..., str],
    *,
    only_if_changed: bool = False,
    cards: Iterable[DashboardCard] = DASHBOARD_CARDS,
) -> None:
    """Create the notification for the missing cards, or remove it when none are missing."""
    urls = await async_loaded_card_urls(hass)
    if urls is None:
        return
    missing = missing_cards(urls, cards)
    names = tuple(card.name for card in missing)
    if not missing:
        hass.data.pop(_LISTED_KEY, None)
        persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
        return
    if only_if_changed and hass.data.get(_LISTED_KEY) == names:
        return
    hass.data[_LISTED_KEY] = names
    title, message = card_notification(missing, t)
    persistent_notification.async_create(hass, message, title=title, notification_id=NOTIFICATION_ID)
    _LOGGER.info("Dashboard cards not found in Home Assistant: %s", ", ".join(names))


@callback
def async_watch_card_resources(hass: HomeAssistant, entry: ConfigEntry, t: Callable[..., str]) -> None:
    """Check again when the Lovelace resources change (HACS installs a card without a restart)."""
    resources = getattr(hass.data.get(LOVELACE_DATA), "resources", None)
    add_listener = getattr(resources, "async_add_listener", None)
    if add_listener is None:  # YAML resources do not change at runtime
        return

    async def _changed(*_args: Any) -> None:
        # the caller (HACS or the UI adding a resource) awaits this listener: an error must not reach it
        try:
            await async_update_card_notification(hass, t, only_if_changed=True)
        except Exception:  # noqa: BLE001
            _LOGGER.exception("Could not check the dashboard cards after the Lovelace resources changed")

    try:
        unsubscribe = add_listener(_changed)
    except Exception as err:  # noqa: BLE001
        _LOGGER.debug("Could not watch the Lovelace resources: %s", err)
        return
    if callable(unsubscribe):
        entry.async_on_unload(unsubscribe)


@callback
def async_dismiss_card_notification(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove the notification unless another loaded entry still shows the dashboard."""
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id or other.state is not ConfigEntryState.LOADED:
            continue
        config = getattr(getattr(other, "runtime_data", None), "config", None)
        if hasattr(config, "get") and config.get("dashboard"):
            return
    hass.data.pop(_LISTED_KEY, None)
    persistent_notification.async_dismiss(hass, NOTIFICATION_ID)
