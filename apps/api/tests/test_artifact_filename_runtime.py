from urllib.parse import unquote_to_bytes

from artifact_content_runtime import FILENAME_PROBES
from artifact_runtime import artifact_runtime


def test_real_http_international_filename_and_retained_header_safety(tmp_path):
    with artifact_runtime(tmp_path) as runtime:
        own, foreign = runtime.clients
        work = runtime.works[0]
        content = b"Synthetic filename acceptance evidence\n"
        for name in FILENAME_PROBES:
            response = own.post(f"/api/runs/{work}/artifacts/upload", headers=runtime.leases[work],
                params={"name": name, "content_type": "text/plain"}, content=content)
            assert response.status_code == 201
            identity = response.json()["id"]
            url = f"/api/work-items/{work}/artifacts/{identity}"
            downloaded = own.get(url)
            assert downloaded.status_code == 200 and downloaded.content == content
            value = downloaded.headers["content-disposition"]
            assert value.isascii()
            assert unquote_to_bytes(value.split("filename*=UTF-8''", 1)[1]) == name.encode()
            assert downloaded.headers["content-security-policy"] == "sandbox"
            assert foreign.get(url).status_code == 404
        retained = runtime.retain_alias(runtime.key(identity), "bad\r\nX-Synthetic-Probe: 1.txt")
        downloaded = own.get(f"/api/work-items/{work}/artifacts/{retained}")
        assert downloaded.status_code == 200 and downloaded.content == content
        assert downloaded.headers["content-disposition"] == (
            'inline; filename="artifact"; filename*=UTF-8\'\'artifact')
        assert "x-synthetic-probe" not in downloaded.headers
