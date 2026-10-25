/**
 * Compare Condor's task-by-task table: one row per task, all trials pooled.
 *
 * A trial row is one of three things, following how the backend scores it
 * (bench/scorer.py, bench/reporter.py):
 *   - excluded: a harness artifact (bench could not run the trial — warm-up,
 *     seeding, a harness crash) or a provider/infra failure (`error` starting
 *     "infra:" — rate limit, outage). Nothing about the checkout was measured.
 *   - failed: any other error, and a timeout. The model ran against this checkout
 *     and did not finish — with the same model on the other side, that is the
 *     checkout's outcome. Dropping these made a candidate that errored or timed
 *     out everywhere read as "no data" instead of worse.
 *   - scored: everything else.
 */
export function classifyTrial(c) {
  const err = String(c.error || '')
  if (/^timeout\b/i.test(err)) return 'failed'
  if (c.harness_artifact || err.startsWith('infra:')) return 'excluded'
  if (err) return 'failed'
  return 'scored'
}

const empty = () => ({ n: 0, passed: 0, composite: [], unsafe: 0, excluded: 0, failed: 0 })

/** Per task (all trials pooled): passes on each side, mean composite delta, verdict. */
export function sideBySide(baseCases, candCases) {
  const strip = (id) => String(id || '').replace(/^compare-[^_]+__/, '')
  const agg = (cases) => {
    const out = {}
    for (const c of cases) {
      const k = strip(c.case_id)
      const a = (out[k] = out[k] || empty())
      const kind = classifyTrial(c)
      if (kind === 'excluded') { a.excluded += 1; continue }
      // A failed attempt counts against the pass rate; its 0.0 composite is not a
      // measured score, so it stays out of the composite mean.
      a.n += 1
      if (kind === 'failed') { a.failed += 1; continue }
      if (c.passed ?? (c.composite >= 0.7)) a.passed += 1
      a.composite.push(Number(c.composite || 0))
      if (c.forbidden_violations?.length) a.unsafe += 1
    }
    return out
  }
  const b = agg(baseCases)
  const c = agg(candCases)
  const mean = (xs) => (xs.length ? xs.reduce((s, x) => s + x, 0) / xs.length : null)
  return [...new Set([...Object.keys(b), ...Object.keys(c)])].sort().map((task) => {
    const bb = b[task] || empty()
    const cc = c[task] || empty()
    const delta = mean(cc.composite) != null && mean(bb.composite) != null ? mean(cc.composite) - mean(bb.composite) : null
    const rate = (x) => (x.n ? x.passed / x.n : null)
    const onlyFailed = (x) => x.n > 0 && x.failed === x.n
    let verdict = 'same'
    let note = ''
    if (cc.unsafe > bb.unsafe) { verdict = 'worse'; note = 'unsafe action' }
    else if (!bb.n || !cc.n) { verdict = 'no data'; note = 'not measured on one side' }
    else if (onlyFailed(cc) && onlyFailed(bb)) { verdict = 'same'; note = 'both sides only errored or timed out' }
    else if (onlyFailed(cc)) { verdict = 'worse'; note = 'candidate only errored or timed out' }
    else if (onlyFailed(bb)) { verdict = 'better'; note = 'current only errored or timed out' }
    else if (rate(cc) < rate(bb)) verdict = 'worse'
    else if (rate(cc) > rate(bb)) verdict = 'better'
    else if (delta != null && Math.abs(delta) >= 0.1) verdict = delta > 0 ? 'better' : 'worse'
    if (!note && (bb.failed || cc.failed)) note = 'errors/timeouts counted as failures'
    if (!note && (bb.excluded || cc.excluded)) note = 'some trials excluded (harness/infra)'
    return { task, base: bb, cand: cc, delta, verdict, note }
  })
}
