#!/usr/bin/env python3
"""
FHIR Field Profiler

Reads the Synthea bundles on disk and reports which fields are actually
filled in for each resource type, so you know what's worth keeping in
Power Query (and what Synthea never populates).

For every field path (for example AllergyIntolerance.reaction.severity) it reports:
  - how many resources have a value there, and what percentage that is
  - the most values seen in a single resource (above 1 means the field
    repeats, so a "take the first one" approach in Power Query would drop data)
  - how many distinct values it has, and the most common ones

Usage (from the project folder, with the venv active):
    python field_profile.py                                  profile every resource type
    python field_profile.py --resource AllergyIntolerance    profile one type
    python field_profile.py --resource Condition --folder "C:\\other\\output\\fhir"
"""

import argparse
import csv
import os
from collections import Counter, defaultdict

from config import SYNTHEA_FHIR_DIR, OUTPUT_DIR
from synthea_loader import find_synthea_bundles, load_bundle

# Longest example value to show (narrative text and base64 data can be huge)
MAX_EXAMPLE_LENGTH = 60

# How many of the most common values to list per field
EXAMPLES_PER_FIELD = 3


def parse_args():
    parser = argparse.ArgumentParser(description="Report which FHIR fields Synthea fills in.")
    parser.add_argument("--resource", help="Resource type to profile, e.g. AllergyIntolerance (default: all)")
    parser.add_argument("--folder", default=SYNTHEA_FHIR_DIR,
                        help="Synthea output/fhir folder (defaults to SYNTHEA_FHIR_DIR in config.py)")
    return parser.parse_args()


def collect_values(node, path, values):
    """
    Walk a resource and record every simple value under its field path.

    Lists are walked through without adding anything to the path, so every
    entry of reaction[] lands under the same "reaction" path, matching how
    FHIR paths are usually written.

    Args:
        node: The part of the resource being walked
        path: The field path so far, e.g. "AllergyIntolerance.reaction"
        values: dict of path -> list of values found in this resource
    """
    if isinstance(node, dict):
        for key, value in node.items():
            collect_values(value, f"{path}.{key}", values)
    elif isinstance(node, list):
        for item in node:
            collect_values(item, path, values)
    else:
        values[path].append(node)


def short_text(value):
    """Shorten a value for display."""
    text = str(value).replace("\n", " ")
    return text if len(text) <= MAX_EXAMPLE_LENGTH else text[:MAX_EXAMPLE_LENGTH - 3] + "..."


def main():
    args = parse_args()

    # Running totals, per resource type and field path
    resource_counts = Counter()                          # resource type -> number of resources
    filled_counts = defaultdict(Counter)                 # type -> path -> resources with a value
    max_per_resource = defaultdict(Counter)              # type -> path -> most values in one resource
    value_counts = defaultdict(lambda: defaultdict(Counter))  # type -> path -> value -> count

    bundle_files = find_synthea_bundles(args.folder)
    print(f"Reading {len(bundle_files)} bundle files from {args.folder} ...")

    for _, path in bundle_files:
        try:
            bundle = load_bundle(path)
        except Exception as e:
            print(f"⚠️ Skipping {os.path.basename(path)}: {e}")
            continue

        for entry in bundle.get("entry", []):
            resource = entry.get("resource", {})
            resource_type = resource.get("resourceType")
            if not resource_type or (args.resource and resource_type != args.resource):
                continue

            resource_counts[resource_type] += 1
            values = defaultdict(list)
            collect_values(resource, resource_type, values)

            for field_path, found in values.items():
                filled_counts[resource_type][field_path] += 1
                if len(found) > max_per_resource[resource_type][field_path]:
                    max_per_resource[resource_type][field_path] = len(found)
                for value in found:
                    value_counts[resource_type][field_path][short_text(value)] += 1

    if not resource_counts:
        wanted = args.resource or "any"
        print(f"❌ No {wanted} resources found. Check the spelling (resource types are case-sensitive).")
        return

    # Build one row per field path
    rows = []
    for resource_type in sorted(resource_counts):
        total = resource_counts[resource_type]
        for field_path in sorted(filled_counts[resource_type]):
            filled = filled_counts[resource_type][field_path]
            common = value_counts[resource_type][field_path].most_common(EXAMPLES_PER_FIELD)
            rows.append({
                "ResourceType": resource_type,
                "FieldPath": field_path,
                "ResourcesWithValue": filled,
                "TotalResources": total,
                "PercentFilled": round(100 * filled / total, 1),
                "MaxPerResource": max_per_resource[resource_type][field_path],
                "DistinctValues": len(value_counts[resource_type][field_path]),
                "CommonValues": " | ".join(f"{value} ({count})" for value, count in common),
            })

    # Save to CSV
    suffix = f"_{args.resource}" if args.resource else ""
    output_path = os.path.join(OUTPUT_DIR, f"field_profile{suffix}.csv")
    with open(output_path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)

    # Print a short summary
    print("\nResources profiled:")
    for resource_type, count in resource_counts.most_common():
        fields = len(filled_counts[resource_type])
        print(f"   - {resource_type}: {count} resources, {fields} fields with values")

    if args.resource:
        print(f"\nFields in {args.resource} (% of resources with a value, ⟳ = repeats):")
        for row in rows:
            repeats = f"  ⟳ up to {row['MaxPerResource']}" if row["MaxPerResource"] > 1 else ""
            print(f"   {row['PercentFilled']:5.1f}%  {row['FieldPath']}{repeats}")

    print(f"\n✅ Full profile saved to {output_path}")


if __name__ == "__main__":
    main()
