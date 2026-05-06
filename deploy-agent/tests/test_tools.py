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
def test_deploy_infrastructure_init_failure_returns_summary(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "init":
            return MagicMock(returncode=1, stderr="NoCredentialProviders: ...")
        raise AssertionError(f"Unexpected subprocess call after init failure: {cmd}")

    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        session=session,
    )
    assert "summary" in result
    assert "details" in result
    assert "credentials" in result["summary"].lower()


@patch("tools.subprocess.run")
def test_deploy_infrastructure_apply_failure_returns_summary(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "apply":
            return MagicMock(returncode=1, stderr="AccessDenied: not authorized to s3:CreateBucket")
        return MagicMock(returncode=0, stderr="", stdout="{}")

    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        session=session,
    )
    assert "summary" in result
    assert "permission" in result["summary"].lower() or "credentials" in result["summary"].lower()


@patch("tools.subprocess.run")
def test_deploy_infrastructure_happy_path_returns_outputs(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(
                returncode=0,
                stdout="""{
              "bucket_name": {"value": "x-proto-static"},
              "site_url": {"value": "https://d.cloudfront.net"},
              "cloudfront_distribution_id": {"value": "ABC"}
            }""",
                stderr="",
            )
        return MagicMock(returncode=0, stderr="", stdout="")

    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        session=session,
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
    import boto3
    from moto import mock_aws

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
        bucket_name="b",
        distribution_id="d",
        session=session,
    )
    assert "summary" in result
    assert "No uploaded files" in result["summary"]


# ── _preflight_uploads tests ──────────────────────────────────────────────────


def test_preflight_empty_upload(tmp_path):
    upload_dir = tmp_path / "empty"
    upload_dir.mkdir()
    err, idx = tools._preflight_uploads(str(upload_dir))
    assert err is not None
    assert "No files uploaded" in err["summary"]
    assert idx is None


def test_preflight_missing_dir():
    err, idx = tools._preflight_uploads(None)
    assert err is not None
    assert "No files uploaded" in err["summary"]
    assert idx is None


def test_preflight_jsx_with_no_html(tmp_path):
    (tmp_path / "App.jsx").write_text("export default () => null")
    (tmp_path / "index.css").write_text("body{}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is not None
    assert "source code" in err["summary"].lower()
    assert "npm run build" in err["summary"]
    assert "App.jsx" in err["details"]
    assert idx is None


def test_preflight_single_non_index_html(tmp_path):
    (tmp_path / "home.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx == "home.html"


def test_preflight_single_html_case_insensitive(tmp_path):
    (tmp_path / "Home.HTML").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx == "Home.HTML"


def test_preflight_multiple_html_no_index(tmp_path):
    (tmp_path / "home.html").write_text("<html></html>")
    (tmp_path / "about.html").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is not None
    assert "Multiple HTML files" in err["summary"]
    assert "home.html" in err["details"]
    assert "about.html" in err["details"]
    assert idx is None


def test_preflight_index_html_present(tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None  # caller defaults to "index.html"


def test_preflight_index_html_with_source_files_passes(tmp_path):
    # If an index.html is present, source files alongside it are fine
    # (e.g., a built site that bundled .ts source maps).
    (tmp_path / "index.html").write_text("<html></html>")
    (tmp_path / "app.ts").write_text("export {}")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None


def test_preflight_only_static_non_html_assets_passes(tmp_path):
    # CSS / images / fonts only — no HTML, no source code. Let it through;
    # caller defaults to "index.html" which won't exist, but tofu will provision
    # the bucket and the user can fix it.
    (tmp_path / "style.css").write_text("body{}")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None


def test_preflight_does_not_match_substring_in_filename(tmp_path):
    # A file named *.tsx-migration.txt should NOT trigger the source-code branch
    # because we match on suffix, not name.
    (tmp_path / "notes-on-tsx-migration.txt").write_text("hello")
    (tmp_path / "index.html").write_text("<html></html>")
    err, idx = tools._preflight_uploads(str(tmp_path))
    assert err is None
    assert idx is None


# ── deploy_infrastructure preflight integration ───────────────────────────────


@patch("tools.subprocess.run")
def test_deploy_short_circuits_on_preflight_fail(mock_run, tmp_path):
    upload_dir = tmp_path / "empty"
    upload_dir.mkdir()
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(upload_dir))

    result = tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        session=session,
    )

    assert "summary" in result
    assert "No files uploaded" in result["summary"]
    mock_run.assert_not_called()


@patch("tools.subprocess.run")
def test_deploy_passes_index_document_var_when_auto_detected(mock_run, tmp_path):
    (tmp_path / "home.html").write_text("<html></html>")
    (tmp_path / "style.css").write_text("body{}")
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(
                returncode=0,
                stdout="""{
              "bucket_name": {"value": "x-proto-static"},
              "site_url": {"value": "https://d.cloudfront.net"},
              "cloudfront_distribution_id": {"value": "ABC"}
            }""",
                stderr="",
            )
        return MagicMock(returncode=0, stderr="", stdout="")

    mock_run.side_effect = fake_run

    result = tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        session=session,
    )
    assert "summary" not in result
    apply_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "apply"]
    assert apply_calls, "tofu apply was not called"
    apply_cmd = apply_calls[0].args[0]
    assert "-var=index_document=home.html" in apply_cmd


@patch("tools.subprocess.run")
def test_deploy_passes_explicit_index_document_overrides_auto(mock_run, tmp_path):
    (tmp_path / "home.html").write_text("<html></html>")
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(
                returncode=0,
                stdout="""{
              "bucket_name": {"value": "x"},
              "site_url": {"value": "https://x"},
              "cloudfront_distribution_id": {"value": "C"}
            }""",
                stderr="",
            )
        return MagicMock(returncode=0, stderr="", stdout="")

    mock_run.side_effect = fake_run

    tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        index_document="custom.html",
        session=session,
    )
    apply_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "apply"]
    apply_cmd = apply_calls[0].args[0]
    assert "-var=index_document=custom.html" in apply_cmd


@patch("tools.subprocess.run")
def test_deploy_default_index_html_when_present(mock_run, tmp_path):
    (tmp_path / "index.html").write_text("<html></html>")
    from sessions import Session

    session = Session(session_id="s", upload_dir=str(tmp_path))

    def fake_run(cmd, **kwargs):
        if cmd[1] == "output":
            return MagicMock(
                returncode=0,
                stdout="""{
              "bucket_name": {"value": "x"},
              "site_url": {"value": "https://x"},
              "cloudfront_distribution_id": {"value": "C"}
            }""",
                stderr="",
            )
        return MagicMock(returncode=0, stderr="", stdout="")

    mock_run.side_effect = fake_run

    tools.deploy_infrastructure(
        project_name="x",
        env="proto",
        site_title="X",
        owner_name="Y",
        owner_email="z@example.com",
        session=session,
    )
    apply_calls = [c for c in mock_run.call_args_list if c.args[0][1] == "apply"]
    apply_cmd = apply_calls[0].args[0]
    assert not any(arg.startswith("-var=index_document=") for arg in apply_cmd), (
        f"Unexpected index_document flag in {apply_cmd}"
    )
