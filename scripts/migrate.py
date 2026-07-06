"""
Patient Data Migration Script
Imports existing patients from Excel or CSV into the Supabase patients table.

USAGE:
  python scripts/migrate.py --file patients.xlsx
  python scripts/migrate.py --file patients.csv

EXPECTED COLUMNS (column names are flexible — see COLUMN_MAP below):
  full_name, nickname, dob, gender, phone, vip_tier, allergies, medical_notes

Run once from project root with .env loaded:
  source .env && python scripts/migrate.py --file your_file.xlsx
"""

import argparse
import sys
import os
from pathlib import Path
import pandas as pd
from supabase import create_client
from dotenv import load_dotenv

load_dotenv()

# ── Flexible column name mapping ──────────────────────────────────────────────
# Maps YOUR Excel column names → database field names
# Edit this if your spreadsheet uses different headers
COLUMN_MAP = {
    # Excel column name       : DB field name
    "Nama Lengkap":             "full_name",
    "Full Name":                "full_name",
    "Name":                     "full_name",
    "Nama":                     "full_name",
    "Nickname":                 "nickname",
    "Panggilan":                "nickname",
    "DOB":                      "dob",
    "Tanggal Lahir":            "dob",
    "Date of Birth":            "dob",
    "Gender":                   "gender",
    "Jenis Kelamin":            "gender",
    "Phone":                    "phone",
    "No HP":                    "phone",
    "VIP":                      "vip_tier",
    "VIP Tier":                 "vip_tier",
    "Tier":                     "vip_tier",
    "Allergies":                "allergies",
    "Alergi":                   "allergies",
    "Medical Notes":            "medical_notes",
    "Catatan Medis":            "medical_notes",
    "Notes":                    "medical_notes",
    "Referral":                 "referral_source",
}

# ── Gender normalization ───────────────────────────────────────────────────────
GENDER_MAP = {
    "f": "F", "female": "F", "perempuan": "F", "p": "F", "w": "F", "wanita": "F",
    "m": "M", "male": "M", "laki": "M", "l": "M", "pria": "M",
}

# ── VIP tier normalization ────────────────────────────────────────────────────
VIP_MAP = {
    "platinum": "Platinum", "plat": "Platinum",
    "gold": "Gold", "emas": "Gold",
    "silver": "Silver", "perak": "Silver",
    "standard": "Standard", "regular": "Standard", "basic": "Standard",
}


def normalize_row(row: dict) -> dict | None:
    """Clean and validate a single patient row."""
    # Require at least a full_name
    name = row.get("full_name", "").strip()
    if not name:
        return None

    # Gender
    gender_raw = str(row.get("gender", "")).strip().lower()
    gender = GENDER_MAP.get(gender_raw[:1], None) or GENDER_MAP.get(gender_raw, None)

    # VIP tier
    vip_raw = str(row.get("vip_tier", "")).strip().lower()
    vip_tier = VIP_MAP.get(vip_raw, "Standard")

    # DOB — try to parse various date formats
    dob = None
    dob_raw = row.get("dob")
    if dob_raw and str(dob_raw).strip() not in ("", "nan", "NaT"):
        try:
            dob = pd.to_datetime(dob_raw).strftime("%Y-%m-%d")
        except Exception:
            print(f"  ⚠️  Could not parse DOB '{dob_raw}' for {name} — skipping DOB")

    return {
        "full_name":      name,
        "nickname":       (row.get("nickname") or "").strip() or None,
        "dob":            dob,
        "gender":         gender,
        "phone":          (row.get("phone") or "").strip() or None,
        "vip_tier":       vip_tier,
        "allergies":      (row.get("allergies") or "").strip() or None,
        "medical_notes":  (row.get("medical_notes") or "").strip() or None,
        "referral_source":(row.get("referral_source") or "").strip() or None,
        "consent_signed": False,
        "is_active":      True,
    }


def load_file(file_path: str) -> pd.DataFrame:
    p = Path(file_path)
    if not p.exists():
        print(f"❌ File not found: {file_path}")
        sys.exit(1)

    if p.suffix.lower() in (".xlsx", ".xlsm", ".xls"):
        df = pd.read_excel(file_path, dtype=str)
    elif p.suffix.lower() == ".csv":
        df = pd.read_csv(file_path, dtype=str)
    else:
        print(f"❌ Unsupported file type: {p.suffix}. Use .xlsx or .csv")
        sys.exit(1)

    # Apply column mapping
    df = df.rename(columns=COLUMN_MAP)
    return df


def main():
    parser = argparse.ArgumentParser(description="Import patients to DG Clinic Supabase")
    parser.add_argument("--file", required=True, help="Path to Excel or CSV file")
    parser.add_argument("--dry-run", action="store_true", help="Preview without inserting")
    parser.add_argument("--sheet", default=0, help="Sheet name or index (Excel only)")
    args = parser.parse_args()

    SUPABASE_URL = os.getenv("SUPABASE_URL")
    SUPABASE_KEY = os.getenv("SUPABASE_KEY")
    if not SUPABASE_URL or not SUPABASE_KEY:
        print("❌ SUPABASE_URL and SUPABASE_KEY must be set in .env")
        sys.exit(1)

    print(f"\n📂 Loading: {args.file}")
    df = load_file(args.file)
    print(f"   Found {len(df)} rows, columns: {list(df.columns)}")

    # Normalize rows
    records = []
    skipped = 0
    for _, row in df.iterrows():
        normalized = normalize_row(row.to_dict())
        if normalized:
            records.append(normalized)
        else:
            skipped += 1

    print(f"   ✅ {len(records)} valid patients | ⚠️  {skipped} skipped (no name)\n")

    if args.dry_run:
        print("── DRY RUN — nothing written ──")
        for r in records[:5]:
            print(f"  • {r['full_name']} | {r['gender']} | VIP:{r['vip_tier']} | DOB:{r['dob']}")
        if len(records) > 5:
            print(f"  ... and {len(records) - 5} more")
        return

    # Insert to Supabase
    db = create_client(SUPABASE_URL, SUPABASE_KEY)
    batch_size = 20
    inserted = 0
    errors = 0

    for i in range(0, len(records), batch_size):
        batch = records[i : i + batch_size]
        try:
            result = db.table("patients").insert(batch).execute()
            inserted += len(result.data)
            print(f"  ✅ Inserted batch {i//batch_size + 1} ({len(batch)} records)")
        except Exception as e:
            print(f"  ❌ Batch {i//batch_size + 1} failed: {e}")
            errors += len(batch)

    print(f"\n── Migration complete ──")
    print(f"  Inserted: {inserted}")
    print(f"  Errors:   {errors}")
    print(f"\n🔗 Check in Supabase: Table Editor → patients\n")


if __name__ == "__main__":
    main()
