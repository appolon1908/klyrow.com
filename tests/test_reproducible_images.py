from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]


def test_python_image_does_not_embed_nondeterministic_bytecode():
    for relative_path in ("apps/gateway/Dockerfile", "docker/migrate.Dockerfile"):
        dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
        assert "PYTHONDONTWRITEBYTECODE=1" in dockerfile
        assert "pip install --no-cache-dir --no-compile --no-deps" in dockerfile
        assert "pip check" in dockerfile


def test_python_dependency_closure_is_exactly_pinned():
    requirements = (ROOT / "apps/gateway/requirements.txt").read_text(encoding="utf-8")
    specifications = [
        line.strip()
        for line in requirements.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    assert specifications
    assert all("==" in specification for specification in specifications)
    assert "pydantic-core==2.46.5" in specifications
    assert "psycopg-binary==3.2.9" in specifications


def test_web_runtime_does_not_retain_nondeterministic_package_log():
    dockerfile = (ROOT / "apps/web/Dockerfile").read_text(encoding="utf-8")
    checksums = (ROOT / "apps/web/vendor/SHA256SUMS").read_text(encoding="utf-8")
    assert "apk upgrade" not in dockerfile
    assert "apk add --no-cache --no-network ./libexpat-2.8.4-r0.apk" in dockerfile
    assert "apk verify --keys-dir /etc/apk/keys libexpat-2.8.4-r0.apk" in dockerfile
    assert "sha256sum -c SHA256SUMS" in dockerfile
    assert "--repository" not in dockerfile
    assert "dl-cdn.alpinelinux.org" not in dockerfile
    assert checksums == (
        "34fa41e3994d9a844f50a7c01cb40bb2e272fa7f506b96e92a2b4a20b23a81bc  "
        "libexpat-2.8.4-r0.apk\n"
        "8306e5bb577696c9069fe1dfd9e1dcc39d2d481c6a1b0e707fd03c3e21aa6aa2  "
        "libuuid-2.42.3-r1.apk\n"
    )
    for entry in checksums.splitlines():
        digest, package = entry.split()
        assert __import__("hashlib").sha256(
            (ROOT / "apps/web/vendor" / package).read_bytes()
        ).hexdigest() == digest
        assert f"apk verify --keys-dir /etc/apk/keys {package}" in dockerfile
        assert f"./{package}" in dockerfile
    assert 'apk info --exists libuuid=2.42.3-r1' in dockerfile
    assert "rm -f /var/log/apk.log" in dockerfile


def test_every_container_build_stage_pins_its_base_manifest_digest():
    for relative_path in (
        "apps/gateway/Dockerfile",
        "apps/web/Dockerfile",
        "docker/migrate.Dockerfile",
        "docker/postal-provisioner.Dockerfile",
    ):
        dockerfile = (ROOT / relative_path).read_text(encoding="utf-8")
        from_lines = [line for line in dockerfile.splitlines() if line.startswith("FROM ")]
        assert from_lines
        assert all("@sha256:" in line for line in from_lines)
        assert "apk upgrade" not in dockerfile


def test_postal_runtime_is_patched_from_immutable_os_and_gem_inputs():
    dockerfile = (ROOT / "docker/postal-provisioner.Dockerfile").read_text(
        encoding="utf-8"
    )
    sources = (ROOT / "docker/postal-security/debian.sources.list").read_text(
        encoding="utf-8"
    )
    gemfile = (ROOT / "docker/postal-security/Gemfile").read_text(encoding="utf-8")
    lock = (ROOT / "docker/postal-security/Gemfile.lock").read_text(encoding="utf-8")
    assert sources.count("snapshot.debian.org/") == 3
    assert sources.count("20260908T120000Z") == 3
    assert "rm -f /etc/apt/sources.list.d/*" in dockerfile
    assert "apt-get dist-upgrade -y" in dockerfile
    assert "libssh2-1)\" = '1.10.0-3+deb12u1'" in dockerfile
    assert "setcap -r /usr/local/bin/ruby" in dockerfile
    assert 'test -z "$(getcap /usr/local/bin/ruby)"' in dockerfile
    os_patch_layer = dockerfile.split(
        "COPY docker/postal-security/Gemfile", maxsplit=1
    )[0]
    assert "rm -f" in os_patch_layer
    assert "/var/cache/ldconfig/aux-cache" in os_patch_layer
    assert "bundle config set deployment true" in dockerfile
    assert "bundle config set without 'development test'" in dockerfile
    assert "bundle clean --force" in dockerfile
    assert "/usr/local/bundle/gems/{activestorage-7.1.6" in dockerfile
    assert "-name gem_make.out -o -name mkmf.log" in dockerfile
    assert "find /opt/postal/app/vendor/bundle -type d -exec chmod 0755" in dockerfile
    assert "rm -rf /root/.bundle/cache" in dockerfile
    assert 'gem "rails", "= 7.2.3.2"' in gemfile
    assert 'gem "jwt", "= 2.10.3"' in gemfile
    assert 'gem "puma", "= 7.2.1"' in gemfile
    assert 'gem "resolv", "= 0.7.2"' in gemfile
    assert 'gem "zlib", "= 3.2.3"' in gemfile
    assert dockerfile.count("resolv-0.6.2") >= 7
    assert '"resolv"=>"0.7.2"' in dockerfile
    assert "CHECKSUMS" in lock
    assert "rails (7.2.3.2) sha256=" in lock
    assert "resolv (0.7.2) sha256=" in lock
    assert "zlib (3.2.3) sha256=" in lock
    assert 'ARG SOURCE_SHA' in dockerfile
    assert 'ARG SOURCE_DATE_EPOCH' in dockerfile
    assert 'export SOURCE_DATE_EPOCH' in dockerfile
    assert 'bundle install --jobs 1 --retry 3' in dockerfile
    assert 'org.opencontainers.image.revision=$SOURCE_SHA' in dockerfile
    assert 'wc -c)" -eq 40' in dockerfile


def test_ci_compares_two_timestamp_rewritten_oci_exports_before_publish():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "SOURCE_DATE_EPOCH=$(git log -1 --pretty=%ct)" in workflow
    assert "Verify reproducible OCI image bytes" in workflow
    assert "for copy in a b" in workflow
    assert "for image in gateway web migrate postal-provisioner" in workflow
    assert '[migrate]="docker/migrate.Dockerfile"' in workflow
    assert '[postal-provisioner]="docker/postal-provisioner.Dockerfile"' in workflow
    assert "--no-cache" in workflow
    assert "type=oci,dest=${RUNNER_TEMP}/klyrow-${image}-${copy}.tar,rewrite-timestamp=true" in workflow
    assert "cmp \\\n" in workflow
    assert workflow.count("outputs: type=registry,rewrite-timestamp=true") == 4
    initial_postal_build = workflow.split(
        "- name: Build Postal provisioner candidate", maxsplit=1
    )[1].split("- name: Build frontend candidate", maxsplit=1)[0]
    assert "SOURCE_DATE_EPOCH=${{ env.SOURCE_DATE_EPOCH }}" in initial_postal_build


def test_protected_publication_covers_every_production_owned_image():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "github.ref == 'refs/heads/main'" in workflow
    assert "github.ref_protected == true" in workflow
    assert 'test "$GITHUB_REF_PROTECTED" = \'true\'' in workflow
    assert 'test "$(git rev-parse HEAD)" = "$KLYROW_SOURCE_SHA"' in workflow
    for image in ("gateway", "web", "migrate", "postal-provisioner"):
        assert f"klyrow-{image}:candidate-${{{{ github.run_id }}}}" in workflow
        assert f"klyrow-{image}" in workflow
    assert "Sign and attest only unpublished exact digests" in workflow
    assert "Verify signatures, provenance, and SBOM attestations" in workflow
    assert "Promote only fully certified digests to immutable source tags" in workflow
    assert workflow.count("provenance: false") == 4
    assert workflow.count("sbom: false") == 4
    assert workflow.count("cosign attest --yes --type slsaprovenance1") == 1
    assert workflow.count("cosign attest --yes --type spdxjson") == 1


def test_protected_publisher_scans_and_verifies_exact_digests_before_promotion():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    publish = workflow.index("  publish:")
    candidate = workflow.index("Publish backend candidate", publish)
    scan = workflow.index("Scan exact backend digest", publish)
    collision = workflow.index("Reject immutable source-tag collisions before signing", publish)
    sign = workflow.index("Sign and attest only unpublished exact digests", publish)
    verify = workflow.index("Verify signatures, provenance, and SBOM attestations", publish)
    promote = workflow.index("Promote only fully certified digests to immutable source tags", publish)
    assert candidate < scan < collision < sign < verify < promote
    assert workflow.count("format: json") >= 4
    assert workflow.count("publish-trivy-") >= 4
    assert 'test "$existing" = "$digest"' in workflow
    assert "imagetools create --prefer-index=false" in workflow
    assert "cosign verify --certificate-identity" in workflow
    assert "sourceCommit == $source" in workflow
    assert "registryDigest == $digest" in workflow
    assert "sha256sum -c PUBLISH_SHA256SUMS" in workflow


def test_cosign_attestation_verification_normalizes_jsonl_and_legacy_arrays():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    normalize = '[.[] | if type == "array" then .[] else . end]'
    assert workflow.count(normalize) == 4
    assert workflow.count("jq -s -e --arg source") == 2
    assert workflow.count("jq -s -e '[.[] | if type") == 2
    assert "| jq -e --arg source" not in workflow
    assert "\n            jq -e 'any(.[];" not in workflow


def test_pull_request_images_and_evidence_bind_to_exact_head_sha():
    workflow = (ROOT / ".github/workflows/ci.yml").read_text(encoding="utf-8")
    assert "KLYROW_SOURCE_SHA: ${{ github.event.pull_request.head.sha || github.sha }}" in workflow
    # The sole foreign checkout supplies immutable test dependencies; every
    # Klyrow checkout must still bind images and evidence to the PR head.
    import json
    import yaml
    source = json.loads((ROOT / "integrations/codestra-middleware/SOURCE.json").read_text())
    foreign = 0
    for job in yaml.safe_load(workflow)["jobs"].values():
        for step in job.get("steps", []):
            if not step.get("uses", "").startswith("actions/checkout@"):
                continue
            options = step.get("with", {})
            if "repository" in options:
                foreign += 1
                assert options["repository"] == source["source_repository"]
                assert options["ref"] == source["source_sha"]
                assert options["path"] == ".ci-middleware"
                assert options["persist-credentials"] is False
            else:
                assert options["ref"] == "${{ env.KLYROW_SOURCE_SHA }}"
    assert foreign == 1
    assert workflow.count("github.sha") == 1
    assert "${{ github.sha }}" not in workflow
    assert "klyrow-gateway:${{ env.KLYROW_SOURCE_SHA }}" in workflow
    assert "org.opencontainers.image.revision=${{ env.KLYROW_SOURCE_SHA }}" in workflow
    assert "trivy-${{ env.KLYROW_SOURCE_SHA }}" in workflow
    assert "klyrow-gateway-${{ env.KLYROW_SOURCE_SHA }}.cdx.json" in workflow
    assert workflow.count("SOURCE_SHA=${{ env.KLYROW_SOURCE_SHA }}") == 2
    assert '--build-arg "SOURCE_SHA=${KLYROW_SOURCE_SHA}"' in workflow


def test_all_third_party_workflow_actions_are_commit_pinned():
    workflow = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / ".github/workflows").glob("*.yml"))
    )
    uses = [
        line.strip().split("uses:", 1)[1].strip().split()[0]
        for line in workflow.splitlines()
        if "uses:" in line
    ]
    assert uses
    for reference in uses:
        if reference.startswith("./"):
            assert "@" not in reference
            assert (ROOT / reference).resolve().is_relative_to(ROOT.resolve())
            assert (ROOT / reference).exists()
        else:
            assert re.fullmatch(r"[^@]+@[0-9a-f]{40}", reference), reference
