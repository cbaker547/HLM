"""
Hospital Markup Index
"""

import os
import numpy as np
import pandas as pd
from scipy import stats
import statsmodels.formula.api as smf

# ── 1. Load & prepare ─────────────────────────────────────────────────────────

# Prefer Parquet (5-20x faster load, 5-10x smaller on disk). Fall back to CSV
# for legacy compatibility.
if os.path.exists('master_final.parquet'):
    df = pd.read_parquet('master_final.parquet')
else:
    df = pd.read_csv('master_final.csv')

model_df = df[[
    'price_adjusted', 'canonical_name', 'wage_index',
    'npi_number', 'state_x', 'hospital_name'
]].dropna().copy()

model_df['npi_number'] = model_df['npi_number'].astype(str)

# Log-transform price before modeling to reduce skew and limit the effect of extreme values.
# This makes hospital comparisons more stable and meaningful, and log1p safely handles $0 prices.

model_df['log_price'] = np.log1p(model_df['price_adjusted'])

# Count rows per hospital first.
# Hospitals with fewer than 10 rows are flagged because their scores are less reliable.
LOW_N_THRESHOLD = 10  # hospitals below this are flagged

obs_counts = (
    model_df.groupby('npi_number')
    .size()
    .reset_index(name='n_observations')
)

# ── 2. Fit the HLM ────────────────────────────────────────────────────────────

formula = "log_price ~ C(canonical_name) + wage_index"
md = smf.mixedlm(formula, model_df, groups=model_df["npi_number"])
mdf = md.fit(reml=True)

# Convergence check.
# statsmodels will silently return results even when the optimizer didn't
# converge. Raise error immediately so the problem is obvious.
if not mdf.converged:
    raise RuntimeError(
        "MixedLM did not converge. Inspect the data for collinearity, "
        "sparse groups, or try reml=False / different optimizer."
    )

print("✓ Model converged successfully\n")

# Calculate ICC to check whether hospital-level clustering is meaningful.
# If ICC is very low, the hospital random effect may not be useful.

tau2   = float(mdf.cov_re.values[0][0])   # between-hospital variance
sigma2 = float(mdf.scale)                  # residual variance
icc    = tau2 / (tau2 + sigma2)

print(f"── Model Diagnostics ──────────────────────────────────────")
print(f"  Between-hospital variance  (τ²):  {tau2:.4f}")
print(f"  Residual variance          (σ²):  {sigma2:.4f}")
print(f"  ICC:                              {icc:.4f}  ({icc*100:.1f}% of price")
print(f"       variation is explained by hospital-level differences)")

if icc < 0.05:
    print("  ⚠ WARNING: ICC < 0.05 — multilevel structure may not be warranted.")
    print("    Consider a simple OLS with hospital fixed effects instead.")
else:
    print("  ✓ ICC is meaningful — HLM is justified.\n")

# ── 3. Extract markup index + Posterior SE per hospital ────────────────
# The random effect is the hospital’s estimated log-price difference after shrinkage.
# Its uncertainty depends on how many observations the hospital has: fewer rows means a wider interval.

re_dict = {str(k): v['Group'] for k, v in mdf.random_effects.items()}
markup_df = pd.DataFrame(
    list(re_dict.items()),
    columns=['npi_number', 'markup_index_raw']
)

# Merge in observation counts for SE calculation
markup_df = markup_df.merge(obs_counts, on='npi_number', how='left')

# Compute posterior SE (analytical approximation)
markup_df['posterior_se'] = np.sqrt(
    (tau2 * sigma2) / (markup_df['n_observations'] * tau2 + sigma2)
)

# 95% interval on the log scale
markup_df['ci_lower_95'] = markup_df['markup_index_raw'] - 1.96 * markup_df['posterior_se']
markup_df['ci_upper_95'] = markup_df['markup_index_raw'] + 1.96 * markup_df['posterior_se']

# Low-confidence flag
markup_df['low_confidence'] = markup_df['n_observations'] < LOW_N_THRESHOLD

# ── 4. Merge hospital context ─────────────────────────────────────────────────

hospital_info = (
    model_df[['npi_number', 'hospital_name', 'state_x']]
    .drop_duplicates(subset=['npi_number'])
)

markup_final = markup_df.merge(hospital_info, on='npi_number', how='left')
markup_final = markup_final.rename(columns={'state_x': 'state'})

# ── 5. Standardize & rank ─────────────────────────────────────────────────────

markup_final['markup_index_zscore'] = stats.zscore(
    markup_final['markup_index_raw'], nan_policy='omit'
)
markup_final['markup_index_percentile'] = (
    markup_final['markup_index_raw'].rank(pct=True) * 100
)

# ── 6. Final column order & sort ──────────────────────────────────────────────

final_cols = [
    'npi_number', 'hospital_name', 'state',
    'n_observations', 'low_confidence',
    'markup_index_raw', 'posterior_se', 'ci_lower_95', 'ci_upper_95',
    'markup_index_zscore', 'markup_index_percentile'
]
markup_final = markup_final[final_cols].sort_values(
    by='markup_index_raw', ascending=False
)

# ── 7. Save ───────────────────────────────────────────────────────────────────

output_path = 'hospital_markup_scores.csv'
markup_final.to_csv(output_path, index=False)
print(f"\n✓ Saved to {output_path}  ({len(markup_final)} hospitals)\n")

# ── 8. Sanity checks ──────────────────────────────────────────────────────────

pd.set_option('display.max_columns', None)
pd.set_option('display.width', 140)

print("── Top 5 Highest Markup Hospitals ─────────────────────────────────────")
print(markup_final.head(5).to_string(index=False))

print("\n── Bottom 5 Lowest Markup Hospitals ────────────────────────────────────")
print(markup_final.tail(5).to_string(index=False))

print("\n── Low-Confidence Hospitals (n < {}) ────────────────────────────────".format(LOW_N_THRESHOLD))
low_conf = markup_final[markup_final['low_confidence']]
print(f"  {len(low_conf)} hospitals flagged ({len(low_conf)/len(markup_final)*100:.1f}% of total)")
print(low_conf[['npi_number','hospital_name','state','n_observations','markup_index_raw','posterior_se']].head(10).to_string(index=False))

print("\n── Posterior SE distribution ───────────────────────────────────────────")
print(markup_final['posterior_se'].describe().round(4).to_string())
print(f"\n  Ratio (max SE / min SE): {markup_final['posterior_se'].max() / markup_final['posterior_se'].min():.1f}x")
print("  This shows how much more uncertain sparse hospitals are vs. well-observed ones.")
