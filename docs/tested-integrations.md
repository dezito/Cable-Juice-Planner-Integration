# Tested integrations

Which price integrations and solar forecasts Cable Juice Planner has been seen working with.

- **✅ Tested:** it has been seen working with **real data**, for a price integration in that country.
- **🧪 Not tested yet:** you can choose it, but nobody has confirmed it with real data. Cable Juice Planner has automated tests for it,
  but they use made-up data built from the integration's source code or documentation. Passing a check on your own dashboard
  does not mark it as tested either.

The same mark 🧪 is shown when you set up Cable Juice Planner.

<!-- tables: made from the code, checked by tests/test_tested_integrations.py -->

## Price integrations by country

| Country | ✅ Tested | 🧪 Not tested yet |
|---|---|---|
| Austria (AT) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast |
| Belgium (BE) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast |
| Denmark (DK) | Strømligning, Energi Data Service | Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast |
| Estonia (EE) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Wattcast |
| Finland (FI) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Wattcast |
| France (FR) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast |
| Germany (DE) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Spot Forecast |
| Great Britain (GB) | – | Octopus Energy, EPEX Spot, GE-Spot, Energy Price Forecast |
| Latvia (LV) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Wattcast |
| Lithuania (LT) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Wattcast |
| Luxembourg (LU) | – | Energi Data Service, EPEX Spot, GE-Spot, Energy Price Forecast, Spot Forecast |
| Netherlands (NL) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Spot Forecast |
| Norway (NO) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Spot Forecast |
| Sweden (SE) | – | Energi Data Service, Nord Pool, EPEX Spot, GE-Spot, Energy Price Forecast, Elprisprognos |
| Any other country | – | EPEX Spot, GE-Spot, Energy Price Forecast |

## Price integrations

| Integration | Can be chosen in | ✅ Tested in |
|---|---|---|
| Strømligning | DK | DK |
| Energi Data Service | AT, BE, DE, DK, EE, FI, FR, LT, LU, LV, NL, NO, SE | DK |
| Octopus Energy | GB | – |
| Nord Pool | AT, BE, DE, DK, EE, FI, FR, LT, LV, NL, NO, SE | – |
| EPEX Spot | any country | – |
| GE-Spot | any country | – |
| Energy Price Forecast | any country | – |
| Spot Forecast | DE, LU, NL, NO | – |
| Elprisprognos | SE | – |
| Wattcast | EE, FI, LT, LV | – |

## Solar forecasts

A solar forecast is the same in every country.

| Solar forecast | Status |
|---|---|
| Solcast PV Forecast | ✅ Tested |
| Forecast.Solar | 🧪 Not tested yet |
| Helios Forecast | 🧪 Not tested yet |

<!-- end of the tables -->

**Any country** means that Cable Juice Planner offers the integration everywhere. Whether it has prices for your market depends on the
integration itself.

## Help get one tested

When you have seen an integration marked 🧪 work with your real data, and the values are right, please report it. The link on
the dashboard fills in most of the form:

- **Price integration:** *Settings → Price check → Price Provider Verification Report (Confirmed)*
  ([form](https://github.com/dezito/Cable-Juice-Planner-Integration/issues/new?template=price_provider_verification.yml))
- **Solar forecast:** *Settings → Solar forecast check → Solar Forecast Verification Report (Confirmed)*
  ([form](https://github.com/dezito/Cable-Juice-Planner-Integration/issues/new?template=solar_forecast_verification.yml))

The issue is public. A report does not change this page by itself: the maintainer marks an integration as tested.
