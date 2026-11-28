# How Cable Juice Planner compares with other smart chargers

[← Back to the README](https://github.com/dezito/Cable-Juice-Planner-Integration/blob/main/README.md)

Smart charging can mean different things: a Home Assistant integration, a charger app that schedules a session, or a bundled electricity tariff that automatically controls a compatible car or charger.

CJP plans charging in Home Assistant. An automation, such as the included blueprint, applies the plan to your charger.

The examples below compare published approaches, not prices or performance in a controlled test. Availability, supported hardware and tariffs vary by country and can change.

Two key advantages of CJP are its potential to save more by planning across the whole week and its support for a lower average battery state of charge during everyday driving.

For a typical driver on variable electricity prices, CJP offers substantial savings potential compared with charging to the maximum recommended level every day.<br><br>CJP can wait for cheaper hours across the week, account for forecast solar surplus and avoid buying energy before it is needed.

This is not a guarantee.<br><br>Actual savings depend on prices, solar production and driving.

Its configurable daily reserve can also avoid routinely keeping the battery at a high state of charge, which may be gentler on battery health. Battery-life effects depend on battery chemistry and the vehicle.<br><br>See the scenarios below and [Daily reserve, cheaper power and battery health](#daily-reserve-cheaper-power-and-battery-health).

Compare the projects and provider offerings by scenario. The table describes features from CJP's documentation and the linked projects' or providers' published descriptions.

**Charging-window assumption for the comparison:** For EV Smart Charging, ha-evcc and public/provider solutions, unless their documentation explicitly states otherwise, assume the available cheap-charging window is only from arriving home today until departure the next day. They are not assumed to shift charging into cheaper hours on earlier days.

| Scenario | Cable Juice Planner | [EV Smart Charging](https://github.com/jonasbkarlsson/ev_smart_charging) | [ha-evcc](https://github.com/marq24/ha-evcc) | Public/provider options |
| --- | --- | --- | --- | --- |
| Potential to save on charging | Plans across the week and can use forecast solar surplus.<br>Actual savings depend on prices and usage. | Selects low-price intervals within the assumed window, ending at departure the next day.<br>Actual savings depend on prices and usage. | Solar modes can prioritize solar charging.<br>No direct price-savings comparison is implied. | Octopus schedules for cheaper energy within the assumed window, ending at departure the next day.<br>Other offers combine tariffs, apps or scheduled charging.<br>Actual savings depend on plan and usage. |
| Maximizing battery longevity | Daily reserve supports a lower average SOC, which research links to slower battery aging. | Defaults to a 100% target if no target-SOC entity is set.<br><br>Prolonged high SOC can accelerate aging. | evcc plans can be set to a high target SOC.<br><br>Prolonged high SOC can accelerate aging. | If the service charges to a high target SOC.<br><br>Prolonged high SOC can accelerate aging. |
| Cheapest hours across the week | Selects the week's cheapest eligible hours, not just the hours before the next departure. | No multi-day look-ahead documented. | No multi-day look-ahead documented. | No multi-day price look-ahead documented. |
| Routine charging | Daily reserve, weekly cheap-hour top-up. | Cheap 15/60-minute slots before the next-day departure. | evcc modes: Solar, Min+Solar, Fast. | [Octopus](https://octopus.energy/smart/intelligent-octopus-go/): schedules compatible EV/charger.<br>[OVO](https://www.ovoenergy.com/electric-cars/charge-anytime): schedules within set times.<br>Comparison assumes departure the next day. |
| Planned trip | Add departure date and time, departure SOC or required distance. | No trip-specific inputs documented.<br>Supports a charge-completion time and minimum SOC.<br>Comparison assumes departure the next day. | Can create or adjust charging plans via HA services.<br>No trip-specific inputs documented. | — |
| Unexpected long trip | A low daily reserve may not cover it.<br>A higher reserve keeps more charge available. | A higher target SOC keeps more charge available.<br>Defaults to 100% if no target-SOC entity is provided. | A higher target can be set in the evcc charging plan. | A higher charge target, where configurable, keeps more charge available. |
| Solar and home battery | Uses solar surplus and forecasts it when planning ahead.<br>Can account for a home battery. | Price-based interval selection. | evcc Solar/Min+Solar modes and battery settings. | Looad advertises Smart Charging in its [app](https://looad.dk/pages/vaelg-din-ladelosning). |
| Convenience after setup | Once configured, plans routine charging and future trips automatically. | Automatic charging after price, vehicle/charger and schedule configuration. | Requires a separate evcc installation as well as the HA integration. | Provider apps/services offer managed charging, subject to device and tariff compatibility. |
| Control and offering | You decide what charging actions to take and adapt the automation to your own setup. | Direct charger or EV-integration control. | Requires a separate evcc instance. | [OK](https://www.ok.dk/privat/produkter/appen/viden/hjemmeladning-med-ok-appen): choose session start/end.<br>[Clever](https://clever.dk/ladeloesninger/): home charging, subscriptions, electricity and public network.<br>[Looad](https://looad.dk/pages/vaelg-din-ladelosning): charger, electricity and app. |

## “Daily charging estimate” section in the Home Assistant CJP dashboard overview

The **Daily charging estimate** is a comparison, not the planner that controls CJP's charging actions. It estimates a daily-charging alternative by using forecast solar from the preceding day and then the cheapest eligible grid-price hours between the car coming home and its next charging deadline. The economy comparison also reports a conventional daily-charging baseline: the same energy in the cheapest hours before departure, without shifting the charge to an earlier, cheaper window or counting solar.

## Daily reserve, cheaper power and battery health

CJP does not assume that the car must always be fully charged, or that the amount needed is the same every day. The daily reserve is configurable with `number.cjp_<prefix>_min_daily_battery_level` (for example, `number.cjp_tesla_min_daily_battery_level`). It represents the battery level you want available when you get home. The plan adds the energy needed for your typical daily distance or the distances in your work plan and trips.

This is a deliberate trade-off compared with charging to the max recommended level every day. CJP prioritizes the energy you expect to need, but can use sufficiently cheap hours to charge further toward the recommended limit. When prices are high, it can leave that extra capacity unfilled. When prices are very low, it can take the opportunity to save more.

For battery health, regular smaller top-ups can keep the battery in a narrower state-of-charge (SOC) range and reduce depth of discharge, rather than repeatedly cycling through a wider range or leaving the car at a high SOC for long periods. For example, daily use around 30–60% SOC is generally a gentler pattern than repeatedly using 60–90%, though the exact effect depends on battery chemistry, temperature, the battery-management system and the vehicle. Laboratory findings support lower depth of discharge and avoiding prolonged high SOC, but their benefit does not translate identically to every EV ([Recurrent](https://www.recurrentauto.com/research/one-simple-trick), [Acterra](https://acterra.org/electric-vehicles-battery/), [WiTricity](https://www.witricity.com/blogs/how-you-charge.php)). CJP's configurable daily reserve supports this charging pattern while still letting you charge higher when needed or when power is very cheap. Follow the vehicle maker's guidance, especially for LFP batteries and trips requiring a higher charge.

## CJP's trade-offs

| ✅ Advantages | ⚠️ Disadvantages |
| --- | --- |
| Plans workdays, trips and preheating.<br>Automates routine charging and can find cheaper hours earlier in the week. | More settings and reliable price, charger and battery data are needed. |
| Combines prices, solar, home battery, driving distance and measured charging loss. | Charger control needs your automation.<br>The included blueprint helps. |
| Daily reserve avoids charging to a high level every day.<br>Very cheap hours can fill toward the recommended limit. | An unexpected long trip may require public charging if only the daily reserve remains. |
| Lower average SOC may help battery longevity.<br>CJP also includes a dashboard, history and cost comparisons. | Built-in price providers are Energi Data Service and/or Strømligning.<br>Other sources may need extra setup. |
| Works with supported HA charger and car integrations through your own automation. | More rules and forecasts mean more setup and complexity than a simple price scheduler. |
