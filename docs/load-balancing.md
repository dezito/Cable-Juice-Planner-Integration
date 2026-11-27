# Multiple cars (load balancing)

If several cars charge on the same grid connection, together they can exceed the main fuse. A **Shared
grid connection** plans them together and keeps them within the fuse per phase; if a phase still goes above
it (a car ahead that starts after all, the house), the last car is limited at once.

## Setup

**Add integration → Cable Juice Planner → Shared grid connection:**

| Field | Meaning |
|---|---|
| Main fuse L1/L2/L3 | the limit per phase (or the share the cars may use together) – default 25 A |
| Safety margin | subtracted from the fuse for short peaks (default 1 A) |
| Current/power L1/L2/L3 (optional) | the house's measured current (A, mA) or power (W, kW, mW, MW, GW) per phase, e.g. from a HAN/P1 meter; not an energy (Wh, kWh) |
| Grid voltage | converts W to A |
| Trip soon | a trip within this many hours gives the car precedence (default 12) |

Then, for each car (under **Configure → Charger**, the *Load balancing* page):

| Field | Meaning |
|---|---|
| Priority | 1 = charges first |
| Phase order | which grid phases the charger's L1/L2/L3 are connected to: L1-L2-L3, L2-L3-L1 or L3-L1-L2. 1-phase charging uses the charger's L1, so the right grid phase is loaded |
| Phase sensors include this car's charger | on by default. Turn it off when the charger is on its own meter that the phase sensors above do not include (but behind the same main fuse): see [A charger outside the phase sensors](#a-charger-outside-the-phase-sensors) |
| Chargers on the same circuit (optional) | the chargers (HA devices) sharing one circuit limit, e.g. Easee chargers behind a master – see [Shared circuit](#shared-circuit-eg-easee-master) |

Without phase sensors, the fuse is a fixed budget for the cars, and the chargers' *Charging power* sensors
show what each car really draws. With all three, the house's consumption is subtracted too. Either way, an
overloaded phase limits the last car at once.

## How it works

- **Order:** the cars are ordered by priority. A car below the daily minimum, or with a trip within *Trip
  soon*, goes first.
- **Planning:** each car gets a capacity per hour: the fuse minus the margin, minus the house's expected
  consumption (the average of the same hour over the last week, only with phase sensors), minus what the cars
  ahead have reserved. If there isn't room for full current, the car charges with fewer amps, and the rest
  is planned in other cheap hours.
- **1 or 3 phases:** the highest power that fits within the available current per phase is chosen. So a car
  can switch to 1 phase when a phase is full.
- **Live:** the amps are always limited to what is available right now. A car ahead counts with what it
  really draws, not with what it was given, so current it doesn't use (a car that doesn't start, a full
  battery) goes to the next car in line at that car's next calculation (its interval, at most 15 minutes, or
  sooner when the plan changes). If a car ahead
  changes its plan or its current, the cars behind are recalculated after 15 seconds.
- **With phase sensors** a sensor that has not reported for 5 minutes (also an unchanged value counts) is no
  reading: a meter that hangs at a low value would free current and hide an overload. Then the cars are
  counted as without phase sensors, below, and the house counts with the higher of its last measured
  consumption and the hour's forecast (the forecast's highest hour when this hour has none) – in the cars'
  amps and in the overload check. With none of them (after a restart, before the meter reported, without a
  forecast) the cars get no current until the meter reports (they are recalculated then). A car's *Charging
  power* sensor that has not reported for 5 minutes, or has no reading in a power unit (mW, W, kW, MW, GW; an
  energy unit such as Wh or kWh is no reading here: it may be a meter picked by mistake), counts
  for the cars behind
  with what the car was given, and a stale reading is taken from the house
  with at most that – and not at all when the car's status says it is not charging (a full car can't hide the
  house, and a car that charges steadily doesn't count its own charging as the house). One without a reading
  in a power unit is not taken from the house, and a fresh reading is taken at most as what the charger can
  draw (its maximum current plus 10 %), also in the house forecast. A phase sensor in an energy unit (Wh,
  kWh …) is no measurement: an energy meter can't tell the current right now. The house then counts as above
  without a measurement (the whole budget without a last measurement or forecast, so no current for the cars;
  otherwise only an estimate: the fuse is not protected by a measurement), the house forecast is not updated, and a repair names the sensors until no
  phase sensor reports an energy any more. The forecast reads the unit the statistics were recorded in: after a fix
  it is updated again once the recorder's own repair for the statistics is fixed (the log says so once). A
  phase sensor in any other unit that is no power unit is read as amps.
- **Without phase sensors** the *Charging power* sensor (in a power unit) of each car is the measurement, so the
  cars ahead are counted carefully. A car ahead counts with what it was given, unless **all** of these hold:
  - its current was raised more than 2 minutes ago (the charger needs a moment to start),
  - its charger status surely means not charging – *completed*, *complete*, *charge_complete*, *finished* (and the
    other built in completed and not ready states, e.g. *charging_finished*, *connected_finished*, *unplugged*), *off*,
    *disconnected*, *ready_to_charge*, *awaiting_start* or *awaiting_authorization*, in any case (*Complete* is
    *complete*), and a state that the charger blueprint's *States that mean done* names for this status entity
    (see [the charging state](charger-control.md#charging-state)). *Charging*, a status that may also mean
    charging (*on*, *connected*, *ready* …), an unknown one or none counts as charging (a power sensor that
    hangs at 0 W must not give its current away),
  - its power sensor has a reading in a power unit,
  - both its status and its power sensor reported within the last 5 minutes (also an unchanged value: a lost
    cloud connection keeps the last values, and a car that starts would not be seen). A sensor that only
    reports when its value changes therefore frees current for at most 5 minutes after its last change,
  - and no other car uses the same power sensor (one charger for two cars shows both).

  Then all its measured power may be on one phase – a car can charge on fewer phases than it was given – so
  only what no phase can draw is freed: a car given 3 × 16 A that still draws 2.3 kW (10 A) after its charger
  stopped (e.g. preconditioning) frees 6 A per phase, one that draws 0 W frees all of it. In practice current
  is freed mostly by a car that doesn't start or is full. A car ahead that charges without having been given
  current (charging without a rule) counts with all of its power on each of its phases.

  *ready_to_charge* and *awaiting_start* are also what a car waiting for its own timer shows: when it starts
  by itself, the overload check below limits the car behind, as fast as the status or power sensor reports.
  If the sensors stop reporting, the car ahead counts with its current again after 5 minutes: CJP checks every
  30 seconds and limits the car behind when that is too much for the fuse.
- **Overload:** if a phase exceeds the fuse – measured by the phase sensors, or without them the sum of the
  chargers' power (counted the same careful way) – the charging car with the lowest priority is limited at
  once, and checked again every 21 seconds while it is still too much. Without phase sensors (or when one has
  no number or has not reported for 5 minutes; the house then counts as above), the reaction comes when a charger's power or status changes or a car ahead gets more current,
  so a car ahead whose status says it charges, or that gets more current, limits the car behind at once,
  even with a slow power sensor. A charger whose power is more than 10 % above all it was given (it did not
  follow a limit yet, or did not switch to one phase) counts with its power spread over the phases it can use,
  so that is seen – an overdraw on fewer phases than it was given is not (only the phase sensors see that), and
  one on a power sensor that two cars share is seen by the overload check but not by the budget. A car that charges
  without having been given current counts with its power spread over its phases here, so another car is not
  limited for a draw that may not be there.

- **Main fuse check:** the house forecast's hourly mean current of each phase is compared with the raw main fuse (the
  margin is not taken off here). A phase with a mean of more than 1.25 × its fuse for 3 hours or more in the last 7 days
  raises the repair *The house and the cars draw more than the main fuse (load balancing)*, which names the phase: either the
  main fuse is set too low, or a phase sensor measures something else. It is a warning only, nothing is limited by it, and it
  goes when the next forecast no longer finds it. See
  [Repairs from the health checks](faq-troubleshooting.md#repairs-from-the-health-checks).

**Example:** a 25 A fuse with 1 A margin, two cars with 3 × 16 A. Car 1 (priority 1) charges at 16 A. The
house uses 5 A on L1. Car 2 gets 25 − 1 − 5 − 16 = 3 A on L1 – too little, so it is planned in other hours
(or with fewer amps once car 1 is done).

## A charger outside the phase sensors

If a car's charger is on its own meter that the phase sensors of the shared grid connection do not include (but is behind
the same main fuse), turn off **Phase sensors include this car's charger** on that car. This is separate from *House
consumption includes this car's charger* on the house page, which is about the house consumption sensor used for the solar
surplus; set each one to what that sensor measures.

Nothing is protected less because of it, the charger's draw is only added where the sensors can't see it:

- **Overload:** its draw (from its *Charging power* sensor, counted the same careful way as without phase sensors, and with
  what the car was given when there is no fresh reading) is added to the measured phase current, and the check also runs
  when its power or status changes or its current is raised. The last charging car is limited at once if the sum is above the fuse.
- **House:** the car's draw is no longer taken off the measured phase current to get the house, since the sensors never
  held it. The house forecast does the same (it is updated when a car joins, e.g. after you changed the setting). If
  an inside and an outside car share one power sensor, nothing is taken off for either (the house counts higher).
- **Cars behind:** a car ahead on its own meter counts the way a car ahead counts without phase sensors: with what it was
  given until its power sensor and status show that it does not draw (a fresh 0 W right after a raise frees nothing).
- **Main fuse check:** the car's draw is added to the phases before the repair compares them with the fuse. When the
  charger's statistics don't cover every hour (no or a wrong unit, missing hours) nothing is judged and the repair stays
  as it is.

Without phase sensors the setting changes nothing: the chargers' power sensors are the measurement. While the phase sensors
measure nothing, the cars are counted from their power sensors and the house from its last measured value or the forecast,
which were calculated with the setting.

Both wrong ways cost something. With the setting on for a charger that the sensors do not include, the house looks too small
by its own draw and an overload from the charger is not seen: this is the unsafe one. With it off for a charger that the
sensors do include, its draw is counted twice: the car itself is limited more than needed (its own budget sees its draw as the
house), the other cars get less current, and the repair *The house and the cars draw more than the main fuse* may appear.
To check, let the car charge and watch the phase sensors: if they rise by what it draws, they include it. If you can't
check, off is the safe side for the fuse.

## Solar surplus between cars

The priority also decides who gets the solar surplus. The car with priority 1 has the first right to it, then
priority 2, and so on. Each car calculates its surplus as the solar production minus the house's consumption, and
the house's consumption includes the other cars' charging **only for the cars ahead of it**: car 2 sees what car 1
charges with as consumption and only gets what is left, while car 1 doesn't count car 2's charging (see
*Ignore chargers of cars with lower priority* on the house page). If every car left
out every other car's charging, they would each count the same surplus as theirs. Cars with the same priority
count each other's charging as consumption, so the default (everyone priority 1) is the careful choice. A charger
you added yourself under *Ignored consumers* is always subtracted, so don't add the charger of a car ahead of this
one there. Turn the setting off if the house consumption sensor doesn't include the chargers. A car whose charger
has its own meter (*House consumption includes this car's charger* off on that car) is never subtracted by the others:
the house sensor doesn't include it, so the cars behind it and those with the same priority add its charging to their
house instead, and the order above holds as with one meter (without a reading from that charger it counts with its
most).

The right is by priority only, also when the car behind charges from the grid at that moment (a trip soon or a
car below its daily minimum goes first in the *current* limit, but not in the solar surplus): the car ahead then
counts the surplus as its own, and the car behind pays the grid price for its charging. The priority only exists
with a shared grid connection (the *Load balancing* page); without one every car has priority 1.

## Shared circuit (e.g. Easee master)

Some chargers share one circuit limit: with Easee, the circuit (set on the master) limits the current per phase
for all its chargers, and each charger has its own charging current. If every car's automation set the
circuit, they would overwrite each other.

**Setup:** on one car's *Load balancing* page, select the car's own charger and the other chargers of the circuit
under **Chargers on the same circuit**. The page shows each charger of the circuit and the cars using it. The
circuit is stored once in the shared grid connection, so the other cars of the circuit show the same selection;
a change made from one car (adding or removing a charger, or clearing the field) applies to all of them. A car
belongs to the circuit through the device of its charger entities (status, power, energy …), so a car whose
charger entities have no device cannot be linked. A charger can be on one circuit only.

**What happens:**

- Every car of the circuit gets the same `circuit_phase_1_amps` … `_3_amps` on its charger action: the **sum**
  per grid phase of what the circuit's cars charge with (0 when none of them charges), at most the fuse minus
  the margin – so when a car behind got current a car ahead doesn't use, the circuit still keeps both within the
  fuse if the car ahead starts after all. This cap knows only the fuse: the house and cars outside the circuit
  are not subtracted from it (they are in each car's own amps and in the overload check). The sum, not the
  highest value, because the circuit limit is shared – two cars at 16 A need 32 A on the circuit. The values
  are grid phases (the cars' phase order is applied); without a shared circuit they are the car's own circuit
  limit, as before.
- The circuit limit decides 1 or 3 phases for all its chargers. The first charging car in the order (priority,
  urgent first) decides; a car behind it on the circuit only charges on the same number of phases, or waits
  when that doesn't fit (e.g. 1-phase solar charging while the car ahead charges on 3 phases). A car whose
  charger only charges on 1 phase is not switched and doesn't decide it.
- In the [blueprint](charger-control.md#shared-circuit), fill in **Set circuit current** only in the automation
  of the car whose charger is the master; leave it empty in the others.

## Sensors

| Sensor | Content |
|---|---|
| `sensor.cjp_site_available_l1` … `_l3` | current per phase not given to any car (the `cars` attribute shows the order, each car's request, what is available to it now – where unused current of the cars ahead counts – and the cars sharing its circuit) |
| `sensor.cjp_site_house_l1` … `_l3` | the house's current per phase, excluding the cars |

The current per car is in its own [action entities](charger-control.md#the-action-entities) – the
blueprint needs no changes for load balancing, only for a [shared circuit](#shared-circuit-eg-easee-master).
