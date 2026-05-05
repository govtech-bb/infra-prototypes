"""Tests for tools._classify_error and deploy_infrastructure error paths."""

from unittest.mock import MagicMock, patch

import pytest

import tools


def test_classify_access_denied():
    result = tools._classify_error("Error: AccessDenied: User: arn:... is not authorized")
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()
    assert "AccessDenied" in result["details"]


def test_classify_no_credentials():
    result = tools._classify_error("NoCredentialProviders: no valid providers in chain")
    assert "credentials" in result["summary"].lower()


def test_classify_bucket_collision():
    result = tools._classify_error("BucketAlreadyOwnedByYou: bucket already exists")
    assert "project_name" in result["summary"]


def test_classify_unknown_falls_through_to_generic():
    result = tools._classify_error("kaboom: weird internal error")
    assert result["summary"] == "Deployment failed — see details."
    assert "kaboom" in result["details"]


def test_classify_truncates_long_details():
    huge = "x" * 10_000
    result = tools._classify_error(huge)
    assert len(result["details"]) <= 2000


@patch("tools.subprocess.run")
def test_deploy_infrastructure_init_failure_returns_summary(mock_run):
    def fake_run(cmd, **kwargs):
        if cmd[1] == "init":
            return MagicMock(returncode=1, stderr="NoCredentialProviders: ...")
        raise AssertionError(f"Unexpected subprocess call after init failure: {cmd}")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
    )
    assert "summary" in result
    assert "details" in result
    assert "credentials" in result["summary"].lower()


@patch("tools.subprocess.run")
def test_deploy_infrastructure_apply_failure_returns_summary(mock_run):
    # init succeeds, workspace cmds succeed, apply fails
    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply":
            return MagicMock(returncode=1, stderr="AccessDenied: not authorized to s3:CreateBucket")
        return MagicMock(returncode=0, stderr="", stdout="{}")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
    )
    assert "summary" in result
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()


@patch("tools.subprocess.run")
def test_deploy_infrastructure_happy_path_returns_outputs(mock_run):
    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(returncode=0, stdout='''{
              "bucket_name": {"value": "x-proto-static"},
              "site_url": {"value": "https://d.cloudfront.net"},
              "cloudfront_distribution_id": {"value": "ABC"}
            }''', stderr="")
        return MagicMock(returncode=0, stderr="", stdout="")
    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x", env="proto",
        site_title="X", owner_name="Y", owner_email="z@example.com",
    )
    assert "summary" not in result
    assert result["bucket_name"] == "x-proto-static"
    assert result["site_url"] == "https://d.cloudfront.net"
    assert result["project_name"] == "x"
    assert result["env"] == "proto"


# ── upload_files tests (with moto) ────────────────────────────────────────────


@pytest.fixture
def aws_credentials(monkeypatch):
    """Stub AWS env vars so moto/boto3 is happy without real creds."""
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "testing")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "testing")
    monkeypatch.setenv("AWS_DEFAULT_REGION", "us-east-1")


def test_upload_files_uploads_nested_with_correct_key(aws_credentials, tmp_path):
    from moto import mock_aws
    import boto3

    from sessions import Session

    upload_dir = tmp_path / "upload"
    (upload_dir / "assets" / "css").mkdir(parents=True)
    (upload_dir / "index.html").write_text("<html></html>")
    (upload_dir / "assets" / "css" / "main.css").write_text("body{}")

    with mock_aws():
        s3 = boto3.client("s3", region_name="us-east-1")
        s3.create_bucket(Bucket="test-bucket")
        # moto's CloudFront mock requires a real-ish distribution; skip create — invalidation
        # call is what we want to verify, and moto raises NoSuchDistribution if missing,
        # so we patch the CF call instead.
        with patch.object(tools.boto3, "client") as mock_client:
            real_s3 = s3
            mock_cf = MagicMock()
            mock_client.side_effect = lambda name, *a, **kw: real_s3 if name == "s3" else mock_cf

            session = Session(session_id="s", upload_dir=str(upload_dir))
            result = tools.upload_files(
                bucket_name="test-bucket",
                distribution_id="DIST123",
                session=session,
            )

            assert result["uploaded_count"] == 2
            assert "assets/css/main.css" in result["files"]
            assert "index.html" in result["files"]

            keys = {o["Key"] for o in real_s3.list_objects_v2(Bucket="test-bucket")["Contents"]}
            assert keys == {"index.html", "assets/css/main.css"}

            mock_cf.create_invalidation.assert_called_once()
            inv_args = mock_cf.create_invalidation.call_args.kwargs
            assert inv_args["DistributionId"] == "DIST123"
            assert inv_args["InvalidationBatch"]["Paths"]["Items"] == ["/*"]


def test_upload_files_returns_summary_when_dir_missing(tmp_path):
    from sessions import Session
    session = Session(session_id="s", upload_dir=str(tmp_path / "does-not-exist"))
    result = tools.upload_files(
        bucket_name="b", distribution_id="d", session=session,
    )
    assert "summary" in result
    assert "No uploaded files" in result["summary"]
