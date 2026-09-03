"""Embedded development TLS certificate and certificate helpers.

Split from networker_dashboard.py (v2.5.0); behavior unchanged.
"""
from __future__ import annotations

from pathlib import Path

EMBEDDED_DEV_CERT_PEM = """-----BEGIN CERTIFICATE-----
MIIDTDCCAjSgAwIBAgIUFSAYa7f7edJjrVb8uehTQ5EmdaAwDQYJKoZIhvcNAQEL
BQAwFDESMBAGA1UEAwwJbG9jYWxob3N0MB4XDTI2MDkwMzE5MzUzOVoXDTI4MTIw
NjE5MzUzOVowFDESMBAGA1UEAwwJbG9jYWxob3N0MIIBIjANBgkqhkiG9w0BAQEF
AAOCAQ8AMIIBCgKCAQEA6ojrLgOwL+ioMaOlvRyN+kRwqoBJ9evzgiwyZySms3TT
UfJM/JZbgowNNU4hBvUaOIJH59ccMOeeKjHSpx6w7M1c/T/QOo6KWm7N3CipNIFk
eOwBUPcy6P21+ovY52ew6z+hDEYI7/yX0MgNaaRvJ6lSfL+7k3lhUo1o77SM/TKJ
f2WP57qFVL/j8qkWD2ZmPpGIpVszQyKkd6Z3iYKsRIOIQfJcFaxttnfIWpT4Z1R8
h7I4AXsQOiRCgHbv8toRRF6F9tXTs/m7LXcIQV99EwojuxACaE+oj8YJYcblhcy8
wzo2ZlnsPfaFqvnDvL9Fvs74AiK3aaRkJrQskyIwAQIDAQABo4GVMIGSMB0GA1Ud
DgQWBBQp2t10ln8yWHSw4Lz2UiRHsI0BzzAfBgNVHSMEGDAWgBQp2t10ln8yWHSw
4Lz2UiRHsI0BzzAaBgNVHREEEzARgglsb2NhbGhvc3SHBH8AAAEwDwYDVR0TAQH/
BAUwAwEB/zAOBgNVHQ8BAf8EBAMCAqQwEwYDVR0lBAwwCgYIKwYBBQUHAwEwDQYJ
KoZIhvcNAQELBQADggEBAFJS1ukbMwHxU0/HqPpXEMG01HOxdi/PjZHYkpx502W8
VfYjw+h+F8pGzz4QCLbuuwQBWJGpmL6LCmpy8IUB2b4RVzRVHViVLiSrgyjB+Qgw
agXr5T1VlSM9JFdcJvL0XJo0Bxlw8YC49hz1FTjnoSyd1WULqOLBmJ19CgXqmHQa
jZaSQYZ7NU+ErYvBB2K6RrsyvVNv5aRNt6REU6oB+xiVX5IT+bedvhaGpp/3SGfM
WHzB+K0GxqTXi6HGTLDZ9/GeLBzeb8O7uRD9yL2JAFtsaTbNXXWMKyIMgyLQouVL
Uf3QAfH4NTbf1hA4y2+C+WY+7iEVZ7ETCyqZXDpjdes=
-----END CERTIFICATE-----
"""


EMBEDDED_DEV_KEY_PEM = """-----BEGIN PRIVATE KEY-----
MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDqiOsuA7Av6Kgx
o6W9HI36RHCqgEn16/OCLDJnJKazdNNR8kz8lluCjA01TiEG9Ro4gkfn1xww554q
MdKnHrDszVz9P9A6jopabs3cKKk0gWR47AFQ9zLo/bX6i9jnZ7DrP6EMRgjv/JfQ
yA1ppG8nqVJ8v7uTeWFSjWjvtIz9Mol/ZY/nuoVUv+PyqRYPZmY+kYilWzNDIqR3
pneJgqxEg4hB8lwVrG22d8halPhnVHyHsjgBexA6JEKAdu/y2hFEXoX21dOz+bst
dwhBX30TCiO7EAJoT6iPxglhxuWFzLzDOjZmWew99oWq+cO8v0W+zvgCIrdppGQm
tCyTIjABAgMBAAECggEAIUEOpN9DAQKqWSIh+DbEmW1Q0uwOZenHBM2tdoXHg9Rs
Y3HJTAXUWHtGeCi+yBTCsKvQSZAXituccnvOqYF8MTyhEwtpmT6Strsqly0baLrG
pdqYs9kzr74hf7KKgQJpHxdOMhulXW8MO5R8n7OqoGNYCHYga4r0q1bU6n/y1FzW
L/XAfvARYuyuPKyiRnO60ybYrThyTGhCjdAbvZc3z2HXke9NMbDFhvShRbHr78nJ
CUb3/l4y8MHwW8xqbLl4rc1FyDFKxC4F/Jhy+dh10ii4XE54EDOOkUXqd6eeUxy+
ssFDzEkAxHyjb0Oiv5caf4zlFNh0gJLNMUD3c14vDQKBgQD88gsEU3NXo/v6RLyh
Uc9IQQgXuzTbTG2od1STeft7VM/EJ0BOGW6B+1fh+bVCj1i9g4OBMQ1Si4G/WrU5
U81QLPKwqYfaxL7GWiCm2WpIC/uE3ji/U+MGfDOfUwqReGc4tAXHXbiquBxscdJ4
2UgQDFuTi9MCiZwpF4AOxKdu4wKBgQDtXfX8YDP1UwDD8e3FRl1dllhcknAK93Qr
oeqvHAS0iuYN9jIjaPG/18DkucVkakLR28kTgmAYbCuPI4gJZ2lkdlS2UstJljIg
cBd/H70D4WHE73pv8ImmMfkucWETUXXismQpdLGu6KbnhG0croBFaO2tRhEA1nbN
h6Vhhf9WywKBgB9a8bqrjZTDoyy28JsexQ8z4Ijwj/DPXJiRkk5lxKLZNJggNXx4
8pXyTkaY6btCgcGcV+Tf68LbwE20NNrSZJUXvU3g9hJMaUm1Sm7kbKRZt+gUk/xD
rdTT+KI7bQfzYPhKeJzqJUYkZIGc6nZImQJEReXYY2PhIxaE7z7lacv5AoGBANhf
56UomRSPlkoIFkPXcbKnI2M3hfUpP3eqwMDhXJSrbSza+Td4Ka9EYKzff0Wa69Bb
dn9XQHvi1w5DSHWyE8ulZnFRJcitpoIVTAXxC58m67XWy5iQ/xlFwq0IY4J1pm2B
SnbTzkjwAX1YJRKZK4qaLNbf4Q4PcfrHJQWyXWFjAoGAYFE7qBH9HS88MwGtKWCJ
GqdIrdnssOgkzP1Fc1zOfx5xyDVnsHKDZC+F8qwSoNnjsodgnArh6HIc4GuczpI6
HLVADKUD9YN5AvwUaFWY0UNd5rAZjHSbgm0q+gy0EkIt8fdpuozNb/lxfmUOEnea
AP/jLU2gJrbDTMfIWYg4pYY=
-----END PRIVATE KEY-----
"""



def write_embedded_dev_certificate(cert_path: Path, key_path: Path) -> None:
    cert_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_text(EMBEDDED_DEV_CERT_PEM, encoding="ascii")
    key_path.write_text(EMBEDDED_DEV_KEY_PEM, encoding="ascii")
    try:
        key_path.chmod(0o600)
    except OSError:
        pass


def ensure_certificate(cert_path: Path, key_path: Path) -> bool:
    if cert_path.exists() and key_path.exists():
        return False

    write_embedded_dev_certificate(cert_path, key_path)
    return True
