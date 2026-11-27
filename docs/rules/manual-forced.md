# Manual and forced charging

## 🔌 Manual charging

**Switches:** *Allow manual charging now*, *Allow manual solar-only charging*

Charges at once, without waiting for the plan – e.g. for a guest, or when you need the car sooner than planned.

<p align="center">
  <img src="../../images/settings-manual-charging.png" alt="The Manual charging switches" width="380">
</p>

| Switch | The car |
|---|---|
| Allow manual charging now | charges at max. current from the grid (and solar) |
| Allow manual solar-only charging | charges only with the solar surplus |

Both charge up to the max. recommended charge limit, and both are turned off automatically when the charging
cable is unplugged: the charger status goes from a plugged in state to disconnected or off. That is also seen
when the status was unavailable in between, or Home Assistant restarted. Without a charger status entity CJP
cannot see the unplug, and the switches stay on. Nothing is charged while the charger reports that nothing is
plugged in (the action stays *stop* and the rule shows that the cable is disconnected), so you can turn a
switch on before the car is plugged in. While the charger status is unavailable, its last known value counts.
If that was a plugged in state and the status comes back as unplugged, the switches are turned off, also a
switch you turned on in between.

Manual charging only looks at the charger, not at your own car, so it also works for a guest's car, whether
your own car is home or not. Turn the switch on before or after the guest plugs in; it turns itself off
again when the guest unplugs. While your own car is away, charging starts at the next calculation (every few
minutes), not at the moment of the plug-in.

## 🪫 Forced charging below the daily battery level

**Switch:** *Forced charging below daily target*

When the battery is below the **daily arrival battery level** and no charge is planned right now, the car
charges up to that level at once – except in the **4 most expensive hours** of the day (see the attribute
`expensive_hours`). Then it waits for the next hour.

Use it as a safety net: whatever the plan says, the car never stays below the daily minimum for long.

<p align="center">
  <img src="../../images/settings-forced-charging.png" alt="The Forced charging switch" width="380">
</p>

## No rules at all

If every charging rule is off, CJP does not charge: the action is *stop* and the rule says that no charging
rules are active. If the car or charger starts by itself anyway, CJP asks for *stop* again and reports
[charging without a rule](../charger-control.md); the rule then also says that no charging rules are active.
All rules are off after the setup, so turn on at least one. Forced charging counts as a
rule: with only that on, the car is charged when it is below the daily minimum. It is turned off at the next
plug-in, like always.
