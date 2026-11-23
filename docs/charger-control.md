# Charger and car control

CJP sends **no commands** to the charger or the car. It publishes what should happen on the action
entities, and your automation does it – easiest with the blueprint that comes with the integration.

## The action entities

| Entity | Content |
|---|---|
| `sensor.cjp_<prefix>_charger_action` | `charge` or `stop`: whether the charger should charge now |
| ↳ attribute `charger_enabled` | whether the charger should be enabled |
| ↳ attribute `stop_request` | counts up each time CJP sees charging without a rule and asks for stop again (0 after a restart of Home Assistant; CJP's own restart of a charger that doesn't start doesn't count it); while it is above 0 it is also in `decision` (`…, stop request 3`) |
| ↳ attributes `phase_1_amps`, `phase_2_amps`, `phase_3_amps` | the charging current per phase in A (0 = phase not used) – so an automation only needs `charger_action` |
| ↳ attributes `circuit_phase_1_amps` … `_3_amps` | the circuit's limit per phase (e.g. Easee's circuit limit); on a [shared circuit](load-balancing.md#shared-circuit-eg-easee-master) the sum per grid phase of all its chargers |
| ↳ attribute `decision` | the action, enabled, mode, amps, circuit and charge limit in one text, e.g. `charge, enabled, mode normal, 16/16/16 A, circuit 24/24/24 A, limit 80 %` – trigger on it to run once per change |
| ↳ attribute `circuit_chargers` | only on a shared circuit: the prefixes of the cars on it |
| ↳ attribute `charging_limit` | the charge limit for the car in % |
| ↳ attribute `mode` | `normal` or `error` (the calculation failed) |
| ↳ attribute `updated` | the time of the latest calculation (heartbeat) |
| `sensor.cjp_<prefix>_charger_phase_1_amps` … `_3_amps` | the same current per phase as sensors |
| `sensor.cjp_<prefix>_charging_watts` | the wanted charging power |
| `sensor.cjp_<prefix>_charging_limit` | the wanted charge limit on the car in % (only with an EV integration) |
| `event.cjp_<prefix>_ev_request` | `wake_up`, `preheat_start` (data: `departure`, `defrost`) and `preheat_stop` |

## The blueprint

**Cable Juice Planner - charger control** is installed with the integration. It is placed in
`config/blueprints/automation/cable_juice_planner/charger_control.yaml` on startup and can be found under
*Settings → Automations & scenes → Blueprints*.

The file is never overwritten on its own. If a new version arrives with a HACS update (or you have edited
the file yourself), a repair shows up after restart under *Settings → System → Repairs*. If you choose to
update, the file is replaced with the bundled version, and automations using it are reloaded. If you ignore
it, you keep your own file. If the file is deleted, the latest version is put back on the next restart.

*Status or "charging" entities* and *Charging power* took one entity in earlier versions and take several now.
An automation with a single entity there still works, but the automation editor shows the field as empty, and
saving the automation without selecting the entity again removes it. A repair lists those automations: open
each one, select the entities again and save. Cable Juice Planner never changes your automations itself.

Create **one automation per car**. Under *Cable Juice Planner* select the car's **Charger action** – the
current per phase is read from its attributes.

Others may already have set it up for your charger or car: see
[Blueprint setups](https://github.com/dezito/Cable-Juice-Planner-Integration/discussions/categories/blueprint-setups).
Paste a setup under *Settings → Automations & scenes → Create automation → ⋮ → Edit in YAML* and change what
is marked `TODO`. When yours works, share it there with **New discussion**, so the next one with the same
charger or car can copy it.

### Charger and car

The blueprint has one section for the **charger** and one for the **car**. All actions are optional: fill in
each task where your setup can do it – in the charger, in the car or both (e.g. the current in the charger
and the charge limit in the car).

| Section | Inputs |
|---|---|
| Charger | Start charging, Stop charging, Set charging current, Set circuit current (master only), Enable the charger, Disable the charger, Wait between charger commands |
| Car | Wait before starting the car, Wait for the charger to be ready, Start charging in the car, Stop charging in the car, Set charging current in the car, Set charge limit in the car, Wait between car commands |

On start: charge limit in the car → enable the charger → circuit limit → current in the charger → current in
the car → start the charger → start the car. On stop, both are stopped.

The waits:

| Input | Default | What it does |
|---|---|---|
| Wait between charger commands | 0 s | Pause between two charger commands in the same run (enable/disable, circuit current, charging current, start/stop) – not before the first one, and not for the car's commands. E.g. 2–5 s for Easee. |
| Wait between car commands | 0 s | Pause between two commands to the car in the same run (charge limit, charging current, start) – not before the first one. |
| Wait before starting the car | 0 s | Fixed time from the charger's commands to **Start charging in the car**. If the car got a command before, the longer of this and *Wait between car commands* is used. |
| Wait for the charger to be ready | on | Before **Start charging in the car**, wait (at most 30 s) while every status entity under *Charging state* is one of **States that mean the charger is not ready** (default `awaiting_start`, `disconnected`, `off`), e.g. until the Easee cloud has applied the current. A car told to charge before the charger gives power may give up. Turn it off if the status entities are only the car's – they stay "not ready" until the car is started. |

The automation carries out one decision at a time, so while a wait or check runs, a new decision (e.g. stop)
waits for it – about a minute at most with the defaults. If one action fails (e.g. the charger's cloud doesn't answer), the
error is logged and the remaining actions still run.

The automation runs **once per change** of the decision (the `decision` attribute), however much changed:
a new action runs the whole start or stop, otherwise only what changed is sent (current, circuit, charge
limit, enable/disable). The trace's `changed` variable shows what it was, e.g. `['amps', 'circuit']`, so each
trace under *Traces* is one whole decision. A calculation that changes nothing doesn't run it.

In your actions you can use the variables `phase_1`, `phase_2`, `phase_3`, `max_phase`, `phases`,
`circuit_phase_1`, `circuit_phase_2`, `circuit_phase_3`, `charging_limit`, `charger_enabled`, `fail_safe` and
`message`. Use them rather than reading the sensors yourself – in fail-safe they hold the fail-safe current.

**Typical setups**

| Setup | Fill in | Example |
|---|---|---|
| The charger can be controlled, the car has no integration (or can't be controlled) | Charger | Easee, Zaptec, Monta, Wallbox or an OCPP charger with a virtual EV |
| The car can be controlled, the charger has no integration ("dumb" charger, optionally measured with an energy meter) | Car | VW/Cupra/Skoda, Kia/Hyundai, Tesla … on a charger without integration – e.g. measured with a Shelly 3EM |
| Both the charger and the car have an integration | Each task where it can be done | current and start/stop in the charger and charge limit in the car – or start/stop and charge limit in the car and current in the charger |

**Typical actions** – the names depend on the integration; find them under *Developer tools → Actions*
and *States*:

| Input | Charger integration | Car integration |
|---|---|---|
| Start / stop | a start/stop or pause/resume service (e.g. Easee `action_command`), a start/stop switch (e.g. Monta, Wallbox, OCPP *Charge Control*) or a button | a charge switch (e.g. Tesla/Tessie) or a start/stop service (e.g. Kia/Hyundai Connect `start_charge`/`stop_charge`, the VW/Cupra integrations) |
| Charging current | a dynamic limit service (e.g. Easee, Zaptec) or a number for max. current (e.g. OCPP *Maximum Current*, Wallbox *Max charging current*) – `{{ max_phase }}` or per phase `{{ phase_1 }}` … | only some cars have a number for the current (e.g. Tesla/Tessie *charge current*) – `{{ max_phase }}` |
| Charge limit | – | a number for the charge limit (e.g. *charge limit*, *AC charging limit*, *target state of charge*) – `{{ charging_limit }}` |
| Enable / disable | the charger's "enabled" switch – select the entity, not the device | – |

If neither the charger nor the car can set the current, the blueprint only starts and stops charging –
solar surplus charging and load balancing then can't regulate the current.

For ready-made actions for KEBA, Wallbox, Easee, myenergi and Ohme, see [Ready-made charger actions](chargers.md).

### Shared circuit

For chargers controlled through a circuit limit, e.g. Easee chargers behind a master. Fill in **Set circuit
current (master only)** **only in the automation of the car whose charger is the master**, and leave it empty in
the other cars' automations – otherwise several automations set the same circuit. A single charger is its own
master, so fill it in there too: Easee sets the current per phase through the circuit limit, while the charger's
own limit (**Set charging current**) is the highest current it may use on its phases, and the lower of the two
applies. Example for Easee:

```yaml
action: easee.set_circuit_dynamic_limit
data:
  device_id: <the master charger>
  current_p1: "{{ circuit_phase_1 }}"
  current_p2: "{{ circuit_phase_2 }}"
  current_p3: "{{ circuit_phase_3 }}"
```

Link the chargers under each car's *Load balancing* page (**Chargers on the same circuit**, see
[Shared circuit](load-balancing.md#shared-circuit-eg-easee-master)). Every car of the circuit then gets the sum
per grid phase of all the circuit's chargers, and the master's automation runs when it changes – also when
another car on the circuit starts or stops. The charging current of each charger (**Set charging current**) is
still set in each car's own automation. Without linked chargers the values are the car's own circuit limit. In
fail-safe the circuit gets the fail-safe current, which the circuit's chargers share.

### Charging state

Tells the blueprint whether it charges, so start isn't sent again and again. Status and power can each be
several entities, e.g. both the charger's and the car's – **one of them is enough**:

| Input | Meaning |
|---|---|
| Status or "charging" entities | the charger's and/or the car's status sensor, or a switch/binary sensor that is `on` while charging |
| States that mean charging / done | e.g. `charging` and `completed`, `finished` (OCPP: `Charging`, `Finishing`) |
| States that mean the charger is not ready | e.g. `awaiting_start`, `disconnected`, `off` (OCPP: `Preparing`) – the charger gives no power (yet or any more) |
| Charging power + Charging above | power sensors for the charger and/or the car, with a threshold (default 500 W) |
| Check that it charges or stops | on by default – see below |

A new charging decision from CJP always sends start.

**Check that it charges or stops:** 30 s after a start or stop the blueprint checks the entities above, and
one of them is enough:

| | Status entity | Power |
|---|---|---|
| Started | in *States that mean charging* or *States that mean done* | above *Charging above* |
| Stopped | in *States that mean the charger is not ready* | below *Charging above* |

If it hasn't started, the charging current and start are sent once more to the charger and the car; if it
hasn't stopped, stop and the current are sent once more. Because one entity is enough, the charger's entities
alone can check a car that can't be read.

**Charging without a rule:** a car or charger can start by itself when it is plugged in (e.g. a charger that
resets its current limit for a new session, or a car that charges as soon as the cable is in) while the action
already is `stop`. Nothing in the decision changes then, so the automation wouldn't run. When CJP sees charging
power without a rule (⚠️), it counts `stop_request` up on every calculation while it lasts, and the automation
sends **Stop charging**, **Stop charging in the car** and the current (0 A) once more – and **Disable the
charger** when it should be disabled. Update the blueprint (Repairs) to get this.

**A charger that doesn't start:** CJP restarts a charger itself when the action has been `charge` for more
than two update intervals (10 minutes by default) and the charger's status still is ready (e.g.
`awaiting_start`) or `error`, while *Charging power* shows no charging, now and on average. Without a power
reading there is no restart: a status alone can be stuck. That includes no history of the power sensor
(the recorder off, or the sensor excluded from it): the average needs one. The two intervals
are counted from when CJP first saw it waiting with a current to charge at (a new charge, the site giving
current again, or the power stopping), the end of a charger test and the last restart. One interval later,
still so, the action becomes `stop` at 0 A – or `charger_enabled` false, with an *enabled* entity – and you
get a notification. One interval after that the charger is enabled again and the plan publishes `charge`.
After three restarts it gives up and notifies you once more (on the alarm channel). As a fail-safe the action
is then `charge` at full power (the charger's maximum current; the charge limit is the plan's, or the max
recommended level if that is higher), whatever the plan says – also in hours the plan wouldn't charge, and
from the grid in the solar modes – so the car charges as soon as it can; the site's load balancing still
limits the current, and the cars behind it get what it doesn't draw (see
[Multiple cars](load-balancing.md#how-it-works)). The charging rule shows why and the fail-safe. It lasts, and no new restart is tried,
until the charger reports charging (with power, when the power can be read) or done, *Charging power* shows
charging (also when the status stays ready), or the cable has been unplugged – not after the hour shift, a stop of the plan or a charger test. Then the plan
decides again (and may stop it at once if the hour is expensive). With an *enabled* entity, a charger the restart has disabled may report `off` or
`disconnected`; that isn't taken as an unplug (or the status back as a plug-in) until the entity is on again and
two update intervals, at least 10 minutes, have passed – at most an hour after the restart. An unplug in that
time is handled when it is over, if the status still says so; an unplug and plug-in within it isn't seen. A car that
stops by itself (e.g. at its own charge limit) while the charger reports ready and gives no power can be
restarted this way too. With an update interval above 15 minutes the restart is slow (the update runs at
least every 15 minutes, the intervals are counted with the setting).

The blueprint's own *Restart when not charging* (below) runs as well: with its default **Restart after** (10
minutes) it comes before CJP's first stop, and each CJP stop makes it count from the start again (with an
*enabled* entity the action stays `charge`, so it doesn't). The two don't block each other: a blueprint start
in CJP's pause only shortens the pause. Turn the blueprint's restart off, or set it above an hour, if you
want CJP's alone.

### Restart when not charging

Actions that run when the action has been `charge` for a while (**Restart after**, e.g. 10 minutes) without
charging – typically `homeassistant.reload_config_entry` targeting one of the charger's or the car's
entities, which reloads that integration. It is repeated after another 10 and 20 minutes (**Max number of
restarts**, default 3), you get a notification, and 1 minute later the current and start are sent again.
Needs *Charging state*.

### Fail-safe

If `charger_action` becomes unavailable, or its `updated` attribute is older than **Fail-safe after**
(default 20 min), the integration has stopped or hangs. The blueprint then charges with **Fail-safe charging
current** (default 16 A) on **Fail-safe phases** (default 3) and sends the **Notification**. The variable
`fail_safe` is `true` in your actions.

## Test your charger automation

To find out whether your automation does what the plan asks, run the test: turn on **Test my charger automation**
under *Settings* on the dashboard (the switch `switch.cjp_<prefix>_charger_test_run`). A car must be plugged in and
not full. CJP then publishes one charging current after the other on `sensor.cjp_<prefix>_charger_action`, reads
the charger's power sensor, and shows the result as a table under the switch (the attribute `markdown` of
`sensor.cjp_<prefix>_charger_test`).

As soon as CJP knows which steps it will try (a few seconds after the start), the table lists every one of them, so you
can see what will be tested and how far the test is: ⏳ waits, ▶️ is tested now, and a step gets its result (✅, ⚠️,
❌ or 🔧) when it is done. A line above the table counts the steps that are done, and when the test ends only the
results are left. A step that will not be tried (the charging steps and the stop after a charging step ended the test
early) is shown as not reached as soon as that is known, and so is every step that was not finished when you stop the
test. Without solar steps or without a way to set the current only the maximum and the stop are listed, and the charge
limit step when the car has a charge limit entity. The sensor's attributes `plan` and `current` hold the listed steps and
the one that is tested now while the test runs.

| Step | What is published | Passes when the charger's power is |
|---|---|---|
| Maximum | the charger's maximum amps on all its phases | the amps × phases × the configured voltage |
| Lowest step | the lowest step of the solar calculation on all phases | the same for those amps |
| Highest 1-phase step | the highest single phase step | the same on one phase |
| Lowest 1-phase step | the lowest single phase step | the same on one phase |
| Stop | stop | under 100 W (the last current that worked is published again first, and the stop is only tried when the charger then charges at 1 kW or more). After the stop, a charger status such as *completed* or *awaiting start* is what a stop looks like (an automation that stops the charging in the car makes the charger report *completed*, as for a full car); only an unplugged cable ends the step |
| Charge limit in the car | only with the car's *charge limit* entity: a charge limit 10 percentage points from the car's limit | the car's charge limit entity shows it within 5 minutes (the power is not judged) |

**The test has three parts that end on their own**: the charging steps, the stop and the charge limit. When the charging
steps end early (no charging at the maximum, an overdraw, the car unplugged or full), the other charging steps and the
stop are not reached (the stop needs a charge just before it); when the stop ends early, nothing else is lost. The
charge limit step (when the car has a charge limit entity) depends on neither and still runs. Only the switch and an
internal error end the whole test; a test stopped after a part ended early still shows why that part ended.

**The charge limit step** runs last, while the action of the step before is still held, so the automation sends the
new limit in a run of its own (*Set charge limit in the car*; a limit that changes together with a stop is never sent).
After the charging steps or the stop ended early (a stop reason such as an overdraw, the car full or unplugged), CJP
first holds a stop and waits 15 seconds, so the limit is not tested on the charge of the step that ended the test.
Otherwise the limit is changed on the action held by the step before. That stop replaces the test's own charge also when the charger is unplugged
or reports *completed*, so a car that resumes or is plugged in again is not charged at the test's current meanwhile.
The limit asked for is 10 percentage points above the car's limit now, or 10 below it while that is still above a
known battery level (a lower limit would make the car stop or report *completed*; no level never allows it, while the
car's last level of up to 24 hours ago counts as known),
within the entity's own minimum and maximum, the *min. charge limit* setting and 100 %, and rounded to 10 % for the
integrations the plan rounds for (Kia/Hyundai, Cupra). It is never the limit the plan published already, nor the car's
limit from before the test (the automation only sends a change). When no such limit exists, or the entity has no number
(the car asleep, its integration offline), the step is not tested; without steps in *Set charge limit in the car* it is
not tried. The charge steps before it may already have sent the plan's limit to the car (the automation sends it with
every new charge). Afterwards CJP publishes the car's limit **from before the test** again – also when the test is
stopped during the step – and waits up to 5 minutes for the car to show it, before the plan's action is put back.

The result says the limit is back only when the car showed the test limit first and then its own again. Otherwise –
the car never showed the test limit (it may still arrive from a slow cloud), its own limit was not seen back, Home
Assistant unloaded CJP or stopped during the step (the limit is published back but not waited for), or restarted
before it was put back – the result tells you to check the limit in the car.

The solar steps are those of the solar charging calculation (the amps CJP can pick from, on one phase and on all
phases, between the minimum and maximum amps of solar charging in the settings). Without solar charging, or when the
blueprint has nothing in *Set charging current*, *Set charging current in the car* or *Set circuit current*, there are
no steps to follow and only the maximum (it passes when the charger starts) and the stop are tried (and the charge
limit, with its entity). A charger that pauses
at 0 A can be started and stopped by *Set charging current* alone, so those steps count for the start and the stop too.

The power must be within 5 % (at least 150 W) of the expected power, so the voltage in the charger specs has to be
right: it is what CJP plans with, too. A step is judged on two readings 10 seconds apart, taken 20 seconds or more
after the current was published, from the average of three readings.

**A voltage setting that is not your grid's** (240 V on a grid that gives 223–229 V under the load) fails currents the
charger took exactly, and the search would end on a lower one that happens to fit. So CJP measures the voltage from the
power per amp, separately on all phases and on one phase, over every step: when the currents that charged span at least
1.5 times (16 A and 8 A, not 16 A and 14 A), their power per amp is the same within 2 %, and that voltage is more than
2 % from the setting (and within 85–110 % of it), those steps are judged again at the measured voltage. The result
shows *Judged at the measured … V* on the steps and a note with the voltage and your setting; set the grid voltage to
your grid's (230 V in most of Europe). A charger that gives an amp less than asked usually does not look like this: its
power per amp changes over the span, so its steps are judged at the setting as before. Neither does a car that limits
its power. The steps are judged again from what was measured after each step, so a voltage that later steps no longer
confirm is dropped, and the steps go back to their result at the setting.

The **maximum comes first**: it shows that the charger charges at all. If the power stays under what a start needs
(5 A) at every current tried, the other charging steps and the stop are not tried (the charge limit still is). If it
charges, but not at the maximum, the test goes on.

If a step fails (the maximum too), CJP searches for the current that works. It goes two amps towards the middle of the
group (up for the *lowest* steps, down for the *highest*), and when that works it tries the one amp in between, so the
result is the current closest to the step that works: *6 A: 0 W, 8 A: 1 840 W, 7 A: 0 W* means 8 A is the lowest that
works. At most four currents are tried after the step's own, and never outside the group's steps (except the one lower
current that confirms a car on fewer phases, below).

What the power shows besides a pass or a fail:

- **The power does not rise with the amps** (*16 A: 8 000 W, 14 A: 8 000 W, 12 A: 8 000 W*): the car or the charger
  limits it. The step is shown with ⚠️, the power it stayed at and the current that gives that power. A full or cold
  battery does that for a while: leave the setting. A car that never takes more (an on-board charger smaller than the
  charger, such as an 11 kW car on a 22 kW charger) needs the maximum amps setting lowered to that current, or the plan
  counts on power the car does not take.
- **The power is the same at every current of every step** (*16 A: 4 140 W … 6 A: 4 140 W*), judged on three phases
  and on one phase separately: nothing shows that the charger follows the amps. A charger the automation never sets stays
  at one current, which may happen to match a step (also when it does switch the phases). The steps that charged are
  then shown with 🔧 (not proven) and are not counted as working.
- **The car charges on fewer phases** than asked (a car with a 1- or 2-phase on-board charger on a 3-phase charger): the
  power matches the amps on fewer phases. This is only believed when two currents match the fewer phases at clearly
  different powers (a car that limits its power can match fewer phases at one current); when the step has no other
  current to try (for example the maximum without solar steps), two amps less than the lowest tried confirms it, once.
  From then on every step is judged
  on the car's phases, and the result says so.
- **The charger takes clearly more than it was given** (more than 25 %, at least 500 W, for a whole minute), for example
  an automation that does not switch the phases: the charging steps stop at once. The load balancing counts on the published
  amps, so it would plan with too little. An average above that limit never passes a step, also not a step that only
  needs the charger to start.
- **The car was already charging** when the test began: the first step that charges is shown with ⚠️, because the start
  is not proven.
- **The load balancing limits the car during a step**: the step ends at once and is not tested. When the site has no
  current left for the car, CJP publishes *stop* (with 0 A), like the plan does, also while the test holds a current.

- ✅ **Worked** at the step's current.
- ⚠️ **Worked, see the note:** at another current (the lowest or highest current the charger and the car accept is not
  the step's; change the matching minimum or maximum amps setting if it should be the one that worked), at a power the
  car or the charger limits, or with a start that is not proven.
- ❌ **The charger did not deliver:** open the automation's trace and check that it ran *Set charging current* with the
  amps of the action, and that the charger took the command. The power also differs when the real voltage is not the
  voltage setting.
- 🔧 **Not tested:** the load balancing gives the car less than the step asks for (a step is never pushed past it), the
  power sensor had no number or had not reported for 3 minutes, the automation has no steps for it, a stop could not be
  proven because the charger was not charging just before, or the power was the same at every current, so the amps are
  not proven.

CJP reads `automations.yaml` (only reads it) for the automation made from the blueprint that uses this action sensor, to
see which fields have steps. An automation kept in a package or in YAML mode is not found; the result then says so, and
the amps are tested if the plan steps by amps.

What the test does and does not do:

- It charges the car for a few minutes at currents the plan may not have chosen. The load balancing still limits the
  current, and a shared circuit sees the request. During the stop step the circuit current stays open, like the plan's
  own stop. The plan and the settings are not changed. With a charge limit entity, the car's charge limit is changed for a
  few minutes and then set back (see the charge limit step). If the plan was not charging, the energy the test charges
  may be missing from the charging history and the estimated battery level.
- The plan does not run while a current is held: no restart of the charger, no *charging without a rule*. The heartbeat
  `updated` is kept, so the blueprint's fail-safe does not start. A car that pauses after a current it does not take is
  therefore not restarted by CJP during the test.
- While a current is held, `sensor.<prefix>_current_charging_rule` shows *🧪Testing your charger automation: …* with
  the current and phases the step asks for (the load balancing may give fewer, shown on the 🚧 line and in the amps
  of the action), or *Stop charging* (also when the load balancing has no current for the car). During the charge
  limit step it also shows the charge limit that is published. No other text replaces it while the test holds the
  charger. When the test ends, the plan's own rule comes back. If the rule from before the test was a restart of the
  charger that the test ended, the rule says that the test has ended until the plan's next calculation.
- Turning the switch off stops the test: the action, enabled and amps from before the test are put back, and the plan is
  calculated again at once (also with a setting changed during the test). The charge steps end at once if the cable is
  pulled out or the car becomes full (the charge limit step only waits for the car's limit, which also works then). A test is never resumed after a restart; the plan publishes again when CJP starts. If the
  test itself is lost, the held current is dropped by the first calculation after 4½ minutes (during the charge limit
  step after up to 12 minutes). The car's limit from before the test is then published together with the plan's action:
  the automation sends it when the action stays the same, but not with a change to *stop*, and a plan that charges sends
  its own limit instead – check the limit in the car then.
- A whole test takes about 5 minutes (up to 10 more with the charge limit step) with a charger that follows, and up to an hour if every step needs the full search.
- The result of the last finished test is kept over a restart.

## Your own automations

Without the blueprint, e.g. the charge limit and the car requests:

```yaml
alias: CJP - charge limit
triggers:
  - trigger: state
    entity_id: sensor.cjp_charging_limit
conditions:
  - "{{ trigger.to_state.state | is_number }}"
actions:
  - action: number.set_value
    target:
      entity_id: number.my_car_charge_limit
    data:
      value: "{{ trigger.to_state.state | int }}"
---
alias: CJP - wake and preheat the car
triggers:
  - trigger: state
    entity_id: event.cjp_ev_request
actions:
  - choose:
      - conditions: "{{ trigger.to_state.attributes.event_type == 'wake_up' }}"
        sequence:
          - action: button.press
            target:
              entity_id: button.my_car_wake
      - conditions: "{{ trigger.to_state.attributes.event_type == 'preheat_start' }}"
        sequence:
          - action: climate.turn_on
            target:
              entity_id: climate.my_car_climate
      - conditions: "{{ trigger.to_state.attributes.event_type == 'preheat_stop' }}"
        sequence:
          - action: climate.turn_off
            target:
              entity_id: climate.my_car_climate
```
