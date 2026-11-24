# Learn departure and homecoming

**Settings → Advanced:** *Learn departure and homecoming* (off by default) · needs a charger whose status CJP follows
(no EV integration needed)

The switch controls whether the learned times are used; the suggestions are still calculated and shown when it is off.
With it on, *Work plan* leaves **earlier** or comes home **later** than entered when that is what the car really does,
and never the other way round.

Instead of keeping the departure and homecoming of every weekday up to date by hand, CJP can learn them from when the
car is unplugged from and plugged in to the charger at home.

## How it works

- CJP records every connection of the car to the charger (plug-in and unplug) from the moment it is installed. At the
  first start it also reads the last days of the charger's status from Home Assistant's history, but it keeps the
  connections only as far back as its charging history reaches (the economy data), so a new installation starts with
  little or nothing. Only what the charger really reported counts, never the connections the What-if models from the
  work plan.
- **Departure** of a day: the *first unplug* of that day between **04:00 and 12:00** (local time).
- **Homecoming** of the day: the *plug-in the same day* that follows it.
- A day counts only when:
  - the weekday is a **workday** in the work plan,
  - the car is plugged in again the **same day**, at least **30 minutes** after the unplug (a shorter unplug and
    plug-in is a re-plug of the cable, not a departure),
  - no trip covers the date. The trip settings are reset two hours after a trip starts, so CJP keeps the dates of
    every trip that has started (in its runtime data) for as long as the learning looks back. A trip that is only
    planned, moved or cancelled before it starts leaves nothing behind.

  Days off without a morning unplug give no sample by themselves, and a day the car stays away overnight is a trip, not
  a workday. A short errand in the morning that is back within 30 minutes costs the whole day (the first unplug of the
  day is the departure, whatever follows), and so does a charger whose status drops out for a moment.
- From the last **8 weeks**, per weekday, at least **3 days** are needed:

  | Time | Suggestion | Why |
  |---|---|---|
  | Departure | the **25th percentile**, rounded **down** to 15 minutes | the plan is ready for the early days too |
  | Homecoming | the **75th percentile**, rounded **up** to 15 minutes | the plan counts on the car being away for the late days too |

- The suggestions are always shown as the `learned_time` and `learned_days` attributes of the departure and homecoming
  time entities – also with the switch off.
- With **Learn departure and homecoming** on, the plan uses, for each weekday with at least 3 days:
  - the **earlier** of the entered and the learned **departure**, and
  - the **later** of the entered and the learned **homecoming**.

  So a learned time only moves the departure earlier or the homecoming later than entered, never the other way. The
  entered times are never changed. The entered times are used instead:
  - on a day with a trip (the order of the trip and the work plan depends on them),
  - for the departure of today, while the car is still plugged in after the learned departure (it did not leave then, so
    today's work plan is still ahead). That the car is still here is read from the charger's connection: an open
    connection means plugged in at home, and a charger status that drops out is never taken as the car having left. The
    homecoming of today is still the later of the entered and the learned,
  - on a weekday with a night shift (the departure entered later than the homecoming).
- It is used by the work plan and the plan's consumption forecast. **Preheating keeps the entered departure**: it is a
  comfort setting, and preheating for a departure that is early on a quarter of the days would run on all the others. The
  *Ready at departure* analysis measures at the departure the plan uses. The *What-if* replay and the analysis of the past
  connections keep using the entered times.
- If anything about the learned times cannot be read, the entered times are used.
- *Analysis → Learned departure and homecoming* shows the entered, the learned and the used times per weekday – see
  [Dashboard](dashboard-analysis.md#learned-departure-and-homecoming).

**Example:** the last six Fridays you left at 06:40, 06:45, 06:50, 06:35, 06:40 and 07:10, and came home at 16:10,
17:05, 17:20, 16:30, 16:50 and 17:40. The 25th percentile of the departures is 06:40 (rounded down: 06:30) and the 75th
percentile of the homecomings is 17:16 (rounded up: 17:30). With 07:00 and 16:00 entered, the plan uses 06:30 and 17:30
on Fridays.

Not counted: a Friday with a trip, a Friday you left at 13:00, a Friday the car came back the next morning.
