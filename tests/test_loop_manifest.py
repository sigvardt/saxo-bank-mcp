from __future__ import annotations

from saxo_bank_mcp.loop_manifest import ManifestSpec, build_manifest


def test_manifest_records_replay_metadata() -> None:
    manifest = build_manifest(
        ManifestSpec(
            run_id="run-1",
            scenario_id="live-write-refusal",
            command=("python", "-m", "saxo_bank_mcp.qa", "live-write-refusal"),
            expected_status="refused",
            evidence_paths=("out.json",),
        ),
    )

    payload = manifest.to_json_value()

    assert payload["run_id"] == "run-1"
    assert payload["scenario_id"] == "live-write-refusal"
    assert payload["expected_status"] == "refused"
    assert "git" in payload
