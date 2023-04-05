import tempfile
from pathlib import Path
from subprocess import check_call

SSL_CONFIG_FILE = "src/templates/ssl.conf.j2"


def gen_certs(model: str, service_name: str):
    """Generate certificates."""

    ssl_conf = Path(SSL_CONFIG_FILE).read_text()
    ssl_conf = ssl_conf.replace("{{ model }}", str(model))
    ssl_conf = ssl_conf.replace("{{ service_name }}", str(service_name))

    with tempfile.TemporaryDirectory() as tmp_dir:
        Path(tmp_dir + "/ssl.conf").write_text(ssl_conf)

        # execute OpenSSL commands
        check_call(["openssl", "genrsa", "-out", tmp_dir + "/ca.key", "2048"])
        check_call(["openssl", "genrsa", "-out", tmp_dir + "/server.key", "2048"])
        check_call(
            [
                "openssl",
                "req",
                "-x509",
                "-new",
                "-sha256",
                "-nodes",
                "-days",
                "3650",
                "-key",
                tmp_dir + "/ca.key",
                "-subj",
                "/CN=127.0.0.1",
                "-out",
                tmp_dir + "/ca.crt",
            ]
        )
        check_call(
            [
                "openssl",
                "req",
                "-new",
                "-sha256",
                "-key",
                tmp_dir + "/server.key",
                "-out",
                tmp_dir + "/server.csr",
                "-config",
                tmp_dir + "/ssl.conf",
            ]
        )
        check_call(
            [
                "openssl",
                "x509",
                "-req",
                "-sha256",
                "-in",
                tmp_dir + "/server.csr",
                "-CA",
                tmp_dir + "/ca.crt",
                "-CAkey",
                tmp_dir + "/ca.key",
                "-CAcreateserial",
                "-out",
                tmp_dir + "/cert.pem",
                "-days",
                "365",
                "-extensions",
                "v3_ext",
                "-extfile",
                tmp_dir + "/ssl.conf",
            ]
        )

        ret_certs = {
            "cert": Path(tmp_dir + "/cert.pem").read_text(),
            "key": Path(tmp_dir + "/server.key").read_text(),
            "ca": Path(tmp_dir + "/ca.crt").read_text(),
        }

        # cleanup temporary files
        check_call(["rm", "-f", tmp_dir + "/cert-gen-*"])

    return ret_certs
