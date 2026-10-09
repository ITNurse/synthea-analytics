#!/usr/bin/env python3
"""
Resource Inventory for Synthea FHIR output

Lists every unique resource type found in the Synthea bundles on disk,
with how many of each there are and how many bundles (patients) contain them.
A companion to field_profile.py: run this first to see WHAT is in the data,
then use field_profile.py to see which fields each resource fills in.

Usage (from the project folder, with the venv active):
    python resource_inventory.py
    python resource_inventory.py --folder "C:\\some\\other\\output\\fhir"
"""

import argparse
import csv
import glob
import json
import os
from collections import Counter

from config import SYNTHEA_FHIR_DIR, OUTPUT_DIR


def parse_args():
    parser = argparse.ArgumentParser(description="List the unique FHIR resource types in Synthea bundles.")
    parser.add_argument("--folder", default=SYNTHEA_FHIR_DIR,
                        help="Synthea output/fhir folder (defaults to SYNTHEA_FHIR_DIR in config.py)")
    return parser.parse_args()


def bundle_kind(filename):
    """Synthea writes shared hospital and practitioner bundles alongside one bundle per patient."""
    name = filename.lower()
    if name.startswith("hospitalinformation"):
        return "hospital"
    if name.startswith("practitionerinformation"):
        return "practitioner"
    return "patient"


def main():
    args = parse_args()

    bundle_files = sorted(glob.glob(os.path.join(args.folder, "*.json")))
    if not bundle_files:
        print(f"❌ No .json files found in {args.folder}")
        return

    print(f"Reading {len(bundle_files)} bundle files from {args.folder} ...")

    resource_counts = Counter()   # total resources of each type
    bundle_counts = Counter()     # how many bundles contain each type at least once
    patient_bundle_counts = Counter()  # same, but only counting patient bundles
    patient_bundles = 0
    unreadable = []

    for path in bundle_files:
        filename = os.path.basename(path)
        try:
            with open(path, encoding="utf-8") as f:
                bundle = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            unreadable.append((filename, str(e)))
            continue

        kind = bundle_kind(filename)
        if kind == "patient":
            patient_bundles += 1

        types_in_this_bundle = set()
        for entry in bundle.get("entry", []):
            resource_type = entry.get("resource", {}).get("resourceType")
            if resource_type:
                resource_counts[resource_type] += 1
                types_in_this_bundle.add(resource_type)

        for resource_type in types_in_this_bundle:
            bundle_counts[resource_type] += 1
            if kind == "patient":
                patient_bundle_counts[resource_type] += 1

    if not resource_counts:
        print("❌ No resources found. Check that these are Synthea FHIR bundles.")
        return

    # Build one row per resource type, largest first
    rows = []
    for resource_type, total in resource_counts.most_common():
        in_patients = patient_bundle_counts[resource_type]
        rows.append({
            "ResourceType": resource_type,
            "TotalResources": total,
            "BundlesContaining": bundle_counts[resource_type],
            "PatientsWithAny": in_patients,
            "PercentOfPatients": round(100 * in_patients / patient_bundles, 1) if patient_bundles else 0,
        })

    # Save to CSV
    output_path = os.path.join(OUTPUT_DIR, "resource_inventory.csv")
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Print a short summary
    print(f"\n{len(rows)} unique resource types across {patient_bundles} patient bundles:\n")
    print(f"   {'Resource type':<32}{'Total':>9}{'Patients':>10}{'% pts':>8}")
    print(f"   {'-' * 32}{'-' * 9}{'-' * 10}{'-' * 8}")
    for row in rows:
        print(f"   {row['ResourceType']:<32}{row['TotalResources']:>9}"
              f"{row['PatientsWithAny']:>10}{row['PercentOfPatients']:>7.1f}%")

    if unreadable:
        print(f"\n⚠️  {len(unreadable)} file(s) could not be read:")
        for filename, error in unreadable:
            print(f"   - {filename}: {error}")

    print(f"\n✅ Inventory saved to {output_path}")


if __name__ == "__main__":
    main()
