# Python Performance Baseline

## Purpose

This note records a concrete Python-side performance baseline before any C# rewrite work. It captures the comparison between the older baseline behavior and the now-adopted default optimized behavior:

- historical baseline: on-disk SQLite state and compact JSON enabled
- current default: in-memory SQLite state and no large durable JSON report

The goal is to understand how much benefit we can get from architectural changes inside Python before deciding how urgent a full rewrite is.

This note now also includes an experimental parallel-image worker result on the same benchmark case.

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

Parallel workers:

```powershell
$env:PYTHONPATH = 'G:\nas\Andrew\Projects\Macrium Analyzer\macrium-analyzer\src'; Set-Location 'G:\nas\Andrew\Projects\Macrium Analyzer'; python -m macrium_analyzer.cli analyze-mrimgx --file 'H:\backup\macrium\94D5B2EDD3B8674D-04-04.mrimgx' --image-count 4 --parallel-images 4 --progress-file 'G:\nas\Andrew\Projects\Macrium Analyzer\analyses\run-status-94D5B2EDD3B8674D-04-04-parallel4.json' --output-base 'G:\nas\Andrew\Projects\Macrium Analyzer\analyses\94D5B2EDD3B8674D-04-04-parallel4.analysis'
```

## Results

| Variant | Total time | Peak working set | State storage | JSON output | Viewpack output |
| --- | ---: | ---: | --- | --- | --- |
| Baseline | 5m 49s | 1.10 GiB | On-disk SQLite | Yes | Yes |
| Current default | 3m 33s | 1.22 GiB | In-memory SQLite | No | Yes |
| Parallel workers (`--parallel-images 4`) | 1m 34s | 1.15 GiB | In-memory SQLite | No | Yes |

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

Parallel workers:

- Parallel merge/update phase started immediately after state init
- Rollup completed by `1m27s`
- Viewpack completed by `1m34s`

### Output sizes

Baseline:

- text: `17,987` bytes
- JSON: `82,824,461` bytes
- viewpack: `80,969,393` bytes
- SQLite state: `176,340,992` bytes

Optimized:

- text: `17,987` bytes
- viewpack: `80,969,292` bytes

Parallel workers:

- text: `17,987` bytes
- viewpack: `80,968,744` bytes

## Interpretation

- The current default Python behavior was materially faster on this case: about `2m16s` faster, or roughly a `39%` reduction in total wall time.
- Removing the large JSON and avoiding the persisted SQLite state file clearly helped overall runtime.
- Peak memory did **not** improve in this experiment; it increased modestly from about `1.10 GiB` to `1.22 GiB`.
- The experimental parallel-image worker mode was faster again: about `1m59s` faster than the current default, or roughly a `56%` reduction in total wall time on this 4-image case.
- Peak memory with 4 parallel workers was about `1.15 GiB`, which was slightly lower than the previous in-memory sequential run on this benchmark.
- The large JSON file does not appear to be the main runtime cost by itself, but it is still substantial disk output and part of the slower baseline path.
- The tiny text report appears negligible. It did not show up as a distinct measurable phase in the logs, which suggests its cost is below the current progress-log granularity on this benchmark.
- The biggest observed win so far came from combining the optimized in-memory design with parallel image workers.

## Use As Rewrite Baseline

This benchmark should be reused when comparing the future C# implementation. At minimum, the rewrite should be evaluated against this same 4-image case for:

- total wall time
- peak memory
- time to results-ready state
- correctness of top contributors and totals
