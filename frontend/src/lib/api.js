async function getJSON(url) {
  const r = await fetch(`${url}?t=${Date.now()}`, { cache: 'no-store' })
  if (!r.ok) {
    let m = `${url} (${r.status})`
    try { m = (await r.json()).error || m } catch { /* noop */ }
    throw new Error(m)
  }
  return r.json()
}
// Toda gravação leva o token da sessão do servidor (finance/server.py). Ele muda quando o
// servidor reinicia; aí a primeira gravação volta 403 e repete com o token novo.
let session = null
const sessionToken = (renew = false) => {
  if (renew || !session) session = getJSON('/api/session').then((s) => s.token)
    .catch((e) => { session = null; throw e })
  return session
}

async function postJSON(url, body, retried = false) {
  const r = await fetch(url, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json',
      'X-Finance-Token': await sessionToken(retried) },
    body: JSON.stringify(body),
  })
  const data = await r.json().catch(() => ({}))
  if (r.status === 403 && !retried && /token da sessão/.test(data.error || ''))
    return postJSON(url, body, true)
  if (!r.ok || data.ok === false) {
    throw new Error(data.error || `Falha (${r.status})`)
  }
  return data
}

export const getDashboard = () => getJSON('/data/dashboard.json')
export const getMonth = (m) => getJSON(`/data/${m}.json`)
export const getQueue = () => getJSON('/api/queue')
export const getRules = () => getJSON('/data/rules.json')
export const removeQueue = (index) => postJSON('/api/queue/remove', { index })
// patch: { note?, suggestion?: {category, subcategory} | null }
export const updateQueue = (index, patch) =>
  postJSON('/api/queue/update', { index, ...patch })

// payload: { mode:'value'|'rule'|'queue', ids[], category, subcategory,
//            rule?{field,match,value,type}, note?, samples? }
export const postEdit = (payload) => postJSON('/api/edit', payload)

// budgets = { income_plan, spending:{recurring,months}, savings_goals }
export const postBudget = (budgets) => postJSON('/api/budget', budgets)

// ---- espaço Investimentos ----------------------------------------------------
export const getInvest = () => getJSON('/data/invest.json')
export const getInvestPending = () => getJSON('/api/invest/pending')
// payload = { trades?, assets?, accounts?, policy?, targets?, locked?, balances?,
//             contribution?, contribution_mode?, allocation_sim? }
export const postInvest = (payload) => postJSON('/api/invest/apply', payload)
export const refreshInvest = () => postJSON('/api/invest/refresh', {})
export const syncInvest = (opts = {}) => postJSON('/api/invest/sync', opts)
