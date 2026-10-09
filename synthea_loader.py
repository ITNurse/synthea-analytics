"""
Helpers for finding and reading Synthea's FHIR R4 output.

Synthea writes three kinds of file to output/fhir:

  hospitalInformation<timestamp>.json      Organizations and Locations
  practitionerInformation<timestamp>.json  Practitioners and PractitionerRoles
  <Given>_<Family>_<uuid>.json             One file per patient

Patient bundles do not embed their hospital or doctor. Instead they point to
them with conditional references, for example
"Organization?identifier=https://github.com/synthetichealth/synthea|<id>".
The server resolves those by searching for a matching resource, so the
hospital and practitioner bundles must be loaded BEFORE any patient bundle,
or the patient uploads will fail.
"""

import glob
import heapq
import json
import os
from collections import Counter

HOSPITAL_PREFIX = "hospitalInformation"
PRACTITIONER_PREFIX = "practitionerInformation"


def find_synthea_bundles(fhir_dir):
    """
    Find Synthea bundle files and return them in the order they must be uploaded.

    Args:
        fhir_dir: Path to Synthea's output/fhir folder

    Returns:
        list of tuples: [(kind, file_path), ...] where kind is
        "hospital", "practitioner", or "patient"
    """
    if not os.path.isdir(fhir_dir):
        raise FileNotFoundError(f"Synthea FHIR folder not found: {fhir_dir}")

    all_files = sorted(glob.glob(os.path.join(fhir_dir, "*.json")))

    hospital_files = []
    practitioner_files = []
    patient_files = []

    for path in all_files:
        name = os.path.basename(path)
        if name.startswith(HOSPITAL_PREFIX):
            hospital_files.append(("hospital", path))
        elif name.startswith(PRACTITIONER_PREFIX):
            practitioner_files.append(("practitioner", path))
        else:
            patient_files.append(("patient", path))

    # Shared reference data first, then patients
    return hospital_files + practitioner_files + patient_files


def load_bundle(file_path):
    """
    Read a Synthea bundle from disk and check it is something we can POST.

    Args:
        file_path: Path to a Synthea JSON file

    Returns:
        dict: The bundle

    Raises:
        ValueError: If the file is not a FHIR transaction Bundle
    """
    with open(file_path, "r", encoding="utf-8") as f:
        bundle = json.load(f)

    if bundle.get("resourceType") != "Bundle":
        raise ValueError(f"Not a FHIR Bundle (resourceType is {bundle.get('resourceType')})")

    # Synthea writes transaction bundles by default. If someone changes
    # exporter.fhir.transaction_bundle to false in synthea.properties, the
    # files become "collection" bundles, which a server will not process.
    if bundle.get("type") not in ("transaction", "batch"):
        raise ValueError(
            f"Bundle type is '{bundle.get('type')}', expected 'transaction'. "
            "Check exporter.fhir.transaction_bundle in synthea.properties."
        )

    return bundle


def convert_posts_to_puts(bundle):
    """
    Switch plain POST entries to PUT so re-uploading the same files does not
    create duplicates.

    Why this works:
      Synthea gives every resource a UUID, used both as resource.id and in
      the entry's fullUrl ("urn:uuid:<uuid>"), but asks the server to POST
      it. A POST always creates a new resource with a new server-assigned ID,
      so uploading twice makes two copies.
      A PUT to "Patient/<uuid>" means "create this resource with this ID, or
      update it if it already exists". Uploading twice updates the same
      resource instead of copying it.

    What is left alone:
      - Entries that are already PUT.
      - POST entries with "ifNoneExist" (a conditional create). Synthea uses
        these in the hospital and practitioner bundles; the server only
        creates them if a match is not already there, so they never duplicate.
      - Any entry without a usable ID (counted, so you can see if it happens).

    References between resources still work: within a transaction, the server
    maps every "urn:uuid:<uuid>" reference to the matching PUT target.

    The bundle is changed in place.

    Args:
        bundle: FHIR transaction bundle (dictionary)

    Returns:
        tuple: (converted_count, left_as_is_count)
    """
    converted = 0
    left_as_is = 0

    for entry in bundle.get("entry", []):
        request = entry.get("request", {})

        if request.get("method") != "POST" or request.get("ifNoneExist"):
            left_as_is += 1
            continue

        resource = entry.get("resource", {})
        resource_type = resource.get("resourceType")
        resource_id = resource.get("id")

        # Fall back to the UUID in fullUrl if the resource has no id
        if not resource_id:
            full_url = entry.get("fullUrl", "")
            if full_url.startswith("urn:uuid:"):
                resource_id = full_url[len("urn:uuid:"):]
                resource["id"] = resource_id

        if not resource_type or not resource_id:
            left_as_is += 1
            continue

        request["method"] = "PUT"
        request["url"] = f"{resource_type}/{resource_id}"
        converted += 1

    return converted, left_as_is


def _find_references(node):
    """Yield every "reference" string anywhere inside a resource."""
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str):
            yield ref
        for value in node.values():
            yield from _find_references(value)
    elif isinstance(node, list):
        for item in node:
            yield from _find_references(item)


def _rewrite_references(node, replacements):
    """Swap reference strings in place using a {old: new} lookup."""
    if isinstance(node, dict):
        ref = node.get("reference")
        if isinstance(ref, str) and ref in replacements:
            node["reference"] = replacements[ref]
        for value in node.values():
            _rewrite_references(value, replacements)
    elif isinstance(node, list):
        for item in node:
            _rewrite_references(item, replacements)


def _dependency_order(dependencies):
    """
    Order entries so every resource comes after the resources it points to
    (a topological sort). Ties keep Synthea's original order.

    Returns None if the references loop back on themselves.
    """
    count = len(dependencies)
    waiting_on = [len(deps) for deps in dependencies]
    needed_by = [[] for _ in range(count)]
    for index, deps in enumerate(dependencies):
        for dep in deps:
            needed_by[dep].append(index)

    ready = [i for i in range(count) if waiting_on[i] == 0]
    heapq.heapify(ready)
    order = []
    while ready:
        index = heapq.heappop(ready)
        order.append(index)
        for dependent in needed_by[index]:
            waiting_on[dependent] -= 1
            if waiting_on[dependent] == 0:
                heapq.heappush(ready, dependent)

    return order if len(order) == count else None


def split_bundle(bundle, max_entries):
    """
    Split a large transaction bundle into smaller transactions.

    Why: HAPI processes a whole transaction in memory, and some Synthea
    patients have 15,000+ entries, which can exhaust the server's memory.

    How:
      1. Order the entries so each resource comes after anything it
         references (Patient, then Encounters, then the Conditions and
         Observations that point to those Encounters, and so on).
      2. Cut that ordered list into chunks of at most max_entries.
      3. Inside a chunk, "urn:uuid:" references work as usual. A reference
         to a resource in an EARLIER chunk is rewritten to its real ID
         (for example "Encounter/<uuid>"), since that resource is already
         on the server by the time this chunk arrives.

    Trade-off: a single transaction is all-or-nothing, but a split patient
    is not. If chunk 3 of 6 fails, chunks 1 and 2 stay on the server. Because
    entries use PUT, re-running the upload fills in the rest without
    duplicating anything.

    Requires convert_posts_to_puts() to have run first, so each resource
    has a fixed ID.

    Args:
        bundle: FHIR transaction bundle (dictionary)
        max_entries: Largest number of entries per transaction

    Returns:
        list of bundles. A bundle already within the limit comes back as-is.

    Raises:
        ValueError: If the bundle cannot be split safely
    """
    entries = bundle.get("entry", [])
    if len(entries) <= max_entries:
        return [bundle]

    # Map each "urn:uuid:..." fullUrl to its position in the entry list
    position_of = {entry["fullUrl"]: i for i, entry in enumerate(entries) if entry.get("fullUrl")}

    # Work out which entries each entry points to
    dependencies = []
    for i, entry in enumerate(entries):
        deps = set()
        for ref in _find_references(entry.get("resource", {})):
            j = position_of.get(ref)
            if j is not None and j != i:
                deps.add(j)
        dependencies.append(deps)

    order = _dependency_order(dependencies)
    if order is None:
        raise ValueError("references form a loop, so the bundle cannot be split safely")

    chunk_of = {index: position // max_entries for position, index in enumerate(order)}
    chunk_count = max(chunk_of.values()) + 1
    chunks = [[] for _ in range(chunk_count)]

    for index in order:
        entry = entries[index]
        my_chunk = chunk_of[index]

        # Rewrite references that point to an earlier chunk
        replacements = {}
        for dep in dependencies[index]:
            if chunk_of[dep] < my_chunk:
                target = entries[dep]
                if target.get("request", {}).get("method") != "PUT":
                    raise ValueError(
                        f"{target.get('fullUrl')} has no fixed ID (not a PUT), "
                        "so other chunks cannot point to it"
                    )
                resource = target["resource"]
                replacements[target["fullUrl"]] = f"{resource['resourceType']}/{resource['id']}"
        if replacements:
            _rewrite_references(entry.get("resource", {}), replacements)

        chunks[my_chunk].append(entry)

    return [{"resourceType": "Bundle", "type": bundle.get("type", "transaction"), "entry": chunk}
            for chunk in chunks]


def describe_bundle(bundle):
    """
    Summarize what is inside a bundle, for printing and logging.

    Args:
        bundle: FHIR bundle (dictionary)

    Returns:
        dict with:
            patient_name: "Given Family" if the bundle has a Patient, else ""
            entry_count:  total number of entries
            resource_counts: Counter of resourceType -> count
    """
    resource_counts = Counter()
    patient_name = ""

    for entry in bundle.get("entry", []):
        resource = entry.get("resource", {})
        resource_type = resource.get("resourceType", "Unknown")
        resource_counts[resource_type] += 1

        if resource_type == "Patient" and not patient_name:
            names = resource.get("name", [])
            if names:
                given = " ".join(names[0].get("given", []))
                family = names[0].get("family", "")
                patient_name = f"{given} {family}".strip()

    return {
        "patient_name": patient_name,
        "entry_count": sum(resource_counts.values()),
        "resource_counts": resource_counts,
    }
