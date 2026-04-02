# Python Performance Baseline

## Purpose

This note records a concrete Python-side performance baseline before any C# rewrite work. It captures the comparison between the older baseline behavior and the now-adopted default optimized behavior:

- historical baseline: on-disk SQLite state and compact JSON enabled
- current default: in-memory SQLite state and no large durable JSON report

The goal is to understand how much benefit we can get from architectural changes inside Python before deciding how urgent a full rewrite is.

## Benchmark Case

- Working directory: `G:\nas\Andrew\Projects\Macrium Analyzer`
- Target file: `H:\backup\macrium\94D5B2EDD3B8674D-04-04.mrimgx`
- Image window: 4 total images ending at the target
- Selected restore points: file numbers `1, 2, 3, 4`

## Commands

Baseline:

```powershell
$env:PYTHONPATH = 'G:\nas\Andrew\Projects\Macrium Analyzer\macrium-analyzer\src'; Set-Location 'G:\nas\Andrew\Projects\Macrium Analyzer'; python -m macrium_analyzer.cli analyze-mrimgx --file 'H:\backup\macrium\94D5B2EDD3B8674D-04-04.mrimgx' --image-count 4 --progress-file 'G:\nas\Andrew\Projects\Macrium Analyzer\analyses\run-status-94D5B2EDD3B8674D-04-04-baseline.json' --output-base 'G:\nas\Andrew\Projects\Macrium Analyzer\analyses\94D5B2EDD3B8674D-04-04-baseline.analysis'
```

Current default:

```powershell
$env:PYTHONPATH = 'G:\nas\Andrew\Projects\Macrium Analyzer\macrium-analyzer\src'; Set-Location 'G:\nas\Andrew\Projects\Macrium Analyzer'; python -m macrium_analyzer.cli analyze-mrimgx --file 'H:\backup\macrium\94D5B2EDD3B8674D-04-04.mrimgx' --image-count 4 --progress-file 'G:\nas\Andrew\Projects\Macrium Analyzer\analyses\run-status-94D5B2EDD3B8674D-04-04-optimized.json' --output-base 'G:\nas\Andrew\Projects\Macrium Analyzer\analyses\94D5B2EDD3B8674D-04-04-optimized.analysis'
```

## Results

| Variant | Total time | Peak working set | State storage | JSON output | Viewpack output |
| --- | ---: | ---: | --- | --- | --- |
| Baseline | 5m 49s | 1.10 GiB | On-disk SQLite | Yes | Yes |
| Current default | 3m 33s | 1.22 GiB | In-memory SQLite | No | Yes |

### Phase timing observations

Baseline:

- Rollup started at `5m16s`
- Rollup completed at `5m35s`
- JSON completed by `5m40s`
- Viewpack completed by `5m49s`

Current default:

- Rollup started at `3m24s`
- Rollup completed at `3m28s`
- Viewpack completed by `3m33s`

### Output sizes

Baseline:

- text: `17,987` bytes
- JSON: `82,824,461` bytes
- viewpack: `80,969,393` bytes
- SQLite state: `176,340,992` bytes

Optimized:

- text: `17,987` bytes
- viewpack: `80,969,292` bytes

## Interpretation

- The current default Python behavior was materially faster on this case: about `2m16s` faster, or roughly a `39%` reduction in total wall time.
- Removing the large JSON and avoiding the persisted SQLite state file clearly helped overall runtime.
- Peak memory did **not** improve in this experiment; it increased modestly from about `1.10 GiB` to `1.22 GiB`.
- The large JSON file does not appear to be the main runtime cost by itself, but it is still substantial disk output and part of the slower baseline path.
- The tiny text report appears negligible. It did not show up as a distinct measurable phase in the logs, which suggests its cost is below the current progress-log granularity on this benchmark.
- The biggest observed win came from the combined optimized mode, not from text-output removal.

## Use As Rewrite Baseline

This benchmark should be reused when comparing the future C# implementation. At minimum, the rewrite should be evaluated against this same 4-image case for:

- total wall time
- peak memory
- time to results-ready state
- correctness of top contributors and totals
