# aibuilder/tests/test_errors.py
from errors import classify_error


def test_classifies_missing_credentials():
    out = classify_error("Error: NoCredentialProviders chain found")
    assert "credentials" in out["summary"].lower()


def test_classifies_bucket_collision():
    out = classify_error("Error: BucketAlreadyOwnedByYou: bucket 'x' already exists")
    assert "bucket" in out["summary"].lower()


def test_classifies_access_denied():
    out = classify_error("is not authorized to perform: s3:CreateBucket")
    assert "permission" in out["summary"].lower()


def test_falls_through_to_generic():
    out = classify_error("weird unparseable thing")
    assert out["summary"] == "Deployment failed — see details."
    assert "weird" in out["details"]


def test_details_truncated_to_last_2000_chars():
    long = "x" * 5000 + "TAIL"
    out = classify_error(long)
    assert out["details"].endswith("TAIL")
    assert len(out["details"]) == 2000
