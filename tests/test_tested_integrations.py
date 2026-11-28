"""docs/tested-integrations.md must say what the code says: which price integrations can be chosen in which country, and
which of them (and which solar forecasts) are confirmed with real data. After changing confirmed_countries, countries or
CONFIRMED_SOLAR_SOURCES, put the text of tables() into the page."""
from __future__ import annotations

import pathlib

from custom_components.cable_juice_planner.const import PRICE_PROVIDER_PRIORITY
from custom_components.cable_juice_planner.flow.schema import PRICE_PROVIDER_NAMES, PRICE_PROVIDERS
from custom_components.cable_juice_planner.prices import providers_for_country
from custom_components.cable_juice_planner.prices.factory import PROVIDER_CLASSES
from custom_components.cable_juice_planner.utils.solar_sources import CONFIRMED_SOLAR_SOURCES, SOLAR_SOURCE_NAMES, SOLAR_SOURCES

PAGE = pathlib.Path(__file__).parents[1] / "docs" / "tested-integrations.md"
START, END = "<!-- tables: made from the code, checked by tests/test_tested_integrations.py -->", "<!-- end of the tables -->"
COUNTRY_NAMES = {
    "AT": "Austria", "BE": "Belgium", "DE": "Germany", "DK": "Denmark", "EE": "Estonia", "FI": "Finland", "FR": "France",
    "GB": "Great Britain", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia", "NL": "Netherlands", "NO": "Norway", "SE": "Sweden",
}
OTHER = "ZZ"  # no country of any list: only the integrations without a country list are offered there


def _names(providers: list[str]) -> str:
    return ", ".join(PRICE_PROVIDER_NAMES[name] for name in providers) or "–"


def _row(label: str, country: str) -> str:
    offered = providers_for_country(country, PRICE_PROVIDER_PRIORITY)
    tested = [name for name in offered if country in PROVIDER_CLASSES[name].confirmed_countries]
    return f"| {label} | {_names(tested)} | {_names([name for name in offered if name not in tested])} |"


def tables() -> str:
    listed = sorted({code for name in PRICE_PROVIDERS for code in PROVIDER_CLASSES[name].countries or ()}, key=lambda code: COUNTRY_NAMES[code])
    lines = ["## Price integrations by country", "", "| Country | ✅ Tested | 🧪 Not tested yet |", "|---|---|---|"]
    lines += [_row(f"{COUNTRY_NAMES[code]} ({code})", code) for code in listed]
    lines.append(_row("Any other country", OTHER))
    lines += ["", "## Price integrations", "", "| Integration | Can be chosen in | ✅ Tested in |", "|---|---|---|"]
    for name in PRICE_PROVIDER_PRIORITY:
        provider = PROVIDER_CLASSES[name]
        where = "any country" if provider.countries is None else ", ".join(sorted(provider.countries))
        lines.append(f"| {PRICE_PROVIDER_NAMES[name]} | {where} | {', '.join(sorted(provider.confirmed_countries)) or '–'} |")
    lines += ["", "## Solar forecasts", "", "A solar forecast is the same in every country.", "", "| Solar forecast | Status |", "|---|---|"]
    lines += [f"| {SOLAR_SOURCE_NAMES[source]} | {'✅ Tested' if source in CONFIRMED_SOLAR_SOURCES else '🧪 Not tested yet'} |" for source in SOLAR_SOURCES]
    return "\n".join(lines)


def test_the_page_says_what_the_code_says() -> None:
    page = PAGE.read_text(encoding="utf-8").replace("\r\n", "\n")
    assert page.count(START) == 1 and page.count(END) == 1
    assert page.split(START)[1].split(END)[0].strip() == tables()


def test_the_page_is_linked_from_the_documentation() -> None:
    root = PAGE.parents[1]
    for path, link in ((root / "README.md", "docs/tested-integrations.md"), (root / "docs" / "README.md", "tested-integrations.md"),
                       (root / "docs" / "getting-started.md", "tested-integrations.md")):
        assert f"]({link})" in path.read_text(encoding="utf-8"), path.name
