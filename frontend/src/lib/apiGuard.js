// Quem pode falar com a API do dashboard (o middleware em vite.config.js).
//
// A API roda no computador de quem usa e grava no banco. Duas coisas a protegem de um
// site qualquer aberto em outra aba do mesmo navegador:
//
// - Host: só nomes do próprio computador. Barra o DNS rebinding, em que um domínio
//   passa a resolver para 127.0.0.1 e a página dele lê os relatórios como se fosse local.
// - Origin, em toda requisição que não é leitura: tem de ser a do próprio dashboard. Um
//   formulário ou um fetch em text/plain de outro site não passam pela checagem de CORS
//   do navegador, e sem isso chegariam aqui e gravariam.
//
// Requisição sem Origin nem Sec-Fetch-Site não vem de página web (curl, script local) e
// passa: quem consegue rodar isso já tem acesso à máquina.

const LOCAL_HOSTS = new Set(['localhost', '127.0.0.1', '[::1]'])
const READ_METHODS = new Set(['GET', 'HEAD', 'OPTIONS'])

function hostname(host) {
  if (!host) return ''
  const name = host.startsWith('[') ? host.slice(0, host.indexOf(']') + 1)
    : host.split(':')[0]
  return name.toLowerCase()
}

// Devolve o motivo da recusa, ou null quando a requisição pode seguir.
export function rejectReason({ method, host, origin, secFetchSite }) {
  if (!LOCAL_HOSTS.has(hostname(host))) return `host não permitido: ${host || '(vazio)'}`
  if (READ_METHODS.has((method || 'GET').toUpperCase())) return null
  if (secFetchSite && !['same-origin', 'none'].includes(secFetchSite))
    return `requisição de outro site (${secFetchSite})`
  if (origin && origin !== `http://${host}` && origin !== `https://${host}`)
    return `origem não permitida: ${origin}`
  return null
}
