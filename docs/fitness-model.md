# Runwise fitness model

Runwise exposes one deterministic calculation for a run:

```python
compute_fitness(run, weather=None, analysis=None)
```

The result always has `score`, `method`, `confidence`, and `factors` keys. It
does not call a provider, an LLM, or the database. `run` and the optional
inputs may be mappings or objects with the same field names. The factors keep
the original `run_type`, the normalized `evidence_class`, and both
`prediction_eligible` and `used_in_coaching_race_predictions` flags so a
caller can distinguish an eligible anchor from a displayed training score.

`score` is a VDOT value for a race or time trial. For training runs it is a
lower-confidence, pace-derived lower-bound effort index. The numeric value is
not reduced by an invented run-type constant; the run type changes the method,
confidence, evidence class, and coaching eligibility. This keeps an easy, long,
or interval run useful on the run detail while preventing its pace from being
presented as an all-out performance.

## Evidence classes

Run type and title are classified in this order:

1. `race` for an explicit race/competition label;
2. `time_trial` for an explicit time trial or `TT` label;
3. `interval` for repetitions, track, fartlek, or interval labels;
4. `tempo` for tempo, threshold, or steady-state labels;
5. `easy`, `long`, `quality`, or `general` for the remaining training runs.

Race and time-trial rows use elapsed finish time and are the strongest
evidence. Tempo is supporting evidence and remains medium confidence. Easy and
long runs are low-confidence proxies. Interval rows use the whole-run value
only as a training proxy; the average including recoveries is never promoted
to race evidence. A stream may later supply an interval-specific analysis,
but that does not change this conservative default.

## VDOT calculation

For sustained hard efforts, Runwise uses the Daniels--Gilbert conversion. If
`v` is speed in metres per minute and `t` is duration in minutes:

```text
VO2 = -4.60 + 0.182258 v + 0.000104 v²
fraction = 0.8 + 0.1894393 exp(-0.012778 t)
                    + 0.2989558 exp(-0.1932605 t)
VDOT = VO2 / fraction
```

Inputs outside 2:00--20:00 per kilometre or below 1 km are rejected as
insufficient. The score is bounded to 20--90 so malformed or implausibly
short records cannot create a training recommendation. The formula is a
race-performance equivalence model; it does not measure laboratory VO2max.

## Weather correction

Weather is read from the existing optional weather response (`status`,
`temperature_c`, and `humidity_percent`). Missing or unavailable weather does
not fail a run: its correction is zero and `factors.weather_missing_reasons`
states what was unavailable. Cold conditions are also left neutral because a
reliable cold adjustment would need wind, clothing, and acclimation data.

When temperature is above 15 °C, a single heat-stress proxy is formed:

```text
temperature_component = temperature - 15
humidity_component = max(0, relative_humidity - 50) × 0.08
heat_penalty = clamp((temperature_component + humidity_component) × 0.004, 0, 0.15)
```

Humidity contributes only inside that combined warm-condition term. It is not
charged as a second full penalty, so a humid hot run cannot receive a
duplicate heat adjustment. A humidity reading without temperature is retained
for display but does not create a guessed penalty. The observed duration is
converted to a neutral-condition duration by dividing by the bounded
multiplier. Consequently, an equally paced hot run receives a slightly higher
neutral-condition score, while the result still records the penalty and its
heuristic method.

## Elevation correction

The preferred elevation input is one of `total_ascent_m`,
`total_elevation_gain_m`, `elevation_gain_m`, `positive_elevation_gain_m`, or
`ascent_m` in `analysis` (then the run). If no aggregate exists, positive
differences from `altitude_m`/`elevation_m` samples are summed once. Drops do
not earn a downhill credit, and implausible ascent is capped at 320 m/km.

```text
ascent_penalty = clamp((total_ascent_m / distance_km) × 0.00025, 0, 0.08)
```

This is a small total-ascent proxy. A grade-adjusted pace algorithm would need
the full grade profile, direction, surface, and downhill cost. Runwise marks
the factor as heuristic rather than claiming that the Minetti treadmill
energy-cost study has been reproduced.

Heat and ascent multipliers are combined multiplicatively and capped
individually. The result includes raw and neutral duration/pace, raw VDOT,
environment-adjusted VDOT, both penalties, data-source flags, and whether the
row is eligible as coaching evidence.

## Coaching use

Coaching first prefers recent race and time-trial rows whose fitness result is
anchor eligible. Their neutral-condition duration is projected with the
existing Riegel relationship (`T2 = T1 × (D2 / D1)^1.06`). If no race evidence
exists, a tempo/threshold result can support a medium-confidence projection.
Only as a final low-confidence fallback may a sustained easy/long/general run
be used, with its explicit training projection multiplier (1.15 for easy,
1.12 for long, 1.10 for general). Interval rows are excluded. This preserves
the existing coaching API shape while making the uncertainty visible in the
prediction method, confidence, and fitness fields.

## Evidence and limits

The requested [Colin Eberhardt Claude Running Coach repository](https://github.com/ColinEberhardt/claude-running-coach)
describes race-goal anchored pace calculation and evidence-based coaching. Its
[`calculate_paces.py`](https://raw.githubusercontent.com/ColinEberhardt/claude-running-coach/main/running-race-coach/skills/training-plan/scripts/calculate_paces.py)
uses a Riegel exponent of 1.06 and labels easy, threshold, VO2max, repetition,
and race zones as guidance. Runwise follows that transparent spirit but uses
the Daniels--Gilbert VDOT equation only for hard efforts.

The official [V.O2 overview](https://vdoto2.com/about) describes VDOT as a
Jack Daniels performance-based training method. VDOT is useful for equivalent
race performances; it is not evidence that an easy run was maximal.

For environment context, a 1,258-race analysis found the most favourable
conditions around 10--17.5 °C air temperature and reported roughly 0.3--0.4%
performance decline per degree WBGT outside the optimum
([Mantzios et al., 2022, PubMed](https://pubmed.ncbi.nlm.nih.gov/34652333/)). A
controlled study in trained runners found increased thermoregulatory stress
and reduced subsequent exhaustion time as humidity rose at 31 °C
([Che Muhamed et al., 2016, PubMed](https://pubmed.ncbi.nlm.nih.gov/28349085/)).
Those studies support considering heat and humidity; they do not validate this
small proxy for every recreational run, distance, wind, sun exposure, or
acclimation state.

For hills, Minetti and colleagues measured running energy cost across treadmill
grades ([Minetti et al., 2002, Journal of Applied Physiology](https://doi.org/10.1152/japplphysiol.01177.2001)).
Runwise uses only a conservative total-ascent heuristic and does not claim to
reproduce that grade-dependent polynomial. Weather and elevation therefore
remain explanatory adjustments with explicit caps and confidence, not a
validated extension of VDOT.
