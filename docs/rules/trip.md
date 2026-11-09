# 🧳 Trip

**Numbers:** *Trip charging target* (%), *Trip km needed* · **Date/time:** *EV trip departure*, *EV trip arrival* ·
**Switch:** *Trip preheat* · **Button:** *Reset trip charging*

For a longer drive outside the normal pattern.

<p align="center">
  <img src="../../images/settings-trip.png" alt="The Trip settings" width="380">
</p>

## Planning a trip

1. Set **EV trip departure** and **EV trip arrival** in the future.
2. Set either **Trip charging target** (the battery level you want at departure) or **Trip km needed**
   (the km the trip needs – converted with the car's real consumption).
3. Optionally turn on **Trip preheat**.

The trip is planned automatically once these are set. What the trip needs is charged in the cheapest hours
before departure. Charging between the max. recommended charge limit and 100 % happens in the **last hours
before departure** (done *Hours before departure* before it), so the battery doesn't stand full for long.

The **trip arrival battery level** (default 25 %) is the level the car must still have at homecoming from the
trip.

## After the trip

The trip is reset automatically 2 hours after departure. **Reset trip charging** clears it at once.

If the car is still plugged in at home after the departure and hasn't reached the trip's level, it keeps
charging for up to 2 hours after the departure (the trip is reset then), but not past the planned homecoming. The charging before departure is done *Hours before departure* before it, see
[the buffer before departure](workplan.md#the-buffer-before-departure).

## Together with other rules

A trip and a workday on the same day each have their own need and their own departure, and the battery
never goes above the battery limit (100 % at most). The earlier departure is served first and stops at its
level. The later one gets what the car can still reach: if the trip departs before the homecoming from work,
only the charging before work counts, so work gets what it needs and the trip gets the rest. If the trip
departs after the homecoming, the car is charged to full before work (when that is cheapest) and the rest is
charged between homecoming and the trip. The [buffer before departure](workplan.md#the-buffer-before-departure) is only a buffer: if the target is not reached, the car charges what it can in it.
Work wins when both depart at the same time. With [load balancing](../load-balancing.md), a car with
a trip soon (default within 12 hours) goes first.
