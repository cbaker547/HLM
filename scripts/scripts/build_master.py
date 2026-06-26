"""
build_master.py — streams DoltHub prices + enriches with CMS/wage/NPI/Medicare
to produce master_final.csv (+ .parquet).

Usage:
  python3 build_master.py                 # full CPT pull
  python3 build_master.py --dry-run       # first 25 hospitals only
  python3 build_master.py --resume        # continue after interruption
  python3 build_master.py --skip-fetch    # re-enrich existing prices_raw.csv
  python3 build_master.py --codes drg     # append DRG codes to prices_raw.csv
  python3 build_master.py --codes new-cpt # append 4 preventive CPTs
"""

import argparse, csv, json, os, sys, time, urllib.error, urllib.parse, urllib.request
from datetime import datetime
import numpy as np
import pandas as pd

# ──────────────────────────────────────────────────────────────── CONFIG

DOLT_API = "https://www.dolthub.com/api/v1alpha1/dolthub/hospital-price-transparency-v3/main"

CPT_MAP = {
    "27130": ("Hip Replacement", "Surgery"),
    "27447": ("Knee Replacement", "Surgery"),
    "29827": ("Shoulder Arthroscopy", "Surgery"),
    "36415": ("Blood Draw", "Lab"),
    "43239": ("Upper GI Endoscopy", "Gastroenterology"),
    "45378": ("Colonoscopy", "Gastroenterology"),
    "47562": ("Gallbladder Removal", "Surgery"),
    "70553": ("Brain MRI", "Imaging"),
    "71046": ("Chest X-Ray", "Imaging"),
    "74177": ("CT Scan Abdomen", "Imaging"),
    "80053": ("Comprehensive Metabolic Panel", "Lab"),
    "93000": ("EKG", "Lab"),
    "99213": ("Office Visit (Established)", "Office Visit"),
    "99283": ("ER Visit (Moderate)", "Office Visit"),
    "77067": ("Mammography Screening", "Imaging"),
    "80061": ("Lipid Panel", "Lab"),
    "83036": ("Hemoglobin A1C", "Lab"),
    "84153": ("PSA Test", "Lab"),
}
DRG_MAP = {
    "469": ("Joint Replacement w/ CC (DRG 469)", "Inpatient Bundle"),
    "470": ("Joint Replacement (DRG 470)", "Inpatient Bundle"),
    "417": ("Lap Chole w/ MCC (DRG 417)", "Inpatient Bundle"),
    "418": ("Lap Chole w/ CC (DRG 418)", "Inpatient Bundle"),
    "419": ("Lap Chole no CC (DRG 419)", "Inpatient Bundle"),
}
NEW_CPT_CODES = ["77067", "80061", "83036", "84153"]
CPT_CODES = sorted(CPT_MAP.keys())
DRG_CODES = sorted(DRG_MAP.keys())

# CPTs billed as inpatient bundles — no per-CPT Medicare reference.
INPATIENT_CPTS = {"27130", "27447", "47562"}
EXCLUDED_PAYERS = {"GROSS CHARGE", "MAX", "MIN"}

PRICES_RAW     = "prices_raw.csv"
OUTPUT_CSV     = "master_final.csv"
OUTPUT_PARQUET = "master_final.parquet"
STATE_FILE     = ".build_state.json"

CMS_HOSPITALS = "cms_hospitals.csv"
WAGE_INDEX    = "wage_index.csv"
NPI_CROSSWALK = "npi_ccn_crosswalk_raw.csv"
MDCR_OPP      = "medicare_opps_filtered.csv"
MDCR_INP      = "medicare_inpatient.csv"

RAW_COLS = ["ccn", "code", "payer", "price", "inpatient_outpatient",
            "description", "dolt_hospital_name", "dolt_city",
            "dolt_state", "dolt_zip"]

# ──────────────────────────────────────────────────────────────── DOLT API

def dolt_query(sql, max_retries=5):
    """Run a SQL query against the DoltHub HTTP API with exponential backoff."""
    url = f"{DOLT_API}?q={urllib.parse.quote(sql)}"
    for attempt in range(max_retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "build_master/1.0"})
            with urllib.request.urlopen(req, timeout=90) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if data.get("query_execution_status") != "Success":
                return []  # soft-fail; caller logs
            return data.get("rows", [])
        except Exception as e:
            wait = min(30, 2 ** attempt)
            print(f"    WARN: {type(e).__name__}: {e} — retry in {wait}s")
            time.sleep(wait)
    sys.exit(f"FATAL: DoltHub query failed: {sql[:100]}")

def fetch_hospitals():
    """Paginate the hospitals table by CCN keyset."""
    print("  fetching hospitals table...")
    out, last = [], ""
    while True:
        rows = dolt_query(
            f"SELECT cms_certification_num, name, city, state, zip5 FROM hospitals "
            f"WHERE cms_certification_num > '{last}' ORDER BY cms_certification_num LIMIT 1000"
        )
        if not rows:
            break
        out.extend(rows)
        last = rows[-1]["cms_certification_num"]
        print(f"    +{len(rows):>4}  total {len(out):>5,}  (last ccn {last})")
        if len(rows) < 1000:
            break
    return out

def fetch_prices(ccn, codes):
    codes_sql = ",".join(f"'{c}'" for c in codes)
    return dolt_query(
        f"SELECT code, payer, price, inpatient_outpatient, description FROM prices "
        f"WHERE cms_certification_num = '{ccn}' AND code IN ({codes_sql}) AND price > 0"
    )

def fetch_all_prices(codes, label, dry_run=False, resume=False, append=False):
    """Per-hospital queries; filter excluded payers client-side; stream to CSV."""
    state_key = f"completed_ccns_{label}"
    state = {state_key: [], "rows_fetched": 0}
    if resume and os.path.exists(STATE_FILE):
        loaded = json.load(open(STATE_FILE))
        state[state_key] = loaded.get(state_key, loaded.get("completed_ccns", []))
        state["rows_fetched"] = loaded.get("rows_fetched", 0)

    hospitals = {h["cms_certification_num"]: h for h in fetch_hospitals()}
    all_ccns = sorted(hospitals)
    if dry_run:
        all_ccns = all_ccns[:25]
    print(f"  will query {len(all_ccns):,} hospitals for {label} codes")

    completed = set(state[state_key])
    file_exists = os.path.exists(PRICES_RAW) and os.path.getsize(PRICES_RAW) > 0
    mode = "a" if ((resume or append) and file_exists) else "w"

    t0 = time.time()
    with open(PRICES_RAW, mode, newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=RAW_COLS)
        if mode == "w":
            w.writeheader()

        for i, ccn in enumerate(all_ccns, 1):
            if ccn in completed:
                continue
            rows = [r for r in fetch_prices(ccn, codes)
                    if r.get("payer") not in EXCLUDED_PAYERS]
            h = hospitals[ccn]
            for r in rows:
                w.writerow({
                    "ccn": ccn,
                    "code": r.get("code", ""),
                    "payer": r.get("payer", ""),
                    "price": r.get("price", ""),
                    "inpatient_outpatient": r.get("inpatient_outpatient", ""),
                    "description": r.get("description", ""),
                    "dolt_hospital_name": h.get("name", ""),
                    "dolt_city": h.get("city", ""),
                    "dolt_state": h.get("state", ""),
                    "dolt_zip": h.get("zip5", ""),
                })
            state["rows_fetched"] += len(rows)
            state[state_key].append(ccn)

            if i % 25 == 0 or i == len(all_ccns):
                elapsed = time.time() - t0
                rate = i / elapsed if elapsed else 0
                eta = (len(all_ccns) - i) / rate / 60 if rate else 0
                print(f"    [{i:>4}/{len(all_ccns)}] rows={state['rows_fetched']:>8,}  "
                      f"rate={rate:.1f}/s  eta={eta:.1f}m")
                json.dump(state, open(STATE_FILE, "w"), indent=2)
                f.flush()

    json.dump(state, open(STATE_FILE, "w"), indent=2)
    return state["rows_fetched"]

# ──────────────────────────────────────────────────────────────── ENRICH

def _ccn(s):
    return s.fillna("").astype(str).str.replace(r"\.0$", "", regex=True).str.strip().str.zfill(6)

def load_medicare_reference():
    """Build {(code, state): median, ...} + {code: national_median} from
    medicare_opps_filtered.csv (outpatient CPTs) and medicare_inpatient.csv (DRGs)."""
    by_state, national = {}, {}

    if os.path.exists(MDCR_OPP):
        print(f"  loading {MDCR_OPP}...")
        opp = pd.read_csv(MDCR_OPP, encoding="latin-1", low_memory=False,
                          usecols=["HCPCS_Cd", "Rndrng_Prvdr_State_Abrvtn",
                                   "Avg_Mdcr_Alowd_Amt", "Place_Of_Srvc"])
        opp.columns = ["cpt", "state", "amt", "place"]
        opp["cpt"] = opp["cpt"].astype(str).str.strip()
        opp["amt"] = pd.to_numeric(opp["amt"], errors="coerce")
        opp = opp.dropna(subset=["amt"])
        # 'O' (non-facility/office) is bundled tech + professional — matches
        # commercial chargemaster bundle. 'F' omits the technical portion and
        # makes markup ratios look ~2-4x too high.
        n_before = len(opp)
        opp = opp[opp["place"] == "O"]
        print(f"    Place_Of_Srvc='O': {len(opp):,} rows (dropped {n_before-len(opp):,})")

        for (cpt, st), v in opp.groupby(["cpt", "state"])["amt"].median().items():
            by_state[(cpt, st)] = float(v)
        for cpt, v in opp.groupby("cpt")["amt"].median().items():
            national[cpt] = float(v)

    if os.path.exists(MDCR_INP):
        inp = pd.read_csv(MDCR_INP, encoding="latin-1", low_memory=False,
                          usecols=["DRG_Cd", "Rndrng_Prvdr_State_Abrvtn", "Avg_Mdcr_Pymt_Amt"])
        inp.columns = ["drg", "state", "amt"]
        inp["drg"] = inp["drg"].astype(str).str.strip().str.zfill(3)
        inp["amt"] = pd.to_numeric(inp["amt"], errors="coerce")
        inp = inp.dropna(subset=["amt"])
        inp = inp[inp["drg"].isin(DRG_CODES)]
        for (drg, st), v in inp.groupby(["drg", "state"])["amt"].median().items():
            by_state[(drg, st)] = float(v)
        for drg, v in inp.groupby("drg")["amt"].median().items():
            national[drg] = float(v)

    return by_state, national

def enrich_and_emit():
    print("\n=== STEP 2 — enrich ===")

    prices = pd.read_csv(PRICES_RAW, dtype=str, low_memory=False)
    prices["ccn"] = _ccn(prices["ccn"])
    prices["price"] = pd.to_numeric(prices["price"], errors="coerce")
    prices = prices.dropna(subset=["price"])

    # Dedupe: DoltHub's PK includes inpatient_outpatient, so the same
    # (ccn, code, payer, price) can appear twice (IN + OUT) and inflate HLM weight.
    n_before = len(prices)
    prices = prices.drop_duplicates(
        subset=["ccn", "code", "payer", "price", "inpatient_outpatient"]
    ).reset_index(drop=True)
    print(f"  prices: {len(prices):,} rows (dropped {n_before-len(prices):,} dupes)")

    # CMS hospitals
    cms = pd.read_csv(CMS_HOSPITALS, dtype=str, low_memory=False)
    cms["ccn"] = _ccn(cms["ccn"])
    cms = cms.rename(columns={"hospital_name": "hospital_name_cms", "city": "city_cms",
                              "state": "state_cms", "zip_code": "zip_code_cms"})
    cms = cms[["ccn", "hospital_name_cms", "city_cms", "state_cms", "zip_code_cms",
               "hospital_type", "ownership_type"]].drop_duplicates("ccn")

    # Wage index (column name has a newline in the CMS file)
    wage = pd.read_csv(WAGE_INDEX, dtype=str, low_memory=False)
    wage.columns = [c.replace("\n", " ").strip() for c in wage.columns]
    wcol = next((c for c in wage.columns if "wage" in c.lower() and "index" in c.lower()), None)
    if wcol:
        wage = wage[["PROV", wcol]].rename(columns={"PROV": "ccn", wcol: "wage_index"})
        wage["ccn"] = _ccn(wage["ccn"])
        wage["wage_index"] = pd.to_numeric(wage["wage_index"], errors="coerce")
        wage = wage.drop_duplicates("ccn")
    else:
        wage = pd.DataFrame(columns=["ccn", "wage_index"])

    # NPI crosswalk
    xwalk = pd.read_csv(NPI_CROSSWALK, dtype=str, low_memory=False)
    xwalk["ccn"] = _ccn(xwalk["ccn"])
    xwalk["npi"] = xwalk["npi"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True).str.strip()
    xwalk = xwalk[(xwalk["ccn"].str.len() == 6) & (xwalk["npi"] != "")][["ccn", "npi"]].drop_duplicates("ccn")

    mcr_by_state, mcr_national = load_medicare_reference()

    df = prices.merge(cms, on="ccn", how="left") \
               .merge(wage, on="ccn", how="left") \
               .merge(xwalk, on="ccn", how="left")

    # Wage-index state-median fallback. ~37% of rows lack a reported wage index
    # (MD waiver, CAHs, VA). Impute from state median so they survive HLM;
    # wage_index_source tracks provenance.
    df["wage_index"] = pd.to_numeric(df["wage_index"], errors="coerce")
    state_col = df["state_cms"].where(df["state_cms"].notna(), df["dolt_state"])
    reported = df["wage_index"].notna()
    state_medians = df[reported].assign(_st=state_col[reported]).groupby("_st")["wage_index"].median()
    df["wage_index_source"] = np.where(reported, "reported", None)
    fill = ~reported
    df.loc[fill, "wage_index"] = state_col[fill].map(state_medians)
    df.loc[fill & df["wage_index"].notna(), "wage_index_source"] = "state_median"
    print(f"  wage_index: reported={reported.sum():,}, "
          f"state_median={(df['wage_index_source']=='state_median').sum():,}, "
          f"null={df['wage_index'].isna().sum():,}")

    # Procedure metadata
    full_map = {**CPT_MAP, **DRG_MAP}
    df["canonical_name"] = df["code"].map(lambda c: full_map.get(c, ("", ""))[0])
    df["category"]       = df["code"].map(lambda c: full_map.get(c, ("", ""))[1])
    df["code_type"]      = np.where(df["code"].isin(CPT_MAP), "CPT",
                            np.where(df["code"].isin(DRG_MAP), "DRG", "UNKNOWN"))

    # price_adjusted = price / wage_index
    wi = pd.to_numeric(df["wage_index"], errors="coerce")
    df["price_adjusted"] = np.where(wi > 0, df["price"] / wi, np.nan)

    # Medicare reference: (code, state) median with national fallback
    state_key = df["state_cms"].fillna(df["dolt_state"])
    df["medicare_ref_price"] = [
        mcr_by_state.get((c, s), mcr_national.get(c, np.nan))
        for c, s in zip(df["code"], state_key)
    ]
    # Inpatient CPTs have no meaningful per-CPT Medicare rate (paid as DRG bundle);
    # the DRG rows carry the correct bundle-level reference instead.
    df.loc[df["code"].isin(INPATIENT_CPTS) & (df["code_type"] == "CPT"), "medicare_ref_price"] = np.nan

    # commercial_markup_ratio = price / Medicare allowed ("how many times Medicare")
    mref = pd.to_numeric(df["medicare_ref_price"], errors="coerce")
    df["commercial_markup_ratio"] = np.where(mref > 0, df["price"] / mref, np.nan)

    # NPI fallback to CCN so HLM group-by always has a value
    df["npi_resolved"] = df["npi"].where(df["npi"].notna() & (df["npi"] != ""), df["ccn"])

    out = pd.DataFrame({
        "code":                    df["code"],
        "npi_number":              df["npi_resolved"],
        "payer":                   df["payer"],
        "price":                   df["price"],
        "name":                    df["dolt_hospital_name"],
        "city_x":                  df["dolt_city"],
        "state_x":                 df["dolt_state"],
        "zip_code_x":              df["dolt_zip"],
        "npi":                     df["npi_resolved"],
        "ccn":                     df["ccn"],
        "hospital_name":           df["hospital_name_cms"],
        "city_y":                  df["city_cms"],
        "state_y":                 df["state_cms"],
        "zip_code_y":              df["zip_code_cms"],
        "hospital_type":           df["hospital_type"],
        "ownership_type":          df["ownership_type"],
        "canonical_name":          df["canonical_name"],
        "category":                df["category"],
        "code_type":               df["code_type"],
        "wage_index":              df["wage_index"],
        "wage_index_source":       df["wage_index_source"],
        "price_adjusted":          df["price_adjusted"],
        "medicare_ref_price":      df["medicare_ref_price"],
        "commercial_markup_ratio": df["commercial_markup_ratio"],
    })
    for c in ("price", "price_adjusted", "wage_index"):
        out[c] = pd.to_numeric(out[c], errors="coerce")

    out.to_parquet(OUTPUT_PARQUET, index=False, compression="snappy")
    out.to_csv(OUTPUT_CSV, index=False)
    print(f"\n  wrote {OUTPUT_PARQUET} + {OUTPUT_CSV}: {len(out):,} rows, "
          f"{out['ccn'].nunique()} hospitals, {out['state_y'].nunique()} states, "
          f"{out['code'].nunique()} procedures")
    return out

# ──────────────────────────────────────────────────────────────── SANITY

def sanity_report(df):
    print("\n=== STEP 3 — sanity ===")
    print(f"  rows={len(df):,}  ccn={df['ccn'].nunique():,}  "
          f"code={df['code'].nunique()}  state={df['state_y'].nunique()}  "
          f"payer={df['payer'].nunique():,}")

    print("\n  Price p01/p50/p99 per code:")
    for code, (name, _) in {**CPT_MAP, **DRG_MAP}.items():
        sub = pd.to_numeric(df[df["code"] == code]["price"], errors="coerce").dropna()
        if len(sub) == 0:
            continue
        q = sub.quantile([0.01, 0.5, 0.99])
        mk = pd.to_numeric(df[df["code"] == code]["commercial_markup_ratio"],
                           errors="coerce").dropna()
        mk_str = f"  mk_med={mk.median():.1f}x" if len(mk) else "  (no mcr ref)"
        print(f"    {code:<6} {name:<35} n={len(sub):>7,}  "
              f"${q[0.01]:>7,.0f} / ${q[0.5]:>7,.0f} / ${q[0.99]:>8,.0f}{mk_str}")

# ──────────────────────────────────────────────────────────────── MAIN

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--resume", action="store_true")
    ap.add_argument("--skip-fetch", action="store_true")
    ap.add_argument("--codes", choices=["cpt", "drg", "new-cpt"], default="cpt",
                    help="which code set (drg/new-cpt imply --append)")
    args = ap.parse_args()

    for p in (CMS_HOSPITALS, WAGE_INDEX, NPI_CROSSWALK):
        if not os.path.exists(p):
            sys.exit(f"FATAL: missing {p}")

    t0 = datetime.now()

    if not args.skip_fetch:
        codes, label, append = {
            "cpt":     (CPT_CODES,     "CPT",    False),
            "drg":     (DRG_CODES,     "DRG",    True),
            "new-cpt": (NEW_CPT_CODES, "NEWCPT", True),
        }[args.codes]
        print(f"=== STEP 1 — DoltHub pull ({label}, "
              f"{'DRY' if args.dry_run else 'FULL'}) ===")
        n = fetch_all_prices(codes, label,
                             dry_run=args.dry_run, resume=args.resume, append=append)
        print(f"  fetched: {n:,} rows")

    df = enrich_and_emit()
    sanity_report(df)
    print(f"\n=== DONE — {(datetime.now() - t0).total_seconds()/60:.1f} min ===")

if __name__ == "__main__":
    main()
