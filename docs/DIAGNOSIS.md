# Research Holdout Diagnosis

## Purpose

`research-diagnose` explains a selected or rejected holdout result without
creating another optimization score. It compares the research candidate with a
baseline on exactly the same dates and decomposes the loss by:

- trading day;
- stock;
- industry;
- candidate score bucket;
- stop, stop gap, target, time stop, and end-of-backtest exit;
- benchmark direction, rolling volatility, and limit-up/down state.

The result is a transaction-level and daily attribution artifact.

## Run

```powershell
cd C:\Users\ZhuanZ\Documents\ChatGPT\kinace\kinace-v3.2

.\run.ps1 research-diagnose `
  --research-report .\reports\research_loop_v32_extended.json `
  --baseline-candidate .\reports\baseline_v32_candidate.json `
  --baseline-config .\reports\baseline_v32_backtest_config.json `
  --output .\reports\holdout_diagnosis_v32.json `
  --markdown .\reports\holdout_diagnosis_v32.md
```

## Outputs

```text
reports\holdout_diagnosis_v32.json
reports\holdout_diagnosis_v32.md
outputs\diagnosis\<diagnosis-id>\baseline-holdout.json
```

The compact lineage record is stored in `research_diagnoses`.

## Interpretation Rule

Attribution explains where the return came from. It does not rank candidates,
change parameters, register challengers, or promote a model. The next research
action must be derived from the observed causes rather than from another score.
