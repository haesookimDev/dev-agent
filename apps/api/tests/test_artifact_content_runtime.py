from artifact_content_runtime import MARKUP, png_evidence, seed_content
from artifact_runtime import artifact_runtime


def test_real_http_artifact_content_policy(tmp_path):
    with artifact_runtime(tmp_path) as runtime:
        evidence = seed_content(runtime)
        assert evidence["registration"] == 415
        work = evidence["work"]
        own, foreign = runtime.clients
        for name, expected_type, content in [
            ("plain-probe.txt", "text/plain", MARKUP),
            ("evidence.png", "image/png", png_evidence()),
            ("result.json", "application/json", b'{"result":"synthetic evidence"}'),
        ]:
            url = f"/api/work-items/{work}/artifacts/{evidence['artifacts'][name]}"
            response = own.get(url)
            assert response.status_code == 200 and response.content == content
            assert response.headers["content-type"].split(";")[0] == expected_type
            assert response.headers["content-security-policy"] == "sandbox"
            assert response.headers["x-content-type-options"] == "nosniff"
            assert foreign.get(url).status_code == 404
        unsafe = evidence["artifacts"]["unsupported-report.html"]
        denied = own.get(f"/api/work-items/{work}/artifacts/{unsafe}")
        assert denied.status_code == 410
        assert denied.json() == {"detail": "artifact content is unavailable"}
