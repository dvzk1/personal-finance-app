"""GET de JSON compartilhado pelos provedores. Erro de rede, HTTP ou JSON vira OSError ou
ValueError, que é o que o encadeamento de provedores trata como "este falhou"."""

import json
import urllib.request

TIMEOUT = 10
# Alguns provedores recusam requisição sem User-Agent de navegador.
USER_AGENT = "Mozilla/5.0 (compatible; finance-control)"


def get_json(url: str, headers: dict | None = None):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT,
                                                   **(headers or {})})
    with urllib.request.urlopen(request, timeout=TIMEOUT) as response:
        return json.load(response)
