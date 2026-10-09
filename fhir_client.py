import json
from collections import Counter

import requests
from config import FHIR_SERVER_URL, FHIR_HEADERS


def upload_bundle_to_server(bundle, timeout=None):
    """
    Upload a FHIR bundle to the server.

    Args:
        bundle: FHIR bundle to upload (dictionary)
        timeout: Seconds to wait for the server (None means wait indefinitely,
                 which matches the original behaviour)

    Returns:
        tuple: (success: bool, status_code: int, response_text: str)
    """
    try:
        response = requests.post(FHIR_SERVER_URL, headers=FHIR_HEADERS, json=bundle, timeout=timeout)

        success = response.status_code in [200, 201]
        return success, response.status_code, response.text

    except requests.exceptions.RequestException as e:
        return False, 0, str(e)


def test_server_connection():
    """
    Test connection to FHIR server.

    Returns:
        bool: True if server is reachable
    """
    try:
        response = requests.get(f"{FHIR_SERVER_URL}/metadata", timeout=5)
        return response.status_code == 200
    except requests.exceptions.RequestException:
        return False


def summarize_transaction_response(response_text):
    """
    Count the per-entry results in a transaction-response Bundle.

    When HAPI accepts a transaction, it returns a Bundle of type
    "transaction-response" with one entry per resource you sent. Each entry
    has a response.status such as "201 Created" or "200 OK".

    Args:
        response_text: Raw text of the server response

    Returns:
        Counter: e.g. Counter({"201 Created": 412, "200 OK": 3})
    """
    try:
        response_bundle = json.loads(response_text)
    except (json.JSONDecodeError, TypeError):
        return Counter()

    statuses = Counter()
    for entry in response_bundle.get("entry", []):
        status = entry.get("response", {}).get("status", "unknown")
        statuses[status] += 1
    return statuses


def extract_error_message(response_text, max_length=300):
    """
    Pull a readable error out of a failed response.

    HAPI usually explains a rejected bundle with an OperationOutcome resource.
    This returns the first issue's diagnostics text if there is one, or the
    start of the raw response otherwise.
    """
    try:
        outcome = json.loads(response_text)
        if outcome.get("resourceType") == "OperationOutcome":
            for issue in outcome.get("issue", []):
                if issue.get("diagnostics"):
                    return issue["diagnostics"][:max_length]
    except (json.JSONDecodeError, TypeError, AttributeError):
        pass
    return str(response_text)[:max_length]
