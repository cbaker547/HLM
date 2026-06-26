"""
build_api_data.py — Generate API JSON shards from the master dataset
======================================================================

This script only builds the API data files used by the viewer at runtime.

It does not build HTML or any static site files. It only creates the data
shards the frontend reads.

Inputs (from ../dataset/):
  - master_final.parquet: all enriched price records, including Medicare reference data
  - hospital_markup_scores.csv: HLM output with HMI scores for 934 hospitals

Outputs (to ../api/data/):
  - overview.json: state list, procedure list, dropdown groups, and proc_state data
  - hospitals/<STATE>.json: one file per state, such as CA.json or TX.json

Usage:
  cd scripts/
  python3 build_api_data.py

To rebuild from a different location:
  python3 build_api_data.py --dataset ../dataset --output ../api/data

Run this script anytime master_final.parquet or hospital_markup_scores.csv
has been updated.

It does not re-run the DoltHub pull or the HLM model. It only reads the
existing files and builds the API data output.
"""

import argparse
import json
import os
import sys

import pandas as pd

# ─── state abbreviation → full name (for map choropleth keys) ────────────
ABBR_TO_NAME = {
    "AL":"Alabama","AK":"Alaska","AZ":"Arizona","AR":"Arkansas","CA":"California",
    "CO":"Colorado","CT":"Connecticut","DE":"Delaware","FL":"Florida","GA":"Georgia",
    "HI":"Hawaii","ID":"Idaho","IL":"Illinois","IN":"Indiana","IA":"Iowa",
    "KS":"Kansas","KY":"Kentucky","LA":"Louisiana","ME":"Maine","MD":"Maryland",
    "MA":"Massachusetts","MI":"Michigan","MN":"Minnesota","MS":"Mississippi","MO":"Missouri",
    "MT":"Montana","NE":"Nebraska","NV":"Nevada","NH":"New Hampshire","NJ":"New Jersey",
    "NM":"New Mexico","NY":"New York","NC":"North Carolina","ND":"North Dakota","OH":"Ohio",
    "OK":"Oklahoma","OR":"Oregon","PA":"Pennsylvania","RI":"Rhode Island","SC":"South Carolina",
    "SD":"South Dakota","TN":"Tennessee","TX":"Texas","UT":"Utah","VT":"Vermont",
    "VA":"Virginia","WA":"Washington","WV":"West Virginia","WI":"Wisconsin","WY":"Wyoming",
    "DC":"District of Columbia","PR":"Puerto Rico",
}

# Inpatient CPTs — their canonical_name goes into the "inpatient_cpt" dropdown
# group, distinct from outpatient CPTs and DRG bundles.
INPATIENT_CPT_NAMES = {"Hip Replacement", "Knee Replacement", "Gallbladder Removal"}


def _repad_ccn(x):
    """Restore 6-digit CCN padding (pandas strips leading zeros on CSV read)."""
    return x.zfill(6) if len(x) <= 6 else x


def _safe(v, digits=2):
    if v is None or pd.isna(v):
        return None
    return round(float(v), digits)


def build(dataset_dir, output_dir):
    # ── load ─────────────────────────────────────────────────────────────
    parquet_path = os.path.join(dataset_dir, "master_final.parquet")
    markup_path  = os.path.join(dataset_dir, "hospital_markup_scores.csv")
    for p in (parquet_path, markup_path):
        if not os.path.exists(p):
            sys.exit(f"FATAL: missing {p}")

    print(f"loading {parquet_path}...")
    master = pd.read_parquet(parquet_path)
    master["npi_number"] = master["npi_number"].astype(str)
    master["state_y"]    = master["state_y"].astype(str)

    print(f"loading {markup_path}...")
    markup = pd.read_csv(markup_path)
    markup["npi_number"] = markup["npi_number"].astype(str).map(_repad_ccn)

    # Canonical state per row: prefer CMS state_y, fall back to DoltHub state_x
    master["state"] = master["state_y"].where(
        master["state_y"].notna() & (master["state_y"] != "nan"),
        master["state_x"],
    )

    # ── per-(hospital, procedure) aggregation ────────────────────────────
    print("aggregating per-hospital procedure prices...")
    agg = (master.groupby(["npi_number", "canonical_name"])
           .agg(avg_price=("price_adjusted", "mean"),
                count=("price_adjusted", "size"),
                mdcr_ref=("medicare_ref_price", "median"),
                markup=("commercial_markup_ratio", "median"))
           .reset_index())

    master_cash = master["payer"].str.upper().str.contains("CASH", na=False)
    cash_med = (master[master_cash]
                .groupby(["npi_number", "canonical_name"])["price"]
                .median().reset_index().rename(columns={"price": "cash_price"}))
    ins_med  = (master[~master_cash]
                .groupby(["npi_number", "canonical_name"])["price"]
                .median().reset_index().rename(columns={"price": "ins_price"}))
    agg = agg.merge(cash_med, on=["npi_number", "canonical_name"], how="left") \
             .merge(ins_med,  on=["npi_number", "canonical_name"], how="left")

    procs_by_npi = {}
    for _, r in agg.iterrows():
        npi, name = r["npi_number"], r["canonical_name"]
        if pd.isna(name) or pd.isna(r["avg_price"]):
            continue
        procs_by_npi.setdefault(npi, {})[name] = {
            "avg_price": round(float(r["avg_price"]), 2),
            "count":     int(r["count"]),
            "mdcr_ref":  _safe(r["mdcr_ref"]),
            "markup":    _safe(r["markup"]),
            "cash":      _safe(r["cash_price"]),
            "ins":       _safe(r["ins_price"]),
        }

    # ── hospital list from markup scores ─────────────────────────────────
    state_lookup = (master.drop_duplicates("npi_number")
                          .set_index("npi_number")["state"].to_dict())
    name_lookup  = (master.drop_duplicates("npi_number")
                          .set_index("npi_number")["hospital_name"]
                          .fillna(master.drop_duplicates("npi_number")
                                         .set_index("npi_number")["name"]).to_dict())

    hospitals = []
    for _, r in markup.iterrows():
        npi = r["npi_number"]
        state_abbr = state_lookup.get(npi, "")
        if not isinstance(state_abbr, str) or state_abbr == "nan":
            state_abbr = ""
        nm = name_lookup.get(npi, "")
        if not isinstance(nm, str) or nm == "nan":
            nm = ""
        hospitals.append({
            "npi":        str(npi),
            "name":       nm.upper() if nm else "",
            "state":      state_abbr,
            "state_full": ABBR_TO_NAME.get(state_abbr, state_abbr),
            "markup":     round(float(r["markup_index_raw"]), 3),
            "zscore":     round(float(r["markup_index_zscore"]), 2),
            "pctile":     round(float(r["markup_index_percentile"]), 1),
            "n_obs":      int(r["n_observations"]),
            "low_conf":   bool(r["low_confidence"]),
            "procs":      procs_by_npi.get(npi, {}),
        })
    print(f"  {len(hospitals)} hospitals scored")

    # ── per-state aggregates (for the choropleth) ────────────────────────
    states_block = {}
    by_state = {}
    for h in hospitals:
        sf = h["state_full"]
        if not sf:
            continue
        by_state.setdefault(sf, []).append(h)
    for state_full, hs in by_state.items():
        states_block[state_full] = {
            "abbr":           hs[0]["state"],
            "avg_markup":     round(sum(h["markup"] for h in hs) / len(hs), 3),
            "num_hospitals":  len(hs),
            "avg_percentile": round(sum(h["pctile"] for h in hs) / len(hs), 1),
        }
    print(f"  {len(states_block)} states with data")

    # ── procedure taxonomy (for grouped dropdown) ────────────────────────
    procedures = sorted(master["canonical_name"].dropna().unique().tolist())
    proc_groups = {"outpatient": [], "inpatient_cpt": [], "drg": []}
    for p in procedures:
        if "DRG" in p:
            proc_groups["drg"].append(p)
        elif p in INPATIENT_CPT_NAMES:
            proc_groups["inpatient_cpt"].append(p)
        else:
            proc_groups["outpatient"].append(p)
    for k in proc_groups:
        proc_groups[k].sort()
    print(f"  {len(procedures)} procedures: outpatient={len(proc_groups['outpatient'])}, "
          f"inpatient_cpt={len(proc_groups['inpatient_cpt'])}, "
          f"drg={len(proc_groups['drg'])}")

    # ── per-(procedure, state) cells (for procedure-filtered choropleth) ──
    proc_state = {p: {} for p in procedures}
    for h in hospitals:
        if not h["state_full"]:
            continue
        for proc_name in h["procs"].keys():
            sf = h["state_full"]
            b = proc_state.setdefault(proc_name, {}).setdefault(sf,
                                                                 {"markups": [], "count": 0})
            b["markups"].append(h["markup"])
            b["count"] += 1
    for proc_name, state_map in proc_state.items():
        for sf, b in state_map.items():
            state_map[sf] = {
                "avg_markup": round(sum(b["markups"]) / len(b["markups"]), 3),
                "count":      b["count"],
            }

    # ── write overview.json ──────────────────────────────────────────────
    os.makedirs(output_dir, exist_ok=True)
    overview = {
        "states":      states_block,
        "procedures":  procedures,
        "proc_groups": proc_groups,
        "proc_state":  proc_state,
    }
    overview_path = os.path.join(output_dir, "overview.json")
    with open(overview_path, "w", encoding="utf-8") as f:
        json.dump(overview, f, separators=(",", ":"), ensure_ascii=False)
    ov_kb = os.path.getsize(overview_path) / 1024
    print(f"\nwrote {overview_path}  ({ov_kb:.0f} KB)")

    # ── write per-state hospital shards ──────────────────────────────────
    hosp_dir = os.path.join(output_dir, "hospitals")
    os.makedirs(hosp_dir, exist_ok=True)
    total_bytes = 0
    state_shards = {}
    for h in hospitals:
        if h["state"]:
            state_shards.setdefault(h["state"], []).append(h)
    for abbr, hs in sorted(state_shards.items()):
        p = os.path.join(hosp_dir, f"{abbr}.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(hs, f, separators=(",", ":"), ensure_ascii=False)
        total_bytes += os.path.getsize(p)
    print(f"wrote {len(state_shards)} state shards in {hosp_dir}  "
          f"({total_bytes/1024:.0f} KB total)")

    print(f"\nInitial page-load wire cost: ~{ov_kb:.0f} KB")
    print(f"Full dataset if every state is clicked: ~{(ov_kb*1024 + total_bytes)/1024:.0f} KB")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dataset", default="../dataset",
                    help="folder with master_final.parquet + hospital_markup_scores.csv")
    ap.add_argument("--output", default="../api/data",
                    help="output folder for overview.json + hospitals/*.json")
    args = ap.parse_args()
    build(args.dataset, args.output)


if __name__ == "__main__":
    main()
