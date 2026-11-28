"""Pages shared by the config flow and the options flow."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.helpers import device_registry as dr
from homeassistant.data_entry_flow import section
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from ..const import CIRCUIT_ENTITY_KEYS, CONF_CIRCUIT, CONF_CIRCUITS, CONF_ENTITY_PREFIX, CONF_SITE, DOMAIN, FLOW_STEPS, OPTIONS_MENU, PRICE_PROVIDER_KEYS, PRICE_PROVIDER_PRIORITY, default_options, object_prefix, price_scale
from ..site import charger_devices, circuit_of, update_circuits
from ..management.dependency_checks import NOTHING, disabled_sensors, sensors_registered, solcast_flow_problems
from ..prices import STROMLIGNING_PAGE_PREFIXES, STROMLIGNING_PLATFORM, STROMLIGNING_UNUSED_PREFIXES, detect_price_scale, has_stromligning_sell_prices, price_currency_errors, price_sensor_errors, DEFAULT_COUNTRY, default_providers, provider_errors, providers_for_country, unconfirmed_providers, provider_sections
from ..utils.chargers import charger_in_house_meter, charger_power_entity, lower_priority_charger_power_entities, site_priority
from ..utils.solcast import SOLCAST_PLATFORM, solcast_endings_for_days
from ..utils.solar_sources import CONFIRMED_SOLAR_SOURCES, NO_SOLAR_SOURCE, SOLAR_SOURCES
from .detection import Detection, async_detect, evidence_placeholders, verdict_values
from .schema import BOOLEAN, ENERGIDATASERVICE_URL, PRICE_PROVIDER_NAMES, PRICE_PROVIDERS, SELECTORS, SPOT_PRICE_PROVIDERS, UNCONFIRMED_SUFFIX, ProviderSelector, build_schema, forecast_selector, normalize_input, provider_of, validate_step

# the sensors the suggestions are made from; they are only made again when one of them was changed in this session
DETECTION_SENSORS = ("power_consumption_entity_id", "powerwall_watt_flow_entity_id", "powerwall_battery_level_entity_id")
REDETECT = "redetect"  # a switch of the review page, not an option


# each choice of a solar forecast has its own solar page: a step id of its own gives it its own texts
SOLAR_PAGES = {"solcast_solar": "solar_solcast", "forecast_solar": "solar_forecast_solar", "helios_forecast": "solar_helios"}
# the Solcast page when every sensor the plan needs is ready: it says so, instead of an empty list of problems
SOLCAST_READY_PAGE = "solar_solcast_ready"


class CJPFlowPages:
    """Pages shared by the config flow and the options flow.

    The subclass sets ``self._options`` (flat option dict), ``self._queue`` (remaining
    page ids) and implements ``_async_finish``.
    """

    _options: dict[str, Any]
    _queue: list[str]
    _providers: list[str]
    # the solar forecast chosen on the page before the solar page (not stored); None when that page was not shown
    _solar_source: str | None = None
    # the three sensors as they were when the options were loaded (restored, imported, stored), and what was found for a set
    _baseline: tuple[str, ...]
    _baseline_sign: bool = False  # the sign the options had then: the stored one, not what the form says now
    _detections: dict[tuple[str, ...], Detection]

    def _detection_sensors(self) -> tuple[str, ...]:
        return tuple(self._options.get(key) or "" for key in DETECTION_SENSORS)

    def _set_baseline(self) -> None:
        """The sensors in the options now are not 'changed': what was stored, restored or imported is not detected again."""
        self._baseline = self._detection_sensors()
        self._baseline_sign = bool(self._options.get("invert_powerwall_watt_flow_entity_id"))

    # chargers on the car's circuit from the load balancing page, saved in the site entry on finish
    _circuit: list[str] | None = None

    def _site_exists(self) -> bool:
        return self._site_entry() is not None

    def _site_entry(self) -> ConfigEntry | None:
        return next((entry for entry in self.hass.config_entries.async_entries(DOMAIN) if entry.data.get(CONF_SITE)), None)  # type: ignore[attr-defined]

    def _this_entry_id(self) -> str | None:
        """The entry being changed (None while a new car is set up)."""
        return None

    def _page_enabled(self, step: str) -> bool:
        if step == "load_balancing":
            return self._site_exists()
        if step == "prices_energidataservice":
            return "energidataservice" in self._providers
        if step == "prices_energypriceforecast":
            return "energypriceforecast" in self._providers
        if step in ("prices_spotforecast", "prices_elprognos", "prices_wattcast", "prices_epexspot", "prices_gespot", "prices_octopus", "prices_nordpool"):
            return step.removeprefix("prices_") in self._providers
        if step == "prices_stromligning":
            return "stromligning" in self._providers
        if step == "prices_stromligning_tariffs":
            return "stromligning" in self._providers and has_stromligning_sell_prices(self._options)
        if step == "solar_charging":
            return bool(self._options.get("production_entity_id"))
        # weather is not only for solar: preheating, the outdoor temperature of the drives and Adjust driving efficiency
        # to temperature use it too, so the page is shown without solar as well
        if step == "detection_review":
            return bool(self._options.get("powerwall_watt_flow_entity_id"))  # without a home battery there is nothing to suggest
        return True

    async def _async_next(self) -> ConfigFlowResult:
        while self._queue:
            step = self._queue.pop(0)
            if self._page_enabled(step):
                return await getattr(self, f"async_step_{step}")()
        return await self._async_finish()

    async def _async_finish(self) -> ConfigFlowResult:
        raise NotImplementedError

    # True while a new entry is made (not changed): the defaults that are amounts of money follow the price currency then
    _new_entry: bool = False

    def _apply_price_scale(self, suggest_fee: bool = False) -> None:
        """Store prices.price_scale from the unit of the price sensors (kept as it is when no sensor has a known unit).

        suggest_fee (the page of the fee, before it is shown): a new car gets the default sell fee, 0.03 kroner, in the price
        currency. Not for an import or a restored backup (their own fee stays) and not once the user has seen the page."""
        previous = price_scale(self._options_as_config())
        scale = detect_price_scale(self._options, self.hass.states.get)  # type: ignore[attr-defined]
        if scale is None:
            return
        self._options["price_scale"] = scale
        own_values = getattr(self, "_restore_backup", None) is not None or getattr(self, "_legacy_prefix", None) is not None
        default_fee = default_options()["sell_fee"]
        if suggest_fee and self._new_entry and not own_values and previous == 1.0 and scale != 1.0 and self._options.get("sell_fee") == default_fee:
            self._options["sell_fee"] = round(default_fee * scale, 3)

    def _options_as_config(self) -> dict[str, Any]:
        return {"prices": {"price_scale": self._options.get("price_scale", 1.0)}}

    def _placeholders(self) -> dict[str, str]:
        return {}

    def _notify_services(self) -> list[str]:
        services = self.hass.services.async_services().get("notify", {})  # type: ignore[attr-defined]
        return [f"notify.{name}" for name in services if name not in ("send_message", "persistent_notification")]

    def _page_keys(self, step: str) -> list[str]:
        """The fields of a page; the fee page also asks for the VAT and the surcharge when a wholesale price provider is chosen."""
        keys = FLOW_STEPS[step]
        if step == "price_fees" and any(name in self._providers for name in SPOT_PRICE_PROVIDERS):
            return [*keys, "spot_vat", "spot_surcharge"]
        return keys

    async def _async_page(self, step: str, user_input: dict[str, Any] | None) -> ConfigFlowResult:
        keys = self._page_keys(step)
        if step == "solar" and self._solar_source not in SOLAR_SOURCES:
            keys = [key for key in keys if key != "forecast_entity_id"]  # no forecast: the page before cleared it
        errors: dict[str, str] = {}
        if step == "price_fees" and user_input is None:
            self._apply_price_scale(suggest_fee=True)  # the price pages are done: the fee suggestion follows the currency
        values_for_form = dict(self._options)
        if user_input is not None:
            values = normalize_input(keys, user_input)
            errors = validate_step(step, values)
            if step == "solar":
                errors = {**self._solar_errors(values), **errors}
            if step.startswith("prices_"):
                get_state = self.hass.states.get  # type: ignore[attr-defined]
                registry = er.async_get(self.hass)  # type: ignore[attr-defined]
                unique_id_of = lambda entity_id: getattr(registry.async_get(entity_id), "unique_id", None)  # noqa: E731
                errors = {**price_currency_errors({**self._options, **values}, keys, get_state), **price_sensor_errors(values, get_state, unique_id_of), **errors}
            if not errors and step == "solar" and self._solar_source in SOLAR_SOURCES and not values.get("forecast_entity_id"):
                # no sensor of the chosen forecast: back to the choice (a flow has no back button), the page's values kept
                self._options.update(values)
                self._queue.insert(0, "solar")
                return self._show_solar_source({"base": "forecast_sensor_missing"})
            if not errors:
                previous_country = self._price_country()
                self._options.update(values)
                if step == "general" and not self._new_entry and self._price_country() != previous_country and set(self._providers) - set(providers_for_country(self._price_country(), PRICE_PROVIDERS)):
                    # the country was changed and a chosen provider has no prices in the new one: the price pages follow, to choose again
                    self._queue = list(OPTIONS_MENU["prices"])
                return await self._async_next()
            values_for_form.update(values)
        data_schema = build_schema(
            keys,
            values_for_form,
            self._notify_services() if step == "notifications" else None,
            self.hass.config.currency,  # type: ignore[attr-defined]
        )
        if step == "solar":
            data_schema = self._solar_schema(data_schema)
        shown = SOLAR_PAGES.get(self._solar_source, step) if step == "solar" else step
        solcast_waits = ""
        if shown == SOLAR_PAGES["solcast_solar"]:
            solcast_waits = solcast_flow_problems(
                self.hass, values_for_form.get("forecast_entity_id") or "", solcast_endings_for_days(7)  # type: ignore[attr-defined]
            )
            shown = shown if solcast_waits else SOLCAST_READY_PAGE
        stromligning_off = ""
        if step in STROMLIGNING_PAGE_PREFIXES:
            # a page of its own for "all enabled" and for "none found": an empty list of disabled ones would not say which
            prefixes = STROMLIGNING_PAGE_PREFIXES[step]
            stromligning_off = disabled_sensors(self.hass, STROMLIGNING_PLATFORM, prefixes, STROMLIGNING_UNUSED_PREFIXES)  # type: ignore[attr-defined]
            if not sensors_registered(self.hass, STROMLIGNING_PLATFORM, prefixes, STROMLIGNING_UNUSED_PREFIXES):  # type: ignore[attr-defined]
                shown = f"{step}_missing"
            elif stromligning_off == NOTHING:
                shown = f"{step}_ready"
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id=shown,
            data_schema=data_schema,
            errors=errors,
            description_placeholders={
                **self._placeholders(),
                "eds_url": ENERGIDATASERVICE_URL,
                # the chargers of the cars with a lower priority that are left out of the house consumption by themselves (the house page)
                "cjp_chargers": self._other_chargers_text(),
                # the disabled sensors of Strømligning on its pages (the picker can't offer them), and what the Solcast sensors lack
                "stromligning_disabled": stromligning_off,
                "solcast_problems": solcast_waits,
            },
        )

    def _other_chargers_text(self) -> str:
        chargers = lower_priority_charger_power_entities(
            self.hass,  # type: ignore[attr-defined]
            self._this_entry_id(),
            charger_power_entity(self._options) or None,
            site_priority(self._options),
        )
        return ", ".join(chargers) or "–"

    async def async_step_detection_review(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The battery sign and what the consumption includes, suggested from the recorder for the chosen sensors.

        Only looked up when one of the three sensors was changed in this session (or the user asks), so an import, a
        restored backup or stored options keep their values. What the history cannot decide keeps its current value.
        """
        keys = FLOW_STEPS["detection_review"]
        if user_input is not None and not user_input.get(REDETECT):
            self._options.update(normalize_input(keys, user_input))
            return await self._async_next()
        if user_input is not None:
            self._options.update(normalize_input(keys, user_input))
        sensors = self._detection_sensors()
        changed = sensors != self._baseline
        detection = self._detections.get(sensors)
        if all(sensors) and (user_input is not None or (changed and detection is None)):
            # a sign set before is only trusted for the sensor it was set for
            trusted = self._baseline_sign if sensors[1] == self._baseline[1] and self._baseline[1] else None
            detection = await async_detect(self.hass, sensors[0], sensors[1], sensors[2], trusted, None, self._left_out_consumers())  # type: ignore[attr-defined]
            if detection.reason not in ("timeout", "error"):  # these are tried again at the next visit
                self._detections[sensors] = detection
            self._options.update(verdict_values(detection))
        schema = build_schema(keys, self._options, None, self.hass.config.currency)  # type: ignore[attr-defined]
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="detection_review",
            data_schema=vol.Schema({**schema.schema, vol.Optional(REDETECT, default=False): BOOLEAN}),
            errors={"base": f"detection_{detection.reason}"} if detection and detection.reason else {},
            description_placeholders={**self._placeholders(), **evidence_placeholders(detection)},
        )

    def _left_out_consumers(self) -> list[str]:
        """What the engine leaves out of the house consumption: the ignored ones, this car's charger and - only when the
        option is on, as in the engine - the chargers of the cars behind this one (a higher priority number)."""
        ignored = self._options.get("ignore_consumption_from_entity_ids") or []
        own = charger_power_entity(self._options)
        behind = (
            lower_priority_charger_power_entities(
                self.hass,  # type: ignore[attr-defined]
                self._this_entry_id(),
                own or None,
                site_priority(self._options),
            )
            if self._options.get("ignore_cjp_chargers", True)
            else []
        )
        # a charger on its own meter is not in the house consumption: the engine adds it instead of subtracting it, and the
        # detection reads the sensors as they are, so it is not left out there
        left_out = [own] if own and charger_in_house_meter(self._options) else []
        return [*([ignored] if isinstance(ignored, str) else ignored), *left_out, *behind]

    async def async_step_country(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """The country of the prices: the page before the providers (the options have it on the General page).

        Home Assistant's country is suggested for a new entry; a restored backup or an import shows its own, as before."""
        own_values = getattr(self, "_restore_backup", None) is not None or getattr(self, "_legacy_prefix", None) is not None
        if user_input is not None:
            country = str(user_input.get("price_country") or DEFAULT_COUNTRY).upper()
            self._options["price_country"] = country
            if not own_values:
                self._providers = default_providers(country)
            return await self._async_next()
        country = self._options.get("price_country") if own_values else self.hass.config.country  # type: ignore[attr-defined]
        schema = vol.Schema({vol.Required("price_country", default=str(country or DEFAULT_COUNTRY).upper()): SELECTORS["price_country"]})
        return self.async_show_form(step_id="country", data_schema=schema, description_placeholders=self._placeholders())  # type: ignore[attr-defined]

    def _price_country(self) -> str:
        """The country of the prices (the country page of the config flow, the General page of the options): it decides which providers are offered."""
        return str(self._options.get("price_country") or DEFAULT_COUNTRY).upper()

    async def async_step_solar_forecast_source(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Which solar forecast: the page before the solar page, whose sensor picker then offers that integration's sensors.

        The choice is not stored: it is read again from the platform of the forecast sensor. A sensor whose platform is not
        known (not in the entity registry, e.g. in a restored backup) is never cleared."""
        stored = self._options.get("forecast_entity_id") or ""
        platform = self._platform_of(stored)
        if user_input is not None:
            source = provider_of(str(user_input.get("solar_forecast_source") or NO_SOLAR_SOURCE))
            self._solar_source = source
            if source == NO_SOLAR_SOURCE or (platform is not None and platform != source):
                self._options["forecast_entity_id"] = ""  # no forecast, or a sensor of another source than the chosen one
            return await self._async_next()
        return self._show_solar_source()

    def _show_solar_source(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        stored = self._options.get("forecast_entity_id") or ""
        platform = self._platform_of(stored)
        # a value shows the label: the source's name, with the mark where nobody has confirmed it
        listed = {
            source: source if source == NO_SOLAR_SOURCE or source in CONFIRMED_SOLAR_SOURCES else source + UNCONFIRMED_SUFFIX
            for source in (NO_SOLAR_SOURCE, *SOLAR_SOURCES)
        }
        chosen = NO_SOLAR_SOURCE if not stored else platform if platform in SOLAR_SOURCES else SOLAR_SOURCES[0]
        schema = vol.Schema(
            {
                vol.Required("solar_forecast_source", default=listed[chosen]): ProviderSelector(
                    selector.SelectSelectorConfig(options=list(listed.values()), mode=selector.SelectSelectorMode.LIST, translation_key="solar_forecast_sources")
                )
            }
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="solar_forecast_source", data_schema=schema, errors=errors or {}, description_placeholders=self._placeholders()
        )

    async def async_step_solar_solcast(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_prices_stromligning_ready(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning", user_input)

    async def async_step_prices_stromligning_missing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning", user_input)

    async def async_step_prices_stromligning_tariffs_ready(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning_tariffs", user_input)

    async def async_step_prices_stromligning_tariffs_missing(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning_tariffs", user_input)

    async def async_step_solar_solcast_ready(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_solar_forecast_solar(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_solar_helios(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    def _platform_of(self, entity_id: str) -> str | None:
        """The integration of an entity in the entity registry, None when it is not known."""
        entry = er.async_get(self.hass).async_get(entity_id) if entity_id else None  # type: ignore[attr-defined]
        return entry.platform if entry else None

    def _solar_errors(self, values: dict[str, Any]) -> dict[str, str]:
        """The forecast sensor must be of the source chosen on the page before (an unknown platform is accepted)."""
        source = self._solar_source
        if source not in SOLAR_SOURCES:
            return {}
        entity_id = values.get("forecast_entity_id")
        if entity_id and self._platform_of(entity_id) not in (None, source):
            return {"forecast_entity_id": "forecast_wrong_source"}
        return {}

    def _solar_schema(self, schema: vol.Schema) -> vol.Schema:
        """The sensor picker offers the chosen source's sensors only. A stored sensor whose platform is not known keeps the
        picker of every source, so it can still be seen."""
        source = self._solar_source
        stored = self._options.get("forecast_entity_id") or ""
        if source not in SOLAR_SOURCES or (stored and self._platform_of(stored) is None):
            return schema
        return vol.Schema({key: forecast_selector(source) if key == "forecast_entity_id" else value for key, value in schema.schema.items()})

    async def async_step_price_provider(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        errors: dict[str, str] = {}
        country = self._price_country()
        # only the providers that have prices in the country, in the order their prices are taken in (the lists follow it)
        offered = providers_for_country(country, PRICE_PROVIDER_PRIORITY)
        unconfirmed = unconfirmed_providers(country, offered)
        listed = {p: p + UNCONFIRMED_SUFFIX if p in unconfirmed else p for p in offered}  # the value shows 🧪
        if user_input is not None:
            picked = {provider_of(value) for part in provider_sections(offered) for value in user_input.get(part, {}).get("providers", [])}
            providers = [p for p in offered if p in picked]
            if not providers:
                errors["base"] = "no_price_provider"
            elif errors := provider_errors(country, providers):
                self._providers = providers
            else:
                self._providers = providers
                for provider, keys in PRICE_PROVIDER_KEYS.items():
                    if provider not in providers:
                        for key in keys:
                            self._options[key] = ""
                return await self._async_next()
        # what was chosen before stays as far as the country has it; nothing left starts as a new entry in the country does
        chosen = [p for p in self._providers if p in offered] or default_providers(country)
        # two sections, both open: the providers with a forecast, and those with published prices only
        fields = {
            vol.Required(part): section(
                vol.Schema(
                    {
                        vol.Required("providers", default=[listed[p] for p in names if p in chosen]): ProviderSelector(
                            selector.SelectSelectorConfig(
                                options=[listed[p] for p in names], multiple=True, mode=selector.SelectSelectorMode.LIST, translation_key="price_providers"
                            )
                        )
                    }
                ),
                {"collapsed": False},
            )
            for part, names in provider_sections(offered).items()
            if names
        }
        schema = vol.Schema(fields)
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="price_provider",
            data_schema=schema,
            errors=errors,
            description_placeholders={
                **self._placeholders(),
                "country": country,
                # what the country does not have, so the user sees why a provider is missing from the list
                "unavailable": ", ".join(PRICE_PROVIDER_NAMES[p] for p in PRICE_PROVIDERS if p not in offered) or "–",
                # the order the prices are taken in, for the providers the country has
                "priority": " → ".join(PRICE_PROVIDER_NAMES[p] for p in offered),
            },
        )

    async def async_step_general(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("general", user_input)

    async def async_step_charger(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("charger", user_input)

    async def async_step_charger_specs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("charger_specs", user_input)

    async def async_step_load_balancing(self, user_input=None) -> ConfigFlowResult:
        """Priority, phase order and the chargers sharing the car's circuit."""
        keys = FLOW_STEPS["load_balancing"]
        own = self._own_charger_devices()
        site = self._site_entry()
        current = self._circuit if self._circuit is not None else circuit_of(site.options.get(CONF_CIRCUITS) if site else [], own)
        errors: dict[str, str] = {}
        values_for_form = dict(self._options)
        if user_input is not None:
            values = normalize_input(keys, user_input)
            selected = [device for device in user_input.get(CONF_CIRCUIT) or [] if device]
            if selected and not own:
                errors[CONF_CIRCUIT] = "circuit_no_charger_device"
            elif selected and not own & set(selected):
                errors[CONF_CIRCUIT] = "circuit_own_charger_missing"
            if not errors:
                self._options.update(values)
                self._circuit = selected
                return await self._async_next()
            values_for_form.update(values)
            current = selected
        schema = build_schema(keys, values_for_form).extend(
            {vol.Optional(CONF_CIRCUIT, description={"suggested_value": current}): self._circuit_selector()}
        )
        return self.async_show_form(  # type: ignore[attr-defined]
            step_id="load_balancing",
            data_schema=schema,
            errors=errors,
            description_placeholders={**self._placeholders(), "circuit": self._circuit_text(current, own)},
        )

    def _own_charger_devices(self) -> set[str]:
        return charger_devices(self.hass, [self._options.get(key) for key in CIRCUIT_ENTITY_KEYS])  # type: ignore[attr-defined]

    def _circuit_selector(self) -> selector.DeviceSelector:
        """Devices of the integrations the car's charger entities come from (all without)."""
        ent_reg = er.async_get(self.hass)  # type: ignore[attr-defined]
        integrations = []
        for key in CIRCUIT_ENTITY_KEYS:
            entity_id = self._options.get(key)
            entry = ent_reg.async_get(entity_id) if entity_id else None
            if entry is not None and entry.device_id and entry.platform not in integrations:
                integrations.append(entry.platform)
        config: dict[str, Any] = {"multiple": True}
        if integrations:
            config["filter"] = [{"integration": integration} for integration in integrations]
        return selector.DeviceSelector(selector.DeviceSelectorConfig(**config))

    def _circuit_text(self, devices: list[str], own: set[str]) -> str:
        """Each charger of the circuit and the cars using it (shown in the page description)."""
        if not devices:
            return "–"
        users: dict[str, list[str]] = {device: [] for device in devices}
        this_entry = self._this_entry_id()
        for device in own:
            if device in users:
                users[device].append(object_prefix(self._placeholders().get("prefix")))
        for entry in self.hass.config_entries.async_entries(DOMAIN):  # type: ignore[attr-defined]
            if entry.data.get(CONF_SITE) or entry.entry_id == this_entry:
                continue
            for device in charger_devices(self.hass, [entry.options.get(key) for key in CIRCUIT_ENTITY_KEYS]):  # type: ignore[attr-defined]
                if device in users:
                    users[device].append(object_prefix(entry.data.get(CONF_ENTITY_PREFIX)))
        dev_reg = dr.async_get(self.hass)  # type: ignore[attr-defined]
        lines = []
        for device, prefixes in users.items():
            info = dev_reg.async_get(device)
            name = (info.name_by_user or info.name) if info is not None else device
            lines.append(f"- {name}: {', '.join(prefixes) or '–'}")
        return "\n".join(lines)

    def _save_circuit(self) -> None:
        """The circuit is kept once in the site entry, so a change from one car applies to all of it."""
        site = self._site_entry()
        if self._circuit is None or site is None:
            return
        old = site.options.get(CONF_CIRCUITS) or []
        circuits = update_circuits(old, self._own_charger_devices(), self._circuit)
        if circuits != old:
            self.hass.config_entries.async_update_entry(site, options={**site.options, CONF_CIRCUITS: circuits})  # type: ignore[attr-defined]

    async def async_step_ev(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("ev", user_input)

    async def async_step_ev_specs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("ev_specs", user_input)

    async def async_step_home(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("home", user_input)

    async def async_step_powerwall(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("powerwall", user_input)

    async def async_step_prices_energidataservice(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_energidataservice", user_input)

    async def async_step_prices_energypriceforecast(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_energypriceforecast", user_input)

    async def async_step_prices_spotforecast(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_spotforecast", user_input)

    async def async_step_prices_elprognos(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_elprognos", user_input)

    async def async_step_prices_wattcast(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_wattcast", user_input)

    async def async_step_prices_epexspot(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_epexspot", user_input)

    async def async_step_prices_gespot(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_gespot", user_input)

    async def async_step_prices_octopus(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_octopus", user_input)

    async def async_step_prices_nordpool(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_nordpool", user_input)

    async def async_step_prices_stromligning(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning", user_input)

    async def async_step_prices_stromligning_tariffs(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("prices_stromligning_tariffs", user_input)

    async def async_step_price_fees(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("price_fees", user_input)

    async def async_step_solar(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("solar", user_input)

    async def async_step_solar_charging(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("solar_charging", user_input)

    async def async_step_weather(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("weather", user_input)

    async def async_step_notifications(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("notifications", user_input)

    async def async_step_advanced(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("advanced", user_input)

    async def async_step_database(self, user_input=None) -> ConfigFlowResult:
        return await self._async_page("database", user_input)
