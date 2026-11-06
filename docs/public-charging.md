# Public charging

Needs an EV integration with a location (`location_entity_id`). Charges away from home are added to the
charging history and the economy, so the cost per km includes them. Without a location, the two entities
below and the dashboard card aren't created; the service under
[Adding a charge yourself](#adding-a-charge-yourself) still works.

## How it works

1. A charge away from home (the charge port or cable used while the car isn't home) is detected
   automatically: the time, the battery level at start and end, and the percentage added.
   Only a car known to be away counts: any state other than `home`, also when it is briefly
   `unavailable` and the last known state from the past day was away. Without a location entity, or with
   no known location, no public charge is detected – the charger's status can't tell, as it often reports
   the plug-in minutes after the car's port or cable. Add such a charge yourself (see below). If you
   remove the location later, a charge still waiting for its price is dropped.
2. When it ends, *Public charging completed* (`binary_sensor.cjp_<prefix>_public_charging_session_done`)
   turns on, and you get a notification – with the mobile app you can enter the price from it.
3. Enter the **total price** of the charge (not the price per kWh) in *Cost of public charging*
   (`number.cjp_<prefix>_public_charging_session_cost`). Enter `-1` to ignore the charge.
4. About 1 minute later the charge is added to the history, and the field is reset.

With several charges, enter them in the order they happened. A charge only waits for its price once it has
ended, so you can start the next public charge before entering the price of the last one. A charge waiting
for its price, or one still in progress, is remembered across restarts. On the dashboard, the public charging card is only shown while a charge waits.

## Adding a charge yourself

`cable_juice_planner.manual_add_public_charging_session` calculates – and optionally saves – a charge you
enter yourself, e.g. one that wasn't detected.
