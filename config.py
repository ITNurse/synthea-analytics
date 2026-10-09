import os

# ============================
# File Paths Configuration
# ============================
BASE_DIR = r"C:\Python\Synthea_PBI"

# Folder where Synthea writes its FHIR R4 bundles (one JSON file per patient,
# plus hospitalInformation*.json and practitionerInformation*.json).
# If your synthea folder holds only the JSON files (not the full Synthea
# project), change this to os.path.join(BASE_DIR, "synthea").
SYNTHEA_FHIR_DIR = os.path.join(BASE_DIR, "synthea", "output", "fhir")

# Output paths
OUTPUT_DIR = os.path.join(BASE_DIR, "output")
SYNTHEA_LOG_PATH = os.path.join(OUTPUT_DIR, "synthea_upload_log.csv")

# ============================
# FHIR Server Configuration
# ============================
# Port 8081 so this project uses its own HAPI container and never touches
# the wellness_way server on port 8080.
FHIR_SERVER_URL = "http://localhost:8081/fhir"
FHIR_HEADERS = {"Content-Type": "application/fhir+json"}

# Synthea patient bundles can hold thousands of resources, so HAPI may take
# a while to process each one. This is how long (in seconds) to wait.
FHIR_TIMEOUT_SECONDS = 300

# True: switch Synthea's POST requests to PUT using each resource's own ID,
# so re-running the upload updates resources instead of duplicating them.
# False: send the bundles exactly as Synthea wrote them.
UPSERT_WITH_PUT = True

# Largest number of entries sent in one transaction. Bigger Synthea patients
# are split into several transactions so HAPI does not run out of memory.
# Only used when UPSERT_WITH_PUT is True (splitting needs fixed IDs).
MAX_ENTRIES_PER_TRANSACTION = 2000

# Stop the run after this many patients fail in a row. Repeated failures
# usually mean the server itself is in trouble, and carrying on just adds
# noise to the log.
MAX_CONSECUTIVE_FAILURES = 3

# Ensure output directory exists
os.makedirs(OUTPUT_DIR, exist_ok=True)
