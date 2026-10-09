#!/usr/bin/env python3
"""
Synthea Bundle Uploader

Posts Synthea's native FHIR R4 transaction bundles, unchanged, to the HAPI
FHIR server. This is the "raw load" step: no PS-CA filtering or mapping
happens here. That comes later as a separate step.

Usage (from the project folder, with the venv active):
    python upload_synthea.py              upload everything in SYNTHEA_FHIR_DIR
    python upload_synthea.py --dry-run    show what would be uploaded, post nothing
    python upload_synthea.py --folder "C:\\some\\other\\output\\fhir"
"""

import argparse
import os
import sys
import time
from collections import Counter

import pandas as pd

from config import (
    SYNTHEA_FHIR_DIR, SYNTHEA_LOG_PATH, FHIR_SERVER_URL, FHIR_TIMEOUT_SECONDS,
    UPSERT_WITH_PUT, MAX_ENTRIES_PER_TRANSACTION, MAX_CONSECUTIVE_FAILURES,
)
from fhir_client import (
    upload_bundle_to_server,
    test_server_connection,
    summarize_transaction_response,
    extract_error_message,
)
from synthea_loader import find_synthea_bundles, load_bundle, describe_bundle, convert_posts_to_puts, split_bundle


def parse_args():
    parser = argparse.ArgumentParser(description="Upload Synthea FHIR bundles to a HAPI FHIR server.")
    parser.add_argument("--folder", default=SYNTHEA_FHIR_DIR,
                        help="Synthea output/fhir folder (defaults to SYNTHEA_FHIR_DIR in config.py)")
    parser.add_argument("--dry-run", action="store_true",
                        help="Read and summarize the bundles without posting anything")
    return parser.parse_args()


def format_counts(counter, top=6):
    """Turn a Counter into a short readable string, largest first."""
    return ", ".join(f"{name} {count}" for name, count in counter.most_common(top))


def main():
    args = parse_args()
    print("\n ------  SYNTHEA UPLOAD STARTING  ------\n")

    # Step 1: Test server connection (skipped in dry-run mode)
    # --------------------------------------------------------
    if not args.dry_run:
        print("STEP 1: TESTING FHIR SERVER CONNECTION")
        print("--------------------------------------")
        if not test_server_connection():
            print("Cannot connect to FHIR server. Please check Docker and the hapi-fhir container are running.")
            sys.exit(1)
        print(f"✅ FHIR server connection successful ({FHIR_SERVER_URL})")
    else:
        print("DRY RUN: nothing will be posted to the server.")

    # Step 2: Find Synthea bundle files
    # ---------------------------------
    print("\nSTEP 2: FINDING SYNTHEA BUNDLES")
    print("-------------------------------")
    try:
        bundle_files = find_synthea_bundles(args.folder)
    except FileNotFoundError as e:
        print(f"❌ {e}")
        sys.exit(1)

    if not bundle_files:
        print(f"❌ No .json files found in {args.folder}")
        sys.exit(1)

    kinds = [kind for kind, _ in bundle_files]
    print(f"Found {len(bundle_files)} files in {args.folder}")
    print(f"   - {kinds.count('hospital')} hospital file(s)")
    print(f"   - {kinds.count('practitioner')} practitioner file(s)")
    print(f"   - {kinds.count('patient')} patient file(s)")

    # Step 3: Upload each bundle, reference data first
    # ------------------------------------------------
    print("\nSTEP 3: UPLOADING BUNDLES")
    print("-------------------------")
    log = []
    consecutive_failures = 0

    for position, (kind, path) in enumerate(bundle_files, start=1):
        filename = os.path.basename(path)
        print(f"\n[{position}/{len(bundle_files)}] {kind}: {filename}")

        log_row = {"File": filename, "Kind": kind, "PatientName": "", "Entries": 0,
                   "SwitchedToPut": "", "Chunks": "", "Status": "", "Seconds": "", "ServerResults": "", "Error": ""}

        # Read and describe the bundle
        try:
            bundle = load_bundle(path)
        except Exception as e:
            print(f"❌ Could not read bundle: {e}")
            log_row.update({"Status": "Read failed", "Error": str(e)[:300]})
            log.append(log_row)
            continue

        summary = describe_bundle(bundle)
        log_row["PatientName"] = summary["patient_name"]
        log_row["Entries"] = summary["entry_count"]
        if summary["patient_name"]:
            print(f"-- Patient: {summary['patient_name']}")
        print(f"-- {summary['entry_count']} entries: {format_counts(summary['resource_counts'])}")

        # Switch POST to PUT so re-runs update instead of duplicating
        if UPSERT_WITH_PUT:
            converted, left_as_is = convert_posts_to_puts(bundle)
            log_row["SwitchedToPut"] = converted
            print(f"-- {converted} entries switched from POST to PUT, {left_as_is} left as-is")

        # Split very large bundles into smaller transactions
        chunks = [bundle]
        if UPSERT_WITH_PUT:
            try:
                chunks = split_bundle(bundle, MAX_ENTRIES_PER_TRANSACTION)
            except ValueError as e:
                print(f"-- ⚠️ Could not split this bundle ({e}); sending it whole")
        log_row["Chunks"] = len(chunks)
        if len(chunks) > 1:
            sizes = ", ".join(str(len(c["entry"])) for c in chunks)
            print(f"-- Split into {len(chunks)} transactions ({sizes} entries)")

        if args.dry_run:
            log_row["Status"] = "Dry run"
            log.append(log_row)
            continue

        # Post each chunk in order, stopping at the first failure
        start = time.time()
        results = Counter()
        failure = None
        for number, chunk in enumerate(chunks, start=1):
            success, status_code, response_text = upload_bundle_to_server(chunk, timeout=FHIR_TIMEOUT_SECONDS)
            if not success:
                failure = (number, status_code, extract_error_message(response_text))
                break
            results += summarize_transaction_response(response_text)
        elapsed = round(time.time() - start, 1)
        log_row["Seconds"] = elapsed
        log_row["ServerResults"] = format_counts(results, top=10)

        if failure is None:
            consecutive_failures = 0
            log_row["Status"] = "Success"
            print(f"-- ✅ Uploaded in {elapsed}s ({format_counts(results, top=10)})")
            log.append(log_row)
        else:
            consecutive_failures += 1
            number, status_code, error = failure
            where = f" on transaction {number} of {len(chunks)}" if len(chunks) > 1 else ""
            log_row.update({"Status": f"Failed ({status_code}){where}", "Error": error})
            print(f"-- ❌ Upload failed ({status_code}){where} after {elapsed}s")
            print(f"   {error}")
            if number > 1:
                print("   Earlier transactions for this patient were saved. Re-running will fill in the rest.")
            log.append(log_row)

            # Patient bundles depend on the hospital and practitioner data.
            # If those fail, every patient upload will fail too, so stop here.
            if kind in ("hospital", "practitioner"):
                print("\n❌ Stopping: patient bundles reference this data and would all fail without it.")
                break

            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                print(f"\n❌ Stopping: {consecutive_failures} uploads failed in a row, so the server may be down "
                      "or out of memory. Check it with: docker logs hapi-synthea --tail 50")
                break

    # Step 4: Save log
    # ----------------
    print(f"\n\nSTEP 4: SAVING LOG TO {SYNTHEA_LOG_PATH}")
    print("-" * 60)
    try:
        pd.DataFrame(log).to_csv(SYNTHEA_LOG_PATH, index=False)
        print("✅ Log saved")
    except Exception as e:
        print(f"❌ Failed to save log: {e}")

    succeeded = sum(1 for row in log if row["Status"].startswith("Success"))
    failed = sum(1 for row in log if row["Status"].startswith(("Failed", "Read failed")))
    print(f"\nSummary: {succeeded} uploaded, {failed} failed, {len(bundle_files) - len(log)} not attempted")
    print("\n ------ SYNTHEA UPLOAD COMPLETED ------\n")


if __name__ == "__main__":
    main()
